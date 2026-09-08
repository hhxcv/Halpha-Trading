"""Bounded values for one manually directed scalping cycle."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from halpha.domain_values import canonical_decimal, content_digest, decimal_from_string
from halpha.planning.models import TradePlanContent, TradePlanVersion
from halpha.planning.order_policies import (
    ConditionGroup,
    DecisionBasisReadyCondition,
    FullFillLossBudgetSpec,
    InitialStopSpec,
    ProtectionPolicy,
    SpreadBpsCondition,
    TakeProfitLadderSpec,
    TakeProfitLevel,
)
from halpha.planning.order_schedule import (
    AmountDistribution,
    AmountDistributionMode,
    EntryProgram,
    EntryProgramKind,
    OrderScheduleSpec,
    ScheduleSubmissionMode,
    ScheduleSubmissionOrder,
    SinglePrice,
    VenueOrderPolicy,
    VenueOrderType,
)
from halpha.planning.registry import DecisionBasisKind, Direction


SCALP_WORKFLOW_KIND = "SCALP_CYCLE"
SCALP_TEMPLATE_SCHEMA = "HALPHA_SCALP_TEMPLATE_V1"


class ScalpModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_serialization_defaults_required=True,
    )


class ScalpTemplate(ScalpModel):
    schema_version: Literal["HALPHA_SCALP_TEMPLATE_V1"] = SCALP_TEMPLATE_SCHEMA
    notional: str = "100"
    entry_mode: Literal["MARKET", "MAKER_ONLY_SAME_SIDE"] = "MARKET"
    initial_stop_bps: str = "35"
    take_profit_r: str = "1.5"
    max_holding_seconds: int = Field(default=300, ge=30, le=86_400)
    max_spread_bps: str = "5"
    entry_fee_bps: str = "6"
    exit_fee_bps: str = "6"

    @field_validator("notional")
    @classmethod
    def bounded_notional(cls, value: str) -> str:
        parsed = decimal_from_string(
            value,
            code="SCALP_TEMPLATE_NOTIONAL_INVALID",
            positive=True,
        )
        if parsed > Decimal("100000000"):
            raise ValueError("SCALP_TEMPLATE_NOTIONAL_INVALID")
        return canonical_decimal(parsed)

    @field_validator("initial_stop_bps")
    @classmethod
    def bounded_stop(cls, value: str) -> str:
        parsed = decimal_from_string(
            value,
            code="SCALP_TEMPLATE_STOP_INVALID",
            positive=True,
        )
        if not Decimal("1") <= parsed <= Decimal("5000"):
            raise ValueError("SCALP_TEMPLATE_STOP_INVALID")
        return canonical_decimal(parsed)

    @field_validator("take_profit_r")
    @classmethod
    def bounded_take_profit(cls, value: str) -> str:
        parsed = decimal_from_string(
            value,
            code="SCALP_TEMPLATE_TAKE_PROFIT_INVALID",
            positive=True,
        )
        if not Decimal("0.1") <= parsed <= Decimal("20"):
            raise ValueError("SCALP_TEMPLATE_TAKE_PROFIT_INVALID")
        return canonical_decimal(parsed)

    @field_validator("max_spread_bps")
    @classmethod
    def bounded_spread(cls, value: str) -> str:
        parsed = decimal_from_string(
            value,
            code="SCALP_TEMPLATE_SPREAD_INVALID",
            non_negative=True,
        )
        if parsed > Decimal("1000"):
            raise ValueError("SCALP_TEMPLATE_SPREAD_INVALID")
        return canonical_decimal(parsed)

    @field_validator("entry_fee_bps", "exit_fee_bps")
    @classmethod
    def bounded_fee(cls, value: str) -> str:
        parsed = decimal_from_string(
            value,
            code="SCALP_TEMPLATE_FEE_INVALID",
            non_negative=True,
        )
        if parsed > Decimal("1000"):
            raise ValueError("SCALP_TEMPLATE_FEE_INVALID")
        return canonical_decimal(parsed)

    @property
    def digest(self) -> str:
        return content_digest(self)

    def order_schedule_spec(
        self,
        *,
        maker_limit_price: str | None = None,
    ) -> OrderScheduleSpec:
        """Build one protected entry, freezing a maker price when selected."""

        if self.entry_mode == "MAKER_ONLY_SAME_SIDE":
            if maker_limit_price is None:
                raise ValueError("SCALP_MAKER_LIMIT_PRICE_REQUIRED")
            price_distribution = SinglePrice(limit_price=maker_limit_price)
            venue_policy = VenueOrderPolicy(post_only=True)
        else:
            price_distribution = SinglePrice(limit_price=None)
            venue_policy = VenueOrderPolicy(
                order_type=VenueOrderType.MARKET,
                time_in_force=None,
            )
        return OrderScheduleSpec(
            entry_program=EntryProgram(kind=EntryProgramKind.ONE_TIME),
            price_distribution=price_distribution,
            amount_distribution=AmountDistribution(
                mode=AmountDistributionMode.FIXED,
                base_notional=self.notional,
            ),
            venue_policy=venue_policy,
            submission_mode=ScheduleSubmissionMode.SERIAL_PROTECTED,
            submission_order=ScheduleSubmissionOrder.LOW_TO_HIGH,
            entry_conditions=ConditionGroup(
                items=(
                    DecisionBasisReadyCondition(),
                    SpreadBpsCondition(maximum_bps=self.max_spread_bps),
                )
            ),
            protection_policy=ProtectionPolicy(
                initial_stop=InitialStopSpec(distance_bps=self.initial_stop_bps),
                full_fill_loss_budget=FullFillLossBudgetSpec(
                    entry_fee_bps=self.entry_fee_bps,
                    exit_fee_bps=self.exit_fee_bps,
                ),
                take_profit_ladder=TakeProfitLadderSpec(
                    levels=(
                        TakeProfitLevel(
                            trigger_r=self.take_profit_r,
                            quantity_fraction="1",
                        ),
                    )
                ),
                time_exit_seconds=self.max_holding_seconds,
            ),
        )


def scalp_playbook_ref(template: ScalpTemplate) -> str:
    """Return the stable Demo/Live cohort identity for one exact template."""

    return f"SCALP_TEMPLATE:{template.digest}"


class ScalpTriggerPayload(ScalpModel):
    instrument_ref: str = Field(min_length=1, max_length=96)
    direction: Direction
    template: ScalpTemplate


class ScalpRecommendationPayload(ScalpModel):
    instrument_ref: str = Field(min_length=1, max_length=96)
    template: ScalpTemplate


class ScalpRecommendationResponse(ScalpModel):
    instrument_ref: str
    source: str
    source_cutoff: datetime
    recommended_template: ScalpTemplate
    basis: tuple[str, ...]


class BinanceContract(ScalpModel):
    instrument_ref: str
    symbol: str
    base_asset: str
    quote_asset: Literal["USDT"] = "USDT"
    contract_type: Literal["PERPETUAL"] = "PERPETUAL"


class BinanceContractCatalog(ScalpModel):
    source: str
    source_cutoff: datetime
    contracts: tuple[BinanceContract, ...]


class ScalpCycleRecord(ScalpModel):
    cycle_id: str
    environment_id: str
    account_ref: str
    instrument_ref: str
    direction: Direction
    template: ScalpTemplate
    template_digest: str
    request_digest: str
    idempotency_key: str
    plan_id: str
    plan_version_id: str
    activation_id: str
    triggered_at: datetime


class ScalpCycleCreateResponse(ScalpModel):
    cycle: ScalpCycleRecord
    activation: dict[str, Any]
    venue_write_created: Literal[False] = False
    runtime_real_write_gate: str


class ScalpCycleResult(ScalpModel):
    cycle_id: str
    activation_id: str
    instrument_ref: str
    direction: Direction
    triggered_at: datetime
    lifecycle: str
    activation_state_version: int
    result_status: Literal["OPEN", "RELIABLE", "NO_TRADE", "UNKNOWN"]
    gross_pnl: str | None = None
    commission: str | None = None
    funding: str | None = None
    net_pnl: str | None = None
    entry_notional: str | None = None
    holding_duration_seconds: str | None = None
    currency: str | None = None


class ScalpAggregate(ScalpModel):
    cycle_count: int
    open_cycle_count: int
    no_trade_cycle_count: int
    unknown_result_count: int
    reliable_trade_count: int
    win_count: int
    loss_count: int
    flat_count: int
    gross_pnl: str | None = None
    commission: str | None = None
    funding: str | None = None
    net_pnl: str | None = None
    win_rate: str | None = None
    currency: str = "USDT"


class ScalpResultsResponse(ScalpModel):
    environment_id: str
    account_ref: str
    scope: Literal["ALL", "TODAY", "ANCHOR"]
    range_start: datetime | None = None
    range_end: datetime
    fact_cutoff: datetime
    latest_cycle: ScalpCycleResult | None = None
    aggregate: ScalpAggregate


def recommended_scalp_template(
    template: ScalpTemplate,
    market: Any,
) -> ScalpRecommendationResponse:
    reference = decimal_from_string(
        str(market.reference_price),
        code="SCALP_RECOMMENDATION_MARKET_INVALID",
        positive=True,
    )
    atr = decimal_from_string(
        str(market.atr_14),
        code="SCALP_RECOMMENDATION_MARKET_INVALID",
        positive=True,
    )
    bid = decimal_from_string(
        str(market.bid_price),
        code="SCALP_RECOMMENDATION_MARKET_INVALID",
        positive=True,
    )
    ask = decimal_from_string(
        str(market.ask_price),
        code="SCALP_RECOMMENDATION_MARKET_INVALID",
        positive=True,
    )
    if ask < bid:
        raise ValueError("SCALP_RECOMMENDATION_MARKET_INVALID")
    stop_bps = min(
        Decimal("500"),
        max(
            Decimal("8"),
            (atr / reference * Decimal("8000")).quantize(
                Decimal("0.1"),
                rounding=ROUND_HALF_UP,
            ),
        ),
    )
    spread_bps = (ask - bid) / ((ask + bid) / Decimal(2)) * Decimal(10_000)
    maximum_spread = min(
        Decimal("100"),
        max(
            Decimal("2"),
            (spread_bps * Decimal("2.5")).quantize(
                Decimal("0.1"),
                rounding=ROUND_HALF_UP,
            ),
        ),
    )
    recommended = template.model_copy(
        update={
            "initial_stop_bps": canonical_decimal(stop_bps),
            "take_profit_r": "1.5",
            "max_holding_seconds": 300,
            "max_spread_bps": canonical_decimal(maximum_spread),
        }
    )
    return ScalpRecommendationResponse(
        instrument_ref=str(market.instrument_ref),
        source=str(market.source),
        source_cutoff=market.source_cutoff,
        recommended_template=recommended,
        basis=("15m ATR × 0.8", "当前价差 × 2.5"),
    )


def is_internal_scalp_cycle(
    value: TradePlanContent | TradePlanVersion,
) -> bool:
    if value.terms.get("workflow_kind") != SCALP_WORKFLOW_KIND:
        return False
    if value.decision_basis.kind is not DecisionBasisKind.DIRECT_EXECUTION:
        return False
    spec = value.order_schedule_spec
    try:
        template = ScalpTemplate.model_validate(value.terms.get("scalp_template"))
    except (TypeError, ValueError):
        return False
    if value.terms.get("scalp_template_digest") != template.digest:
        return False
    maker_limit_price = value.terms.get("scalp_entry_limit_price")
    if maker_limit_price is not None and not isinstance(maker_limit_price, str):
        return False
    try:
        expected_spec = template.order_schedule_spec(
            maker_limit_price=maker_limit_price,
        )
    except ValueError:
        return False
    if spec is None or spec != expected_spec:
        return False
    if value.position_alignment is not None:
        return False
    if (
        value.target_exposure != template.notional
        or value.requested_limits.max_notional != template.notional
    ):
        return False
    context = value.decision_context
    return bool(
        context is not None
        and context.rationale == "人工选择方向并触发剥头皮周期"
        and context.intent is not None
        and context.intent.value == "PROFIT_SEEKING"
        and context.setup_family is not None
        and context.setup_family.value == "OTHER"
        and context.playbook_ref == scalp_playbook_ref(template)
        and context.evidence_cutoff is not None
    )
