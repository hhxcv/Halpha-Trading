"""App boundary for plans and activation transactions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

import psycopg
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    TypeAdapter,
    field_validator,
    model_validator,
)

from halpha.app.plan_ai_review import (
    PLAN_AI_REVIEW_PROMPT_VERSION,
    PlanAiReviewConfiguration,
    PlanAiReviewRecord,
    PlanAiReviewResult,
    PlanAiReviewStatus,
    PostgreSQLPlanAiReviewStore,
    plan_ai_review_approval_is_current,
)

from halpha.binance_contracts import (
    BINANCE_USDM_ACCOUNT_POSITION_ORDER_QUERY_PATHS,
    BINANCE_USDM_ACCOUNT_SNAPSHOT_QUERY_PATHS,
    BINANCE_USDM_ACCOUNT_SNAPSHOT_SCHEMA,
    parse_complete_binance_usdm_account_snapshot,
)
from halpha.capital.models import (
    ACCOUNT_SYSTEM_STOP_RELEASE_EVIDENCE_MAX_AGE,
    AccountSystemStopReleaseRequest,
    AccountSystemStopSource,
    AuthorityClass,
    EnvironmentKind,
    NewRiskDisciplinePolicy,
    StopCategory,
    StopStateVersion,
)
from halpha.capital.repository import CapitalConflict, PostgreSQLCapitalRepository
from halpha.capital.service import CapitalApplicationService
from halpha.domain_values import canonical_decimal, content_digest, decimal_from_string
from halpha.live_write_gate import (
    GateStatusProvider,
    LiveWriteGateError,
    LiveWriteGateStatus,
    closed_live_write_gate_status,
    require_live_activation_safety_index,
)
from halpha.outcomes.trade_result import summarize_trade_result
from halpha.position_attribution import (
    account_instrument_attribution_from_rows,
)
from halpha.planning.models import (
    POSITION_ALIGNMENT_ALLOWED_ACTIONS,
    PlanCreatorKind,
    PlanDecisionContext,
    PlanDecisionIntent,
    PlanSetupFamily,
    PositionAlignmentSpec,
    RequestedLimits,
    TradePlanContent,
    TradePlanVersion,
    new_risk_decision_context_incompatibility,
    validate_current_plan_admission,
)
from halpha.planning.order_schedule import (
    OrderSchedulePreview,
    OrderScheduleSpec,
    direct_allowed_action_profiles,
    validate_current_order_schedule_support,
    validate_new_direct_execution_schedule,
)
from halpha.planning.live_profit_qualification import (
    LiveProfitQualificationStatus,
    evaluate_live_profit_qualification,
    require_live_profit_qualification,
)
from halpha.planning.control_service import ActivationControlService
from halpha.planning.registry import (
    DIRECT_EXECUTION_REF,
    DecisionBasisKind,
    Direction,
    DraftDecisionBasis,
    FixedDecisionBasis,
    FixedStrategyPlanBasis,
    describe_strategy,
    list_strategies,
    strategy_parameter_schema,
    strategy_decision_intent_incompatibility,
)
from halpha.planning.repository import (
    PlanningConflict,
    PostgreSQLPlanningRepository,
    load_persisted_order_schedule_spec,
)
from halpha.planning.service import (
    PlanningApplicationService,
    plan_runtime_incompatibility,
)
from halpha.planning.transitions import ControlIntent
from halpha.scalping.models import (
    SCALP_WORKFLOW_KIND,
    ScalpCycleCreateResponse,
    ScalpCycleRecord,
    ScalpResultsResponse,
    ScalpTriggerPayload,
    scalp_playbook_ref,
)
from halpha.scalping.repository import PostgreSQLScalpRepository
from halpha.scalping.results import summarize_scalp_results
from halpha.user_workbench.commands import build_command
from halpha.user_workbench.repository import PostgreSQLCommandRepository


_FIXED_DECISION_BASIS_ADAPTER = TypeAdapter(FixedDecisionBasis)
LiveProfitQualificationProvider = Callable[
    [TradePlanVersion, datetime],
    LiveProfitQualificationStatus,
]


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanDraftPayload(ApiModel):
    plan_name: str
    decision_context: PlanDecisionContext
    decision_basis: DraftDecisionBasis
    order_schedule_spec: OrderScheduleSpec | None = None
    position_alignment: PositionAlignmentSpec | None = None
    venue_ref: Literal["BINANCE_USDM"] = "BINANCE_USDM"
    instrument_ref: str
    direction: str
    target_exposure: str
    max_margin: str
    max_notional: str
    max_allowed_loss: str
    valid_minutes: int = Field(ge=15, le=10080)

    @field_validator("plan_name")
    @classmethod
    def readable_plan_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 80:
            raise ValueError("PLAN_NAME_INVALID")
        return normalized

    @field_validator(
        "target_exposure",
        "max_margin",
        "max_notional",
        "max_allowed_loss",
    )
    @classmethod
    def exact_positive_decimal(cls, value: str) -> str:
        return canonical_decimal(
            decimal_from_string(value, code="PLAN_VALUE_INVALID", positive=True)
        )

    @model_validator(mode="after")
    def decision_basis_is_supported(self) -> PlanDraftPayload:
        if self.position_alignment is not None:
            if (
                self.decision_basis.kind is not DecisionBasisKind.DIRECT_EXECUTION
                or self.order_schedule_spec is not None
                or self.position_alignment.instrument_ref != self.instrument_ref
                or self.position_alignment.direction.value != self.direction
                or self.position_alignment.venue_ref != self.venue_ref
            ):
                raise ValueError("POSITION_ALIGNMENT_PLAN_SHAPE_INVALID")
            return self
        if (
            self.decision_basis.kind is DecisionBasisKind.STRATEGY_SIGNAL
            and self.decision_basis.parameters.get("demo_immediate_entry") is True
            and self.decision_context.intent is not PlanDecisionIntent.VALIDATION
        ):
            raise ValueError("DEMO_FLOW_CHECK_REQUIRES_VALIDATION_INTENT")
        if self.decision_basis.kind is DecisionBasisKind.STRATEGY_SIGNAL:
            try:
                definition = describe_strategy(self.decision_basis.decision_basis_ref)
            except KeyError:
                raise ValueError("STRATEGY_UNAVAILABLE") from None
            intent_incompatibility = strategy_decision_intent_incompatibility(
                definition.economic_scope,
                (
                    self.decision_context.intent.value
                    if self.decision_context.intent is not None
                    else None
                ),
            )
            if intent_incompatibility is not None:
                raise ValueError(intent_incompatibility)
        validate_current_order_schedule_support(
            self.decision_basis.kind,
            self.order_schedule_spec,
        )
        if self.decision_basis.kind is DecisionBasisKind.DIRECT_EXECUTION:
            validate_new_direct_execution_schedule(self.order_schedule_spec)
        return self

    def resolved_decision_basis(self) -> DraftDecisionBasis:
        basis = self.decision_basis
        if basis.kind is DecisionBasisKind.STRATEGY_SIGNAL:
            return basis.model_copy(
                update={"parameters": {**basis.parameters, "direction": self.direction}}
            )
        return basis


class PlanCreatePayload(PlanDraftPayload):
    creator_kind: PlanCreatorKind


class ActivationPayload(ApiModel):
    plan_version_id: str
    expected_schedule_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


class ControlPayload(ApiModel):
    expected_version: int = Field(gt=0)
    takeover_scope: dict[str, Any] = Field(default_factory=dict)


class SystemStopReleasePayload(ApiModel):
    expected_stop_version: int = Field(gt=0)
    confirmation: Literal["USER_CONFIRMED_SYSTEM_STOP_RELEASE"]


class PlanningApiUnavailable(RuntimeError):
    pass


def _stable_id(environment_id: str, kind: str, idempotency_key: str) -> str:
    if (
        not isinstance(idempotency_key, str)
        or not idempotency_key
        or len(idempotency_key) > 160
        or any(character.isspace() for character in idempotency_key)
    ):
        raise ValueError("IDEMPOTENCY_KEY_INVALID")
    return str(
        uuid5(NAMESPACE_URL, f"urn:halpha:{environment_id}:{kind}:{idempotency_key}")
    )


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _iso_value(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


CONTINUITY_RESUME_EVIDENCE_MAX_AGE = timedelta(seconds=30)


def _continuity_resume_evidence(
    current: dict[str, Any],
    *,
    observed_at: datetime,
) -> dict[str, Any]:
    """Accept only a fresh target-scoped position proof produced by EXE."""

    activation = dict(current.get("activation") or {})
    position = dict(current.get("position_attribution") or {})
    actions = list(current.get("execution_actions") or ())
    denial_reasons: list[str] = []
    activation_id = str(activation.get("activation_id") or "")
    if (
        activation.get("run_state") != "PAUSED"
        or activation.get("pause_reason") != "WRITER_CONTINUITY_LOST"
    ):
        denial_reasons.append("ACTIVATION_NOT_CONTINUITY_PAUSED")
    if activation.get("entry_opportunity_consumed") is True:
        denial_reasons.append("ENTRY_OPPORTUNITY_CONSUMED")
    rule_state = activation.get("rule_state")
    deadlines = rule_state.get("deadlines") if isinstance(rule_state, dict) else None
    entry_deadline_raw = (
        deadlines.get("entry_valid_until")
        if isinstance(deadlines, dict)
        else None
    )
    if entry_deadline_raw:
        try:
            entry_deadline = datetime.fromisoformat(
                str(entry_deadline_raw).replace("Z", "+00:00")
            )
        except ValueError:
            denial_reasons.append("ENTRY_DEADLINE_INVALID")
        else:
            if entry_deadline.utcoffset() is None:
                denial_reasons.append("ENTRY_DEADLINE_INVALID")
            elif entry_deadline <= observed_at:
                denial_reasons.append("ENTRY_WINDOW_EXPIRED")
    if position.get("reconciliation_status") != "MATCH":
        denial_reasons.append("POSITION_ATTRIBUTION_NOT_RECONCILED")
    if position.get("fact_activation_id") != activation_id:
        denial_reasons.append("POSITION_FACT_ACTIVATION_MISMATCH")
    fact_ref = position.get("fact_ref")
    fact_digest = position.get("fact_digest")
    if not isinstance(fact_ref, str) or not isinstance(fact_digest, str):
        denial_reasons.append("POSITION_FACT_IDENTITY_UNKNOWN")
    try:
        paused_at = datetime.fromisoformat(
            str(activation["paused_at"]).replace("Z", "+00:00")
        )
        fact_cutoff = datetime.fromisoformat(
            str(position["fact_cutoff"]).replace("Z", "+00:00")
        )
    except (KeyError, TypeError, ValueError):
        paused_at = None
        fact_cutoff = None
        denial_reasons.append("POSITION_FACT_TIME_UNKNOWN")
    if paused_at is not None and fact_cutoff is not None:
        if (
            paused_at.utcoffset() is None
            or fact_cutoff.utcoffset() is None
            or fact_cutoff < paused_at
        ):
            denial_reasons.append("POSITION_FACT_PREDATES_PAUSE")
        elif (
            fact_cutoff > observed_at
            or observed_at - fact_cutoff > CONTINUITY_RESUME_EVIDENCE_MAX_AGE
        ):
            denial_reasons.append("POSITION_FACT_STALE")
    action_basis: list[tuple[str, str, int, str]] = []
    for item in actions:
        action = dict(item)
        action_id = str(action.get("execution_action_id") or "")
        state = str(action.get("state") or "")
        updated_at_text = str(action.get("updated_at") or "")
        try:
            state_version = int(action["state_version"])
            action_updated_at = datetime.fromisoformat(
                updated_at_text.replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError):
            denial_reasons.append("ACTION_STATE_EVIDENCE_INVALID")
            continue
        if state in {"SUBMITTING", "UNKNOWN"}:
            denial_reasons.append("ACTION_RESULT_UNRESOLVED")
        if fact_cutoff is not None and action_updated_at > fact_cutoff:
            denial_reasons.append("ACTION_STATE_NEWER_THAN_POSITION_FACT")
        action_basis.append(
            (action_id, state, state_version, action_updated_at.isoformat())
        )
    evidence_basis = {
        "environment_id": activation.get("environment_id"),
        "activation_id": activation_id,
        "activation_state_version": activation.get("state_version"),
        "paused_at": activation.get("paused_at"),
        "position_fact_ref": fact_ref,
        "position_fact_digest": fact_digest,
        "position_fact_cutoff": position.get("fact_cutoff"),
        "activation_signed_position": position.get(
            "activation_signed_position"
        ),
        "attributed_account_signed_position": position.get(
            "attributed_account_signed_position"
        ),
        "venue_account_signed_position": position.get(
            "venue_account_signed_position"
        ),
        "actions": sorted(action_basis),
    }
    return {
        "eligible": not denial_reasons,
        "denial_reasons": sorted(set(denial_reasons)),
        "reconciliation_digest": (
            content_digest(evidence_basis) if not denial_reasons else None
        ),
        "evidence_cutoff": (
            fact_cutoff.isoformat() if fact_cutoff is not None else None
        ),
    }


def _fixed_decision_basis_projection(version: Any) -> dict[str, Any]:
    basis = version.decision_basis
    payload = basis.model_dump(mode="json")
    return {
        "model": basis,
        "payload": payload,
        "kind": basis.kind.value,
        "decision_basis_ref": basis.decision_basis_ref,
        "parameter_digest": str(basis.parameter_digest),
        "normalized_parameters": dict(basis.normalized_parameters),
        "product_build_id": str(basis.product_build_id),
    }


class PostgreSQLPlanningApi:
    def __init__(
        self,
        *,
        database_name: str,
        database_role_name: str,
        password: SecretStr,
        environment_id: str,
        environment_kind: str,
        authority_class: str,
        account_ref: str,
        product_build_id: str,
        profile: str | None = None,
        new_risk_discipline_policy: NewRiskDisciplinePolicy | None = None,
        gate_status_provider: GateStatusProvider | None = None,
        venue_account_type: str | None = None,
        live_profit_qualification_provider: (
            LiveProfitQualificationProvider | None
        ) = None,
        scalp_executor_ready_provider: Callable[[], bool] | None = None,
    ) -> None:
        self._database_name = database_name
        self._database_role_name = database_role_name
        self._password = password
        self._environment_id = environment_id
        self._environment_kind = EnvironmentKind(environment_kind)
        self._authority_class = AuthorityClass(authority_class)
        self._account_ref = account_ref
        self._product_build_id = product_build_id
        self._profile = profile or (
            "BINANCE_DEMO"
            if self._environment_kind is EnvironmentKind.DEMO
            else (
                "BINANCE_LIVE_WRITE"
                if self._authority_class is AuthorityClass.LIVE_REAL_CAPITAL
                else "BINANCE_LIVE_READ_ONLY"
            )
        )
        self._new_risk_discipline_policy = (
            new_risk_discipline_policy or NewRiskDisciplinePolicy()
        )
        self._venue_account_type = venue_account_type or (
            "USDM_DEMO"
            if self._environment_kind is EnvironmentKind.DEMO
            else "USDM_COPY_LEAD"
        )
        self._live_profit_qualification_provider = (
            live_profit_qualification_provider
        )
        self._scalp_executor_ready_provider = scalp_executor_ready_provider
        self._gate_status_provider = (
            gate_status_provider or closed_live_write_gate_status
        )

    def _gate_status(self) -> LiveWriteGateStatus:
        try:
            return self._gate_status_provider()
        except Exception:
            return closed_live_write_gate_status()

    def _live_profit_qualification(
        self,
        version: TradePlanVersion,
        observed_at: datetime,
    ) -> LiveProfitQualificationStatus:
        if self._live_profit_qualification_provider is not None:
            return self._live_profit_qualification_provider(version, observed_at)
        return evaluate_live_profit_qualification(
            version,
            environment_kind=self._environment_kind,
            target_venue_account_type=self._venue_account_type,
            references=(),
            observed_at=observed_at,
        )

    def _require_live_profit_qualification_snapshot(
        self,
        version: TradePlanVersion,
        observed_at: datetime,
    ) -> dict[str, Any]:
        status = self._live_profit_qualification(version, observed_at)
        require_live_profit_qualification(status)
        return status.model_dump(mode="json")

    def _connect(self) -> psycopg.Connection[Any]:
        try:
            return psycopg.connect(
                host="127.0.0.1",
                port=5432,
                dbname=self._database_name,
                user=self._database_role_name,
                password=self._password.get_secret_value(),
                connect_timeout=2,
                options=(
                    "-c default_transaction_read_only=on"
                    if self._profile == "BINANCE_LIVE_READ_ONLY"
                    else None
                ),
            )
        except Exception as exc:
            raise PlanningApiUnavailable(
                f"PLANNING_DATABASE_UNAVAILABLE type={type(exc).__name__}"
            ) from None

    def _require_demo_parameter_scope(self, parameters: dict[str, Any]) -> None:
        if (
            parameters.get("demo_immediate_entry") is True
            and self._profile != "BINANCE_DEMO"
        ):
            raise ValueError("DEMO_IMMEDIATE_ENTRY_REQUIRES_DEMO")

    def _require_product_mutation_allowed(self) -> None:
        if self._profile == "BINANCE_LIVE_READ_ONLY":
            raise ValueError("LIVE_READ_ONLY_PRODUCT_MUTATION_FORBIDDEN")

    def strategies(self) -> list[dict[str, Any]]:
        return [
            {
                "strategy_id": item.strategy_id,
                "strategy_version": item.strategy_version,
                "display_name": item.display_name,
                "value_logic": item.value_logic,
                "applicable_scenarios": item.applicable_scenarios,
                "execution_behavior": item.execution_behavior,
                "parameter_schema_version": item.parameter_schema_version,
                "supported_directions": [
                    direction.value for direction in item.supported_directions
                ],
                "economic_scope": item.economic_scope,
                "plan_key_parameters": [
                    parameter.model_dump(mode="json")
                    for parameter in item.plan_key_parameters
                ],
            }
            for item in list_strategies()
        ]

    def strategy_schema(self, strategy_id: str) -> dict[str, Any]:
        return strategy_parameter_schema(strategy_id)

    def list_plans(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT d.plan_id, d.draft_version, d.content_digest, d.updated_at,
                       d.content,
                       a.activation_id, a.lifecycle, a.created_at, a.updated_at,
                       v.plan_version_id, v.fixed_at, v.content_digest,
                       v.product_build_id, v.terms ->> 'valid_until',
                       v.fixed_decision_basis, v.order_schedule_spec,
                       v.order_schedule_spec_digest, v.terms -> 'allowed_actions',
                       v.position_alignment, v.terms -> 'decision_context',
                       v.ai_review_ref,
                       CASE WHEN r.review_id IS NULL THEN NULL ELSE jsonb_build_object(
                           'review_id', r.review_id,
                           'status', r.status,
                           'decision', r.decision,
                           'draft_content_digest', r.draft_content_digest,
                           'prompt_version', r.prompt_version,
                           'configuration', jsonb_build_object(
                               'model', r.review_model,
                               'reasoning_effort', r.reasoning_effort
                           ),
                           'market_context_digest', r.market_context_digest,
                           'market_source_cutoff', r.market_source_cutoff,
                           'reason', r.reason,
                           'suggestions', r.suggestions,
                           'completed_at', r.completed_at
                ) END
                FROM halpha.trade_plan_draft d
                LEFT JOIN LATERAL (
                    SELECT activation.activation_id, activation.lifecycle,
                           activation.created_at, activation.updated_at,
                           activation.plan_version_ref
                    FROM halpha.plan_activation activation
                    JOIN halpha.trade_plan_version snapshot
                      ON snapshot.environment_id = activation.environment_id
                     AND snapshot.plan_version_id = activation.plan_version_ref
                    WHERE activation.environment_id = d.environment_id
                      AND snapshot.plan_id = d.plan_id
                    ORDER BY activation.created_at DESC, activation.activation_id DESC
                    LIMIT 1
                ) a ON true
                LEFT JOIN halpha.trade_plan_version v
                  ON v.environment_id = d.environment_id
                 AND v.plan_version_id = a.plan_version_ref
                LEFT JOIN halpha.plan_ai_review r
                  ON r.environment_id = d.environment_id
                 AND r.review_id = v.ai_review_ref
                WHERE d.environment_id = %s
                  AND COALESCE(
                    d.content -> 'terms' ->> 'workflow_kind',
                    'PLAN'
                  ) <> 'SCALP_CYCLE'
                ORDER BY d.updated_at DESC
                """,
                (self._environment_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            content = dict(row[4])
            basis = DraftDecisionBasis.model_validate(content["decision_basis"])
            requested_limits = dict(content["requested_limits"])
            runtime_incompatibility: str | None = None
            if row[9] is not None:
                try:
                    fixed_schedule_spec = load_persisted_order_schedule_spec(
                        row[15],
                        row[16],
                    )
                    runtime_incompatibility = plan_runtime_incompatibility(
                        decision_basis=_FIXED_DECISION_BASIS_ADAPTER.validate_python(
                            row[14]
                        ),
                        decision_context=(
                            PlanDecisionContext.model_validate(row[19])
                            if row[19] is not None
                            else None
                        ),
                        order_schedule_spec=fixed_schedule_spec,
                        allowed_actions=frozenset(
                            str(item) for item in (row[17] or ())
                        ),
                        position_alignment=(
                            PositionAlignmentSpec.model_validate(row[18])
                            if row[18] is not None
                            else None
                        ),
                    )
                except (TypeError, ValueError):
                    runtime_incompatibility = "PLAN_FIXED_CONTENT_UNREADABLE"
            ai_review_ref = str(row[20]) if row[20] is not None else None
            activation_lifecycle = str(row[6]) if row[6] is not None else None
            status = (
                "DRAFT"
                if row[5] is None
                else "ENDED"
                if activation_lifecycle == "COMPLETED"
                else "RUNNING"
            )
            result.append(
                {
                    "plan_id": str(row[0]),
                    "draft_version": int(row[1]),
                    "draft_content_digest": str(row[2]),
                    "updated_at": row[3].isoformat(),
                    "plan_name": content.get("plan_name"),
                    "created_at": _iso_value(content.get("created_at")),
                    "creator_kind": _enum_value(content.get("creator_kind")),
                    "decision_context": content.get("decision_context"),
                    "decision_basis": basis.model_dump(mode="json"),
                    "decision_basis_kind": basis.kind.value,
                    "decision_basis_ref": basis.decision_basis_ref,
                    "strategy_id": (
                        basis.decision_basis_ref
                        if basis.kind is DecisionBasisKind.STRATEGY_SIGNAL
                        else None
                    ),
                    "instrument_ref": str(content["instrument_ref"]),
                    "direction": str(_enum_value(content["direction"])),
                    "parameters": dict(basis.parameters),
                    "order_schedule_spec": content.get("order_schedule_spec"),
                    "position_alignment": content.get("position_alignment"),
                    "max_notional": str(requested_limits["max_notional"]),
                    "valid_from": _iso_value(content["valid_from"]),
                    "valid_until": _iso_value(content["valid_until"]),
                    "status": status,
                    "activation_id": str(row[5]) if row[5] is not None else None,
                    "activation_lifecycle": activation_lifecycle,
                    "activation_created_at": (
                        row[7].isoformat() if row[7] is not None else None
                    ),
                    "activation_updated_at": (
                        row[8].isoformat() if row[8] is not None else None
                    ),
                    "plan_version_id": str(row[9]) if row[9] is not None else None,
                    "fixed_at": row[10].isoformat() if row[10] is not None else None,
                    "fixed_content_digest": str(row[11]) if row[11] is not None else None,
                    "fixed_product_build_id": str(row[12]) if row[12] is not None else None,
                    "fixed_valid_until": str(row[13]) if row[13] is not None else None,
                    "product_build_consistent": (
                        str(row[12]) == self._product_build_id
                        if row[12] is not None
                        else None
                    ),
                    "runtime_compatible": (
                        runtime_incompatibility is None
                        if row[9] is not None
                        else None
                    ),
                    "runtime_incompatibility_reason": runtime_incompatibility,
                    "ai_review_ref": ai_review_ref,
                    "ai_review": (
                        dict(row[21]) if row[21] is not None else None
                    ),
                }
            )
        return result

    def scalp_cycle_preview(
        self,
        payload: ScalpTriggerPayload,
        *,
        idempotency_key: str,
        maker_limit_price: str | None = None,
    ) -> dict[str, Any]:
        """Return the exact server-built schedule input for one trigger request."""

        plan_version_id = _stable_id(
            self._environment_id,
            "scalp-plan-version",
            idempotency_key,
        )
        spec = payload.template.order_schedule_spec(
            maker_limit_price=maker_limit_price,
        )
        validate_new_direct_execution_schedule(spec)
        return {
            "plan_version_id": plan_version_id,
            "decision_basis_kind": DecisionBasisKind.DIRECT_EXECUTION.value,
            "venue_ref": "BINANCE_USDM",
            "instrument_ref": payload.instrument_ref,
            "direction": payload.direction.value,
            "trade_amount": payload.template.notional,
            "order_schedule_spec": spec.model_dump(mode="json"),
        }

    def scalp_cycle_replay(
        self,
        payload: ScalpTriggerPayload,
        *,
        idempotency_key: str,
    ) -> ScalpCycleCreateResponse | None:
        """Return an already-created cycle before any fresh market dependency."""

        request_digest = content_digest(
            {
                "environment_id": self._environment_id,
                "account_ref": self._account_ref,
                "instrument_ref": payload.instrument_ref,
                "direction": payload.direction,
                "template": payload.template,
            }
        )
        with self._connect() as connection, connection.transaction():
            cycle = PostgreSQLScalpRepository(
                connection,
                self._environment_id,
            ).get_by_idempotency(idempotency_key)
            if cycle is None:
                return None
            if cycle.request_digest != request_digest:
                raise ValueError("IDEMPOTENCY_CONTENT_CONFLICT")
            activation = PostgreSQLPlanningRepository(
                connection,
                self._environment_id,
            ).get_activation(cycle.activation_id)
        return ScalpCycleCreateResponse(
            cycle=cycle,
            activation=activation.model_dump(mode="json"),
            runtime_real_write_gate=self._gate_status().runtime_real_write_gate,
        )

    def scalp_cycle_lookup(
        self,
        *,
        idempotency_key: str,
    ) -> ScalpCycleCreateResponse | None:
        """Read a committed request; absence does not settle an in-flight POST."""

        _stable_id(self._environment_id, "scalp-cycle", idempotency_key)
        with self._connect() as connection, connection.transaction():
            cycle = PostgreSQLScalpRepository(
                connection,
                self._environment_id,
            ).get_by_idempotency(idempotency_key)
            if cycle is None or cycle.account_ref != self._account_ref:
                return None
            activation = PostgreSQLPlanningRepository(
                connection,
                self._environment_id,
            ).get_activation(cycle.activation_id)
        return ScalpCycleCreateResponse(
            cycle=cycle,
            activation=activation.model_dump(mode="json"),
            runtime_real_write_gate=self._gate_status().runtime_real_write_gate,
        )

    def _require_scalp_write_gate(self) -> None:
        self._require_product_mutation_allowed()
        gate_status = self._gate_status()
        if self._profile == "BINANCE_LIVE_WRITE":
            if gate_status.product_build_consistent is not True:
                raise ValueError("LIVE_WRITE_PRODUCT_BUILD_MISMATCH")
            if gate_status.violations:
                raise ValueError("LIVE_WRITE_GATE_BINDING_INVALID_FOR_ACTIVATION")
            if gate_status.runtime_real_write_gate != "OPEN":
                raise ValueError("LIVE_WRITE_GATE_MUST_BE_OPEN_FOR_SCALP")

    def trigger_scalp_cycle(
        self,
        payload: ScalpTriggerPayload,
        *,
        idempotency_key: str,
        observed_at: datetime,
        order_schedule_snapshot: OrderSchedulePreview,
        maker_limit_price: str | None = None,
    ) -> ScalpCycleCreateResponse:
        """Atomically create one bounded technical activation and cycle identity."""

        self._require_scalp_write_gate()

        plan_id = _stable_id(self._environment_id, "scalp-plan", idempotency_key)
        plan_version_id = _stable_id(
            self._environment_id,
            "scalp-plan-version",
            idempotency_key,
        )
        activation_id = _stable_id(
            self._environment_id,
            "scalp-activation",
            idempotency_key,
        )
        cycle_id = _stable_id(self._environment_id, "scalp-cycle", idempotency_key)
        request_digest = content_digest(
            {
                "environment_id": self._environment_id,
                "account_ref": self._account_ref,
                "instrument_ref": payload.instrument_ref,
                "direction": payload.direction,
                "template": payload.template,
            }
        )
        expected_spec = payload.template.order_schedule_spec(
            maker_limit_price=maker_limit_price,
        )
        protection = order_schedule_snapshot.full_fill_protection_estimate
        if (
            not order_schedule_snapshot.valid
            or order_schedule_snapshot.schedule_ref != plan_version_id
            or order_schedule_snapshot.instrument_ref != payload.instrument_ref
            or order_schedule_snapshot.direction != payload.direction
            or order_schedule_snapshot.max_notional != payload.template.notional
            or order_schedule_snapshot.schedule_spec != expected_spec
            or protection is None
        ):
            raise ValueError("SCALP_PREVIEW_MISMATCH")

        direction_label = "做多" if payload.direction is Direction.LONG else "做空"
        content = TradePlanContent(
            plan_name=(
                f"剥头皮 {payload.instrument_ref.removesuffix('-PERP')} "
                f"{direction_label} {observed_at.astimezone(ZoneInfo('Asia/Shanghai')):%H:%M:%S}"
            ),
            created_at=observed_at,
            creator_kind=PlanCreatorKind.HUMAN,
            decision_context=PlanDecisionContext(
                rationale="人工选择方向并触发剥头皮周期",
                intent=PlanDecisionIntent.PROFIT_SEEKING,
                setup_family=PlanSetupFamily.OTHER,
                playbook_ref=scalp_playbook_ref(payload.template),
                evidence_cutoff=observed_at,
            ),
            decision_basis=DraftDecisionBasis(
                kind=DecisionBasisKind.DIRECT_EXECUTION,
                decision_basis_ref=DIRECT_EXECUTION_REF,
                parameters={},
            ),
            order_schedule_spec=expected_spec,
            environment_id=self._environment_id,
            environment_kind=self._environment_kind,
            authority_class=self._authority_class,
            account_ref=self._account_ref,
            venue_ref="BINANCE_USDM",
            instrument_ref=payload.instrument_ref,
            direction=payload.direction,
            target_exposure=payload.template.notional,
            requested_limits=RequestedLimits(
                max_margin=payload.template.notional,
                max_notional=payload.template.notional,
                max_allowed_loss=protection.maximum_projected_loss,
            ),
            valid_from=observed_at,
            valid_until=observed_at
            + timedelta(seconds=max(900, payload.template.max_holding_seconds + 300)),
            allowed_actions=direct_allowed_action_profiles(expected_spec),
            terms={
                "one_entry_cycle": True,
                "resume_policy": "MANUAL_PLAN_RESUME",
                "workflow_kind": SCALP_WORKFLOW_KIND,
                "scalp_template": payload.template.model_dump(mode="json"),
                "scalp_template_digest": payload.template.digest,
                "scalp_entry_limit_price": maker_limit_price,
            },
        )

        with self._connect() as connection:
            scalp_repository = PostgreSQLScalpRepository(
                connection,
                self._environment_id,
            )
            planning_repository = PostgreSQLPlanningRepository(
                connection,
                self._environment_id,
            )
            try:
                with connection.transaction():
                    scalp_repository.lock_open_cycle_scope(
                        account_ref=self._account_ref,
                    )
                    existing_cycle = scalp_repository.get_by_idempotency(
                        idempotency_key
                    )
                    if existing_cycle is not None:
                        if existing_cycle.request_digest != request_digest:
                            raise ValueError("IDEMPOTENCY_CONTENT_CONFLICT")
                        activation = planning_repository.get_activation(
                            existing_cycle.activation_id
                        )
                        return ScalpCycleCreateResponse(
                            cycle=existing_cycle,
                            activation=activation.model_dump(mode="json"),
                            runtime_real_write_gate=(
                                self._gate_status().runtime_real_write_gate
                            ),
                        )
                    self._require_scalp_write_gate()
                    try:
                        executor_ready = (
                            self._scalp_executor_ready_provider is not None
                            and self._scalp_executor_ready_provider() is True
                        )
                    except Exception:
                        executor_ready = False
                    if not executor_ready:
                        raise ValueError("EXECUTOR_NOT_READY")
                    if self._profile == "BINANCE_LIVE_WRITE":
                        try:
                            require_live_activation_safety_index(connection)
                        except LiveWriteGateError as exc:
                            raise ValueError(str(exc)) from None
                    cycle = ScalpCycleRecord(
                        cycle_id=cycle_id,
                        environment_id=self._environment_id,
                        account_ref=self._account_ref,
                        instrument_ref=payload.instrument_ref,
                        direction=payload.direction,
                        template=payload.template,
                        template_digest=payload.template.digest,
                        request_digest=request_digest,
                        idempotency_key=idempotency_key,
                        plan_id=plan_id,
                        plan_version_id=plan_version_id,
                        activation_id=activation_id,
                        triggered_at=observed_at,
                    )
                    _version, activation = PlanningApplicationService(
                        connection,
                        self._environment_id,
                    ).create_scalp_cycle(
                        cycle=cycle,
                        content=content,
                        environment_kind=self._environment_kind,
                        authority_class=self._authority_class,
                        product_build_id=self._product_build_id,
                        order_schedule_snapshot=order_schedule_snapshot,
                        live_profit_qualification_checker=(
                            lambda version: self._require_live_profit_qualification_snapshot(
                                version,
                                observed_at,
                            )
                        ),
                        new_risk_discipline_policy=self._new_risk_discipline_policy,
                    )
            except psycopg.errors.UniqueViolation:
                connection.rollback()
                with connection.transaction():
                    existing_cycle = scalp_repository.get_by_idempotency(
                        idempotency_key
                    )
                    if (
                        existing_cycle is None
                        or existing_cycle.request_digest != request_digest
                    ):
                        raise ValueError("IDEMPOTENCY_CONTENT_CONFLICT") from None
                    activation = planning_repository.get_activation(
                        existing_cycle.activation_id
                    )
                    cycle = existing_cycle
        return ScalpCycleCreateResponse(
            cycle=cycle,
            activation=activation.model_dump(mode="json"),
            runtime_real_write_gate=self._gate_status().runtime_real_write_gate,
        )

    def scalp_results(
        self,
        *,
        scope: Literal["ALL", "TODAY", "ANCHOR"],
        anchor_at: datetime | None,
        observed_at: datetime,
    ) -> ScalpResultsResponse:
        if observed_at.utcoffset() is None:
            raise ValueError("SCALP_RESULT_TIME_INVALID")
        if scope == "ALL":
            range_start = None
        elif scope == "TODAY":
            local_now = observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
            range_start = local_now.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            ).astimezone(UTC)
        else:
            if (
                anchor_at is None
                or anchor_at.utcoffset() is None
                or anchor_at > observed_at
            ):
                raise ValueError("SCALP_RESULT_ANCHOR_INVALID")
            range_start = anchor_at.astimezone(UTC)
        with self._connect() as connection, connection.transaction():
            repository = PostgreSQLScalpRepository(
                connection,
                self._environment_id,
            )
            rows = repository.result_rows(
                account_ref=self._account_ref,
                range_start=range_start,
                range_end=observed_at,
            )
            latest = repository.latest_result_row(
                account_ref=self._account_ref,
                range_end=observed_at,
            )
        return summarize_scalp_results(
            environment_id=self._environment_id,
            account_ref=self._account_ref,
            scope=scope,
            range_start=range_start,
            range_end=observed_at.astimezone(UTC),
            rows=rows,
            latest_row=latest,
        )

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        with self._connect() as connection, connection.transaction():
            draft = PostgreSQLPlanningRepository(
                connection, self._environment_id
            ).get_draft(plan_id)
        return draft.model_dump(mode="json")

    def request_ai_review(
        self,
        plan_id: str,
        *,
        idempotency_key: str,
        expected_version: int,
        configuration: PlanAiReviewConfiguration,
        review_evidence_cutoff: datetime,
        observed_at: datetime,
    ) -> dict[str, Any]:
        """Bind a queued review to a freshly collected server market cutoff.

        ``evidence_cutoff`` is system metadata, not a browser assertion.  It
        is refreshed in the same transaction that creates the immutable review
        identity, so the reviewer never receives a current market snapshot
        paired with an old draft cutoff.
        """

        self._require_product_mutation_allowed()
        if review_evidence_cutoff.utcoffset() is None:
            raise ValueError("PLAN_AI_REVIEW_CONTEXT_INVALID")
        review_id = _stable_id(
            self._environment_id,
            "plan-ai-review",
            idempotency_key,
        )
        with self._connect() as connection, connection.transaction():
            planning = PostgreSQLPlanningRepository(
                connection,
                self._environment_id,
            )
            draft = planning.get_draft(plan_id, for_update=True)
            review_store = PostgreSQLPlanAiReviewStore(
                connection,
                self._environment_id,
            )
            existing = review_store.by_idempotency_key(idempotency_key)
            if existing is not None:
                if (
                    existing.plan_id != plan_id
                    or existing.configuration != configuration
                ):
                    raise ValueError("IDEMPOTENCY_CONTENT_CONFLICT")
                return existing.model_dump(mode="json")
            if draft.draft_version != expected_version:
                raise ValueError("PLAN_VERSION_CONFLICT")
            if planning.has_activation(plan_id):
                raise ValueError("PLAN_ALREADY_STARTED")
            if draft.content.position_alignment is not None:
                raise ValueError("PLAN_AI_REVIEW_NOT_APPLICABLE")
            decision_context = draft.content.decision_context
            if decision_context is None:
                raise ValueError("PLAN_DECISION_EXPERIMENT_INCOMPLETE")
            review_content = draft.content.model_copy(
                update={
                    "decision_context": decision_context.model_copy(
                        update={"evidence_cutoff": review_evidence_cutoff}
                    )
                }
            )
            draft = PlanningApplicationService(
                connection,
                self._environment_id,
            ).update_draft(
                plan_id=plan_id,
                expected_version=expected_version,
                content=review_content,
                observed_at=observed_at,
                preserve_system_evidence_cutoff=False,
            )
            context_incompatibility = new_risk_decision_context_incompatibility(
                decision_basis_kind=draft.content.decision_basis.kind,
                decision_context=draft.content.decision_context,
            )
            if context_incompatibility is not None:
                raise ValueError(context_incompatibility)
            validate_current_plan_admission(
                decision_basis_kind=draft.content.decision_basis.kind,
                order_schedule_spec=draft.content.order_schedule_spec,
                allowed_actions=draft.content.allowed_actions,
                position_alignment=draft.content.position_alignment,
            )
            review = review_store.create_queued(
                review_id=review_id,
                plan_id=plan_id,
                draft_version=draft.draft_version,
                draft_content_digest=draft.content_digest,
                configuration=configuration,
                idempotency_key=idempotency_key,
                observed_at=observed_at,
            )
        return review.model_dump(mode="json")

    def get_ai_review(self, review_id: str) -> dict[str, Any]:
        with self._connect() as connection, connection.transaction():
            review = PostgreSQLPlanAiReviewStore(
                connection,
                self._environment_id,
            ).get(review_id)
        return review.model_dump(mode="json")

    def latest_ai_review(self, plan_id: str) -> dict[str, Any] | None:
        with self._connect() as connection, connection.transaction():
            review = PostgreSQLPlanAiReviewStore(
                connection,
                self._environment_id,
            ).latest_for_plan(plan_id)
        return review.model_dump(mode="json") if review is not None else None

    def mark_ai_review_running(
        self,
        review_id: str,
        market_context_digest: str,
        market_source_cutoff: datetime,
    ) -> PlanAiReviewRecord:
        observed_at = datetime.now(UTC)
        with self._connect() as connection, connection.transaction():
            return PostgreSQLPlanAiReviewStore(
                connection,
                self._environment_id,
            ).mark_running(
                review_id,
                market_context_digest=market_context_digest,
                market_source_cutoff=market_source_cutoff,
                observed_at=observed_at,
            )

    def update_ai_review_progress(
        self,
        review_id: str,
        message: str,
        public_output: str,
    ) -> PlanAiReviewRecord:
        with self._connect() as connection, connection.transaction():
            return PostgreSQLPlanAiReviewStore(
                connection,
                self._environment_id,
            ).update_progress(
                review_id,
                message=message,
                public_output=public_output,
                observed_at=datetime.now(UTC),
            )

    def complete_ai_review(
        self,
        review_id: str,
        result: PlanAiReviewResult,
    ) -> PlanAiReviewRecord:
        with self._connect() as connection, connection.transaction():
            return PostgreSQLPlanAiReviewStore(
                connection,
                self._environment_id,
            ).complete(
                review_id,
                result=result,
                observed_at=datetime.now(UTC),
            )

    def fail_ai_review(
        self,
        review_id: str,
        failure_code: str,
    ) -> PlanAiReviewRecord:
        with self._connect() as connection, connection.transaction():
            return PostgreSQLPlanAiReviewStore(
                connection,
                self._environment_id,
            ).fail(
                review_id,
                failure_code=failure_code,
                observed_at=datetime.now(UTC),
            )

    @staticmethod
    def _review_activation_snapshot(
        review: PlanAiReviewRecord,
    ) -> dict[str, Any]:
        return {
            "review_id": review.review_id,
            "status": review.status.value,
            "decision": review.decision.value if review.decision is not None else None,
            "draft_content_digest": review.draft_content_digest,
            "prompt_version": review.prompt_version,
            "configuration": review.configuration.model_dump(mode="json"),
            "market_context_digest": review.market_context_digest,
            "market_source_cutoff": (
                review.market_source_cutoff.isoformat()
                if review.market_source_cutoff is not None
                else None
            ),
            "reason": review.reason,
            "suggestions": list(review.suggestions),
            "completed_at": (
                review.completed_at.isoformat()
                if review.completed_at is not None
                else None
            ),
        }

    def _stored_ai_review_snapshot(
        self,
        connection: psycopg.Connection[Any],
        review_id: str | None,
    ) -> dict[str, Any] | None:
        """Read the one immutable review bound to a fixed plan version.

        UI readers must not use ``latest_for_plan`` here: a later draft can have
        a newer review that does not authorize the fixed or activated version.
        """

        if not review_id:
            return None
        try:
            review = PostgreSQLPlanAiReviewStore(
                connection,
                self._environment_id,
            ).get(review_id)
        except ValueError:
            # Historical fixed versions remain readable even when an older
            # installation did not retain a review row.
            return None
        return self._review_activation_snapshot(review)

    def _approved_current_draft_ai_review(
        self,
        connection: psycopg.Connection[Any],
        draft: Any,
        *,
        observed_at: datetime,
    ) -> PlanAiReviewRecord:
        """Return the exact, still-current review that may start this draft."""

        review = PostgreSQLPlanAiReviewStore(
            connection,
            self._environment_id,
        ).approved_for_draft(
            plan_id=draft.plan_id,
            draft_version=draft.draft_version,
            draft_content_digest=draft.content_digest,
        )
        if review is None:
            raise ValueError("PLAN_AI_REVIEW_REQUIRED")
        if not plan_ai_review_approval_is_current(
            review,
            observed_at=observed_at,
        ):
            raise ValueError("PLAN_AI_REVIEW_EXPIRED")
        return review

    def _require_approved_version_ai_review(
        self,
        connection: psycopg.Connection[Any],
        version: TradePlanVersion,
        *,
        observed_at: datetime | None = None,
        require_current_market_context: bool = False,
    ) -> dict[str, Any]:
        if getattr(version, "position_alignment", None) is not None:
            return {}
        ai_review_ref = getattr(version, "ai_review_ref", None)
        if not ai_review_ref:
            raise ValueError("PLAN_AI_REVIEW_REQUIRED")
        review = PostgreSQLPlanAiReviewStore(
            connection,
            self._environment_id,
        ).get(ai_review_ref)
        draft = PostgreSQLPlanningRepository(
            connection,
            self._environment_id,
        ).get_draft(version.plan_id)
        if (
            review.plan_id != version.plan_id
            or review.draft_version != draft.draft_version
            or review.draft_content_digest != draft.content_digest
            or review.prompt_version != PLAN_AI_REVIEW_PROMPT_VERSION
            or review.status is not PlanAiReviewStatus.APPROVED
            or review.decision is None
            or review.decision.value != "APPROVE"
        ):
            raise ValueError("PLAN_AI_REVIEW_NOT_APPROVED")
        if require_current_market_context:
            if observed_at is None or not plan_ai_review_approval_is_current(
                review,
                observed_at=observed_at,
            ):
                raise ValueError("PLAN_AI_REVIEW_EXPIRED")
        return self._review_activation_snapshot(review)

    def get_plan_version(self, plan_version_id: str) -> dict[str, Any]:
        with self._connect() as connection, connection.transaction():
            version = PostgreSQLPlanningRepository(
                connection, self._environment_id
            ).get_version(plan_version_id)
        return version.model_dump(mode="json")

    def save_new_plan(
        self,
        payload: PlanCreatePayload,
        *,
        idempotency_key: str,
        observed_at: datetime,
    ) -> dict[str, Any]:
        self._require_product_mutation_allowed()
        basis = payload.resolved_decision_basis()
        if basis.kind is DecisionBasisKind.STRATEGY_SIGNAL:
            definition = describe_strategy(basis.decision_basis_ref)
            if payload.direction not in {
                item.value for item in definition.supported_directions
            }:
                raise ValueError("PARAMETER_INVALID")
            allowed_actions = frozenset(
                definition.allowed_action_profiles
            )
            self._require_demo_parameter_scope(basis.parameters)
        else:
            allowed_actions = (
                POSITION_ALIGNMENT_ALLOWED_ACTIONS
                if payload.position_alignment is not None
                else direct_allowed_action_profiles(payload.order_schedule_spec)
            )
        plan_id = _stable_id(self._environment_id, "plan", idempotency_key)
        content = TradePlanContent(
            plan_name=payload.plan_name,
            created_at=observed_at,
            creator_kind=payload.creator_kind,
            # The browser may display a current market cutoff while the owner
            # edits, but this is server-owned review metadata.  A new draft
            # receives its authoritative cutoff only when an AI review collects
            # its fresh, bounded market context.
            decision_context=payload.decision_context.model_copy(
                update={"evidence_cutoff": None}
            ),
            decision_basis=basis,
            order_schedule_spec=payload.order_schedule_spec,
            position_alignment=payload.position_alignment,
            environment_id=self._environment_id,
            environment_kind=self._environment_kind,
            authority_class=self._authority_class,
            account_ref=self._account_ref,
            venue_ref=payload.venue_ref,
            instrument_ref=payload.instrument_ref,
            direction=payload.direction,
            target_exposure=payload.target_exposure,
            requested_limits=RequestedLimits(
                max_margin=payload.max_margin,
                max_notional=payload.max_notional,
                max_allowed_loss=payload.max_allowed_loss,
            ),
            valid_from=observed_at,
            valid_until=observed_at + timedelta(minutes=payload.valid_minutes),
            allowed_actions=allowed_actions,
            terms={
                "one_entry_cycle": True,
                "resume_policy": "MANUAL_PLAN_RESUME",
            },
        )
        with self._connect() as connection:
            repository = PostgreSQLPlanningRepository(connection, self._environment_id)
            service = PlanningApplicationService(connection, self._environment_id)
            try:
                with connection.transaction():
                    draft = service.create_draft(
                        plan_id=plan_id,
                        content=content,
                        observed_at=observed_at,
                    )
            except PlanningConflict:
                with connection.transaction():
                    draft = repository.get_draft(plan_id)
                request_duration = content.valid_until - content.valid_from
                comparable = content.model_copy(
                    update={
                        "created_at": draft.content.created_at,
                        "valid_from": draft.content.valid_from,
                        "valid_until": draft.content.valid_from + request_duration,
                    }
                )
                if content_digest(draft.content) != content_digest(comparable):
                    raise ValueError("IDEMPOTENCY_CONTENT_CONFLICT") from None
        return draft.model_dump(mode="json")

    def update_plan(
        self,
        plan_id: str,
        payload: PlanDraftPayload,
        *,
        expected_version: int,
        observed_at: datetime,
    ) -> dict[str, Any]:
        self._require_product_mutation_allowed()
        basis = payload.resolved_decision_basis()
        if basis.kind is DecisionBasisKind.STRATEGY_SIGNAL:
            definition = describe_strategy(basis.decision_basis_ref)
            if payload.direction not in {
                item.value for item in definition.supported_directions
            }:
                raise ValueError("PARAMETER_INVALID")
            allowed_actions = frozenset(
                definition.allowed_action_profiles
            )
            self._require_demo_parameter_scope(basis.parameters)
        else:
            allowed_actions = (
                POSITION_ALIGNMENT_ALLOWED_ACTIONS
                if payload.position_alignment is not None
                else direct_allowed_action_profiles(payload.order_schedule_spec)
            )
        content = TradePlanContent(
            plan_name=payload.plan_name,
            decision_context=payload.decision_context,
            decision_basis=basis,
            order_schedule_spec=payload.order_schedule_spec,
            position_alignment=payload.position_alignment,
            environment_id=self._environment_id,
            environment_kind=self._environment_kind,
            authority_class=self._authority_class,
            account_ref=self._account_ref,
            venue_ref=payload.venue_ref,
            instrument_ref=payload.instrument_ref,
            direction=payload.direction,
            target_exposure=payload.target_exposure,
            requested_limits=RequestedLimits(
                max_margin=payload.max_margin,
                max_notional=payload.max_notional,
                max_allowed_loss=payload.max_allowed_loss,
            ),
            valid_from=observed_at,
            valid_until=observed_at + timedelta(minutes=payload.valid_minutes),
            allowed_actions=allowed_actions,
            terms={"one_entry_cycle": True, "resume_policy": "MANUAL_PLAN_RESUME"},
        )
        with self._connect() as connection, connection.transaction():
            draft = PlanningApplicationService(
                connection, self._environment_id
            ).update_draft(
                plan_id=plan_id,
                expected_version=expected_version,
                content=content,
                observed_at=observed_at,
            )
        return draft.model_dump(mode="json")

    def delete_plan(self, plan_id: str, *, expected_version: int) -> dict[str, Any]:
        self._require_product_mutation_allowed()
        with self._connect() as connection, connection.transaction():
            PlanningApplicationService(
                connection, self._environment_id
            ).delete_draft(
                plan_id=plan_id,
                expected_version=expected_version,
            )
        return {
            "result": "APPLIED",
            "plan_id": plan_id,
            "deleted_draft_version": expected_version,
        }

    def fix_plan(
        self,
        plan_id: str,
        *,
        idempotency_key: str,
        expected_version: int,
        observed_at: datetime,
    ) -> dict[str, Any]:
        """Compatibility-only historical snapshot writer.

        The normal workbench path is ``submit_and_start``.  It is deliberately
        not exposed as a public HTTP command, because creating a snapshot
        without its activation would reintroduce a fourth lifecycle state.
        """
        self._require_product_mutation_allowed()
        plan_version_id = _stable_id(
            self._environment_id, "plan-version", idempotency_key
        )
        with self._connect() as connection:
            repository = PostgreSQLPlanningRepository(connection, self._environment_id)
            try:
                with connection.transaction():
                    draft = repository.get_draft(plan_id, for_update=True)
                    if draft.draft_version != expected_version:
                        raise ValueError("PLAN_VERSION_CONFLICT")
                    approved_review = (
                        None
                        if draft.content.position_alignment is not None
                        else self._approved_current_draft_ai_review(
                            connection,
                            draft,
                            observed_at=observed_at,
                        )
                    )
                    version = PlanningApplicationService(
                        connection, self._environment_id
                    ).fix_draft(
                        plan_id=plan_id,
                        expected_draft_version=expected_version,
                        plan_version_id=plan_version_id,
                        product_build_id=self._product_build_id,
                        fixed_at=observed_at,
                        ai_review_ref=(
                            approved_review.review_id
                            if approved_review is not None
                            else None
                        ),
                        new_risk_discipline_policy=(
                            self._new_risk_discipline_policy
                        ),
                    )
            except psycopg.errors.UniqueViolation:
                connection.rollback()
                with connection.transaction():
                    version = repository.get_version(plan_version_id)
                if version.plan_id != plan_id:
                    raise ValueError("IDEMPOTENCY_CONTENT_CONFLICT") from None
        return version.model_dump(mode="json")

    def _activation_preview_for_version(
        self,
        connection: psycopg.Connection[Any],
        version: TradePlanVersion,
        *,
        previewed_at: datetime,
    ) -> dict[str, Any]:
        """Build a current-fact preview for a persisted or transient snapshot."""

        position_alignment = getattr(version, "position_alignment", None)
        ai_review: dict[str, Any] | None = None
        ai_review_blocker: str | None = None
        if position_alignment is None:
            try:
                ai_review = self._require_approved_version_ai_review(
                    connection,
                    version,
                )
            except ValueError as exc:
                ai_review_blocker = str(exc)
        position_alignment_blocker: str | None = None
        if position_alignment is not None:
            try:
                PlanningApplicationService(
                    connection,
                    self._environment_id,
                ).require_current_position_alignment(
                    version,
                    observed_at=previewed_at,
                )
            except ValueError as exc:
                position_alignment_blocker = str(exc)
        new_risk_discipline = (
            None
            if position_alignment is not None
            else PlanningApplicationService(
                connection,
                self._environment_id,
            ).new_risk_discipline_status(
                account_ref=version.account_ref,
                policy=self._new_risk_discipline_policy,
                observed_at=previewed_at,
                proposed_max_allowed_loss=(
                    version.requested_limits.max_allowed_loss
                ),
                proposed_max_notional=version.requested_limits.max_notional,
                proposed_instrument_ref=version.instrument_ref,
                proposed_direction=version.direction.value,
            )
        )
        live_profit_qualification = self._live_profit_qualification(
            version,
            previewed_at,
        )
        basis = _fixed_decision_basis_projection(version)
        gate_status = self._gate_status()
        product_build_consistent = (
            basis["product_build_id"] == self._product_build_id
        )
        runtime_incompatibility = plan_runtime_incompatibility(
            decision_basis=version.decision_basis,
            decision_context=getattr(version, "decision_context", None),
            order_schedule_spec=version.order_schedule_spec,
            allowed_actions=version.allowed_actions,
            position_alignment=position_alignment,
        )
        runtime_compatible = runtime_incompatibility is None
        return {
            "plan_version_id": version.plan_version_id,
            "plan_name": version.plan_name,
            "created_at": (
                version.created_at.isoformat() if version.created_at is not None else None
            ),
            "creator_kind": (
                version.creator_kind.value if version.creator_kind is not None else None
            ),
            "decision_context": (
                decision_context.model_dump(mode="json")
                if (
                    decision_context := getattr(version, "decision_context", None)
                ) is not None
                else None
            ),
            "environment_id": self._environment_id,
            "environment_kind": self._environment_kind.value,
            "authority_class": self._authority_class.value,
            "account_ref": version.account_ref,
            "venue_ref": getattr(version, "venue_ref", "BINANCE_USDM"),
            "instrument_ref": version.instrument_ref,
            "direction": version.direction.value,
            "decision_basis": basis["payload"],
            "decision_basis_kind": basis["kind"],
            "decision_basis_ref": basis["decision_basis_ref"],
            "strategy_ref": (
                basis["decision_basis_ref"]
                if basis["kind"] == DecisionBasisKind.STRATEGY_SIGNAL.value
                else None
            ),
            "parameter_digest": basis["parameter_digest"],
            "strategy_parameters": basis["normalized_parameters"],
            "order_schedule_spec": (
                version.order_schedule_spec.model_dump(mode="json")
                if getattr(version, "order_schedule_spec", None) is not None
                else None
            ),
            "position_alignment": (
                position_alignment.model_dump(mode="json")
                if position_alignment is not None
                else None
            ),
            "trade_amount": version.requested_limits.max_notional,
            "limits": version.requested_limits.model_dump(mode="json"),
            "valid_until": version.valid_until.isoformat(),
            "allowed_actions": sorted(version.allowed_actions),
            "actual_account_configuration": "PRE_SUBMIT_FACT_NOT_REQUIRED_FOR_PLAN_ACTIVATION",
            "account_mode_policy": (
                "NEW_RISK_SUPPORTS_ONE_WAY_SINGLE_ASSET_CROSSED_OR_ISOLATED"
            ),
            "product_build_id": basis["product_build_id"],
            "product_build_consistent": product_build_consistent,
            "runtime_compatible": runtime_compatible,
            "runtime_incompatibility_reason": runtime_incompatibility,
            "position_alignment_ready": (
                None
                if position_alignment is None
                else position_alignment_blocker is None
            ),
            "position_alignment_blocker": position_alignment_blocker,
            "ai_review": ai_review,
            "ai_review_ready": (
                None if position_alignment is not None else ai_review_blocker is None
            ),
            "ai_review_blocker": ai_review_blocker,
            "new_risk_discipline": (
                new_risk_discipline.model_dump(mode="json")
                if new_risk_discipline is not None
                else None
            ),
            "live_profit_qualification": (
                live_profit_qualification.model_dump(mode="json")
            ),
            "configured_runtime_real_write_gate": (
                gate_status.configured_runtime_real_write_gate
            ),
            "runtime_real_write_gate": gate_status.runtime_real_write_gate,
            "live_activation_eligible": (
                self._profile == "BINANCE_LIVE_WRITE"
                and runtime_compatible
                and position_alignment_blocker is None
                and ai_review_blocker is None
                and (
                    new_risk_discipline is None
                    or new_risk_discipline.new_risk_allowed
                )
                and live_profit_qualification.admission_satisfied
                and gate_status.product_build_consistent is True
                and not gate_status.violations
                and gate_status.configured_runtime_real_write_gate == "CLOSED"
            ),
            "capital_notice": "计划中的交易金额就是本次边界；激活不再要求独立资金授权，也不会绕过事实、CAP 或 EXE。",
        }

    def activation_preview(self, plan_version_id: str) -> dict[str, Any]:
        """Compatibility preview for an already persisted historical snapshot."""

        previewed_at = datetime.now(UTC)
        with self._connect() as connection, connection.transaction():
            version = PostgreSQLPlanningRepository(
                connection,
                self._environment_id,
            ).get_version(plan_version_id)
            return self._activation_preview_for_version(
                connection,
                version,
                previewed_at=previewed_at,
            )

    def submit_and_start_preview(
        self,
        plan_id: str,
        *,
        idempotency_key: str,
        expected_version: int,
        observed_at: datetime,
    ) -> dict[str, Any]:
        """Preflight a transient run snapshot without changing plan state."""

        self._require_product_mutation_allowed()
        plan_version_id = _stable_id(
            self._environment_id,
            "activation-snapshot",
            idempotency_key,
        )
        with self._connect() as connection, connection.transaction():
            repository = PostgreSQLPlanningRepository(
                connection,
                self._environment_id,
            )
            draft = repository.get_draft(plan_id, for_update=True)
            if draft.draft_version != expected_version:
                raise ValueError("PLAN_VERSION_CONFLICT")
            if repository.has_activation(plan_id):
                raise ValueError("PLAN_ALREADY_STARTED")
            approved_review = (
                None
                if draft.content.position_alignment is not None
                else self._approved_current_draft_ai_review(
                    connection,
                    draft,
                    observed_at=observed_at,
                )
            )
            version = PlanningApplicationService(
                connection,
                self._environment_id,
            ).build_activation_snapshot(
                plan_id=plan_id,
                expected_draft_version=expected_version,
                plan_version_id=plan_version_id,
                product_build_id=self._product_build_id,
                snapshot_at=observed_at,
                ai_review_ref=(
                    approved_review.review_id if approved_review is not None else None
                ),
                new_risk_discipline_policy=self._new_risk_discipline_policy,
            )
            return self._activation_preview_for_version(
                connection,
                version,
                previewed_at=observed_at,
            )

    def submit_and_start_replay(
        self,
        plan_id: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        """Return a previously committed start before rechecking current facts."""

        activation_id = _stable_id(self._environment_id, "activation", idempotency_key)
        with self._connect() as connection, connection.transaction():
            repository = PostgreSQLPlanningRepository(
                connection,
                self._environment_id,
            )
            try:
                activation = repository.get_activation(activation_id)
            except PlanningConflict as exc:
                if str(exc) == "ACTIVATION_NOT_FOUND":
                    return None
                raise
            version = repository.get_version(activation.plan_version_ref)
        if version.plan_id != plan_id:
            raise ValueError("IDEMPOTENCY_CONTENT_CONFLICT")
        return self._activation_response(activation)

    def submit_and_start(
        self,
        plan_id: str,
        *,
        idempotency_key: str,
        expected_version: int,
        observed_at: datetime,
        order_schedule_snapshot: OrderSchedulePreview | None,
    ) -> dict[str, Any]:
        """Atomically preserve a run snapshot and create its activation.

        This is the only normal workbench start command.  A rejected preview,
        discipline check, or activation admission raises inside the transaction,
        so the draft is still the same editable draft afterwards.
        """

        self._require_product_mutation_allowed()
        gate_status = self._gate_status()
        if self._profile == "BINANCE_LIVE_WRITE":
            if gate_status.product_build_consistent is not True:
                raise ValueError("LIVE_WRITE_PRODUCT_BUILD_MISMATCH")
            if gate_status.violations:
                raise ValueError("LIVE_WRITE_GATE_BINDING_INVALID_FOR_ACTIVATION")
            if gate_status.configured_runtime_real_write_gate != "CLOSED":
                raise ValueError("LIVE_WRITE_GATE_MUST_BE_CLOSED_FOR_ACTIVATION")
        plan_version_id = _stable_id(
            self._environment_id,
            "activation-snapshot",
            idempotency_key,
        )
        activation_id = _stable_id(self._environment_id, "activation", idempotency_key)
        with self._connect() as connection:
            repository = PostgreSQLPlanningRepository(connection, self._environment_id)
            try:
                with connection.transaction():
                    try:
                        existing = repository.get_activation(activation_id)
                    except PlanningConflict as exc:
                        if str(exc) != "ACTIVATION_NOT_FOUND":
                            raise
                    else:
                        version = repository.get_version(existing.plan_version_ref)
                        if version.plan_id != plan_id:
                            raise ValueError("IDEMPOTENCY_CONTENT_CONFLICT")
                        return self._activation_response(existing)
                    if self._profile == "BINANCE_LIVE_WRITE":
                        try:
                            require_live_activation_safety_index(connection)
                        except LiveWriteGateError as exc:
                            raise ValueError(str(exc)) from None
                    draft = repository.get_draft(plan_id, for_update=True)
                    if draft.draft_version != expected_version:
                        raise ValueError("PLAN_VERSION_CONFLICT")
                    if repository.has_activation(plan_id):
                        raise ValueError("PLAN_ALREADY_STARTED")
                    approved_review = (
                        None
                        if draft.content.position_alignment is not None
                        else self._approved_current_draft_ai_review(
                            connection,
                            draft,
                            observed_at=observed_at,
                        )
                    )
                    _version, activation = PlanningApplicationService(
                        connection,
                        self._environment_id,
                    ).fix_and_activate(
                        plan_id=plan_id,
                        expected_draft_version=expected_version,
                        plan_version_id=plan_version_id,
                        activation_id=activation_id,
                        environment_kind=self._environment_kind,
                        authority_class=self._authority_class,
                        product_build_id=self._product_build_id,
                        observed_at=observed_at,
                        ai_review_ref=(
                            approved_review.review_id
                            if approved_review is not None
                            else None
                        ),
                        order_schedule_snapshot=order_schedule_snapshot,
                        ai_review_checker=(
                            lambda version: self._require_approved_version_ai_review(
                                connection,
                                version,
                                observed_at=observed_at,
                                require_current_market_context=True,
                            )
                        ),
                        live_profit_qualification_checker=(
                            lambda version: self._require_live_profit_qualification_snapshot(
                                version,
                                observed_at,
                            )
                        ),
                        new_risk_discipline_policy=self._new_risk_discipline_policy,
                    )
            except psycopg.errors.UniqueViolation:
                connection.rollback()
                with connection.transaction():
                    try:
                        activation = repository.get_activation(activation_id)
                        version = repository.get_version(activation.plan_version_ref)
                    except PlanningConflict:
                        raise ValueError("IDEMPOTENCY_CONTENT_CONFLICT") from None
                if version.plan_id != plan_id:
                    raise ValueError("IDEMPOTENCY_CONTENT_CONFLICT") from None
        return self._activation_response(activation)

    def activate(
        self,
        payload: ActivationPayload,
        *,
        idempotency_key: str,
        observed_at: datetime,
        order_schedule_snapshot: OrderSchedulePreview | None = None,
    ) -> dict[str, Any]:
        self._require_product_mutation_allowed()
        gate_status = self._gate_status()
        if self._profile == "BINANCE_LIVE_WRITE":
            if gate_status.product_build_consistent is not True:
                raise ValueError("LIVE_WRITE_PRODUCT_BUILD_MISMATCH")
            if gate_status.violations:
                raise ValueError("LIVE_WRITE_GATE_BINDING_INVALID_FOR_ACTIVATION")
            if gate_status.configured_runtime_real_write_gate != "CLOSED":
                raise ValueError("LIVE_WRITE_GATE_MUST_BE_CLOSED_FOR_ACTIVATION")
        actual_schedule_digest = (
            order_schedule_snapshot.schedule_digest
            if order_schedule_snapshot is not None
            else None
        )
        if payload.expected_schedule_digest != actual_schedule_digest:
            raise ValueError("ACTIVATION_PREVIEW_STALE")
        activation_id = _stable_id(self._environment_id, "activation", idempotency_key)
        with self._connect() as connection:
            planning = PostgreSQLPlanningRepository(connection, self._environment_id)
            try:
                with connection.transaction():
                    if self._profile == "BINANCE_LIVE_WRITE":
                        try:
                            require_live_activation_safety_index(connection)
                        except LiveWriteGateError as exc:
                            raise ValueError(str(exc)) from None
                    activation = PlanningApplicationService(
                        connection, self._environment_id
                    ).activate_version(
                        plan_version_id=payload.plan_version_id,
                        activation_id=activation_id,
                        environment_kind=self._environment_kind,
                        authority_class=self._authority_class,
                        observed_at=observed_at,
                        order_schedule_snapshot=order_schedule_snapshot,
                        new_risk_discipline_policy=(
                            self._new_risk_discipline_policy
                        ),
                        live_profit_qualification_checker=(
                            lambda version: (
                                self._require_live_profit_qualification_snapshot(
                                    version,
                                    observed_at,
                                )
                            )
                        ),
                        ai_review_checker=(
                            lambda version: (
                                self._require_approved_version_ai_review(
                                    connection,
                                    version,
                                    observed_at=observed_at,
                                    require_current_market_context=True,
                                )
                            )
                        ),
                    )
            except psycopg.errors.UniqueViolation:
                connection.rollback()
                with connection.transaction():
                    try:
                        activation = planning.get_activation(activation_id)
                    except PlanningConflict:
                        raise ValueError("IDEMPOTENCY_CONTENT_CONFLICT") from None
                if activation.plan_version_ref != payload.plan_version_id:
                    raise ValueError("IDEMPOTENCY_CONTENT_CONFLICT") from None
                persisted_digest = (
                    activation.order_schedule_snapshot.schedule_digest
                    if activation.order_schedule_snapshot is not None
                    else None
                )
                if persisted_digest != actual_schedule_digest:
                    raise ValueError("IDEMPOTENCY_CONTENT_CONFLICT") from None
        return self._activation_response(activation)

    def activation_replay(
        self,
        payload: ActivationPayload,
        *,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        """Replay a committed activation before any rule refresh or readiness check."""

        activation_id = _stable_id(self._environment_id, "activation", idempotency_key)
        with self._connect() as connection, connection.transaction():
            try:
                activation = PostgreSQLPlanningRepository(
                    connection,
                    self._environment_id,
                ).get_activation(activation_id)
            except PlanningConflict as exc:
                if str(exc) == "ACTIVATION_NOT_FOUND":
                    return None
                raise
        persisted_digest = (
            activation.order_schedule_snapshot.schedule_digest
            if activation.order_schedule_snapshot is not None
            else None
        )
        if (
            activation.plan_version_ref != payload.plan_version_id
            or persisted_digest != payload.expected_schedule_digest
        ):
            raise ValueError("IDEMPOTENCY_CONTENT_CONFLICT")
        return self._activation_response(activation)

    def _activation_response(self, activation: Any) -> dict[str, Any]:
        return {
            "activation": activation.model_dump(mode="json"),
            "venue_write_created": False,
            "runtime_real_write_gate": self._gate_status().runtime_real_write_gate,
        }

    def list_activations(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT a.activation_id,
                       v.terms ->> 'plan_name',
                       v.terms ->> 'created_at',
                       v.terms ->> 'creator_kind',
                       latest_event.reason_code,
                       latest_event.no_action_reason,
                       closing_action.action_kind,
                       closing_action.action_terms ->> 'causation_ref',
                       closing_command.intent,
                       latest_review.primary_result,
                       latest_review.account_result -> 'trade_result'
                FROM halpha.plan_activation a
                LEFT JOIN halpha.trade_plan_version v
                  ON v.environment_id = a.environment_id
                 AND v.plan_version_id = a.plan_version_ref
                LEFT JOIN LATERAL (
                    SELECT reason_code, no_action_reason
                    FROM halpha.plan_event event
                    WHERE event.environment_id = a.environment_id
                      AND event.activation_id = a.activation_id
                    ORDER BY event.created_at DESC, event.plan_event_id DESC
                    LIMIT 1
                ) latest_event ON true
                LEFT JOIN LATERAL (
                    SELECT action_kind, action_terms
                    FROM halpha.execution_action action
                    WHERE action.environment_id = a.environment_id
                      AND action.activation_id = a.activation_id
                      AND action.action_kind = 'EXIT'
                    ORDER BY action.updated_at DESC, action.execution_action_id DESC
                    LIMIT 1
                ) closing_action ON true
                LEFT JOIN LATERAL (
                    SELECT c.intent
                    FROM halpha.command c
                    JOIN halpha.receipt r
                      ON r.environment_id = c.environment_id
                     AND r.command_id = c.command_id
                    WHERE c.environment_id = a.environment_id
                      AND c.target_kind = 'PLAN_ACTIVATION'
                      AND c.target_ref = a.activation_id::text
                      AND c.intent IN ('EXIT_STRATEGY', 'USER_TAKEOVER')
                      AND r.state = 'EFFECTIVE'
                    ORDER BY r.updated_at DESC, c.command_id DESC
                    LIMIT 1
                ) closing_command ON true
                LEFT JOIN LATERAL (
                    SELECT primary_result, account_result
                    FROM halpha.review review
                    WHERE review.environment_id = a.environment_id
                      AND review.activation_id = a.activation_id
                    ORDER BY review.review_version DESC
                    LIMIT 1
                ) latest_review ON true
                WHERE a.environment_id = %s
                  AND COALESCE(v.terms ->> 'workflow_kind', 'PLAN') <> 'SCALP_CYCLE'
                ORDER BY a.created_at DESC
                """,
                (self._environment_id,),
            ).fetchall()
            repository = PostgreSQLPlanningRepository(connection, self._environment_id)
            return [
                {
                    **repository.get_activation(str(row[0])).model_dump(mode="json"),
                    "plan_name": str(row[1]) if row[1] is not None else None,
                    "plan_created_at": (
                        str(row[2]) if row[2] is not None else None
                    ),
                    "plan_creator_kind": (
                        str(row[3]) if row[3] is not None else None
                    ),
                    "closure_reason_code": (
                        str(row[7])
                        if row[7] is not None
                        else (
                            str(row[6])
                            if row[6] is not None
                            else (
                                str(row[8])
                                if row[8] is not None
                                else (
                                    str(row[5])
                                    if row[5] is not None
                                    else (
                                        str(row[4])
                                        if row[4] is not None
                                        else None
                                    )
                                )
                            )
                        )
                    ),
                    "primary_result": (
                        str(row[9]) if row[9] is not None else None
                    ),
                    "trade_result": (
                        dict(row[10]) if row[10] is not None else None
                    ),
                }
                for row in rows
            ]

    def activation_detail(self, activation_id: str) -> dict[str, Any]:
        with self._connect() as connection, connection.transaction():
            planning_repository = PostgreSQLPlanningRepository(
                connection,
                self._environment_id,
            )
            activation = planning_repository.get_activation(activation_id)
            version = planning_repository.get_version(
                activation.plan_version_ref
            )
            ai_review_ref = getattr(version, "ai_review_ref", None)
            ai_review = self._stored_ai_review_snapshot(
                connection,
                ai_review_ref,
            )
            actions = connection.execute(
                """
                SELECT execution_action_id, action_kind, action_class, action_terms,
                       client_order_id, state, state_version, unknown_reason,
                       not_submitted_reason, protection_digest, closure_evidence_digest,
                       created_at, updated_at, execution_profile_ref, account_ref,
                       authority_class, cancel_target, call_started_at,
                       call_completed_at
                FROM halpha.execution_action
                WHERE environment_id = %s AND activation_id = %s
                ORDER BY created_at, execution_action_id
                """,
                (self._environment_id, activation_id),
            ).fetchall()
            facts = connection.execute(
                """
                SELECT venue_fact_id, kind, source_class, source_object_id,
                       source_time, received_at, cutoff, payload, action_ref,
                       attribution_class, content_digest
                FROM halpha.venue_fact
                WHERE environment_id = %s AND activation_ref = %s
                ORDER BY cutoff, venue_fact_id
                """,
                (self._environment_id, activation_id),
            ).fetchall()
            receipts = connection.execute(
                """
                SELECT c.command_id, c.intent, c.submitted_at, r.receipt_id,
                       r.state, r.state_version, r.reason_code, r.updated_at
                FROM halpha.command c
                JOIN halpha.receipt r
                  ON r.environment_id = c.environment_id AND r.command_id = c.command_id
                WHERE c.environment_id = %s AND c.target_kind = 'PLAN_ACTIVATION'
                  AND c.target_ref = %s
                ORDER BY c.submitted_at, c.command_id
                """,
                (self._environment_id, activation_id),
            ).fetchall()
            stops = connection.execute(
                """
                SELECT activation_id, stopped_categories, reason, source,
                       started_at, version, stop_state_version_id
                FROM (
                  SELECT DISTINCT ON (
                    CASE WHEN activation_id IS NULL THEN 'ACCOUNT'
                         ELSE activation_id::text END
                  ) activation_id, stopped_categories, reason, source,
                    started_at, version, stop_state_version_id
                  FROM halpha.stop_state_version
                  WHERE environment_id = %s AND account_ref = %s
                    AND (activation_id IS NULL OR activation_id = %s)
                  ORDER BY CASE WHEN activation_id IS NULL THEN 'ACCOUNT'
                                ELSE activation_id::text END,
                           version DESC
                ) current_stops
                """,
                (self._environment_id, self._account_ref, activation_id),
            ).fetchall()
            latest_position_row = connection.execute(
                """
                SELECT venue_fact_id, payload, cutoff, content_digest, received_at
                FROM halpha.venue_fact
                WHERE environment_id = %s
                  AND account_ref = %s
                  AND instrument_ref = %s
                  AND kind = 'POSITION_STATE'
                  AND source_class = 'VENUE_QUERY'
                  AND action_ref IS NULL
                  AND activation_ref IS NULL
                  AND payload ->> 'activation_id' = %s
                ORDER BY cutoff DESC, received_at DESC, venue_fact_id DESC
                LIMIT 1
                """,
                (
                    self._environment_id,
                    activation.account_ref,
                    activation.instrument_ref,
                    activation.activation_id,
                ),
            ).fetchone()
            scope_activations = (
                planning_repository.list_account_instrument_activations(
                    account_ref=activation.account_ref,
                    instrument_ref=activation.instrument_ref,
                )
            )
            scope_activation_ids = [
                item.activation_id for item in scope_activations
            ]
            attribution_action_rows = connection.execute(
                """
                SELECT activation_id, execution_action_id, account_ref,
                       action_kind, action_terms, client_order_id, state,
                       created_at
                FROM halpha.execution_action
                WHERE environment_id = %s
                  AND activation_id = ANY(%s::uuid[])
                ORDER BY created_at, execution_action_id
                """,
                (self._environment_id, scope_activation_ids),
            ).fetchall()
            attribution_action_ids = [
                str(row[1]) for row in attribution_action_rows
            ]
            attribution_fact_rows = (
                connection.execute(
                    """
                    SELECT venue_fact_id, action_ref, activation_ref, kind,
                           payload, source_time
                    FROM halpha.venue_fact
                    WHERE environment_id = %s
                      AND action_ref = ANY(%s::uuid[])
                    ORDER BY cutoff, received_at, venue_fact_id
                    """,
                    (self._environment_id, attribution_action_ids),
                ).fetchall()
                if attribution_action_ids
                else []
            )
            try:
                attribution = account_instrument_attribution_from_rows(
                    activation,
                    scope_activations,
                    attribution_action_rows,
                    attribution_fact_rows,
                )
                attribution_reason = None
            except ValueError as exc:
                attribution = None
                attribution_reason = str(exc)
            latest_position_payload = (
                dict(latest_position_row[1])
                if latest_position_row is not None
                else {}
            )
            venue_signed_position = latest_position_payload.get(
                "position_quantity"
            )
            attributed_account_position = (
                attribution.account_signed_position
                if attribution is not None
                else None
            )
            reconciled_attributed_position = latest_position_payload.get(
                "attributed_account_position_quantity"
            )
            if (
                venue_signed_position is None
                or reconciled_attributed_position is None
                or attributed_account_position is None
            ):
                reconciliation_status = "UNKNOWN"
            else:
                try:
                    venue_position = Decimal(str(venue_signed_position))
                    fact_attributed_position = Decimal(
                        str(reconciled_attributed_position)
                    )
                    current_attributed_position = Decimal(
                        attributed_account_position
                    )
                    if current_attributed_position != fact_attributed_position:
                        reconciliation_status = "STALE"
                    elif venue_position == fact_attributed_position:
                        reconciliation_status = "MATCH"
                    else:
                        reconciliation_status = "MISMATCH"
                except (InvalidOperation, TypeError, ValueError):
                    reconciliation_status = "UNKNOWN"
                    attribution_reason = "POSITION_FACT_INVALID"
        stopped_categories = {str(category) for row in stops for category in row[1]}
        if "ALL_EXCHANGE_CHANGES" in stopped_categories:
            stopped_categories.update(
                {
                    "NEW_RISK",
                    "PROTECTION",
                    "RISK_REDUCTION_OR_ORDER_MANAGEMENT",
                }
            )
        return {
            "activation": activation.model_dump(mode="json"),
            "ai_review_ref": ai_review_ref,
            "ai_review": ai_review,
            "plan": {
                "plan_version_id": version.plan_version_id,
                "plan_id": version.plan_id,
                "plan_name": version.plan_name,
                "created_at": (
                    version.created_at.isoformat()
                    if version.created_at is not None
                    else None
                ),
                "creator_kind": (
                    version.creator_kind.value
                    if version.creator_kind is not None
                    else None
                ),
                "decision_context": (
                    decision_context.model_dump(mode="json")
                    if (
                        decision_context := getattr(
                            version,
                            "decision_context",
                            None,
                        )
                    ) is not None
                    else None
                ),
            },
            "decision_basis": version.decision_basis.model_dump(mode="json"),
            "strategy": (
                {
                    "strategy_ref": version.decision_basis.decision_basis_ref,
                    "parameters": version.decision_basis.normalized_parameters,
                }
                if isinstance(version.decision_basis, FixedStrategyPlanBasis)
                else None
            ),
            "order_schedule": (
                activation.order_schedule_snapshot.model_dump(mode="json")
                if activation.order_schedule_snapshot is not None
                else None
            ),
            "capital": {
                "max_margin": version.requested_limits.max_margin,
                "max_notional": version.requested_limits.max_notional,
                "max_allowed_loss": version.requested_limits.max_allowed_loss,
                "runtime_pnl_stop_supported": False,
            },
            "position_attribution": {
                "activation_signed_position": (
                    attribution.activation_signed_position
                    if attribution is not None
                    else None
                ),
                "attributed_account_signed_position": (
                    attributed_account_position
                ),
                "venue_account_signed_position": (
                    str(venue_signed_position)
                    if venue_signed_position is not None
                    else None
                ),
                "reconciled_attributed_account_signed_position": (
                    str(reconciled_attributed_position)
                    if reconciled_attributed_position is not None
                    else None
                ),
                "reconciliation_status": reconciliation_status,
                "reason_code": attribution_reason,
                "activation_owned_order_identity_count": (
                    len(attribution.activation_ordinary_client_ids)
                    + len(attribution.activation_algo_client_ids)
                    if attribution is not None
                    else None
                ),
                "account_owned_order_identity_count": (
                    len(attribution.account_ordinary_client_ids)
                    + len(attribution.account_algo_client_ids)
                    if attribution is not None
                    else None
                ),
                "margin_mode": latest_position_payload.get("margin_mode"),
                "leverage": latest_position_payload.get("leverage"),
                "fact_cutoff": (
                    latest_position_row[2].isoformat()
                    if latest_position_row is not None
                    else None
                ),
                "fact_received_at": (
                    latest_position_row[4].isoformat()
                    if latest_position_row is not None
                    else None
                ),
                "fact_ref": (
                    str(latest_position_row[0])
                    if latest_position_row is not None
                    else None
                ),
                "fact_digest": (
                    str(latest_position_row[3])
                    if latest_position_row is not None
                    else None
                ),
                "fact_activation_id": latest_position_payload.get(
                    "activation_id"
                ),
            },
            "trade_result": summarize_trade_result(
                direction=activation.direction.value,
                action_kinds={str(row[0]): str(row[1]) for row in actions},
                facts=(
                    {
                        "kind": str(row[1]),
                        "payload": dict(row[7]),
                        "action_ref": str(row[8]) if row[8] is not None else None,
                        "source_time": row[4].isoformat() if row[4] is not None else None,
                    }
                    for row in facts
                ),
                opening_position_quantity=(
                    activation.position_alignment.requested_reduction_quantity
                    if activation.position_alignment is not None
                    else None
                ),
            ),
            "execution_actions": [
                {
                    "execution_action_id": str(row[0]),
                    "action_kind": str(row[1]),
                    "action_class": str(row[2]),
                    "action_terms": dict(row[3]),
                    "client_order_id": str(row[4]) if row[4] is not None else None,
                    "state": str(row[5]),
                    "state_version": int(row[6]),
                    "unknown_reason": str(row[7]) if row[7] is not None else None,
                    "not_submitted_reason": (
                        str(row[8]) if row[8] is not None else None
                    ),
                    "protection_digest": str(row[9]) if row[9] is not None else None,
                    "closure_evidence_digest": str(row[10])
                    if row[10] is not None
                    else None,
                    "created_at": row[11].isoformat(),
                    "updated_at": row[12].isoformat(),
                    "execution_profile_ref": str(row[13]),
                    "account_ref": str(row[14]),
                    "authority_class": str(row[15]),
                    "cancel_target": dict(row[16]) if row[16] is not None else None,
                    "call_started_at": (
                        row[17].isoformat() if row[17] is not None else None
                    ),
                    "call_completed_at": (
                        row[18].isoformat() if row[18] is not None else None
                    ),
                }
                for row in actions
            ],
            "venue_facts": [
                {
                    "venue_fact_id": str(row[0]),
                    "kind": str(row[1]),
                    "source_class": str(row[2]),
                    "source_object_id": str(row[3]) if row[3] is not None else None,
                    "source_time": row[4].isoformat() if row[4] is not None else None,
                    "received_at": row[5].isoformat(),
                    "cutoff": row[6].isoformat(),
                    "payload": dict(row[7]),
                    "action_ref": str(row[8]) if row[8] is not None else None,
                    "attribution_class": str(row[9]) if row[9] is not None else None,
                    "content_digest": str(row[10]),
                }
                for row in facts
            ],
            "receipts": [
                {
                    "command_id": str(row[0]),
                    "intent": str(row[1]),
                    "submitted_at": row[2].isoformat(),
                    "receipt_id": str(row[3]),
                    "state": str(row[4]),
                    "state_version": int(row[5]),
                    "reason_code": str(row[6]) if row[6] is not None else None,
                    "updated_at": row[7].isoformat(),
                }
                for row in receipts
            ],
            "stopped_categories": sorted(stopped_categories),
            "stop_evidence": [
                {
                    "scope": "ACCOUNT" if row[0] is None else "ACTIVATION",
                    "activation_id": str(row[0]) if row[0] is not None else None,
                    "categories": list(row[1]),
                    "reason": str(row[2]),
                    "source": str(row[3]),
                    "started_at": row[4].isoformat(),
                    "version": int(row[5]),
                    "stop_state_version_id": str(row[6]),
                }
                for row in stops
            ],
            "runtime_real_write_gate": self._gate_status().runtime_real_write_gate,
        }

    def activation_timeline(self, activation_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            activation_rows = connection.execute(
                """
                SELECT created_at, plan_version_ref
                FROM halpha.plan_activation
                WHERE environment_id = %s AND activation_id = %s
                """,
                (self._environment_id, activation_id),
            ).fetchall()
            event_rows = connection.execute(
                """
                SELECT plan_event_id, rule_id, source_identity, source_cutoff,
                       reason_code, no_action_reason, capital_decision,
                       proposed_action, created_at
                FROM halpha.plan_event
                WHERE environment_id = %s AND activation_id = %s
                ORDER BY created_at, plan_event_id
                """,
                (self._environment_id, activation_id),
            ).fetchall()
            action_rows = connection.execute(
                """
                SELECT execution_action_id, action_kind, state, state_version,
                       client_order_id, unknown_reason, created_at, updated_at,
                       execution_profile_ref, account_ref, authority_class
                FROM halpha.execution_action
                WHERE environment_id = %s AND activation_id = %s
                ORDER BY created_at, execution_action_id
                """,
                (self._environment_id, activation_id),
            ).fetchall()
            fact_rows = connection.execute(
                """
                SELECT venue_fact_id, kind, source_class, source_object_id,
                       action_ref, cutoff, content_digest
                FROM halpha.venue_fact
                WHERE environment_id = %s AND activation_ref = %s
                ORDER BY cutoff, venue_fact_id
                """,
                (self._environment_id, activation_id),
            ).fetchall()
            command_rows = connection.execute(
                """
                SELECT c.command_id, c.intent, c.submitted_at, r.receipt_id,
                       r.state, r.reason_code, r.updated_at
                FROM halpha.command c
                JOIN halpha.receipt r
                  ON r.environment_id = c.environment_id
                 AND r.command_id = c.command_id
                WHERE c.environment_id = %s
                  AND c.target_kind = 'PLAN_ACTIVATION'
                  AND c.target_ref = %s
                ORDER BY c.submitted_at, c.command_id
                """,
                (self._environment_id, activation_id),
            ).fetchall()
        timeline = [
            {
                "source": "ACTIVATION",
                "source_ref": activation_id,
                "stage_order": 0,
                "at": row[0].isoformat(),
                "status": "STARTED",
                "detail": {
                    "plan_version_ref": str(row[1]),
                },
            }
            for row in activation_rows
        ]
        timeline.extend(
            {
                "source": "PLAN_EVENT",
                "source_ref": str(row[0]),
                "stage_order": 1,
                "at": row[8].isoformat(),
                "status": str(row[4]),
                "detail": {
                    "rule_id": str(row[1]),
                    "source_identity": str(row[2]),
                    "source_cutoff": row[3].isoformat(),
                    "no_action_reason": str(row[5]) if row[5] is not None else None,
                    "capital_decision": dict(row[6]),
                    "entry_risk_context": (
                        dict(entry_risk_context)
                        if isinstance(
                            proposed_action := row[7],
                            dict,
                        )
                        and isinstance(
                            execution_context := proposed_action.get(
                                "execution_context"
                            ),
                            dict,
                        )
                        and isinstance(
                            entry_risk_context := execution_context.get(
                                "entry_risk_context"
                            ),
                            dict,
                        )
                        else None
                    ),
                },
            }
            for row in event_rows
        )
        timeline.extend(
            {
                "source": "EXECUTION_ACTION",
                "source_ref": str(row[0]),
                "stage_order": 2,
                "at": row[7].isoformat(),
                "status": str(row[2]),
                "detail": {
                    "action_kind": str(row[1]),
                    "state_version": int(row[3]),
                    "client_order_id": str(row[4]) if row[4] is not None else None,
                    "unknown_reason": str(row[5]) if row[5] is not None else None,
                    "created_at": row[6].isoformat(),
                    "environment_id": self._environment_id,
                    "execution_profile_ref": str(row[8]),
                    "account_ref": str(row[9]),
                    "authority_class": str(row[10]),
                },
            }
            for row in action_rows
        )
        timeline.extend(
            {
                "source": "VENUE_FACT",
                "source_ref": str(row[0]),
                "stage_order": 3,
                "at": row[5].isoformat(),
                "status": str(row[1]),
                "detail": {
                    "source_class": str(row[2]),
                    "source_object_id": str(row[3]) if row[3] is not None else None,
                    "action_ref": str(row[4]) if row[4] is not None else None,
                    "content_digest": str(row[6]),
                },
            }
            for row in fact_rows
        )
        timeline.extend(
            {
                "source": "CONTROL_COMMAND",
                "source_ref": str(row[0]),
                "stage_order": 4,
                "at": row[6].isoformat(),
                "status": str(row[4]),
                "detail": {
                    "intent": str(row[1]),
                    "submitted_at": row[2].isoformat(),
                    "receipt_id": str(row[3]),
                    "reason_code": (
                        str(row[5]) if row[5] is not None else None
                    ),
                },
            }
            for row in command_rows
        )
        return sorted(
            timeline,
            key=lambda item: (item["at"], item["stage_order"], item["source_ref"]),
        )

    def _system_stop_release_evidence(
        self,
        connection: psycopg.Connection[Any],
        *,
        activation_id: str,
        current_stop: StopStateVersion | None,
        observed_at: datetime,
    ) -> dict[str, Any]:
        consequence = (
            "仅解除账户级系统新增风险停止；不会启动计划、下单、撤单、"
            "修改保护或改变持仓。后续任何新风险仍需重新通过当前计划、事实和资本检查。"
        )
        if current_stop is None:
            return {
                "eligible": False,
                "denial_reasons": ["ACCOUNT_SYSTEM_STOP_NOT_ACTIVE"],
                "consequence": consequence,
                "stop": None,
                "evidence_cutoff": None,
            }
        stop_projection = {
            "stop_state_version_id": current_stop.stop_state_version_id,
            "version": current_stop.version,
            "source": current_stop.source,
            "started_at": current_stop.started_at.isoformat(),
        }
        denial_reasons: list[str] = []
        if (
            StopCategory.NEW_RISK not in current_stop.stopped_categories
            or current_stop.source
            not in {source.value for source in AccountSystemStopSource}
        ):
            denial_reasons.append("ACCOUNT_SYSTEM_STOP_NOT_ACTIVE")

        try:
            activation = PostgreSQLPlanningRepository(
                connection,
                self._environment_id,
            ).get_activation(activation_id)
        except PlanningConflict as exc:
            if str(exc) != "ACTIVATION_NOT_FOUND":
                raise
            return {
                "eligible": False,
                "denial_reasons": [
                    "SYSTEM_STOP_RELEASE_CONTEXT_ACTIVATION_NOT_FOUND"
                ],
                "consequence": consequence,
                "stop": stop_projection,
                "evidence_cutoff": None,
            }
        if activation.account_ref != self._account_ref:
            denial_reasons.append("SYSTEM_STOP_RELEASE_ACCOUNT_MISMATCH")
        if (
            activation.lifecycle.value != "COMPLETED"
            or activation.responsibility_owner != "USER"
            or not activation.takeover_scope
            or not activation.closure_digest
        ):
            denial_reasons.append("USER_TAKEOVER_CLOSURE_REQUIRED")
        elif activation.updated_at < current_stop.started_at:
            denial_reasons.append("SYSTEM_STOP_RELEASE_CLOSURE_PREDATES_STOP")
        if denial_reasons:
            return {
                "eligible": False,
                "denial_reasons": denial_reasons,
                "consequence": consequence,
                "stop": stop_projection,
                "evidence_cutoff": None,
            }

        account_row = connection.execute(
            """
            SELECT venue_fact_id, received_at, cutoff, payload, content_digest
            FROM halpha.venue_fact
            WHERE environment_id = %s
              AND account_ref = %s
              AND venue_ref = 'BINANCE_USDM'
              AND instrument_ref IS NULL
              AND kind = 'ACCOUNT_STATE'
              AND source_class = 'VENUE_QUERY'
              AND received_at >= %s
            ORDER BY received_at DESC, venue_fact_id DESC
            LIMIT 1
            """,
            (
                self._environment_id,
                self._account_ref,
                current_stop.started_at,
            ),
        ).fetchone()
        if account_row is None:
            denial_reasons.append("SYSTEM_STOP_RELEASE_ACCOUNT_SNAPSHOT_MISSING")
            return {
                "eligible": False,
                "denial_reasons": denial_reasons,
                "consequence": consequence,
                "stop": stop_projection,
                "evidence_cutoff": None,
            }

        account_fact_id = str(account_row[0])
        received_at = account_row[1]
        cutoff = account_row[2]
        snapshot = parse_complete_binance_usdm_account_snapshot(account_row[3])
        account_content_digest = str(account_row[4])
        query_paths = snapshot.get("query_paths") if snapshot is not None else None
        positions = snapshot["positions"] if snapshot is not None else None
        ordinary_orders = (
            snapshot["ordinary_open_orders"] if snapshot is not None else None
        )
        algo_orders = snapshot["algo_open_orders"] if snapshot is not None else None
        snapshot_schema = snapshot["schema"] if snapshot is not None else None
        required_query_paths = set(
            BINANCE_USDM_ACCOUNT_SNAPSHOT_QUERY_PATHS
            if snapshot_schema == BINANCE_USDM_ACCOUNT_SNAPSHOT_SCHEMA
            else BINANCE_USDM_ACCOUNT_POSITION_ORDER_QUERY_PATHS
        )
        snapshot_shape_valid = (
            snapshot is not None
            and isinstance(query_paths, list)
            and all(isinstance(item, str) for item in query_paths)
            and required_query_paths.issubset(query_paths)
            and isinstance(positions, list)
            and isinstance(ordinary_orders, list)
            and isinstance(algo_orders, list)
            and received_at == cutoff
        )
        if not snapshot_shape_valid:
            denial_reasons.append("SYSTEM_STOP_RELEASE_ACCOUNT_SNAPSHOT_INVALID")
        else:
            if positions:
                denial_reasons.append("SYSTEM_STOP_RELEASE_ACCOUNT_NOT_FLAT")
            if ordinary_orders or algo_orders:
                denial_reasons.append("SYSTEM_STOP_RELEASE_OPEN_ORDERS_REMAIN")
        if (
            received_at > observed_at
            or observed_at - received_at
            > ACCOUNT_SYSTEM_STOP_RELEASE_EVIDENCE_MAX_AGE
        ):
            denial_reasons.append("SYSTEM_STOP_RELEASE_EVIDENCE_STALE")
        if received_at < current_stop.started_at:
            denial_reasons.append(
                "SYSTEM_STOP_RELEASE_EVIDENCE_PREDATES_STOP"
            )

        open_activation_rows = connection.execute(
            """
            SELECT activation_id, lifecycle
            FROM halpha.plan_activation
            WHERE environment_id = %s
              AND account_ref = %s
              AND lifecycle <> 'COMPLETED'
            ORDER BY activation_id
            """,
            (self._environment_id, self._account_ref),
        ).fetchall()
        if open_activation_rows:
            denial_reasons.append("SYSTEM_STOP_RELEASE_ACCOUNT_ACTIVATIONS_OPEN")

        open_action_rows = connection.execute(
            """
            SELECT execution_action_id, state, client_order_id
            FROM halpha.execution_action
            WHERE environment_id = %s
              AND account_ref = %s
              AND state NOT IN ('CLOSED', 'NOT_SUBMITTED', 'HANDED_OVER')
            ORDER BY execution_action_id
            """,
            (self._environment_id, self._account_ref),
        ).fetchall()
        if open_action_rows:
            denial_reasons.append("SYSTEM_STOP_RELEASE_ACCOUNT_ACTIONS_OPEN")

        later_account_fact_rows = connection.execute(
            """
            SELECT venue_fact_id, kind, content_digest
            FROM halpha.venue_fact
            WHERE environment_id = %s
              AND account_ref = %s
              AND received_at > %s
            ORDER BY received_at, venue_fact_id
            """,
            (
                self._environment_id,
                self._account_ref,
                received_at,
            ),
        ).fetchall()
        if later_account_fact_rows:
            denial_reasons.append("SYSTEM_STOP_RELEASE_NEW_UNCLAIMED_FACT")

        resolution_evidence = {
            "environment_id": self._environment_id,
            "account_ref": self._account_ref,
            "activation_id": activation.activation_id,
            "activation_state_version": activation.state_version,
            "activation_lifecycle": activation.lifecycle.value,
            "responsibility_owner": activation.responsibility_owner,
            "closure_digest": activation.closure_digest,
            "takeover_command_ref": (
                activation.takeover_scope.get("command_ref")
                if isinstance(activation.takeover_scope, dict)
                else None
            ),
            "stop_state_version_id": current_stop.stop_state_version_id,
            "stop_content_digest": current_stop.content_digest,
            "account_fact_id": account_fact_id,
            "account_fact_content_digest": account_content_digest,
            "account_fact_cutoff": cutoff,
            "open_activations": tuple(
                (str(row[0]), str(row[1]))
                for row in open_activation_rows
            ),
            "open_actions": tuple(
                (
                    str(row[0]),
                    str(row[1]),
                    str(row[2]) if row[2] is not None else None,
                )
                for row in open_action_rows
            ),
            "later_account_facts": tuple(
                (str(row[0]), str(row[1]), str(row[2]))
                for row in later_account_fact_rows
            ),
        }
        reconciliation = {
            "resolution_evidence_digest": content_digest(resolution_evidence),
            "account_fact_id": account_fact_id,
            "account_fact_content_digest": account_content_digest,
            "position_count": len(positions) if isinstance(positions, list) else None,
            "ordinary_open_order_count": (
                len(ordinary_orders) if isinstance(ordinary_orders, list) else None
            ),
            "algo_open_order_count": (
                len(algo_orders) if isinstance(algo_orders, list) else None
            ),
            "observed_at": received_at,
        }
        return {
            "eligible": not denial_reasons,
            "denial_reasons": denial_reasons,
            "consequence": consequence,
            "stop": stop_projection,
            "evidence_cutoff": received_at.isoformat(),
            "_reconciliation_digest": content_digest(reconciliation),
            "_resolution_evidence_digest": content_digest(resolution_evidence),
            "_reconciliation_observed_at": received_at,
        }

    @staticmethod
    def _public_system_stop_release_preview(
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            key: value
            for key, value in evidence.items()
            if not key.startswith("_")
        }

    def system_stop_release_preview(
        self,
        activation_id: str,
        *,
        observed_at: datetime,
    ) -> dict[str, Any]:
        with self._connect() as connection, connection.transaction():
            current_stop = PostgreSQLCapitalRepository(
                connection,
                self._environment_id,
            ).get_current_account_stop_state(account_ref=self._account_ref)
            evidence = self._system_stop_release_evidence(
                connection,
                activation_id=activation_id,
                current_stop=current_stop,
                observed_at=observed_at,
            )
        return self._public_system_stop_release_preview(evidence)

    def release_system_stop(
        self,
        activation_id: str,
        payload: SystemStopReleasePayload,
        *,
        idempotency_key: str,
        observed_at: datetime,
    ) -> dict[str, Any]:
        self._require_product_mutation_allowed()
        stop_state_version_id = _stable_id(
            self._environment_id,
            "system-stop-release",
            idempotency_key,
        )
        with self._connect() as connection, connection.transaction():
            repository = PostgreSQLCapitalRepository(
                connection,
                self._environment_id,
            )
            existing = repository.find_stop_state(stop_state_version_id)
            if existing is not None:
                release = existing.release_rules.get("system_stop_release")
                if (
                    existing.account_ref != self._account_ref
                    or existing.source != "USER_SYSTEM_STOP_RELEASE"
                    or not isinstance(release, dict)
                    or release.get("resolution_activation_id") != activation_id
                    or release.get("released_stop_version")
                    != payload.expected_stop_version
                ):
                    raise CapitalConflict("SYSTEM_STOP_RELEASE_IDENTITY_CONFLICT")
                return {
                    "effective": True,
                    "replayed": True,
                    "stop_state": existing.model_dump(mode="json"),
                }

            current_stop = repository.lock_current_account_stop_state(
                account_ref=self._account_ref,
            )
            if (
                current_stop is None
                or current_stop.version != payload.expected_stop_version
            ):
                raise CapitalConflict("SYSTEM_STOP_VERSION_CONFLICT")
            evidence = self._system_stop_release_evidence(
                connection,
                activation_id=activation_id,
                current_stop=current_stop,
                observed_at=observed_at,
            )
            denial_reasons = evidence["denial_reasons"]
            if denial_reasons:
                raise CapitalConflict(str(denial_reasons[0]))
            request = AccountSystemStopReleaseRequest(
                new_stop_state_version_id=stop_state_version_id,
                environment_id=self._environment_id,
                environment_kind=self._environment_kind,
                authority_class=self._authority_class,
                account_ref=self._account_ref,
                resolution_activation_id=activation_id,
                expected_version=current_stop.version,
                expected_stop_content_digest=current_stop.content_digest,
                expected_source=AccountSystemStopSource(current_stop.source),
                reconciliation_digest=evidence["_reconciliation_digest"],
                resolution_evidence_digest=evidence[
                    "_resolution_evidence_digest"
                ],
                reconciliation_observed_at=evidence[
                    "_reconciliation_observed_at"
                ],
                submitted_at=observed_at,
                resolution_status="NO_UNRESOLVED_ACCOUNT_STOP_CAUSE",
                confirmation=payload.confirmation,
            )
            released = CapitalApplicationService(
                connection,
                self._environment_id,
            ).release_account_system_stop(request)
        return {
            "effective": True,
            "replayed": False,
            "stop_state": released.model_dump(mode="json"),
        }

    def control_preview(
        self,
        activation_id: str,
        intent: ControlIntent,
        *,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        previewed_at = observed_at or datetime.now(UTC)
        current = self.activation_detail(activation_id)
        consequences = {
            ControlIntent.STOP_NEW_RISK: "立即阻止新风险；已有查询、保护、撤单和减险责任继续。",
            ControlIntent.RESUME_ACTIVATION: (
                "核对通过后恢复本计划后续开仓、加仓与入场重挂；不会改变已有订单、持仓、保护、"
                "退出规则或其他安全停止。"
            ),
            ControlIntent.EXIT_STRATEGY: "进入 EXITING，停止增险并等待执行与闭合责任。",
            ControlIntent.USER_TAKEOVER: "先持久化责任转移，再停止自动发起交易所变更请求；不会自动撤单或平仓。",
        }
        activation = current["activation"]
        capital = current["capital"]
        preview_basis = {
            "activation_id": activation_id,
            "intent": intent.value,
            "activation_state_version": activation["state_version"],
            "lifecycle": activation["lifecycle"],
            "run_state": activation["run_state"],
            "pause_reason": activation["pause_reason"],
            "protection_state": activation["protection_state"],
            "runtime_pnl_stop_supported": bool(
                capital.get("runtime_pnl_stop_supported")
            ),
        }
        resume_evidence = (
            _continuity_resume_evidence(
                current,
                observed_at=previewed_at,
            )
            if intent is ControlIntent.RESUME_ACTIVATION
            else {
                "eligible": False,
                "denial_reasons": [],
                "reconciliation_digest": None,
                "evidence_cutoff": None,
            }
        )
        return {
            **current,
            "intent": intent.value,
            "consequence": consequences[intent],
            "preview_digest": content_digest(preview_basis),
            "previewed_at": previewed_at.isoformat(),
            "resume_eligible": (
                resume_evidence["eligible"]
                if intent is ControlIntent.RESUME_ACTIVATION
                else None
            ),
            "resume_denial_reasons": (
                resume_evidence["denial_reasons"]
                if intent is ControlIntent.RESUME_ACTIVATION
                else []
            ),
            "reconciliation_digest": resume_evidence[
                "reconciliation_digest"
            ],
            "reconciliation_evidence_cutoff": resume_evidence[
                "evidence_cutoff"
            ],
            "venue_write_created_by_preview": False,
        }

    def submit_control(
        self,
        activation_id: str,
        intent: ControlIntent,
        payload: ControlPayload,
        *,
        idempotency_key: str,
        observed_at: datetime,
    ) -> dict[str, Any]:
        self._require_product_mutation_allowed()
        resume_evidence = (
            self.control_preview(
                activation_id,
                intent,
                observed_at=observed_at,
            )
            if intent is ControlIntent.RESUME_ACTIVATION
            else None
        )
        command_id = _stable_id(self._environment_id, "command", idempotency_key)
        receipt_id = _stable_id(self._environment_id, "receipt", idempotency_key)
        stop_id = _stable_id(self._environment_id, "stop-state", idempotency_key)
        scope = {
            **payload.takeover_scope,
            "activation_id": activation_id,
            "cutoff": observed_at.isoformat(),
        }
        if intent is ControlIntent.USER_TAKEOVER:
            # EXE closure attributes the post-handover boundary to this exact
            # immutable command. User-provided scope must not be able to
            # replace that identity.
            scope["command_ref"] = command_id
        command = build_command(
            command_id=command_id,
            environment_id=self._environment_id,
            owner_scope="local-owner",
            idempotency_key=idempotency_key,
            activation_id=activation_id,
            expected_version=payload.expected_version,
            intent=intent,
            scope=scope,
            parameters={},
            submitted_at=observed_at,
        )
        with self._connect() as connection, connection.transaction():
            receipt = ActivationControlService(connection, self._environment_id).submit(
                command,
                receipt_id=receipt_id,
                stop_state_version_id=stop_id,
                # The user never supplies this digest. It is derived from the
                # latest target-scoped EXE position fact immediately before the
                # command transaction.
                reconciliation_digest=(
                    resume_evidence["reconciliation_digest"]
                    if resume_evidence is not None
                    and resume_evidence["resume_eligible"] is True
                    else None
                ),
                facts_known=(
                    resume_evidence is None
                    or resume_evidence["resume_eligible"] is True
                ),
            )
        # The persisted model carries its database partition key, while the
        # public receipt contract intentionally does not expose it. Keep the
        # immediate submission response identical to GET /receipts/{id}.
        public_receipt = receipt.model_dump(mode="json")
        public_receipt.pop("environment_id", None)
        return public_receipt

    def exit_receipt_lookup(
        self,
        activation_id: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        """Read the exact submitted exit without creating or retrying a command."""

        _stable_id(self._environment_id, "command", idempotency_key)
        with self._connect() as connection, connection.transaction():
            found = PostgreSQLCommandRepository(
                connection,
                self._environment_id,
            ).find_by_idempotency("local-owner", idempotency_key)
            if found is None:
                return None
            command, receipt = found
            if (
                command.target_kind != "PLAN_ACTIVATION"
                or command.target_ref != activation_id
                or command.intent is not ControlIntent.EXIT_STRATEGY
            ):
                return None
            activation = PostgreSQLPlanningRepository(
                connection,
                self._environment_id,
            ).get_activation(activation_id)
            if activation.account_ref != self._account_ref:
                return None
        return receipt.model_dump(mode="json", exclude={"environment_id"})

    def receipt(self, receipt_id: str) -> dict[str, Any]:
        query = """
                SELECT r.receipt_id, r.command_id, r.processing_owner, r.state,
                       r.state_version, r.reason_code, r.result,
                       r.pending_responsibility_refs, r.content_digest,
                       r.created_at, r.updated_at, c.target_ref
                FROM halpha.receipt r
                JOIN halpha.command c
                  ON c.environment_id = r.environment_id
                 AND c.command_id = r.command_id
                WHERE r.environment_id = %s AND r.receipt_id = %s
                """
        with self._connect() as connection, connection.transaction():
            row = connection.execute(
                query,
                (self._environment_id, receipt_id),
            ).fetchone()
        if row is None:
            raise ValueError("RECEIPT_NOT_FOUND")
        return {
            "receipt_id": str(row[0]),
            "command_id": str(row[1]),
            "processing_owner": str(row[2]),
            "state": str(row[3]),
            "state_version": int(row[4]),
            "reason_code": str(row[5]) if row[5] is not None else None,
            "result": dict(row[6]) if row[6] is not None else None,
            "pending_responsibility_refs": list(row[7]),
            "content_digest": str(row[8]),
            "created_at": row[9].isoformat(),
            "updated_at": row[10].isoformat(),
        }
