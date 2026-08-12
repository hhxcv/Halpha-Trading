from datetime import UTC, datetime

import pytest

from halpha.outcomes.account_reconciliation import (
    AccountReconciliationError,
    EXTERNAL_ACCOUNT_CLOSURE,
    account_result_role,
    build_external_account_closure_facts,
)
from halpha.outcomes.trade_result import summarize_trade_result


def _facts(*, reduce_only: bool = True, realized_pnl: str = "-5"):
    return build_external_account_closure_facts(
        environment_id="demo-main",
        account_ref="demo-account",
        instrument_ref="TESTUSDT-PERP",
        activation_id="activation-1",
        direction="LONG",
        open_quantity="1",
        average_entry_price="100",
        attributed_trade_ids=frozenset({"synthetic-entry-trade"}),
        order={
            "order_id": "synthetic-exit-order",
            "client_order_id": "synthetic-client-order",
            "symbol": "TESTUSDT",
            "status": "FILLED",
            "side": "SELL",
            "order_type": "MARKET",
            "executed_quantity": "1",
            "average_price": "90",
            "reduce_only": reduce_only,
            "update_time_ms": 1894665840000,
        },
        trades=(
            {
                "trade_id": "synthetic-exit-trade-1",
                "order_id": "synthetic-exit-order",
                "symbol": "TESTUSDT",
                "side": "SELL",
                "price": "90",
                "quantity": "0.6",
                "commission": "0.06",
                "commission_asset": "USDT",
                "realized_pnl": "-6",
                "time_ms": 1894665840000,
                "maker": False,
            },
            {
                "trade_id": "synthetic-exit-trade-2",
                "order_id": "synthetic-exit-order",
                "symbol": "TESTUSDT",
                "side": "SELL",
                "price": "90",
                "quantity": "0.4",
                "commission": "0.04",
                "commission_asset": "USDT",
                "realized_pnl": realized_pnl,
                "time_ms": 1894665840000,
                "maker": False,
            },
        ),
        observed_at=datetime(2030, 1, 15, 0, 4, tzinfo=UTC),
    )


def test_external_closure_remains_unclaimed_but_closes_exact_account_result() -> None:
    facts = _facts(realized_pnl="-4")

    assert len(facts) == 5
    assert all(fact.activation_ref is None for fact in facts)
    assert all(fact.action_ref is None for fact in facts)
    assert all(fact.attribution_class is None for fact in facts)
    assert all(
        account_result_role(fact.impact_scope) == EXTERNAL_ACCOUNT_CLOSURE
        for fact in facts
    )

    entry_facts = (
        {
            "kind": "FILL",
            "action_ref": "entry-action",
            "source_time": "2030-01-15T00:00:00+00:00",
            "payload": {
                "trade_id": "synthetic-entry-trade",
                "last_price": "100",
                "last_quantity": "1",
            },
        },
        {
            "kind": "COMMISSION",
            "action_ref": "entry-action",
            "payload": {
                "trade_id": "synthetic-entry-trade",
                "amount": "0.1 USDT",
                "currency": "USDT",
            },
        },
    )
    external_facts = tuple(
        {
            "kind": fact.kind.value,
            "action_ref": None,
            "source_time": fact.source_time,
            "result_role": account_result_role(fact.impact_scope),
            "payload": fact.payload,
        }
        for fact in facts
    )
    result = summarize_trade_result(
        direction="LONG",
        action_kinds={"entry-action": "ENTRY"},
        facts=(*entry_facts, *external_facts),
    )

    assert result["closed"] is True
    assert result["average_exit_price"] == "90"
    assert result["gross_pnl"] == "-10"
    assert result["commission"] == "0.2"
    assert result["net_pnl"] == "-10.2"
    assert result["holding_duration_seconds"] == "240"
    assert result["result_scope"] == "ACCOUNT_FACTS_WITH_EXTERNAL_CLOSURE"
    assert result["strategy_attribution_complete"] is False
    assert result["external_closure_fill_count"] == 2


def test_external_closure_requires_reduce_only_order() -> None:
    with pytest.raises(
        AccountReconciliationError,
        match="EXTERNAL_ORDER_NOT_REDUCE_ONLY",
    ):
        _facts(reduce_only=False, realized_pnl="-4")


def test_external_closure_rejects_exchange_pnl_mismatch() -> None:
    with pytest.raises(
        AccountReconciliationError,
        match="EXTERNAL_REALIZED_PNL_MISMATCH",
    ):
        _facts(realized_pnl="0")
