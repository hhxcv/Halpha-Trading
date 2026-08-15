import { describe, expect, it } from "vitest";

import type { OrderScheduleCondition } from "../api/client";
import {
  currentEntryBoundaryBreach,
  entrySignalQualityWarning,
  takeProfitAfterCostEstimate,
  takeProfitSpreadCoverageWarning,
} from "./orderScheduleDecisionAid";

const ready: OrderScheduleCondition = { kind: "DECISION_BASIS_READY" };
const mark: OrderScheduleCondition = {
  kind: "MARK_PRICE",
  comparator: "LTE",
  price: "65000",
};

describe("currentEntryBoundaryBreach", () => {
  it("blocks a long entry at or below its invalidation price", () => {
    expect(currentEntryBoundaryBreach({
      direction: "LONG",
      referencePrice: "64780",
      invalidationPrice: "64780",
      opportunityMissedPrice: "64920",
    })).toEqual({
      kind: "ENTRY_INVALIDATED",
      currentPrice: 64780,
      boundaryPrice: 64780,
    });
  });

  it("blocks a long entry at or above its opportunity-missed price", () => {
    expect(currentEntryBoundaryBreach({
      direction: "LONG",
      referencePrice: "64980.9",
      invalidationPrice: "64780",
      opportunityMissedPrice: "64920",
    })).toEqual({
      kind: "OPPORTUNITY_MISSED",
      currentPrice: 64980.9,
      boundaryPrice: 64920,
    });
  });

  it("applies the inverse boundaries to a short entry", () => {
    expect(currentEntryBoundaryBreach({
      direction: "SHORT",
      referencePrice: "65020",
      invalidationPrice: "65000",
      opportunityMissedPrice: "64700",
    })?.kind).toBe("ENTRY_INVALIDATED");
    expect(currentEntryBoundaryBreach({
      direction: "SHORT",
      referencePrice: "64690",
      invalidationPrice: "65000",
      opportunityMissedPrice: "64700",
    })?.kind).toBe("OPPORTUNITY_MISSED");
  });

  it("does not block a price inside the configured entry range", () => {
    expect(currentEntryBoundaryBreach({
      direction: "LONG",
      referencePrice: "64875",
      invalidationPrice: "64780",
      opportunityMissedPrice: "64920",
    })).toBeNull();
  });

  it("stays quiet without a fresh usable reference price", () => {
    expect(currentEntryBoundaryBreach({
      direction: "LONG",
      referencePrice: null,
      invalidationPrice: "64780",
      opportunityMissedPrice: "64920",
    })).toBeNull();
  });
});

describe("entrySignalQualityWarning", () => {
  it("keeps an intentionally immediate plan quiet", () => {
    expect(entrySignalQualityWarning("LONG", "ALL", [ready])).toBeNull();
  });

  it("does not mistake a spread-only constraint for a directional entry signal", () => {
    expect(entrySignalQualityWarning("SHORT", "ALL", [
      ready,
      { kind: "SPREAD_BPS", maximum_bps: "3" },
    ])).toContain("不提供交易方向或入场时机");
  });

  it("explains that ANY does not form a combined confirmation", () => {
    expect(entrySignalQualityWarning("LONG", "ANY", [
      ready,
      mark,
      { kind: "SPREAD_BPS", maximum_bps: "10" },
    ])).toContain("并不构成组合确认");
  });

  it("warns when a mark-price threshold is mistaken for reversal confirmation", () => {
    expect(entrySignalQualityWarning("LONG", "ALL", [ready, mark]))
      .toContain("不确认触达顺序、反转或突破延续");
  });

  it("requires the short-window direction to match the trade direction", () => {
    expect(entrySignalQualityWarning("LONG", "ALL", [
      ready,
      {
        kind: "PRICE_MOVE_BPS",
        comparator: "DROP_GTE",
        window_seconds: 60,
        threshold_bps: "5",
      },
    ])).toContain("不确认上涨");
  });

  it("warns when directional movement is no larger than the allowed spread", () => {
    expect(entrySignalQualityWarning("SHORT", "ALL", [
      ready,
      { kind: "SPREAD_BPS", maximum_bps: "10" },
      {
        kind: "PRICE_MOVE_BPS",
        comparator: "DROP_GTE",
        window_seconds: 60,
        threshold_bps: "5",
      },
    ])).toContain("5 bps 不高于允许价差 10 bps");
  });

  it("shows the hidden window-start requirement for a short fade condition", () => {
    expect(entrySignalQualityWarning("SHORT", "ALL", [
      ready,
      { kind: "MARK_PRICE", comparator: "GTE", price: "63560" },
      {
        kind: "PRICE_MOVE_BPS",
        comparator: "DROP_GTE",
        window_seconds: 300,
        threshold_bps: "10",
      },
    ])).toContain("窗口起点必须至少约 63623.62");
  });

  it("stays quiet for a coherent all-of directional confirmation", () => {
    expect(entrySignalQualityWarning("SHORT", "ALL", [
      ready,
      mark,
      { kind: "SPREAD_BPS", maximum_bps: "2" },
      {
        kind: "PRICE_MOVE_BPS",
        comparator: "DROP_GTE",
        window_seconds: 60,
        threshold_bps: "8",
      },
    ])).toBeNull();
  });
});

describe("takeProfitSpreadCoverageWarning", () => {
  it("warns when a market-entry target does not cover the current round-trip spread", () => {
    expect(takeProfitSpreadCoverageWarning({
      initialStopDistanceBps: "10",
      levels: [
        { trigger_r: "0.2" },
        { trigger_r: "0.4" },
      ],
      bidPrice: "63964.5",
      askPrice: "63979.9",
      orderType: "MARKET",
      postOnly: false,
    })).toContain("TP1 2 bps 不高于当前盘口约 2.41 bps 的往返价差成本");
  });

  it("uses only the exit-side spread estimate for a maker-only entry", () => {
    expect(takeProfitSpreadCoverageWarning({
      initialStopDistanceBps: "10",
      levels: [{ trigger_r: "0.2" }],
      bidPrice: "63960",
      askPrice: "63985.6",
      orderType: "LIMIT",
      postOnly: true,
    })).toContain("约 2 bps 的退出价差成本");
  });

  it("includes exact recent fee evidence in the coverage floor when available", () => {
    expect(takeProfitSpreadCoverageWarning({
      initialStopDistanceBps: "10",
      levels: [{ trigger_r: "0.5" }],
      bidPrice: "99.99",
      askPrice: "100.01",
      orderType: "MARKET",
      postOnly: false,
      makerFeeRateBps: "2",
      takerFeeRateBps: "4",
    })).toContain("TP1 5 bps 不高于近期实付手续费与当前盘口合计约 10 bps");
  });

  it("stays quiet when every target clears the observable spread floor", () => {
    expect(takeProfitSpreadCoverageWarning({
      initialStopDistanceBps: "20",
      levels: [
        { trigger_r: "1" },
        { trigger_r: "2" },
      ],
      bidPrice: "63964.5",
      askPrice: "63979.9",
      orderType: "MARKET",
      postOnly: false,
    })).toBeNull();
  });

  it("stays quiet when the live spread is unavailable", () => {
    expect(takeProfitSpreadCoverageWarning({
      initialStopDistanceBps: "10",
      levels: [{ trigger_r: "0.2" }],
      bidPrice: null,
      askPrice: null,
      orderType: "MARKET",
      postOnly: false,
    })).toBeNull();
  });
});

describe("takeProfitAfterCostEstimate", () => {
  it("shows absolute risk, reward, fee, spread and net ratio for a market entry", () => {
    expect(takeProfitAfterCostEstimate({
      initialStopDistanceBps: "10",
      levels: [{ trigger_r: "2", quantity_fraction: "1" }],
      bidPrice: "99.99",
      askPrice: "100.01",
      orderType: "MARKET",
      postOnly: false,
      effectiveNotional: "100",
      makerFeeRateBps: "2",
      takerFeeRateBps: "4",
    })).toEqual({
      effectiveNotional: 100,
      grossRisk: 0.1,
      grossReward: 0.2,
      estimatedFee: 0.08,
      estimatedSpreadCost: expect.closeTo(0.02, 10),
      netReward: expect.closeTo(0.1, 10),
      netRisk: expect.closeTo(0.2, 10),
      netRiskReward: expect.closeTo(0.5, 10),
      breakEvenBps: expect.closeTo(10, 10),
      entryFeeRateBps: 4,
      exitFeeRateBps: 4,
      entryLiquidity: "TAKER",
    });
  });

  it("uses maker entry fee and only exit-side spread for a maker entry", () => {
    expect(takeProfitAfterCostEstimate({
      initialStopDistanceBps: "10",
      levels: [{ trigger_r: "2", quantity_fraction: "1" }],
      bidPrice: "99.99",
      askPrice: "100.01",
      orderType: "LIMIT",
      postOnly: true,
      effectiveNotional: "100",
      makerFeeRateBps: "2",
      takerFeeRateBps: "4",
    })).toMatchObject({
      grossRisk: 0.1,
      grossReward: 0.2,
      estimatedFee: 0.06,
      estimatedSpreadCost: expect.closeTo(0.01, 10),
      netReward: expect.closeTo(0.13, 10),
      netRisk: expect.closeTo(0.17, 10),
      netRiskReward: expect.closeTo(0.76470588235, 10),
      breakEvenBps: expect.closeTo(7, 10),
      entryFeeRateBps: 2,
      exitFeeRateBps: 4,
      entryLiquidity: "MAKER",
    });
  });

  it("weights a multi-level target by its closing fractions", () => {
    expect(takeProfitAfterCostEstimate({
      initialStopDistanceBps: "10",
      levels: [
        { trigger_r: "1", quantity_fraction: "0.5" },
        { trigger_r: "3", quantity_fraction: "0.5" },
      ],
      bidPrice: "99.99",
      askPrice: "100.01",
      orderType: "MARKET",
      postOnly: false,
      effectiveNotional: "100",
      makerFeeRateBps: "2",
      takerFeeRateBps: "4",
    })?.grossReward).toBe(0.2);
  });

  it("does not guess when live prices or the required fee role are unavailable", () => {
    expect(takeProfitAfterCostEstimate({
      initialStopDistanceBps: "10",
      levels: [{ trigger_r: "2", quantity_fraction: "1" }],
      bidPrice: null,
      askPrice: null,
      orderType: "MARKET",
      postOnly: false,
      effectiveNotional: "100",
      makerFeeRateBps: "2",
      takerFeeRateBps: "4",
    })).toBeNull();
    expect(takeProfitAfterCostEstimate({
      initialStopDistanceBps: "10",
      levels: [{ trigger_r: "2", quantity_fraction: "1" }],
      bidPrice: "99.99",
      askPrice: "100.01",
      orderType: "LIMIT",
      postOnly: true,
      effectiveNotional: "100",
      makerFeeRateBps: null,
      takerFeeRateBps: "4",
    })).toBeNull();
  });
});
