"""Read-only public market stream projected through the local workbench."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol, TypeAlias

import msgspec
from nautilus_trader.adapters.binance.common.enums import (
    BinanceAccountType,
    BinanceEnvironment,
)
from nautilus_trader.adapters.binance.common.schemas.market import (
    BinanceAggregatedTradeMsg,
    BinanceCandlestickMsg,
    BinanceDataMsgWrapper,
    BinanceOrderBookMsg,
    BinanceQuoteMsg,
)
from nautilus_trader.adapters.binance.common.urls import (
    get_ws_base_url,
    get_ws_public_base_url,
)
from nautilus_trader.adapters.binance.futures.schemas.market import (
    BinanceFuturesMarkPriceMsg,
)
from nautilus_trader.adapters.binance.websocket.client import BinanceWebSocketClient
from nautilus_trader.common.component import LiveClock
from pydantic import BaseModel, ConfigDict, model_validator

from halpha.domain_values import canonical_decimal
from halpha.public_instrument_rules import (
    InstrumentRulesUnavailable,
    perpetual_instrument_for_symbol,
    symbol_for_perpetual_instrument,
)
from halpha.public_market import (
    MARKET_INTERVAL_MILLISECONDS,
    MARKET_INTERVALS,
    MarketBar,
    MarketContextUnavailable,
    MarketInterval,
    binance_public_market_identity,
)


_QUEUE_CAPACITY = 256
_DEFAULT_CLOSE_TIMEOUT_SECONDS = 5.0
_ORDER_BOOK_DEPTH_LEVELS = 10
_ORDER_BOOK_SPEED_MILLISECONDS = 100


def _positive_finite_decimal(value: str) -> Decimal:
    decimal = Decimal(value)
    if not decimal.is_finite() or decimal <= 0:
        raise ValueError("MARKET_STREAM_DECIMAL_INVALID")
    return decimal


class MarketStreamStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["status"] = "status"
    state: Literal["CONNECTING", "LIVE", "RECONNECTING", "FAILED"]
    source: str
    observed_at: datetime
    reason: str | None = None


class MarketStreamQuote(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["quote"] = "quote"
    instrument_ref: str
    source: str
    source_cutoff: datetime
    received_at: datetime
    bid_price: str
    ask_price: str
    reference_price: str


class MarketStreamBar(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["bar"] = "bar"
    instrument_ref: str
    interval: MarketInterval
    source: str
    source_cutoff: datetime
    received_at: datetime
    closed: bool
    bar: MarketBar

    @model_validator(mode="after")
    def bar_boundary_matches_interval(self) -> MarketStreamBar:
        interval = timedelta(
            milliseconds=MARKET_INTERVAL_MILLISECONDS[self.interval],
        )
        if self.bar.close_at != self.bar.open_at + interval:
            raise ValueError("MARKET_STREAM_BAR_BOUNDARY_INVALID")
        return self


class MarketStreamFunding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["funding"] = "funding"
    instrument_ref: str
    source: str
    source_cutoff: datetime
    received_at: datetime
    mark_price: str
    index_price: str
    funding_rate: str
    next_funding_at: datetime

    @model_validator(mode="after")
    def next_funding_time_is_plausible(self) -> MarketStreamFunding:
        if not (
            self.source_cutoff - timedelta(minutes=5)
            <= self.next_funding_at
            <= self.source_cutoff + timedelta(days=7)
        ):
            raise ValueError("MARKET_STREAM_FUNDING_TIME_INVALID")
        return self


class MarketStreamBookLevel(BaseModel):
    """One current venue depth level used only for the bounded DOM view."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    price: str
    quantity: str

    @model_validator(mode="after")
    def values_are_positive(self) -> MarketStreamBookLevel:
        _positive_finite_decimal(self.price)
        _positive_finite_decimal(self.quantity)
        return self


class MarketStreamDepth(BaseModel):
    """Top-of-book snapshot; it deliberately is not a persisted order book."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["depth"] = "depth"
    instrument_ref: str
    source: str
    source_cutoff: datetime
    received_at: datetime
    update_id: int
    bids: tuple[MarketStreamBookLevel, ...]
    asks: tuple[MarketStreamBookLevel, ...]

    @model_validator(mode="after")
    def depth_is_ordered_and_not_crossed(self) -> MarketStreamDepth:
        if self.update_id <= 0:
            raise ValueError("MARKET_STREAM_DEPTH_UPDATE_INVALID")
        if not (1 <= len(self.bids) <= _ORDER_BOOK_DEPTH_LEVELS):
            raise ValueError("MARKET_STREAM_DEPTH_BIDS_INVALID")
        if not (1 <= len(self.asks) <= _ORDER_BOOK_DEPTH_LEVELS):
            raise ValueError("MARKET_STREAM_DEPTH_ASKS_INVALID")
        bid_prices = tuple(_positive_finite_decimal(level.price) for level in self.bids)
        ask_prices = tuple(_positive_finite_decimal(level.price) for level in self.asks)
        if any(left <= right for left, right in zip(bid_prices, bid_prices[1:])):
            raise ValueError("MARKET_STREAM_DEPTH_BIDS_UNORDERED")
        if any(left >= right for left, right in zip(ask_prices, ask_prices[1:])):
            raise ValueError("MARKET_STREAM_DEPTH_ASKS_UNORDERED")
        if bid_prices[0] >= ask_prices[0]:
            raise ValueError("MARKET_STREAM_DEPTH_CROSSED")
        return self


class MarketStreamTrade(BaseModel):
    """One public aggregate print, bounded by the browser tape buffer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["trade"] = "trade"
    instrument_ref: str
    source: str
    source_cutoff: datetime
    received_at: datetime
    trade_id: str
    price: str
    quantity: str
    aggressor_side: Literal["BUYER", "SELLER"]

    @model_validator(mode="after")
    def trade_values_are_valid(self) -> MarketStreamTrade:
        if not self.trade_id.strip():
            raise ValueError("MARKET_STREAM_TRADE_ID_INVALID")
        _positive_finite_decimal(self.price)
        _positive_finite_decimal(self.quantity)
        return self


MarketStreamEvent: TypeAlias = (
    MarketStreamStatus
    | MarketStreamQuote
    | MarketStreamBar
    | MarketStreamFunding
    | MarketStreamDepth
    | MarketStreamTrade
)


class PublicMarketStreamProvider(Protocol):
    def stream(self, instrument_ref: str) -> AsyncIterator[MarketStreamEvent]: ...

    async def close(self) -> None: ...


class BinanceWebSocketPort(Protocol):
    async def subscribe_book_ticker(self, symbol: str | None = None) -> None: ...

    async def unsubscribe_book_ticker(self, symbol: str | None = None) -> None: ...

    async def subscribe_partial_book_depth(
        self,
        symbol: str,
        depth: int,
        speed: int,
    ) -> None: ...

    async def unsubscribe_partial_book_depth(
        self,
        symbol: str,
        depth: int,
        speed: int,
    ) -> None: ...

    async def subscribe_agg_trades(self, symbol: str) -> None: ...

    async def unsubscribe_agg_trades(self, symbol: str) -> None: ...

    async def subscribe_bars(self, symbol: str, interval: str) -> None: ...

    async def unsubscribe_bars(self, symbol: str, interval: str) -> None: ...

    async def subscribe_mark_price(
        self,
        symbol: str | None = None,
        speed: int | None = None,
    ) -> None: ...

    async def unsubscribe_mark_price(
        self,
        symbol: str | None = None,
        speed: int | None = None,
    ) -> None: ...

    async def disconnect(self) -> None: ...


StreamRoute: TypeAlias = Literal["market", "public"]
WebSocketClientFactory: TypeAlias = Callable[
    [
        StreamRoute,
        BinanceEnvironment,
        Callable[[bytes], None],
        Callable[[], Awaitable[None]],
    ],
    BinanceWebSocketPort,
]


class BinancePublicMarketStream:
    """Relay isolated Nautilus-managed public streams through one local UI feed."""

    def __init__(
        self,
        profile: str,
        *,
        proxy_url: str | None = None,
        client_factory: WebSocketClientFactory | None = None,
        close_timeout_seconds: float = _DEFAULT_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        environment, source = binance_public_market_identity(profile)
        self._route_environments: dict[StreamRoute, BinanceEnvironment] = {
            "public": environment,
            "market": environment,
        }
        self._route_sources: dict[StreamRoute, str] = {
            "public": source,
            "market": source,
        }
        self._status_source = "+".join(dict.fromkeys(self._route_sources.values()))
        self._proxy_url = proxy_url
        self._client_factory = client_factory
        self._close_timeout_seconds = close_timeout_seconds
        self._clients: dict[StreamRoute, BinanceWebSocketPort] = {}
        self._route_live: dict[StreamRoute, bool] = {
            "market": False,
            "public": False,
        }
        self._subscribers: dict[asyncio.Queue[MarketStreamEvent], str] = {}
        self._subscribed_symbols: set[str] = set()
        self._overflowed_subscribers: set[
            asyncio.Queue[MarketStreamEvent]
        ] = set()
        self._start_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self._status = MarketStreamStatus(
            state="CONNECTING",
            source=self._status_source,
            observed_at=datetime.now(UTC),
            reason=None,
        )
        self._wrapper_decoder = msgspec.json.Decoder(BinanceDataMsgWrapper)
        self._quote_decoder = msgspec.json.Decoder(BinanceQuoteMsg)
        self._depth_decoder = msgspec.json.Decoder(BinanceOrderBookMsg)
        self._trade_decoder = msgspec.json.Decoder(BinanceAggregatedTradeMsg)
        self._candlestick_decoder = msgspec.json.Decoder(BinanceCandlestickMsg)
        self._mark_price_decoder = msgspec.json.Decoder(BinanceFuturesMarkPriceMsg)

    async def stream(
        self,
        instrument_ref: str,
    ) -> AsyncIterator[MarketStreamEvent]:
        try:
            symbol = symbol_for_perpetual_instrument(instrument_ref)
        except InstrumentRulesUnavailable:
            raise MarketContextUnavailable("MARKET_CONTEXT_INSTRUMENT_UNSUPPORTED")
        queue: asyncio.Queue[MarketStreamEvent] = asyncio.Queue(
            maxsize=_QUEUE_CAPACITY,
        )
        self._subscribers[queue] = instrument_ref
        try:
            await self._ensure_started(symbol)
            if queue.empty():
                self._put(queue, self._status)
            while True:
                event = await queue.get()
                if queue in self._overflowed_subscribers:
                    # The waiter may already have been suspended in queue.get()
                    # when a producer overflowed the queue.  Do not expose the
                    # fetched event before the resynchronization boundary.  If
                    # it was the latest retained event, put it back so it is
                    # delivered strictly after the boundary status.
                    if queue.empty():
                        queue.put_nowait(event)
                    yield MarketStreamStatus(
                        state="RECONNECTING",
                        source=self._status_source,
                        observed_at=datetime.now(UTC),
                        reason="MARKET_STREAM_SUBSCRIBER_RESYNC_REQUIRED",
                    )
                    self._overflowed_subscribers.discard(queue)
                    continue
                yield event
        finally:
            self._subscribers.pop(queue, None)
            self._overflowed_subscribers.discard(queue)
            if instrument_ref not in self._subscribers.values():
                await self._unsubscribe_symbol(symbol)

    async def close(self) -> None:
        self._closed = True
        try:
            async with asyncio.timeout(self._close_timeout_seconds):
                async with self._start_lock:
                    clients = tuple(self._clients.values())
                    self._clients.clear()
                    self._subscribed_symbols.clear()
                    self._route_live = {"market": False, "public": False}
                    self._loop = None
                if clients:
                    await asyncio.gather(
                        *(client.disconnect() for client in clients),
                        return_exceptions=True,
                    )
        except TimeoutError:
            # The market feed is informational and must never keep the whole
            # App process alive after its HTTP listener has stopped.  Nautilus
            # owns the underlying reconnect machinery; cancellation at this
            # boundary lets the process finish even when a venue/proxy close
            # handshake never returns.
            self._clients.clear()
            self._subscribed_symbols.clear()
            self._route_live = {"market": False, "public": False}
            self._loop = None

    async def _ensure_started(self, symbol: str) -> None:
        if self._closed:
            raise MarketContextUnavailable("MARKET_STREAM_CLOSED")
        if self._clients and symbol in self._subscribed_symbols:
            return
        async with self._start_lock:
            if self._closed:
                raise MarketContextUnavailable("MARKET_STREAM_CLOSED")
            if self._clients and symbol in self._subscribed_symbols:
                return
            self._loop = asyncio.get_running_loop()
            self._set_status("CONNECTING")
            clients: dict[StreamRoute, BinanceWebSocketPort] = dict(self._clients)
            try:
                if not clients:
                    clients["public"] = self._make_client("public")
                    clients["market"] = self._make_client("market")
                    self._clients = clients
                await clients["public"].subscribe_book_ticker(symbol)
                await clients["public"].subscribe_partial_book_depth(
                    symbol,
                    _ORDER_BOOK_DEPTH_LEVELS,
                    _ORDER_BOOK_SPEED_MILLISECONDS,
                )
                self._route_live["public"] = True
                for interval in MARKET_INTERVALS:
                    await clients["market"].subscribe_bars(symbol, interval)
                await clients["market"].subscribe_mark_price(symbol, speed=1000)
                await clients["market"].subscribe_agg_trades(symbol)
                self._route_live["market"] = True
                self._subscribed_symbols.add(symbol)
                self._set_status("LIVE")
            except Exception as exc:
                self._set_status(
                    "FAILED",
                    f"MARKET_STREAM_CONNECT_FAILED_{type(exc).__name__.upper()}",
                )
                if not self._subscribed_symbols:
                    self._clients.clear()
                    await asyncio.gather(
                        *(client.disconnect() for client in clients.values()),
                        return_exceptions=True,
                    )
                raise MarketContextUnavailable(
                    f"MARKET_STREAM_CONNECT_FAILED_{type(exc).__name__.upper()}"
                ) from None

    async def _unsubscribe_symbol(self, symbol: str) -> None:
        """Release streams no longer used by any workbench consumer."""

        async with self._start_lock:
            if (
                symbol not in self._subscribed_symbols
                or perpetual_instrument_for_symbol(symbol)
                in self._subscribers.values()
            ):
                return
            public_client = self._clients.get("public")
            market_client = self._clients.get("market")
            if public_client is None or market_client is None:
                return
            # Any successful unsubscription makes the old complete-feed marker
            # invalid. A later consumer must subscribe every feed again, even
            # when a subsequent unsubscribe fails or times out.
            self._subscribed_symbols.discard(symbol)
            try:
                async with asyncio.timeout(self._close_timeout_seconds):
                    await public_client.unsubscribe_book_ticker(symbol)
                    await public_client.unsubscribe_partial_book_depth(
                        symbol,
                        _ORDER_BOOK_DEPTH_LEVELS,
                        _ORDER_BOOK_SPEED_MILLISECONDS,
                    )
                    for interval in MARKET_INTERVALS:
                        await market_client.unsubscribe_bars(symbol, interval)
                    await market_client.unsubscribe_mark_price(symbol, speed=1000)
                    await market_client.unsubscribe_agg_trades(symbol)
            except Exception:
                pass
            finally:
                if not self._subscribed_symbols:
                    # With no remaining consumer, discard the entire pair even
                    # after partial cleanup. Bound a stalled venue handshake so
                    # the next subscriber can acquire the startup lock.
                    clients = tuple(self._clients.values())
                    self._clients.clear()
                    self._route_live = {"market": False, "public": False}
                    self._loop = None
                    try:
                        async with asyncio.timeout(self._close_timeout_seconds):
                            await asyncio.gather(
                                *(client.disconnect() for client in clients),
                                return_exceptions=True,
                            )
                    except TimeoutError:
                        pass

    def _make_client(self, route: StreamRoute) -> BinanceWebSocketPort:
        async def reconnected() -> None:
            if self._closed:
                return
            self._route_live[route] = False
            self._set_status("RECONNECTING", f"{route.upper()}_STREAM_RECONNECTED")

        def handler(raw: bytes) -> None:
            self._handle_message(route, raw)

        if self._client_factory is not None:
            return self._client_factory(
                route,
                self._route_environments[route],
                handler,
                reconnected,
            )
        environment = self._route_environments[route]
        base_url = (
            get_ws_public_base_url(
                BinanceAccountType.USDT_FUTURES,
                environment,
                False,
            )
            if route == "public"
            else get_ws_base_url(
                BinanceAccountType.USDT_FUTURES,
                environment,
                False,
            )
        )
        return BinanceWebSocketClient(
            clock=LiveClock(),
            base_url=base_url,
            handler=handler,
            handler_reconnect=reconnected,
            loop=asyncio.get_running_loop(),
            proxy_url=self._proxy_url,
        )

    def _handle_message(self, route: StreamRoute, raw: bytes) -> None:
        if self._closed:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            self._decode_and_publish(route, raw)
        else:
            loop.call_soon_threadsafe(self._decode_and_publish, route, raw)

    def _decode_and_publish(self, route: StreamRoute, raw: bytes) -> None:
        if self._closed:
            return
        try:
            wrapper = self._wrapper_decoder.decode(raw)
            stream = wrapper.stream
            if stream is None:
                return
            if route == "public" and "@bookTicker" in stream:
                event = self._quote_event(self._quote_decoder.decode(raw))
            elif route == "public" and "@depth" in stream:
                event = self._depth_event(self._depth_decoder.decode(raw))
            elif route == "market" and "@kline_" in stream:
                event = self._bar_event(self._candlestick_decoder.decode(raw))
            elif route == "market" and "@markPrice" in stream:
                event = self._funding_event(self._mark_price_decoder.decode(raw))
            elif route == "market" and "@aggTrade" in stream:
                event = self._trade_event(self._trade_decoder.decode(raw))
            else:
                return
        except (InvalidOperation, TypeError, ValueError, msgspec.DecodeError):
            return
        self._route_live[route] = True
        if all(self._route_live.values()) and self._status.state != "LIVE":
            self._set_status("LIVE")
        self._publish(event)

    def _quote_event(self, message: BinanceQuoteMsg) -> MarketStreamQuote:
        data = message.data
        if data.s not in self._subscribed_symbols or data.T is None or data.T <= 0:
            raise ValueError("MARKET_STREAM_QUOTE_INVALID")
        bid = Decimal(data.b)
        ask = Decimal(data.a)
        if bid <= 0 or ask <= 0 or ask < bid:
            raise ValueError("MARKET_STREAM_QUOTE_INVALID")
        return MarketStreamQuote(
            instrument_ref=perpetual_instrument_for_symbol(data.s),
            source=self._route_sources["public"],
            source_cutoff=datetime.fromtimestamp(data.T / 1000, tz=UTC),
            received_at=datetime.now(UTC),
            bid_price=canonical_decimal(bid),
            ask_price=canonical_decimal(ask),
            reference_price=canonical_decimal((bid + ask) / Decimal(2)),
        )

    def _bar_event(self, message: BinanceCandlestickMsg) -> MarketStreamBar:
        data = message.data
        candle = data.k
        interval = candle.i.value
        if (
            data.s not in self._subscribed_symbols
            or candle.s != data.s
            or interval not in MARKET_INTERVALS
            or data.E <= 0
            or candle.t <= 0
            or candle.T < candle.t
        ):
            raise ValueError("MARKET_STREAM_BAR_INVALID")
        open_price = Decimal(candle.o)
        high = Decimal(candle.h)
        low = Decimal(candle.l)
        close = Decimal(candle.c)
        volume = Decimal(candle.v)
        if (
            min(open_price, high, low, close) <= 0
            or volume < 0
            or high < max(open_price, close)
            or low > min(open_price, close)
            or high < low
        ):
            raise ValueError("MARKET_STREAM_BAR_INVALID")
        return MarketStreamBar(
            instrument_ref=perpetual_instrument_for_symbol(data.s),
            interval=interval,
            source=self._route_sources["market"],
            source_cutoff=datetime.fromtimestamp(data.E / 1000, tz=UTC),
            received_at=datetime.now(UTC),
            closed=candle.x,
            bar=MarketBar(
                open_at=datetime.fromtimestamp(candle.t / 1000, tz=UTC),
                close_at=datetime.fromtimestamp((candle.T + 1) / 1000, tz=UTC),
                open=canonical_decimal(open_price),
                high=canonical_decimal(high),
                low=canonical_decimal(low),
                close=canonical_decimal(close),
                volume=canonical_decimal(volume),
            ),
        )

    def _depth_event(self, message: BinanceOrderBookMsg) -> MarketStreamDepth:
        data = message.data
        source_time = data.T if data.T is not None and data.T > 0 else data.E
        if (
            data.s not in self._subscribed_symbols
            or data.E <= 0
            or source_time <= 0
            or data.u <= 0
        ):
            raise ValueError("MARKET_STREAM_DEPTH_INVALID")
        bids = tuple(
            MarketStreamBookLevel(
                price=canonical_decimal(_positive_finite_decimal(level.price)),
                quantity=canonical_decimal(_positive_finite_decimal(level.size)),
            )
            for level in data.b
        )
        asks = tuple(
            MarketStreamBookLevel(
                price=canonical_decimal(_positive_finite_decimal(level.price)),
                quantity=canonical_decimal(_positive_finite_decimal(level.size)),
            )
            for level in data.a
        )
        return MarketStreamDepth(
            instrument_ref=perpetual_instrument_for_symbol(data.s),
            source=self._route_sources["public"],
            source_cutoff=datetime.fromtimestamp(source_time / 1000, tz=UTC),
            received_at=datetime.now(UTC),
            update_id=data.u,
            bids=bids,
            asks=asks,
        )

    def _funding_event(
        self,
        message: BinanceFuturesMarkPriceMsg,
    ) -> MarketStreamFunding:
        data = message.data
        if data.s not in self._subscribed_symbols or data.E <= 0 or data.T <= 0:
            raise ValueError("MARKET_STREAM_FUNDING_INVALID")
        mark_price = Decimal(data.p)
        index_price = Decimal(data.i)
        funding_rate = Decimal(data.r)
        if (
            not mark_price.is_finite()
            or not index_price.is_finite()
            or not funding_rate.is_finite()
            or mark_price <= 0
            or index_price <= 0
            or abs(funding_rate) > 1
        ):
            raise ValueError("MARKET_STREAM_FUNDING_INVALID")
        return MarketStreamFunding(
            instrument_ref=perpetual_instrument_for_symbol(data.s),
            source=self._route_sources["market"],
            source_cutoff=datetime.fromtimestamp(data.E / 1000, tz=UTC),
            received_at=datetime.now(UTC),
            mark_price=canonical_decimal(mark_price),
            index_price=canonical_decimal(index_price),
            funding_rate=canonical_decimal(funding_rate),
            next_funding_at=datetime.fromtimestamp(data.T / 1000, tz=UTC),
        )

    def _trade_event(self, message: BinanceAggregatedTradeMsg) -> MarketStreamTrade:
        data = message.data
        if (
            data.s not in self._subscribed_symbols
            or data.E <= 0
            or data.T <= 0
            or data.a <= 0
        ):
            raise ValueError("MARKET_STREAM_TRADE_INVALID")
        price = _positive_finite_decimal(data.p)
        quantity = _positive_finite_decimal(data.q)
        return MarketStreamTrade(
            instrument_ref=perpetual_instrument_for_symbol(data.s),
            source=self._route_sources["market"],
            source_cutoff=datetime.fromtimestamp(data.T / 1000, tz=UTC),
            received_at=datetime.now(UTC),
            trade_id=str(data.a),
            price=canonical_decimal(price),
            quantity=canonical_decimal(quantity),
            aggressor_side="SELLER" if data.m else "BUYER",
        )

    def _set_status(
        self,
        state: Literal["CONNECTING", "LIVE", "RECONNECTING", "FAILED"],
        reason: str | None = None,
    ) -> None:
        self._status = MarketStreamStatus(
            state=state,
            source=self._status_source,
            observed_at=datetime.now(UTC),
            reason=reason,
        )
        self._publish(self._status)

    def _publish(self, event: MarketStreamEvent) -> None:
        event_instrument = getattr(event, "instrument_ref", None)
        for queue, instrument_ref in tuple(self._subscribers.items()):
            if event_instrument is None or event_instrument == instrument_ref:
                self._put(queue, event)

    def _put(
        self,
        queue: asyncio.Queue[MarketStreamEvent],
        event: MarketStreamEvent,
    ) -> None:
        if queue in self._overflowed_subscribers or queue.full():
            # Once any event was dropped, the remaining sequence is not a
            # trustworthy K-line history.  Discard the backlog and make the
            # consumer perform its existing REST resynchronization.  Until the
            # consumer observes that boundary, retain only the newest event so
            # no truncated prefix can be projected after resynchronization.
            self._overflowed_subscribers.add(queue)
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        queue.put_nowait(event)
