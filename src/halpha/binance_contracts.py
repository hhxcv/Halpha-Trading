"""Capability-free Binance identities shared across process boundaries."""

from __future__ import annotations

from typing import NotRequired, TypedDict, cast


FUTURES_POSITION_RISK_PATH = "/fapi/v3/positionRisk"
FUTURES_ACCOUNT_INFORMATION_PATH = "/fapi/v3/account"
FUTURES_SYMBOL_CONFIG_PATH = "/fapi/v1/symbolConfig"
FUTURES_OPEN_ORDERS_PATH = "/fapi/v1/openOrders"
FUTURES_OPEN_ALGO_ORDERS_PATH = "/fapi/v1/openAlgoOrders"

BINANCE_USDM_ACCOUNT_SNAPSHOT_LEGACY_SCHEMA = "HALPHA_BINANCE_USDM_ACCOUNT_SNAPSHOT_V2"
BINANCE_USDM_ACCOUNT_SNAPSHOT_SCHEMA = "HALPHA_BINANCE_USDM_ACCOUNT_SNAPSHOT_V3"
BINANCE_USDM_ACCOUNT_POSITION_ORDER_QUERY_PATHS = (
    FUTURES_POSITION_RISK_PATH,
    FUTURES_SYMBOL_CONFIG_PATH,
    FUTURES_OPEN_ORDERS_PATH,
    FUTURES_OPEN_ALGO_ORDERS_PATH,
)
BINANCE_USDM_ACCOUNT_SNAPSHOT_QUERY_PATHS = (
    FUTURES_ACCOUNT_INFORMATION_PATH,
    *BINANCE_USDM_ACCOUNT_POSITION_ORDER_QUERY_PATHS,
)


class CompleteBinanceUsdmAccountSnapshot(TypedDict):
    """The transport-wide portion of a complete account snapshot."""

    schema: str
    snapshot_complete: bool
    read_only: bool
    management_authority: str
    positions: list[dict[str, object]]
    open_position_count: int
    ordinary_open_orders: list[dict[str, object]]
    ordinary_open_order_count: int
    algo_open_orders: list[dict[str, object]]
    algo_open_order_count: int
    account_summary: NotRequired[dict[str, object]]
    query_paths: NotRequired[list[object]]


def _counted_record_list(
    payload: dict[str, object],
    *,
    records_key: str,
    count_key: str,
) -> bool:
    records = payload.get(records_key)
    count = payload.get(count_key)
    return (
        isinstance(records, list)
        and all(isinstance(item, dict) for item in records)
        and type(count) is int
        and count >= 0
        and count == len(records)
    )


def parse_complete_binance_usdm_account_snapshot(
    payload: object,
) -> CompleteBinanceUsdmAccountSnapshot | None:
    """Return a complete account snapshot's shared shape, or ``None``.

    This validates the transport-wide contract only. Consumers still own
    query-path, freshness, and per-position/order field validation.
    """

    if not isinstance(payload, dict):
        return None
    snapshot = cast(dict[str, object], payload)
    schema = snapshot.get("schema")
    if schema not in {
        BINANCE_USDM_ACCOUNT_SNAPSHOT_LEGACY_SCHEMA,
        BINANCE_USDM_ACCOUNT_SNAPSHOT_SCHEMA,
    }:
        return None
    if (
        snapshot.get("snapshot_complete") is not True
        or snapshot.get("read_only") is not True
        or snapshot.get("management_authority") != "NONE"
    ):
        return None
    if schema == BINANCE_USDM_ACCOUNT_SNAPSHOT_SCHEMA and not isinstance(
        snapshot.get("account_summary"), dict
    ):
        return None
    if not all(
        _counted_record_list(
            snapshot,
            records_key=records_key,
            count_key=count_key,
        )
        for records_key, count_key in (
            ("positions", "open_position_count"),
            ("ordinary_open_orders", "ordinary_open_order_count"),
            ("algo_open_orders", "algo_open_order_count"),
        )
    ):
        return None
    return cast(CompleteBinanceUsdmAccountSnapshot, snapshot)
