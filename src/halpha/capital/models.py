"""Immutable inputs and results for stateless capital checks."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from halpha.domain_values import canonical_decimal, decimal_from_string


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
ACCOUNT_SYSTEM_STOP_RELEASE_EVIDENCE_MAX_AGE = timedelta(seconds=65)
ACCOUNT_PORTFOLIO_RISK_POLICY_VERSION = "ACCOUNT_PORTFOLIO_RISK@2"
MAX_PLAN_LOSS_FRACTION = "0.0075"
MAX_OPEN_RISK_FRACTION = "0.025"
MAX_GROSS_EXPOSURE_FRACTION = "2"
MAX_INSTRUMENT_EXPOSURE_FRACTION = "1"
MAX_CORRELATED_EXPOSURE_FRACTION = "1.5"
DAILY_LOSS_STOP_FRACTION = "0.015"
WEEKLY_LOSS_STOP_FRACTION = "0.04"
ROLLING_DRAWDOWN_STOP_FRACTION = "0.1"
ROLLING_DRAWDOWN_LOOKBACK_DAYS = 30


class EnvironmentKind(StrEnum):
    DEMO = "DEMO"
    LIVE = "LIVE"


class AuthorityClass(StrEnum):
    DEMO_VALIDATION = "DEMO_VALIDATION"
    LIVE_REAL_CAPITAL = "LIVE_REAL_CAPITAL"
    NO_TRADING_AUTHORITY = "NO_TRADING_AUTHORITY"


class RiskClass(StrEnum):
    RISK_INCREASING = "RISK_INCREASING"
    RISK_NEUTRAL = "RISK_NEUTRAL"
    RISK_REDUCING = "RISK_REDUCING"
    AMBIGUOUS = "AMBIGUOUS"


class StopCategory(StrEnum):
    NEW_RISK = "NEW_RISK"
    PROTECTION = "PROTECTION"
    RISK_REDUCTION_OR_ORDER_MANAGEMENT = "RISK_REDUCTION_OR_ORDER_MANAGEMENT"
    ALL_EXCHANGE_CHANGES = "ALL_EXCHANGE_CHANGES"


class AccountSystemStopSource(StrEnum):
    EXTERNAL_ACTIVITY = "SYSTEM_EXTERNAL_ACTIVITY"
    ATTRIBUTED_ACTION_ANOMALY = "SYSTEM_ATTRIBUTED_ACTION_ANOMALY"


class CapModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NewRiskDisciplinePolicy(CapModel):
    """Configuration which can only narrow admission of new-risk plans."""

    max_plan_loss_fraction: str = MAX_PLAN_LOSS_FRACTION
    max_open_risk_fraction: str = MAX_OPEN_RISK_FRACTION
    max_gross_exposure_fraction: str = MAX_GROSS_EXPOSURE_FRACTION
    max_instrument_exposure_fraction: str = MAX_INSTRUMENT_EXPOSURE_FRACTION
    max_correlated_exposure_fraction: str = MAX_CORRELATED_EXPOSURE_FRACTION
    daily_loss_stop_fraction: str = DAILY_LOSS_STOP_FRACTION
    weekly_loss_stop_fraction: str = WEEKLY_LOSS_STOP_FRACTION
    rolling_drawdown_stop_fraction: str = ROLLING_DRAWDOWN_STOP_FRACTION
    rolling_drawdown_lookback_days: int = Field(
        default=ROLLING_DRAWDOWN_LOOKBACK_DAYS, ge=1, le=365
    )
    account_snapshot_max_age_seconds: int = Field(default=65, ge=5, le=300)

    @field_validator(
        "max_plan_loss_fraction",
        "max_open_risk_fraction",
        "daily_loss_stop_fraction",
        "weekly_loss_stop_fraction",
        "rolling_drawdown_stop_fraction",
    )
    @classmethod
    def fractions_are_bounded(cls, value: str) -> str:
        normalized = canonical_decimal(
            decimal_from_string(
                value,
                code="NEW_RISK_DISCIPLINE_FRACTION_INVALID",
                positive=True,
            )
        )
        if (
            decimal_from_string(
                normalized,
                code="NEW_RISK_DISCIPLINE_FRACTION_INVALID",
            )
            > 1
        ):
            raise ValueError("NEW_RISK_DISCIPLINE_FRACTION_INVALID")
        return normalized

    @field_validator(
        "max_gross_exposure_fraction",
        "max_instrument_exposure_fraction",
        "max_correlated_exposure_fraction",
    )
    @classmethod
    def exposure_fractions_are_bounded(cls, value: str) -> str:
        normalized = canonical_decimal(
            decimal_from_string(
                value,
                code="NEW_RISK_DISCIPLINE_EXPOSURE_FRACTION_INVALID",
                positive=True,
            )
        )
        if (
            decimal_from_string(
                normalized,
                code="NEW_RISK_DISCIPLINE_EXPOSURE_FRACTION_INVALID",
            )
            > 20
        ):
            raise ValueError("NEW_RISK_DISCIPLINE_EXPOSURE_FRACTION_INVALID")
        return normalized

    @model_validator(mode="after")
    def fractions_are_ordered(self) -> "NewRiskDisciplinePolicy":
        plan = decimal_from_string(
            self.max_plan_loss_fraction,
            code="NEW_RISK_DISCIPLINE_FRACTION_INVALID",
        )
        open_risk = decimal_from_string(
            self.max_open_risk_fraction,
            code="NEW_RISK_DISCIPLINE_FRACTION_INVALID",
        )
        daily_loss = decimal_from_string(
            self.daily_loss_stop_fraction,
            code="NEW_RISK_DISCIPLINE_FRACTION_INVALID",
        )
        weekly_loss = decimal_from_string(
            self.weekly_loss_stop_fraction,
            code="NEW_RISK_DISCIPLINE_FRACTION_INVALID",
        )
        drawdown = decimal_from_string(
            self.rolling_drawdown_stop_fraction,
            code="NEW_RISK_DISCIPLINE_FRACTION_INVALID",
        )
        instrument = decimal_from_string(
            self.max_instrument_exposure_fraction,
            code="NEW_RISK_DISCIPLINE_EXPOSURE_FRACTION_INVALID",
        )
        correlated = decimal_from_string(
            self.max_correlated_exposure_fraction,
            code="NEW_RISK_DISCIPLINE_EXPOSURE_FRACTION_INVALID",
        )
        gross = decimal_from_string(
            self.max_gross_exposure_fraction,
            code="NEW_RISK_DISCIPLINE_EXPOSURE_FRACTION_INVALID",
        )
        if not plan <= open_risk:
            raise ValueError("NEW_RISK_DISCIPLINE_FRACTIONS_UNORDERED")
        if not daily_loss <= weekly_loss <= drawdown:
            raise ValueError("NEW_RISK_DISCIPLINE_LOSS_STOPS_UNORDERED")
        if not instrument <= correlated <= gross:
            raise ValueError("NEW_RISK_DISCIPLINE_EXPOSURES_UNORDERED")
        return self


class AccountPositionRisk(CapModel):
    """Risk-view position where direction carries sign and notional is a magnitude."""

    instrument_ref: str
    direction: Literal["LONG", "SHORT"]
    notional: str
    unrealized_pnl: str

    @field_validator("notional")
    @classmethod
    def notional_is_risk_magnitude(cls, value: str) -> str:
        return canonical_decimal(
            abs(
                decimal_from_string(
                    value,
                    code="ACCOUNT_POSITION_NOTIONAL_INVALID",
                )
            )
        )

    @field_validator("unrealized_pnl")
    @classmethod
    def unrealized_pnl_is_finite(cls, value: str) -> str:
        return canonical_decimal(
            decimal_from_string(value, code="ACCOUNT_POSITION_UNREALIZED_PNL_INVALID")
        )


class AccountEquitySnapshot(CapModel):
    fact_ref: str
    cutoff: datetime
    can_trade: bool
    wallet_balance: str
    unrealized_pnl: str
    margin_balance: str
    available_balance: str
    positions: tuple[AccountPositionRisk, ...] = ()

    @field_validator(
        "wallet_balance",
        "unrealized_pnl",
        "margin_balance",
        "available_balance",
    )
    @classmethod
    def balances_are_finite(cls, value: str) -> str:
        return canonical_decimal(
            decimal_from_string(value, code="ACCOUNT_EQUITY_INVALID")
        )

    @field_validator("cutoff")
    @classmethod
    def cutoff_is_aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("ACCOUNT_EQUITY_CUTOFF_INVALID")
        return value


class NewRiskAttempt(CapModel):
    activation_id: str
    instrument_ref: str
    max_notional: str
    max_allowed_loss: str
    lifecycle: str
    has_entry_fill: bool
    created_at: datetime
    first_entry_fill_at: datetime | None = None

    @field_validator("max_notional", "max_allowed_loss")
    @classmethod
    def loss_is_positive(cls, value: str) -> str:
        return canonical_decimal(
            decimal_from_string(
                value,
                code="NEW_RISK_ATTEMPT_LOSS_INVALID",
                positive=True,
            )
        )

    @field_validator("created_at", "first_entry_fill_at")
    @classmethod
    def times_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("NEW_RISK_ATTEMPT_TIME_INVALID")
        return value


class NewRiskResult(CapModel):
    activation_id: str
    net_pnl: str
    closed_at: datetime

    @field_validator("net_pnl")
    @classmethod
    def pnl_is_finite(cls, value: str) -> str:
        return canonical_decimal(
            decimal_from_string(value, code="NEW_RISK_RESULT_PNL_INVALID")
        )

    @field_validator("closed_at")
    @classmethod
    def closed_at_is_aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("NEW_RISK_RESULT_TIME_INVALID")
        return value


class NewRiskDisciplineStatus(CapModel):
    status: Literal["ALLOWED", "BLOCKED", "UNKNOWN"]
    new_risk_allowed: bool
    blocker_codes: tuple[str, ...]
    account_snapshot_ref: str | None
    account_snapshot_cutoff: datetime | None
    risk_equity: str | None
    max_plan_loss: str | None
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
    day_window_started_at: datetime
    week_window_started_at: datetime
    evaluated_at: datetime


class EnvironmentAuthority(CapModel):
    environment_id: str
    environment_kind: EnvironmentKind
    authority_class: AuthorityClass

    @model_validator(mode="after")
    def authority_matches_environment(self) -> EnvironmentAuthority:
        expected = (
            AuthorityClass.DEMO_VALIDATION
            if self.environment_kind is EnvironmentKind.DEMO
            else AuthorityClass.LIVE_REAL_CAPITAL
        )
        if self.authority_class is not expected:
            raise ValueError("AUTHORITY_ENVIRONMENT_MISMATCH")
        return self


class ActivationCapitalBoundary(EnvironmentAuthority):
    """Current plan and activation fields used by one action check."""

    activation_id: str
    account_ref: str
    instrument_ref: str
    valid_from: datetime
    valid_until: datetime
    allowed_actions: frozenset[str]
    max_margin: str
    max_notional: str
    max_allowed_loss: str
    lifecycle: str
    responsibility_owner: str

    @field_validator("max_margin", "max_notional", "max_allowed_loss")
    @classmethod
    def amounts_are_non_negative(cls, value: str) -> str:
        return canonical_decimal(
            decimal_from_string(
                value, code="PLAN_CAPITAL_BOUNDARY_INVALID", non_negative=True
            )
        )

    @model_validator(mode="after")
    def window_is_valid(self) -> "ActivationCapitalBoundary":
        if self.valid_until <= self.valid_from:
            raise ValueError("PLAN_WINDOW_INVALID")
        if not self.allowed_actions:
            raise ValueError("PLAN_ACTIONS_EMPTY")
        return self


class StopStateVersion(EnvironmentAuthority):
    stop_state_version_id: str
    account_ref: str
    activation_id: str | None
    version: int
    stopped_categories: frozenset[StopCategory]
    reason: str
    source: str
    started_at: datetime
    loss_latch_digest: str | None = None
    release_rules: dict[str, Any]
    content_digest: str


class AccountSystemStopReleaseRequest(EnvironmentAuthority):
    new_stop_state_version_id: str
    account_ref: str
    resolution_activation_id: str
    expected_version: int
    expected_stop_content_digest: str
    expected_source: AccountSystemStopSource
    reconciliation_digest: str
    resolution_evidence_digest: str
    reconciliation_observed_at: datetime
    submitted_at: datetime
    resolution_status: Literal["NO_UNRESOLVED_ACCOUNT_STOP_CAUSE"]
    confirmation: Literal["USER_CONFIRMED_SYSTEM_STOP_RELEASE"]

    @field_validator(
        "expected_stop_content_digest",
        "reconciliation_digest",
        "resolution_evidence_digest",
    )
    @classmethod
    def digests_are_sha256(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("SYSTEM_STOP_RELEASE_DIGEST_INVALID")
        return value

    @field_validator("reconciliation_observed_at", "submitted_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("SYSTEM_STOP_RELEASE_TIMEZONE_REQUIRED")
        return value

    @model_validator(mode="after")
    def release_evidence_is_current(self) -> "AccountSystemStopReleaseRequest":
        if self.expected_version <= 0:
            raise ValueError("SYSTEM_STOP_RELEASE_VERSION_INVALID")
        if (
            not self.new_stop_state_version_id
            or not self.account_ref
            or not self.resolution_activation_id
        ):
            raise ValueError("SYSTEM_STOP_RELEASE_IDENTITY_INVALID")
        if (
            self.reconciliation_observed_at > self.submitted_at
            or self.submitted_at - self.reconciliation_observed_at
            > ACCOUNT_SYSTEM_STOP_RELEASE_EVIDENCE_MAX_AGE
        ):
            raise ValueError("SYSTEM_STOP_RELEASE_EVIDENCE_STALE")
        return self


class ActionCheckInput(EnvironmentAuthority):
    activation_id: str
    account_ref: str
    instrument_ref: str
    action_profile: str
    control_category: StopCategory
    risk_class: RiskClass
    checked_at: datetime
    quantized_quantity: str
    conservative_price: str
    economic_action_prior_notional: str = "0"
    activation_current_notional: str = "0"
    account_current_notional: str = "0"
    activation_current_margin: str = "0"
    account_dynamic_available_margin: str
    actual_margin_mode: str
    actual_leverage: str
    post_action_abs_position: str
    current_abs_position: str
    would_reverse_position: bool = False
    facts_fresh: bool = True
    attribution_unambiguous: bool = True

    @field_validator(
        "quantized_quantity",
        "conservative_price",
        "economic_action_prior_notional",
        "activation_current_notional",
        "account_current_notional",
        "activation_current_margin",
        "account_dynamic_available_margin",
        "actual_leverage",
        "post_action_abs_position",
        "current_abs_position",
    )
    @classmethod
    def decimal_inputs_are_non_negative(cls, value: str) -> str:
        return canonical_decimal(
            decimal_from_string(value, code="ACTION_VALUE_INVALID", non_negative=True)
        )


class CapDecision(CapModel):
    accepted: bool
    reason_code: str
    risk_class: RiskClass
    effective_leverage: str | None
    action_notional: str
    economic_action_notional: str
    activation_notional_after: str
    account_notional_after: str
    activation_margin_after: str
    stopped_categories: tuple[StopCategory, ...]
    input_digest: str
    decision_digest: str
