"""On-demand, short-lived public instrument rules for order-schedule preview."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import re
from time import monotonic
from typing import Any, Mapping, Protocol

from nautilus_trader.adapters.binance import get_cached_binance_http_client
from nautilus_trader.adapters.binance.common.enums import (
    BinanceAccountType,
    BinanceEnvironment,
)
from nautilus_trader.adapters.binance.futures.http.market import (
    BinanceFuturesMarketHttpAPI,
)
from nautilus_trader.common.component import LiveClock

from halpha.planning.order_schedule import InstrumentOrderRules
from halpha.scalping.models import (
    BinanceContract,
    BinanceContractCatalog,
)
from halpha.venue_integration.binance_rules import (
    BinanceInstrumentRulesError,
    binance_exchange_symbol_rules,
)


INSTRUMENT_RULES_TIMEOUT_SECONDS = 5
INSTRUMENT_RULES_QUERY_ATTEMPTS = 2
INSTRUMENT_RULES_RETRY_DELAY_SECONDS = 0.2
INSTRUMENT_RULES_TTL_SECONDS = 60
INSTRUMENT_RULES_STALE_ON_ERROR_SECONDS = 15 * 60
INSTRUMENT_RULES_FAILURE_COOLDOWN_SECONDS = 30
INSTRUMENT_RULES_MAX_ENTRIES = 2048
_PERPETUAL_INSTRUMENT = re.compile(r"^[A-Z0-9]+-PERP$")


class InstrumentRulesUnavailable(RuntimeError):
    """Sanitized public-rule read failure."""


class BinanceExchangeInfoApi(Protocol):
    async def query_futures_exchange_info(self) -> object: ...


class InstrumentRulesProvider(Protocol):
    async def fetch(self, instrument_ref: str) -> InstrumentOrderRules: ...

    async def list_contracts(self) -> BinanceContractCatalog: ...


def binance_public_instrument_rules_identity(
    profile: str,
) -> tuple[BinanceEnvironment, str]:
    """Resolve one exchange-info identity for the complete runtime profile."""

    if profile == "BINANCE_DEMO":
        return BinanceEnvironment.DEMO, "BINANCE_DEMO_EXCHANGE_INFO"
    if profile in {"BINANCE_LIVE_READ_ONLY", "BINANCE_LIVE_WRITE"}:
        return BinanceEnvironment.LIVE, "BINANCE_LIVE_EXCHANGE_INFO"
    raise ValueError("INSTRUMENT_RULES_PROFILE_UNSUPPORTED")


class BinancePublicInstrumentRules:
    """Fetch exchangeInfo only when a schedule is previewed, with a short TTL."""

    def __init__(
        self,
        profile: str,
        *,
        proxy_url: str | None = None,
        market_api: BinanceExchangeInfoApi | None = None,
        ttl_seconds: int = INSTRUMENT_RULES_TTL_SECONDS,
        stale_on_error_seconds: int = INSTRUMENT_RULES_STALE_ON_ERROR_SECONDS,
        failure_cooldown_seconds: int = INSTRUMENT_RULES_FAILURE_COOLDOWN_SECONDS,
        max_entries: int = INSTRUMENT_RULES_MAX_ENTRIES,
        query_attempts: int = INSTRUMENT_RULES_QUERY_ATTEMPTS,
        retry_delay_seconds: float = INSTRUMENT_RULES_RETRY_DELAY_SECONDS,
    ) -> None:
        environment, self._source = binance_public_instrument_rules_identity(profile)
        if market_api is None:
            client = get_cached_binance_http_client(
                clock=LiveClock(),
                account_type=BinanceAccountType.USDT_FUTURES,
                environment=environment,
                proxy_url=proxy_url,
            )
            market_api = BinanceFuturesMarketHttpAPI(
                client,
                BinanceAccountType.USDT_FUTURES,
            )
        self._market_api = market_api
        self._ttl_seconds = ttl_seconds
        self._stale_on_error_seconds = max(0, stale_on_error_seconds)
        self._failure_cooldown_seconds = max(0, failure_cooldown_seconds)
        self._max_entries = max(1, max_entries)
        self._query_attempts = max(1, query_attempts)
        self._retry_delay_seconds = max(0.0, retry_delay_seconds)
        self._cache: dict[str, tuple[float, InstrumentOrderRules]] = {}
        self._catalog_cache: tuple[float, BinanceContractCatalog] | None = None
        self._failure_retry_after: dict[str, float] = {}
        self._failure_reason: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def fetch(self, instrument_ref: str) -> InstrumentOrderRules:
        symbol = _symbol_for_instrument(instrument_ref)
        cached_result = self._preview_cached_result(symbol, monotonic())
        if cached_result is not None:
            return cached_result
        async with self._lock:
            now = monotonic()
            self._prune_expired_state(now)
            cached_result = self._preview_cached_result(symbol, now)
            if cached_result is not None:
                return cached_result
            try:
                return await self._query_and_cache(symbol)
            except InstrumentRulesUnavailable as exc:
                self._remember_failure(symbol, exc)
                # Exchange precision metadata changes rarely. A bounded stale
                # value keeps draft preview responsive during a transient
                # public exchangeInfo timeout; activation still calls
                # refresh() and therefore always fails closed.
                cached = self._cache.get(symbol)
                if (
                    cached is not None
                    and cached[0] + self._stale_on_error_seconds > monotonic()
                ):
                    return cached[1]
                raise

    async def refresh(self, instrument_ref: str) -> InstrumentOrderRules:
        """Bypass the preview cache for an activation-time rule check."""

        symbol = _symbol_for_instrument(instrument_ref)
        async with self._lock:
            self._prune_expired_state(monotonic())
            try:
                return await self._query_and_cache(symbol)
            except InstrumentRulesUnavailable as exc:
                self._remember_failure(symbol, exc)
                raise

    async def list_contracts(self) -> BinanceContractCatalog:
        cached = self._catalog_cache
        now = monotonic()
        if cached is not None and cached[0] > now:
            return cached[1]
        async with self._lock:
            cached = self._catalog_cache
            now = monotonic()
            if cached is not None and cached[0] > now:
                return cached[1]
            exchange_info = await self._query_exchange_info()
            return self._remember_catalog(exchange_info)

    def _preview_cached_result(
        self,
        symbol: str,
        now: float,
    ) -> InstrumentOrderRules | None:
        cached = self._cache.get(symbol)
        if cached is not None and cached[0] > now:
            return cached[1]
        if self._failure_retry_after.get(symbol, 0.0) <= now:
            return None
        if (
            cached is not None
            and cached[0] + self._stale_on_error_seconds > now
        ):
            return cached[1]
        raise InstrumentRulesUnavailable(
            self._failure_reason.get(symbol, "INSTRUMENT_RULES_QUERY_COOLDOWN")
        )

    def _remember_failure(
        self,
        symbol: str,
        error: InstrumentRulesUnavailable,
    ) -> None:
        self._failure_retry_after.pop(symbol, None)
        self._failure_reason.pop(symbol, None)
        self._failure_reason[symbol] = str(error)
        self._failure_retry_after[symbol] = (
            monotonic() + self._failure_cooldown_seconds
        )
        while len(self._failure_retry_after) > self._max_entries:
            oldest = next(iter(self._failure_retry_after))
            self._failure_retry_after.pop(oldest, None)
            self._failure_reason.pop(oldest, None)

    def _prune_expired_state(self, now: float) -> None:
        expired_failures = [
            symbol
            for symbol, retry_after in self._failure_retry_after.items()
            if retry_after <= now
        ]
        for symbol in expired_failures:
            self._failure_retry_after.pop(symbol, None)
            self._failure_reason.pop(symbol, None)
        expired_cache = [
            symbol
            for symbol, (fresh_until, _rules) in self._cache.items()
            if fresh_until + self._stale_on_error_seconds <= now
        ]
        for symbol in expired_cache:
            self._cache.pop(symbol, None)

    async def _query_and_cache(self, symbol: str) -> InstrumentOrderRules:
        exchange_info = await self._query_exchange_info()
        self._remember_catalog(exchange_info)
        try:
            rules = binance_exchange_symbol_rules(exchange_info, symbol)
            source_time_ms = _value(exchange_info, "serverTime")
            if not isinstance(source_time_ms, int) or source_time_ms <= 0:
                raise BinanceInstrumentRulesError("INSTRUMENT_RULES_CUTOFF_UNKNOWN")
            result = InstrumentOrderRules(
                **rules.order_schedule_payload(),
                source=self._source,
                source_cutoff=datetime.fromtimestamp(
                    source_time_ms / 1000,
                    tz=UTC,
                ).isoformat(),
            )
        except InstrumentRulesUnavailable:
            raise
        except Exception as exc:
            raise InstrumentRulesUnavailable(
                f"INSTRUMENT_RULES_QUERY_FAILED_{type(exc).__name__.upper()}"
            ) from None
        self._cache.pop(symbol, None)
        self._cache[symbol] = (monotonic() + self._ttl_seconds, result)
        while len(self._cache) > self._max_entries:
            self._cache.pop(next(iter(self._cache)), None)
        self._failure_retry_after.pop(symbol, None)
        self._failure_reason.pop(symbol, None)
        return result

    async def _query_exchange_info(self) -> object:
        exchange_info: object | None = None
        for attempt in range(self._query_attempts):
            try:
                exchange_info = await asyncio.wait_for(
                    self._market_api.query_futures_exchange_info(),
                    timeout=INSTRUMENT_RULES_TIMEOUT_SECONDS,
                )
                break
            except Exception as exc:
                is_timeout = "TIMEOUT" in type(exc).__name__.upper()
                if not is_timeout or attempt + 1 >= self._query_attempts:
                    raise InstrumentRulesUnavailable(
                        f"INSTRUMENT_RULES_QUERY_FAILED_{type(exc).__name__.upper()}"
                    ) from None
                if self._retry_delay_seconds > 0:
                    await asyncio.sleep(self._retry_delay_seconds)
        if exchange_info is None:
            raise InstrumentRulesUnavailable("INSTRUMENT_RULES_QUERY_FAILED_UNKNOWN")
        return exchange_info

    def _remember_catalog(self, exchange_info: object) -> BinanceContractCatalog:
        source_time_ms = _value(exchange_info, "serverTime")
        symbols = _value(exchange_info, "symbols")
        if (
            not isinstance(source_time_ms, int)
            or source_time_ms <= 0
            or not isinstance(symbols, list)
        ):
            raise InstrumentRulesUnavailable("INSTRUMENT_CATALOG_INVALID")
        contracts: list[BinanceContract] = []
        for item in symbols:
            symbol = str(_value(item, "symbol") or "")
            status = str(_enum_text(_value(item, "status")) or "")
            contract_type = str(_enum_text(_value(item, "contractType")) or "")
            quote_asset = str(_value(item, "quoteAsset") or "")
            base_asset = str(_value(item, "baseAsset") or "")
            if (
                status != "TRADING"
                or contract_type != "PERPETUAL"
                or quote_asset != "USDT"
                or not base_asset
                or _PERPETUAL_INSTRUMENT.fullmatch(f"{symbol}-PERP") is None
            ):
                continue
            contracts.append(
                BinanceContract(
                    instrument_ref=f"{symbol}-PERP",
                    symbol=symbol,
                    base_asset=base_asset,
                )
            )
        contracts.sort(key=lambda item: item.symbol)
        catalog = BinanceContractCatalog(
            source=self._source,
            source_cutoff=datetime.fromtimestamp(source_time_ms / 1000, tz=UTC),
            contracts=tuple(contracts),
        )
        self._catalog_cache = (monotonic() + self._ttl_seconds, catalog)
        return catalog


def _symbol_for_instrument(instrument_ref: str) -> str:
    if not _PERPETUAL_INSTRUMENT.fullmatch(instrument_ref):
        raise InstrumentRulesUnavailable("INSTRUMENT_RULES_INSTRUMENT_UNSUPPORTED")
    return instrument_ref.removesuffix("-PERP")


def symbol_for_perpetual_instrument(instrument_ref: str) -> str:
    return _symbol_for_instrument(instrument_ref)


def perpetual_instrument_for_symbol(symbol: str) -> str:
    instrument_ref = f"{symbol}-PERP"
    _symbol_for_instrument(instrument_ref)
    return instrument_ref


def _value(source: object, name: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _enum_text(value: object) -> object:
    return getattr(value, "value", value)
