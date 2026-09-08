"""Application coordination across TRADEPLAN and stateless CAP checks.

The service accepts an existing PostgreSQL connection so the caller owns one
local transaction. It never imports EXE or any venue client; planning commits only
plans, activations, and UX command state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from psycopg import Connection

from halpha.binance_contracts import parse_complete_binance_usdm_account_snapshot
from halpha.capital.checks import check_action
from halpha.capital.discipline import read_new_risk_discipline
from halpha.capital.models import (
    ActivationCapitalBoundary,
    ActionCheckInput,
    AuthorityClass,
    EnvironmentKind,
    NewRiskDisciplinePolicy,
    NewRiskDisciplineStatus,
    RiskClass,
    StopCategory,
)
from halpha.capital.repository import PostgreSQLCapitalRepository
from halpha.domain_values import content_digest
from halpha.planning.adapter import strategy_id_for_activation
from halpha.planning.models import (
    ConditionJudgement,
    ConditionResult,
    PlanActivation,
    PlanEvent,
    PlanLifecycle,
    PlanDecisionContext,
    PlanDecisionIntent,
    PositionAlignmentSpec,
    ProtectionState,
    ProposedAction,
    RunState,
    TradePlanContent,
    TradePlanDraft,
    TradePlanVersion,
    new_risk_decision_context_incompatibility,
    validate_current_plan_admission,
)
from halpha.planning.order_schedule import (
    OrderSchedulePreview,
    OrderScheduleSpec,
    validate_order_schedule_snapshot,
)
from halpha.planning.order_policies import RuntimeConditionState
from halpha.planning.registry import (
    DecisionBasisKind,
    FixedDecisionBasis,
    FixedStrategyPlanBasis,
    build_fixed_decision_basis,
    fixed_decision_basis_runtime_incompatibility,
    strategy_decision_intent_incompatibility,
)
from halpha.planning.repository import PostgreSQLPlanningRepository
from halpha.planning.strategies.one_shot import StrategyProposal
from halpha.scalping.models import (
    SCALP_WORKFLOW_KIND,
    ScalpCycleRecord,
    is_internal_scalp_cycle,
)
from halpha.scalping.repository import PostgreSQLScalpRepository
from halpha.planning.transitions import (
    ControlIntent,
    build_plan_event,
    consume_entry_opportunity,
    complete_activation,
    deadline_source_identity,
    proposed_action_from_strategy_proposal,
    record_direct_fill,
    record_first_fill,
    record_runtime_condition_state,
    resolve_existing_event,
    update_protection_projection,
)
from halpha.user_workbench.commands import Receipt, ReceiptState, advance_receipt
from halpha.user_workbench.repository import PostgreSQLCommandRepository
from halpha.venue_integration.dispatch_lock import acquire_activation_control_lock


def _entry_valid_until(
    version: TradePlanVersion,
    *,
    activated_at: datetime,
) -> datetime:
    if getattr(version, "position_alignment", None) is not None:
        return version.valid_until
    if version.decision_basis.kind is DecisionBasisKind.DIRECT_EXECUTION:
        return version.valid_until
    value = version.strategy_basis.normalized_parameters.get("entry_valid_minutes")
    if not isinstance(value, int):
        raise ValueError("ENTRY_VALID_MINUTES_INVALID")
    return min(version.valid_until, activated_at + timedelta(minutes=value))


def plan_runtime_incompatibility(
    *,
    decision_basis: FixedDecisionBasis,
    decision_context: PlanDecisionContext | None,
    order_schedule_spec: OrderScheduleSpec | None,
    allowed_actions: frozenset[str],
    position_alignment: PositionAlignmentSpec | None = None,
) -> str | None:
    """Return one bounded reason the current product cannot activate a fixed plan."""

    try:
        validate_current_plan_admission(
            decision_basis_kind=decision_basis.kind,
            order_schedule_spec=order_schedule_spec,
            allowed_actions=allowed_actions,
            position_alignment=position_alignment,
        )
    except ValueError:
        return "PLAN_ORDER_SCHEDULE_RUNTIME_INCOMPATIBLE"
    if position_alignment is None:
        context_incompatibility = new_risk_decision_context_incompatibility(
            decision_basis_kind=decision_basis.kind,
            decision_context=decision_context,
        )
        if context_incompatibility is not None:
            return context_incompatibility
        if isinstance(decision_basis, FixedStrategyPlanBasis):
            intent_incompatibility = strategy_decision_intent_incompatibility(
                decision_basis.economic_scope,
                (
                    decision_context.intent.value
                    if decision_context.intent is not None
                    else None
                ),
            )
            if intent_incompatibility is not None:
                return intent_incompatibility
    return fixed_decision_basis_runtime_incompatibility(decision_basis)


def plan_reward_risk_ratio(
    *,
    decision_basis: FixedDecisionBasis,
    order_schedule_spec: OrderScheduleSpec | None,
) -> Decimal | None:
    """Return the conservative weighted price-exit reward/risk ratio."""

    if decision_basis.kind is DecisionBasisKind.DIRECT_EXECUTION:
        if order_schedule_spec is None:
            return None
        ladder = order_schedule_spec.protection_policy.take_profit_ladder
        if ladder is None:
            return None
        return sum(
            (
                Decimal(level.trigger_r) * Decimal(level.quantity_fraction)
                for level in ladder.levels
            ),
            Decimal(0),
        )
    if not isinstance(decision_basis, FixedStrategyPlanBasis):
        return None
    parameters = decision_basis.normalized_parameters
    try:
        first_r = Decimal(str(parameters["take_profit_1_r"]))
        first_fraction = Decimal(str(parameters["take_profit_1_fraction"]))
        second_r = Decimal(str(parameters["take_profit_2_r"]))
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return None
    ratio = first_r * first_fraction + second_r * (Decimal(1) - first_fraction)
    return ratio if ratio.is_finite() and ratio > 0 else None


def require_plan_reward_risk_discipline(
    *,
    decision_basis: FixedDecisionBasis,
    order_schedule_spec: OrderScheduleSpec | None,
    policy: NewRiskDisciplinePolicy,
) -> Decimal:
    ratio = plan_reward_risk_ratio(
        decision_basis=decision_basis,
        order_schedule_spec=order_schedule_spec,
    )
    if ratio is None:
        raise ValueError("PLAN_REWARD_RISK_UNAVAILABLE")
    if ratio < Decimal(policy.minimum_reward_risk_ratio):
        raise ValueError("PLAN_REWARD_RISK_BELOW_DISCIPLINE_MINIMUM")
    return ratio


def _aware_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _alignment_position_matches(
    payload: object,
    alignment: PositionAlignmentSpec,
) -> bool:
    snapshot = parse_complete_binance_usdm_account_snapshot(payload)
    if (
        snapshot is None
        or snapshot.get("ordinary_open_order_count") != 0
        or snapshot.get("algo_open_order_count") != 0
    ):
        return False
    matches = [
        item
        for item in snapshot["positions"]
        if isinstance(item, dict)
        and item.get("instrument_ref") == alignment.instrument_ref
        and item.get("position_side") == alignment.position_side
    ]
    if len(matches) != 1:
        return False
    position = matches[0]
    try:
        quantity = Decimal(str(position["absolute_quantity"]))
        entry = Decimal(str(position["entry_price"]))
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return False
    return (
        quantity.is_finite()
        and entry.is_finite()
        and quantity == Decimal(alignment.baseline_quantity)
        and entry == Decimal(alignment.baseline_entry_price)
        and position.get("direction") == alignment.direction.value
    )


class PlanningApplicationService:
    """Coordinate owner-specific repositories without taking semantic ownership."""

    def __init__(self, connection: Connection[Any], environment_id: str) -> None:
        self._connection = connection
        self._planning = PostgreSQLPlanningRepository(connection, environment_id)
        self._capital = PostgreSQLCapitalRepository(connection, environment_id)
        self._environment_id = environment_id

    def new_risk_discipline_status(
        self,
        *,
        account_ref: str,
        policy: NewRiskDisciplinePolicy,
        observed_at: datetime,
        proposed_max_allowed_loss: str | None = None,
        proposed_max_notional: str | None = None,
        proposed_instrument_ref: str | None = None,
        proposed_direction: str | None = None,
        lock: bool = False,
    ) -> NewRiskDisciplineStatus:
        return read_new_risk_discipline(
            self._connection,
            environment_id=self._environment_id,
            account_ref=account_ref,
            policy=policy,
            observed_at=observed_at,
            proposed_max_allowed_loss=proposed_max_allowed_loss,
            proposed_max_notional=proposed_max_notional,
            proposed_instrument_ref=proposed_instrument_ref,
            proposed_direction=proposed_direction,
            lock=lock,
        )

    def _finalize_completed_receipts(
        self,
        activation: PlanActivation,
        *,
        observed_at: datetime,
    ) -> tuple[Receipt, ...]:
        if activation.lifecycle is not PlanLifecycle.COMPLETED:
            return ()
        commands = PostgreSQLCommandRepository(
            self._connection,
            self._environment_id,
        )
        finalized: list[Receipt] = []
        for command, receipt in commands.list_processing_for_target(
            activation.activation_id,
            for_update=True,
        ):
            reason = {
                ControlIntent.EXIT_STRATEGY: "EXIT_COMPLETED",
                ControlIntent.STOP_NEW_RISK: (
                    "NEW_RISK_STOPPED_AND_RESPONSIBILITIES_TERMINAL"
                ),
            }.get(command.intent)
            if reason is None:
                continue
            updated = advance_receipt(
                receipt,
                state=ReceiptState.EFFECTIVE,
                reason_code=reason,
                result={
                    "activation_id": activation.activation_id,
                    "activation_state_version": activation.state_version,
                    "result_ref": activation.result_ref,
                },
                pending_responsibility_refs=(),
                observed_at=observed_at,
            )
            commands.update_receipt(updated, expected_version=receipt.state_version)
            finalized.append(updated)
        return tuple(finalized)

    def require_current_position_alignment(
        self,
        version: TradePlanVersion,
        *,
        observed_at: datetime,
    ) -> None:
        alignment = version.position_alignment
        if alignment is None:
            return
        original = self._connection.execute(
            """
            SELECT cutoff, payload
            FROM halpha.venue_fact
            WHERE environment_id = %s
              AND venue_fact_id = %s
              AND account_ref = %s
              AND kind = 'ACCOUNT_STATE'
              AND source_class = 'VENUE_QUERY'
            """,
            (
                self._environment_id,
                alignment.snapshot_ref,
                alignment.account_ref,
            ),
        ).fetchone()
        if original is None or _aware_utc(original[0]) != alignment.fact_cutoff.astimezone(UTC):
            raise ValueError("POSITION_ALIGNMENT_BASELINE_UNKNOWN")
        if not _alignment_position_matches(original[1], alignment):
            raise ValueError("POSITION_ALIGNMENT_BASELINE_INVALID")
        latest = self._connection.execute(
            """
            SELECT cutoff, payload
            FROM halpha.venue_fact
            WHERE environment_id = %s
              AND account_ref = %s
              AND kind = 'ACCOUNT_STATE'
              AND source_class = 'VENUE_QUERY'
            ORDER BY cutoff DESC, received_at DESC, venue_fact_id DESC
            LIMIT 1
            """,
            (self._environment_id, alignment.account_ref),
        ).fetchone()
        cutoff = _aware_utc(latest[0]) if latest is not None else None
        observed = _aware_utc(observed_at)
        if (
            latest is None
            or cutoff is None
            or observed is None
            or cutoff > observed + timedelta(seconds=5)
            or observed - cutoff > timedelta(seconds=90)
        ):
            raise ValueError("POSITION_ALIGNMENT_FACT_NOT_CURRENT")
        if not _alignment_position_matches(latest[1], alignment):
            raise ValueError("POSITION_ALIGNMENT_FACT_CHANGED")

    def recover_completed_command_receipts(
        self,
        *,
        observed_at: datetime,
    ) -> tuple[Receipt, ...]:
        """Finish pre-existing receipts without relying on any page request."""

        commands = PostgreSQLCommandRepository(
            self._connection,
            self._environment_id,
        )
        finalized: list[Receipt] = []
        for activation_id in commands.list_processing_target_refs():
            acquire_activation_control_lock(
                self._connection,
                environment_id=self._environment_id,
                activation_id=activation_id,
            )
            activation = self._planning.get_activation(
                activation_id,
                for_update=True,
            )
            finalized.extend(
                self._finalize_completed_receipts(
                    activation,
                    observed_at=observed_at,
                )
            )
        return tuple(finalized)

    def create_draft(
        self,
        *,
        plan_id: str,
        content: TradePlanContent,
        observed_at: datetime,
    ) -> TradePlanDraft:
        fields = {
            "plan_id": plan_id,
            "environment_id": self._environment_id,
            "draft_version": 1,
            "content": content,
            "updated_at": observed_at,
        }
        draft = TradePlanDraft(**fields, content_digest=content_digest(content))
        self._planning.save_draft(draft, expected_version=None)
        return draft

    def update_draft(
        self,
        *,
        plan_id: str,
        expected_version: int,
        content: TradePlanContent,
        observed_at: datetime,
        preserve_system_evidence_cutoff: bool = True,
    ) -> TradePlanDraft:
        current = self._planning.get_draft(plan_id, for_update=True)
        if current.draft_version != expected_version:
            raise ValueError("PLAN_VERSION_CONFLICT")
        if self._planning.has_activation(plan_id):
            raise ValueError("PLAN_ALREADY_STARTED")
        if preserve_system_evidence_cutoff and content.decision_context is not None:
            current_context = current.content.decision_context
            content = content.model_copy(
                update={
                    "decision_context": content.decision_context.model_copy(
                        update={
                            "evidence_cutoff": (
                                current_context.evidence_cutoff
                                if current_context is not None
                                else None
                            )
                        }
                    )
                }
            )
        requested_duration = content.valid_until - content.valid_from
        comparable = content.model_copy(
            update={
                "created_at": current.content.created_at,
                "creator_kind": current.content.creator_kind,
                "valid_from": current.content.valid_from,
                "valid_until": current.content.valid_from + requested_duration,
            }
        )
        # Periodic saving must not manufacture a new draft version when the
        # trader has not changed a plan field. That could otherwise invalidate
        # an AI review already bound to this draft.
        if content_digest(current.content) == content_digest(comparable):
            return current
        content = content.model_copy(
            update={
                "created_at": current.content.created_at,
                "creator_kind": current.content.creator_kind,
            }
        )
        draft = TradePlanDraft(
            plan_id=plan_id,
            environment_id=self._environment_id,
            draft_version=expected_version + 1,
            content=content,
            content_digest=content_digest(content),
            updated_at=observed_at,
        )
        self._planning.save_draft(draft, expected_version=expected_version)
        return draft

    def delete_draft(self, *, plan_id: str, expected_version: int) -> None:
        draft = self._planning.get_draft(plan_id, for_update=True)
        if draft.draft_version != expected_version:
            raise ValueError("PLAN_VERSION_CONFLICT")
        if self._planning.has_activation(plan_id):
            raise ValueError("PLAN_ALREADY_STARTED")
        self._planning.delete_draft(plan_id, expected_version=expected_version)

    def build_activation_snapshot(
        self,
        *,
        plan_id: str,
        expected_draft_version: int,
        plan_version_id: str,
        product_build_id: str,
        snapshot_at: datetime,
        ai_review_ref: str | None = None,
        new_risk_discipline_policy: NewRiskDisciplinePolicy | None = None,
    ) -> TradePlanVersion:
        return self._build_activation_snapshot(
            plan_id=plan_id,
            expected_draft_version=expected_draft_version,
            plan_version_id=plan_version_id,
            product_build_id=product_build_id,
            snapshot_at=snapshot_at,
            ai_review_ref=ai_review_ref,
            new_risk_discipline_policy=new_risk_discipline_policy,
            scalp_cycle=None,
        )

    def _build_activation_snapshot(
        self,
        *,
        plan_id: str,
        expected_draft_version: int,
        plan_version_id: str,
        product_build_id: str,
        snapshot_at: datetime,
        ai_review_ref: str | None,
        new_risk_discipline_policy: NewRiskDisciplinePolicy | None,
        scalp_cycle: ScalpCycleRecord | None,
    ) -> TradePlanVersion:
        """Build the immutable executor input without persisting it.

        A snapshot is an implementation detail of a successful start, not a
        separately visible plan state.  The caller may validate and compile a
        schedule from this transient value, then persist it only in the same
        transaction that creates the activation.
        """

        draft = self._planning.get_draft(plan_id, for_update=True)
        if draft.draft_version != expected_draft_version:
            raise ValueError("PLAN_VERSION_CONFLICT")
        if self._planning.has_activation(plan_id):
            raise ValueError("PLAN_ALREADY_STARTED")
        content = draft.content
        self._require_scalp_cycle_binding(content, scalp_cycle)
        if scalp_cycle is not None and (
            plan_id != scalp_cycle.plan_id
            or plan_version_id != scalp_cycle.plan_version_id
        ):
            raise ValueError("SCALP_CYCLE_IDENTITY_MISMATCH")
        basis = build_fixed_decision_basis(
            content.decision_basis,
            product_build_id=product_build_id,
        )
        if content.position_alignment is None:
            if not ai_review_ref and scalp_cycle is None:
                raise ValueError("PLAN_AI_REVIEW_REQUIRED")
            context_incompatibility = new_risk_decision_context_incompatibility(
                decision_basis_kind=basis.kind,
                decision_context=content.decision_context,
            )
            if context_incompatibility is not None:
                raise ValueError(context_incompatibility)
            if isinstance(basis, FixedStrategyPlanBasis):
                intent_incompatibility = strategy_decision_intent_incompatibility(
                    basis.economic_scope,
                    (
                        content.decision_context.intent.value
                        if content.decision_context.intent is not None
                        else None
                    ),
                )
                if intent_incompatibility is not None:
                    raise ValueError(intent_incompatibility)
            if new_risk_discipline_policy is not None:
                require_plan_reward_risk_discipline(
                    decision_basis=basis,
                    order_schedule_spec=content.order_schedule_spec,
                    policy=new_risk_discipline_policy,
                )
        fields = {
            "plan_version_id": plan_version_id,
            "plan_id": plan_id,
            "environment_id": self._environment_id,
            # The persisted column retains its historical physical name.  It
            # records snapshot creation time and is never exposed as a plan
            # lifecycle state.
            "fixed_at": snapshot_at,
            "plan_name": content.plan_name,
            "created_at": content.created_at,
            "creator_kind": content.creator_kind,
            "ai_review_ref": ai_review_ref,
            "decision_context": content.decision_context,
            "decision_basis": basis,
            "order_schedule_spec": content.order_schedule_spec,
            "position_alignment": content.position_alignment,
            "account_ref": content.account_ref,
            "venue_ref": content.venue_ref,
            "instrument_ref": content.instrument_ref,
            "direction": content.direction,
            "target_exposure": content.target_exposure,
            "requested_limits": content.requested_limits,
            "valid_from": content.valid_from,
            "valid_until": content.valid_until,
            "allowed_actions": content.allowed_actions,
            "terms": content.terms,
        }
        return TradePlanVersion(**fields, content_digest=content_digest(fields))

    def fix_draft(
        self,
        *,
        plan_id: str,
        expected_draft_version: int,
        plan_version_id: str,
        product_build_id: str,
        fixed_at: datetime,
        ai_review_ref: str | None = None,
        new_risk_discipline_policy: NewRiskDisciplinePolicy | None = None,
    ) -> TradePlanVersion:
        """Persist one immutable snapshot for compatibility callers.

        Normal workbench submission uses :meth:`fix_and_activate`, which keeps
        this insert and activation creation in one transaction.
        """

        version = self.build_activation_snapshot(
            plan_id=plan_id,
            expected_draft_version=expected_draft_version,
            plan_version_id=plan_version_id,
            product_build_id=product_build_id,
            snapshot_at=fixed_at,
            ai_review_ref=ai_review_ref,
            new_risk_discipline_policy=new_risk_discipline_policy,
        )
        self._planning.insert_version(version)
        return version

    def activate_version(
        self,
        *,
        plan_version_id: str,
        activation_id: str,
        environment_kind: EnvironmentKind,
        authority_class: AuthorityClass,
        observed_at: datetime,
        order_schedule_snapshot: OrderSchedulePreview | None = None,
        new_risk_discipline_policy: NewRiskDisciplinePolicy | None = None,
        live_profit_qualification_checker: (
            Callable[[TradePlanVersion], Mapping[str, Any]] | None
        ) = None,
        ai_review_checker: (
            Callable[[TradePlanVersion], Mapping[str, Any]] | None
        ) = None,
    ) -> PlanActivation:
        return self._activate_version(
            plan_version_id=plan_version_id,
            activation_id=activation_id,
            environment_kind=environment_kind,
            authority_class=authority_class,
            observed_at=observed_at,
            order_schedule_snapshot=order_schedule_snapshot,
            new_risk_discipline_policy=new_risk_discipline_policy,
            live_profit_qualification_checker=live_profit_qualification_checker,
            ai_review_checker=ai_review_checker,
            scalp_cycle=None,
        )

    def _activate_version(
        self,
        *,
        plan_version_id: str,
        activation_id: str,
        environment_kind: EnvironmentKind,
        authority_class: AuthorityClass,
        observed_at: datetime,
        order_schedule_snapshot: OrderSchedulePreview | None,
        new_risk_discipline_policy: NewRiskDisciplinePolicy | None,
        live_profit_qualification_checker: (
            Callable[[TradePlanVersion], Mapping[str, Any]] | None
        ),
        ai_review_checker: (
            Callable[[TradePlanVersion], Mapping[str, Any]] | None
        ),
        scalp_cycle: ScalpCycleRecord | None,
    ) -> PlanActivation:
        version = self._planning.get_version(plan_version_id)
        self._require_scalp_cycle_binding(version, scalp_cycle)
        if scalp_cycle is not None and (
            version.plan_id != scalp_cycle.plan_id
            or plan_version_id != scalp_cycle.plan_version_id
            or activation_id != scalp_cycle.activation_id
        ):
            raise ValueError("SCALP_CYCLE_IDENTITY_MISMATCH")
        ai_review_snapshot: dict[str, Any] | None = None
        if version.position_alignment is None:
            if not version.ai_review_ref and scalp_cycle is None:
                raise ValueError("PLAN_AI_REVIEW_REQUIRED")
            if version.ai_review_ref is None:
                pass
            elif ai_review_checker is None:
                raise ValueError("PLAN_AI_REVIEW_NOT_CONFIGURED")
            else:
                review_result = ai_review_checker(version)
                if not isinstance(review_result, Mapping):
                    raise ValueError("PLAN_AI_REVIEW_SNAPSHOT_INVALID")
                ai_review_snapshot = dict(review_result)
                if (
                    ai_review_snapshot.get("review_id") != version.ai_review_ref
                    or ai_review_snapshot.get("status") != "APPROVED"
                    or ai_review_snapshot.get("decision") != "APPROVE"
                    or ai_review_snapshot.get("draft_content_digest") is None
                ):
                    raise ValueError("PLAN_AI_REVIEW_NOT_APPROVED")
        live_profit_qualification_snapshot: dict[str, Any] | None = None
        if (
            environment_kind is EnvironmentKind.LIVE
            and version.position_alignment is None
            and version.decision_context is not None
            and version.decision_context.intent is PlanDecisionIntent.PROFIT_SEEKING
        ):
            if live_profit_qualification_checker is None:
                raise ValueError("LIVE_PROFIT_QUALIFICATION_NOT_CONFIGURED")
            qualification_result = live_profit_qualification_checker(version)
            if not isinstance(qualification_result, Mapping):
                raise ValueError("LIVE_PROFIT_QUALIFICATION_SNAPSHOT_INVALID")
            live_profit_qualification_snapshot = dict(qualification_result)
            artifact_digest = live_profit_qualification_snapshot.get(
                "artifact_content_digest"
            )
            if (
                live_profit_qualification_snapshot.get("status")
                != "ELIGIBLE_INPUT"
                or live_profit_qualification_snapshot.get("eligible_input") is not True
                or live_profit_qualification_snapshot.get(
                    "live_activation_authority"
                )
                is not False
                or live_profit_qualification_snapshot.get(
                    "capital_scaling_authority"
                )
                is not False
                or not isinstance(artifact_digest, str)
                or len(artifact_digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in artifact_digest
                )
            ):
                raise ValueError("LIVE_PROFIT_QUALIFICATION_SNAPSHOT_INVALID")
        if (
            version.position_alignment is None
            and new_risk_discipline_policy is not None
        ):
            require_plan_reward_risk_discipline(
                decision_basis=version.decision_basis,
                order_schedule_spec=version.order_schedule_spec,
                policy=new_risk_discipline_policy,
            )
            discipline = self.new_risk_discipline_status(
                account_ref=version.account_ref,
                policy=new_risk_discipline_policy,
                observed_at=observed_at,
                proposed_max_allowed_loss=(
                    version.requested_limits.max_allowed_loss
                ),
                proposed_max_notional=version.requested_limits.max_notional,
                proposed_instrument_ref=version.instrument_ref,
                proposed_direction=version.direction.value,
                lock=True,
            )
            if not discipline.new_risk_allowed:
                raise ValueError(
                    discipline.blocker_codes[0]
                    if discipline.blocker_codes
                    else "NEW_RISK_DISCIPLINE_UNKNOWN"
                )
        self.require_current_position_alignment(
            version,
            observed_at=observed_at,
        )
        incompatibility = plan_runtime_incompatibility(
            decision_basis=version.decision_basis,
            decision_context=version.decision_context,
            order_schedule_spec=version.order_schedule_spec,
            allowed_actions=version.allowed_actions,
            position_alignment=version.position_alignment,
        )
        if incompatibility is not None:
            raise ValueError(incompatibility)
        if not (version.valid_from <= observed_at < version.valid_until):
            raise ValueError("PLAN_EXPIRED")
        existing_scope = self._planning.lock_and_list_open_instrument_activations(
            account_ref=version.account_ref,
            instrument_ref=version.instrument_ref,
        )
        if version.position_alignment is not None and existing_scope:
            raise ValueError("POSITION_ALIGNMENT_SCOPE_CONFLICT")
        if any(
            activation.lifecycle is PlanLifecycle.USER_TAKEOVER
            for activation in existing_scope
        ):
            raise ValueError("ACCOUNT_INSTRUMENT_TAKEOVER_CONFLICT")
        if any(
            activation.direction is not version.direction
            for activation in existing_scope
        ):
            raise ValueError("ACCOUNT_INSTRUMENT_DIRECTION_CONFLICT")
        entry_valid_until = _entry_valid_until(version, activated_at=observed_at)
        if (version.order_schedule_spec is None) != (order_schedule_snapshot is None):
            raise ValueError("ORDER_SCHEDULE_SNAPSHOT_REQUIRED")
        if order_schedule_snapshot is not None:
            validate_order_schedule_snapshot(order_schedule_snapshot)
            protection_estimate = (
                order_schedule_snapshot.full_fill_protection_estimate
            )
            if (
                protection_estimate is not None
                and Decimal(protection_estimate.maximum_projected_loss)
                > Decimal(version.requested_limits.max_allowed_loss)
            ):
                raise ValueError(
                    "ORDER_SCHEDULE_LOSS_BUDGET_EXCEEDS_PLAN_LIMIT"
                )
            if (
                not order_schedule_snapshot.valid
                or order_schedule_snapshot.schedule_ref != version.plan_version_id
                or content_digest(order_schedule_snapshot.schedule_spec)
                != content_digest(version.order_schedule_spec)
                or order_schedule_snapshot.venue_ref != version.venue_ref
                or order_schedule_snapshot.instrument_ref != version.instrument_ref
                or order_schedule_snapshot.direction is not version.direction
                or order_schedule_snapshot.max_notional
                != version.requested_limits.max_notional
            ):
                raise ValueError("ORDER_SCHEDULE_SNAPSHOT_MISMATCH")
        activation = PlanActivation(
            activation_id=activation_id,
            environment_id=self._environment_id,
            environment_kind=environment_kind,
            authority_class=authority_class,
            plan_version_ref=plan_version_id,
            account_ref=version.account_ref,
            instrument_ref=version.instrument_ref,
            direction=version.direction,
            decision_basis_ref=version.decision_basis.decision_basis_ref,
            framework_strategy_id=strategy_id_for_activation(activation_id),
            order_schedule_snapshot=order_schedule_snapshot,
            position_alignment=version.position_alignment,
            target_exposure=version.target_exposure,
            lifecycle=(
                PlanLifecycle.EXITING
                if version.position_alignment is not None
                else PlanLifecycle.RUNNING
            ),
            entry_opportunity_consumed=version.position_alignment is not None,
            rule_state={
                "deadlines": {"entry_valid_until": entry_valid_until.isoformat()},
                "condition_judgements": {},
                "last_bar_cursors": {},
                **(
                    {"ai_review": ai_review_snapshot}
                    if ai_review_snapshot is not None
                    else {}
                ),
                **(
                    {
                        "live_profit_qualification": (
                            live_profit_qualification_snapshot
                        )
                    }
                    if live_profit_qualification_snapshot is not None
                    else {}
                ),
            },
            protection_state=ProtectionState.NONE,
            created_at=observed_at,
            updated_at=observed_at,
        )
        self._planning.insert_activation(activation)
        return activation

    def record_plan_event(
        self,
        *,
        plan_event_id: str,
        activation_id: str,
        rule_id: str,
        source_identity: str,
        source_cutoff: datetime,
        input_digest: str,
        reason_code: str,
        proposed_action: ProposedAction | None,
        no_action_reason: str | None,
        condition_judgement: ConditionJudgement | None,
        capital_decision: dict[str, object],
        created_at: datetime,
    ) -> PlanEvent:
        """Append or replay one source-identity event under the activation lock."""

        activation = self._planning.get_activation(activation_id, for_update=True)
        existing = self._planning.find_event_by_source(activation_id, source_identity)
        replay = resolve_existing_event(
            existing,
            source_identity=source_identity,
            input_digest=input_digest,
        )
        if replay is not None:
            return replay
        event = build_plan_event(
            plan_event_id=plan_event_id,
            activation=activation,
            rule_id=rule_id,
            source_identity=source_identity,
            source_cutoff=source_cutoff,
            input_digest=input_digest,
            reason_code=reason_code,
            proposed_action=proposed_action,
            no_action_reason=no_action_reason,
            condition_judgement=condition_judgement,
            capital_decision=capital_decision,
            created_at=created_at,
        )
        self._planning.insert_event(event)
        return event

    def consume_strategy_proposal(
        self,
        *,
        plan_event_id: str,
        proposal: StrategyProposal,
        action_check: ActionCheckInput,
        entry_responsibility_open: bool,
        created_at: datetime,
    ) -> PlanEvent:
        """Normalize one proposal, perform CAP's first check, and append one event."""

        activation = self._planning.get_activation(
            proposal.activation_id,
            for_update=True,
        )
        existing = self._planning.find_event_by_source(
            activation.activation_id,
            proposal.source_identity,
        )
        replay = resolve_existing_event(
            existing,
            source_identity=proposal.source_identity,
            input_digest=proposal.input_digest,
        )
        if replay is not None:
            return replay

        proposed_action = proposed_action_from_strategy_proposal(activation, proposal)
        block_reason = None
        if (
            activation.lifecycle is not PlanLifecycle.RUNNING
            or activation.run_state is not RunState.ACTIVE
            or activation.entry_opportunity_consumed
        ):
            block_reason = "NEW_RISK_STOPPED"
        elif entry_responsibility_open:
            block_reason = "ENTRY_RESPONSIBILITY_OPEN"
        if block_reason is not None:
            event = build_plan_event(
                plan_event_id=plan_event_id,
                activation=activation,
                rule_id=proposal.rule_id,
                source_identity=proposal.source_identity,
                source_cutoff=proposal.source_cutoff,
                input_digest=proposal.input_digest,
                reason_code=block_reason,
                proposed_action=None,
                no_action_reason=block_reason,
                condition_judgement=ConditionJudgement(
                    rule_id=proposal.rule_id,
                    source_identity=proposal.source_identity,
                    source_cutoff=proposal.source_cutoff,
                    input_digest=proposal.input_digest,
                    result=ConditionResult.TRUE,
                    reason_code=proposal.reason_code,
                    next_responsibility="NONE",
                ),
                capital_decision={
                    "accepted": False,
                    "reason_code": f"NOT_EVALUATED_{block_reason}",
                },
                created_at=created_at,
            )
            self._planning.insert_event(event)
            return event
        if created_at >= proposal.valid_until:
            event = build_plan_event(
                plan_event_id=plan_event_id,
                activation=activation,
                rule_id=proposal.rule_id,
                source_identity=proposal.source_identity,
                source_cutoff=proposal.source_cutoff,
                input_digest=proposal.input_digest,
                reason_code="PROPOSAL_EXPIRED",
                proposed_action=None,
                no_action_reason="PROPOSAL_EXPIRED",
                condition_judgement=ConditionJudgement(
                    rule_id=proposal.rule_id,
                    source_identity=proposal.source_identity,
                    source_cutoff=proposal.source_cutoff,
                    input_digest=proposal.input_digest,
                    result=ConditionResult.MISSED,
                    reason_code="PROPOSAL_EXPIRED",
                    next_responsibility="NONE",
                ),
                capital_decision={
                    "accepted": False,
                    "reason_code": "NOT_EVALUATED_PROPOSAL_EXPIRED",
                },
                created_at=created_at,
            )
            self._planning.insert_event(event)
            return event

        self._validate_entry_action_check(
            activation=activation,
            proposal=proposal,
            action=action_check,
            created_at=created_at,
        )
        version = self._planning.get_version(activation.plan_version_ref)
        boundary = ActivationCapitalBoundary(
            activation_id=activation.activation_id,
            environment_id=activation.environment_id,
            environment_kind=activation.environment_kind,
            authority_class=activation.authority_class,
            account_ref=activation.account_ref,
            instrument_ref=activation.instrument_ref,
            valid_from=version.valid_from,
            valid_until=version.valid_until,
            allowed_actions=version.allowed_actions,
            max_margin=version.requested_limits.max_margin,
            max_notional=version.requested_limits.max_notional,
            max_allowed_loss=version.requested_limits.max_allowed_loss,
            lifecycle=activation.lifecycle.value,
            responsibility_owner=activation.responsibility_owner,
        )
        stop_states = self._capital.lock_current_stop_states(
            account_ref=activation.account_ref,
            activation_id=activation.activation_id,
        )
        decision = check_action(
            action_check,
            boundary=boundary,
            stop_states=stop_states,
        )
        condition = ConditionJudgement(
            rule_id=proposal.rule_id,
            source_identity=proposal.source_identity,
            source_cutoff=proposal.source_cutoff,
            input_digest=proposal.input_digest,
            result=ConditionResult.TRUE,
            reason_code=proposal.reason_code,
            next_responsibility="EXE" if decision.accepted else "NONE",
        )
        event = build_plan_event(
            plan_event_id=plan_event_id,
            activation=activation,
            rule_id=proposal.rule_id,
            source_identity=proposal.source_identity,
            source_cutoff=proposal.source_cutoff,
            input_digest=proposal.input_digest,
            reason_code=(
                "PROPOSED_ACTION_CAP_ACCEPTED"
                if decision.accepted
                else "PROPOSED_ACTION_CAP_REJECTED"
            ),
            proposed_action=proposed_action,
            no_action_reason=None,
            condition_judgement=condition,
            capital_decision=decision.model_dump(mode="json"),
            created_at=created_at,
        )
        self._planning.insert_event(event)
        return event

    @staticmethod
    def _validate_entry_action_check(
        *,
        activation: PlanActivation,
        proposal: StrategyProposal,
        action: ActionCheckInput,
        created_at: datetime,
    ) -> None:
        if (
            action.environment_id != activation.environment_id
            or action.environment_kind is not activation.environment_kind
            or action.authority_class is not activation.authority_class
            or action.activation_id != activation.activation_id
            or action.account_ref != activation.account_ref
            or action.instrument_ref != activation.instrument_ref
            or action.action_profile != proposal.action_profile
            or action.control_category is not StopCategory.NEW_RISK
            or action.risk_class is not RiskClass.RISK_INCREASING
            or action.quantized_quantity != proposal.quantity
            or action.checked_at != created_at
        ):
            raise ValueError("PLAN_BOUNDARY_MISMATCH")

    def pause_for_writer_continuity_loss(self, observed_at: datetime) -> int:
        """Stop new actions before a replacement Executor resumes responsibility."""

        return self._planning.pause_all_open_for_writer_continuity_loss(observed_at)

    def get_activation(
        self, activation_id: str, *, for_update: bool = False
    ) -> PlanActivation:
        """Return the TRADEPLAN-owned activation through its public boundary."""

        return self._planning.get_activation(activation_id, for_update=for_update)

    def list_runtime_responsibility_activations(
        self,
    ) -> tuple[PlanActivation, ...]:
        return self._planning.list_runtime_responsibility_activations()

    def list_account_instrument_activations(
        self,
        *,
        account_ref: str,
        instrument_ref: str,
    ) -> tuple[PlanActivation, ...]:
        return self._planning.list_account_instrument_activations(
            account_ref=account_ref,
            instrument_ref=instrument_ref,
        )

    def record_runtime_condition_state(
        self,
        *,
        activation_id: str,
        state_key: str,
        state: RuntimeConditionState,
    ) -> PlanActivation:
        activation = self._planning.get_activation(activation_id, for_update=True)
        updated = record_runtime_condition_state(
            activation,
            state_key=state_key,
            state=state,
        )
        if updated is not activation:
            self._planning.update_activation(
                updated,
                expected_version=activation.state_version,
            )
        return updated

    def record_first_fill(
        self,
        *,
        activation_id: str,
        entry_action_ref: str,
        fill_fact_ref: str,
        fill_price: str,
        fill_time: datetime,
        entry_risk_context: dict[str, object],
        observed_at: datetime,
    ) -> PlanActivation:
        activation = self._planning.get_activation(activation_id, for_update=True)
        updated = record_first_fill(
            activation,
            entry_action_ref=entry_action_ref,
            fill_fact_ref=fill_fact_ref,
            fill_price=fill_price,
            fill_time=fill_time,
            entry_risk_context=entry_risk_context,
            observed_at=observed_at,
        )
        if updated is not activation:
            self._planning.update_activation(
                updated,
                expected_version=activation.state_version,
            )
        return updated

    def record_direct_fill(
        self,
        *,
        activation_id: str,
        entry_action_ref: str,
        fill_fact_ref: str,
        fill_price: str,
        fill_quantity: str,
        fill_time: datetime,
        protection_policy: dict[str, object],
        price_tick_size: str,
        quantity_step: str,
        observed_at: datetime,
    ) -> PlanActivation:
        activation = self._planning.get_activation(activation_id, for_update=True)
        updated = record_direct_fill(
            activation,
            entry_action_ref=entry_action_ref,
            fill_fact_ref=fill_fact_ref,
            fill_price=fill_price,
            fill_quantity=fill_quantity,
            fill_time=fill_time,
            protection_policy=protection_policy,
            price_tick_size=price_tick_size,
            quantity_step=quantity_step,
            observed_at=observed_at,
        )
        if updated is not activation:
            self._planning.update_activation(
                updated,
                expected_version=activation.state_version,
            )
        return updated

    def update_protection_projection(
        self,
        *,
        activation_id: str,
        protection_state: ProtectionState,
        pending_action_digest: str | None,
        observed_at: datetime,
    ) -> PlanActivation:
        activation = self._planning.get_activation(activation_id, for_update=True)
        updated = update_protection_projection(
            activation,
            protection_state=protection_state,
            pending_action_digest=pending_action_digest,
            observed_at=observed_at,
        )
        if updated is not activation:
            self._planning.update_activation(
                updated,
                expected_version=activation.state_version,
            )
        return updated

    def complete_with_execution_closure(
        self,
        *,
        activation_id: str,
        closure_digest: str,
        result_ref: str,
        observed_at: datetime,
    ) -> PlanActivation:
        acquire_activation_control_lock(
            self._connection,
            environment_id=self._environment_id,
            activation_id=activation_id,
        )
        activation = self._planning.get_activation(activation_id, for_update=True)
        completed = complete_activation(
            activation,
            closure_digest=closure_digest,
            result_ref=result_ref,
            observed_at=observed_at,
        )
        self._planning.update_activation(
            completed,
            expected_version=activation.state_version,
        )
        self._finalize_completed_receipts(completed, observed_at=observed_at)
        return completed

    def expire_entry_deadline(
        self,
        *,
        activation_id: str,
        plan_event_id: str,
        observed_at: datetime,
    ) -> tuple[PlanActivation, PlanEvent]:
        """Persist one deadline event and irreversibly consume the entry window."""

        activation = self._planning.get_activation(activation_id, for_update=True)
        deadline_value = (
            activation.rule_state.get("deadlines", {}).get("entry_valid_until")
            if isinstance(activation.rule_state.get("deadlines"), dict)
            else None
        )
        if not isinstance(deadline_value, str):
            raise ValueError("ENTRY_DEADLINE_MISSING")
        try:
            deadline = datetime.fromisoformat(deadline_value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("ENTRY_DEADLINE_INVALID") from None
        if observed_at < deadline:
            raise ValueError("ENTRY_DEADLINE_NOT_REACHED")
        source_identity = deadline_source_identity(
            activation_id=activation.activation_id,
            rule_id="ENTRY_DEADLINE",
            deadline=deadline,
        )
        input_digest = content_digest(
            {
                "activation_id": activation.activation_id,
                "rule_id": "ENTRY_DEADLINE",
                "deadline": deadline,
            }
        )
        existing = self._planning.find_event_by_source(
            activation.activation_id,
            source_identity,
        )
        replay = resolve_existing_event(
            existing,
            source_identity=source_identity,
            input_digest=input_digest,
        )
        if replay is not None:
            return activation, replay
        event = build_plan_event(
            plan_event_id=plan_event_id,
            activation=activation,
            rule_id="ENTRY_DEADLINE",
            source_identity=source_identity,
            source_cutoff=deadline,
            input_digest=input_digest,
            reason_code="ENTRY_DEADLINE_EXPIRED",
            proposed_action=None,
            no_action_reason="ENTRY_WINDOW_EXPIRED",
            condition_judgement=None,
            capital_decision={
                "accepted": False,
                "reason_code": "ENTRY_WINDOW_EXPIRED",
            },
            created_at=observed_at,
        )
        self._planning.insert_event(event)
        consumed = consume_entry_opportunity(activation, observed_at=observed_at)
        if consumed.state_version != activation.state_version:
            self._planning.update_activation(
                consumed,
                expected_version=activation.state_version,
            )
        return consumed, event

    def expire_remaining_entry_opportunity(
        self,
        *,
        activation_id: str,
        plan_event_id: str,
        source_cutoff: datetime,
        observed_at: datetime,
    ) -> tuple[PlanActivation, PlanEvent]:
        """Consume entry after the frozen post-submission wait has elapsed."""

        if source_cutoff.utcoffset() is None or observed_at.utcoffset() is None:
            raise ValueError("ENTRY_REMAINING_DEADLINE_TIMEZONE_REQUIRED")
        if observed_at < source_cutoff:
            raise ValueError("ENTRY_REMAINING_DEADLINE_NOT_REACHED")
        activation = self._planning.get_activation(activation_id, for_update=True)
        rule_id = "ENTRY_REMAINING_EXPIRY"
        source_identity = deadline_source_identity(
            activation_id=activation.activation_id,
            rule_id=rule_id,
            deadline=source_cutoff,
        )
        input_digest = content_digest(
            {
                "activation_id": activation.activation_id,
                "rule_id": rule_id,
                "deadline": source_cutoff,
            }
        )
        existing = self._planning.find_event_by_source(
            activation.activation_id,
            source_identity,
        )
        replay = resolve_existing_event(
            existing,
            source_identity=source_identity,
            input_digest=input_digest,
        )
        if replay is not None:
            return activation, replay
        event = build_plan_event(
            plan_event_id=plan_event_id,
            activation=activation,
            rule_id=rule_id,
            source_identity=source_identity,
            source_cutoff=source_cutoff,
            input_digest=input_digest,
            reason_code="ENTRY_REMAINING_EXPIRED",
            proposed_action=None,
            no_action_reason="ENTRY_REMAINING_EXPIRED",
            condition_judgement=None,
            capital_decision={
                "accepted": False,
                "reason_code": "ENTRY_REMAINING_EXPIRED",
            },
            created_at=observed_at,
        )
        self._planning.insert_event(event)
        consumed = consume_entry_opportunity(activation, observed_at=observed_at)
        if consumed.state_version != activation.state_version:
            self._planning.update_activation(
                consumed,
                expected_version=activation.state_version,
            )
        return consumed, event

    def invalidate_entry_opportunity(
        self,
        *,
        activation_id: str,
        plan_event_id: str,
        source_cutoff: datetime,
        evidence: dict[str, Any],
        observed_at: datetime,
    ) -> tuple[PlanActivation, PlanEvent]:
        """Persist one market-invalidation event and consume the entry opportunity."""

        activation = self._planning.get_activation(activation_id, for_update=True)
        rule_id = "ENTRY_MARKET_INVALIDATION"
        source_identity = f"{activation.activation_id}:DYNAMIC:{rule_id}"
        input_digest = content_digest(
            {
                "activation_id": activation.activation_id,
                "rule_id": rule_id,
                "evidence": evidence,
            }
        )
        existing = self._planning.find_event_by_source(
            activation.activation_id,
            source_identity,
        )
        replay = resolve_existing_event(
            existing,
            source_identity=source_identity,
            input_digest=input_digest,
        )
        if replay is not None:
            return activation, replay
        judgement = ConditionJudgement(
            rule_id=rule_id,
            source_identity=source_identity,
            source_cutoff=source_cutoff,
            input_digest=input_digest,
            result=ConditionResult.TRUE,
            reason_code="ENTRY_MARKET_INVALIDATED",
            next_responsibility="NONE",
        )
        event = build_plan_event(
            plan_event_id=plan_event_id,
            activation=activation,
            rule_id=rule_id,
            source_identity=source_identity,
            source_cutoff=source_cutoff,
            input_digest=input_digest,
            reason_code="ENTRY_MARKET_INVALIDATED",
            proposed_action=None,
            no_action_reason="ENTRY_MARKET_INVALIDATED",
            condition_judgement=judgement,
            capital_decision={
                "accepted": False,
                "reason_code": "ENTRY_MARKET_INVALIDATED",
                "evidence": evidence,
            },
            created_at=observed_at,
        )
        self._planning.insert_event(event)
        consumed = consume_entry_opportunity(activation, observed_at=observed_at)
        if consumed.state_version != activation.state_version:
            self._planning.update_activation(
                consumed,
                expected_version=activation.state_version,
            )
        return consumed, event

    def _require_scalp_cycle_binding(
        self,
        value: TradePlanContent | TradePlanVersion,
        cycle: ScalpCycleRecord | None,
    ) -> None:
        if cycle is None:
            if value.terms.get("workflow_kind") == SCALP_WORKFLOW_KIND:
                raise ValueError("SCALP_CYCLE_INTERNAL_COMMAND_REQUIRED")
            return
        if (
            not is_internal_scalp_cycle(value)
            or cycle.environment_id != self._environment_id
            or value.environment_id != cycle.environment_id
            or value.account_ref != cycle.account_ref
            or value.instrument_ref != cycle.instrument_ref
            or value.direction is not cycle.direction
            or value.created_at != cycle.triggered_at
            or cycle.template_digest != cycle.template.digest
            or value.terms.get("scalp_template_digest") != cycle.template_digest
        ):
            raise ValueError("SCALP_CYCLE_IDENTITY_MISMATCH")

    def create_scalp_cycle(
        self,
        *,
        cycle: ScalpCycleRecord,
        content: TradePlanContent,
        environment_kind: EnvironmentKind,
        authority_class: AuthorityClass,
        product_build_id: str,
        order_schedule_snapshot: OrderSchedulePreview,
        live_profit_qualification_checker: (
            Callable[[TradePlanVersion], Mapping[str, Any]] | None
        ),
        new_risk_discipline_policy: NewRiskDisciplinePolicy,
    ) -> tuple[TradePlanVersion, PlanActivation]:
        """Create the server-built cycle and its only activation in one transaction.

        Only the dedicated scalping command calls this entry point. The ordinary
        plan methods cannot accept a cycle identity or request the review exemption.
        The caller owns the transaction and has resolved request idempotency.
        """

        self._require_scalp_cycle_binding(content, cycle)
        repository = PostgreSQLScalpRepository(self._connection, self._environment_id)
        repository.lock_open_cycle_scope(account_ref=cycle.account_ref)
        if repository.has_open_cycle(account_ref=cycle.account_ref):
            raise ValueError("SCALP_CYCLE_ALREADY_OPEN")
        self.create_draft(
            plan_id=cycle.plan_id,
            content=content,
            observed_at=cycle.triggered_at,
        )
        version = self._build_activation_snapshot(
            plan_id=cycle.plan_id,
            expected_draft_version=1,
            plan_version_id=cycle.plan_version_id,
            product_build_id=product_build_id,
            snapshot_at=cycle.triggered_at,
            ai_review_ref=None,
            new_risk_discipline_policy=new_risk_discipline_policy,
            scalp_cycle=cycle,
        )
        self._planning.insert_version(version)
        activation = self._activate_version(
            plan_version_id=cycle.plan_version_id,
            activation_id=cycle.activation_id,
            environment_kind=environment_kind,
            authority_class=authority_class,
            observed_at=cycle.triggered_at,
            order_schedule_snapshot=order_schedule_snapshot,
            new_risk_discipline_policy=new_risk_discipline_policy,
            live_profit_qualification_checker=live_profit_qualification_checker,
            ai_review_checker=None,
            scalp_cycle=cycle,
        )
        repository.insert(cycle)
        return version, activation

    def fix_and_activate(
        self,
        *,
        plan_id: str,
        expected_draft_version: int,
        plan_version_id: str,
        activation_id: str,
        environment_kind: EnvironmentKind,
        authority_class: AuthorityClass,
        product_build_id: str,
        observed_at: datetime,
        ai_review_ref: str | None = None,
        order_schedule_snapshot: OrderSchedulePreview | None = None,
        ai_review_checker: (
            Callable[[TradePlanVersion], Mapping[str, Any]] | None
        ) = None,
        live_profit_qualification_checker: (
            Callable[[TradePlanVersion], Mapping[str, Any]] | None
        ) = None,
        new_risk_discipline_policy: NewRiskDisciplinePolicy | None = None,
    ) -> tuple[TradePlanVersion, PlanActivation]:
        """Persist a run snapshot and activation as one transaction unit.

        The caller owns the transaction.  If any current-fact, discipline,
        schedule, or activation check fails, the inserted snapshot rolls back
        with the activation and the editable draft remains unchanged.
        """

        version = self.fix_draft(
            plan_id=plan_id,
            expected_draft_version=expected_draft_version,
            plan_version_id=plan_version_id,
            product_build_id=product_build_id,
            fixed_at=observed_at,
            ai_review_ref=ai_review_ref,
            new_risk_discipline_policy=new_risk_discipline_policy,
        )
        activation = self.activate_version(
            plan_version_id=plan_version_id,
            activation_id=activation_id,
            environment_kind=environment_kind,
            authority_class=authority_class,
            observed_at=observed_at,
            order_schedule_snapshot=order_schedule_snapshot,
            ai_review_checker=ai_review_checker,
            live_profit_qualification_checker=live_profit_qualification_checker,
            new_risk_discipline_policy=new_risk_discipline_policy,
        )
        return version, activation
