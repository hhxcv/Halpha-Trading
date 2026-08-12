import { describe, expect, it } from "vitest";

import {
  directionalEntryDistance,
  entrySetupAssessment,
  entrySetupAtr,
  entrySetupBps,
  entrySetupPrice,
} from "./ReviewEntrySetupEvidencePanel";


describe("review entry setup evidence", () => {
  it("computes symmetric directional distance from the breakout boundary", () => {
    expect(directionalEntryDistance("LONG", "101", "100", "2")).toEqual({
      price: 1,
      atr: 0.5,
      bps: 100,
    });
    expect(directionalEntryDistance("SHORT", "99", "100", "2")).toEqual({
      price: 1,
      atr: 0.5,
      bps: 100,
    });
  });

  it("distinguishes a near-boundary trigger from a late trigger", () => {
    expect(entrySetupAssessment("DONCHIAN_BREAKOUT", "0.1", "0.5", true).message)
      .toContain("靠近突破边界");
    expect(entrySetupAssessment("DONCHIAN_BREAKOUT", "0.45", "0.5", true).message)
      .toContain("至少 80%");
  });

  it("does not present the Demo order-flow check as breakout evidence", () => {
    expect(entrySetupAssessment("DEMO_ORDER_FLOW_CHECK", "0", "0.5", true).message)
      .toContain("不是 Donchian 突破样本");
  });

  it("keeps invalid values unknown instead of coercing them to zero", () => {
    expect(entrySetupPrice(null)).toBe("未知");
    expect(entrySetupAtr(undefined)).toBe("未知");
    expect(entrySetupBps("not-a-number")).toBe("未知");
  });
});
