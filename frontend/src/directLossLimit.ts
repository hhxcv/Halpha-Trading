import type { OrderScheduleSpec } from "./api/client";

export type DirectLossLimitedEntry = {
  maximumEntryNotional: string | null;
  constrained: boolean;
};

function finitePositive(value: string | null | undefined): number | null {
  if (value === null || value === undefined || value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function decimalText(value: number): string {
  return (Math.floor(value * 1e8) / 1e8).toFixed(8).replace(/\.?0+$/, "");
}

/**
 * Converts the CAP-owned single-plan loss boundary into a maximum executable
 * notional for the currently previewed stop. The funding input stays intact;
 * only the schedule included in the final plan is reduced when necessary.
 */
export function lossLimitedDirectEntry(input: {
  effectiveNotional: string | null | undefined;
  maximumProjectedLoss: string | null | undefined;
  maxPlanLoss: string | null | undefined;
}): DirectLossLimitedEntry {
  const effectiveNotional = finitePositive(input.effectiveNotional);
  const maximumProjectedLoss = finitePositive(input.maximumProjectedLoss);
  const maxPlanLoss = finitePositive(input.maxPlanLoss);
  if (effectiveNotional === null || maximumProjectedLoss === null || maxPlanLoss === null) {
    return { maximumEntryNotional: null, constrained: false };
  }
  if (maximumProjectedLoss <= maxPlanLoss) {
    return { maximumEntryNotional: decimalText(effectiveNotional), constrained: false };
  }
  return {
    maximumEntryNotional: decimalText(effectiveNotional * maxPlanLoss / maximumProjectedLoss),
    constrained: true,
  };
}

export function scaleScheduleToEntryNotional(
  spec: OrderScheduleSpec,
  currentEffectiveNotional: string | null | undefined,
  targetEffectiveNotional: string | null | undefined,
): OrderScheduleSpec {
  const current = finitePositive(currentEffectiveNotional);
  const target = finitePositive(targetEffectiveNotional);
  if (current === null || target === null || target >= current) return spec;
  const scale = target / current;
  const distribution = spec.amount_distribution;
  return {
    ...spec,
    amount_distribution: {
      ...distribution,
      base_notional: decimalText(Number(distribution.base_notional) * scale),
      linear_step: decimalText(Number(distribution.linear_step) * scale),
      custom_notionals: distribution.custom_notionals.map((value) => (
        decimalText(Number(value) * scale)
      )),
    },
  };
}
