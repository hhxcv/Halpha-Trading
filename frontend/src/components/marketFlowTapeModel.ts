import type {
  MarketStreamDepth,
  MarketStreamDepthLevel,
  MarketStreamTrade,
} from "../marketStream";

export const MARKET_FLOW_TAPE_WINDOW_MS = 8_000;
export const MARKET_FLOW_TAPE_BUCKET_MS = 1_000;
/** Labels stay legible in the tape instead of becoming an unreadable cloud. */
export const MARKET_FLOW_TAPE_MAX_PRINTS = 24;
export const MARKET_FLOW_BOOK_VISIBLE_LEVELS = 5;

export type TapePrint = Readonly<{
  id: string;
  at: number;
  price: string;
  quantity: number;
  aggressorSide: "BUYER" | "SELLER";
  tradeCount: number;
}>;

export type OrderBookDisplayLevel = Readonly<{
  price: string;
  quantity: string;
  quantityNumber: number;
  shareOfVisibleDepth: number;
}>;

export type MarketFlowBook = Readonly<{
  asks: ReadonlyArray<OrderBookDisplayLevel>;
  bids: ReadonlyArray<OrderBookDisplayLevel>;
}>;

function positiveFinite(value: string): number | null {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}

/**
 * Aggregates only the current, bounded public tape by price, aggressor, and a
 * short time bucket. It is a visual compression, not a reconstructed exchange
 * matching record.
 */
export function aggregateMarketStreamTrades(
  trades: ReadonlyArray<MarketStreamTrade>,
  now: number,
  windowMs = MARKET_FLOW_TAPE_WINDOW_MS,
  bucketMs = MARKET_FLOW_TAPE_BUCKET_MS,
): ReadonlyArray<TapePrint> {
  if (
    !Number.isFinite(now)
    || !Number.isSafeInteger(windowMs)
    || windowMs < 1
    || !Number.isSafeInteger(bucketMs)
    || bucketMs < 1
  ) {
    return [];
  }
  const latestByTradeId = new Map<string, MarketStreamTrade>();
  trades.forEach((trade) => latestByTradeId.set(trade.trade_id, trade));
  const aggregate = new Map<string, TapePrint>();
  for (const trade of latestByTradeId.values()) {
    const at = Date.parse(trade.source_cutoff);
    const quantity = positiveFinite(trade.quantity);
    const price = positiveFinite(trade.price);
    if (
      !Number.isFinite(at)
      || at < now - windowMs
      || at > now + 5_000
      || quantity === null
      || price === null
    ) {
      continue;
    }
    const bucketAt = Math.floor(at / bucketMs) * bucketMs;
    const key = `${bucketAt}:${trade.aggressor_side}:${trade.price}`;
    const current = aggregate.get(key);
    aggregate.set(key, current
      ? {
          ...current,
          quantity: current.quantity + quantity,
          tradeCount: current.tradeCount + 1,
          at: Math.max(current.at, at),
        }
      : {
          id: key,
          at,
          price: trade.price,
          quantity,
          aggressorSide: trade.aggressor_side,
          tradeCount: 1,
        });
  }
  return [...aggregate.values()]
    .sort((left, right) => left.at - right.at || left.id.localeCompare(right.id))
    .slice(-MARKET_FLOW_TAPE_MAX_PRINTS);
}

function displayLevel(
  level: MarketStreamDepthLevel,
  visibleDepth: number,
): OrderBookDisplayLevel | null {
  const quantityNumber = positiveFinite(level.quantity);
  if (quantityNumber === null || visibleDepth <= 0) return null;
  return {
    price: level.price,
    quantity: level.quantity,
    quantityNumber,
    shareOfVisibleDepth: Math.max(0, Math.min(1, quantityNumber / visibleDepth)),
  };
}

/** Places the best ask and best bid at the center of the ladder. */
export function marketFlowBook(depth: MarketStreamDepth | null): MarketFlowBook {
  if (depth === null) return { asks: [], bids: [] };
  const visibleAsks = depth.asks.slice(0, MARKET_FLOW_BOOK_VISIBLE_LEVELS);
  const visibleBids = depth.bids.slice(0, MARKET_FLOW_BOOK_VISIBLE_LEVELS);
  const visibleDepth = [...visibleAsks, ...visibleBids]
    .reduce((sum, level) => sum + (positiveFinite(level.quantity) ?? 0), 0);
  const toDisplayLevel = (level: MarketStreamDepthLevel) => (
    displayLevel(level, visibleDepth)
  );
  return {
    asks: visibleAsks
      .map(toDisplayLevel)
      .filter((level): level is OrderBookDisplayLevel => level !== null)
      .reverse(),
    bids: visibleBids
      .map(toDisplayLevel)
      .filter((level): level is OrderBookDisplayLevel => level !== null),
  };
}

export function tapePriceDomain(
  prints: ReadonlyArray<TapePrint>,
  depth: MarketStreamDepth | null,
): readonly [number, number] | null {
  const prices = [
    ...prints.map((print) => positiveFinite(print.price)),
    ...(depth?.bids.map((level) => positiveFinite(level.price)) ?? []),
    ...(depth?.asks.map((level) => positiveFinite(level.price)) ?? []),
  ].filter((price): price is number => price !== null);
  if (prices.length === 0) return null;
  const low = Math.min(...prices);
  const high = Math.max(...prices);
  if (low === high) {
    const padding = Math.max(low * 0.00005, 0.00000001);
    return [low - padding, high + padding];
  }
  const padding = (high - low) * 0.1;
  return [low - padding, high + padding];
}

export function tapeBubbleRadius(
  quantity: number,
  maximumQuantity: number,
): number {
  if (!Number.isFinite(quantity) || quantity <= 0) return 4;
  const scale = Number.isFinite(maximumQuantity) && maximumQuantity > 0
    ? Math.sqrt(quantity / maximumQuantity)
    : 0;
  return Math.max(4, Math.min(18, 4 + scale * 14));
}

/** Compact, stable tape labels retain useful small volumes without false precision. */
export function formatTapeVolume(quantity: number): string {
  if (!Number.isFinite(quantity) || quantity <= 0) return "—";
  return new Intl.NumberFormat("en-US", {
    notation: quantity >= 1_000 ? "compact" : "standard",
    compactDisplay: "short",
    maximumSignificantDigits: 3,
    minimumSignificantDigits: 1,
    useGrouping: false,
  }).format(quantity);
}
