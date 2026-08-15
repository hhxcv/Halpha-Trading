"""TRADEPLAN immutable values and the one mutable draft snapshot."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import re
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from halpha.capital.models import AuthorityClass, EnvironmentKind
from halpha.domain_values import canonical_decimal, decimal_from_string
from halpha.planning.order_schedule import (
    OrderSchedulePreview,
    OrderScheduleSpec,
    direct_allowed_action_profiles,
    validate_current_order_schedule_support,
    validate_order_schedule_snapshot,
)
from halpha.planning.registry import (
    DIRECT_EXECUTION_REF,
    DecisionBasisKind,
    Direction,
    DraftDecisionBasis,
    FixedDecisionBasis,
    FixedStrategyPlanBasis,
)


class PlanLifecycle(StrEnum):
    RUNNING = "RUNNING"
    EXITING = "EXITING"
    USER_TAKEOVER = "USER_TAKEOVER"
    COMPLETED = "COMPLETED"
    UNKNOWN = "UNKNOWN"


class RunState(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"


class ProtectionState(StrEnum):
    NONE = "NONE"
    WORKING = "WORKING"
    UNKNOWN = "UNKNOWN"
    GAP = "GAP"
    CLOSED = "CLOSED"


class PlanCreatorKind(StrEnum):
    HUMAN = "HUMAN"
    AI = "AI"
    MONITOR = "MONITOR"


class PlanDecisionIntent(StrEnum):
    """Why this plan is allowed to consume one risk attempt."""

    PROFIT_SEEKING = "PROFIT_SEEKING"
    VALIDATION = "VALIDATION"


class PlanSetupFamily(StrEnum):
    """Small, stable comparison buckets; never executable conditions."""

    BREAKOUT_CONTINUATION = "BREAKOUT_CONTINUATION"
    PULLBACK_CONTINUATION = "PULLBACK_CONTINUATION"
    RANGE_MEAN_REVERSION = "RANGE_MEAN_REVERSION"
    REVERSAL = "REVERSAL"
    EVENT_DRIVEN = "EVENT_DRIVEN"
    OTHER = "OTHER"


class PositionAlignmentOperation(StrEnum):
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"


POSITION_ALIGNMENT_ALLOWED_ACTIONS = frozenset({"REDUCE_OR_CLOSE_MARKET"})


PERSISTED_HISTORY_CONTEXT_KEY = "persisted_history"
PLAYBOOK_REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$"
_PLAYBOOK_REF_RE = re.compile(PLAYBOOK_REF_PATTERN)


def validate_current_plan_admission(
    *,
    decision_basis_kind: DecisionBasisKind,
    order_schedule_spec: OrderScheduleSpec | None,
    allowed_actions: frozenset[str],
    position_alignment: PositionAlignmentSpec | None = None,
) -> None:
    """Apply release-current admission without redefining persisted history."""

    if position_alignment is not None:
        if (
            decision_basis_kind is not DecisionBasisKind.DIRECT_EXECUTION
            or order_schedule_spec is not None
            or allowed_actions != POSITION_ALIGNMENT_ALLOWED_ACTIONS
        ):
            raise ValueError("POSITION_ALIGNMENT_PLAN_SHAPE_INVALID")
        return

    validate_current_order_schedule_support(
        decision_basis_kind,
        order_schedule_spec,
    )
    if (
        decision_basis_kind is DecisionBasisKind.DIRECT_EXECUTION
        and allowed_actions != direct_allowed_action_profiles(order_schedule_spec)
    ):
        raise ValueError("DIRECT_EXECUTION_ACTION_SCOPE_MISMATCH")


def _is_persisted_history(info: ValidationInfo) -> bool:
    return bool(
        info.context
        and info.context.get(PERSISTED_HISTORY_CONTEXT_KEY) is True
    )


class ConditionResult(StrEnum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    MISSED = "MISSED"
    INVALID = "INVALID"


class ProposedActionKind(StrEnum):
    ENTRY = "ENTRY"
    CANCEL = "CANCEL"
    PROTECTION = "PROTECTION"
    TAKE_PROFIT = "TAKE_PROFIT"
    RISK_REDUCTION = "RISK_REDUCTION"
    EXIT = "EXIT"


class PlanningModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_serialization_defaults_required=True,
    )


class RequestedLimits(PlanningModel):
    max_margin: str
    max_notional: str
    max_allowed_loss: str

    @field_validator("max_margin", "max_notional", "max_allowed_loss")
    @classmethod
    def positive_amounts(cls, value: str) -> str:
        return canonical_decimal(
            decimal_from_string(value, code="PLAN_LIMIT_INVALID", positive=True)
        )


class PlanDecisionContext(PlanningModel):
    """Human-readable decision record; never an executable trading condition."""

    # Drafts preserve unfinished user input as-is.  The admission gate below
    # remains the single definition of when the record is complete enough to
    # be reviewed, fixed, or activated.
    rationale: str | None = Field(default=None, max_length=2000)
    evidence: str | None = Field(default=None, max_length=2000)
    limitations: str | None = Field(default=None, max_length=2000)
    intent: PlanDecisionIntent | None = None
    setup_family: PlanSetupFamily | None = None
    playbook_ref: str | None = Field(
        default=None,
        min_length=1,
        max_length=96,
        pattern=PLAYBOOK_REF_PATTERN,
    )
    invalidation: str | None = Field(default=None, max_length=2000)
    evidence_cutoff: datetime | None = None

    @field_validator("rationale", "evidence", "limitations", "invalidation")
    @classmethod
    def text_is_readable(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized or len(normalized) > 2000 or "\x00" in normalized:
            raise ValueError("PLAN_DECISION_CONTEXT_INVALID")
        return normalized

    @field_validator("playbook_ref", mode="before")
    @classmethod
    def playbook_ref_is_stable(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("PLAN_DECISION_PLAYBOOK_REF_INVALID")
        normalized = value.strip()
        if _PLAYBOOK_REF_RE.fullmatch(normalized) is None:
            raise ValueError("PLAN_DECISION_PLAYBOOK_REF_INVALID")
        return normalized

    @field_validator("evidence_cutoff")
    @classmethod
    def evidence_cutoff_is_aware(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("PLAN_DECISION_EVIDENCE_CUTOFF_INVALID")
        return value

    @property
    def experiment_complete(self) -> bool:
        return (
            self.rationale is not None
            and self.intent is not None
            and self.setup_family is not None
            and self.evidence_cutoff is not None
        )


def new_risk_decision_context_incompatibility(
    *,
    decision_basis_kind: DecisionBasisKind,
    decision_context: PlanDecisionContext | None,
) -> str | None:
    """Return one current-admission reason without invalidating old history."""

    if decision_context is None or not decision_context.experiment_complete:
        return "PLAN_DECISION_EXPERIMENT_INCOMPLETE"
    if (
        decision_basis_kind is DecisionBasisKind.STRATEGY_SIGNAL
        and decision_context.playbook_ref is not None
    ):
        return "PLAN_DECISION_PLAYBOOK_REF_UNEXPECTED"
    return None


class PositionAlignmentSpec(PlanningModel):
    """One immutable external-position baseline and its bounded reduction."""

    schema_version: Literal["HALPHA_POSITION_ALIGNMENT_V1"] = (
        "HALPHA_POSITION_ALIGNMENT_V1"
    )
    operation: PositionAlignmentOperation
    snapshot_ref: str
    fact_cutoff: datetime
    account_ref: str
    venue_ref: str
    instrument_ref: str
    direction: Direction
    position_side: Literal["BOTH", "LONG", "SHORT"]
    baseline_quantity: str
    requested_reduction_quantity: str
    target_quantity_after: str
    baseline_entry_price: str
    baseline_mark_price: str

    @field_validator(
        "baseline_quantity",
        "requested_reduction_quantity",
        "baseline_entry_price",
        "baseline_mark_price",
    )
    @classmethod
    def positive_decimals(cls, value: str) -> str:
        return canonical_decimal(
            decimal_from_string(
                value,
                code="POSITION_ALIGNMENT_VALUE_INVALID",
                positive=True,
            )
        )

    @field_validator("target_quantity_after")
    @classmethod
    def non_negative_target(cls, value: str) -> str:
        normalized = canonical_decimal(
            decimal_from_string(
                value,
                code="POSITION_ALIGNMENT_VALUE_INVALID",
            )
        )
        if normalized.startswith("-"):
            raise ValueError("POSITION_ALIGNMENT_VALUE_INVALID")
        return normalized

    @field_validator("snapshot_ref")
    @classmethod
    def snapshot_identity_is_bounded(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 160:
            raise ValueError("POSITION_ALIGNMENT_SNAPSHOT_INVALID")
        return normalized

    @model_validator(mode="after")
    def reduction_matches_baseline(self) -> PositionAlignmentSpec:
        baseline = decimal_from_string(
            self.baseline_quantity,
            code="POSITION_ALIGNMENT_VALUE_INVALID",
            positive=True,
        )
        reduction = decimal_from_string(
            self.requested_reduction_quantity,
            code="POSITION_ALIGNMENT_VALUE_INVALID",
            positive=True,
        )
        target = decimal_from_string(
            self.target_quantity_after,
            code="POSITION_ALIGNMENT_VALUE_INVALID",
        )
        if reduction > baseline or target != baseline - reduction:
            raise ValueError("POSITION_ALIGNMENT_QUANTITY_MISMATCH")
        if (
            self.operation is PositionAlignmentOperation.CLOSE
            and target != 0
        ) or (
            self.operation is PositionAlignmentOperation.REDUCE
            and target <= 0
        ):
            raise ValueError("POSITION_ALIGNMENT_OPERATION_MISMATCH")
        if self.fact_cutoff.utcoffset() is None:
            raise ValueError("POSITION_ALIGNMENT_CUTOFF_INVALID")
        if self.position_side != "BOTH" and self.position_side != self.direction.value:
            raise ValueError("POSITION_ALIGNMENT_SIDE_DIRECTION_MISMATCH")
        return self


class TradePlanContent(PlanningModel):
    plan_name: str | None = None
    created_at: datetime | None = None
    creator_kind: PlanCreatorKind | None = None
    decision_context: PlanDecisionContext | None = None
    decision_basis: DraftDecisionBasis
    order_schedule_spec: OrderScheduleSpec | None = None
    position_alignment: PositionAlignmentSpec | None = None
    environment_id: str
    environment_kind: EnvironmentKind
    authority_class: AuthorityClass
    account_ref: str
    venue_ref: str
    instrument_ref: str
    direction: Direction
    target_exposure: str
    requested_limits: RequestedLimits
    valid_from: datetime
    valid_until: datetime
    allowed_actions: frozenset[str]
    terms: dict[str, Any]

    @field_validator("plan_name")
    @classmethod
    def plan_name_is_readable(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or len(normalized) > 80:
            raise ValueError("PLAN_NAME_INVALID")
        return normalized

    @field_validator("target_exposure")
    @classmethod
    def target_is_positive(cls, value: str) -> str:
        return canonical_decimal(
            decimal_from_string(value, code="TARGET_EXPOSURE_INVALID", positive=True)
        )

    @model_validator(mode="after")
    def boundaries_are_consistent(self, info: ValidationInfo) -> TradePlanContent:
        expected = (
            AuthorityClass.DEMO_VALIDATION
            if self.environment_kind is EnvironmentKind.DEMO
            else AuthorityClass.LIVE_REAL_CAPITAL
        )
        if self.authority_class is not expected:
            raise ValueError("AUTHORITY_ENVIRONMENT_MISMATCH")
        if self.valid_until <= self.valid_from:
            raise ValueError("PLAN_WINDOW_INVALID")
        if not self.allowed_actions:
            raise ValueError("PLAN_ACTIONS_EMPTY")
        if not _is_persisted_history(info):
            validate_current_plan_admission(
                decision_basis_kind=self.decision_basis.kind,
                order_schedule_spec=self.order_schedule_spec,
                allowed_actions=self.allowed_actions,
                position_alignment=self.position_alignment,
            )
        alignment = self.position_alignment
        if alignment is not None and (
            alignment.account_ref != self.account_ref
            or alignment.venue_ref != self.venue_ref
            or alignment.instrument_ref != self.instrument_ref
            or alignment.direction is not self.direction
        ):
            raise ValueError("POSITION_ALIGNMENT_PLAN_BOUNDARY_MISMATCH")
        return self


class TradePlanDraft(PlanningModel):
    plan_id: str
    environment_id: str
    draft_version: int
    content: TradePlanContent
    content_digest: str
    updated_at: datetime

    @model_validator(mode="after")
    def environment_matches_content(self) -> TradePlanDraft:
        if self.environment_id != self.content.environment_id:
            raise ValueError("PLAN_ENVIRONMENT_MISMATCH")
        if self.draft_version <= 0:
            raise ValueError("PLAN_VERSION_CONFLICT")
        return self


class TradePlanVersion(PlanningModel):
    plan_version_id: str
    plan_id: str
    environment_id: str
    fixed_at: datetime
    plan_name: str | None = None
    created_at: datetime | None = None
    creator_kind: PlanCreatorKind | None = None
    ai_review_ref: str | None = None
    decision_context: PlanDecisionContext | None = None
    decision_basis: FixedDecisionBasis
    order_schedule_spec: OrderScheduleSpec | None = None
    position_alignment: PositionAlignmentSpec | None = None
    account_ref: str
    venue_ref: str
    instrument_ref: str
    direction: Direction
    target_exposure: str
    requested_limits: RequestedLimits
    valid_from: datetime
    valid_until: datetime
    allowed_actions: frozenset[str]
    terms: dict[str, Any]
    content_digest: str

    @model_validator(mode="after")
    def schedule_has_a_current_runtime_consumer(
        self,
        info: ValidationInfo,
    ) -> TradePlanVersion:
        if not _is_persisted_history(info):
            validate_current_plan_admission(
                decision_basis_kind=self.decision_basis.kind,
                order_schedule_spec=self.order_schedule_spec,
                allowed_actions=self.allowed_actions,
                position_alignment=self.position_alignment,
            )
        alignment = self.position_alignment
        if alignment is not None and (
            alignment.account_ref != self.account_ref
            or alignment.venue_ref != self.venue_ref
            or alignment.instrument_ref != self.instrument_ref
            or alignment.direction is not self.direction
        ):
            raise ValueError("POSITION_ALIGNMENT_PLAN_BOUNDARY_MISMATCH")
        return self

    @property
    def strategy_basis(self) -> FixedStrategyPlanBasis:
        if not isinstance(self.decision_basis, FixedStrategyPlanBasis):
            raise ValueError("STRATEGY_BASIS_NOT_APPLICABLE")
        return self.decision_basis


class ConditionJudgement(PlanningModel):
    rule_id: str
    source_identity: str
    source_cutoff: datetime
    input_digest: str
    fact_refs: tuple[str, ...] = ()
    result: ConditionResult
    reason_code: str
    next_responsibility: str


class ProposedAction(PlanningModel):
    environment_id: str
    action_kind: ProposedActionKind = ProposedActionKind.ENTRY
    action_profile: str
    instrument_ref: str
    direction: Direction
    quantity: str | None = None
    close_position: bool = False
    order_type: str
    price: str | None = None
    trigger_price: str | None = None
    valid_until: datetime | None = None
    reduce_only: bool
    source_responsibility: str
    causation_ref: str
    cancel_target: dict[str, Any] | None = None
    execution_context: dict[str, Any] = Field(default_factory=dict)

    @field_validator("quantity", "price", "trigger_price")
    @classmethod
    def optional_positive_decimal(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return canonical_decimal(
            decimal_from_string(value, code="PROPOSED_ACTION_INVALID", positive=True)
        )

    @model_validator(mode="after")
    def action_shape_is_unambiguous(self) -> ProposedAction:
        if self.action_kind is ProposedActionKind.CANCEL:
            if self.quantity is not None or self.close_position or self.cancel_target is None:
                raise ValueError("PROPOSED_ACTION_CANCEL_TARGET_INVALID")
        elif (self.quantity is None) == (not self.close_position):
            raise ValueError("PROPOSED_ACTION_QUANTITY_AMBIGUOUS")
        if self.action_kind is not ProposedActionKind.CANCEL and self.cancel_target is not None:
            raise ValueError("PROPOSED_ACTION_CANCEL_TARGET_INVALID")
        return self


class PlanActivation(PlanningModel):
    activation_id: str
    environment_id: str
    environment_kind: EnvironmentKind
    authority_class: AuthorityClass
    plan_version_ref: str
    account_ref: str
    instrument_ref: str
    direction: Direction
    decision_basis_ref: str
    framework_strategy_id: str
    order_schedule_snapshot: OrderSchedulePreview | None = None
    position_alignment: PositionAlignmentSpec | None = None
    target_exposure: str
    lifecycle: PlanLifecycle = PlanLifecycle.RUNNING
    run_state: RunState = RunState.ACTIVE
    pause_reason: str | None = None
    paused_at: datetime | None = None
    reconciliation_digest: str | None = None
    current_resume_command_ref: str | None = None
    has_entry_fill: bool = False
    entry_opportunity_consumed: bool = False
    responsibility_owner: str = "HALPHA"
    state_version: int = 1
    rule_state: dict[str, Any]
    pending_action_digest: str | None = None
    protection_state: ProtectionState = ProtectionState.NONE
    takeover_scope: dict[str, Any] | None = None
    latest_venue_cutoff: datetime | None = None
    closure_digest: str | None = None
    result_ref: str | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def state_is_consistent(self) -> PlanActivation:
        expected = (
            AuthorityClass.DEMO_VALIDATION
            if self.environment_kind is EnvironmentKind.DEMO
            else AuthorityClass.LIVE_REAL_CAPITAL
        )
        if self.authority_class is not expected:
            raise ValueError("AUTHORITY_ENVIRONMENT_MISMATCH")
        if self.run_state is RunState.PAUSED:
            if self.pause_reason != "WRITER_CONTINUITY_LOST" or self.paused_at is None:
                raise ValueError("PAUSE_STATE_INVALID")
        elif self.pause_reason is not None or self.paused_at is not None:
            raise ValueError("PAUSE_STATE_INVALID")
        if self.lifecycle is PlanLifecycle.USER_TAKEOVER and not self.takeover_scope:
            raise ValueError("TAKEOVER_SCOPE_REQUIRED")
        if self.lifecycle is PlanLifecycle.COMPLETED and not self.closure_digest:
            raise ValueError("CLOSURE_UNPROVEN")
        is_direct = self.decision_basis_ref == DIRECT_EXECUTION_REF
        if self.order_schedule_snapshot is not None and not is_direct:
            raise ValueError("STRATEGY_ORDER_SCHEDULE_NOT_SUPPORTED")
        if self.position_alignment is not None and (
            not is_direct or self.order_schedule_snapshot is not None
        ):
            raise ValueError("POSITION_ALIGNMENT_ACTIVATION_INVALID")
        if (
            is_direct
            and self.order_schedule_snapshot is None
            and self.position_alignment is None
        ):
            raise ValueError("ORDER_SCHEDULE_SNAPSHOT_REQUIRED")
        if self.order_schedule_snapshot is not None:
            # The activation snapshot is immutable execution history.  Current
            # catalog admission belongs to TradePlanContent/TradePlanVersion;
            # reapplying it here would make a valid historical activation
            # unreadable whenever a later release narrows supported inputs.
            validate_order_schedule_snapshot(self.order_schedule_snapshot)
            if (
                self.order_schedule_snapshot.schedule_ref != self.plan_version_ref
                or self.order_schedule_snapshot.instrument_ref != self.instrument_ref
                or self.order_schedule_snapshot.direction is not self.direction
            ):
                raise ValueError("ORDER_SCHEDULE_SNAPSHOT_MISMATCH")
        alignment = self.position_alignment
        if alignment is not None:
            if (
                alignment.account_ref != self.account_ref
                or alignment.instrument_ref != self.instrument_ref
                or alignment.direction is not self.direction
                or self.lifecycle
                not in {
                    PlanLifecycle.EXITING,
                    PlanLifecycle.USER_TAKEOVER,
                    PlanLifecycle.COMPLETED,
                }
                or not self.entry_opportunity_consumed
            ):
                raise ValueError("POSITION_ALIGNMENT_ACTIVATION_INVALID")
        return self

    @property
    def strategy_id(self) -> str:
        if self.decision_basis_ref == DIRECT_EXECUTION_REF:
            raise ValueError("STRATEGY_BASIS_NOT_APPLICABLE")
        return self.decision_basis_ref.split("@", maxsplit=1)[0]


class PlanEvent(PlanningModel):
    plan_event_id: str
    environment_id: str
    activation_id: str
    rule_id: str
    source_identity: str
    source_cutoff: datetime
    input_digest: str
    reason_code: str
    condition_judgement: ConditionJudgement | None
    proposed_action: ProposedAction | None
    no_action_reason: str | None
    capital_decision: dict[str, Any]
    capital_decision_digest: str
    created_at: datetime
    content_digest: str

    @model_validator(mode="after")
    def has_exactly_one_result(self) -> PlanEvent:
        if (self.proposed_action is None) == (self.no_action_reason is None):
            raise ValueError("PLAN_EVENT_RESULT_AMBIGUOUS")
        return self
