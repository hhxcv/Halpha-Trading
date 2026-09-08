import { useEffect, useRef, useState } from "react";

import type { MarketInterval } from "./api/client";

export type { MarketInterval } from "./api/client";

export const MARKET_INTERVALS = [
  "1m",
  "5m",
  "15m",
  "1h",
  "4h",
  "1d",
] as const satisfies ReadonlyArray<MarketInterval>;

export const MARKET_STREAM_QUOTE_THROTTLE_MS = 250;
/** DOM and tape are informational surfaces, committed in bounded batches. */
export const MARKET_STREAM_FLOW_THROTTLE_MS = 100;
export const MARKET_STREAM_MAX_TRADE_TAPE_EVENTS = 80;
export const MARKET_STREAM_STALE_AFTER_MS = 5_000;
export const MARKET_STREAM_MAX_FUTURE_SKEW_MS = 5_000;
export const MARKET_STREAM_RECONNECT_BASE_MS = 500;
export const MARKET_STREAM_RECONNECT_MAX_MS = 8_000;

/** A hidden workbench has no current market-observation consumer. */
export function shouldObservePublicMarketStream(
  enabled: boolean,
  visibilityState: DocumentVisibilityState | undefined,
): boolean {
  return enabled && (visibilityState === undefined || visibilityState === "visible");
}

export function marketEnvironmentScopeKey(
  environmentKind: string,
  environmentId: string,
): string {
  return `${environmentKind}:${environmentId}`;
}

export function expectedMarketSourceForEnvironment(
  environmentKind: string,
): string | null {
  if (environmentKind === "DEMO") return "BINANCE_DEMO_PUBLIC";
  if (environmentKind === "LIVE") return "BINANCE_LIVE_PUBLIC";
  return null;
}

export function isMarketSourceForEnvironment(
  source: string | null | undefined,
  environmentKind: string,
): boolean {
  const expectedSource = expectedMarketSourceForEnvironment(environmentKind);
  return expectedSource !== null && source === expectedSource;
}

export type MarketStreamServerStatus = Readonly<{
  type: "status";
  state: "CONNECTING" | "LIVE" | "RECONNECTING" | "FAILED";
  source: string;
  observed_at: string;
  reason: string | null;
}>;

export type MarketStreamQuote = Readonly<{
  type: "quote";
  instrument_ref: string;
  source: string;
  source_cutoff: string;
  received_at: string;
  bid_price: string;
  ask_price: string;
  reference_price: string;
}>;

export type MarketStreamBarValue = Readonly<{
  open_at: string;
  close_at: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
}>;

export type MarketStreamBar = Readonly<{
  type: "bar";
  instrument_ref: string;
  interval: MarketInterval;
  source: string;
  source_cutoff: string;
  received_at: string;
  closed: boolean;
  bar: MarketStreamBarValue;
}>;

export type MarketStreamFunding = Readonly<{
  type: "funding";
  instrument_ref: string;
  source: string;
  source_cutoff: string;
  received_at: string;
  mark_price: string;
  index_price: string;
  funding_rate: string;
  next_funding_at: string;
}>;

export type MarketStreamDepthLevel = Readonly<{
  price: string;
  quantity: string;
}>;

/** Latest bounded top-of-book snapshot from the selected venue environment. */
export type MarketStreamDepth = Readonly<{
  type: "depth";
  instrument_ref: string;
  source: string;
  source_cutoff: string;
  received_at: string;
  update_id: number;
  bids: ReadonlyArray<MarketStreamDepthLevel>;
  asks: ReadonlyArray<MarketStreamDepthLevel>;
}>;

/** One public aggregate print; the hook keeps only a bounded recent tape. */
export type MarketStreamTrade = Readonly<{
  type: "trade";
  instrument_ref: string;
  source: string;
  source_cutoff: string;
  received_at: string;
  trade_id: string;
  price: string;
  quantity: string;
  aggressor_side: "BUYER" | "SELLER";
}>;

export type MarketStreamEvent =
  | MarketStreamServerStatus
  | MarketStreamQuote
  | MarketStreamBar
  | MarketStreamFunding
  | MarketStreamDepth
  | MarketStreamTrade;

export type MarketStreamClientStatus =
  | "DISABLED"
  | "CONNECTING"
  | "LIVE"
  | "RECONNECTING"
  | "STALE"
  | "FAILED";

export type PublicMarketStreamSnapshot = Readonly<{
  environmentScope: string;
  status: MarketStreamClientStatus;
  statusReason: string | null;
  statusSource: string | null;
  statusObservedAt: string | null;
  quote: MarketStreamQuote | null;
  liveBar: MarketStreamBar | null;
  funding: MarketStreamFunding | null;
  /** Current top-ten snapshot; null means unavailable rather than an empty book. */
  depth: MarketStreamDepth | null;
  /** Bounded current-session aggregate prints. They are never trade-execution facts. */
  trades: ReadonlyArray<MarketStreamTrade>;
  /**
   * UI-only top-of-book midpoint estimates for configured short windows.
   * Executor conditions continue to use their frozen venue facts; consumers
   * must label these values as estimates rather than execution evidence.
   */
  priceMoveBpsByWindow: Readonly<Record<string, string>>;
  /**
   * Increments once when the first quote arrives after an initial connection,
   * reconnect, or stale period. Consumers can use it to reload REST history and
   * close any gap without tying REST requests to every quote.
   */
  generation: number;
}>;

export function marketStreamPriceMoves(
  quotes: ReadonlyArray<MarketStreamQuote>,
  windows: ReadonlyArray<number>,
  nowMs: number,
): Readonly<Record<string, string>> {
  const latest = quotes.at(-1);
  if (!latest) return {};
  const latestAt = Date.parse(latest.source_cutoff);
  const latestValue = Number(latest.reference_price);
  if (
    !Number.isFinite(latestAt)
    || !Number.isFinite(latestValue)
    || latestValue <= 0
    || nowMs - latestAt < 0
    || nowMs - latestAt > MARKET_STREAM_STALE_AFTER_MS
  ) {
    return {};
  }
  const result: Record<string, string> = {};
  windows.forEach((windowSeconds) => {
    if (!Number.isInteger(windowSeconds) || windowSeconds < 1 || windowSeconds > 300) {
      return;
    }
    const targetAt = latestAt - windowSeconds * 1_000;
    const start = [...quotes].reverse().find((quote) => (
      Date.parse(quote.source_cutoff) <= targetAt
    ));
    if (!start) return;
    const startAt = Date.parse(start.source_cutoff);
    const startValue = Number(start.reference_price);
    const allowedGapMs = Math.max(3, Math.min(windowSeconds, 15)) * 1_000;
    if (
      !Number.isFinite(startAt)
      || !Number.isFinite(startValue)
      || startValue <= 0
      || targetAt - startAt > allowedGapMs
    ) {
      return;
    }
    const relevant = quotes.filter((quote) => Date.parse(quote.source_cutoff) >= startAt);
    const hasGap = relevant.slice(1).some((quote, index) => (
      Date.parse(quote.source_cutoff)
      - Date.parse(relevant[index]?.source_cutoff ?? "")
      > allowedGapMs
    ));
    if (hasGap) return;
    result[String(windowSeconds)] = (
      (latestValue - startValue) / startValue * 10_000
    ).toFixed(4).replace(/\.?0+$/, "");
  });
  return result;
}

const MARKET_INTERVAL_SET: ReadonlySet<string> = new Set(MARKET_INTERVALS);
const MARKET_INTERVAL_MILLISECONDS = {
  "1m": 60_000,
  "5m": 5 * 60_000,
  "15m": 15 * 60_000,
  "1h": 60 * 60_000,
  "4h": 4 * 60 * 60_000,
  "1d": 24 * 60 * 60_000,
} as const satisfies Readonly<Record<MarketInterval, number>>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isTimestamp(value: unknown): value is string {
  return isNonEmptyString(value) && Number.isFinite(Date.parse(value));
}

function decimalNumber(value: unknown): number | null {
  if (!isNonEmptyString(value)) {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function isMarketInterval(value: unknown): value is MarketInterval {
  return typeof value === "string" && MARKET_INTERVAL_SET.has(value);
}

function parseServerStatus(value: Record<string, unknown>): MarketStreamServerStatus | null {
  const state = value.state;
  const reason = value.reason;
  if (
    (state !== "CONNECTING"
      && state !== "LIVE"
      && state !== "RECONNECTING"
      && state !== "FAILED")
    || !isNonEmptyString(value.source)
    || !isTimestamp(value.observed_at)
    || (reason !== null && typeof reason !== "string")
  ) {
    return null;
  }
  return {
    type: "status",
    state,
    source: value.source,
    observed_at: value.observed_at,
    reason,
  };
}

function parseQuote(value: Record<string, unknown>): MarketStreamQuote | null {
  const bid = decimalNumber(value.bid_price);
  const ask = decimalNumber(value.ask_price);
  const reference = decimalNumber(value.reference_price);
  if (
    !isNonEmptyString(value.instrument_ref)
    || !isNonEmptyString(value.source)
    || !isTimestamp(value.source_cutoff)
    || !isTimestamp(value.received_at)
    || bid === null
    || ask === null
    || reference === null
    || bid <= 0
    || ask < bid
    || reference < bid
    || reference > ask
  ) {
    return null;
  }
  return {
    type: "quote",
    instrument_ref: value.instrument_ref,
    source: value.source,
    source_cutoff: value.source_cutoff,
    received_at: value.received_at,
    bid_price: value.bid_price as string,
    ask_price: value.ask_price as string,
    reference_price: value.reference_price as string,
  };
}

function parseFunding(value: Record<string, unknown>): MarketStreamFunding | null {
  const markPrice = decimalNumber(value.mark_price);
  const indexPrice = decimalNumber(value.index_price);
  const fundingRate = decimalNumber(value.funding_rate);
  if (
    !isNonEmptyString(value.instrument_ref)
    || !isNonEmptyString(value.source)
    || !isTimestamp(value.source_cutoff)
    || !isTimestamp(value.received_at)
    || !isTimestamp(value.next_funding_at)
    || markPrice === null
    || indexPrice === null
    || fundingRate === null
    || markPrice <= 0
    || indexPrice <= 0
    || Math.abs(fundingRate) > 1
  ) {
    return null;
  }
  const sourceCutoff = Date.parse(value.source_cutoff);
  const nextFundingAt = Date.parse(value.next_funding_at);
  if (
    nextFundingAt < sourceCutoff - 5 * 60_000
    || nextFundingAt > sourceCutoff + 7 * 24 * 60 * 60_000
  ) {
    return null;
  }
  return {
    type: "funding",
    instrument_ref: value.instrument_ref,
    source: value.source,
    source_cutoff: value.source_cutoff,
    received_at: value.received_at,
    mark_price: value.mark_price as string,
    index_price: value.index_price as string,
    funding_rate: value.funding_rate as string,
    next_funding_at: value.next_funding_at,
  };
}

function parseBar(value: Record<string, unknown>): MarketStreamBar | null {
  if (
    !isNonEmptyString(value.instrument_ref)
    || !isMarketInterval(value.interval)
    || !isNonEmptyString(value.source)
    || !isTimestamp(value.source_cutoff)
    || !isTimestamp(value.received_at)
    || typeof value.closed !== "boolean"
    || !isRecord(value.bar)
  ) {
    return null;
  }
  const bar = value.bar;
  const open = decimalNumber(bar.open);
  const high = decimalNumber(bar.high);
  const low = decimalNumber(bar.low);
  const close = decimalNumber(bar.close);
  const volume = decimalNumber(bar.volume);
  if (
    !isTimestamp(bar.open_at)
    || !isTimestamp(bar.close_at)
    || Date.parse(bar.close_at) - Date.parse(bar.open_at)
      !== MARKET_INTERVAL_MILLISECONDS[value.interval]
    || open === null
    || high === null
    || low === null
    || close === null
    || volume === null
    || Math.min(open, high, low, close) <= 0
    || volume < 0
    || high < Math.max(open, close)
    || low > Math.min(open, close)
    || high < low
  ) {
    return null;
  }
  return {
    type: "bar",
    instrument_ref: value.instrument_ref,
    interval: value.interval,
    source: value.source,
    source_cutoff: value.source_cutoff,
    received_at: value.received_at,
    closed: value.closed,
    bar: {
      open_at: bar.open_at,
      close_at: bar.close_at,
      open: bar.open as string,
      high: bar.high as string,
      low: bar.low as string,
      close: bar.close as string,
      volume: bar.volume as string,
    },
  };
}

function parseDepthLevels(
  value: unknown,
): ReadonlyArray<MarketStreamDepthLevel> | null {
  if (!Array.isArray(value) || value.length < 1 || value.length > 10) {
    return null;
  }
  const levels: MarketStreamDepthLevel[] = [];
  for (const item of value) {
    if (!isRecord(item)) return null;
    const price = decimalNumber(item.price);
    const quantity = decimalNumber(item.quantity);
    if (
      !isNonEmptyString(item.price)
      || !isNonEmptyString(item.quantity)
      || price === null
      || quantity === null
      || price <= 0
      || quantity <= 0
    ) {
      return null;
    }
    levels.push({ price: item.price, quantity: item.quantity });
  }
  return levels;
}

function isStrictlyOrdered(
  levels: ReadonlyArray<MarketStreamDepthLevel>,
  direction: "DESC" | "ASC",
): boolean {
  return levels.slice(1).every((level, index) => {
    const prior = Number(levels[index]?.price);
    const current = Number(level.price);
    return direction === "DESC" ? prior > current : prior < current;
  });
}

function parseDepth(value: Record<string, unknown>): MarketStreamDepth | null {
  const bids = parseDepthLevels(value.bids);
  const asks = parseDepthLevels(value.asks);
  if (
    !isNonEmptyString(value.instrument_ref)
    || !isNonEmptyString(value.source)
    || !isTimestamp(value.source_cutoff)
    || !isTimestamp(value.received_at)
    || typeof value.update_id !== "number"
    || !Number.isSafeInteger(value.update_id)
    || value.update_id <= 0
    || bids === null
    || asks === null
    || !isStrictlyOrdered(bids, "DESC")
    || !isStrictlyOrdered(asks, "ASC")
    || Number(bids[0]?.price) >= Number(asks[0]?.price)
  ) {
    return null;
  }
  return {
    type: "depth",
    instrument_ref: value.instrument_ref,
    source: value.source,
    source_cutoff: value.source_cutoff,
    received_at: value.received_at,
    update_id: value.update_id,
    bids,
    asks,
  };
}

function parseTrade(value: Record<string, unknown>): MarketStreamTrade | null {
  const price = decimalNumber(value.price);
  const quantity = decimalNumber(value.quantity);
  if (
    !isNonEmptyString(value.instrument_ref)
    || !isNonEmptyString(value.source)
    || !isTimestamp(value.source_cutoff)
    || !isTimestamp(value.received_at)
    || !isNonEmptyString(value.trade_id)
    || !isNonEmptyString(value.price)
    || !isNonEmptyString(value.quantity)
    || (value.aggressor_side !== "BUYER" && value.aggressor_side !== "SELLER")
    || price === null
    || quantity === null
    || price <= 0
    || quantity <= 0
  ) {
    return null;
  }
  return {
    type: "trade",
    instrument_ref: value.instrument_ref,
    source: value.source,
    source_cutoff: value.source_cutoff,
    received_at: value.received_at,
    trade_id: value.trade_id,
    price: value.price,
    quantity: value.quantity,
    aggressor_side: value.aggressor_side,
  };
}

/**
 * Parses the local relay boundary defensively. Unknown or malformed messages
 * are ignored instead of being allowed to corrupt the visible market state.
 */
export function parseMarketStreamEvent(raw: unknown): MarketStreamEvent | null {
  let value: unknown = raw;
  if (typeof raw === "string") {
    try {
      value = JSON.parse(raw) as unknown;
    } catch {
      return null;
    }
  }
  if (!isRecord(value)) {
    return null;
  }
  if (value.type === "status") {
    return parseServerStatus(value);
  }
  if (value.type === "quote") {
    return parseQuote(value);
  }
  if (value.type === "funding") {
    return parseFunding(value);
  }
  if (value.type === "bar") {
    return parseBar(value);
  }
  if (value.type === "depth") {
    return parseDepth(value);
  }
  if (value.type === "trade") {
    return parseTrade(value);
  }
  return null;
}

export function shouldUseMarketStreamBar(
  event: MarketStreamBar,
  instrumentRef: string,
  interval: MarketInterval,
  expectedSource?: string | null,
): boolean {
  return event.instrument_ref === instrumentRef
    && event.interval === interval
    && (!expectedSource || event.source === expectedSource);
}

export function marketStreamReconnectDelayMs(attempt: number): number {
  const boundedAttempt = Number.isFinite(attempt)
    ? Math.max(0, Math.floor(attempt))
    : 0;
  return Math.min(
    MARKET_STREAM_RECONNECT_BASE_MS * (2 ** boundedAttempt),
    MARKET_STREAM_RECONNECT_MAX_MS,
  );
}

export function nextMarketStreamGeneration(
  generation: number,
  awaitingFreshQuote: boolean,
): number {
  return generation + (awaitingFreshQuote ? 1 : 0);
}

export function marketStreamQuoteThrottleDelayMs(
  lastCommitAt: number,
  now: number,
): number {
  if (lastCommitAt <= 0) {
    return 0;
  }
  return Math.max(
    0,
    Math.min(
      MARKET_STREAM_QUOTE_THROTTLE_MS,
      MARKET_STREAM_QUOTE_THROTTLE_MS - (now - lastCommitAt),
    ),
  );
}

export function isMarketStreamQuoteStale(
  lastQuoteReceivedAt: number | null,
  now: number,
): boolean {
  return lastQuoteReceivedAt !== null
    && now - lastQuoteReceivedAt >= MARKET_STREAM_STALE_AFTER_MS;
}

export function isUsableExecutionQuote(
  quote: MarketStreamQuote | null,
  expectedSource: string | null,
  now: number,
): quote is MarketStreamQuote {
  if (quote === null) return false;
  if (expectedSource === null || quote.source !== expectedSource) return false;
  return isCurrentStreamTimestamp(quote.source_cutoff, now)
    && isCurrentStreamTimestamp(quote.received_at, now);
}

export function isUsableMarketStreamBar(
  bar: MarketStreamBar | null,
  expectedSource: string | null,
  now: number,
): bar is MarketStreamBar {
  if (bar === null) return false;
  if (expectedSource === null || bar.source !== expectedSource) return false;
  return isCurrentStreamTimestamp(bar.source_cutoff, now)
    && isCurrentStreamTimestamp(bar.received_at, now);
}

export function isUsableMarketStreamFunding(
  funding: MarketStreamFunding | null,
  expectedSource: string | null,
  now: number,
): funding is MarketStreamFunding {
  if (funding === null) return false;
  if (expectedSource === null || funding.source !== expectedSource) return false;
  return isCurrentStreamTimestamp(funding.source_cutoff, now)
    && isCurrentStreamTimestamp(funding.received_at, now);
}

export function isUsableMarketStreamDepth(
  depth: MarketStreamDepth | null,
  expectedSource: string | null,
  now: number,
): depth is MarketStreamDepth {
  if (depth === null) return false;
  if (expectedSource === null || depth.source !== expectedSource) return false;
  return isCurrentStreamTimestamp(depth.source_cutoff, now)
    && isCurrentStreamTimestamp(depth.received_at, now);
}

export function isUsableMarketStreamTrade(
  trade: MarketStreamTrade | null,
  expectedSource: string | null,
  now: number,
): trade is MarketStreamTrade {
  if (trade === null) return false;
  if (expectedSource === null || trade.source !== expectedSource) return false;
  return isCurrentStreamTimestamp(trade.source_cutoff, now)
    && isCurrentStreamTimestamp(trade.received_at, now);
}

function isCurrentStreamTimestamp(value: string, now: number): boolean {
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp)
    && timestamp <= now + MARKET_STREAM_MAX_FUTURE_SKEW_MS
    && now - timestamp < MARKET_STREAM_STALE_AFTER_MS;
}

export function marketStreamWebSocketUrl(
  pageUrl: string,
  instrumentRef: string,
): string {
  const url = new URL("/api/v1/market-stream", pageUrl);
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new TypeError("MARKET_STREAM_PAGE_PROTOCOL_UNSUPPORTED");
  }
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("instrument_ref", instrumentRef);
  return url.toString();
}

function initialSnapshot(
  enabled: boolean,
  environmentScope: string,
): PublicMarketStreamSnapshot {
  return {
    environmentScope,
    status: enabled ? "CONNECTING" : "DISABLED",
    statusReason: null,
    statusSource: null,
    statusObservedAt: null,
    quote: null,
    liveBar: null,
    funding: null,
    depth: null,
    trades: [],
    priceMoveBpsByWindow: {},
    generation: 0,
  };
}

/**
 * Subscribes to Halpha's same-origin, read-only public-market relay.
 *
 * The local socket reconnects with a capped exponential retry schedule. Venue
 * reconnect and resubscription remain owned by the server-side market adapter.
 */
export function usePublicMarketStream(
  enabled: boolean,
  instrumentRef: string,
  selectedInterval: MarketInterval,
  environmentScope: string,
  expectedSource: string | null,
  priceMoveWindows: ReadonlyArray<number> = [],
): PublicMarketStreamSnapshot {
  const priceMoveWindowKey = [...new Set(priceMoveWindows)]
    .filter((windowSeconds) => (
      Number.isInteger(windowSeconds)
      && windowSeconds >= 1
      && windowSeconds <= 300
    ))
    .sort((left, right) => left - right)
    .join(",");
  const [snapshot, setSnapshot] = useState<PublicMarketStreamSnapshot>(
    () => initialSnapshot(enabled, environmentScope),
  );
  const [documentVisible, setDocumentVisible] = useState(() => (
    shouldObservePublicMarketStream(
      true,
      typeof document === "undefined" ? undefined : document.visibilityState,
    )
  ));
  const selectedIntervalRef = useRef(selectedInterval);
  const generationRef = useRef(0);
  selectedIntervalRef.current = selectedInterval;

  useEffect(() => {
    if (typeof document === "undefined") return undefined;
    const syncVisibility = (): void => {
      setDocumentVisible(document.visibilityState === "visible");
    };
    syncVisibility();
    document.addEventListener("visibilitychange", syncVisibility);
    return () => document.removeEventListener("visibilitychange", syncVisibility);
  }, []);

  const streamEnabled = enabled && documentVisible;

  useEffect(() => {
    setSnapshot((current) => (
      current.liveBar === null || current.liveBar.interval === selectedInterval
        ? current
        : { ...current, liveBar: null }
    ));
  }, [selectedInterval]);

  useEffect(() => {
    if (!streamEnabled) {
      setSnapshot(initialSnapshot(false, environmentScope));
      return undefined;
    }
    if (expectedSource === null) {
      setSnapshot({
        ...initialSnapshot(true, environmentScope),
        status: "FAILED",
        statusReason: "MARKET_STREAM_ENVIRONMENT_UNSUPPORTED",
      });
      return undefined;
    }
    if (typeof window === "undefined" || typeof WebSocket === "undefined") {
      setSnapshot({
        ...initialSnapshot(true, environmentScope),
        status: "FAILED",
        statusReason: "LOCAL_WEBSOCKET_UNAVAILABLE",
      });
      return undefined;
    }

    let disposed = false;
    let socket: WebSocket | null = null;
    let reconnectAttempt = 0;
    let reconnectTimer: number | null = null;
    let quoteTimer: number | null = null;
    let flowTimer: number | null = null;
    let staleTimer: number | null = null;
    let pendingQuote: MarketStreamQuote | null = null;
    let pendingDepth: MarketStreamDepth | null = null;
    let pendingTrades: MarketStreamTrade[] = [];
    let recentQuotes: MarketStreamQuote[] = [];
    let lastQuoteCommitAt = 0;
    let lastQuoteReceivedAt: number | null = null;
    let awaitingFreshQuote = true;

    setSnapshot(initialSnapshot(true, environmentScope));

    const clearTimer = (timer: number | null): void => {
      if (timer !== null) {
        window.clearTimeout(timer);
      }
    };

    const clearPendingQuote = (): void => {
      clearTimer(quoteTimer);
      quoteTimer = null;
      pendingQuote = null;
    };

    const clearPendingFlow = (): void => {
      clearTimer(flowTimer);
      flowTimer = null;
      pendingDepth = null;
      pendingTrades = [];
    };

    const commitPendingFlow = (): void => {
      flowTimer = null;
      if (disposed || (pendingDepth === null && pendingTrades.length === 0)) {
        return;
      }
      const depth = pendingDepth;
      const trades = pendingTrades;
      pendingDepth = null;
      pendingTrades = [];
      const now = Date.now();
      setSnapshot((current) => {
        const usableCurrentTrades = current.trades.filter((trade) => (
          isUsableMarketStreamTrade(trade, expectedSource, now)
        ));
        const byTradeId = new Map(
          [...usableCurrentTrades, ...trades].map((trade) => [trade.trade_id, trade]),
        );
        const boundedTrades = [...byTradeId.values()]
          .sort((left, right) => (
            Date.parse(left.source_cutoff) - Date.parse(right.source_cutoff)
          ))
          .slice(-MARKET_STREAM_MAX_TRADE_TAPE_EVENTS);
        return {
          ...current,
          depth: depth ?? current.depth,
          trades: boundedTrades,
        };
      });
    };

    const scheduleFlowCommit = (): void => {
      if (flowTimer !== null) return;
      flowTimer = window.setTimeout(
        commitPendingFlow,
        MARKET_STREAM_FLOW_THROTTLE_MS,
      );
    };

    const clearQuoteHistory = (): void => {
      recentQuotes = [];
    };

    const armStaleTimer = (): void => {
      clearTimer(staleTimer);
      const quoteAtWhenArmed = lastQuoteReceivedAt;
      staleTimer = window.setTimeout(() => {
        staleTimer = null;
        if (
          disposed
          || lastQuoteReceivedAt !== quoteAtWhenArmed
          || (
            quoteAtWhenArmed !== null
            && !isMarketStreamQuoteStale(quoteAtWhenArmed, Date.now())
          )
        ) {
          return;
        }
        awaitingFreshQuote = true;
        clearPendingFlow();
        setSnapshot((current) => ({
          ...current,
          status: "STALE",
          statusReason: "MARKET_STREAM_QUOTE_STALE",
          statusObservedAt: new Date().toISOString(),
          quote: null,
          liveBar: null,
          funding: null,
          depth: null,
          trades: [],
          priceMoveBpsByWindow: {},
        }));
      }, MARKET_STREAM_STALE_AFTER_MS);
    };

    const commitPendingQuote = (): void => {
      quoteTimer = null;
      if (disposed || pendingQuote === null) {
        return;
      }
      const quote = pendingQuote;
      pendingQuote = null;
      const recovered = awaitingFreshQuote;
      awaitingFreshQuote = false;
      reconnectAttempt = 0;
      lastQuoteCommitAt = Date.now();
      const quoteAt = Date.parse(quote.source_cutoff);
      recentQuotes = [
        ...recentQuotes.filter((item) => (
          Date.parse(item.source_cutoff) >= quoteAt - 310_000
        )),
        quote,
      ];
      const configuredWindows = priceMoveWindowKey
        ? priceMoveWindowKey.split(",").map(Number)
        : [];
      generationRef.current = nextMarketStreamGeneration(generationRef.current, recovered);
      const generation = generationRef.current;
      setSnapshot((current) => ({
        ...current,
        status: "LIVE",
        statusReason: null,
        statusSource: quote.source,
        statusObservedAt: quote.received_at,
        quote,
        priceMoveBpsByWindow: marketStreamPriceMoves(
          recentQuotes,
          configuredWindows,
          Date.now(),
        ),
        generation,
      }));
    };

    const receiveQuote = (quote: MarketStreamQuote): void => {
      const receivedNow = Date.now();
      if (!isUsableExecutionQuote(quote, expectedSource, receivedNow)) {
        lastQuoteReceivedAt = null;
        awaitingFreshQuote = true;
        clearPendingQuote();
        clearPendingFlow();
        clearQuoteHistory();
        clearTimer(staleTimer);
        staleTimer = null;
        setSnapshot((current) => ({
          ...current,
          status: "STALE",
          statusReason: "MARKET_STREAM_QUOTE_TIMESTAMP_INVALID",
          statusObservedAt: new Date(receivedNow).toISOString(),
          quote: null,
          liveBar: null,
          funding: null,
          depth: null,
          trades: [],
          priceMoveBpsByWindow: {},
        }));
        return;
      }
      lastQuoteReceivedAt = receivedNow;
      armStaleTimer();
      pendingQuote = quote;
      const remaining = marketStreamQuoteThrottleDelayMs(
        lastQuoteCommitAt,
        Date.now(),
      );
      if (remaining === 0) {
        clearTimer(quoteTimer);
        commitPendingQuote();
      } else if (quoteTimer === null) {
        quoteTimer = window.setTimeout(commitPendingQuote, remaining);
      }
    };

    const applyServerStatus = (event: MarketStreamServerStatus): void => {
      if (event.source !== expectedSource) {
        awaitingFreshQuote = true;
        clearPendingQuote();
        clearPendingFlow();
        clearQuoteHistory();
        clearTimer(staleTimer);
        staleTimer = null;
        setSnapshot({
          ...initialSnapshot(true, environmentScope),
          status: "FAILED",
          statusReason: "MARKET_STREAM_SOURCE_MISMATCH",
          statusSource: event.source,
          statusObservedAt: event.observed_at,
        });
        return;
      }
      if (event.state === "LIVE") {
        const missing = lastQuoteReceivedAt === null;
        const stale = missing
          || isMarketStreamQuoteStale(lastQuoteReceivedAt, Date.now());
        if (stale) clearPendingFlow();
        setSnapshot((current) => ({
          ...current,
          status: stale ? "STALE" : "LIVE",
          statusReason: missing
            ? "MARKET_STREAM_QUOTE_MISSING"
            : stale
              ? "MARKET_STREAM_QUOTE_STALE"
              : event.reason,
          statusSource: event.source,
          statusObservedAt: event.observed_at,
          ...(stale ? { quote: null, liveBar: null } : {}),
          ...(stale ? { funding: null } : {}),
          ...(stale ? { depth: null, trades: [] } : {}),
          ...(stale ? { priceMoveBpsByWindow: {} } : {}),
        }));
        armStaleTimer();
        return;
      }
      awaitingFreshQuote = true;
      clearPendingQuote();
      clearPendingFlow();
      clearQuoteHistory();
      clearTimer(staleTimer);
      staleTimer = null;
      setSnapshot((current) => ({
        ...current,
        status: event.state,
        statusReason: event.reason,
        statusSource: event.source,
        statusObservedAt: event.observed_at,
        quote: null,
        liveBar: null,
        funding: null,
        depth: null,
        trades: [],
        priceMoveBpsByWindow: {},
      }));
    };

    const scheduleReconnect = (reason: string): void => {
      if (disposed || reconnectTimer !== null) {
        return;
      }
      awaitingFreshQuote = true;
      clearPendingQuote();
      clearPendingFlow();
      clearQuoteHistory();
      clearTimer(staleTimer);
      staleTimer = null;
      const delay = marketStreamReconnectDelayMs(reconnectAttempt);
      reconnectAttempt += 1;
      setSnapshot((current) => ({
        ...current,
        status: "RECONNECTING",
        statusReason: reason,
        statusObservedAt: new Date().toISOString(),
        quote: null,
        liveBar: null,
        funding: null,
        depth: null,
        trades: [],
        priceMoveBpsByWindow: {},
      }));
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, delay);
    };

    const connect = (): void => {
      if (disposed) {
        return;
      }
      let nextSocket: WebSocket;
      try {
        nextSocket = new WebSocket(
          marketStreamWebSocketUrl(window.location.href, instrumentRef),
        );
      } catch {
        scheduleReconnect("LOCAL_WEBSOCKET_CONSTRUCTION_FAILED");
        return;
      }
      socket = nextSocket;

      nextSocket.onmessage = (message) => {
        if (disposed || socket !== nextSocket) {
          return;
        }
        const event = parseMarketStreamEvent(message.data);
        if (event === null) {
          return;
        }
        if (event.type === "status") {
          applyServerStatus(event);
        } else if (
          (event.type === "quote"
            || event.type === "bar"
            || event.type === "funding"
            || event.type === "depth"
            || event.type === "trade")
          && event.instrument_ref === instrumentRef
          && event.source !== expectedSource
        ) {
          awaitingFreshQuote = true;
          clearPendingQuote();
          clearPendingFlow();
          clearQuoteHistory();
          clearTimer(staleTimer);
          staleTimer = null;
          setSnapshot({
            ...initialSnapshot(true, environmentScope),
            status: "FAILED",
            statusReason: "MARKET_STREAM_SOURCE_MISMATCH",
            statusSource: event.source,
            statusObservedAt: event.received_at,
            priceMoveBpsByWindow: {},
          });
        } else if (
          event.type === "quote"
          && event.instrument_ref === instrumentRef
          && event.source === expectedSource
        ) {
          receiveQuote(event);
        } else if (
          event.type === "funding"
          && event.instrument_ref === instrumentRef
          && event.source === expectedSource
        ) {
          setSnapshot((current) => ({
            ...current,
            funding: isUsableMarketStreamFunding(
              event,
              expectedSource,
              Date.now(),
            ) ? event : null,
          }));
        } else if (
          event.type === "depth"
          && event.instrument_ref === instrumentRef
          && event.source === expectedSource
        ) {
          if (!isUsableMarketStreamDepth(event, expectedSource, Date.now())) {
            pendingDepth = null;
            setSnapshot((current) => ({ ...current, depth: null }));
            return;
          }
          pendingDepth = event;
          scheduleFlowCommit();
        } else if (
          event.type === "trade"
          && event.instrument_ref === instrumentRef
          && event.source === expectedSource
        ) {
          if (!isUsableMarketStreamTrade(event, expectedSource, Date.now())) {
            return;
          }
          pendingTrades = [...pendingTrades, event].slice(
            -MARKET_STREAM_MAX_TRADE_TAPE_EVENTS,
          );
          scheduleFlowCommit();
        } else if (
          event.type === "bar"
          && shouldUseMarketStreamBar(
            event,
            instrumentRef,
            selectedIntervalRef.current,
            expectedSource,
          )
        ) {
          if (!isUsableMarketStreamBar(event, expectedSource, Date.now())) {
            lastQuoteReceivedAt = null;
            awaitingFreshQuote = true;
            clearPendingQuote();
            clearPendingFlow();
            clearQuoteHistory();
            clearTimer(staleTimer);
            staleTimer = null;
            setSnapshot((current) => ({
              ...current,
              status: "STALE",
              statusReason: "MARKET_STREAM_BAR_TIMESTAMP_INVALID",
              statusObservedAt: new Date().toISOString(),
              quote: null,
              liveBar: null,
              funding: null,
              depth: null,
              trades: [],
              priceMoveBpsByWindow: {},
            }));
            return;
          }
          setSnapshot((current) => ({ ...current, liveBar: event }));
        }
      };

      nextSocket.onclose = (event) => {
        if (disposed || socket !== nextSocket) {
          return;
        }
        socket = null;
        awaitingFreshQuote = true;
        clearPendingQuote();
        clearPendingFlow();
        clearQuoteHistory();
        clearTimer(staleTimer);
        staleTimer = null;
        if (event.code === 1008) {
          setSnapshot((current) => ({
            ...current,
            status: "FAILED",
            statusReason: "LOCAL_WEBSOCKET_POLICY_REJECTED",
            statusObservedAt: new Date().toISOString(),
            quote: null,
            liveBar: null,
            funding: null,
            depth: null,
            trades: [],
          }));
          return;
        }
        scheduleReconnect(`LOCAL_WEBSOCKET_CLOSED_${event.code}`);
      };

      nextSocket.onerror = () => {
        if (
          disposed
          || socket !== nextSocket
          || nextSocket.readyState === WebSocket.CLOSING
          || nextSocket.readyState === WebSocket.CLOSED
        ) {
          return;
        }
        nextSocket.close();
      };
    };

    connect();

    return () => {
      disposed = true;
      clearTimer(reconnectTimer);
      clearTimer(quoteTimer);
      clearTimer(flowTimer);
      clearTimer(staleTimer);
      reconnectTimer = null;
      quoteTimer = null;
      flowTimer = null;
      staleTimer = null;
      pendingQuote = null;
      pendingDepth = null;
      pendingTrades = [];
      if (socket !== null) {
        const closingSocket = socket;
        socket = null;
        closingSocket.onopen = null;
        closingSocket.onmessage = null;
        closingSocket.onerror = null;
        closingSocket.onclose = null;
        if (
          closingSocket.readyState === WebSocket.CONNECTING
          || closingSocket.readyState === WebSocket.OPEN
        ) {
          closingSocket.close(1000, "MARKET_STREAM_HOOK_DISPOSED");
        }
      }
    };
  }, [environmentScope, expectedSource, instrumentRef, priceMoveWindowKey, streamEnabled]);

  return snapshot.environmentScope === environmentScope
    ? snapshot
    : initialSnapshot(streamEnabled, environmentScope);
}
