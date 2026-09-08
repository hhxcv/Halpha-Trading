"""Read-only public market context for the plan decision surface."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Protocol, TypeAlias

from async_lru import alru_cache
from nautilus_trader.adapters.binance import get_cached_binance_http_client
from nautilus_trader.adapters.binance.common.enums import (
    BinanceAccountType,
    BinanceEnvironment,
    BinanceKlineInterval,
)
from nautilus_trader.adapters.binance.futures.http.market import (
    BinanceFuturesMarketHttpAPI,
)
from nautilus_trader.common.component import LiveClock
from nautilus_trader.indicators import LinearRegression, OnBalanceVolume, Swings
from nautilus_trader.core.nautilus_pyo3.network import HttpMethod
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from halpha.domain_values import canonical_decimal, decimal_from_string
from halpha.public_instrument_rules import (
    InstrumentRulesUnavailable,
    symbol_for_perpetual_instrument,
)
from halpha.planning.indicators import IndicatorBar, native_donchian_atr_snapshot


FIFTEEN_MINUTES_MS = 15 * 60 * 1000
ONE_MINUTE_MS = 60 * 1000
PUBLIC_MARKET_TIMEOUT_SECONDS = 10
PUBLIC_MARKET_TRANSIENT_READ_ATTEMPTS = 3
PUBLIC_MARKET_TRANSIENT_RETRY_DELAY_SECONDS = 0.25
PUBLIC_MARKET_MAX_SOURCE_AGE_SECONDS = 30
PUBLIC_MARKET_MAX_FUTURE_SKEW_SECONDS = 5
MAX_MARKET_WINDOW_BARS = 300
PUBLIC_MARKET_CONTEXT_CACHE_TTL_SECONDS = 5
PUBLIC_MARKET_RECENT_WINDOW_CACHE_TTL_SECONDS = 5
PUBLIC_MARKET_HISTORICAL_WINDOW_CACHE_TTL_SECONDS = 60 * 60
PUBLIC_FUNDING_HISTORY_CACHE_TTL_SECONDS = 60
FUNDING_RATE_HISTORY_PATH = "/fapi/v1/fundingRate"
FUNDING_RATE_HISTORY_SAMPLE_COUNT = 8
STOP_REFERENCE_METHOD_VERSION = "STOP_REFERENCE_MULTI_INTERVAL_V1"
STOP_STRUCTURE_ATR_BUFFER = Decimal("0.2")
STOP_SWING_ATR_BUFFER = Decimal("0.2")
STOP_TREND_ATR_BUFFER = Decimal("0.8")

MarketInterval: TypeAlias = Literal["1m", "5m", "15m", "1h", "4h", "1d"]
MARKET_INTERVALS: tuple[MarketInterval, ...] = (
    "1m",
    "5m",
    "15m",
    "1h",
    "4h",
    "1d",
)
MARKET_INTERVAL_MILLISECONDS: dict[MarketInterval, int] = {
    "1m": ONE_MINUTE_MS,
    "5m": 5 * ONE_MINUTE_MS,
    "15m": FIFTEEN_MINUTES_MS,
    "1h": 60 * ONE_MINUTE_MS,
    "4h": 4 * 60 * ONE_MINUTE_MS,
    "1d": 24 * 60 * ONE_MINUTE_MS,
}
BINANCE_KLINE_INTERVALS: dict[MarketInterval, BinanceKlineInterval] = {
    "1m": BinanceKlineInterval.MINUTE_1,
    "5m": BinanceKlineInterval.MINUTE_5,
    "15m": BinanceKlineInterval.MINUTE_15,
    "1h": BinanceKlineInterval.HOUR_1,
    "4h": BinanceKlineInterval.HOUR_4,
    "1d": BinanceKlineInterval.DAY_1,
}


class MarketStopReference(BaseModel):
    """One explainable, direction-neutral market level for stop planning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["STRUCTURE_ATR", "SWING_OBV", "TREND_ATR"]
    side: Literal["LOWER", "UPPER"]
    price: str
    interval: MarketInterval = "15m"
    lookback_bars: int
    atr_buffer_multiple: str
    volume_bias: Literal["POSITIVE", "NEGATIVE", "NEUTRAL"] | None = None
    trend_slope: str | None = None
    trend_r_squared: str | None = None
    method_version: Literal["STOP_REFERENCE_MULTI_INTERVAL_V1"] = (
        STOP_REFERENCE_METHOD_VERSION
    )

    @field_validator("price")
    @classmethod
    def price_is_positive_and_canonical(cls, value: str) -> str:
        return canonical_decimal(
            decimal_from_string(
                value,
                code="MARKET_STOP_REFERENCE_PRICE_INVALID",
                positive=True,
            )
        )

    @field_validator(
        "trend_slope",
        "trend_r_squared",
    )
    @classmethod
    def optional_values_are_canonical(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return canonical_decimal(
            decimal_from_string(value, code="MARKET_STOP_REFERENCE_VALUE_INVALID")
        )

    @field_validator("atr_buffer_multiple")
    @classmethod
    def atr_buffer_is_non_negative(cls, value: str) -> str:
        return canonical_decimal(
            decimal_from_string(
                value,
                code="MARKET_STOP_REFERENCE_ATR_BUFFER_INVALID",
                non_negative=True,
            )
        )

    @field_validator("lookback_bars")
    @classmethod
    def lookback_is_bounded(cls, value: int) -> int:
        if not 3 <= value <= 96:
            raise ValueError("MARKET_STOP_REFERENCE_LOOKBACK_INVALID")
        return value


class MarketContextUnavailable(RuntimeError):
    """Sanitized public-market read failure."""


async def _retry_transient_market_read(operation: Callable[[], Any]) -> Any:
    """Retry only an upstream timeout; validation failures remain final."""

    for attempt in range(PUBLIC_MARKET_TRANSIENT_READ_ATTEMPTS):
        try:
            return await operation()
        except MarketContextUnavailable as exc:
            if (
                "TIMEOUT" not in str(exc).upper()
                or attempt + 1 >= PUBLIC_MARKET_TRANSIENT_READ_ATTEMPTS
            ):
                raise
            await asyncio.sleep(
                PUBLIC_MARKET_TRANSIENT_RETRY_DELAY_SECONDS * (attempt + 1)
            )


class MarketFundingRateSample(BaseModel):
    """One venue-settled public funding rate used only for local estimates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    settled_at: datetime
    funding_rate: str

    @field_validator("settled_at")
    @classmethod
    def settled_at_is_aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("MARKET_FUNDING_RATE_TIMEZONE_REQUIRED")
        return value.astimezone(UTC)

    @field_validator("funding_rate")
    @classmethod
    def funding_rate_is_finite_and_bounded(cls, value: str) -> str:
        rate = decimal_from_string(value, code="MARKET_FUNDING_RATE_INVALID")
        if abs(rate) > 1:
            raise ValueError("MARKET_FUNDING_RATE_INVALID")
        return canonical_decimal(rate)


class MarketFundingRateHistory(BaseModel):
    """Recent funding-rate observations from the current public market source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_ref: str
    source: str
    source_cutoff: datetime
    samples: tuple[MarketFundingRateSample, ...]
    average_funding_rate: str
    average_interval_seconds: int

    @field_validator("source_cutoff")
    @classmethod
    def source_cutoff_is_aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("MARKET_FUNDING_HISTORY_TIMEZONE_REQUIRED")
        return value.astimezone(UTC)

    @field_validator("average_funding_rate")
    @classmethod
    def average_rate_is_finite_and_bounded(cls, value: str) -> str:
        rate = decimal_from_string(value, code="MARKET_FUNDING_HISTORY_AVERAGE_INVALID")
        if abs(rate) > 1:
            raise ValueError("MARKET_FUNDING_HISTORY_AVERAGE_INVALID")
        return canonical_decimal(rate)

    @model_validator(mode="after")
    def history_is_ordered_and_complete(self) -> MarketFundingRateHistory:
        if not 3 <= len(self.samples) <= FUNDING_RATE_HISTORY_SAMPLE_COUNT:
            raise ValueError("MARKET_FUNDING_HISTORY_SAMPLE_COUNT_INVALID")
        if self.average_interval_seconds <= 0:
            raise ValueError("MARKET_FUNDING_HISTORY_INTERVAL_INVALID")
        settled = [sample.settled_at for sample in self.samples]
        if settled != sorted(settled) or len(set(settled)) != len(settled):
            raise ValueError("MARKET_FUNDING_HISTORY_ORDER_INVALID")
        return self


class MarketContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_ref: str
    source: str
    source_cutoff: datetime
    latest_closed_1m_at: datetime
    latest_closed_15m_at: datetime
    latest_closed_stop_reference_at: datetime
    channel_lookback_15m: int
    stop_reference_interval: MarketInterval
    bid_price: str
    ask_price: str
    reference_price: str
    latest_close_1m: str
    latest_volume_1m: str
    latest_trade_count_1m: int
    latest_close_15m: str
    channel_upper: str
    channel_lower: str
    atr_14: str
    stop_reference_atr_14: str
    long_breakout_gap_pct: str
    short_breakout_gap_pct: str
    stop_references: tuple[MarketStopReference, ...] = ()


class MarketBar(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    open_at: datetime
    close_at: datetime
    open: str
    high: str
    low: str
    close: str
    volume: str

    @field_validator("open_at", "close_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("MARKET_BAR_TIMEZONE_REQUIRED")
        return value.astimezone(UTC)

    @field_validator("open", "high", "low", "close")
    @classmethod
    def prices_are_positive_and_canonical(cls, value: str) -> str:
        return canonical_decimal(
            decimal_from_string(
                value,
                code="MARKET_BAR_PRICE_INVALID",
                positive=True,
            )
        )

    @field_validator("volume")
    @classmethod
    def volume_is_non_negative_and_canonical(cls, value: str) -> str:
        return canonical_decimal(
            decimal_from_string(
                value,
                code="MARKET_BAR_VOLUME_INVALID",
                non_negative=True,
            )
        )

    @model_validator(mode="after")
    def bar_invariants_hold(self) -> MarketBar:
        open_price = Decimal(self.open)
        high = Decimal(self.high)
        low = Decimal(self.low)
        close = Decimal(self.close)
        if (
            self.close_at <= self.open_at
            or high < max(open_price, close)
            or low > min(open_price, close)
            or high < low
        ):
            raise ValueError("MARKET_BAR_INVALID")
        return self


class MarketWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_ref: str
    interval: MarketInterval
    source: str
    source_cutoff: datetime
    bars: tuple[MarketBar, ...]

    @field_validator("source_cutoff")
    @classmethod
    def source_cutoff_is_aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("MARKET_WINDOW_TIMEZONE_REQUIRED")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def window_is_complete_and_contiguous(self) -> MarketWindow:
        if not self.bars or len(self.bars) > MAX_MARKET_WINDOW_BARS:
            raise ValueError("MARKET_WINDOW_BAR_COUNT_INVALID")
        interval = timedelta(milliseconds=MARKET_INTERVAL_MILLISECONDS[self.interval])
        for index, bar in enumerate(self.bars):
            if bar.close_at != bar.open_at + interval:
                raise ValueError("MARKET_WINDOW_BAR_BOUNDARY_INVALID")
            if index > 0 and bar.open_at != self.bars[index - 1].close_at:
                raise ValueError("MARKET_WINDOW_NOT_CONTIGUOUS")
        latest = self.bars[-1]
        if not latest.open_at <= self.source_cutoff <= latest.close_at:
            raise ValueError("MARKET_WINDOW_SOURCE_CUTOFF_INVALID")
        return self


class MarketContextProvider(Protocol):
    async def fetch(
        self,
        instrument_ref: str,
        lookback: int,
        stop_reference_interval: MarketInterval = "15m",
    ) -> MarketContext: ...

    async def fetch_window(
        self,
        instrument_ref: str,
        interval: MarketInterval,
        start_at: datetime,
        end_at: datetime,
    ) -> MarketWindow: ...

    async def fetch_funding_rate_history(
        self,
        instrument_ref: str,
    ) -> MarketFundingRateHistory: ...


class BinanceMarketApi(Protocol):
    async def query_ticker_book(
        self,
        symbol: str | None = None,
        symbols: list[str] | None = None,
    ) -> list[Any]: ...

    async def query_klines(
        self,
        symbol: str,
        interval: BinanceKlineInterval,
        limit: int | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[Any]: ...


def binance_public_market_identity(
    profile: str,
) -> tuple[BinanceEnvironment, str]:
    """Resolve one market-data identity for the complete runtime profile."""

    if profile == "BINANCE_DEMO":
        return BinanceEnvironment.DEMO, "BINANCE_DEMO_PUBLIC"
    if profile in {"BINANCE_LIVE_READ_ONLY", "BINANCE_LIVE_WRITE"}:
        return BinanceEnvironment.LIVE, "BINANCE_LIVE_PUBLIC"
    raise ValueError("PUBLIC_MARKET_PROFILE_UNSUPPORTED")


def _market_stop_references(
    *,
    bars: tuple[IndicatorBar, ...],
    interval: MarketInterval,
    channel_lookback: int,
    channel_lower: Decimal,
    channel_upper: Decimal,
    atr: Decimal,
) -> tuple[MarketStopReference, ...]:
    """Derive bounded stop-planning levels with Nautilus indicators.

    The result is direction-neutral market evidence. The planning UI decides
    whether a lower or upper level is adverse to the selected direction and
    converts an explicitly selected level into the existing relative stop
    distance contract.
    """

    if atr <= 0 or len(bars) < 15:
        return ()

    trend_period = min(20, len(bars))
    volume_period = min(14, len(bars))
    regression = LinearRegression(trend_period)
    swings = Swings(3)
    obv = OnBalanceVolume(volume_period)
    for item in bars:
        open_price = float(item.open)
        high = float(item.high)
        low = float(item.low)
        close = float(item.close)
        volume = float(item.volume)
        timestamp = datetime.fromtimestamp(item.ts_event_ns / 1_000_000_000, tz=UTC)
        regression.update_raw(close)
        swings.update_raw(high, low, timestamp)
        obv.update_raw(open_price, close, volume)

    references: list[MarketStopReference] = []

    def append_pair(
        *,
        kind: Literal["STRUCTURE_ATR", "SWING_OBV", "TREND_ATR"],
        lower: Decimal,
        upper: Decimal,
        lookback_bars: int,
        atr_buffer_multiple: Decimal,
        volume_bias: Literal["POSITIVE", "NEGATIVE", "NEUTRAL"] | None = None,
        trend_slope: Decimal | None = None,
        trend_r_squared: Decimal | None = None,
    ) -> None:
        if lower <= 0 or upper <= lower:
            return
        common = {
            "kind": kind,
            "interval": interval,
            "lookback_bars": lookback_bars,
            "atr_buffer_multiple": canonical_decimal(atr_buffer_multiple),
            "volume_bias": volume_bias,
            "trend_slope": (
                canonical_decimal(trend_slope) if trend_slope is not None else None
            ),
            "trend_r_squared": (
                canonical_decimal(trend_r_squared)
                if trend_r_squared is not None
                else None
            ),
        }
        references.extend(
            (
                MarketStopReference(
                    **common,
                    side="LOWER",
                    price=canonical_decimal(lower),
                ),
                MarketStopReference(
                    **common,
                    side="UPPER",
                    price=canonical_decimal(upper),
                ),
            )
        )

    append_pair(
        kind="STRUCTURE_ATR",
        lower=channel_lower - atr * STOP_STRUCTURE_ATR_BUFFER,
        upper=channel_upper + atr * STOP_STRUCTURE_ATR_BUFFER,
        lookback_bars=channel_lookback,
        atr_buffer_multiple=STOP_STRUCTURE_ATR_BUFFER,
    )

    obv_value = Decimal(str(obv.value))
    volume_bias: Literal["POSITIVE", "NEGATIVE", "NEUTRAL"] = (
        "POSITIVE"
        if obv_value.is_finite() and obv_value > 0
        else "NEGATIVE"
        if obv_value.is_finite() and obv_value < 0
        else "NEUTRAL"
    )
    swing_low = Decimal(str(swings.low_price))
    swing_high = Decimal(str(swings.high_price))
    if swings.initialized and swing_low.is_finite() and swing_high.is_finite():
        append_pair(
            kind="SWING_OBV",
            lower=swing_low - atr * STOP_SWING_ATR_BUFFER,
            upper=swing_high + atr * STOP_SWING_ATR_BUFFER,
            lookback_bars=len(bars),
            atr_buffer_multiple=STOP_SWING_ATR_BUFFER,
            volume_bias=volume_bias,
        )

    regression_value = Decimal(str(regression.value))
    regression_slope = Decimal(str(regression.slope))
    regression_r_squared = Decimal(str(regression.R2))
    if all(
        value.is_finite()
        for value in (regression_value, regression_slope, regression_r_squared)
    ):
        append_pair(
            kind="TREND_ATR",
            lower=regression_value - atr * STOP_TREND_ATR_BUFFER,
            upper=regression_value + atr * STOP_TREND_ATR_BUFFER,
            lookback_bars=trend_period,
            atr_buffer_multiple=STOP_TREND_ATR_BUFFER,
            trend_slope=regression_slope,
            trend_r_squared=regression_r_squared,
        )

    return tuple(references)


class BinancePublicMarketContext:
    """Read environment-qualified Binance market data for the planning UI."""

    def __init__(
        self,
        profile: str,
        *,
        proxy_url: str | None = None,
        market_api: BinanceMarketApi | None = None,
        funding_rate_query: Callable[[str, int], Any] | None = None,
        observed_at_provider: Callable[[], datetime] | None = None,
    ) -> None:
        environment, self._source = binance_public_market_identity(profile)
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
            funding_rate_query = funding_rate_query or self._build_funding_rate_query(client)
        self._market_api = market_api
        self._funding_rate_query = funding_rate_query
        self._observed_at_provider = observed_at_provider or (lambda: datetime.now(UTC))
        # async-lru supplies bounded TTL caching and single-flight behavior: all
        # same-key callers await one venue request rather than multiplying it by
        # the number of open browser windows. Keep current and immutable history
        # separate so a live candle never inherits the longer historical TTL.
        self._fetch_cached = alru_cache(
            maxsize=32,
            ttl=PUBLIC_MARKET_CONTEXT_CACHE_TTL_SECONDS,
        )(self._fetch_uncached)
        self._fetch_recent_window_cached = alru_cache(
            maxsize=128,
            ttl=PUBLIC_MARKET_RECENT_WINDOW_CACHE_TTL_SECONDS,
        )(self._fetch_window_uncached)
        self._fetch_historical_window_cached = alru_cache(
            maxsize=256,
            ttl=PUBLIC_MARKET_HISTORICAL_WINDOW_CACHE_TTL_SECONDS,
        )(self._fetch_window_uncached)
        self._fetch_funding_history_cached = alru_cache(
            maxsize=32,
            ttl=PUBLIC_FUNDING_HISTORY_CACHE_TTL_SECONDS,
        )(self._fetch_funding_rate_history_uncached)

    @staticmethod
    def _build_funding_rate_query(client: Any) -> Callable[[str, int], Any]:
        async def query(symbol: str, limit: int) -> Any:
            raw = await client.send_request(
                http_method=HttpMethod.GET,
                url_path=FUNDING_RATE_HISTORY_PATH,
                payload={"symbol": symbol, "limit": str(limit)},
                ratelimiter_keys=[
                    f"binance:{FUNDING_RATE_HISTORY_PATH}",
                    "binance:global",
                ],
            )
            return json.loads(raw)

        return query

    async def fetch(
        self,
        instrument_ref: str,
        lookback: int,
        stop_reference_interval: MarketInterval = "15m",
    ) -> MarketContext:
        return await _retry_transient_market_read(
            lambda: self._fetch_cached(
                instrument_ref,
                lookback,
                stop_reference_interval,
            )
        )

    async def fetch_funding_rate_history(
        self,
        instrument_ref: str,
    ) -> MarketFundingRateHistory:
        return await _retry_transient_market_read(
            lambda: self._fetch_funding_history_cached(instrument_ref)
        )

    async def _fetch_funding_rate_history_uncached(
        self,
        instrument_ref: str,
    ) -> MarketFundingRateHistory:
        try:
            symbol = symbol_for_perpetual_instrument(instrument_ref)
        except InstrumentRulesUnavailable:
            raise MarketContextUnavailable("MARKET_FUNDING_HISTORY_INSTRUMENT_UNSUPPORTED")
        if self._funding_rate_query is None:
            raise MarketContextUnavailable("MARKET_FUNDING_HISTORY_UNAVAILABLE")
        try:
            raw_samples = await asyncio.wait_for(
                self._funding_rate_query(symbol, FUNDING_RATE_HISTORY_SAMPLE_COUNT),
                timeout=PUBLIC_MARKET_TIMEOUT_SECONDS,
            )
            if not isinstance(raw_samples, list):
                raise ValueError("funding history response invalid")
            samples = tuple(
                MarketFundingRateSample(
                    settled_at=datetime.fromtimestamp(
                        int(item["fundingTime"]) / 1000,
                        tz=UTC,
                    ),
                    funding_rate=str(item["fundingRate"]),
                )
                for item in raw_samples
                if isinstance(item, dict) and item.get("symbol") == symbol
            )
            if len(samples) < 3:
                raise ValueError("funding history incomplete")
            ordered_samples = tuple(sorted(samples, key=lambda item: item.settled_at))
            intervals = [
                int((later.settled_at - earlier.settled_at).total_seconds())
                for earlier, later in zip(ordered_samples, ordered_samples[1:])
            ]
            if any(interval <= 0 for interval in intervals):
                raise ValueError("funding history timestamps invalid")
            observed_at = self._observed_at_provider()
            if observed_at.utcoffset() is None:
                raise ValueError("funding history observation timezone invalid")
            observed_at = observed_at.astimezone(UTC)
            latest_settlement = ordered_samples[-1].settled_at
            if (
                observed_at - latest_settlement > timedelta(days=2)
                or latest_settlement - observed_at > timedelta(minutes=5)
            ):
                raise ValueError("funding history stale")
            average_rate = sum(
                (Decimal(item.funding_rate) for item in ordered_samples),
                Decimal(0),
            ) / len(ordered_samples)
            average_interval = sum(intervals) // len(intervals)
            return MarketFundingRateHistory(
                instrument_ref=instrument_ref,
                source=self._source,
                source_cutoff=observed_at,
                samples=ordered_samples,
                average_funding_rate=canonical_decimal(average_rate),
                average_interval_seconds=average_interval,
            )
        except MarketContextUnavailable:
            raise
        except Exception as exc:
            raise MarketContextUnavailable(
                f"MARKET_FUNDING_HISTORY_READ_FAILED_{type(exc).__name__.upper()}"
            ) from None

    async def _fetch_uncached(
        self,
        instrument_ref: str,
        lookback: int,
        stop_reference_interval: MarketInterval,
    ) -> MarketContext:
        try:
            symbol = symbol_for_perpetual_instrument(instrument_ref)
        except InstrumentRulesUnavailable:
            raise MarketContextUnavailable("MARKET_CONTEXT_INSTRUMENT_UNSUPPORTED")
        if not 4 <= lookback <= 96:
            raise MarketContextUnavailable("MARKET_CONTEXT_LOOKBACK_INVALID")
        try:
            books = await asyncio.wait_for(
                self._market_api.query_ticker_book(symbol=symbol),
                timeout=PUBLIC_MARKET_TIMEOUT_SECONDS,
            )
            book = next(
                (item for item in books if getattr(item, "symbol", None) == symbol),
                None,
            )
            if book is None or getattr(book, "time", None) is None:
                raise ValueError("public market book invalid")
            server_time_ms = int(book.time)
            indicator_bar_count = max(lookback, 15)
            latest_open_ms = (
                server_time_ms // FIFTEEN_MINUTES_MS - 1
            ) * FIFTEEN_MINUTES_MS
            start_ms = (
                latest_open_ms
                - (indicator_bar_count - 1) * FIFTEEN_MINUTES_MS
            )
            end_ms = latest_open_ms + FIFTEEN_MINUTES_MS - 1
            latest_1m_open_ms = (
                server_time_ms // ONE_MINUTE_MS - 1
            ) * ONE_MINUTE_MS
            stop_interval_ms = MARKET_INTERVAL_MILLISECONDS[
                stop_reference_interval
            ]
            stop_latest_open_ms = (
                server_time_ms // stop_interval_ms - 1
            ) * stop_interval_ms
            stop_start_ms = (
                stop_latest_open_ms
                - (indicator_bar_count - 1) * stop_interval_ms
            )
            stop_end_ms = stop_latest_open_ms + stop_interval_ms - 1
            requests = [
                asyncio.wait_for(
                    self._market_api.query_klines(
                        symbol=symbol,
                        interval=BinanceKlineInterval.MINUTE_15,
                        limit=indicator_bar_count,
                        start_time=start_ms,
                        end_time=end_ms,
                    ),
                    timeout=PUBLIC_MARKET_TIMEOUT_SECONDS,
                ),
                asyncio.wait_for(
                    self._market_api.query_klines(
                        symbol=symbol,
                        interval=BinanceKlineInterval.MINUTE_1,
                        limit=1,
                        start_time=latest_1m_open_ms,
                        end_time=latest_1m_open_ms + ONE_MINUTE_MS - 1,
                    ),
                    timeout=PUBLIC_MARKET_TIMEOUT_SECONDS,
                ),
            ]
            if stop_reference_interval != "15m":
                requests.append(
                    asyncio.wait_for(
                        self._market_api.query_klines(
                            symbol=symbol,
                            interval=BINANCE_KLINE_INTERVALS[
                                stop_reference_interval
                            ],
                            limit=indicator_bar_count,
                            start_time=stop_start_ms,
                            end_time=stop_end_ms,
                        ),
                        timeout=PUBLIC_MARKET_TIMEOUT_SECONDS,
                    )
                )
            results = await asyncio.gather(*requests)
            bars, latest_1m_bars = results[:2]
            stop_bars = bars if stop_reference_interval == "15m" else results[2]
            if len(bars) != indicator_bar_count:
                raise ValueError("public market sample incomplete")
            if (
                len(latest_1m_bars) != 1
                or int(latest_1m_bars[0].open_time) != latest_1m_open_ms
            ):
                raise ValueError("public market latest 1m bar invalid")
            expected_open_times = [
                start_ms + index * FIFTEEN_MINUTES_MS
                for index in range(indicator_bar_count)
            ]
            if [int(bar.open_time) for bar in bars] != expected_open_times:
                raise ValueError("public market bars not contiguous")
            expected_stop_open_times = [
                stop_start_ms + index * stop_interval_ms
                for index in range(indicator_bar_count)
            ]
            if (
                len(stop_bars) != indicator_bar_count
                or [int(bar.open_time) for bar in stop_bars]
                != expected_stop_open_times
            ):
                raise ValueError("public market stop-reference bars not contiguous")
            indicator_bars = tuple(
                IndicatorBar(
                    open=str(bar.open),
                    high=str(bar.high),
                    low=str(bar.low),
                    close=str(bar.close),
                    volume=str(bar.volume),
                    ts_event_ns=(int(bar.close_time) + 1) * 1_000_000,
                )
                for bar in bars
            )
            stop_indicator_bars = tuple(
                IndicatorBar(
                    open=str(bar.open),
                    high=str(bar.high),
                    low=str(bar.low),
                    close=str(bar.close),
                    volume=str(bar.volume),
                    ts_event_ns=(int(bar.close_time) + 1) * 1_000_000,
                )
                for bar in stop_bars
            )
            indicators = native_donchian_atr_snapshot(
                instrument_id=f"{instrument_ref}.BINANCE",
                lookback=lookback,
                bars=indicator_bars,
            )
            if not indicators.initialized:
                raise ValueError("public market indicators not initialized")
            stop_indicators = (
                indicators
                if stop_reference_interval == "15m"
                else native_donchian_atr_snapshot(
                    instrument_id=f"{instrument_ref}.BINANCE",
                    lookback=lookback,
                    bars=stop_indicator_bars,
                )
            )
            if not stop_indicators.initialized:
                raise ValueError("public market stop-reference indicators not initialized")
            bid = Decimal(str(book.bidPrice))
            ask = Decimal(str(book.askPrice))
            reference = (bid + ask) / Decimal(2)
            upper = Decimal(indicators.upper)
            lower = Decimal(indicators.lower)
            atr = Decimal(indicators.atr)
            stop_upper = Decimal(stop_indicators.upper)
            stop_lower = Decimal(stop_indicators.lower)
            stop_atr = Decimal(stop_indicators.atr)
            latest_volume_1m = Decimal(str(latest_1m_bars[0].volume))
            latest_trade_count_1m = int(latest_1m_bars[0].trades_count)
            source_cutoff = datetime.fromtimestamp(
                server_time_ms / 1000,
                tz=UTC,
            )
            observed_at = self._observed_at_provider()
            if observed_at.utcoffset() is None:
                raise ValueError("public market observation timezone invalid")
            observed_at = observed_at.astimezone(UTC)
            source_age = observed_at - source_cutoff
            if (
                bid <= 0
                or ask <= 0
                or ask < bid
                or upper <= lower
                or atr <= 0
                or stop_upper <= stop_lower
                or stop_atr <= 0
                or latest_volume_1m < 0
                or latest_trade_count_1m < 0
                or source_age
                > timedelta(seconds=PUBLIC_MARKET_MAX_SOURCE_AGE_SECONDS)
                or source_age
                < -timedelta(seconds=PUBLIC_MARKET_MAX_FUTURE_SKEW_SECONDS)
            ):
                raise ValueError("public market values invalid")
            return MarketContext(
                instrument_ref=instrument_ref,
                source=self._source,
                source_cutoff=source_cutoff,
                latest_closed_1m_at=datetime.fromtimestamp(
                    (int(latest_1m_bars[0].close_time) + 1) / 1000,
                    tz=UTC,
                ),
                latest_closed_15m_at=datetime.fromtimestamp(
                    (int(bars[-1].close_time) + 1) / 1000,
                    tz=UTC,
                ),
                latest_closed_stop_reference_at=datetime.fromtimestamp(
                    (int(stop_bars[-1].close_time) + 1) / 1000,
                    tz=UTC,
                ),
                channel_lookback_15m=lookback,
                stop_reference_interval=stop_reference_interval,
                bid_price=canonical_decimal(bid),
                ask_price=canonical_decimal(ask),
                reference_price=canonical_decimal(reference),
                latest_close_1m=canonical_decimal(
                    Decimal(str(latest_1m_bars[0].close))
                ),
                latest_volume_1m=canonical_decimal(latest_volume_1m),
                latest_trade_count_1m=latest_trade_count_1m,
                latest_close_15m=canonical_decimal(Decimal(str(bars[-1].close))),
                channel_upper=canonical_decimal(upper),
                channel_lower=canonical_decimal(lower),
                atr_14=canonical_decimal(atr),
                stop_reference_atr_14=canonical_decimal(stop_atr),
                long_breakout_gap_pct=canonical_decimal(
                    (upper - reference) / reference * Decimal(100)
                ),
                short_breakout_gap_pct=canonical_decimal(
                    (reference - lower) / reference * Decimal(100)
                ),
                stop_references=_market_stop_references(
                    bars=stop_indicator_bars,
                    interval=stop_reference_interval,
                    channel_lookback=lookback,
                    channel_lower=stop_lower,
                    channel_upper=stop_upper,
                    atr=stop_atr,
                ),
            )
        except MarketContextUnavailable:
            raise
        except Exception as exc:
            raise MarketContextUnavailable(
                f"MARKET_CONTEXT_READ_FAILED_{type(exc).__name__.upper()}"
            ) from None

    async def fetch_window(
        self,
        instrument_ref: str,
        interval: MarketInterval,
        start_at: datetime,
        end_at: datetime,
    ) -> MarketWindow:
        if start_at.utcoffset() is None or end_at.utcoffset() is None:
            raise MarketContextUnavailable("MARKET_WINDOW_TIMEZONE_REQUIRED")
        interval_ms = MARKET_INTERVAL_MILLISECONDS[interval]
        start_ms = int(start_at.timestamp() * 1000) // interval_ms * interval_ms
        end_ms = int(end_at.timestamp() * 1000) // interval_ms * interval_ms
        normalized_start = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
        normalized_end = datetime.fromtimestamp(end_ms / 1000, tz=UTC)
        observed_at = self._observed_at_provider()
        historical = (
            observed_at.utcoffset() is not None
            and normalized_end + timedelta(milliseconds=interval_ms)
            <= observed_at.astimezone(UTC)
        )
        fetcher = (
            self._fetch_historical_window_cached
            if historical
            else self._fetch_recent_window_cached
        )
        return await _retry_transient_market_read(
            lambda: fetcher(
                instrument_ref,
                interval,
                normalized_start,
                normalized_end,
            )
        )

    async def _fetch_window_uncached(
        self,
        instrument_ref: str,
        interval: MarketInterval,
        start_at: datetime,
        end_at: datetime,
    ) -> MarketWindow:
        try:
            symbol = symbol_for_perpetual_instrument(instrument_ref)
        except InstrumentRulesUnavailable:
            raise MarketContextUnavailable("MARKET_CONTEXT_INSTRUMENT_UNSUPPORTED")
        interval_ms = MARKET_INTERVAL_MILLISECONDS[interval]
        native_interval = BINANCE_KLINE_INTERVALS[interval]
        start_ms = int(start_at.timestamp() * 1000)
        end_ms = int(end_at.timestamp() * 1000)
        count = (end_ms - start_ms) // interval_ms + 1
        if count <= 0 or count > MAX_MARKET_WINDOW_BARS:
            raise MarketContextUnavailable("MARKET_WINDOW_RANGE_INVALID")
        try:
            bars = await asyncio.wait_for(
                self._market_api.query_klines(
                    symbol=symbol,
                    interval=native_interval,
                    limit=count,
                    start_time=start_ms,
                    end_time=end_ms + interval_ms - 1,
                ),
                timeout=PUBLIC_MARKET_TIMEOUT_SECONDS,
            )
            expected_open_times = [
                start_ms + index * interval_ms for index in range(count)
            ]
            expected_close_times = [
                open_time + interval_ms - 1 for open_time in expected_open_times
            ]
            if (
                len(bars) != count
                or [int(bar.open_time) for bar in bars] != expected_open_times
                or [int(bar.close_time) for bar in bars] != expected_close_times
            ):
                raise ValueError("public market window incomplete")
            try:
                normalized = tuple(
                    MarketBar(
                        open_at=datetime.fromtimestamp(
                            int(bar.open_time) / 1000,
                            tz=UTC,
                        ),
                        close_at=datetime.fromtimestamp(
                            (int(bar.close_time) + 1) / 1000,
                            tz=UTC,
                        ),
                        open=str(bar.open),
                        high=str(bar.high),
                        low=str(bar.low),
                        close=str(bar.close),
                        volume=str(bar.volume),
                    )
                    for bar in bars
                )
            except ValueError:
                raise ValueError("public market window values invalid") from None
            observed_at = self._observed_at_provider()
            if observed_at.utcoffset() is None:
                raise ValueError("public market observation timezone invalid")
            observed_at = observed_at.astimezone(UTC)
            if normalized[-1].open_at > observed_at + timedelta(
                seconds=PUBLIC_MARKET_MAX_FUTURE_SKEW_SECONDS
            ):
                raise ValueError("public market window is in the future")
            return MarketWindow(
                instrument_ref=instrument_ref,
                interval=interval,
                source=self._source,
                source_cutoff=min(
                    normalized[-1].close_at,
                    observed_at,
                ),
                bars=normalized,
            )
        except MarketContextUnavailable:
            raise
        except Exception as exc:
            raise MarketContextUnavailable(
                f"MARKET_WINDOW_READ_FAILED_{type(exc).__name__.upper()}"
            ) from None

    async def close(self) -> None:
        """Release cache timers and any in-flight read tasks at App shutdown."""

        await asyncio.gather(
            self._fetch_cached.cache_close(),
            self._fetch_recent_window_cached.cache_close(),
            self._fetch_historical_window_cached.cache_close(),
        )
