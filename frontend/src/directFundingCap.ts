import type { Overview } from "./api/client";

type NewRiskDiscipline = Overview["new_risk_discipline"];

export type DirectFundingCap = {
  maximum: string | null;
};

function finiteNonNegative(value: string | null | undefined): number | null {
  if (value === null || value === undefined || value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

function decimalText(value: number): string {
  return value.toFixed(2);
}

/**
 * Exposes the server-owned notional capacity as a planning reference. The
 * user's plan funding limit is never derived from an order preview or loss
 * estimate, so editing protection cannot rewrite the funding field or slider.
 * CAP still rechecks all current discipline facts at activation and action
 * submission.
 */
export function directFundingCap(
  discipline: NewRiskDiscipline | null | undefined,
): DirectFundingCap {
  if (!discipline || !discipline.new_risk_allowed) {
    return { maximum: null };
  }
  const exposureCapacity = finiteNonNegative(
    discipline.available_notional_capacity,
  );
  if (exposureCapacity === null) {
    return { maximum: null };
  }
  return { maximum: decimalText(exposureCapacity) };
}

export function fundingAmountAtPercentage(
  maximum: string,
  percentage: number,
): string {
  const cap = finiteNonNegative(maximum);
  if (cap === null || !Number.isFinite(percentage)) return "";
  return decimalText(Math.max(0, cap * Math.min(100, Math.max(0, percentage)) / 100));
}

/**
 * The slider is a view of the plan's persisted funding ceiling, not a second
 * source of truth.  Derive its position from the amount on every render so a
 * reopened draft cannot retain the new-plan default of 100%.
 */
export function fundingPercentageForAmount(
  maximum: string | null | undefined,
  amount: string | null | undefined,
): number {
  const cap = finiteNonNegative(maximum);
  const selected = finiteNonNegative(amount);
  if (cap === null || cap <= 0 || selected === null) return 0;
  return Math.round(Math.max(0, Math.min(100, selected / cap * 100)));
}
