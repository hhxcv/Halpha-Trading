from __future__ import annotations

from halpha.binance_contracts import (
    BINANCE_USDM_ACCOUNT_SNAPSHOT_LEGACY_SCHEMA,
    BINANCE_USDM_ACCOUNT_SNAPSHOT_SCHEMA,
    parse_complete_binance_usdm_account_snapshot,
)


def _complete_snapshot() -> dict[str, object]:
    return {
        "schema": BINANCE_USDM_ACCOUNT_SNAPSHOT_LEGACY_SCHEMA,
        "snapshot_complete": True,
        "read_only": True,
        "management_authority": "NONE",
        "positions": [],
        "open_position_count": 0,
        "ordinary_open_orders": [],
        "ordinary_open_order_count": 0,
        "algo_open_orders": [],
        "algo_open_order_count": 0,
    }


def test_shared_snapshot_parser_requires_counted_complete_lists() -> None:
    payload = _complete_snapshot()

    assert parse_complete_binance_usdm_account_snapshot(payload) is payload
    assert (
        parse_complete_binance_usdm_account_snapshot(
            {**payload, "open_position_count": 1}
        )
        is None
    )
    assert (
        parse_complete_binance_usdm_account_snapshot(
            {**payload, "ordinary_open_orders": ["not-a-record"]}
        )
        is None
    )


def test_shared_snapshot_parser_requires_v3_account_summary() -> None:
    payload = {**_complete_snapshot(), "schema": BINANCE_USDM_ACCOUNT_SNAPSHOT_SCHEMA}

    assert parse_complete_binance_usdm_account_snapshot(payload) is None
