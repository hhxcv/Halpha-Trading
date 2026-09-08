"""Pure projection of latest and cumulative scalping results."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from halpha.domain_values import canonical_decimal
from halpha.scalping.models import (
    ScalpAggregate,
    ScalpCycleResult,
    ScalpResultsResponse,
)


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def scalp_cycle_result(row: tuple[Any, ...]) -> ScalpCycleResult:
    lifecycle = str(row[5])
    account_result = row[7] if isinstance(row[7], dict) else None
    raw_result = account_result.get("trade_result") if account_result else None
    if not isinstance(raw_result, dict):
        raw_result = None
    responsibilities = row[9] if len(row) > 9 and isinstance(row[9], dict) else None
    evidence_resolved = bool(
        account_result is not None
        and account_result.get("classification")
        in {
            "NO_EXTERNAL_CHANGE",
            "ATTRIBUTED_FACTS_AVAILABLE",
            "ACCOUNT_FACTS_WITH_EXTERNAL_CLOSURE",
        }
        and account_result.get("missing_refs") == []
        and responsibilities is not None
        and responsibilities.get("execution_action_refs") == []
        and responsibilities.get("unknown_action_refs") == []
    )
    if lifecycle != "COMPLETED":
        status: Literal["OPEN", "RELIABLE", "NO_TRADE", "UNKNOWN"] = "OPEN"
    elif not evidence_resolved or raw_result is None:
        status = "UNKNOWN"
    elif (
        raw_result.get("fill_count") == 0
        and account_result is not None
        and account_result.get("classification") == "NO_EXTERNAL_CHANGE"
    ):
        status = "NO_TRADE"
    else:
        required = (
            _decimal(raw_result.get("gross_pnl")) if raw_result else None,
            _decimal(raw_result.get("commission")) if raw_result else None,
            _decimal(raw_result.get("funding")) if raw_result else None,
            _decimal(raw_result.get("net_pnl")) if raw_result else None,
        )
        status = (
            "RELIABLE"
            if raw_result is not None
            and raw_result.get("calculation_complete") is True
            and raw_result.get("execution_cost_complete") is True
            and raw_result.get("closed") is True
            and all(value is not None for value in required)
            and required[1] is not None
            and required[1] >= 0
            else "UNKNOWN"
        )
    reliable = status == "RELIABLE" and raw_result is not None
    return ScalpCycleResult(
        cycle_id=str(row[0]),
        activation_id=str(row[1]),
        instrument_ref=str(row[2]),
        direction=str(row[3]),
        triggered_at=row[4],
        lifecycle=lifecycle,
        activation_state_version=int(row[6]),
        result_status=status,
        gross_pnl=str(raw_result.get("gross_pnl")) if reliable else None,
        commission=str(raw_result.get("commission")) if reliable else None,
        funding=str(raw_result.get("funding")) if reliable else None,
        net_pnl=str(raw_result.get("net_pnl")) if reliable else None,
        entry_notional=(
            str(raw_result.get("entry_notional"))
            if reliable and raw_result.get("entry_notional") is not None
            else None
        ),
        holding_duration_seconds=(
            str(raw_result.get("holding_duration_seconds"))
            if reliable and raw_result.get("holding_duration_seconds") is not None
            else None
        ),
        currency=(str(raw_result.get("currency")) if reliable else None),
    )


def summarize_scalp_results(
    *,
    environment_id: str,
    account_ref: str,
    scope: Literal["ALL", "TODAY", "ANCHOR"],
    range_start: datetime | None,
    range_end: datetime,
    rows: tuple[tuple[Any, ...], ...],
    latest_row: tuple[Any, ...] | None,
) -> ScalpResultsResponse:
    results = tuple(scalp_cycle_result(row) for row in rows)
    reliable = tuple(item for item in results if item.result_status == "RELIABLE")
    pnl_values = tuple(Decimal(item.net_pnl or "0") for item in reliable)

    def total(field: str) -> str | None:
        if not reliable:
            return None
        return canonical_decimal(
            sum((Decimal(str(getattr(item, field))) for item in reliable), Decimal(0))
        )

    wins = sum(value > 0 for value in pnl_values)
    losses = sum(value < 0 for value in pnl_values)
    flat = sum(value == 0 for value in pnl_values)
    win_rate = (
        canonical_decimal(Decimal(wins) / Decimal(len(reliable)) * Decimal(100))
        if reliable
        else None
    )
    cutoffs = [row[8] for row in rows if isinstance(row[8], datetime)]
    if latest_row is not None and isinstance(latest_row[8], datetime):
        cutoffs.append(latest_row[8])
    fact_cutoff = max(cutoffs) if cutoffs else range_end
    return ScalpResultsResponse(
        environment_id=environment_id,
        account_ref=account_ref,
        scope=scope,
        range_start=range_start,
        range_end=range_end,
        fact_cutoff=fact_cutoff,
        latest_cycle=(
            scalp_cycle_result(latest_row) if latest_row is not None else None
        ),
        aggregate=ScalpAggregate(
            cycle_count=len(results),
            open_cycle_count=sum(item.result_status == "OPEN" for item in results),
            no_trade_cycle_count=sum(
                item.result_status == "NO_TRADE" for item in results
            ),
            unknown_result_count=sum(
                item.result_status == "UNKNOWN" for item in results
            ),
            reliable_trade_count=len(reliable),
            win_count=wins,
            loss_count=losses,
            flat_count=flat,
            gross_pnl=total("gross_pnl"),
            commission=total("commission"),
            funding=total("funding"),
            net_pnl=total("net_pnl"),
            win_rate=win_rate,
        ),
    )
