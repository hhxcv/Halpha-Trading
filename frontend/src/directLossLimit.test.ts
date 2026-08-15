import { describe, expect, it } from "vitest";

import { lossLimitedDirectEntry, scaleScheduleToEntryNotional } from "./directLossLimit";
import { createDefaultOrderScheduleSpec } from "./components/orderScheduleEditorModel";

describe("directLossLimit", () => {
  it("keeps the funding input separate while deriving an executable cap", () => {
    expect(lossLimitedDirectEntry({
      effectiveNotional: "1000",
      maximumProjectedLoss: "25",
      maxPlanLoss: "10",
    })).toEqual({ maximumEntryNotional: "400", constrained: true });
  });

  it("scales final schedule notionals without mutating the editing schedule", () => {
    const spec = createDefaultOrderScheduleSpec();
    const scaled = scaleScheduleToEntryNotional(spec, "1000", "400");
    expect(scaled.amount_distribution.base_notional).toBe("200");
    expect(spec.amount_distribution.base_notional).toBe("500");
  });

  it("scales from requested total rather than rounded executable notional", () => {
    const initial = createDefaultOrderScheduleSpec();
    const spec = {
      ...initial,
      amount_distribution: {
        ...initial.amount_distribution,
        base_notional: "4812.01",
      },
    };
    const scaled = scaleScheduleToEntryNotional(spec, "4812.01", "4809.1689");

    expect(Number(scaled.amount_distribution.base_notional)).toBeLessThanOrEqual(4809.1689);
    expect(4809.1689 - Number(scaled.amount_distribution.base_notional)).toBeLessThan(0.000001);
    expect(spec.amount_distribution.base_notional).toBe("4812.01");
  });
});
