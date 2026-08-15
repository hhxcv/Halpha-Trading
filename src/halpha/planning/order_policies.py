"""Bounded entry, protection, and dynamic policies for one order schedule.

The module is deliberately a small typed catalog, not a script or rule engine.
It owns pure validation and three-valued condition evaluation only; venue state
and mutation responsibility remain in TRADEPLAN/EXE records.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from halpha.domain_values import canonical_decimal, decimal_from_string


MAX_POLICY_ITEMS = 8
MAX_POLICY_SIGNIFICANT_DIGITS = 38
MAX_POLICY_ABS_ADJUSTED_EXPONENT = 18


class PolicyModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_serialization_defaults_required=True,
    )


def _decimal(
    value: str,
    *,
    code: str,
    positive: bool = False,
    non_negative: bool = False,
) -> str:
    parsed = decimal_from_string(
        value,
        code=code,
        positive=positive,
        non_negative=non_negative,
    )
    if (
        len(parsed.as_tuple().digits) > MAX_POLICY_SIGNIFICANT_DIGITS
        or abs(parsed.adjusted()) > MAX_POLICY_ABS_ADJUSTED_EXPONENT
    ):
        raise ValueError(code)
    return canonical_decimal(parsed)


class ConditionOperator(StrEnum):
    ALL = "ALL"
    ANY = "ANY"


class ConditionResult(StrEnum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


class EntryConditionKind(StrEnum):
    DECISION_BASIS_READY = "DECISION_BASIS_READY"
    MARK_PRICE = "MARK_PRICE"
    CLOSED_BAR_PRICE_15M = "CLOSED_BAR_PRICE_15M"
    SPREAD_BPS = "SPREAD_BPS"
    PRICE_MOVE_BPS = "PRICE_MOVE_BPS"
    PROFIT_R = "PROFIT_R"
    ELAPSED_SECONDS = "ELAPSED_SECONDS"


class NumericComparator(StrEnum):
    GTE = "GTE"
    LTE = "LTE"
    DROP_GTE = "DROP_GTE"
    ABS_GTE = "ABS_GTE"


class DecisionBasisReadyCondition(PolicyModel):
    kind: Literal[EntryConditionKind.DECISION_BASIS_READY] = (
        EntryConditionKind.DECISION_BASIS_READY
    )


class MarkPriceCondition(PolicyModel):
    kind: Literal[EntryConditionKind.MARK_PRICE] = EntryConditionKind.MARK_PRICE
    comparator: Literal[NumericComparator.GTE, NumericComparator.LTE]
    price: str

    @field_validator("price")
    @classmethod
    def positive_price(cls, value: str) -> str:
        return _decimal(value, code="ENTRY_CONDITION_INVALID", positive=True)


class ClosedBarPrice15mCondition(PolicyModel):
    """Compare only the latest fully closed 15-minute bar close."""

    kind: Literal[EntryConditionKind.CLOSED_BAR_PRICE_15M] = (
        EntryConditionKind.CLOSED_BAR_PRICE_15M
    )
    comparator: Literal[NumericComparator.GTE, NumericComparator.LTE]
    price: str

    @field_validator("price")
    @classmethod
    def positive_price(cls, value: str) -> str:
        return _decimal(value, code="ENTRY_CONDITION_INVALID", positive=True)


class SpreadBpsCondition(PolicyModel):
    kind: Literal[EntryConditionKind.SPREAD_BPS] = EntryConditionKind.SPREAD_BPS
    maximum_bps: str

    @field_validator("maximum_bps")
    @classmethod
    def non_negative_bps(cls, value: str) -> str:
        return _decimal(value, code="ENTRY_CONDITION_INVALID", non_negative=True)


class PriceMoveBpsCondition(PolicyModel):
    kind: Literal[EntryConditionKind.PRICE_MOVE_BPS] = EntryConditionKind.PRICE_MOVE_BPS
    comparator: NumericComparator
    threshold_bps: str
    window_seconds: int = Field(ge=1, le=300)

    @field_validator("threshold_bps")
    @classmethod
    def threshold_is_valid(cls, value: str) -> str:
        return _decimal(
            value,
            code="ENTRY_CONDITION_INVALID",
            positive=True,
        )


class ProfitRCondition(PolicyModel):
    kind: Literal[EntryConditionKind.PROFIT_R] = EntryConditionKind.PROFIT_R
    comparator: Literal[NumericComparator.GTE, NumericComparator.LTE]
    threshold_r: str

    @field_validator("threshold_r")
    @classmethod
    def finite_r(cls, value: str) -> str:
        return _decimal(value, code="ENTRY_CONDITION_INVALID")


class ElapsedCondition(PolicyModel):
    kind: Literal[EntryConditionKind.ELAPSED_SECONDS] = (
        EntryConditionKind.ELAPSED_SECONDS
    )
    minimum_seconds: int = Field(ge=0, le=604_800)


EntryCondition = Annotated[
    DecisionBasisReadyCondition
    | MarkPriceCondition
    | ClosedBarPrice15mCondition
    | SpreadBpsCondition
    | PriceMoveBpsCondition
    | ProfitRCondition
    | ElapsedCondition,
    Field(discriminator="kind"),
]


class ConditionGroup(PolicyModel):
    operator: ConditionOperator = ConditionOperator.ALL
    items: tuple[EntryCondition, ...] = (DecisionBasisReadyCondition(),)

    @model_validator(mode="after")
    def item_count_is_bounded(self) -> ConditionGroup:
        if not self.items or len(self.items) > MAX_POLICY_ITEMS:
            raise ValueError("ENTRY_CONDITION_COUNT_INVALID")
        return self


class ConditionFactProvenance(PolicyModel):
    """Identify the stream facts and clock boundary used by one evaluation."""

    source: str = Field(min_length=1, max_length=128)
    source_cutoff: datetime
    evaluated_at: datetime
    quote_source_time: datetime | None = None
    quote_received_at: datetime | None = None
    mark_source_time: datetime | None = None
    mark_received_at: datetime | None = None

    @model_validator(mode="after")
    def times_are_aware_and_ordered(self) -> ConditionFactProvenance:
        values = (
            self.source_cutoff,
            self.evaluated_at,
            self.quote_source_time,
            self.quote_received_at,
            self.mark_source_time,
            self.mark_received_at,
        )
        if any(value is not None and value.utcoffset() is None for value in values):
            raise ValueError("CONDITION_FACT_PROVENANCE_INVALID")
        if self.source_cutoff > self.evaluated_at:
            raise ValueError("CONDITION_FACT_PROVENANCE_INVALID")
        pairs = (
            (self.quote_source_time, self.quote_received_at),
            (self.mark_source_time, self.mark_received_at),
        )
        for source_time, received_at in pairs:
            if (source_time is None) != (received_at is None):
                raise ValueError("CONDITION_FACT_PROVENANCE_INVALID")
            if (
                source_time is not None
                and received_at is not None
                and (source_time > received_at or received_at > self.evaluated_at)
            ):
                raise ValueError("CONDITION_FACT_PROVENANCE_INVALID")
        return self


class ConditionFacts(PolicyModel):
    basis_ready: bool | None = None
    mark_price: str | None = None
    closed_bar_15m_close: str | None = None
    closed_bar_15m_at: datetime | None = None
    bid_price: str | None = None
    ask_price: str | None = None
    price_move_bps_by_window: dict[int, str] = Field(default_factory=dict)
    profit_r: str | None = None
    elapsed_seconds: int | None = Field(default=None, ge=0)
    provenance: ConditionFactProvenance | None = None

    @field_validator(
        "mark_price",
        "closed_bar_15m_close",
        "bid_price",
        "ask_price",
    )
    @classmethod
    def optional_positive_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _decimal(value, code="CONDITION_FACT_INVALID", positive=True)

    @field_validator("profit_r")
    @classmethod
    def optional_finite_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _decimal(value, code="CONDITION_FACT_INVALID")

    @field_validator("price_move_bps_by_window")
    @classmethod
    def move_windows_are_valid(cls, values: dict[int, str]) -> dict[int, str]:
        if any(window < 1 or window > 300 for window in values):
            raise ValueError("CONDITION_FACT_INVALID")
        return {
            window: _decimal(value, code="CONDITION_FACT_INVALID")
            for window, value in values.items()
        }

    @model_validator(mode="after")
    def book_is_consistent(self) -> ConditionFacts:
        if (self.bid_price is None) != (self.ask_price is None):
            raise ValueError("CONDITION_FACT_INVALID")
        if (
            self.bid_price is not None
            and self.ask_price is not None
            and Decimal(self.bid_price) > Decimal(self.ask_price)
        ):
            raise ValueError("CONDITION_FACT_INVALID")
        if (self.closed_bar_15m_close is None) != (
            self.closed_bar_15m_at is None
        ):
            raise ValueError("CONDITION_FACT_INVALID")
        if (
            self.closed_bar_15m_at is not None
            and self.closed_bar_15m_at.utcoffset() is None
        ):
            raise ValueError("CONDITION_FACT_INVALID")
        if (
            self.closed_bar_15m_at is not None
            and self.provenance is not None
            and self.closed_bar_15m_at > self.provenance.evaluated_at
        ):
            raise ValueError("CONDITION_FACT_INVALID")
        return self


class ConditionEvaluation(PolicyModel):
    result: ConditionResult
    item_results: tuple[ConditionResult, ...]


class RuntimeConditionState(PolicyModel):
    """Latest Executor-owned entry-condition observation for one activation."""

    result: ConditionResult
    item_results: tuple[ConditionResult, ...]
    phase: Literal["INITIAL", "PRE_SUBMIT_RECHECK", "LATER_LEG_RECHECK"]
    source_cutoff: datetime
    evaluated_at: datetime
    facts: ConditionFacts
    submission_ready: bool | None = None
    blocking_reason: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def timestamps_and_result_are_consistent(self) -> RuntimeConditionState:
        if (
            self.source_cutoff.utcoffset() is None
            or self.evaluated_at.utcoffset() is None
        ):
            raise ValueError("RUNTIME_CONDITION_STATE_INVALID")
        if self.source_cutoff > self.evaluated_at:
            raise ValueError("RUNTIME_CONDITION_STATE_INVALID")
        if len(self.item_results) > MAX_POLICY_ITEMS:
            raise ValueError("RUNTIME_CONDITION_STATE_INVALID")
        if (
            self.result is not ConditionResult.TRUE
            and self.submission_ready is not None
        ):
            raise ValueError("RUNTIME_CONDITION_STATE_INVALID")
        if self.submission_ready is False and self.blocking_reason is None:
            raise ValueError("RUNTIME_CONDITION_STATE_INVALID")
        return self


def _compare(value: Decimal, comparator: NumericComparator, threshold: Decimal) -> bool:
    if comparator is NumericComparator.GTE:
        return value >= threshold
    if comparator is NumericComparator.LTE:
        return value <= threshold
    if comparator is NumericComparator.DROP_GTE:
        return value <= -threshold
    return abs(value) >= threshold


def evaluate_condition(
    condition: EntryCondition,
    facts: ConditionFacts,
) -> ConditionResult:
    """Evaluate one typed condition without inventing a missing fact."""

    if isinstance(condition, DecisionBasisReadyCondition):
        if facts.basis_ready is None:
            return ConditionResult.UNKNOWN
        return ConditionResult.TRUE if facts.basis_ready else ConditionResult.FALSE
    if isinstance(condition, MarkPriceCondition):
        if facts.mark_price is None:
            return ConditionResult.UNKNOWN
        matched = _compare(
            Decimal(facts.mark_price),
            NumericComparator(condition.comparator),
            Decimal(condition.price),
        )
    elif isinstance(condition, ClosedBarPrice15mCondition):
        if facts.closed_bar_15m_close is None:
            return ConditionResult.UNKNOWN
        matched = _compare(
            Decimal(facts.closed_bar_15m_close),
            NumericComparator(condition.comparator),
            Decimal(condition.price),
        )
    elif isinstance(condition, SpreadBpsCondition):
        if facts.bid_price is None or facts.ask_price is None:
            return ConditionResult.UNKNOWN
        bid = Decimal(facts.bid_price)
        ask = Decimal(facts.ask_price)
        midpoint = (bid + ask) / Decimal(2)
        if midpoint <= 0:
            return ConditionResult.UNKNOWN
        spread_bps = (ask - bid) / midpoint * Decimal(10_000)
        matched = spread_bps <= Decimal(condition.maximum_bps)
    elif isinstance(condition, PriceMoveBpsCondition):
        value = facts.price_move_bps_by_window.get(condition.window_seconds)
        if value is None:
            return ConditionResult.UNKNOWN
        matched = _compare(
            Decimal(value),
            condition.comparator,
            Decimal(condition.threshold_bps),
        )
    elif isinstance(condition, ProfitRCondition):
        if facts.profit_r is None:
            return ConditionResult.UNKNOWN
        matched = _compare(
            Decimal(facts.profit_r),
            NumericComparator(condition.comparator),
            Decimal(condition.threshold_r),
        )
    else:
        if facts.elapsed_seconds is None:
            return ConditionResult.UNKNOWN
        matched = facts.elapsed_seconds >= condition.minimum_seconds
    return ConditionResult.TRUE if matched else ConditionResult.FALSE


def evaluate_condition_group(
    group: ConditionGroup,
    facts: ConditionFacts,
) -> ConditionEvaluation:
    """Apply Kleene ALL/ANY semantics to one deliberately shallow group."""

    results = tuple(evaluate_condition(item, facts) for item in group.items)
    if group.operator is ConditionOperator.ALL:
        if ConditionResult.FALSE in results:
            result = ConditionResult.FALSE
        elif all(item is ConditionResult.TRUE for item in results):
            result = ConditionResult.TRUE
        else:
            result = ConditionResult.UNKNOWN
    elif ConditionResult.TRUE in results:
        result = ConditionResult.TRUE
    elif all(item is ConditionResult.FALSE for item in results):
        result = ConditionResult.FALSE
    else:
        result = ConditionResult.UNKNOWN
    return ConditionEvaluation(result=result, item_results=results)


class StopTriggerSource(StrEnum):
    MARK_PRICE = "MARK_PRICE"


class StopCoverage(StrEnum):
    EACH_CONFIRMED_FILL = "EACH_CONFIRMED_FILL"


class InitialStopSpec(PolicyModel):
    distance_bps: str
    trigger_source: StopTriggerSource = StopTriggerSource.MARK_PRICE
    coverage: StopCoverage = StopCoverage.EACH_CONFIRMED_FILL

    @field_validator("distance_bps")
    @classmethod
    def bounded_distance(cls, value: str) -> str:
        normalized = _decimal(
            value,
            code="PROTECTION_POLICY_INVALID",
            positive=True,
        )
        if Decimal(normalized) >= Decimal(10_000):
            raise ValueError("PROTECTION_POLICY_INVALID")
        return normalized


class FullFillLossBudgetSpec(PolicyModel):
    """Fee assumptions frozen with a new direct plan's maximum-loss preview."""

    entry_fee_bps: str
    exit_fee_bps: str

    @field_validator("entry_fee_bps", "exit_fee_bps")
    @classmethod
    def bounded_fee_rate(cls, value: str) -> str:
        normalized = _decimal(
            value,
            code="PROTECTION_LOSS_BUDGET_INVALID",
            non_negative=True,
        )
        if Decimal(normalized) > Decimal(1_000):
            raise ValueError("PROTECTION_LOSS_BUDGET_INVALID")
        return normalized


class TakeProfitLevel(PolicyModel):
    trigger_r: str
    quantity_fraction: str

    @field_validator("trigger_r", "quantity_fraction")
    @classmethod
    def positive_value(cls, value: str) -> str:
        return _decimal(value, code="PROTECTION_POLICY_INVALID", positive=True)

    @model_validator(mode="after")
    def fraction_is_bounded(self) -> TakeProfitLevel:
        if Decimal(self.quantity_fraction) > 1:
            raise ValueError("PROTECTION_POLICY_INVALID")
        return self


class TakeProfitLadderSpec(PolicyModel):
    levels: tuple[TakeProfitLevel, ...]

    @model_validator(mode="after")
    def ladder_is_bounded_and_ordered(self) -> TakeProfitLadderSpec:
        if not self.levels or len(self.levels) > MAX_POLICY_ITEMS:
            raise ValueError("TAKE_PROFIT_LEVEL_COUNT_INVALID")
        triggers = tuple(Decimal(item.trigger_r) for item in self.levels)
        if any(right <= left for left, right in zip(triggers, triggers[1:])):
            raise ValueError("TAKE_PROFIT_TRIGGER_ORDER_INVALID")
        if (
            sum(
                (Decimal(item.quantity_fraction) for item in self.levels),
                Decimal(0),
            )
            > 1
        ):
            raise ValueError("TAKE_PROFIT_FRACTION_EXCEEDED")
        return self


class ProtectionPolicy(PolicyModel):
    initial_stop: InitialStopSpec
    full_fill_loss_budget: FullFillLossBudgetSpec | None = None
    take_profit_ladder: TakeProfitLadderSpec | None = None
    time_exit_seconds: int | None = Field(default=None, ge=1, le=2_592_000)


class DynamicRuleKind(StrEnum):
    CANCEL_ON_SHOCK = "CANCEL_ON_SHOCK"
    EXPIRE_REMAINING = "EXPIRE_REMAINING"
    REPRICE_ENTRY = "REPRICE_ENTRY"
    STEPPED_PROTECTION = "STEPPED_PROTECTION"
    PROFIT_LOCK = "PROFIT_LOCK"


class CancelOnShockRule(PolicyModel):
    kind: Literal[DynamicRuleKind.CANCEL_ON_SHOCK] = DynamicRuleKind.CANCEL_ON_SHOCK
    window_seconds: int | None = Field(default=None, ge=1, le=300)
    adverse_move_bps: str | None = None
    invalidation_price: str | None = None
    opportunity_missed_price: str | None = None
    max_triggers: int = Field(default=1, ge=1, le=8)

    @field_validator(
        "adverse_move_bps",
        "invalidation_price",
        "opportunity_missed_price",
    )
    @classmethod
    def positive_threshold(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _decimal(value, code="DYNAMIC_RULE_INVALID", positive=True)

    @model_validator(mode="after")
    def has_complete_invalidation_trigger(self) -> CancelOnShockRule:
        shock_fields = (self.window_seconds, self.adverse_move_bps)
        if (shock_fields[0] is None) != (shock_fields[1] is None):
            raise ValueError("DYNAMIC_RULE_INVALID")
        if (
            self.invalidation_price is None
            and self.opportunity_missed_price is None
            and shock_fields[0] is None
        ):
            raise ValueError("DYNAMIC_RULE_INVALID")
        return self


class ExpireRemainingRule(PolicyModel):
    kind: Literal[DynamicRuleKind.EXPIRE_REMAINING] = DynamicRuleKind.EXPIRE_REMAINING
    after_seconds: int = Field(ge=1, le=604_800)


class RepriceEntryRule(PolicyModel):
    """Bounded cancel-confirm-replace for one still-unfilled entry order."""

    kind: Literal[DynamicRuleKind.REPRICE_ENTRY] = DynamicRuleKind.REPRICE_ENTRY
    trigger_distance_bps: str = "5"
    book_offset_bps: str = "1"
    maximum_total_move_bps: str = "30"
    minimum_update_interval_seconds: int = Field(default=5, ge=1, le=3_600)
    max_adjustments: int = Field(default=3, ge=1, le=8)

    @field_validator("trigger_distance_bps", "maximum_total_move_bps")
    @classmethod
    def positive_distance(cls, value: str) -> str:
        normalized = _decimal(value, code="DYNAMIC_RULE_INVALID", positive=True)
        if Decimal(normalized) > Decimal(2_000):
            raise ValueError("DYNAMIC_RULE_INVALID")
        return normalized

    @field_validator("book_offset_bps")
    @classmethod
    def non_negative_offset(cls, value: str) -> str:
        normalized = _decimal(
            value,
            code="DYNAMIC_RULE_INVALID",
            non_negative=True,
        )
        if Decimal(normalized) > Decimal(500):
            raise ValueError("DYNAMIC_RULE_INVALID")
        return normalized

    @model_validator(mode="after")
    def trigger_fits_total_boundary(self) -> RepriceEntryRule:
        if Decimal(self.trigger_distance_bps) > Decimal(
            self.maximum_total_move_bps
        ):
            raise ValueError("DYNAMIC_RULE_INVALID")
        return self


class ProtectionStep(PolicyModel):
    trigger_r: str
    stop_r: str

    @field_validator("trigger_r")
    @classmethod
    def positive_trigger(cls, value: str) -> str:
        return _decimal(value, code="DYNAMIC_RULE_INVALID", positive=True)

    @field_validator("stop_r")
    @classmethod
    def finite_stop(cls, value: str) -> str:
        return _decimal(value, code="DYNAMIC_RULE_INVALID")

    @model_validator(mode="after")
    def stop_remains_behind_trigger(self) -> ProtectionStep:
        if Decimal(self.stop_r) >= Decimal(self.trigger_r):
            raise ValueError("DYNAMIC_RULE_INVALID")
        return self


class SteppedProtectionRule(PolicyModel):
    kind: Literal[DynamicRuleKind.STEPPED_PROTECTION] = (
        DynamicRuleKind.STEPPED_PROTECTION
    )
    steps: tuple[ProtectionStep, ...]
    minimum_update_interval_seconds: int = Field(default=5, ge=1, le=3_600)
    max_adjustments: int = Field(default=8, ge=1, le=8)

    @model_validator(mode="after")
    def steps_are_monotonic(self) -> SteppedProtectionRule:
        if not self.steps or len(self.steps) > MAX_POLICY_ITEMS:
            raise ValueError("DYNAMIC_RULE_STEP_COUNT_INVALID")
        triggers = tuple(Decimal(item.trigger_r) for item in self.steps)
        stops = tuple(Decimal(item.stop_r) for item in self.steps)
        if any(right <= left for left, right in zip(triggers, triggers[1:])):
            raise ValueError("DYNAMIC_RULE_TRIGGER_ORDER_INVALID")
        if any(right < left for left, right in zip(stops, stops[1:])):
            raise ValueError("DYNAMIC_RULE_STOP_NOT_MONOTONIC")
        if self.max_adjustments < len(self.steps):
            raise ValueError("DYNAMIC_RULE_ADJUSTMENT_LIMIT_INVALID")
        return self


class ProfitLockMode(StrEnum):
    RATIO = "RATIO"
    FIXED_GIVEBACK = "FIXED_GIVEBACK"


class ProfitLockRule(PolicyModel):
    """Continuously tighten protection from observed profit without loosening it."""

    kind: Literal[DynamicRuleKind.PROFIT_LOCK] = DynamicRuleKind.PROFIT_LOCK
    mode: ProfitLockMode = ProfitLockMode.RATIO
    activation_r: str = "1"
    lock_fraction: str | None = "0.5"
    giveback_r: str | None = None
    minimum_step_r: str = "0.25"
    minimum_update_interval_seconds: int = Field(default=5, ge=1, le=3_600)
    max_adjustments: int = Field(default=8, ge=1, le=8)

    @field_validator("activation_r", "minimum_step_r")
    @classmethod
    def positive_r(cls, value: str) -> str:
        return _decimal(value, code="DYNAMIC_RULE_INVALID", positive=True)

    @field_validator("lock_fraction", "giveback_r")
    @classmethod
    def optional_positive_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _decimal(value, code="DYNAMIC_RULE_INVALID", positive=True)

    @model_validator(mode="after")
    def mode_parameters_are_consistent(self) -> ProfitLockRule:
        if self.mode is ProfitLockMode.RATIO:
            if (
                self.lock_fraction is None
                or Decimal(self.lock_fraction) > 1
                or self.giveback_r is not None
            ):
                raise ValueError("DYNAMIC_RULE_INVALID")
        elif self.giveback_r is None or self.lock_fraction is not None:
            raise ValueError("DYNAMIC_RULE_INVALID")
        return self


DynamicRule = Annotated[
    CancelOnShockRule
    | ExpireRemainingRule
    | RepriceEntryRule
    | SteppedProtectionRule
    | ProfitLockRule,
    Field(discriminator="kind"),
]


def validate_dynamic_rules(
    rules: tuple[DynamicRule, ...],
    *,
    protection_policy: ProtectionPolicy | None,
) -> tuple[DynamicRule, ...]:
    if len(rules) > MAX_POLICY_ITEMS:
        raise ValueError("DYNAMIC_RULE_COUNT_INVALID")
    kinds = tuple(item.kind for item in rules)
    if len(set(kinds)) != len(kinds):
        raise ValueError("DYNAMIC_RULE_DUPLICATE")
    if (
        DynamicRuleKind.STEPPED_PROTECTION in kinds
        or DynamicRuleKind.PROFIT_LOCK in kinds
    ) and protection_policy is None:
        raise ValueError("DYNAMIC_PROTECTION_REQUIRES_INITIAL_STOP")
    if (
        DynamicRuleKind.STEPPED_PROTECTION in kinds
        and DynamicRuleKind.PROFIT_LOCK in kinds
    ):
        raise ValueError("DYNAMIC_PROTECTION_MODE_CONFLICT")
    return rules


class CompiledProtectionTargets(PolicyModel):
    risk_distance: str
    initial_stop_price: str
    take_profit_prices: tuple[str, ...]


def _tick_quantize(value: Decimal, tick: Decimal, *, round_up: bool) -> Decimal:
    rounding = ROUND_UP if round_up else ROUND_DOWN
    return (value / tick).to_integral_value(rounding=rounding) * tick


def compile_protection_targets(
    policy: ProtectionPolicy,
    *,
    direction: Literal["LONG", "SHORT"],
    fill_price: str,
    price_tick_size: str,
) -> CompiledProtectionTargets:
    """Compile fill-relative stop/TP prices with risk-conservative rounding."""

    fill = Decimal(_decimal(fill_price, code="PROTECTION_FACT_INVALID", positive=True))
    tick = Decimal(
        _decimal(price_tick_size, code="PROTECTION_FACT_INVALID", positive=True)
    )
    distance = fill * Decimal(policy.initial_stop.distance_bps) / Decimal(10_000)
    if direction == "LONG":
        raw_stop = fill - distance
        stop = _tick_quantize(raw_stop, tick, round_up=True)
        sign = Decimal(1)
    else:
        raw_stop = fill + distance
        stop = _tick_quantize(raw_stop, tick, round_up=False)
        sign = Decimal(-1)
    if (
        stop <= 0
        or (direction == "LONG" and stop >= fill)
        or (direction == "SHORT" and stop <= fill)
    ):
        raise ValueError("PROTECTION_PRICE_INVALID")
    ladder = policy.take_profit_ladder
    targets = (
        ()
        if ladder is None
        else tuple(
            canonical_decimal(
                _tick_quantize(
                    fill + sign * distance * Decimal(level.trigger_r),
                    tick,
                    round_up=direction == "SHORT",
                )
            )
            for level in ladder.levels
        )
    )
    target_values = tuple(Decimal(item) for item in targets)
    if any(item <= 0 for item in target_values):
        raise ValueError("PROTECTION_PRICE_INVALID")
    if direction == "LONG" and (
        any(item <= fill for item in target_values)
        or any(
            current <= previous
            for previous, current in zip(target_values, target_values[1:])
        )
    ):
        raise ValueError("PROTECTION_PRICE_INVALID")
    if direction == "SHORT" and (
        any(item >= fill for item in target_values)
        or any(
            current >= previous
            for previous, current in zip(target_values, target_values[1:])
        )
    ):
        raise ValueError("PROTECTION_PRICE_INVALID")
    return CompiledProtectionTargets(
        risk_distance=canonical_decimal(distance),
        initial_stop_price=canonical_decimal(stop),
        take_profit_prices=targets,
    )
