"""Pure one-shot Donchian/ATR decision and exact Decimal sizing logic.

This module deliberately imports no NautilusTrader order, Strategy, node, or
execution API. Native indicator calculation is supplied through the separate
indicator boundary; this class only evaluates its immutable output.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, ROUND_DOWN
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from halpha.domain_values import canonical_decimal, content_digest, decimal_from_string
from halpha.planning.registry import Direction, ONE_SHOT_STRATEGY_ID, OneShotParameters


class RiskDirection(StrEnum):
    INCREASE = "INCREASE"
    REDUCE = "REDUCE"


class ActivationStrategyState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entry_opportunity_consumed: bool = False
    lifecycle: str = "RUNNING"
    run_state: str = "ACTIVE"
    new_risk_allowed: bool = True


class NativeIndicatorSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    upper: str
    lower: str
    atr: str
    initialized: bool
    source_digest: str
    source_cutoff_ns: int

    @field_validator("upper", "lower", "atr")
    @classmethod
    def finite_values(cls, value: str) -> str:
        return canonical_decimal(
            decimal_from_string(value, code="INDICATOR_VALUE_INVALID")
        )


class InstrumentQuantityRules(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_size: str
    price_tick_size: str
    min_quantity: str
    max_market_quantity: str
    min_notional: str

    @field_validator(
        "step_size",
        "price_tick_size",
        "min_quantity",
        "max_market_quantity",
        "min_notional",
    )
    @classmethod
    def positive_values(cls, value: str) -> str:
        return canonical_decimal(
            decimal_from_string(value, code="INSTRUMENT_RULE_INVALID", positive=True)
        )


class EntryEvaluationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    activation_id: str
    instrument_id: str
    source_identity: str
    source_cutoff: datetime
    input_digest: str
    decision_at: datetime
    valid_until: datetime
    confirmation_closes: tuple[str, ...]
    confirmation_close_times: tuple[datetime, ...]
    indicators: NativeIndicatorSnapshot
    reference_price: str
    reference_source: str
    max_allowed_loss: str
    max_notional: str
    max_margin: str
    effective_leverage: str
    taker_fee_rate: str
    rules: InstrumentQuantityRules

    @field_validator(
        "reference_price",
        "max_allowed_loss",
        "max_notional",
        "max_margin",
        "effective_leverage",
        "rules",
    )
    @classmethod
    def retain_values(cls, value: object) -> object:
        return value

    @field_validator("confirmation_closes")
    @classmethod
    def closes_are_positive(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("CONFIRMATION_BARS_MISSING")
        return tuple(
            canonical_decimal(
                decimal_from_string(value, code="BAR_VALUE_INVALID", positive=True)
            )
            for value in values
        )

    @model_validator(mode="after")
    def time_window_is_valid(self) -> EntryEvaluationInput:
        if not self.source_identity.startswith(f"{self.activation_id}:"):
            raise ValueError("SOURCE_IDENTITY_MISMATCH")
        if self.source_cutoff > self.decision_at:
            raise ValueError("SOURCE_CUTOFF_AFTER_DECISION")
        if self.valid_until <= self.decision_at:
            raise ValueError("PROPOSAL_WINDOW_INVALID")
        if len(self.confirmation_close_times) != len(self.confirmation_closes):
            raise ValueError("CONFIRMATION_BAR_IDENTITY_INCOMPLETE")
        if any(item.utcoffset() is None for item in self.confirmation_close_times):
            raise ValueError("CONFIRMATION_BAR_TIMEZONE_REQUIRED")
        if self.confirmation_close_times[-1] != self.source_cutoff:
            raise ValueError("CONFIRMATION_BAR_CUTOFF_MISMATCH")
        if any(
            later <= earlier
            for earlier, later in zip(
                self.confirmation_close_times,
                self.confirmation_close_times[1:],
            )
        ):
            raise ValueError("CONFIRMATION_BAR_ORDER_INVALID")
        for field in (
            "reference_price",
            "max_allowed_loss",
            "max_notional",
            "max_margin",
            "effective_leverage",
        ):
            decimal_from_string(
                getattr(self, field), code="SIZING_INPUT_INVALID", positive=True
            )
        fee = decimal_from_string(
            self.taker_fee_rate,
            code="SIZING_INPUT_INVALID",
            non_negative=True,
        )
        if fee >= 1:
            raise ValueError("FEE_RATE_INVALID")
        return self


class EntrySetupEvidence(BaseModel):
    """Immutable facts explaining why one strategy entry was proposed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["DONCHIAN_BREAKOUT", "DEMO_ORDER_FLOW_CHECK"]
    direction: Direction
    source_cutoff: datetime
    channel_upper: str
    channel_lower: str
    trigger_boundary: str
    trigger_atr: str
    max_entry_extension_atr: str
    max_entry_extension_price: str
    confirmation_bars_required: int
    confirmation_closes: tuple[str, ...]
    confirmation_close_times: tuple[datetime, ...]
    confirmation_all_beyond_boundary: bool | None
    last_confirmation_extension_price: str
    last_confirmation_extension_atr: str
    last_confirmation_extension_bps: str
    last_confirmation_within_max_extension: bool
    trigger_reference_price: str
    trigger_reference_source: str
    reference_extension_price: str
    reference_extension_atr: str
    reference_extension_bps: str
    reference_within_max_extension: bool
    indicator_source_digest: str
    indicator_source_cutoff_at: datetime

    @field_validator(
        "channel_upper",
        "channel_lower",
        "trigger_boundary",
        "trigger_atr",
        "max_entry_extension_atr",
        "max_entry_extension_price",
        "trigger_reference_price",
    )
    @classmethod
    def positive_prices_and_ranges(cls, value: str) -> str:
        return canonical_decimal(
            decimal_from_string(value, code="ENTRY_SETUP_EVIDENCE_INVALID", positive=True)
        )

    @field_validator(
        "last_confirmation_extension_price",
        "last_confirmation_extension_atr",
        "last_confirmation_extension_bps",
        "reference_extension_price",
        "reference_extension_atr",
        "reference_extension_bps",
    )
    @classmethod
    def finite_distances(cls, value: str) -> str:
        return canonical_decimal(
            decimal_from_string(value, code="ENTRY_SETUP_EVIDENCE_INVALID")
        )

    @field_validator("confirmation_closes")
    @classmethod
    def confirmation_prices_are_positive(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        return tuple(
            canonical_decimal(
                decimal_from_string(
                    value,
                    code="ENTRY_SETUP_EVIDENCE_INVALID",
                    positive=True,
                )
            )
            for value in values
        )

    @field_validator("indicator_source_digest")
    @classmethod
    def indicator_digest_is_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError("ENTRY_SETUP_EVIDENCE_INVALID")
        return value

    @model_validator(mode="after")
    def evidence_is_complete(self) -> EntrySetupEvidence:
        if self.confirmation_bars_required <= 0 or (
            len(self.confirmation_closes) != self.confirmation_bars_required
            or len(self.confirmation_close_times) != self.confirmation_bars_required
        ):
            raise ValueError("ENTRY_SETUP_EVIDENCE_INVALID")
        if any(item.utcoffset() is None for item in self.confirmation_close_times) or (
            self.source_cutoff.utcoffset() is None
            or self.indicator_source_cutoff_at.utcoffset() is None
        ):
            raise ValueError("ENTRY_SETUP_EVIDENCE_INVALID")
        if self.confirmation_close_times[-1] != self.source_cutoff:
            raise ValueError("ENTRY_SETUP_EVIDENCE_INVALID")
        if Decimal(self.channel_upper) <= Decimal(self.channel_lower):
            raise ValueError("ENTRY_SETUP_EVIDENCE_INVALID")
        if self.mode == "DONCHIAN_BREAKOUT":
            expected_boundary = (
                self.channel_upper
                if self.direction is Direction.LONG
                else self.channel_lower
            )
            if self.trigger_boundary != expected_boundary:
                raise ValueError("ENTRY_SETUP_EVIDENCE_INVALID")
            if self.confirmation_all_beyond_boundary is not True:
                raise ValueError("ENTRY_SETUP_EVIDENCE_INVALID")
        elif self.confirmation_all_beyond_boundary is not None:
            raise ValueError("ENTRY_SETUP_EVIDENCE_INVALID")
        return self


class EntryRiskContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trigger_atr: str
    initial_stop_atr_multiple: str
    take_profit_1_r: str
    take_profit_1_fraction: str
    take_profit_2_r: str
    max_hold_bars_15m: int
    indicator_source_digest: str
    indicator_source_cutoff_ns: int
    quantity_step: str
    price_tick_size: str
    entry_extension_boundary: str
    sizing_taker_fee_rate: str
    sizing_effective_leverage: str
    instrument_rules_digest: str
    setup_evidence: EntrySetupEvidence | None = None

    @field_validator(
        "trigger_atr",
        "initial_stop_atr_multiple",
        "take_profit_1_r",
        "take_profit_1_fraction",
        "take_profit_2_r",
        "quantity_step",
        "price_tick_size",
        "entry_extension_boundary",
        "sizing_effective_leverage",
    )
    @classmethod
    def positive_context_values(cls, value: str) -> str:
        return canonical_decimal(
            decimal_from_string(value, code="ENTRY_RISK_CONTEXT_INVALID", positive=True)
        )

    @field_validator("sizing_taker_fee_rate")
    @classmethod
    def fee_rate_is_bounded(cls, value: str) -> str:
        normalized = canonical_decimal(
            decimal_from_string(
                value,
                code="ENTRY_RISK_CONTEXT_INVALID",
                non_negative=True,
            )
        )
        if Decimal(normalized) >= 1:
            raise ValueError("ENTRY_RISK_CONTEXT_INVALID")
        return normalized

    @field_validator("instrument_rules_digest")
    @classmethod
    def rules_digest_is_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError("ENTRY_RISK_CONTEXT_INVALID")
        return value


class StrategyProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy_id: str
    activation_id: str
    rule_id: str
    source_identity: str
    source_cutoff: datetime
    input_digest: str
    instrument_id: str
    direction: Direction
    action_profile: str
    risk_direction: RiskDirection
    quantity: str
    reference_price: str
    reference_source: str
    reason_code: str
    valid_until: datetime
    entry_risk_context: EntryRiskContext | None = None
    proposal_digest: str


class StrategyEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reason_code: str
    proposal: StrategyProposal | None = None


def _floor_to_step(quantity: Decimal, step: Decimal) -> Decimal:
    units = (quantity / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step


def _datetime_from_ns(timestamp_ns: int) -> datetime:
    seconds, nanoseconds = divmod(timestamp_ns, 1_000_000_000)
    return datetime.fromtimestamp(seconds, tz=UTC).replace(
        microsecond=nanoseconds // 1_000,
    )


class OneShotDonchianAtrLogic:
    """Deterministic decision object; activation state is always explicit input."""

    strategy_id = ONE_SHOT_STRATEGY_ID

    def __init__(self, parameters: OneShotParameters) -> None:
        self.parameters = parameters

    def evaluate_entry(
        self,
        evaluation: EntryEvaluationInput,
        state: ActivationStrategyState,
    ) -> StrategyEvaluation:
        if state.entry_opportunity_consumed:
            return StrategyEvaluation(reason_code="ENTRY_OPPORTUNITY_CONSUMED")
        if (
            state.lifecycle != "RUNNING"
            or state.run_state != "ACTIVE"
            or not state.new_risk_allowed
        ):
            return StrategyEvaluation(reason_code="NEW_RISK_NOT_ALLOWED")
        if not evaluation.indicators.initialized:
            return StrategyEvaluation(reason_code="INDICATORS_NOT_INITIALIZED")
        if (
            len(evaluation.confirmation_closes)
            != self.parameters.effective_confirmation_bars_1m
        ):
            return StrategyEvaluation(reason_code="CONFIRMATION_WINDOW_INCOMPLETE")

        upper = Decimal(evaluation.indicators.upper)
        lower = Decimal(evaluation.indicators.lower)
        atr = Decimal(evaluation.indicators.atr)
        if upper <= lower or atr <= 0:
            return StrategyEvaluation(reason_code="INDICATOR_VALUE_INVALID")
        closes = tuple(Decimal(value) for value in evaluation.confirmation_closes)
        extension = Decimal(self.parameters.max_entry_extension_atr) * atr
        reference_price = Decimal(evaluation.reference_price)
        if self.parameters.demo_immediate_entry:
            triggered = True
            entry_extension_boundary = (
                reference_price + extension
                if self.parameters.direction is Direction.LONG
                else reference_price - extension
            )
        elif self.parameters.direction is Direction.LONG:
            triggered = (
                all(close > upper for close in closes)
                and closes[-1] <= upper + extension
            )
            entry_extension_boundary = upper + extension
        else:
            triggered = (
                all(close < lower for close in closes)
                and closes[-1] >= lower - extension
            )
            entry_extension_boundary = lower - extension
        if not triggered:
            return StrategyEvaluation(reason_code="ENTRY_CONDITION_FALSE")

        trigger_boundary = (
            reference_price
            if self.parameters.demo_immediate_entry
            else upper
            if self.parameters.direction is Direction.LONG
            else lower
        )

        def directional_extension(price: Decimal) -> Decimal:
            return (
                price - trigger_boundary
                if self.parameters.direction is Direction.LONG
                else trigger_boundary - price
            )

        last_confirmation_extension = directional_extension(closes[-1])
        reference_extension = directional_extension(reference_price)
        max_extension_atr = Decimal(self.parameters.max_entry_extension_atr)
        setup_evidence = EntrySetupEvidence(
            mode=(
                "DEMO_ORDER_FLOW_CHECK"
                if self.parameters.demo_immediate_entry
                else "DONCHIAN_BREAKOUT"
            ),
            direction=self.parameters.direction,
            source_cutoff=evaluation.source_cutoff,
            channel_upper=canonical_decimal(upper),
            channel_lower=canonical_decimal(lower),
            trigger_boundary=canonical_decimal(trigger_boundary),
            trigger_atr=canonical_decimal(atr),
            max_entry_extension_atr=canonical_decimal(max_extension_atr),
            max_entry_extension_price=canonical_decimal(entry_extension_boundary),
            confirmation_bars_required=(
                self.parameters.effective_confirmation_bars_1m
            ),
            confirmation_closes=evaluation.confirmation_closes,
            confirmation_close_times=evaluation.confirmation_close_times,
            confirmation_all_beyond_boundary=(
                None
                if self.parameters.demo_immediate_entry
                else all(
                    close > trigger_boundary
                    if self.parameters.direction is Direction.LONG
                    else close < trigger_boundary
                    for close in closes
                )
            ),
            last_confirmation_extension_price=canonical_decimal(
                last_confirmation_extension
            ),
            last_confirmation_extension_atr=canonical_decimal(
                last_confirmation_extension / atr
            ),
            last_confirmation_extension_bps=canonical_decimal(
                last_confirmation_extension / trigger_boundary * Decimal("10000")
            ),
            last_confirmation_within_max_extension=(
                last_confirmation_extension <= extension
            ),
            trigger_reference_price=canonical_decimal(reference_price),
            trigger_reference_source=evaluation.reference_source,
            reference_extension_price=canonical_decimal(reference_extension),
            reference_extension_atr=canonical_decimal(reference_extension / atr),
            reference_extension_bps=canonical_decimal(
                reference_extension / trigger_boundary * Decimal("10000")
            ),
            reference_within_max_extension=(reference_extension <= extension),
            indicator_source_digest=evaluation.indicators.source_digest,
            indicator_source_cutoff_at=_datetime_from_ns(
                evaluation.indicators.source_cutoff_ns
            ),
        )

        stop_distance = Decimal(self.parameters.initial_stop_atr_multiple) * atr
        risk_budget = Decimal(evaluation.max_allowed_loss) * Decimal("0.80")
        fee = Decimal(evaluation.taker_fee_rate)
        two_side_cost = reference_price * (fee * 2 + Decimal("0.0010") * 2)
        candidates = (
            risk_budget / (stop_distance + two_side_cost),
            Decimal(evaluation.max_notional) / (reference_price * Decimal("1.0020")),
            Decimal(evaluation.max_margin)
            * Decimal(evaluation.effective_leverage)
            / (reference_price * Decimal("1.0020")),
            Decimal(evaluation.rules.max_market_quantity),
        )
        quantity = _floor_to_step(min(candidates), Decimal(evaluation.rules.step_size))
        if quantity < Decimal(evaluation.rules.step_size) * 2:
            return StrategyEvaluation(
                reason_code="QUANTITY_BELOW_TAKE_PROFIT_SPLIT_MINIMUM"
            )
        if quantity < Decimal(evaluation.rules.min_quantity):
            return StrategyEvaluation(reason_code="QUANTITY_BELOW_MINIMUM")
        if quantity * reference_price < Decimal(evaluation.rules.min_notional):
            return StrategyEvaluation(reason_code="NOTIONAL_BELOW_MINIMUM")

        proposal_fields = {
            "strategy_id": self.strategy_id,
            "activation_id": evaluation.activation_id,
            "rule_id": (
                "DEMO_ORDER_FLOW_CHECK"
                if self.parameters.demo_immediate_entry
                else "ENTRY_BREAKOUT"
            ),
            "source_identity": evaluation.source_identity,
            "source_cutoff": evaluation.source_cutoff,
            "input_digest": evaluation.input_digest,
            "instrument_id": evaluation.instrument_id,
            "direction": self.parameters.direction,
            "action_profile": "ENTRY_MARKET",
            "risk_direction": RiskDirection.INCREASE,
            "quantity": canonical_decimal(quantity),
            "reference_price": canonical_decimal(reference_price),
            "reference_source": evaluation.reference_source,
            "reason_code": (
                "DEMO_ORDER_FLOW_CHECK_REQUESTED"
                if self.parameters.demo_immediate_entry
                else "ENTRY_BREAKOUT_CONFIRMED"
            ),
            "valid_until": evaluation.valid_until,
            "entry_risk_context": EntryRiskContext(
                trigger_atr=canonical_decimal(atr),
                initial_stop_atr_multiple=self.parameters.initial_stop_atr_multiple,
                take_profit_1_r=self.parameters.take_profit_1_r,
                take_profit_1_fraction=self.parameters.take_profit_1_fraction,
                take_profit_2_r=self.parameters.take_profit_2_r,
                max_hold_bars_15m=self.parameters.max_hold_bars_15m,
                indicator_source_digest=evaluation.indicators.source_digest,
                indicator_source_cutoff_ns=evaluation.indicators.source_cutoff_ns,
                quantity_step=evaluation.rules.step_size,
                price_tick_size=evaluation.rules.price_tick_size,
                entry_extension_boundary=canonical_decimal(entry_extension_boundary),
                sizing_taker_fee_rate=evaluation.taker_fee_rate,
                sizing_effective_leverage=evaluation.effective_leverage,
                instrument_rules_digest=content_digest(evaluation.rules),
                setup_evidence=setup_evidence,
            ),
        }
        return StrategyEvaluation(
            reason_code="PROPOSAL_CREATED",
            proposal=StrategyProposal(
                **proposal_fields,
                proposal_digest=content_digest(proposal_fields),
            ),
        )
