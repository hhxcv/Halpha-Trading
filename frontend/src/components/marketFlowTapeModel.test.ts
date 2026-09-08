import { describe, expect, it } from "vitest";

import type { MarketStreamDepth, MarketStreamTrade } from "../marketStream";
import {
  aggregateMarketStreamTrades,
  formatTapeVolume,
  marketFlowBook,
  tapeBubbleRadius,
  tapePriceDomain,
} from "./marketFlowTapeModel";

const at = Date.parse("2026-08-16T08:00:10.000Z");

const trade = (
  id: string,
  milliseconds: number,
  price: string,
  quantity: string,
  aggressor_side: "BUYER" | "SELLER" = "BUYER",
): MarketStreamTrade => ({
  type: "trade",
  instrument_ref: "BTCUSDT-PERP",
  source: "BINANCE_DEMO_PUBLIC",
  source_cutoff: new Date(at + milliseconds).toISOString(),
  received_at: new Date(at + milliseconds).toISOString(),
  trade_id: id,
  price,
  quantity,
  aggressor_side,
});

const depth: MarketStreamDepth = {
  type: "depth",
  instrument_ref: "BTCUSDT-PERP",
  source: "BINANCE_DEMO_PUBLIC",
  source_cutoff: new Date(at).toISOString(),
  received_at: new Date(at).toISOString(),
  update_id: 1,
  asks: [
    { price: "101", quantity: "2" },
    { price: "102", quantity: "4" },
  ],
  bids: [
    { price: "100", quantity: "3" },
    { price: "99", quantity: "1" },
  ],
};

describe("market flow tape model", () => {
  it("compresses current prints by bucket, price, and aggressor", () => {
    const prints = aggregateMarketStreamTrades([
      trade("1", 0, "100", "1"),
      trade("2", 80, "100", "2"),
      trade("3", 100, "100", "3", "SELLER"),
      trade("expired", -21_000, "100", "8"),
    ], at + 200);

    expect(prints).toHaveLength(2);
    expect(prints[0]).toMatchObject({
      price: "100",
      quantity: 3,
      aggressorSide: "BUYER",
      tradeCount: 2,
    });
    expect(prints[1]).toMatchObject({
      quantity: 3,
      aggressorSide: "SELLER",
      tradeCount: 1,
    });
  });

  it("centers the best prices and scales book bars by visible depth", () => {
    const book = marketFlowBook(depth);

    expect(book.asks.map((level) => level.price)).toEqual(["102", "101"]);
    expect(book.bids.map((level) => level.price)).toEqual(["100", "99"]);
    expect(book.bids[0]?.shareOfVisibleDepth).toBeCloseTo(0.3);
    expect(book.asks[1]?.shareOfVisibleDepth).toBeCloseTo(0.2);
  });

  it("derives a stable visible price domain and bounded circle radii", () => {
    const prints = aggregateMarketStreamTrades([
      trade("1", 0, "100", "1"),
      trade("2", 100, "103", "9"),
    ], at + 200);
    expect(tapePriceDomain(prints, depth)).toEqual([98.6, 103.4]);
    expect(tapeBubbleRadius(1, 9)).toBeGreaterThanOrEqual(4);
    expect(tapeBubbleRadius(9, 9)).toBe(18);
    expect(tapeBubbleRadius(0, 9)).toBe(4);
  });

  it("uses compact three-significant-digit labels for tape volumes", () => {
    expect(formatTapeVolume(0.0012345)).toBe("0.00123");
    expect(formatTapeVolume(51.9074)).toBe("51.9");
    expect(formatTapeVolume(882.1535)).toBe("882");
    expect(formatTapeVolume(2_452.5603)).toBe("2.45K");
    expect(formatTapeVolume(0)).toBe("—");
  });
});
