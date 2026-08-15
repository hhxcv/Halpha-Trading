import { describe, expect, it } from "vitest";

import {
  minimumRewardRisk,
  rewardRiskDisciplineBlocked,
  strategyRewardRisk,
  weightedTakeProfitRewardRisk,
} from "./planRewardRisk";

describe("plan reward/risk discipline", () => {
  it("uses the full position-weighted price targets", () => {
    expect(weightedTakeProfitRewardRisk([
      { trigger_r: "1", quantity_fraction: "0.5" },
      { trigger_r: "3", quantity_fraction: "0.5" },
    ])).toBe(2);
    expect(weightedTakeProfitRewardRisk([])).toBeNull();
  });

  it("uses the same weighting for strategy parameters", () => {
    expect(strategyRewardRisk({
      take_profit_1_r: "1.5",
      take_profit_1_fraction: "0.5",
      take_profit_2_r: "3",
    })).toBe(2.25);
  });

  it("fails closed below or without the code-owned minimum", () => {
    expect(minimumRewardRisk(undefined)).toBe(1);
    expect(rewardRiskDisciplineBlocked(null, 1)).toBe(true);
    expect(rewardRiskDisciplineBlocked(0.99, 1)).toBe(true);
    expect(rewardRiskDisciplineBlocked(1, 1)).toBe(false);
  });
});
