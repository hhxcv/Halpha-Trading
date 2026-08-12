import { describe, expect, it } from "vitest";

import {
  initialStrategyPlanIntent,
  strategyAllowedPlanIntents,
  strategyAllowsPlanIntent,
} from "./strategyIntentQualification";


describe("strategy plan-intent qualification", () => {
  it("defaults a validation-only strategy to validation", () => {
    const scope = { allowed_plan_intents: ["VALIDATION"] };

    expect(initialStrategyPlanIntent(scope)).toBe("VALIDATION");
    expect(strategyAllowsPlanIntent(scope, "PROFIT_SEEKING")).toBe(false);
  });

  it("allows profit-seeking only when it is explicit", () => {
    const scope = { allowed_plan_intents: ["PROFIT_SEEKING", "VALIDATION"] };

    expect(initialStrategyPlanIntent(scope)).toBe("PROFIT_SEEKING");
    expect(strategyAllowsPlanIntent(scope, "PROFIT_SEEKING")).toBe(true);
  });

  it("fails closed for missing, duplicate, or unknown intent declarations", () => {
    expect(strategyAllowedPlanIntents({}).size).toBe(0);
    expect(strategyAllowedPlanIntents({ allowed_plan_intents: ["VALIDATION", "VALIDATION"] }).size).toBe(0);
    expect(strategyAllowedPlanIntents({ allowed_plan_intents: ["UNBOUNDED"] }).size).toBe(0);
  });
});
