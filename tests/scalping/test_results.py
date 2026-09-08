from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from halpha.scalping.results import scalp_cycle_result, summarize_scalp_results


NOW = datetime(2026, 8, 16, 8, tzinfo=UTC)


def _row(
    cycle_id: str,
    *,
    lifecycle: str = "COMPLETED",
    trade_result: dict[str, object] | None = None,
    triggered_at: datetime = NOW,
    classification: str | None = None,
    unknown_action_refs: tuple[str, ...] = (),
    open_action_refs: tuple[str, ...] = (),
) -> tuple[object, ...]:
    return (
        cycle_id,
        f"activation-{cycle_id}",
        "BTCUSDT-PERP",
        "LONG",
        triggered_at,
        lifecycle,
        3,
        (
            {
                "classification": classification or (
                    "NO_EXTERNAL_CHANGE"
                    if trade_result.get("fill_count") == 0
                    else "ATTRIBUTED_FACTS_AVAILABLE"
                ),
                "missing_refs": [],
                "trade_result": trade_result,
            }
            if trade_result is not None
            else None
        ),
        triggered_at + timedelta(minutes=5),
        {
            "execution_action_refs": list(open_action_refs),
            "unknown_action_refs": list(unknown_action_refs),
        },
    )


def _reliable(net_pnl: str, *, gross_pnl: str, commission: str, funding: str):
    return {
        "fill_count": 4,
        "calculation_complete": True,
        "execution_cost_complete": True,
        "closed": True,
        "gross_pnl": gross_pnl,
        "commission": commission,
        "funding": funding,
        "net_pnl": net_pnl,
        "entry_notional": "100",
        "holding_duration_seconds": "42",
        "currency": "USDT",
    }


def test_cycle_result_never_presents_incomplete_money_as_zero() -> None:
    open_result = scalp_cycle_result(_row("open", lifecycle="RUNNING"))
    unknown_result = scalp_cycle_result(
        _row("unknown", trade_result={"fill_count": 2, "net_pnl": "3"})
    )
    no_trade = scalp_cycle_result(
        _row("none", trade_result={"fill_count": 0})
    )

    assert open_result.result_status == "OPEN"
    assert open_result.net_pnl is None
    assert unknown_result.result_status == "UNKNOWN"
    assert unknown_result.gross_pnl is None
    assert no_trade.result_status == "NO_TRADE"
    assert no_trade.commission is None


def test_summary_aggregates_only_reliable_closed_results() -> None:
    rows = (
        _row(
            "win",
            trade_result=_reliable(
                "8",
                gross_pnl="10",
                commission="1.5",
                funding="-0.5",
            ),
        ),
        _row(
            "loss",
            trade_result=_reliable(
                "-4",
                gross_pnl="-2",
                commission="1.5",
                funding="-0.5",
            ),
            triggered_at=NOW - timedelta(minutes=10),
        ),
        _row("open", lifecycle="RUNNING", triggered_at=NOW - timedelta(minutes=20)),
        _row(
            "none",
            trade_result={"fill_count": 0},
            triggered_at=NOW - timedelta(minutes=30),
        ),
        _row(
            "unknown",
            trade_result={"fill_count": 1, "closed": True},
            triggered_at=NOW - timedelta(minutes=40),
        ),
    )

    result = summarize_scalp_results(
        environment_id="demo-main",
        account_ref="demo-account",
        scope="TODAY",
        range_start=NOW.replace(hour=0),
        range_end=NOW + timedelta(minutes=1),
        rows=rows,
        latest_row=rows[0],
    )

    aggregate = result.aggregate
    assert aggregate.cycle_count == 5
    assert aggregate.reliable_trade_count == 2
    assert aggregate.open_cycle_count == 1
    assert aggregate.no_trade_cycle_count == 1
    assert aggregate.unknown_result_count == 1
    assert aggregate.win_count == 1
    assert aggregate.loss_count == 1
    assert aggregate.win_rate == "50"
    assert aggregate.gross_pnl == "8"
    assert aggregate.commission == "3"
    assert aggregate.funding == "-1"
    assert aggregate.net_pnl == "4"
    assert result.latest_cycle is not None
    assert result.latest_cycle.cycle_id == "win"


@pytest.mark.parametrize(
    "trade_result",
    (
        {"fill_count": 0},
        _reliable("8", gross_pnl="10", commission="1.5", funding="-0.5"),
    ),
)
@pytest.mark.parametrize(
    "unresolved",
    (
        {"classification": "UNKNOWN"},
        {"unknown_action_refs": ("called-entry-handed-over",)},
        {"open_action_refs": ("working-entry-handed-over",)},
    ),
)
def test_handover_unknown_responsibility_cannot_be_no_trade_or_reliable(
    trade_result: dict[str, object],
    unresolved: dict[str, object],
) -> None:
    row = _row("handover", trade_result=trade_result, **unresolved)

    result = summarize_scalp_results(
        environment_id="demo-main",
        account_ref="demo-account",
        scope="ALL",
        range_start=None,
        range_end=NOW,
        rows=(row,),
        latest_row=row,
    )

    assert result.latest_cycle.result_status == "UNKNOWN"
    assert result.latest_cycle.net_pnl is None
    assert result.aggregate.unknown_result_count == 1
    assert result.aggregate.reliable_trade_count == 0
    assert result.aggregate.no_trade_cycle_count == 0
    assert result.aggregate.net_pnl is None


def test_missing_fill_count_is_unknown_instead_of_no_trade() -> None:
    row = _row("missing", trade_result={}, classification="NO_EXTERNAL_CHANGE")

    assert scalp_cycle_result(row).result_status == "UNKNOWN"


def test_reconciled_owner_exit_remains_part_of_the_cycle_economic_result() -> None:
    row = _row(
        "owner-exit",
        trade_result={
            **_reliable("-4", gross_pnl="-2", commission="1.5", funding="-0.5"),
            "strategy_attribution_complete": False,
        },
        classification="ACCOUNT_FACTS_WITH_EXTERNAL_CLOSURE",
    )

    result = scalp_cycle_result(row)

    assert result.result_status == "RELIABLE"
    assert result.net_pnl == "-4"
