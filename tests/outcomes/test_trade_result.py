from datetime import UTC, datetime

import pytest

from halpha.outcomes.trade_result import summarize_trade_result


def _fact(
    kind: str,
    trade_id: str,
    action_ref: str,
    *,
    source_time: object | None = None,
    cutoff: object | None = None,
    **payload: str,
):
    return {
        "kind": kind,
        "action_ref": action_ref,
        "source_time": source_time,
        "cutoff": cutoff,
        "payload": {"trade_id": trade_id, **payload},
    }


def test_closed_long_result_uses_attributed_fills_and_actual_commissions() -> None:
    result = summarize_trade_result(
        direction="LONG",
        action_kinds={"entry": "ENTRY", "exit": "EXIT"},
        facts=(
            _fact(
                "FILL",
                "trade-1",
                "entry",
                source_time=datetime(2026, 7, 20, tzinfo=UTC),
                last_price="64704.60",
                last_quantity="0.0015",
                order_side="BUY",
                liquidity_side="TAKER",
            ),
            _fact(
                "COMMISSION",
                "trade-1",
                "entry",
                amount="0.03882276 USDT",
                currency="USDT",
            ),
            _fact(
                "FILL",
                "trade-2",
                "exit",
                source_time="2026-07-20T00:05:30+00:00",
                last_price="64699.50",
                last_quantity="0.0015",
                order_side="SELL",
                liquidity_side="MAKER",
            ),
            _fact(
                "COMMISSION",
                "trade-2",
                "exit",
                amount="0.03881970 USDT",
                currency="USDT",
            ),
        ),
    )

    assert result["position_quantity"] == "0"
    assert result["average_entry_price"] == "64704.6"
    assert result["average_exit_price"] == "64699.5"
    assert result["entry_notional"] == "97.0569"
    assert result["gross_pnl"] == "-0.00765"
    assert result["commission"] == "0.07764246"
    assert result["net_pnl"] == "-0.08529246"
    assert result["calculation_complete"] is True
    assert result["funding_included"] is False
    assert result["fills"] == [
        {
            "trade_id": "trade-1",
            "action_kind": "ENTRY",
            "price": "64704.6",
            "quantity": "0.0015",
            "notional": "97.0569",
            "order_side": "BUY",
            "liquidity_side": "TAKER",
            "fee": "0.03882276",
            "fee_currency": "USDT",
            "fill_time": "2026-07-20T00:00:00+00:00",
        },
        {
            "trade_id": "trade-2",
            "action_kind": "EXIT",
            "price": "64699.5",
            "quantity": "0.0015",
            "notional": "97.04925",
            "order_side": "SELL",
            "liquidity_side": "MAKER",
            "fee": "0.0388197",
            "fee_currency": "USDT",
            "fill_time": "2026-07-20T00:05:30+00:00",
        },
    ]
    assert result["fill_times_complete"] is True
    assert result["first_fill_time"] == "2026-07-20T00:00:00+00:00"
    assert result["last_fill_time"] == "2026-07-20T00:05:30+00:00"
    assert result["holding_duration_seconds"] == "330"


def test_closed_result_includes_only_activation_allocated_funding() -> None:
    result = summarize_trade_result(
        direction="LONG",
        action_kinds={"entry": "ENTRY", "exit": "EXIT"},
        facts=(
            _fact(
                "FILL",
                "trade-1",
                "entry",
                last_price="100",
                last_quantity="1",
            ),
            _fact(
                "COMMISSION",
                "trade-1",
                "entry",
                amount="0.1",
                currency="USDT",
            ),
            _fact(
                "FILL",
                "trade-2",
                "exit",
                last_price="101",
                last_quantity="1",
            ),
            _fact(
                "COMMISSION",
                "trade-2",
                "exit",
                amount="0.1",
                currency="USDT",
            ),
            {
                "kind": "FUNDING",
                "action_ref": None,
                "source_time": "2026-07-20T08:00:00+00:00",
                "payload": {
                    "record_type": "ACTIVATION_FUNDING_ALLOCATION",
                    "transaction_id": "funding-1",
                    "income": "-0.03",
                    "currency": "USDT",
                },
            },
        ),
    )

    assert result["gross_pnl"] == "1"
    assert result["commission"] == "0.2"
    assert result["funding"] == "-0.03"
    assert result["net_pnl"] == "0.77"
    assert result["funding_included"] is True
    assert result["funding_complete"] is True


def test_open_short_result_exposes_cash_flow_for_current_mark_estimate() -> None:
    result = summarize_trade_result(
        direction="SHORT",
        action_kinds={"entry": "ENTRY"},
        facts=(
            _fact(
                "FILL",
                "trade-1",
                "entry",
                last_price="100",
                last_quantity="2",
            ),
            _fact(
                "COMMISSION",
                "trade-1",
                "entry",
                amount="0.1 USDT",
                currency="USDT",
            ),
        ),
    )

    assert result["position_quantity"] == "-2"
    assert result["fill_cash_flow"] == "200"
    assert result["average_entry_price"] == "100"
    assert result["net_pnl"] is None
    assert result["calculation_complete"] is True


def test_missing_commission_keeps_pnl_unknown() -> None:
    result = summarize_trade_result(
        direction="LONG",
        action_kinds={"entry": "ENTRY", "exit": "EXIT"},
        facts=(
            _fact(
                "FILL",
                "trade-1",
                "entry",
                last_price="100",
                last_quantity="1",
            ),
            _fact(
                "FILL",
                "trade-2",
                "exit",
                last_price="101",
                last_quantity="1",
            ),
        ),
    )

    assert result["closed"] is True
    assert result["commission_complete"] is False
    assert result["net_pnl"] is None
    assert result["fills"][0]["fee"] is None
    assert result["fills"][0]["fee_currency"] is None


def test_incomplete_source_times_keep_trade_timing_unknown() -> None:
    result = summarize_trade_result(
        direction="LONG",
        action_kinds={"entry": "ENTRY", "exit": "EXIT"},
        facts=(
            _fact(
                "FILL",
                "trade-1",
                "entry",
                source_time="2026-07-20T00:00:00+00:00",
                last_price="100",
                last_quantity="1",
            ),
            _fact(
                "COMMISSION",
                "trade-1",
                "entry",
                amount="0.1 USDT",
                currency="USDT",
            ),
            _fact(
                "FILL",
                "trade-2",
                "exit",
                cutoff="2026-07-20T00:05:30+00:00",
                last_price="101",
                last_quantity="1",
            ),
            _fact(
                "COMMISSION",
                "trade-2",
                "exit",
                amount="0.1 USDT",
                currency="USDT",
            ),
        ),
    )

    assert result["fills"][0]["fill_time"] == "2026-07-20T00:00:00+00:00"
    assert result["fills"][1]["fill_time"] is None
    assert result["fill_times_complete"] is False
    assert result["first_fill_time"] is None
    assert result["last_fill_time"] is None
    assert result["holding_duration_seconds"] is None


def test_conflicting_commissions_do_not_choose_a_fee_value() -> None:
    result = summarize_trade_result(
        direction="LONG",
        action_kinds={"entry": "ENTRY"},
        facts=(
            _fact(
                "FILL",
                "trade-1",
                "entry",
                last_price="100",
                last_quantity="1",
            ),
            _fact(
                "COMMISSION",
                "trade-1",
                "entry",
                amount="0.1 USDT",
                currency="USDT",
            ),
            _fact(
                "COMMISSION",
                "trade-1",
                "entry",
                amount="0.2 USDT",
                currency="USDT",
            ),
        ),
    )

    assert result["commission_complete"] is False
    assert result["calculation_complete"] is False
    assert result["fills"][0]["fee"] is None
    assert result["fills"][0]["fee_currency"] is None


def test_commission_without_a_matching_fill_keeps_result_incomplete() -> None:
    result = summarize_trade_result(
        direction="LONG",
        action_kinds={"entry": "ENTRY"},
        facts=(
            _fact(
                "FILL",
                "trade-1",
                "entry",
                last_price="100",
                last_quantity="1",
            ),
            _fact(
                "COMMISSION",
                "trade-1",
                "entry",
                amount="0.1 USDT",
                currency="USDT",
            ),
            _fact(
                "COMMISSION",
                "trade-without-fill",
                "entry",
                amount="0.2 USDT",
                currency="USDT",
            ),
        ),
    )

    assert result["commission_complete"] is False
    assert result["calculation_complete"] is False
    assert result["net_pnl"] is None


@pytest.mark.parametrize(
    "placeholder_trade_id",
    (
        "00000000-0000-4000-8000-000000000001",
        "S-synthetic-placeholder",
    ),
)
def test_binance_trade_query_replaces_framework_reconciliation_placeholder(
    placeholder_trade_id: str,
) -> None:
    source_time = datetime(2030, 1, 15, 0, 5, tzinfo=UTC)
    common = {
        "client_order_id": "client-take-profit",
        "venue_order_ref": "synthetic-take-profit-order",
        "last_price": "99",
        "last_quantity": "1",
        "order_side": "BUY",
    }
    result = summarize_trade_result(
        direction="SHORT",
        action_kinds={"entry": "ENTRY", "take-profit": "TAKE_PROFIT"},
        facts=(
            _fact(
                "FILL",
                "synthetic-entry-trade",
                "entry",
                source_time=datetime(2030, 1, 15, 0, 0, tzinfo=UTC),
                last_price="100",
                last_quantity="1",
                order_side="SELL",
                liquidity_side="TAKER",
            ),
            _fact(
                "COMMISSION",
                "synthetic-entry-trade",
                "entry",
                amount="0.1 USDT",
                currency="USDT",
            ),
            _fact(
                "FILL",
                placeholder_trade_id,
                "take-profit",
                source_time=source_time,
                reconciliation=True,
                event_type="OrderFilled",
                liquidity_side="TAKER",
                **common,
            ),
            _fact(
                "COMMISSION",
                placeholder_trade_id,
                "take-profit",
                amount="0 USDT",
                currency="USDT",
            ),
            _fact(
                "FILL",
                "synthetic-exit-trade",
                "take-profit",
                source_time=source_time,
                event_type="BinanceUserTradeQuery",
                liquidity_side="TAKER",
                **common,
            ),
            _fact(
                "COMMISSION",
                "synthetic-exit-trade",
                "take-profit",
                amount="0.1 USDT",
                currency="USDT",
            ),
        ),
    )

    assert result["fill_count"] == 2
    assert result["position_quantity"] == "0"
    assert result["closed"] is True
    assert result["commission"] == "0.2"
    assert result["net_pnl"] == "0.8"
    assert [fill["trade_id"] for fill in result["fills"]] == [
        "synthetic-entry-trade",
        "synthetic-exit-trade",
    ]


def test_external_position_disposition_closes_operational_quantity_without_pnl() -> None:
    result = summarize_trade_result(
        direction="SHORT",
        action_kinds={"disposition": "EXIT"},
        facts=(
            _fact(
                "FILL",
                "trade-disposition",
                "disposition",
                source_time="2026-08-02T15:00:00+00:00",
                last_price="0.55",
                last_quantity="200",
                order_side="BUY",
                liquidity_side="TAKER",
            ),
            _fact(
                "COMMISSION",
                "trade-disposition",
                "disposition",
                amount="0.044",
                currency="USDT",
            ),
        ),
        opening_position_quantity="200",
    )

    assert result["position_quantity"] == "0"
    assert result["closed"] is True
    assert result["execution_cost_complete"] is True
    assert result["commission"] == "0.044"
    assert result["calculation_complete"] is False
    assert result["gross_pnl"] is None
    assert result["net_pnl"] is None
    assert result["result_scope"] == "EXTERNAL_POSITION_DISPOSITION"
    assert result["strategy_attribution_complete"] is False
