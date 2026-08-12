export type StrategyPlanIntent = "PROFIT_SEEKING" | "VALIDATION";

const SUPPORTED_PLAN_INTENTS = new Set<StrategyPlanIntent>([
  "PROFIT_SEEKING",
  "VALIDATION",
]);

export function strategyAllowedPlanIntents(
  economicScope: Record<string, unknown>,
): ReadonlySet<StrategyPlanIntent> {
  const raw = economicScope.allowed_plan_intents;
  if (!Array.isArray(raw) || raw.length === 0) return new Set();
  if (raw.some((item) => typeof item !== "string" || !SUPPORTED_PLAN_INTENTS.has(item as StrategyPlanIntent))) {
    return new Set();
  }
  const values = new Set(raw as StrategyPlanIntent[]);
  return values.size === raw.length ? values : new Set();
}

export function strategyAllowsPlanIntent(
  economicScope: Record<string, unknown>,
  intent: StrategyPlanIntent,
): boolean {
  return strategyAllowedPlanIntents(economicScope).has(intent);
}

export function initialStrategyPlanIntent(
  economicScope: Record<string, unknown>,
): StrategyPlanIntent {
  return strategyAllowsPlanIntent(economicScope, "PROFIT_SEEKING")
    ? "PROFIT_SEEKING"
    : "VALIDATION";
}
