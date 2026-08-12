import { describe, expect, it } from "vitest";

import {
  isStrategyPerformanceClassification,
  summarizeAccountAndProfitSeekingPerformance,
  summarizeReviewPerformance,
} from "./reviewPerformanceSummary";

describe("summarizeReviewPerformance", () => {
  it("computes expectancy, path drawdown and the latest directional streak", () => {
    expect(summarizeReviewPerformance([
      { netPnl: 2, commission: .2 },
      { netPnl: -1, commission: .1 },
      { netPnl: -3, commission: .3 },
      { netPnl: 4, commission: .4 },
      { netPnl: -5, commission: .5 },
      { netPnl: -2, commission: .2 },
    ])).toEqual({
      tradeCount: 6,
      netPnl: -5,
      commissions: 1.7,
      wins: 2,
      grossProfit: 6,
      grossLoss: 11,
      averageNetPnl: -5 / 6,
      totalEntryNotional: null,
      notionalReturnPercent: null,
      entryNotionalTradeCount: 0,
      maximumDrawdown: 7,
      currentStreakKind: "LOSS",
      currentStreakCount: 2,
    });
  });

  it("uses zero as the starting equity peak", () => {
    expect(summarizeReviewPerformance([
      { netPnl: -2, commission: .1 },
      { netPnl: 1, commission: .1 },
    ])).toMatchObject({
      netPnl: -1,
      maximumDrawdown: 2,
      currentStreakKind: "WIN",
      currentStreakCount: 1,
    });
  });

  it("shows a winning streak and lets a flat result clear either direction", () => {
    expect(summarizeReviewPerformance([
      { netPnl: -2, commission: .1 },
      { netPnl: 1, commission: .1 },
      { netPnl: 3, commission: .1 },
    ])).toMatchObject({
      currentStreakKind: "WIN",
      currentStreakCount: 2,
    });
    expect(summarizeReviewPerformance([
      { netPnl: -2, commission: .1 },
      { netPnl: 0, commission: .1 },
    ])).toMatchObject({
      currentStreakKind: "NONE",
      currentStreakCount: 0,
    });
  });

  it("normalizes net results only when every trade has reliable entry notional", () => {
    expect(summarizeReviewPerformance([
      { netPnl: 1, commission: .2, entryNotional: 100 },
      { netPnl: -2, commission: .4, entryNotional: 300 },
    ])).toMatchObject({
      netPnl: -1,
      totalEntryNotional: 400,
      notionalReturnPercent: -.25,
      entryNotionalTradeCount: 2,
    });

    expect(summarizeReviewPerformance([
      { netPnl: 1, commission: .2, entryNotional: 100 },
      { netPnl: -2, commission: .4, entryNotional: null },
    ])).toMatchObject({
      totalEntryNotional: null,
      notionalReturnPercent: null,
      entryNotionalTradeCount: 1,
    });
  });

  it("ignores records whose required accounting values are unreliable", () => {
    expect(summarizeReviewPerformance([
      { netPnl: Number.NaN, commission: 1 },
      { netPnl: 5, commission: -1 },
    ])).toEqual({
      tradeCount: 0,
      netPnl: 0,
      commissions: 0,
      wins: 0,
      grossProfit: 0,
      grossLoss: 0,
      averageNetPnl: null,
      totalEntryNotional: null,
      notionalReturnPercent: null,
      entryNotionalTradeCount: 0,
      maximumDrawdown: 0,
      currentStreakKind: "NONE",
      currentStreakCount: 0,
    });
  });

  it("keeps validation and tooling results out of strategy performance samples", () => {
    expect(isStrategyPerformanceClassification("USABLE_SAMPLE")).toBe(true);
    expect(isStrategyPerformanceClassification("TRADE_DECISION_ISSUE")).toBe(true);
    expect(isStrategyPerformanceClassification("AS_EXPECTED")).toBe(true);
    expect(isStrategyPerformanceClassification("VALIDATION_TRADE")).toBe(false);
    expect(isStrategyPerformanceClassification("TOOLING_ISSUE")).toBe(false);
    expect(isStrategyPerformanceClassification("ISSUE_FOUND")).toBe(false);
    expect(isStrategyPerformanceClassification("PENDING")).toBe(false);
  });

  it("retains every reliable result in account totals while isolating strategy metrics", () => {
    const summary = summarizeAccountAndProfitSeekingPerformance([
      {
        netPnl: 3,
        commission: .1,
        entryNotional: 100,
        classification: "USABLE_SAMPLE",
        profitSeekingEligible: true,
      },
      {
        netPnl: -4,
        commission: .2,
        entryNotional: 200,
        classification: "VALIDATION_TRADE",
      },
      {
        netPnl: -5,
        commission: .3,
        entryNotional: 300,
        classification: "TOOLING_ISSUE",
      },
      {
        netPnl: -1,
        commission: .4,
        entryNotional: 100,
        classification: "TRADE_DECISION_ISSUE",
        profitSeekingEligible: true,
      },
    ]);

    expect(summary.account).toMatchObject({
      tradeCount: 4,
      netPnl: -7,
      commissions: 1,
      totalEntryNotional: 700,
      notionalReturnPercent: -1,
    });
    expect(summary.profitSeeking).toMatchObject({
      tradeCount: 2,
      netPnl: 2,
      commissions: .5,
      totalEntryNotional: 200,
      notionalReturnPercent: 1,
      currentStreakKind: "LOSS",
      currentStreakCount: 1,
    });
  });

  it("does not infer profit-seeking eligibility from a favorable classification", () => {
    const summary = summarizeAccountAndProfitSeekingPerformance([
      {
        netPnl: 5,
        commission: .1,
        entryNotional: 100,
        classification: "USABLE_SAMPLE",
      },
    ]);

    expect(summary.account.tradeCount).toBe(1);
    expect(summary.profitSeeking.tradeCount).toBe(0);
  });
});
