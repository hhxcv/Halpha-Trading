"""Restart-safe account discipline derived from facts and plan history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from psycopg import Connection

from halpha.binance_contracts import (
    BINANCE_USDM_ACCOUNT_SNAPSHOT_SCHEMA,
    parse_complete_binance_usdm_account_snapshot,
)
from halpha.capital.models import (
    AccountEquitySnapshot,
    AccountPositionRisk,
    NewRiskAttempt,
    NewRiskDisciplinePolicy,
    NewRiskDisciplineStatus,
    NewRiskResult,
)
from halpha.domain_values import canonical_decimal


_OWNER_TIMEZONE = ZoneInfo("Asia/Shanghai")
_MAX_FUTURE_SKEW_SECONDS = 5
_USDT_PERPETUAL_INSTRUMENT_REF = re.compile(r"^[A-Z0-9]+USDT-PERP$")
_USDT_PERPETUAL_CORRELATION_CLUSTER = "CRYPTO_USDM_BETA"


@dataclass(frozen=True)
class _RiskLimits:
    max_plan_loss: Decimal
    open_risk: Decimal
    gross: Decimal
    instrument: Decimal
    correlated: Decimal
    daily_loss: Decimal
    weekly_loss: Decimal


@dataclass(frozen=True)
class _ProposedRisk:
    present: bool
    loss: Decimal
    notional: Decimal
    instrument_ref: str | None
    direction: str | None
    cluster: str | None
    blocker_codes: tuple[str, ...]


@dataclass(frozen=True)
class _ExposureState:
    gross_before: Decimal
    gross_after: Decimal
    instrument_before: Decimal
    instrument_after: Decimal
    correlated_before: Decimal
    correlated_after: Decimal
    unknown_instruments: tuple[str, ...]


@dataclass(frozen=True)
class _LossState:
    daily_loss: Decimal
    weekly_loss: Decimal
    peak_equity: Decimal
    drawdown: Decimal
    drawdown_fraction: Decimal


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _calendar_window_starts(observed_at: datetime) -> tuple[datetime, datetime]:
    if observed_at.utcoffset() is None:
        raise ValueError("NEW_RISK_DISCIPLINE_TIMEZONE_REQUIRED")
    local = observed_at.astimezone(_OWNER_TIMEZONE)
    day_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_local.astimezone(UTC), (
        day_local - timedelta(days=day_local.weekday())
    ).astimezone(UTC)


def _decimal(value: str | None) -> Decimal | None:
    try:
        result = Decimal(value) if value is not None else None
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result is not None and result.is_finite() else None


def _equity(snapshot: AccountEquitySnapshot) -> Decimal:
    return min(Decimal(snapshot.wallet_balance), Decimal(snapshot.margin_balance))


def _cluster(instrument_ref: str) -> str | None:
    """Keep every workbench-supported USDT perpetual in one conservative bucket.

    The contract catalog remains the authority for whether a symbol is currently
    tradeable.  This evaluator only assigns the common portfolio-risk bucket to
    the normalized instrument form used by that catalog, so a malformed or
    out-of-scope account fact still fails closed instead of silently bypassing
    correlated-exposure checks.
    """

    if _USDT_PERPETUAL_INSTRUMENT_REF.fullmatch(instrument_ref):
        return _USDT_PERPETUAL_CORRELATION_CLUSTER
    return None


def _risk_limits(
    *,
    policy: NewRiskDisciplinePolicy,
    risk_equity: Decimal,
) -> _RiskLimits:
    base = max(Decimal(0), risk_equity)
    return _RiskLimits(
        max_plan_loss=base * Decimal(policy.max_plan_loss_fraction),
        open_risk=base * Decimal(policy.max_open_risk_fraction),
        gross=base * Decimal(policy.max_gross_exposure_fraction),
        instrument=base * Decimal(policy.max_instrument_exposure_fraction),
        correlated=base * Decimal(policy.max_correlated_exposure_fraction),
        daily_loss=base * Decimal(policy.daily_loss_stop_fraction),
        weekly_loss=base * Decimal(policy.weekly_loss_stop_fraction),
    )


def _proposed_risk(
    *,
    max_allowed_loss: str | None,
    max_notional: str | None,
    instrument_ref: str | None,
    direction: str | None,
) -> _ProposedRisk:
    loss = _decimal(max_allowed_loss)
    notional = _decimal(max_notional)
    present = any(
        value is not None
        for value in (max_allowed_loss, max_notional, instrument_ref, direction)
    )
    cluster = _cluster(instrument_ref) if instrument_ref else None
    blockers: list[str] = []
    if present:
        if loss is None or loss <= 0:
            blockers.append("NEW_RISK_PROPOSED_LOSS_INVALID")
        if notional is None or notional <= 0:
            blockers.append("NEW_RISK_PROPOSED_NOTIONAL_INVALID")
        if not instrument_ref:
            blockers.append("NEW_RISK_PROPOSED_INSTRUMENT_INVALID")
        elif cluster is None:
            blockers.append("NEW_RISK_CORRELATION_CLUSTER_UNKNOWN")
        if direction not in {"LONG", "SHORT"}:
            blockers.append("NEW_RISK_PROPOSED_DIRECTION_INVALID")
    return _ProposedRisk(
        present=present,
        loss=loss if loss is not None and loss > 0 else Decimal(0),
        notional=notional if notional is not None and notional > 0 else Decimal(0),
        instrument_ref=instrument_ref,
        direction=direction,
        cluster=cluster,
        blocker_codes=tuple(blockers),
    )


def _overlay_exposure(
    actual: dict[str, Decimal],
    planned: dict[str, Decimal],
) -> dict[str, Decimal]:
    return {
        instrument: max(
            actual.get(instrument, Decimal(0)),
            planned.get(instrument, Decimal(0)),
        )
        for instrument in set(actual) | set(planned)
    }


def _cluster_totals(values: dict[str, Decimal]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for instrument, amount in values.items():
        cluster = _cluster(instrument)
        if cluster is not None:
            totals[cluster] = totals.get(cluster, Decimal(0)) + amount
    return totals


def _exposure_state(
    *,
    positions: tuple[AccountPositionRisk, ...],
    attempts: tuple[NewRiskAttempt, ...],
    proposed: _ProposedRisk,
) -> _ExposureState:
    planned: dict[str, Decimal] = {}
    for attempt in attempts:
        planned[attempt.instrument_ref] = planned.get(
            attempt.instrument_ref,
            Decimal(0),
        ) + Decimal(attempt.max_notional)
    actual: dict[str, Decimal] = {}
    for position in positions:
        actual[position.instrument_ref] = actual.get(
            position.instrument_ref,
            Decimal(0),
        ) + abs(Decimal(position.notional))

    before = _overlay_exposure(actual, planned)
    planned_after = dict(planned)
    if proposed.instrument_ref:
        planned_after[proposed.instrument_ref] = (
            planned_after.get(
                proposed.instrument_ref,
                Decimal(0),
            )
            + proposed.notional
        )
    after = _overlay_exposure(actual, planned_after)
    before_clusters = _cluster_totals(before)
    after_clusters = _cluster_totals(after)
    unknown_instruments = tuple(
        sorted(instrument for instrument in before if _cluster(instrument) is None)
    )

    if proposed.instrument_ref:
        instrument_before = before.get(proposed.instrument_ref, Decimal(0))
        instrument_after = after.get(proposed.instrument_ref, Decimal(0))
        correlated_before = before_clusters.get(proposed.cluster or "", Decimal(0))
    else:
        instrument_before = max(before.values(), default=Decimal(0))
        instrument_after = Decimal(0)
        correlated_before = max(before_clusters.values(), default=Decimal(0))
    correlated_after = (
        after_clusters.get(proposed.cluster, Decimal(0))
        if proposed.cluster is not None
        else Decimal(0)
    )
    return _ExposureState(
        gross_before=sum(before.values(), Decimal(0)),
        gross_after=sum(after.values(), Decimal(0))
        + (proposed.notional if not proposed.instrument_ref else Decimal(0)),
        instrument_before=instrument_before,
        instrument_after=instrument_after,
        correlated_before=correlated_before,
        correlated_after=correlated_after,
        unknown_instruments=unknown_instruments,
    )


def _available_notional_capacity(
    *,
    positions: tuple[AccountPositionRisk, ...],
    attempts: tuple[NewRiskAttempt, ...],
    limits: _RiskLimits,
    exposure: _ExposureState,
    instrument_ref: str | None,
) -> Decimal | None:
    """Project remaining target-instrument nominal exposure without reserving it.

    The result deliberately covers only the exposure portion of the account
    discipline.  A caller must still apply the compiled schedule's loss budget
    and the activation / submission-time CAP checks.
    """
    if instrument_ref is None:
        return None
    cluster = _cluster(instrument_ref)
    if cluster is None:
        return None

    planned: dict[str, Decimal] = {}
    for attempt in attempts:
        planned[attempt.instrument_ref] = planned.get(
            attempt.instrument_ref, Decimal(0)
        ) + Decimal(attempt.max_notional)
    actual: dict[str, Decimal] = {}
    for position in positions:
        actual[position.instrument_ref] = actual.get(
            position.instrument_ref, Decimal(0)
        ) + abs(Decimal(position.notional))

    planned_target = planned.get(instrument_ref, Decimal(0))
    target_before = max(actual.get(instrument_ref, Decimal(0)), planned_target)
    gross_other = exposure.gross_before - target_before
    cluster_before = sum(
        amount
        for current_instrument, amount in _overlay_exposure(actual, planned).items()
        if _cluster(current_instrument) == cluster
    )
    cluster_other = cluster_before - target_before
    candidates = (
        limits.gross - gross_other - planned_target,
        limits.instrument - planned_target,
        limits.correlated - cluster_other - planned_target,
    )
    return max(Decimal(0), min(candidates))


def _loss_state(
    *,
    results: tuple[NewRiskResult, ...],
    day_start: datetime,
    week_start: datetime,
    risk_equity: Decimal,
    rolling_peak_equity: str | None,
) -> _LossState:
    daily_loss = -sum(
        (
            min(Decimal(0), Decimal(item.net_pnl))
            for item in results
            if item.closed_at.astimezone(UTC) >= day_start
        ),
        Decimal(0),
    )
    weekly_loss = -sum(
        (
            min(Decimal(0), Decimal(item.net_pnl))
            for item in results
            if item.closed_at.astimezone(UTC) >= week_start
        ),
        Decimal(0),
    )
    peak_equity = max(_decimal(rolling_peak_equity) or risk_equity, risk_equity)
    drawdown = max(Decimal(0), peak_equity - risk_equity)
    return _LossState(
        daily_loss=daily_loss,
        weekly_loss=weekly_loss,
        peak_equity=peak_equity,
        drawdown=drawdown,
        drawdown_fraction=(drawdown / peak_equity if peak_equity > 0 else Decimal(0)),
    )


def _proposal_limit_blockers(
    *,
    proposed: _ProposedRisk,
    planned_risk: Decimal,
    limits: _RiskLimits,
    exposure: _ExposureState,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if proposed.loss > limits.max_plan_loss:
        blockers.append("NEW_RISK_PLAN_LOSS_LIMIT_EXCEEDED")
    if planned_risk + proposed.loss > limits.open_risk:
        blockers.append("NEW_RISK_OPEN_RISK_LIMIT_EXCEEDED")
    if exposure.gross_after > limits.gross:
        blockers.append("NEW_RISK_GROSS_EXPOSURE_LIMIT_EXCEEDED")
    if exposure.instrument_after > limits.instrument:
        blockers.append("NEW_RISK_INSTRUMENT_EXPOSURE_LIMIT_EXCEEDED")
    if exposure.correlated_after > limits.correlated:
        blockers.append("NEW_RISK_CORRELATED_EXPOSURE_LIMIT_EXCEEDED")
    return tuple(blockers)


def _existing_risk_limit_blockers(
    *,
    planned_risk: Decimal,
    limits: _RiskLimits,
    exposure: _ExposureState,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if planned_risk > limits.open_risk:
        blockers.append("NEW_RISK_OPEN_RISK_LIMIT_EXCEEDED")
    if exposure.gross_before > limits.gross:
        blockers.append("NEW_RISK_GROSS_EXPOSURE_LIMIT_EXCEEDED")
    if exposure.instrument_before > limits.instrument:
        blockers.append("NEW_RISK_INSTRUMENT_EXPOSURE_LIMIT_EXCEEDED")
    if exposure.correlated_before > limits.correlated:
        blockers.append("NEW_RISK_CORRELATED_EXPOSURE_LIMIT_EXCEEDED")
    return tuple(blockers)


def _account_and_history_blockers(
    *,
    can_trade: bool,
    risk_equity: Decimal,
    limits: _RiskLimits,
    losses: _LossState,
    policy: NewRiskDisciplinePolicy,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if not can_trade:
        blockers.append("ACCOUNT_TRADING_DISABLED")
    if risk_equity <= 0:
        blockers.append("ACCOUNT_RISK_EQUITY_NOT_POSITIVE")
    if losses.daily_loss >= limits.daily_loss:
        blockers.append("NEW_RISK_DAILY_LOSS_STOP_REACHED")
    if losses.weekly_loss >= limits.weekly_loss:
        blockers.append("NEW_RISK_WEEKLY_LOSS_STOP_REACHED")
    if losses.drawdown_fraction >= Decimal(policy.rolling_drawdown_stop_fraction):
        blockers.append("NEW_RISK_ROLLING_DRAWDOWN_STOP_REACHED")
    return tuple(blockers)


def _losing_position_blockers(
    *,
    positions: tuple[AccountPositionRisk, ...],
    instrument_ref: str | None,
    direction: str | None,
    frozen_scale_in: bool,
) -> tuple[str, ...]:
    if frozen_scale_in:
        return ()
    if _losing_same_direction_position_exists(
        positions,
        instrument_ref=instrument_ref,
        direction=direction,
    ):
        return ("NEW_RISK_LOSING_POSITION_ADD_PROHIBITED",)
    return ()


def _discipline_status(
    blockers: list[str],
) -> Literal["ALLOWED", "BLOCKED", "UNKNOWN"]:
    if any(
        code.startswith("ACCOUNT_EQUITY_")
        or code.startswith("NEW_RISK_PROPOSED_")
        or code == "NEW_RISK_ENTRY_DIRECTION_INVALID"
        or code == "NEW_RISK_CORRELATION_CLUSTER_UNKNOWN"
        for code in blockers
    ):
        return "UNKNOWN"
    return "BLOCKED" if blockers else "ALLOWED"


def _losing_same_direction_position_exists(
    positions: tuple[AccountPositionRisk, ...],
    *,
    instrument_ref: str | None,
    direction: str | None,
) -> bool:
    """Return whether the proposed entry would add to a current floating loss."""

    if instrument_ref is None or direction not in {"LONG", "SHORT"}:
        return False
    return any(
        position.instrument_ref == instrument_ref
        and position.direction == direction
        and Decimal(position.notional) > 0
        and Decimal(position.unrealized_pnl) < 0
        for position in positions
    )


def _status_unknown(
    *, policy: NewRiskDisciplinePolicy, observed_at: datetime, blocker: str
) -> NewRiskDisciplineStatus:
    day_start, week_start = _calendar_window_starts(observed_at)
    return NewRiskDisciplineStatus(
        status="UNKNOWN",
        new_risk_allowed=False,
        blocker_codes=(blocker,),
        account_snapshot_ref=None,
        account_snapshot_cutoff=None,
        risk_equity=None,
        max_plan_loss=None,
        minimum_reward_risk_ratio=policy.minimum_reward_risk_ratio,
        available_notional_capacity=None,
        open_risk_limit=None,
        open_risk_committed=None,
        open_risk_after_proposal=None,
        gross_exposure_limit=None,
        gross_exposure=None,
        gross_exposure_after_proposal=None,
        instrument_ref=None,
        instrument_exposure_limit=None,
        instrument_exposure=None,
        instrument_exposure_after_proposal=None,
        correlation_cluster=None,
        correlated_exposure_limit=None,
        correlated_exposure=None,
        correlated_exposure_after_proposal=None,
        daily_loss_limit=None,
        daily_loss_measure=None,
        weekly_loss_limit=None,
        weekly_loss_measure=None,
        rolling_peak_equity=None,
        rolling_drawdown=None,
        rolling_drawdown_fraction=None,
        rolling_drawdown_limit_fraction=policy.rolling_drawdown_stop_fraction,
        rolling_drawdown_lookback_days=policy.rolling_drawdown_lookback_days,
        open_new_risk_activation_count=0,
        day_window_started_at=day_start,
        week_window_started_at=week_start,
        evaluated_at=observed_at.astimezone(UTC),
    )


def evaluate_new_risk_discipline(
    *,
    policy: NewRiskDisciplinePolicy,
    observed_at: datetime,
    account_equity: AccountEquitySnapshot | None,
    attempts: tuple[NewRiskAttempt, ...],
    results: tuple[NewRiskResult, ...] = (),
    rolling_peak_equity: str | None = None,
    proposed_max_allowed_loss: str | None = None,
    proposed_max_notional: str | None = None,
    proposed_instrument_ref: str | None = None,
    proposed_direction: str | None = None,
    entry_instrument_ref: str | None = None,
    entry_direction: str | None = None,
    is_frozen_scale_in: bool = False,
) -> NewRiskDisciplineStatus:
    """Apply one portfolio-risk contract for every trading context.

    Per instrument, the larger of the observed and planned views avoids double
    counting an attributable position. Different instruments remain additive.
    """
    if account_equity is None:
        return _status_unknown(
            policy=policy,
            observed_at=observed_at,
            blocker="ACCOUNT_EQUITY_SNAPSHOT_UNAVAILABLE",
        )
    now = observed_at.astimezone(UTC)
    cutoff = account_equity.cutoff.astimezone(UTC)
    if (now - cutoff).total_seconds() < -_MAX_FUTURE_SKEW_SECONDS:
        return _status_unknown(
            policy=policy,
            observed_at=observed_at,
            blocker="ACCOUNT_EQUITY_SNAPSHOT_TIME_INVALID",
        )
    if (now - cutoff).total_seconds() > policy.account_snapshot_max_age_seconds:
        return _status_unknown(
            policy=policy,
            observed_at=observed_at,
            blocker="ACCOUNT_EQUITY_SNAPSHOT_STALE",
        )

    day_start, week_start = _calendar_window_starts(observed_at)
    risk_equity = _equity(account_equity)
    limits = _risk_limits(policy=policy, risk_equity=risk_equity)
    proposed = _proposed_risk(
        max_allowed_loss=proposed_max_allowed_loss,
        max_notional=proposed_max_notional,
        instrument_ref=proposed_instrument_ref,
        direction=proposed_direction,
    )
    exposure_target = proposed if proposed.instrument_ref else _ProposedRisk(
        present=False,
        loss=Decimal(0),
        notional=Decimal(0),
        instrument_ref=entry_instrument_ref,
        direction=entry_direction,
        cluster=_cluster(entry_instrument_ref) if entry_instrument_ref else None,
        blocker_codes=(),
    )

    open_attempts = tuple(item for item in attempts if item.lifecycle != "COMPLETED")
    planned_risk = sum(
        (Decimal(item.max_allowed_loss) for item in open_attempts), Decimal(0)
    )
    exposure = _exposure_state(
        positions=account_equity.positions,
        attempts=open_attempts,
        proposed=exposure_target,
    )
    losses = _loss_state(
        results=results,
        day_start=day_start,
        week_start=week_start,
        risk_equity=risk_equity,
        rolling_peak_equity=rolling_peak_equity,
    )
    blockers = list(proposed.blocker_codes)
    if entry_direction is not None and entry_direction not in {"LONG", "SHORT"}:
        blockers.append("NEW_RISK_ENTRY_DIRECTION_INVALID")
    if entry_instrument_ref is not None and entry_direction is None:
        blockers.append("NEW_RISK_ENTRY_DIRECTION_INVALID")
    if entry_instrument_ref is not None and _cluster(entry_instrument_ref) is None:
        blockers.append("NEW_RISK_CORRELATION_CLUSTER_UNKNOWN")
    if exposure.unknown_instruments:
        blockers.append("NEW_RISK_CORRELATION_CLUSTER_UNKNOWN")
    blockers.extend(
        _account_and_history_blockers(
            can_trade=account_equity.can_trade,
            risk_equity=risk_equity,
            limits=limits,
            losses=losses,
            policy=policy,
        )
    )
    blockers.extend(
        _proposal_limit_blockers(
            proposed=proposed,
            planned_risk=planned_risk,
            limits=limits,
            exposure=exposure,
        )
        if proposed.present
        else _existing_risk_limit_blockers(
            planned_risk=planned_risk,
            limits=limits,
            exposure=exposure,
        )
    )
    blockers.extend(
        _losing_position_blockers(
            positions=account_equity.positions,
            instrument_ref=entry_instrument_ref or proposed.instrument_ref,
            direction=entry_direction or proposed.direction,
            frozen_scale_in=is_frozen_scale_in and not proposed.present,
        )
    )
    capacity_instrument = proposed.instrument_ref or entry_instrument_ref
    available_notional_capacity = (
        _available_notional_capacity(
            positions=account_equity.positions,
            attempts=open_attempts,
            limits=limits,
            exposure=exposure,
            instrument_ref=capacity_instrument,
        )
        if not blockers
        else None
    )
    return NewRiskDisciplineStatus(
        status=_discipline_status(blockers),
        new_risk_allowed=not blockers,
        blocker_codes=tuple(dict.fromkeys(blockers)),
        account_snapshot_ref=account_equity.fact_ref,
        account_snapshot_cutoff=cutoff,
        risk_equity=canonical_decimal(risk_equity),
        max_plan_loss=canonical_decimal(limits.max_plan_loss),
        minimum_reward_risk_ratio=policy.minimum_reward_risk_ratio,
        available_notional_capacity=(
            canonical_decimal(available_notional_capacity)
            if available_notional_capacity is not None
            else None
        ),
        open_risk_limit=canonical_decimal(limits.open_risk),
        open_risk_committed=canonical_decimal(planned_risk),
        open_risk_after_proposal=canonical_decimal(planned_risk + proposed.loss),
        gross_exposure_limit=canonical_decimal(limits.gross),
        gross_exposure=canonical_decimal(exposure.gross_before),
        gross_exposure_after_proposal=canonical_decimal(exposure.gross_after),
        instrument_ref=proposed.instrument_ref or entry_instrument_ref,
        instrument_exposure_limit=canonical_decimal(limits.instrument),
        instrument_exposure=canonical_decimal(exposure.instrument_before),
        instrument_exposure_after_proposal=canonical_decimal(exposure.instrument_after),
        correlation_cluster=exposure_target.cluster,
        correlated_exposure_limit=canonical_decimal(limits.correlated),
        correlated_exposure=canonical_decimal(exposure.correlated_before),
        correlated_exposure_after_proposal=canonical_decimal(exposure.correlated_after),
        daily_loss_limit=canonical_decimal(limits.daily_loss),
        daily_loss_measure=canonical_decimal(losses.daily_loss),
        weekly_loss_limit=canonical_decimal(limits.weekly_loss),
        weekly_loss_measure=canonical_decimal(losses.weekly_loss),
        rolling_peak_equity=canonical_decimal(losses.peak_equity),
        rolling_drawdown=canonical_decimal(losses.drawdown),
        rolling_drawdown_fraction=canonical_decimal(losses.drawdown_fraction),
        rolling_drawdown_limit_fraction=policy.rolling_drawdown_stop_fraction,
        rolling_drawdown_lookback_days=policy.rolling_drawdown_lookback_days,
        open_new_risk_activation_count=len(open_attempts),
        day_window_started_at=day_start,
        week_window_started_at=week_start,
        evaluated_at=now,
    )


def _account_equity_from_fact(
    row: tuple[Any, ...] | None,
) -> AccountEquitySnapshot | None:
    if row is None:
        return None
    fact_ref, cutoff, payload = row
    if not isinstance(cutoff, datetime):
        return None
    snapshot = parse_complete_binance_usdm_account_snapshot(payload)
    if snapshot is None or snapshot["schema"] != BINANCE_USDM_ACCOUNT_SNAPSHOT_SCHEMA:
        return None
    summary = snapshot.get("account_summary")
    if not isinstance(summary, dict):
        return None
    can_trade = summary.get("can_trade")
    if not isinstance(can_trade, bool):
        return None
    positions = snapshot["positions"]
    try:
        return AccountEquitySnapshot(
            fact_ref=str(fact_ref),
            cutoff=_aware_utc(cutoff),
            can_trade=can_trade,
            wallet_balance=str(summary["wallet_balance"]),
            unrealized_pnl=str(summary["unrealized_pnl"]),
            margin_balance=str(summary["margin_balance"]),
            available_balance=str(summary["available_balance"]),
            positions=tuple(
                AccountPositionRisk(
                    instrument_ref=str(item["instrument_ref"]),
                    direction=cast(
                        Literal["LONG", "SHORT"],
                        item["direction"],
                    ),
                    notional=str(item["notional"]),
                    unrealized_pnl=str(item["unrealized_pnl"]),
                )
                for item in positions
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _reliable_discipline_result(row: Any) -> NewRiskResult | None:
    (
        activation_id, review_status, workflow_kind,
        account_result, responsibilities, fact_cutoff,
    ) = row
    if review_status != "COMPLETE" and workflow_kind != "SCALP_CYCLE":
        return None
    if (
        not isinstance(account_result, dict)
        or account_result.get("classification")
        not in {"ATTRIBUTED_FACTS_AVAILABLE", "ACCOUNT_FACTS_WITH_EXTERNAL_CLOSURE"}
        or account_result.get("missing_refs") != []
        or not isinstance(responsibilities, dict)
        or responsibilities.get("execution_action_refs") != []
        or responsibilities.get("unknown_action_refs") != []
    ):
        return None
    trade_result = account_result.get("trade_result")
    if not isinstance(trade_result, dict) or any(
        trade_result.get(field) is not True
        for field in (
            "calculation_complete",
            "execution_cost_complete",
            "closed",
        )
    ):
        return None
    net_pnl = _decimal(trade_result.get("net_pnl"))
    commission = _decimal(trade_result.get("commission"))
    if (
        net_pnl is None
        or commission is None
        or commission < 0
        or _decimal(trade_result.get("gross_pnl")) is None
        or _decimal(trade_result.get("funding")) is None
    ):
        return None
    return NewRiskResult(
        activation_id=str(activation_id),
        net_pnl=canonical_decimal(net_pnl),
        closed_at=_aware_utc(fact_cutoff),
    )


def read_new_risk_discipline(
    connection: Connection[Any],
    *,
    environment_id: str,
    account_ref: str,
    policy: NewRiskDisciplinePolicy,
    observed_at: datetime,
    proposed_max_allowed_loss: str | None = None,
    proposed_max_notional: str | None = None,
    proposed_instrument_ref: str | None = None,
    proposed_direction: str | None = None,
    entry_instrument_ref: str | None = None,
    entry_direction: str | None = None,
    is_frozen_scale_in: bool = False,
    lock: bool = False,
) -> NewRiskDisciplineStatus:
    if lock:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"{environment_id}:{account_ref}:new-risk-discipline",),
        )
    now = observed_at.astimezone(UTC)
    day_start, week_start = _calendar_window_starts(observed_at)
    rolling_start = now - timedelta(days=policy.rolling_drawdown_lookback_days)
    snapshot_row = connection.execute(
        """SELECT venue_fact_id, cutoff, payload FROM halpha.venue_fact WHERE environment_id = %s AND account_ref = %s AND kind = 'ACCOUNT_STATE' AND source_class = 'VENUE_QUERY' ORDER BY cutoff DESC, received_at DESC, venue_fact_id DESC LIMIT 1""",
        (environment_id, account_ref),
    ).fetchone()
    attempts_rows = connection.execute(
        """SELECT activation.activation_id, version.instrument_ref, version.max_notional, version.max_allowed_loss, activation.lifecycle, activation.has_entry_fill, activation.created_at FROM halpha.plan_activation activation JOIN halpha.trade_plan_version version ON version.environment_id = activation.environment_id AND version.plan_version_id = activation.plan_version_ref WHERE activation.environment_id = %s AND activation.account_ref = %s AND activation.position_alignment IS NULL""",
        (environment_id, account_ref),
    ).fetchall()
    result_rows = connection.execute(
        """
        SELECT DISTINCT ON (review.activation_id)
               review.activation_id, review.status,
               version.terms ->> 'workflow_kind',
               review.account_result, review.open_responsibilities,
               review.fact_cutoff
        FROM halpha.review review
        JOIN halpha.plan_activation activation
          ON activation.environment_id = review.environment_id
         AND activation.activation_id = review.activation_id
        JOIN halpha.trade_plan_version version
          ON version.environment_id = activation.environment_id
         AND version.plan_version_id = activation.plan_version_ref
        WHERE review.environment_id = %s
          AND activation.account_ref = %s
          AND activation.position_alignment IS NULL
        ORDER BY review.activation_id, review.review_version DESC
        """,
        (environment_id, account_ref),
    ).fetchall()
    peak_rows = connection.execute(
        """SELECT venue_fact_id, cutoff, payload FROM halpha.venue_fact WHERE environment_id = %s AND account_ref = %s AND kind = 'ACCOUNT_STATE' AND source_class = 'VENUE_QUERY' AND cutoff >= %s AND cutoff <= %s AND payload ->> 'schema' = %s ORDER BY cutoff DESC, received_at DESC, venue_fact_id DESC""",
        (
            environment_id,
            account_ref,
            rolling_start,
            now,
            BINANCE_USDM_ACCOUNT_SNAPSHOT_SCHEMA,
        ),
    ).fetchall()
    attempts = tuple(
        NewRiskAttempt(
            activation_id=str(row[0]),
            instrument_ref=str(row[1]),
            max_notional=str(row[2]),
            max_allowed_loss=str(row[3]),
            lifecycle=str(row[4]),
            has_entry_fill=bool(row[5]),
            created_at=_aware_utc(row[6]),
        )
        for row in attempts_rows
    )
    # Select the latest authority before checking reliability, so later unknown
    # facts cannot fall back to an older completed review. Scalp cycles retain
    # their machine-derived DRAFT review without requiring an owner evaluation.
    results = tuple(
        result
        for row in result_rows
        if (result := _reliable_discipline_result(row)) is not None
    )
    peaks = tuple(
        _equity(item)
        for row in peak_rows
        if (item := _account_equity_from_fact(row)) is not None
    )
    return evaluate_new_risk_discipline(
        policy=policy,
        observed_at=observed_at,
        account_equity=_account_equity_from_fact(snapshot_row),
        attempts=attempts,
        results=results,
        rolling_peak_equity=canonical_decimal(max(peaks)) if peaks else None,
        proposed_max_allowed_loss=proposed_max_allowed_loss,
        proposed_max_notional=proposed_max_notional,
        proposed_instrument_ref=proposed_instrument_ref,
        proposed_direction=proposed_direction,
        entry_instrument_ref=entry_instrument_ref,
        entry_direction=entry_direction,
        is_frozen_scale_in=is_frozen_scale_in,
    )
