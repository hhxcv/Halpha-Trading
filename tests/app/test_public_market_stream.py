from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment

from halpha.public_market import MarketBar, MarketContextUnavailable
from halpha.public_market_stream import (
    BinancePublicMarketStream,
    MarketStreamBar,
    MarketStreamBookLevel,
    MarketStreamDepth,
    MarketStreamEvent,
    MarketStreamFunding,
    MarketStreamQuote,
    MarketStreamStatus,
    MarketStreamTrade,
)


class FakeWebSocketClient:
    def __init__(
        self,
        handler: Callable[[bytes], None],
        reconnect: Callable[[], Awaitable[None]],
    ) -> None:
        self.handler = handler
        self.reconnect = reconnect
        self.book_tickers: list[str | None] = []
        self.unsubscribed_book_tickers: list[str | None] = []
        self.partial_depths: list[tuple[str, int, int]] = []
        self.unsubscribed_partial_depths: list[tuple[str, int, int]] = []
        self.agg_trades: list[str] = []
        self.unsubscribed_agg_trades: list[str] = []
        self.bars: list[tuple[str, str]] = []
        self.unsubscribed_bars: list[tuple[str, str]] = []
        self.mark_prices: list[tuple[str | None, int | None]] = []
        self.unsubscribed_mark_prices: list[tuple[str | None, int | None]] = []
        self.disconnected = False

    async def subscribe_book_ticker(self, symbol: str | None = None) -> None:
        self.book_tickers.append(symbol)

    async def unsubscribe_book_ticker(self, symbol: str | None = None) -> None:
        self.unsubscribed_book_tickers.append(symbol)

    async def subscribe_partial_book_depth(
        self,
        symbol: str,
        depth: int,
        speed: int,
    ) -> None:
        self.partial_depths.append((symbol, depth, speed))

    async def unsubscribe_partial_book_depth(
        self,
        symbol: str,
        depth: int,
        speed: int,
    ) -> None:
        self.unsubscribed_partial_depths.append((symbol, depth, speed))

    async def subscribe_agg_trades(self, symbol: str) -> None:
        self.agg_trades.append(symbol)

    async def unsubscribe_agg_trades(self, symbol: str) -> None:
        self.unsubscribed_agg_trades.append(symbol)

    async def subscribe_bars(self, symbol: str, interval: str) -> None:
        self.bars.append((symbol, interval))

    async def unsubscribe_bars(self, symbol: str, interval: str) -> None:
        self.unsubscribed_bars.append((symbol, interval))

    async def subscribe_mark_price(
        self,
        symbol: str | None = None,
        speed: int | None = None,
    ) -> None:
        self.mark_prices.append((symbol, speed))

    async def unsubscribe_mark_price(
        self,
        symbol: str | None = None,
        speed: int | None = None,
    ) -> None:
        self.unsubscribed_mark_prices.append((symbol, speed))

    async def disconnect(self) -> None:
        self.disconnected = True

    def emit(self, payload: dict[str, object]) -> None:
        self.handler(json.dumps(payload, separators=(",", ":")).encode())


def test_market_stream_bar_requires_boundary_matching_declared_interval() -> None:
    open_at = datetime(2027, 1, 15, 8, 0, tzinfo=UTC)
    correct_bar = MarketBar(
        open_at=open_at,
        close_at=open_at + timedelta(minutes=5),
        open="101",
        high="103",
        low="100",
        close="102",
        volume="12.5",
    )
    event = MarketStreamBar(
        instrument_ref="BTCUSDT-PERP",
        interval="5m",
        source="BINANCE_DEMO_PUBLIC",
        source_cutoff=open_at + timedelta(minutes=1),
        received_at=open_at + timedelta(minutes=1),
        closed=False,
        bar=correct_bar,
    )
    assert event.bar.close_at == open_at + timedelta(minutes=5)

    mismatched_bar = MarketBar(
        open_at=open_at,
        close_at=open_at + timedelta(minutes=1),
        open="101",
        high="103",
        low="100",
        close="102",
        volume="12.5",
    )
    with pytest.raises(
        ValueError,
        match="MARKET_STREAM_BAR_BOUNDARY_INVALID",
    ):
        MarketStreamBar(
            instrument_ref="BTCUSDT-PERP",
            interval="5m",
            source="BINANCE_DEMO_PUBLIC",
            source_cutoff=open_at + timedelta(minutes=1),
            received_at=open_at + timedelta(minutes=1),
            closed=False,
            bar=mismatched_bar,
        )


def test_market_stream_depth_rejects_crossed_or_unordered_levels() -> None:
    observed_at = datetime(2027, 1, 15, 8, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="MARKET_STREAM_DEPTH_CROSSED"):
        MarketStreamDepth(
            instrument_ref="BTCUSDT-PERP",
            source="BINANCE_DEMO_PUBLIC",
            source_cutoff=observed_at,
            received_at=observed_at,
            update_id=1,
            bids=(MarketStreamBookLevel(price="101", quantity="1"),),
            asks=(MarketStreamBookLevel(price="101", quantity="2"),),
        )
    with pytest.raises(ValueError, match="MARKET_STREAM_DEPTH_BIDS_UNORDERED"):
        MarketStreamDepth(
            instrument_ref="BTCUSDT-PERP",
            source="BINANCE_DEMO_PUBLIC",
            source_cutoff=observed_at,
            received_at=observed_at,
            update_id=2,
            bids=(
                MarketStreamBookLevel(price="101", quantity="1"),
                MarketStreamBookLevel(price="102", quantity="1"),
            ),
            asks=(MarketStreamBookLevel(price="103", quantity="1"),),
        )


async def _next_kind(
    stream,
    kind: type[MarketStreamEvent],
) -> MarketStreamEvent:
    for _ in range(12):
        event = await asyncio.wait_for(anext(stream), timeout=1)
        if isinstance(event, kind):
            return event
    raise AssertionError(f"stream did not emit {kind.__name__}")


async def _next_live_status(stream) -> MarketStreamStatus:
    for _ in range(6):
        event = await asyncio.wait_for(anext(stream), timeout=1)
        if isinstance(event, MarketStreamStatus) and event.state == "LIVE":
            return event
    raise AssertionError("stream did not become live")


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
def test_public_market_stream_routes_every_feed_to_the_profile_environment(
    profile: str,
    expected_environment: BinanceEnvironment,
    expected_source: str,
) -> None:
    async def scenario() -> None:
        clients: dict[str, FakeWebSocketClient] = {}
        environments: dict[str, BinanceEnvironment] = {}

        def factory(route, environment, handler, reconnect):
            client = FakeWebSocketClient(handler, reconnect)
            clients[route] = client
            environments[route] = environment
            return client

        provider = BinancePublicMarketStream(
            profile,
            client_factory=factory,
        )
        stream = provider.stream("BTCUSDT-PERP")
        status = await _next_live_status(stream)
        assert status.source == expected_source
        assert environments == {
            "public": expected_environment,
            "market": expected_environment,
        }
        assert clients["public"].book_tickers == ["BTCUSDT"]
        assert clients["public"].partial_depths == [("BTCUSDT", 10, 100)]
        assert clients["market"].bars == [
            ("BTCUSDT", "1m"),
            ("BTCUSDT", "5m"),
            ("BTCUSDT", "15m"),
            ("BTCUSDT", "1h"),
            ("BTCUSDT", "4h"),
            ("BTCUSDT", "1d"),
        ]
        assert clients["market"].mark_prices == [("BTCUSDT", 1000)]
        assert clients["market"].agg_trades == ["BTCUSDT"]

        clients["public"].emit(
            {
                "stream": "btcusdt@bookTicker",
                "data": {
                    "s": "BTCUSDT",
                    "u": 42,
                    "b": "101.1",
                    "B": "2",
                    "a": "101.3",
                    "A": "3",
                    "T": 1_800_000_100_000,
                },
            }
        )
        quote = await _next_kind(stream, MarketStreamQuote)
        assert isinstance(quote, MarketStreamQuote)
        assert quote.bid_price == "101.1"
        assert quote.ask_price == "101.3"
        assert quote.reference_price == "101.2"
        assert quote.source == expected_source
        assert quote.source_cutoff.isoformat() == "2027-01-15T08:01:40+00:00"

        clients["public"].emit(
            {
                "stream": "btcusdt@depth10@100ms",
                "data": {
                    "e": "depthUpdate",
                    "E": 1_800_000_100_100,
                    "T": 1_800_000_100_090,
                    "s": "BTCUSDT",
                    "U": 40,
                    "u": 42,
                    "pu": 39,
                    "b": [["101.1", "2"], ["101.0", "3"]],
                    "a": [["101.3", "4"], ["101.4", "5"]],
                },
            }
        )
        depth = await _next_kind(stream, MarketStreamDepth)
        assert isinstance(depth, MarketStreamDepth)
        assert depth.update_id == 42
        assert depth.bids[0].price == "101.1"
        assert depth.asks[0].quantity == "4"
        assert depth.source == expected_source

        clients["market"].emit(
            {
                "stream": "btcusdt@aggTrade",
                "data": {
                    "e": "aggTrade",
                    "E": 1_800_000_100_150,
                    "s": "BTCUSDT",
                    "a": 501,
                    "p": "101.2",
                    "q": "0.25",
                    "f": 20,
                    "l": 21,
                    "T": 1_800_000_100_140,
                    "m": False,
                },
            }
        )
        trade = await _next_kind(stream, MarketStreamTrade)
        assert isinstance(trade, MarketStreamTrade)
        assert trade.trade_id == "501"
        assert trade.price == "101.2"
        assert trade.aggressor_side == "BUYER"
        assert trade.source == expected_source

        clients["market"].emit(
            {
                "stream": "btcusdt@kline_5m",
                "data": {
                    "e": "kline",
                    "E": 1_800_000_100_250,
                    "s": "BTCUSDT",
                    "k": {
                        "t": 1_800_000_000_000,
                        "T": 1_800_000_299_999,
                        "s": "BTCUSDT",
                        "i": "5m",
                        "f": 1,
                        "L": 2,
                        "o": "101",
                        "c": "102",
                        "h": "103",
                        "l": "100",
                        "v": "12.5",
                        "n": 2,
                        "x": False,
                        "q": "1000",
                        "V": "6",
                        "Q": "500",
                        "B": "0",
                    },
                },
            }
        )
        bar = await _next_kind(stream, MarketStreamBar)
        assert isinstance(bar, MarketStreamBar)
        assert bar.interval == "5m"
        assert bar.closed is False
        assert bar.bar.open == "101"
        assert bar.bar.close == "102"
        assert bar.bar.volume == "12.5"
        assert bar.source == expected_source

        clients["market"].emit(
            {
                "stream": "btcusdt@markPrice@1s",
                "data": {
                    "e": "markPriceUpdate",
                    "E": 1_800_000_100_500,
                    "s": "BTCUSDT",
                    "p": "101.25",
                    "i": "101.2",
                    "P": "101.3",
                    "r": "0.0001",
                    "T": 1_800_028_900_000,
                },
            }
        )
        funding = await _next_kind(stream, MarketStreamFunding)
        assert isinstance(funding, MarketStreamFunding)
        assert funding.mark_price == "101.25"
        assert funding.index_price == "101.2"
        assert funding.funding_rate == "0.0001"
        assert funding.next_funding_at.isoformat() == "2027-01-15T16:01:40+00:00"
        assert funding.source == expected_source

        await clients["market"].reconnect()
        reconnecting = await _next_kind(stream, MarketStreamStatus)
        assert isinstance(reconnecting, MarketStreamStatus)
        assert reconnecting.state == "RECONNECTING"
        assert reconnecting.reason == "MARKET_STREAM_RECONNECTED"

        await stream.aclose()
        await provider.close()
        assert all(client.disconnected for client in clients.values())

    asyncio.run(scenario())


def test_public_market_stream_ignores_malformed_or_wrong_symbol_payloads() -> None:
    async def scenario() -> None:
        clients: dict[str, FakeWebSocketClient] = {}

        def factory(route, _environment, handler, reconnect):
            client = FakeWebSocketClient(handler, reconnect)
            clients[route] = client
            return client

        provider = BinancePublicMarketStream(
            "BINANCE_LIVE_READ_ONLY",
            client_factory=factory,
        )
        stream = provider.stream("BTCUSDT-PERP")
        await _next_live_status(stream)
        clients["public"].emit(
            {
                "stream": "ethusdt@bookTicker",
                "data": {
                    "s": "ETHUSDT",
                    "u": 1,
                    "b": "10",
                    "B": "1",
                    "a": "11",
                    "A": "1",
                    "T": 1_800_000_100_000,
                },
            }
        )
        clients["public"].handler(b"not-json")
        clients["market"].emit(
            {
                "stream": "btcusdt@markPrice@1s",
                "data": {
                    "e": "markPriceUpdate",
                    "E": 1_800_000_100_500,
                    "s": "BTCUSDT",
                    "p": "101.25",
                    "i": "101.2",
                    "P": "101.3",
                    "r": "1.1",
                    "T": 1_800_028_900_000,
                },
            }
        )
        await asyncio.sleep(0)

        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.02)
        assert not pending.done()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()
        await provider.close()

    asyncio.run(scenario())


def test_public_market_stream_routes_dynamic_contracts_and_releases_unused_symbol() -> None:
    async def scenario() -> None:
        clients: dict[str, FakeWebSocketClient] = {}

        def factory(route, _environment, handler, reconnect):
            client = FakeWebSocketClient(handler, reconnect)
            clients[route] = client
            return client

        provider = BinancePublicMarketStream(
            "BINANCE_DEMO",
            client_factory=factory,
        )
        stream = provider.stream("ETHUSDT-PERP")
        await _next_live_status(stream)

        assert clients["public"].book_tickers == ["ETHUSDT"]
        assert clients["public"].partial_depths == [("ETHUSDT", 10, 100)]
        assert clients["market"].mark_prices == [("ETHUSDT", 1000)]
        assert clients["market"].agg_trades == ["ETHUSDT"]
        clients["public"].emit(
            {
                "stream": "ethusdt@bookTicker",
                "data": {
                    "s": "ETHUSDT",
                    "u": 7,
                    "b": "2000.1",
                    "B": "2",
                    "a": "2000.3",
                    "A": "3",
                    "T": 1_800_000_100_000,
                },
            }
        )
        quote = await _next_kind(stream, MarketStreamQuote)
        assert isinstance(quote, MarketStreamQuote)
        assert quote.instrument_ref == "ETHUSDT-PERP"

        await stream.aclose()
        released_clients = dict(clients)
        assert released_clients["public"].unsubscribed_book_tickers == ["ETHUSDT"]
        assert released_clients["public"].unsubscribed_partial_depths == [
            ("ETHUSDT", 10, 100),
        ]
        assert released_clients["market"].unsubscribed_mark_prices == [("ETHUSDT", 1000)]
        assert released_clients["market"].unsubscribed_agg_trades == ["ETHUSDT"]
        assert "ETHUSDT" not in provider._subscribed_symbols
        assert provider._clients == {}
        assert released_clients["public"].disconnected is True
        assert released_clients["market"].disconnected is True

        next_stream = provider.stream("BTCUSDT-PERP")
        await _next_live_status(next_stream)
        assert clients["public"] is not released_clients["public"]
        assert clients["market"] is not released_clients["market"]
        assert clients["public"].book_tickers == ["BTCUSDT"]
        await next_stream.aclose()
        await provider.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("keep_other_symbol", (False, True))
def test_partial_unsubscribe_reopens_all_feeds_without_disrupting_other_symbols(
    keep_other_symbol: bool,
) -> None:
    async def scenario() -> None:
        clients: dict[str, FakeWebSocketClient] = {}

        class PartialUnsubscribeClient(FakeWebSocketClient):
            fail_next_depth_unsubscribe = False

            async def unsubscribe_partial_book_depth(self, symbol, depth, speed):
                if self.fail_next_depth_unsubscribe:
                    self.fail_next_depth_unsubscribe = False
                    raise RuntimeError("fixture unsubscribe failure")
                await super().unsubscribe_partial_book_depth(symbol, depth, speed)

        def factory(route, _environment, handler, reconnect):
            client = PartialUnsubscribeClient(handler, reconnect)
            clients[route] = client
            return client

        provider = BinancePublicMarketStream("BINANCE_DEMO", client_factory=factory)
        first = provider.stream("SOLUSDT-PERP")
        other = provider.stream("BTCUSDT-PERP") if keep_other_symbol else None
        reopened = None
        try:
            await _next_live_status(first)
            if other is not None:
                await _next_live_status(other)
            released_clients = dict(clients)
            public_client = clients["public"]
            public_client.fail_next_depth_unsubscribe = True

            await first.aclose()

            assert public_client.unsubscribed_book_tickers == ["SOLUSDT"]
            assert "SOLUSDT" not in provider._subscribed_symbols
            if keep_other_symbol:
                assert provider._subscribed_symbols == {"BTCUSDT"}
                assert all(not client.disconnected for client in clients.values())
            else:
                assert provider._clients == {}
                assert all(client.disconnected for client in released_clients.values())

            reopened = provider.stream("SOLUSDT-PERP")
            await _next_live_status(reopened)

            expected_subscribe_count = 2 if keep_other_symbol else 1
            assert clients["public"].book_tickers.count("SOLUSDT") == expected_subscribe_count
            assert clients["public"].partial_depths.count(
                ("SOLUSDT", 10, 100)
            ) == expected_subscribe_count
            assert clients["market"].mark_prices.count(
                ("SOLUSDT", 1000)
            ) == expected_subscribe_count
            clients["public"].emit(
                {
                    "stream": "solusdt@bookTicker",
                    "data": {
                        "s": "SOLUSDT", "u": 8,
                        "b": "100", "B": "2", "a": "101", "A": "3",
                        "T": 1_800_000_100_000,
                    },
                }
            )
            quote = await _next_kind(reopened, MarketStreamQuote)
            assert quote.instrument_ref == "SOLUSDT-PERP"
        finally:
            await first.aclose()
            if reopened is not None:
                await reopened.aclose()
            if other is not None:
                await other.aclose()
            await provider.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("hanging_operation", ("unsubscribe", "disconnect"))
def test_last_subscriber_release_is_bounded_and_allows_a_new_connection(
    hanging_operation: str,
) -> None:
    async def scenario() -> None:
        hang_started = asyncio.Event()
        never_finishes = asyncio.Event()
        clients: list[FakeWebSocketClient] = []

        class HangingClient(FakeWebSocketClient):
            async def unsubscribe_partial_book_depth(self, symbol, depth, speed):
                if hanging_operation == "unsubscribe":
                    hang_started.set()
                    await never_finishes.wait()
                await super().unsubscribe_partial_book_depth(symbol, depth, speed)

            async def disconnect(self):
                if hanging_operation == "disconnect":
                    hang_started.set()
                    await never_finishes.wait()
                await super().disconnect()

        def factory(_route, _environment, handler, reconnect):
            client = HangingClient(handler, reconnect)
            clients.append(client)
            return client

        provider = BinancePublicMarketStream(
            "BINANCE_DEMO", client_factory=factory, close_timeout_seconds=0.01,
        )
        first = provider.stream("SOLUSDT-PERP")
        reopened = None
        try:
            await _next_live_status(first)
            await asyncio.wait_for(first.aclose(), timeout=0.2)
            assert hang_started.is_set()
            assert provider._clients == {}
            assert provider._subscribed_symbols == set()

            reopened = provider.stream("SOLUSDT-PERP")
            await _next_live_status(reopened)
            assert len(clients) == 4
        finally:
            never_finishes.set()
            await first.aclose()
            if reopened is not None:
                await reopened.aclose()
            await provider.close()

    asyncio.run(scenario())


def test_public_market_stream_close_serializes_with_startup() -> None:
    async def scenario() -> None:
        clients: dict[str, FakeWebSocketClient] = {}
        subscribe_started = asyncio.Event()
        release_subscribe = asyncio.Event()

        class BlockingWebSocketClient(FakeWebSocketClient):
            async def subscribe_book_ticker(self, symbol: str | None = None) -> None:
                await super().subscribe_book_ticker(symbol)
                subscribe_started.set()
                await release_subscribe.wait()

        def factory(route, _environment, handler, reconnect):
            client = BlockingWebSocketClient(handler, reconnect)
            clients[route] = client
            return client

        provider = BinancePublicMarketStream(
            "BINANCE_DEMO",
            client_factory=factory,
        )
        stream = provider.stream("BTCUSDT-PERP")
        first_event = asyncio.create_task(anext(stream))
        await asyncio.wait_for(subscribe_started.wait(), timeout=1)

        close_task = asyncio.create_task(provider.close())
        await asyncio.sleep(0)
        assert not close_task.done()

        release_subscribe.set()
        await asyncio.wait_for(first_event, timeout=1)
        await asyncio.wait_for(close_task, timeout=1)
        await stream.aclose()

        assert clients
        assert all(client.disconnected for client in clients.values())

        closed_stream = provider.stream("BTCUSDT-PERP")
        with pytest.raises(MarketContextUnavailable, match="MARKET_STREAM_CLOSED"):
            await anext(closed_stream)

    asyncio.run(scenario())


def test_public_market_stream_close_is_bounded_when_disconnect_hangs() -> None:
    async def scenario() -> None:
        disconnect_started = asyncio.Event()
        never_disconnects = asyncio.Event()

        class HangingDisconnectClient(FakeWebSocketClient):
            async def disconnect(self) -> None:
                disconnect_started.set()
                await never_disconnects.wait()

        def factory(_route, _environment, handler, reconnect):
            return HangingDisconnectClient(handler, reconnect)

        provider = BinancePublicMarketStream(
            "BINANCE_DEMO",
            client_factory=factory,
            close_timeout_seconds=0.01,
        )
        stream = provider.stream("BTCUSDT-PERP")
        await _next_live_status(stream)

        await asyncio.wait_for(provider.close(), timeout=0.2)
        assert disconnect_started.is_set()
        assert provider._clients == {}
        assert provider._loop is None

        await stream.aclose()

    asyncio.run(scenario())


def test_public_market_stream_overflow_precedes_only_the_latest_event() -> None:
    async def scenario() -> None:
        def factory(_route, _environment, handler, reconnect):
            return FakeWebSocketClient(handler, reconnect)

        provider = BinancePublicMarketStream(
            "BINANCE_DEMO",
            client_factory=factory,
        )
        stream = provider.stream("BTCUSDT-PERP")
        await _next_live_status(stream)
        observed_at = datetime(2027, 1, 15, 8, 1, 40, tzinfo=UTC)
        waiting_consumer = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        assert not waiting_consumer.done()

        latest_quote: MarketStreamQuote | None = None
        for index in range(300):
            bid = 1010 + index
            latest_quote = MarketStreamQuote(
                instrument_ref="BTCUSDT-PERP",
                source="BINANCE_DEMO_PUBLIC",
                source_cutoff=observed_at,
                received_at=observed_at,
                bid_price=str(bid),
                ask_price=str(bid + 2),
                reference_price=str(bid + 1),
            )
            provider._publish(latest_quote)

        resync = await asyncio.wait_for(waiting_consumer, timeout=1)
        assert isinstance(resync, MarketStreamStatus)
        assert resync.state == "RECONNECTING"
        assert resync.reason == "MARKET_STREAM_SUBSCRIBER_RESYNC_REQUIRED"
        assert latest_quote is not None
        assert await asyncio.wait_for(anext(stream), timeout=1) == latest_quote

        await stream.aclose()
        await provider.close()

    asyncio.run(scenario())


def test_public_market_stream_rejects_captured_handlers_after_close() -> None:
    async def scenario() -> None:
        clients: dict[str, FakeWebSocketClient] = {}

        def factory(route, _environment, handler, reconnect):
            client = FakeWebSocketClient(handler, reconnect)
            clients[route] = client
            return client

        provider = BinancePublicMarketStream(
            "BINANCE_DEMO",
            client_factory=factory,
        )
        stream = provider.stream("BTCUSDT-PERP")
        await _next_live_status(stream)
        captured_handler = clients["public"].handler
        captured_reconnect = clients["public"].reconnect
        raw_quote = json.dumps(
            {
                "stream": "btcusdt@bookTicker",
                "data": {
                    "s": "BTCUSDT",
                    "u": 43,
                    "b": "102.1",
                    "B": "2",
                    "a": "102.3",
                    "A": "3",
                    "T": 1_800_000_100_001,
                },
            },
            separators=(",", ":"),
        ).encode()

        await provider.close()
        assert provider._loop is None
        closed_route_live = dict(provider._route_live)
        closed_status = provider._status

        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        assert not pending.done()
        await captured_reconnect()
        captured_handler(raw_quote)
        provider._decode_and_publish("public", raw_quote)
        await asyncio.sleep(0.02)
        assert not pending.done()
        assert provider._route_live == closed_route_live
        assert provider._status == closed_status

        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()

    asyncio.run(scenario())
