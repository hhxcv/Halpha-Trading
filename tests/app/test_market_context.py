from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment

import halpha.public_market as public_market_module
from halpha.public_market import (
    BINANCE_KLINE_INTERVALS,
    BinancePublicMarketContext,
    MARKET_INTERVAL_MILLISECONDS,
    MarketBar,
    MarketContextUnavailable,
    MarketWindow,
)


class FakeMarketApi:
    def __init__(self, *, complete: bool = True) -> None:
        self.complete = complete
        self.server_time_ms = 1_800_000_100_000
        self.ticker_query_count = 0
        self.kline_query_count = 0

    async def query_ticker_book(self, symbol=None, symbols=None):
        self.ticker_query_count += 1
        assert symbol == "BTCUSDT"
        assert symbols is None
        return [
            SimpleNamespace(
                symbol=symbol,
                bidPrice="119",
                askPrice="121",
                time=self.server_time_ms,
            )
        ]

    async def query_klines(
        self,
        symbol,
        interval,
        limit=None,
        start_time=None,
        end_time=None,
    ):
        self.kline_query_count += 1
        assert symbol == "BTCUSDT"
        assert end_time is not None
        count = int(limit)
        start = int(start_time)
        interval_ms = next(
            MARKET_INTERVAL_MILLISECONDS[key]
            for key, native in BINANCE_KLINE_INTERVALS.items()
            if interval is native
        )
        if not self.complete:
            count -= 1
        return [
            SimpleNamespace(
                open_time=start + index * interval_ms,
                open=str(100 + index),
                high=str(102 + index),
                low=str(98 + index),
                close=str(101 + index),
                volume="10",
                close_time=start + (index + 1) * interval_ms - 1,
                trades_count=7,
            )
            for index in range(count)
        ]


def _observed_after(api: FakeMarketApi, seconds: int = 1) -> datetime:
    return datetime.fromtimestamp(api.server_time_ms / 1000, tz=UTC) + timedelta(
        seconds=seconds
    )


def test_public_market_context_uses_closed_contiguous_bars_and_exact_prices() -> None:
    api = FakeMarketApi()
    provider = BinancePublicMarketContext(
        "BINANCE_DEMO",
        market_api=api,
        observed_at_provider=lambda: _observed_after(api),
    )

    context = asyncio.run(provider.fetch("BTCUSDT-PERP", 20))

    assert context.source == "BINANCE_DEMO_PUBLIC"
    assert context.reference_price == "120"
    assert context.bid_price == "119"
    assert context.ask_price == "121"
    assert context.latest_close_1m == "101"
    assert context.latest_volume_1m == "10"
    assert context.latest_trade_count_1m == 7
    assert context.latest_close_15m == "120"
    assert context.channel_upper == "121"
    assert context.channel_lower == "98"
    assert {
        (reference.kind, reference.side)
        for reference in context.stop_references
    } == {
        ("STRUCTURE_ATR", "LOWER"),
        ("STRUCTURE_ATR", "UPPER"),
        ("TREND_ATR", "LOWER"),
        ("TREND_ATR", "UPPER"),
    }
    assert all(
        reference.method_version == "STOP_REFERENCE_MULTI_INTERVAL_V1"
        and Decimal(reference.price) > 0
        for reference in context.stop_references
    )
    assert Decimal(context.long_breakout_gap_pct) == Decimal(1) / Decimal(120) * 100
    assert Decimal(context.short_breakout_gap_pct) == Decimal(22) / Decimal(120) * 100


def test_public_market_context_reads_recent_settled_funding_rates_for_local_estimates() -> None:
    api = FakeMarketApi()

    async def funding_history(symbol: str, limit: int):
        assert symbol == "BTCUSDT"
        assert limit == 8
        base = api.server_time_ms - 7 * 8 * 60 * 60 * 1000
        return [
            {
                "symbol": symbol,
                "fundingTime": base + index * 8 * 60 * 60 * 1000,
                "fundingRate": rate,
            }
            for index, rate in enumerate(
                ("0.0001", "0.0002", "-0.0001", "0.0001", "0.0001", "0.0001", "0", "0.0001")
            )
        ]

    provider = BinancePublicMarketContext(
        "BINANCE_DEMO",
        market_api=api,
        funding_rate_query=funding_history,
        observed_at_provider=lambda: _observed_after(api),
    )

    history = asyncio.run(provider.fetch_funding_rate_history("BTCUSDT-PERP"))

    assert history.source == "BINANCE_DEMO_PUBLIC"
    assert len(history.samples) == 8
    assert history.average_funding_rate == "0.000075"
    assert history.average_interval_seconds == 28_800


@pytest.mark.parametrize("interval", ("1m", "5m", "1h", "4h", "1d"))
def test_public_market_context_calculates_stop_references_on_selected_interval(
    interval: str,
) -> None:
    api = FakeMarketApi()
    provider = BinancePublicMarketContext(
        "BINANCE_DEMO",
        market_api=api,
        observed_at_provider=lambda: _observed_after(api),
    )

    context = asyncio.run(provider.fetch("BTCUSDT-PERP", 20, interval))

    assert context.stop_reference_interval == interval
    cutoff_milliseconds = int(
        context.latest_closed_stop_reference_at.timestamp() * 1000
    )
    assert cutoff_milliseconds % MARKET_INTERVAL_MILLISECONDS[interval] == 0
    assert context.stop_reference_atr_14 == context.atr_14
    assert context.stop_references
    assert all(reference.interval == interval for reference in context.stop_references)


def test_public_market_context_adds_volume_qualified_swing_references() -> None:
    class OscillatingMarketApi(FakeMarketApi):
        async def query_klines(self, *args, **kwargs):
            bars = await super().query_klines(*args, **kwargs)
            if len(bars) <= 1:
                return bars
            pattern = (100, 106, 102, 109, 101, 111, 104, 113)
            for index, bar in enumerate(bars):
                center = Decimal(pattern[index % len(pattern)])
                bar.open = str(center - Decimal("0.5"))
                bar.close = str(center + Decimal("0.5"))
                bar.high = str(center + 2)
                bar.low = str(center - 2)
                bar.volume = str(10 + index * 2)
            return bars

    api = OscillatingMarketApi()
    provider = BinancePublicMarketContext(
        "BINANCE_DEMO",
        market_api=api,
        observed_at_provider=lambda: _observed_after(api),
    )

    context = asyncio.run(provider.fetch("BTCUSDT-PERP", 20))

    swing_references = [
        reference
        for reference in context.stop_references
        if reference.kind == "SWING_OBV"
    ]
    assert {reference.side for reference in swing_references} == {"LOWER", "UPPER"}
    assert all(reference.volume_bias == "POSITIVE" for reference in swing_references)


def test_public_market_context_keeps_atr_warmup_for_short_channel() -> None:
    api = FakeMarketApi()
    provider = BinancePublicMarketContext(
        "BINANCE_DEMO",
        market_api=api,
        observed_at_provider=lambda: _observed_after(api),
    )

    context = asyncio.run(provider.fetch("BTCUSDT-PERP", 4))

    assert context.channel_lookback_15m == 4
    assert context.latest_close_15m == "115"
    assert context.channel_upper == "116"
    assert context.channel_lower == "109"
    assert Decimal(context.long_breakout_gap_pct) == Decimal(-4) / Decimal(120) * 100
    assert Decimal(context.short_breakout_gap_pct) == Decimal(11) / Decimal(120) * 100


def test_public_market_context_rejects_incomplete_window() -> None:
    api = FakeMarketApi(complete=False)
    provider = BinancePublicMarketContext(
        "BINANCE_DEMO",
        market_api=api,
        observed_at_provider=lambda: _observed_after(api),
    )

    with pytest.raises(
        MarketContextUnavailable,
        match="MARKET_CONTEXT_READ_FAILED_VALUEERROR",
    ):
        asyncio.run(provider.fetch("BTCUSDT-PERP", 20))


def test_public_market_context_retries_a_transient_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FlakyMarketApi(FakeMarketApi):
        async def query_ticker_book(self, *args, **kwargs):
            if self.ticker_query_count == 0:
                self.ticker_query_count += 1
                raise TimeoutError()
            return await super().query_ticker_book(*args, **kwargs)

    monkeypatch.setattr(
        public_market_module,
        "PUBLIC_MARKET_TRANSIENT_RETRY_DELAY_SECONDS",
        0,
    )
    api = FlakyMarketApi()
    provider = BinancePublicMarketContext(
        "BINANCE_DEMO",
        market_api=api,
        observed_at_provider=lambda: _observed_after(api),
    )

    context = asyncio.run(provider.fetch("BTCUSDT-PERP", 20))

    assert context.reference_price == "120"
    assert api.ticker_query_count == 2


@pytest.mark.parametrize("offset_seconds", [31, -6])
def test_public_market_context_rejects_stale_or_future_book(
    offset_seconds: int,
) -> None:
    api = FakeMarketApi()
    provider = BinancePublicMarketContext(
        "BINANCE_DEMO",
        market_api=api,
        observed_at_provider=lambda: _observed_after(api, offset_seconds),
    )

    with pytest.raises(
        MarketContextUnavailable,
        match="MARKET_CONTEXT_READ_FAILED_VALUEERROR",
    ):
        asyncio.run(provider.fetch("BTCUSDT-PERP", 20))


def test_public_market_window_returns_exact_contiguous_review_bars() -> None:
    provider = BinancePublicMarketContext(
        "BINANCE_DEMO",
        market_api=FakeMarketApi(),
        observed_at_provider=lambda: datetime(2027, 2, 1, tzinfo=UTC),
    )
    start = datetime(2027, 1, 15, 8, 0, 30, tzinfo=UTC)

    window = asyncio.run(
        provider.fetch_window(
            "BTCUSDT-PERP",
            "1m",
            start,
            start + timedelta(minutes=4),
        )
    )

    assert window.interval == "1m"
    assert window.source == "BINANCE_DEMO_PUBLIC"
    assert len(window.bars) == 5
    assert window.bars[0].open_at == datetime(2027, 1, 15, 8, 0, tzinfo=UTC)
    assert window.bars[-1].close == "105"
    assert window.source_cutoff == datetime(2027, 1, 15, 8, 5, tzinfo=UTC)


def test_public_market_context_coalesces_concurrent_same_key_reads() -> None:
    class SlowMarketApi(FakeMarketApi):
        async def query_ticker_book(self, *args, **kwargs):
            await asyncio.sleep(0.01)
            return await super().query_ticker_book(*args, **kwargs)

        async def query_klines(self, *args, **kwargs):
            await asyncio.sleep(0.01)
            return await super().query_klines(*args, **kwargs)

    api = SlowMarketApi()
    provider = BinancePublicMarketContext(
        "BINANCE_DEMO",
        market_api=api,
        observed_at_provider=lambda: _observed_after(api),
    )

    async def read_concurrently():
        try:
            return await asyncio.gather(
                provider.fetch("BTCUSDT-PERP", 20),
                provider.fetch("BTCUSDT-PERP", 20),
                provider.fetch("BTCUSDT-PERP", 20),
            )
        finally:
            await provider.close()

    contexts = asyncio.run(read_concurrently())

    assert len(contexts) == 3
    assert contexts[0] == contexts[1] == contexts[2]
    assert api.ticker_query_count == 1
    assert api.kline_query_count == 2


def test_public_market_window_normalizes_and_shares_historical_reads() -> None:
    api = FakeMarketApi()
    start = datetime(2027, 1, 15, 8, 0, 5, tzinfo=UTC)
    provider = BinancePublicMarketContext(
        "BINANCE_DEMO",
        market_api=api,
        observed_at_provider=lambda: datetime(2027, 2, 1, tzinfo=UTC),
    )

    async def read_concurrently():
        try:
            return await asyncio.gather(
                provider.fetch_window(
                    "BTCUSDT-PERP",
                    "1m",
                    start,
                    start + timedelta(minutes=4),
                ),
                provider.fetch_window(
                    "BTCUSDT-PERP",
                    "1m",
                    start + timedelta(seconds=20),
                    start + timedelta(minutes=4, seconds=20),
                ),
            )
        finally:
            await provider.close()

    first, second = asyncio.run(read_concurrently())

    assert first == second
    assert first.bars[0].open_at == datetime(2027, 1, 15, 8, 0, tzinfo=UTC)
    assert api.kline_query_count == 1


@pytest.mark.parametrize(
    "case",
    (
        "ZERO_PRICE",
        "HIGH_BELOW_BODY",
        "LOW_ABOVE_BODY",
        "NEGATIVE_VOLUME",
        "NON_FINITE_CLOSE",
        "WRONG_CLOSE_TIME",
    ),
)
def test_public_market_window_rejects_one_invalid_bar_in_the_complete_window(
    case: str,
) -> None:
    class InvalidBarMarketApi(FakeMarketApi):
        async def query_klines(self, *args, **kwargs):
            bars = await super().query_klines(*args, **kwargs)
            target = bars[1]
            if case == "ZERO_PRICE":
                target.open = "0"
            elif case == "HIGH_BELOW_BODY":
                target.high = target.open
            elif case == "LOW_ABOVE_BODY":
                target.low = target.close
            elif case == "NEGATIVE_VOLUME":
                target.volume = "-1"
            elif case == "NON_FINITE_CLOSE":
                target.close = "NaN"
            elif case == "WRONG_CLOSE_TIME":
                target.close_time -= 1
            return bars

    start = datetime(2027, 1, 15, 8, 0, tzinfo=UTC)
    provider = BinancePublicMarketContext(
        "BINANCE_DEMO",
        market_api=InvalidBarMarketApi(),
        observed_at_provider=lambda: start + timedelta(minutes=3),
    )

    with pytest.raises(
        MarketContextUnavailable,
        match="MARKET_WINDOW_READ_FAILED_VALUEERROR",
    ):
        asyncio.run(
            provider.fetch_window(
                "BTCUSDT-PERP",
                "1m",
                start,
                start + timedelta(minutes=2),
            )
        )


def test_market_bar_and_window_reject_invalid_time_boundaries() -> None:
    start = datetime(2027, 1, 15, 8, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="MARKET_BAR_TIMEZONE_REQUIRED"):
        MarketBar(
            open_at=start.replace(tzinfo=None),
            close_at=start + timedelta(minutes=1),
            open="100",
            high="102",
            low="99",
            close="101",
            volume="10",
        )
    with pytest.raises(ValueError, match="MARKET_BAR_INVALID"):
        MarketBar(
            open_at=start,
            close_at=start,
            open="100",
            high="102",
            low="99",
            close="101",
            volume="10",
        )

    first = MarketBar(
        open_at=start,
        close_at=start + timedelta(minutes=1),
        open="100",
        high="102",
        low="99",
        close="101",
        volume="10",
    )
    second = MarketBar(
        open_at=start + timedelta(minutes=2),
        close_at=start + timedelta(minutes=3),
        open="101",
        high="103",
        low="100",
        close="102",
        volume="11",
    )
    with pytest.raises(ValueError, match="MARKET_WINDOW_NOT_CONTIGUOUS"):
        MarketWindow(
            instrument_ref="BTCUSDT-PERP",
            interval="1m",
            source="BINANCE_DEMO_PUBLIC",
            source_cutoff=second.close_at,
            bars=(first, second),
        )


def test_public_market_window_rejects_a_future_window() -> None:
    start = datetime(2027, 1, 15, 8, 0, tzinfo=UTC)
    provider = BinancePublicMarketContext(
        "BINANCE_DEMO",
        market_api=FakeMarketApi(),
        observed_at_provider=lambda: start - timedelta(seconds=6),
    )

    with pytest.raises(
        MarketContextUnavailable,
        match="MARKET_WINDOW_READ_FAILED_VALUEERROR",
    ):
        asyncio.run(
            provider.fetch_window(
                "BTCUSDT-PERP",
                "1m",
                start,
                start,
            )
        )


def test_current_open_daily_candle_does_not_project_a_future_source_cutoff() -> None:
    observed_at = datetime(2027, 1, 15, 8, 2, 30, tzinfo=UTC)
    provider = BinancePublicMarketContext(
        "BINANCE_DEMO",
        market_api=FakeMarketApi(),
        observed_at_provider=lambda: observed_at,
    )

    window = asyncio.run(
        provider.fetch_window(
            "BTCUSDT-PERP",
            "1d",
            observed_at,
            observed_at,
        )
    )

    assert len(window.bars) == 1
    assert window.bars[0].open_at == datetime(2027, 1, 15, tzinfo=UTC)
    assert window.bars[0].close_at == datetime(2027, 1, 16, tzinfo=UTC)
    assert window.source_cutoff == observed_at
    assert window.source_cutoff < window.bars[0].close_at


@pytest.mark.parametrize(
    ("profile", "expected_environment", "expected_source"),
    (
        (
            "BINANCE_DEMO",
            BinanceEnvironment.DEMO,
            "BINANCE_DEMO_PUBLIC",
        ),
        (
            "BINANCE_LIVE_READ_ONLY",
            BinanceEnvironment.LIVE,
            "BINANCE_LIVE_PUBLIC",
        ),
        (
            "BINANCE_LIVE_WRITE",
            BinanceEnvironment.LIVE,
            "BINANCE_LIVE_PUBLIC",
        ),
    ),
)
def test_market_profile_routes_context_and_history_to_only_its_own_environment(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    expected_environment: BinanceEnvironment,
    expected_source: str,
) -> None:
    environments: list[BinanceEnvironment] = []

    def fake_http_client(**kwargs):
        environments.append(kwargs["environment"])
        return object()

    monkeypatch.setattr(
        public_market_module,
        "get_cached_binance_http_client",
        fake_http_client,
    )
    monkeypatch.setattr(
        public_market_module,
        "BinanceFuturesMarketHttpAPI",
        lambda *_args, **_kwargs: FakeMarketApi(),
    )

    api = FakeMarketApi()
    provider = BinancePublicMarketContext(
        profile,
        observed_at_provider=lambda: _observed_after(api),
    )
    start = datetime(2027, 1, 15, 8, 0, tzinfo=UTC)
    context = asyncio.run(provider.fetch("BTCUSDT-PERP", 20))
    window = asyncio.run(
        provider.fetch_window(
            "BTCUSDT-PERP",
            "1m",
            start,
            start,
        )
    )

    assert environments == [expected_environment]
    assert context.source == expected_source
    assert window.source == expected_source


def test_unknown_profile_cannot_fall_back_to_live_market_data() -> None:
    with pytest.raises(ValueError, match="PUBLIC_MARKET_PROFILE_UNSUPPORTED"):
        BinancePublicMarketContext(
            "UNRECOGNIZED_PROFILE",
            market_api=FakeMarketApi(),
        )


@pytest.mark.parametrize(
    ("interval", "duration"),
    [
        ("5m", timedelta(minutes=5)),
        ("15m", timedelta(minutes=15)),
        ("1h", timedelta(hours=1)),
        ("4h", timedelta(hours=4)),
        ("1d", timedelta(days=1)),
    ],
)
def test_public_market_window_supports_chart_intervals(
    interval: str,
    duration: timedelta,
) -> None:
    start = datetime(2027, 1, 15, 0, 0, tzinfo=UTC)
    provider = BinancePublicMarketContext(
        "BINANCE_DEMO",
        market_api=FakeMarketApi(),
        observed_at_provider=lambda: start + duration * 3,
    )

    window = asyncio.run(
        provider.fetch_window(
            "BTCUSDT-PERP",
            interval,
            start,
            start + duration * 2,
        )
    )

    assert window.interval == interval
    assert len(window.bars) == 3
    assert window.bars[1].open_at == start + duration


def test_public_market_window_rejects_unbounded_review_range() -> None:
    provider = BinancePublicMarketContext(
        "BINANCE_DEMO",
        market_api=FakeMarketApi(),
    )
    start = datetime(2027, 1, 15, 8, 0, tzinfo=UTC)

    with pytest.raises(MarketContextUnavailable, match="MARKET_WINDOW_RANGE_INVALID"):
        asyncio.run(
            provider.fetch_window(
                "BTCUSDT-PERP",
                "1m",
                start,
                start + timedelta(minutes=300),
            )
        )
