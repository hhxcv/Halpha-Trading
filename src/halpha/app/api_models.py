"""Typed HTTP contracts owned by the local workbench API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from halpha.capital.models import StopStateVersion
from halpha.app.plan_ai_review import (
    PlanAiReviewConfiguration,
    PlanAiReviewDecision,
    PlanAiReviewStatus,
)
from halpha.outcomes.models import Review
from halpha.outcomes.price_path import ReviewPricePathInterval
from halpha.outcomes.sequence_evidence import (
    ReviewSequenceResultKind,
    ReviewSequenceScope,
)
from halpha.planning.models import PlanActivation, PlanLifecycle
from halpha.planning.models import (
    PlanDecisionContext,
    PlanDecisionIntent,
    PlanSetupFamily,
    PositionAlignmentSpec,
)
from halpha.planning.live_profit_qualification import (
    LiveProfitQualificationStatus,
    PlaybookQualificationArtifact,
)
from halpha.planning.order_schedule import (
    OrderSchedulePreview,
    OrderScheduleSpec,
)
from halpha.planning.registry import (
    DecisionBasisKind,
    Direction,
    DraftDecisionBasis,
    PlanKeyParameterDefinition,
)
from halpha.planning.transitions import ControlIntent


class FrozenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AccountPositionResponse(FrozenResponse):
    instrument_ref: str
    symbol: str
    direction: Literal["LONG", "SHORT"]
    position_side: Literal["BOTH", "LONG", "SHORT"]
    quantity: str
    absolute_quantity: str
    entry_price: str
    break_even_price: str | None
    mark_price: str
    unrealized_pnl: str
    liquidation_price: str | None
    leverage: int
    margin_mode: Literal["CROSS", "ISOLATED"]
    notional: str
    isolated_margin: str | None
    fact_cutoff: str
    snapshot_ref: str
    origin: Literal[
        "EXTERNAL_UNMANAGED",
        "ACCOUNT_TOTAL_WITH_HALPHA_ATTRIBUTION",
    ]
    management_status: Literal["OBSERVED_ONLY"]
    takeover_allowed: bool
    takeover_blockers: list[
        Literal[
            "READ_ONLY_CREDENTIAL",
            "ACCOUNT_POSITION_SNAPSHOT_NOT_CURRENT",
            "ATTRIBUTION_REQUIRES_RECONCILIATION",
            "OPEN_ORDERS_REQUIRE_RECONCILIATION",
        ]
    ]


class AccountOrderResponse(FrozenResponse):
    kind: Literal["ORDINARY", "ALGO"]
    instrument_ref: str
    symbol: str
    order_id: str
    client_order_id: str | None
    side: Literal["BUY", "SELL"]
    position_side: Literal["BOTH", "LONG", "SHORT"]
    order_type: str
    status: str
    time_in_force: str | None
    price: str | None
    trigger_price: str | None
    quantity: str | None
    executed_quantity: str | None
    reduce_only: bool | None
    close_position: bool | None
    source_create_time_ms: int | None
    source_update_time_ms: int | None
    fact_cutoff: str
    snapshot_ref: str


class AccountSummaryResponse(FrozenResponse):
    can_trade: bool
    wallet_balance: str
    unrealized_pnl: str
    margin_balance: str
    available_balance: str
    initial_margin: str
    maintenance_margin: str
    position_initial_margin: str
    open_order_initial_margin: str
    cross_wallet_balance: str
    cross_unrealized_pnl: str
    source_update_time_ms: int | None
    fact_cutoff: str
    snapshot_ref: str


class NewRiskDisciplineResponse(FrozenResponse):
    status: Literal["ALLOWED", "BLOCKED", "UNKNOWN"]
    new_risk_allowed: bool
    blocker_codes: list[str]
    account_snapshot_ref: str | None
    account_snapshot_cutoff: str | None
    risk_equity: str | None
    max_plan_loss: str | None
    minimum_reward_risk_ratio: str
    available_notional_capacity: str | None
    open_risk_limit: str | None
    open_risk_committed: str | None
    open_risk_after_proposal: str | None
    gross_exposure_limit: str | None
    gross_exposure: str | None
    gross_exposure_after_proposal: str | None
    instrument_ref: str | None
    instrument_exposure_limit: str | None
    instrument_exposure: str | None
    instrument_exposure_after_proposal: str | None
    correlation_cluster: str | None
    correlated_exposure_limit: str | None
    correlated_exposure: str | None
    correlated_exposure_after_proposal: str | None
    daily_loss_limit: str | None
    daily_loss_measure: str | None
    weekly_loss_limit: str | None
    weekly_loss_measure: str | None
    rolling_peak_equity: str | None
    rolling_drawdown: str | None
    rolling_drawdown_fraction: str | None
    rolling_drawdown_limit_fraction: str
    rolling_drawdown_lookback_days: int
    open_new_risk_activation_count: int
    day_window_started_at: str
    week_window_started_at: str
    evaluated_at: str


class OverviewResponse(FrozenResponse):
    environment_kind: str
    environment_id: str
    account_id: str
    profile: str
    authority_class: str
    runtime_real_write_gate: str
    server_fact_cutoff: str
    view_retrieved_at: str
    open_activation_count: int
    database_name: str
    account_snapshot_status: Literal[
        "CURRENT",
        "STALE",
        "UNAVAILABLE",
        "UNKNOWN",
    ]
    account_snapshot_ref: str | None
    account_snapshot_cutoff: str | None
    account_snapshot_age_seconds: int | None
    account_observation_failure_at: str | None = None
    account_observation_failure_code: str | None = None
    account_observation_retry_after_seconds: float | None = None
    account_ordinary_open_order_count: int | None
    account_algo_open_order_count: int | None
    account_summary: AccountSummaryResponse | None
    new_risk_discipline: NewRiskDisciplineResponse
    account_positions: list[AccountPositionResponse]
    account_orders: list[AccountOrderResponse]


class AccountPositionOperationPreviewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["REDUCE", "CLOSE", "ADD"]
    snapshot_ref: str = Field(min_length=1, max_length=160)
    fact_cutoff: str
    instrument_ref: str = Field(min_length=1, max_length=96)
    position_side: Literal["BOTH", "LONG", "SHORT"]
    expected_absolute_quantity: str
    requested_quantity: str | None = None
    requested_notional: str | None = None


class AccountPositionOperationPlanPrefill(FrozenResponse):
    kind: Literal["POSITION_DISPOSITION", "NEW_EXPOSURE"]
    plan_name: str
    instrument_ref: str
    direction: Literal["LONG", "SHORT"]
    trade_amount: str
    valid_minutes: int
    baseline_quantity: str
    target_quantity_after: str | None
    position_alignment: PositionAlignmentSpec | None


class AccountPositionOperationPreviewResponse(FrozenResponse):
    operation: Literal["REDUCE", "CLOSE", "ADD"]
    snapshot_ref: str
    fact_cutoff: str
    instrument_ref: str
    position_side: Literal["BOTH", "LONG", "SHORT"]
    direction: Literal["LONG", "SHORT"]
    preparation_allowed: bool
    activation_allowed: bool
    venue_action_created: Literal[False]
    blockers: list[
        Literal[
            "READ_ONLY_CREDENTIAL",
            "OPEN_ORDERS_REQUIRE_RECONCILIATION",
            "ATTRIBUTION_REQUIRES_RECONCILIATION",
            "HEDGE_MODE_POSITION_OPERATIONS_UNSUPPORTED",
            "EXTERNAL_POSITION_REQUIRES_ALIGNMENT",
        ]
    ]
    plan_prefill: AccountPositionOperationPlanPrefill


class OrderSchedulePreviewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schedule_ref: str
    decision_basis_kind: DecisionBasisKind = DecisionBasisKind.DIRECT_EXECUTION
    venue_ref: Literal["BINANCE_USDM"] = "BINANCE_USDM"
    instrument_ref: str
    direction: Direction
    max_notional: str
    reference_price: str | None = None
    spec: OrderScheduleSpec


class TradingContextTargetResponse(FrozenResponse):
    venue_account_type: Literal[
        "USDM_DEMO",
        "USDM_COPY_LEAD",
        "USDM_PERSONAL",
    ]
    environment_id: str
    account_id: str
    url: str


class SettingsStatusResponse(FrozenResponse):
    environment_kind: str
    environment_id: str
    account_id: str
    venue_account_type: Literal[
        "USDM_DEMO",
        "USDM_COPY_LEAD",
        "USDM_PERSONAL",
    ]
    profile: str
    authority_class: str
    bind: str
    port: int
    trading_contexts: list[TradingContextTargetResponse]
    database_name: str
    database_available: bool
    database_reason_code: str | None
    server_fact_cutoff: str | None
    product_build_id: str
    app_executor_product_build_consistent: bool | None
    executor_status: str
    executor_status_checked_at: str
    configured_runtime_real_write_gate: str
    runtime_real_write_gate: str
    live_write_gate_violations: list[str]
    authorized_activation_ids: list[str]
    email_delivery_enabled: bool
    email_configuration_status: str
    view_retrieved_at: str


class StrategySummaryResponse(FrozenResponse):
    strategy_id: str
    strategy_version: str
    display_name: str
    value_logic: str
    applicable_scenarios: str
    execution_behavior: str
    parameter_schema_version: str
    supported_directions: list[Direction]
    economic_scope: dict[str, Any]
    plan_key_parameters: list[PlanKeyParameterDefinition]


class ActivationPreviewResponse(FrozenResponse):
    plan_version_id: str
    plan_name: str | None
    created_at: str | None
    creator_kind: Literal["HUMAN", "AI", "MONITOR"] | None
    decision_context: PlanDecisionContext | None = None
    environment_id: str
    environment_kind: str
    authority_class: str
    account_ref: str
    venue_ref: str
    instrument_ref: str
    direction: Direction
    decision_basis: dict[str, Any]
    decision_basis_kind: DecisionBasisKind
    decision_basis_ref: str
    strategy_ref: str | None
    parameter_digest: str
    strategy_parameters: dict[str, Any]
    order_schedule_spec: OrderScheduleSpec | None
    position_alignment: PositionAlignmentSpec | None
    trade_amount: str
    limits: dict[str, Any]
    valid_until: str
    allowed_actions: list[str]
    actual_account_configuration: str
    account_mode_policy: str
    product_build_id: str
    product_build_consistent: bool
    runtime_compatible: bool
    runtime_incompatibility_reason: str | None
    position_alignment_ready: bool | None
    position_alignment_blocker: str | None
    ai_review: dict[str, Any] | None = None
    ai_review_ready: bool | None = None
    ai_review_blocker: str | None = None
    new_risk_discipline: NewRiskDisciplineResponse | None = None
    live_profit_qualification: LiveProfitQualificationStatus
    configured_runtime_real_write_gate: str
    runtime_real_write_gate: str
    live_activation_eligible: bool
    capital_notice: str
    order_schedule_snapshot: OrderSchedulePreview | None
    expected_schedule_digest: str | None
    executor_status: str
    executor_status_checked_at: str


class PlanSummaryResponse(FrozenResponse):
    plan_id: str
    draft_version: int
    draft_content_digest: str
    updated_at: str
    plan_name: str | None
    created_at: str | None
    creator_kind: Literal["HUMAN", "AI", "MONITOR"] | None
    decision_context: PlanDecisionContext | None = None
    decision_basis: DraftDecisionBasis
    decision_basis_kind: DecisionBasisKind
    decision_basis_ref: str
    strategy_id: str | None
    instrument_ref: str
    direction: Direction
    parameters: dict[str, Any]
    order_schedule_spec: OrderScheduleSpec | None
    position_alignment: PositionAlignmentSpec | None
    max_notional: str
    valid_from: str
    valid_until: str
    # Primary lifecycle shown to users.  Runtime control substates stay on the
    # activation detail and never introduce a fourth plan status.
    status: Literal["DRAFT", "RUNNING", "ENDED"]
    activation_id: str | None = None
    activation_lifecycle: PlanLifecycle | None = None
    activation_created_at: str | None = None
    activation_updated_at: str | None = None
    plan_version_id: str | None
    fixed_at: str | None
    fixed_content_digest: str | None
    fixed_product_build_id: str | None
    fixed_valid_until: str | None
    product_build_consistent: bool | None
    runtime_compatible: bool | None
    runtime_incompatibility_reason: str | None
    ai_review_ref: str | None = None
    ai_review: dict[str, Any] | None = None


class PlanDeleteResponse(FrozenResponse):
    result: Literal["APPLIED"]
    plan_id: str
    deleted_draft_version: int


class PlanAiReviewResponse(FrozenResponse):
    review_id: str
    environment_id: str
    plan_id: str
    draft_version: int
    draft_content_digest: str
    prompt_version: str
    configuration: PlanAiReviewConfiguration
    status: PlanAiReviewStatus
    market_context_digest: str | None = None
    market_source_cutoff: str | None = None
    approval_valid_until: str | None = None
    decision: PlanAiReviewDecision | None = None
    reason: str | None = None
    suggestions: list[str] = Field(default_factory=list)
    progress_message: str
    public_output: str = ""
    failure_code: str | None = None
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    updated_at: str


class PlanAiReviewRequest(FrozenResponse):
    configuration: PlanAiReviewConfiguration = Field(
        default_factory=PlanAiReviewConfiguration
    )


class ActivationCreateResponse(FrozenResponse):
    activation: PlanActivation
    venue_write_created: bool
    runtime_real_write_gate: str


class ActivationSummaryResponse(PlanActivation):
    plan_name: str | None
    plan_created_at: str | None
    plan_creator_kind: str | None
    closure_reason_code: str | None
    primary_result: str | None
    trade_result: dict[str, Any] | None


class ActivationDetailResponse(FrozenResponse):
    activation: PlanActivation
    ai_review_ref: str | None = None
    ai_review: dict[str, Any] | None = None
    plan: dict[str, Any]
    decision_basis: dict[str, Any]
    strategy: dict[str, Any] | None
    order_schedule: OrderSchedulePreview | None
    capital: dict[str, Any]
    position_attribution: dict[str, Any]
    trade_result: dict[str, Any]
    execution_actions: list[dict[str, Any]]
    venue_facts: list[dict[str, Any]]
    receipts: list[dict[str, Any]]
    stopped_categories: list[str]
    stop_evidence: list[dict[str, Any]]
    runtime_real_write_gate: str


class ActivationTimelineEntryResponse(FrozenResponse):
    source: Literal[
        "ACTIVATION",
        "PLAN_EVENT",
        "EXECUTION_ACTION",
        "VENUE_FACT",
        "CONTROL_COMMAND",
    ]
    source_ref: str
    stage_order: int
    at: str
    status: str
    detail: dict[str, Any]


class ControlPreviewResponse(ActivationDetailResponse):
    intent: ControlIntent
    consequence: str
    preview_digest: str
    previewed_at: str
    resume_eligible: bool | None
    resume_denial_reasons: list[str]
    reconciliation_digest: str | None
    reconciliation_evidence_cutoff: str | None
    venue_write_created_by_preview: Literal[False]


class SystemStopSummaryResponse(FrozenResponse):
    stop_state_version_id: str
    version: int
    source: str
    started_at: str


class SystemStopReleasePreviewResponse(FrozenResponse):
    eligible: bool
    denial_reasons: list[str]
    consequence: str
    stop: SystemStopSummaryResponse | None
    evidence_cutoff: str | None


class SystemStopReleaseResponse(FrozenResponse):
    effective: Literal[True]
    replayed: bool
    stop_state: StopStateVersion


class ReceiptResponse(FrozenResponse):
    receipt_id: str
    command_id: str
    processing_owner: str
    state: str
    state_version: int
    reason_code: str | None
    result: dict[str, Any] | None
    pending_responsibility_refs: list[str]
    content_digest: str
    created_at: str
    updated_at: str


class ReviewResponse(Review):
    trade_context: dict[str, Any]
    resolved_trade_result: dict[str, Any]


class ExecutionFeeSampleResponse(FrozenResponse):
    conservative_rate_bps: str
    sample_count: int
    latest_fill_time: str


class ExecutionFeeEvidenceResponse(FrozenResponse):
    instrument_ref: str
    source: Literal["RECENT_ATTRIBUTED_COMPLETED_FILLS"]
    calculation: Literal["MAX_RATE_OF_LATEST_FILLS"]
    sample_limit: int
    maker: ExecutionFeeSampleResponse | None
    taker: ExecutionFeeSampleResponse | None
    source_cutoff: str | None


class DecisionEvidenceMetricsResponse(FrozenResponse):
    trade_count: int
    wins: int
    losses: int
    flat: int
    net_pnl: str
    commission: str
    average_net_pnl: str | None
    gross_profit: str
    gross_loss: str
    profit_factor: str | None
    total_entry_notional: str
    notional_return_percent: str | None
    maximum_drawdown: str
    worst_trade_net_pnl: str | None
    net_pnl_without_worst_trade: str | None
    largest_loss_share_percent: str | None
    best_trade_net_pnl: str | None
    net_pnl_without_best_trade: str | None
    largest_win_share_percent: str | None
    longest_winning_streak: int
    longest_losing_streak: int
    current_streak_kind: Literal["WIN", "LOSS", "FLAT"] | None
    current_streak_count: int


PlaybookRepeatabilityReason = Literal[
    "MINIMUM_SAMPLE_NOT_MET",
    "RISK_BASIS_INCOMPLETE",
    "NET_EXPECTANCY_NOT_POSITIVE",
    "PROFIT_FACTOR_MARGIN_NOT_MET",
    "BEST_TRADE_DEPENDENCE",
    "EARLY_SEGMENT_NOT_POSITIVE",
    "RECENT_SEGMENT_NOT_POSITIVE",
    "MEAN_R_CONFIDENCE_NOT_POSITIVE",
]


class PlaybookRepeatabilityResponse(FrozenResponse):
    status: Literal[
        "NOT_APPLICABLE_VALIDATION",
        "NOT_READY",
        "EVIDENCE_CANDIDATE",
    ]
    reason_codes: list[PlaybookRepeatabilityReason]
    policy_version: str
    minimum_trade_count: int
    minimum_profit_factor: str
    confidence_level_percent: str
    bootstrap_resamples: int
    bootstrap_block_length: int | None
    risk_basis_trade_count: int
    net_r_multiple: str | None
    average_r_multiple: str | None
    net_r_without_best_trade: str | None
    early_segment_trade_count: int
    early_segment_net_r: str | None
    recent_segment_trade_count: int
    recent_segment_net_r: str | None
    mean_r_lower_confidence_bound: str | None
    live_promotion_authority: Literal[False]
    capital_scaling_authority: Literal[False]
    limitations: list[str]


class DecisionEvidenceSampleRefResponse(FrozenResponse):
    review_id: str
    review_version: int
    review_content_digest: str
    fact_cutoff: str


class DecisionEvidenceResponse(FrozenResponse):
    instrument_ref: str
    direction: Direction
    decision_basis_ref: str
    parameter_digest: str
    intent: PlanDecisionIntent
    setup_family: PlanSetupFamily
    playbook_ref: str | None
    source: Literal["CURRENT_COMPLETED_REVIEWS"]
    source_cutoff: str | None
    matched_review_count: int
    comparable_trade_count: int
    excluded_review_count: int
    exclusions: dict[str, int]
    sample_review_refs: list[DecisionEvidenceSampleRefResponse]
    sample_traceability_complete: bool
    sample_identity_digest: str | None
    evidence_grade: Literal[
        "VALIDATION_INTENT",
        "NO_COMPARABLE_SAMPLE",
        "SINGLE_DIGIT_ANECDOTAL",
        "REPEATED_OBSERVATION_UNPROVEN",
    ]
    repeated_sample_floor: int
    metrics: DecisionEvidenceMetricsResponse
    repeatability: PlaybookRepeatabilityResponse
    capital_scaling_authority: Literal[False]
    limitations: list[str]


class PlaybookQualificationExportResponse(FrozenResponse):
    artifact: PlaybookQualificationArtifact
    artifact_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    save_outside_repository: Literal[True]


ReviewPricePathReason = Literal[
    "EXTERNAL_POSITION_DISPOSITION",
    "NO_ATTRIBUTED_ENTRY",
    "TRADE_NOT_CLOSED",
    "STRATEGY_ATTRIBUTION_INCOMPLETE",
    "FILL_TIMES_INCOMPLETE",
    "DIRECTION_UNKNOWN",
    "AVERAGE_ENTRY_PRICE_UNKNOWN",
    "FIRST_REDUCTION_UNKNOWN",
    "FILL_SEQUENCE_INVALID",
    "NO_COMPLETE_HOLDING_BAR",
    "INTERVAL_TOO_FINE",
    "INITIAL_RISK_UNKNOWN",
    "MARKET_WINDOW_COVERAGE_MISMATCH",
    "MARKET_WINDOW_NOT_CLOSED",
]


class ReviewPricePathEvidenceResponse(FrozenResponse):
    review_id: str
    review_version: int
    evidence_status: Literal["AVAILABLE", "NOT_APPLICABLE", "UNKNOWN"]
    reason_codes: list[ReviewPricePathReason]
    instrument_ref: str | None
    direction: Direction | None
    interval: ReviewPricePathInterval
    calculation_version: Literal["REVIEW_PRICE_PATH_V1"]
    market_source: str | None
    market_source_cutoff: str | None
    last_entry_fill_at: str | None
    first_reduction_fill_at: str | None
    analysis_start_at: str | None
    analysis_end_at: str | None
    complete_bar_count: int
    average_entry_price: str | None
    initial_stop_distance_bps: str | None
    planned_stop_price: str | None
    best_favorable_price: str | None
    best_favorable_bar_open_at: str | None
    worst_adverse_price: str | None
    worst_adverse_bar_open_at: str | None
    best_directional_move_percent: str | None
    worst_directional_move_percent: str | None
    maximum_favorable_excursion_percent: str | None
    maximum_adverse_excursion_percent: str | None
    maximum_favorable_excursion_r: str | None
    maximum_adverse_excursion_r: str | None
    ever_favorable_on_complete_bars: bool | None
    first_complete_bar_close_price: str | None
    first_complete_bar_close_at: str | None
    first_complete_bar_directional_return_percent: str | None
    first_complete_bar_favorable: bool | None
    one_r_touched: bool | None
    two_r_touched: bool | None
    planned_stop_touched: bool | None
    threshold_sequence: Literal[
        "ONE_R_BEFORE_STOP",
        "STOP_BEFORE_ONE_R",
        "SAME_BAR_AMBIGUOUS",
        "ONE_R_ONLY",
        "STOP_ONLY",
        "NEITHER",
        "UNKNOWN",
    ]
    capital_scaling_authority: Literal[False]
    limitations: list[str]


class ReviewSequenceReviewRefResponse(FrozenResponse):
    review_id: str
    review_version: int


class ReviewSequenceTradePricePathResponse(FrozenResponse):
    evidence_status: Literal["AVAILABLE", "NOT_APPLICABLE", "UNKNOWN"]
    reason_codes: list[str]
    ever_favorable_on_complete_bars: bool | None
    maximum_favorable_excursion_percent: str | None
    maximum_adverse_excursion_percent: str | None
    maximum_favorable_excursion_r: str | None
    maximum_adverse_excursion_r: str | None
    first_complete_bar_favorable: bool | None
    threshold_sequence: Literal[
        "ONE_R_BEFORE_STOP",
        "STOP_BEFORE_ONE_R",
        "SAME_BAR_AMBIGUOUS",
        "ONE_R_ONLY",
        "STOP_ONLY",
        "NEITHER",
        "UNKNOWN",
    ]


class ReviewSequencePricePathStatisticsResponse(FrozenResponse):
    requested: Literal[True]
    interval: ReviewPricePathInterval
    trade_count: int
    available_count: int
    unknown_count: int
    not_applicable_count: int
    ever_favorable_count: int
    never_favorable_count: int
    first_complete_bar_favorable_count: int
    median_mfe_percent: str | None
    median_mae_percent: str | None
    median_mfe_r: str | None
    median_mae_r: str | None
    reason_counts: dict[str, int]
    threshold_counts: dict[str, int]
    market_source_cutoff: str | None


class ReviewSequencePricePathSummaryResponse(
    ReviewSequencePricePathStatisticsResponse
):
    by_result: dict[str, ReviewSequencePricePathStatisticsResponse]


class ReviewSequenceSegmentResponse(FrozenResponse):
    segment_index: int
    result_kind: ReviewSequenceResultKind
    start_at: str
    end_at: str
    trade_count: int
    net_pnl: str
    commission: str
    total_entry_notional: str | None
    notional_return_percent: str | None
    review_refs: list[ReviewSequenceReviewRefResponse]
    price_path: ReviewSequencePricePathStatisticsResponse | None


class ReviewSequenceTradeResponse(FrozenResponse):
    review_id: str
    review_version: int
    closed_at: str
    instrument_ref: str | None
    direction: Direction | None
    classification: str | None
    intent: str | None
    result_kind: ReviewSequenceResultKind
    net_pnl: str
    commission: str
    entry_notional: str | None
    notional_return_percent: str | None
    cumulative_net_pnl: str
    drawdown_from_peak: str
    segment_index: int
    price_path: ReviewSequenceTradePricePathResponse | None


class ReviewSequenceMetricsResponse(FrozenResponse):
    trade_count: int
    wins: int
    losses: int
    flat: int
    net_pnl: str
    commission: str
    gross_profit: str
    gross_loss: str
    profit_factor: str | None
    total_entry_notional: str | None
    notional_return_percent: str | None
    maximum_drawdown: str
    maximum_drawdown_peak_review_id: str | None
    maximum_drawdown_peak_at: str | None
    maximum_drawdown_trough_review_id: str | None
    maximum_drawdown_trough_at: str | None
    current_segment_index: int | None
    longest_win_segment_index: int | None
    longest_loss_segment_index: int | None
    best_win_segment_index: int | None
    worst_loss_segment_index: int | None
    worst_trade_review_id: str | None
    worst_trade_net_pnl: str | None
    net_pnl_without_worst_trade: str | None
    largest_loss_share_percent: str | None
    top_loss_count: int
    top_loss_total: str | None
    net_pnl_without_top_losses: str | None
    top_loss_share_percent: str | None


class ReviewSequenceEvidenceResponse(FrozenResponse):
    scope: ReviewSequenceScope
    range_start: str | None
    range_end: str | None
    source: Literal["CURRENT_LATEST_REVIEWS"]
    source_cutoff: str | None
    source_review_count: int
    eligible_trade_count: int
    excluded_review_count: int
    exclusions: dict[str, int]
    metrics: ReviewSequenceMetricsResponse
    segments: list[ReviewSequenceSegmentResponse]
    trades: list[ReviewSequenceTradeResponse]
    price_path: ReviewSequencePricePathSummaryResponse | None
    capital_scaling_authority: Literal[False]
    limitations: list[str]


class ReviewHistoryResponse(FrozenResponse):
    review: ReviewResponse
    versions: list[ReviewResponse]


class ReviewCompletionResponse(FrozenResponse):
    review: Review


class TestEmailResponse(FrozenResponse):
    status: Literal["DELIVERED"]
    environment_id: str
    recipient_route_ref: str
    delivered_at: str
    business_state_changed: Literal[False]
