import { describe, expect, it } from "vitest";

import {
  directFundingCap,
  fundingAmountAtPercentage,
  fundingPercentageForAmount,
} from "./directFundingCap";

const allowed = {
  new_risk_allowed: true,
  available_notional_capacity: "900",
  max_plan_loss: "10",
  open_risk_limit: "24",
  open_risk_committed: "4",
} as const;

describe("directFundingCap", () => {
  it("uses only the server-owned notional capacity as the planning reference", () => {
    expect(directFundingCap(allowed as never)).toEqual({ maximum: "900.00" });
  });

  it("withholds the ceiling while new risk is blocked", () => {
    expect(directFundingCap({ ...allowed, new_risk_allowed: false } as never))
      .toEqual({ maximum: null });
  });

  it("maps the slider to an exact proportion of the authoritative ceiling", () => {
    expect(fundingAmountAtPercentage("500", 25)).toBe("125.00");
    expect(fundingAmountAtPercentage("500", 100)).toBe("500.00");
  });

  it("derives the slider position from a persisted funding amount", () => {
    expect(fundingPercentageForAmount("500.00", "125.00")).toBe(25);
    expect(fundingPercentageForAmount("500.00", "365.00")).toBe(73);
  });

  it("keeps an over-limit draft amount intact while pinning its slider view", () => {
    expect(fundingPercentageForAmount("500.00", "600.00")).toBe(100);
  });
});
