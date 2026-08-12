"""Authenticated read-only Binance account snapshots for DAT projection."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol
from uuid import uuid4

from nautilus_trader.adapters.binance import (
    BinanceAccountType,
    get_cached_binance_http_client,
)
from nautilus_trader.adapters.binance.common.enums import (
    BinanceEnvironment,
    BinanceKeyType,
)
from nautilus_trader.adapters.binance.futures.http.account import (
    BinanceFuturesAccountHttpAPI,
)
from nautilus_trader.common.component import LiveClock
from pydantic import SecretStr

from halpha.binance_contracts import (
    BINANCE_USDM_ACCOUNT_SNAPSHOT_QUERY_PATHS,
    BINANCE_USDM_ACCOUNT_SNAPSHOT_SCHEMA,
)
from halpha.domain_values import canonical_decimal
from halpha.venue_integration.binance_rate_limits import (
    binance_retry_after_seconds,
)
from halpha.venue_integration.facts import build_venue_fact
from halpha.venue_integration.models import (
    BINANCE_USDM_VENUE_REF,
    VenueFact,
    VenueFactKind,
    VenueFactSourceClass,
)


ACCOUNT_SNAPSHOT_TIMEOUT_SECONDS = 10


def _valid_binance_symbol(value: str) -> bool:
    # USDⓈ-M venue symbols can contain underscores and Unicode product names.
    # Preserve the exact venue identity for display; delimiters used by Halpha
    # references and whitespace remain forbidden.
    return bool(value) and len(value) <= 40 and not any(
        character.isspace() or character in {"/", "\\", ":"}
        for character in value
    )


class AccountObservationError(RuntimeError):
    """A stable, secret-free account observation failure."""

    def __init__(
        self,
        reason_code: str,
        *,
        retry_after_seconds: float | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.retry_after_seconds = retry_after_seconds
        self.retryable = retryable


class AccountFactRepository(Protocol):
    def insert(self, fact: VenueFact) -> bool: ...


class FuturesAccountReadApi(Protocol):
    async def query_futures_account_info(self, **kwargs: object) -> object: ...

    async def query_futures_position_risk(self, **kwargs: object) -> object: ...

    async def query_open_orders(self, **kwargs: object) -> object: ...

    async def query_open_algo_orders(self, **kwargs: object) -> object: ...

    async def query_futures_symbol_config(self, **kwargs: object) -> object: ...


def _value(item: object, name: str, *, required: bool = True) -> object | None:
    if isinstance(item, dict):
        value = item.get(name)
    else:
        value = getattr(item, name, None)
    if required and value is None:
        raise AccountObservationError("ACCOUNT_POSITION_SCHEMA_MISMATCH")
    return value


def _decimal(value: object, *, code: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise AccountObservationError(code) from None
    if not result.is_finite():
        raise AccountObservationError(code)
    return result


def _enum_text(value: object) -> str:
    return str(getattr(value, "value", value))


def _canonical_optional_decimal(
    item: object,
    name: str,
    *,
    code: str,
) -> str | None:
    value = _value(item, name, required=False)
    if value is None or str(value).strip() == "":
        return None
    return canonical_decimal(_decimal(value, code=code))


def _normalize_account_summary(account_info: object) -> dict[str, object]:
    can_trade = _value(account_info, "canTrade", required=False)
    if type(can_trade) is not bool:
        raise AccountObservationError("ACCOUNT_SUMMARY_CAN_TRADE_INVALID")
    decimal_fields = {
        "wallet_balance": ("totalWalletBalance", True),
        "unrealized_pnl": ("totalUnrealizedProfit", False),
        "margin_balance": ("totalMarginBalance", True),
        "available_balance": ("availableBalance", False),
        "initial_margin": ("totalInitialMargin", True),
        "maintenance_margin": ("totalMaintMargin", True),
        "position_initial_margin": ("totalPositionInitialMargin", True),
        "open_order_initial_margin": ("totalOpenOrderInitialMargin", True),
        "cross_wallet_balance": ("totalCrossWalletBalance", False),
        "cross_unrealized_pnl": ("totalCrossUnPnl", False),
    }
    normalized: dict[str, object] = {"can_trade": can_trade}
    for target, (source, non_negative) in decimal_fields.items():
        raw = _value(account_info, source, required=False)
        if raw is None:
            raise AccountObservationError("ACCOUNT_SUMMARY_DECIMAL_INVALID")
        value = _decimal(
            raw,
            code="ACCOUNT_SUMMARY_DECIMAL_INVALID",
        )
        if non_negative and value < 0:
            raise AccountObservationError("ACCOUNT_SUMMARY_DECIMAL_INVALID")
        normalized[target] = canonical_decimal(value)
    update_time = _value(account_info, "updateTime", required=False)
    if update_time is not None:
        try:
            update_time_ms = int(update_time)
        except (TypeError, ValueError):
            raise AccountObservationError("ACCOUNT_SUMMARY_UPDATE_TIME_INVALID") from None
        if update_time_ms < 0:
            raise AccountObservationError("ACCOUNT_SUMMARY_UPDATE_TIME_INVALID")
        normalized["source_update_time_ms"] = update_time_ms
    else:
        normalized["source_update_time_ms"] = None
    return normalized


def _order_text(
    item: object,
    name: str,
    *,
    code: str,
    required: bool = True,
    maximum_length: int = 128,
) -> str | None:
    value = _value(item, name, required=required)
    if value is None:
        return None
    text = _enum_text(value).strip()
    if not text:
        if required:
            raise AccountObservationError(code)
        return None
    if len(text) > maximum_length or any(
        ord(character) < 32 or ord(character) == 127 for character in text
    ):
        raise AccountObservationError(code)
    return text


def _order_decimal(
    item: object,
    name: str,
    *,
    code: str,
    required: bool = False,
) -> str | None:
    value = _value(item, name, required=required)
    if value is None or str(value).strip() == "":
        if required:
            raise AccountObservationError(code)
        return None
    parsed = _decimal(value, code=code)
    if parsed < 0:
        raise AccountObservationError(code)
    return canonical_decimal(parsed)


def _order_integer(
    item: object,
    name: str,
    *,
    code: str,
    required: bool = False,
) -> int | None:
    value = _value(item, name, required=required)
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise AccountObservationError(code) from None
    if parsed < 0:
        raise AccountObservationError(code)
    return parsed


def _order_boolean(item: object, name: str) -> bool | None:
    value = _value(item, name, required=False)
    if value is None:
        return None
    if type(value) is not bool:
        raise AccountObservationError("ACCOUNT_ORDER_BOOLEAN_INVALID")
    return value


def _normalize_order_identity(
    item: object,
    *,
    identifier_name: str,
    client_identifier_name: str,
    kind: str,
) -> dict[str, object]:
    symbol = str(_value(item, "symbol")).upper()
    if not _valid_binance_symbol(symbol):
        raise AccountObservationError("ACCOUNT_ORDER_SYMBOL_INVALID")
    identifier = _order_integer(
        item,
        identifier_name,
        code="ACCOUNT_ORDER_ID_INVALID",
        required=True,
    )
    client_identifier = _order_text(
        item,
        client_identifier_name,
        code="ACCOUNT_ORDER_CLIENT_ID_INVALID",
        required=False,
    )
    side = _order_text(
        item,
        "side",
        code="ACCOUNT_ORDER_SIDE_INVALID",
    )
    side = str(side).upper()
    if side not in {"BUY", "SELL"}:
        raise AccountObservationError("ACCOUNT_ORDER_SIDE_INVALID")
    position_side = _order_text(
        item,
        "positionSide",
        code="ACCOUNT_ORDER_POSITION_SIDE_INVALID",
        required=False,
    )
    position_side = str(position_side or "BOTH").upper()
    if position_side not in {"BOTH", "LONG", "SHORT"}:
        raise AccountObservationError("ACCOUNT_ORDER_POSITION_SIDE_INVALID")
    return {
        "kind": kind,
        "instrument_ref": f"{symbol}-PERP",
        "symbol": symbol,
        "order_id": str(identifier),
        "client_order_id": client_identifier,
        "side": side,
        "position_side": position_side,
    }


def _normalize_ordinary_order(item: object) -> dict[str, object]:
    normalized = _normalize_order_identity(
        item,
        identifier_name="orderId",
        client_identifier_name="clientOrderId",
        kind="ORDINARY",
    )
    order_type = _order_text(
        item,
        "type",
        code="ACCOUNT_ORDER_TYPE_INVALID",
    )
    status = _order_text(
        item,
        "status",
        code="ACCOUNT_ORDER_STATUS_INVALID",
    )
    quantity = _order_decimal(
        item,
        "origQty",
        code="ACCOUNT_ORDER_QUANTITY_INVALID",
        required=True,
    )
    if quantity == "0":
        raise AccountObservationError("ACCOUNT_ORDER_QUANTITY_INVALID")
    return {
        **normalized,
        "order_type": str(order_type).upper(),
        "status": str(status).upper(),
        "time_in_force": (
            value.upper()
            if (
                value := _order_text(
                    item,
                    "timeInForce",
                    code="ACCOUNT_ORDER_TIME_IN_FORCE_INVALID",
                    required=False,
                )
            )
            else None
        ),
        "price": _order_decimal(
            item,
            "price",
            code="ACCOUNT_ORDER_PRICE_INVALID",
        ),
        "trigger_price": _order_decimal(
            item,
            "stopPrice",
            code="ACCOUNT_ORDER_TRIGGER_PRICE_INVALID",
        ),
        "quantity": quantity,
        "executed_quantity": _order_decimal(
            item,
            "executedQty",
            code="ACCOUNT_ORDER_EXECUTED_QUANTITY_INVALID",
            required=True,
        ),
        "reduce_only": _order_boolean(item, "reduceOnly"),
        "close_position": _order_boolean(item, "closePosition"),
        "source_create_time_ms": _order_integer(
            item,
            "time",
            code="ACCOUNT_ORDER_CREATE_TIME_INVALID",
        ),
        "source_update_time_ms": _order_integer(
            item,
            "updateTime",
            code="ACCOUNT_ORDER_UPDATE_TIME_INVALID",
        ),
    }


def _normalize_algo_order(item: object) -> dict[str, object]:
    normalized = _normalize_order_identity(
        item,
        identifier_name="algoId",
        client_identifier_name="clientAlgoId",
        kind="ALGO",
    )
    order_type = _order_text(
        item,
        "orderType",
        code="ACCOUNT_ORDER_TYPE_INVALID",
    )
    status = _order_text(
        item,
        "algoStatus",
        code="ACCOUNT_ORDER_STATUS_INVALID",
    )
    return {
        **normalized,
        "order_type": str(order_type).upper(),
        "status": str(status).upper(),
        "time_in_force": (
            value.upper()
            if (
                value := _order_text(
                    item,
                    "timeInForce",
                    code="ACCOUNT_ORDER_TIME_IN_FORCE_INVALID",
                    required=False,
                )
            )
            else None
        ),
        "price": _order_decimal(
            item,
            "price",
            code="ACCOUNT_ORDER_PRICE_INVALID",
        ),
        "trigger_price": _order_decimal(
            item,
            "triggerPrice",
            code="ACCOUNT_ORDER_TRIGGER_PRICE_INVALID",
        ),
        "quantity": _order_decimal(
            item,
            "quantity",
            code="ACCOUNT_ORDER_QUANTITY_INVALID",
        ),
        "executed_quantity": None,
        "reduce_only": _order_boolean(item, "reduceOnly"),
        "close_position": _order_boolean(item, "closePosition"),
        "source_create_time_ms": _order_integer(
            item,
            "createTime",
            code="ACCOUNT_ORDER_CREATE_TIME_INVALID",
        ),
        "source_update_time_ms": _order_integer(
            item,
            "updateTime",
            code="ACCOUNT_ORDER_UPDATE_TIME_INVALID",
        ),
    }


def _order_sort_key(item: dict[str, object]) -> tuple[str, int, str]:
    update_time = item.get("source_update_time_ms")
    create_time = item.get("source_create_time_ms")
    event_time = update_time if isinstance(update_time, int) else create_time
    return (
        str(item["symbol"]),
        -(event_time if isinstance(event_time, int) else 0),
        str(item["order_id"]),
    )


def _normalize_symbol_configs(items: object) -> dict[str, tuple[str, int]]:
    if not isinstance(items, (list, tuple)):
        raise AccountObservationError("ACCOUNT_SYMBOL_CONFIG_RESPONSE_SCHEMA_MISMATCH")
    configs: dict[str, tuple[str, int]] = {}
    for item in items:
        symbol = str(_value(item, "symbol")).upper()
        if not _valid_binance_symbol(symbol) or symbol in configs:
            raise AccountObservationError("ACCOUNT_SYMBOL_CONFIG_INVALID")
        raw_margin_mode = _enum_text(_value(item, "marginType")).upper()
        margin_mode = {
            "CROSSED": "CROSS",
            "CROSS": "CROSS",
            "ISOLATED": "ISOLATED",
        }.get(raw_margin_mode)
        if margin_mode is None:
            raise AccountObservationError("ACCOUNT_POSITION_MARGIN_MODE_INVALID")
        try:
            leverage = int(_value(item, "leverage"))
        except (TypeError, ValueError):
            raise AccountObservationError(
                "ACCOUNT_POSITION_LEVERAGE_INVALID"
            ) from None
        if leverage <= 0:
            raise AccountObservationError("ACCOUNT_POSITION_LEVERAGE_INVALID")
        configs[symbol] = (margin_mode, leverage)
    return configs


def _normalize_position(
    item: object,
    *,
    symbol_configs: dict[str, tuple[str, int]],
) -> dict[str, object] | None:
    quantity = _decimal(
        _value(item, "positionAmt"),
        code="ACCOUNT_POSITION_QUANTITY_INVALID",
    )
    if quantity == 0:
        return None
    symbol = str(_value(item, "symbol")).upper()
    if not _valid_binance_symbol(symbol):
        raise AccountObservationError("ACCOUNT_POSITION_SYMBOL_INVALID")
    position_side = _enum_text(
        _value(item, "positionSide", required=False) or "BOTH"
    ).upper()
    if position_side not in {"BOTH", "LONG", "SHORT"}:
        raise AccountObservationError("ACCOUNT_POSITION_SIDE_INVALID")
    direction = "LONG" if quantity > 0 else "SHORT"
    if position_side != "BOTH" and position_side != direction:
        raise AccountObservationError("ACCOUNT_POSITION_DIRECTION_CONFLICT")
    config = symbol_configs.get(symbol)
    if config is None:
        raise AccountObservationError("ACCOUNT_POSITION_SYMBOL_CONFIG_MISSING")
    margin_mode, leverage = config
    mark_price = _decimal(
        _value(item, "markPrice"),
        code="ACCOUNT_POSITION_MARK_PRICE_INVALID",
    )
    entry_price = _decimal(
        _value(item, "entryPrice"),
        code="ACCOUNT_POSITION_ENTRY_PRICE_INVALID",
    )
    if mark_price <= 0 or entry_price <= 0:
        raise AccountObservationError("ACCOUNT_POSITION_PRICE_INVALID")
    update_time = _value(item, "updateTime", required=False)
    try:
        source_update_time_ms = int(update_time) if update_time is not None else None
    except (TypeError, ValueError):
        raise AccountObservationError("ACCOUNT_POSITION_UPDATE_TIME_INVALID") from None
    if source_update_time_ms is not None and source_update_time_ms < 0:
        raise AccountObservationError("ACCOUNT_POSITION_UPDATE_TIME_INVALID")
    return {
        "instrument_ref": f"{symbol}-PERP",
        "symbol": symbol,
        "direction": direction,
        "position_side": position_side,
        "quantity": canonical_decimal(quantity),
        "absolute_quantity": canonical_decimal(abs(quantity)),
        "entry_price": canonical_decimal(entry_price),
        "break_even_price": _canonical_optional_decimal(
            item,
            "breakEvenPrice",
            code="ACCOUNT_POSITION_BREAK_EVEN_PRICE_INVALID",
        ),
        "mark_price": canonical_decimal(mark_price),
        "unrealized_pnl": canonical_decimal(
            _decimal(
                _value(item, "unRealizedProfit"),
                code="ACCOUNT_POSITION_UNREALIZED_PNL_INVALID",
            )
        ),
        "liquidation_price": _canonical_optional_decimal(
            item,
            "liquidationPrice",
            code="ACCOUNT_POSITION_LIQUIDATION_PRICE_INVALID",
        ),
        "leverage": leverage,
        "margin_mode": margin_mode,
        "notional": canonical_decimal(
            _decimal(
                _value(item, "notional"),
                code="ACCOUNT_POSITION_NOTIONAL_INVALID",
            )
        ),
        "isolated_margin": _canonical_optional_decimal(
            item,
            "isolatedMargin",
            code="ACCOUNT_POSITION_ISOLATED_MARGIN_INVALID",
        ),
        "source_update_time_ms": source_update_time_ms,
    }


def build_account_snapshot_fact(
    *,
    environment_id: str,
    account_ref: str,
    account_info: object,
    positions: object,
    symbol_configs: object,
    open_orders: object,
    open_algo_orders: object,
    started_at: datetime,
    checked_at: datetime,
) -> VenueFact:
    """Normalize one complete private read without attributing external exposure."""

    if started_at.utcoffset() is None or checked_at.utcoffset() is None:
        raise AccountObservationError("ACCOUNT_SNAPSHOT_TIMEZONE_REQUIRED")
    if checked_at < started_at:
        raise AccountObservationError("ACCOUNT_SNAPSHOT_TIME_REGRESSION")
    if not isinstance(positions, (list, tuple)):
        raise AccountObservationError("ACCOUNT_POSITION_RESPONSE_SCHEMA_MISMATCH")
    if not isinstance(open_orders, (list, tuple)):
        raise AccountObservationError("ACCOUNT_ORDER_RESPONSE_SCHEMA_MISMATCH")
    if not isinstance(open_algo_orders, (list, tuple)):
        raise AccountObservationError("ACCOUNT_ALGO_ORDER_RESPONSE_SCHEMA_MISMATCH")
    normalized_account_summary = _normalize_account_summary(account_info)
    normalized_symbol_configs = _normalize_symbol_configs(symbol_configs)
    normalized_positions = tuple(
        normalized
        for item in positions
        if (
            normalized := _normalize_position(
                item,
                symbol_configs=normalized_symbol_configs,
            )
        )
        is not None
    )
    normalized_positions = tuple(
        sorted(
            normalized_positions,
            key=lambda item: (str(item["symbol"]), str(item["position_side"])),
        )
    )
    normalized_ordinary_orders = tuple(
        sorted(
            (_normalize_ordinary_order(item) for item in open_orders),
            key=_order_sort_key,
        )
    )
    normalized_algo_orders = tuple(
        sorted(
            (_normalize_algo_order(item) for item in open_algo_orders),
            key=_order_sort_key,
        )
    )
    sequence = str(int(checked_at.timestamp() * 1_000_000))
    return build_venue_fact(
        venue_fact_id=str(uuid4()),
        environment_id=environment_id,
        venue_ref=BINANCE_USDM_VENUE_REF,
        account_ref=account_ref,
        instrument_ref=None,
        kind=VenueFactKind.ACCOUNT_STATE,
        source_class=VenueFactSourceClass.VENUE_QUERY,
        source_object_id=f"{account_ref}:ACCOUNT_STATE",
        source_sequence=sequence,
        source_time=None,
        received_at=checked_at.astimezone(UTC),
        cutoff=checked_at.astimezone(UTC),
        payload={
            "schema": BINANCE_USDM_ACCOUNT_SNAPSHOT_SCHEMA,
            "query_paths": list(BINANCE_USDM_ACCOUNT_SNAPSHOT_QUERY_PATHS),
            "read_only": True,
            "snapshot_complete": True,
            "snapshot_started_at": started_at.astimezone(UTC).isoformat(),
            "management_authority": "NONE",
            "account_summary": normalized_account_summary,
            "positions": list(normalized_positions),
            "open_position_count": len(normalized_positions),
            "ordinary_open_orders": list(normalized_ordinary_orders),
            "algo_open_orders": list(normalized_algo_orders),
            "ordinary_open_order_count": len(normalized_ordinary_orders),
            "algo_open_order_count": len(normalized_algo_orders),
        },
    )


class ProductAccountObserver:
    """Poll private account reads and append only complete account facts."""

    def __init__(
        self,
        *,
        profile: str,
        environment_id: str,
        account_ref: str,
        api_key: SecretStr,
        api_secret: SecretStr,
        proxy_url: str | None,
        repository: AccountFactRepository,
        account_api: FuturesAccountReadApi | None = None,
        clock: object | None = None,
    ) -> None:
        if profile not in {
            "BINANCE_DEMO",
            "BINANCE_LIVE_READ_ONLY",
            "BINANCE_LIVE_WRITE",
        }:
            raise AccountObservationError("ACCOUNT_OBSERVATION_PROFILE_MISMATCH")
        self._environment_id = environment_id
        self._account_ref = account_ref
        self._repository = repository
        self._clock = clock or LiveClock()
        if account_api is None:
            client = get_cached_binance_http_client(
                clock=self._clock,
                account_type=BinanceAccountType.USDT_FUTURES,
                api_key=api_key.get_secret_value(),
                api_secret=api_secret.get_secret_value(),
                key_type=BinanceKeyType.HMAC,
                base_url=None,
                environment=(
                    BinanceEnvironment.DEMO
                    if profile == "BINANCE_DEMO"
                    else BinanceEnvironment.LIVE
                ),
                is_us=False,
                proxy_url=proxy_url,
            )
            account_api = BinanceFuturesAccountHttpAPI(
                client,
                self._clock,
                BinanceAccountType.USDT_FUTURES,
            )
        self._account_api = account_api

    async def observe(self) -> VenueFact:
        started_at = datetime.now(UTC)
        try:
            (
                account_info,
                positions,
                symbol_configs,
                open_orders,
                open_algo_orders,
            ) = await asyncio.wait_for(
                asyncio.gather(
                    self._account_api.query_futures_account_info(
                        recv_window="5000"
                    ),
                    self._account_api.query_futures_position_risk(
                        recv_window="5000"
                    ),
                    self._account_api.query_futures_symbol_config(
                        recv_window="5000"
                    ),
                    self._account_api.query_open_orders(recv_window="5000"),
                    self._account_api.query_open_algo_orders(
                        recv_window="5000"
                    ),
                ),
                timeout=ACCOUNT_SNAPSHOT_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            if isinstance(exc, AccountObservationError):
                raise
            raise AccountObservationError(
                f"ACCOUNT_SNAPSHOT_QUERY_FAILED_{type(exc).__name__.upper()}",
                retry_after_seconds=binance_retry_after_seconds(exc),
                retryable=True,
            ) from None
        checked_at = datetime.now(UTC)
        fact = build_account_snapshot_fact(
            environment_id=self._environment_id,
            account_ref=self._account_ref,
            account_info=account_info,
            positions=positions,
            symbol_configs=symbol_configs,
            open_orders=open_orders,
            open_algo_orders=open_algo_orders,
            started_at=started_at,
            checked_at=checked_at,
        )
        try:
            inserted = self._repository.insert(fact)
        except Exception as exc:
            raise AccountObservationError(
                f"ACCOUNT_SNAPSHOT_PERSIST_FAILED_{type(exc).__name__.upper()}"
            ) from None
        if not inserted:
            raise AccountObservationError("ACCOUNT_SNAPSHOT_IDENTITY_CONFLICT")
        return fact
