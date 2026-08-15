import { describe, expect, it } from "vitest";

import {
  estimateFundingCost,
  formatFundingRatePercent,
  formatFundingSettlementInterval,
} from "./fundingEstimate";

describe("estimateFundingCost", () => {
  const base = {
    averageFundingRate: "0.0001",
    averageIntervalSeconds: 28_800,
    nextFundingAt: "2026-08-13T08:00:00Z",
    sourceCutoff: "2026-08-13T04:00:00Z",
    notional: "1000",
    direction: "LONG" as const,
  };

  it("counts settlement points from the next settlement and average interval", () => {
    expect(estimateFundingCost({ ...base, holdingHours: "20" })).toEqual({
      settlements: 3,
      signedAmount: "-0.30000000",
    });
  });

  it("keeps a short-side receipt positive and excludes a settlement beyond the holding window", () => {
    expect(estimateFundingCost({ ...base, direction: "SHORT", holdingHours: "3.5" })).toEqual({
      settlements: 0,
      signedAmount: "0.00000000",
    });
    expect(estimateFundingCost({ ...base, direction: "SHORT", holdingHours: "4" })).toEqual({
      settlements: 1,
      signedAmount: "0.10000000",
    });
  });

  it("keeps the market funding rate independent from the selected position", () => {
    expect(formatFundingRatePercent("0.0001")).toBe("+0.01%");
    expect(formatFundingRatePercent("-0.0001")).toBe("-0.01%");
    expect(formatFundingSettlementInterval(28_799)).toBe("每 8 小时");
  });

  it("estimates a long receipt and a short payment when the market rate is negative", () => {
    expect(estimateFundingCost({
      ...base,
      averageFundingRate: "-0.0001",
      direction: "LONG",
      holdingHours: "4",
    })?.signedAmount).toBe("0.10000000");
    expect(estimateFundingCost({
      ...base,
      averageFundingRate: "-0.0001",
      direction: "SHORT",
      holdingHours: "4",
    })?.signedAmount).toBe("-0.10000000");
  });
});
