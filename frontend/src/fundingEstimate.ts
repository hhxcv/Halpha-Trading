export type FundingEstimateInput = Readonly<{
  averageFundingRate: string | null;
  averageIntervalSeconds: number | null;
  nextFundingAt: string | null;
  sourceCutoff: string | null;
  holdingHours: string;
  notional: string;
  direction: "LONG" | "SHORT";
}>;

export type FundingEstimate = Readonly<{
  settlements: number;
  signedAmount: string;
}>;

export function formatFundingRatePercent(value: string | null): string {
  const rate = value === null ? Number.NaN : Number(value);
  if (!Number.isFinite(rate)) return "未知";
  const magnitude = Math.abs(rate * 100)
    .toFixed(4)
    .replace(/\.?0+$/, "");
  return `${rate > 0 ? "+" : rate < 0 ? "-" : ""}${magnitude || "0"}%`;
}

export function formatFundingSettlementInterval(
  averageIntervalSeconds: number | null,
): string | null {
  if (
    averageIntervalSeconds === null
    || !Number.isFinite(averageIntervalSeconds)
    || averageIntervalSeconds <= 0
  ) return null;
  const roundedHours = Math.round(averageIntervalSeconds / 3_600);
  if (roundedHours >= 24 && roundedHours % 24 === 0) {
    return `每 ${roundedHours / 24} 天`;
  }
  return `每 ${roundedHours} 小时`;
}

export function estimateFundingCost(input: FundingEstimateInput): FundingEstimate | null {
  const averageRate = input.averageFundingRate === null ? Number.NaN : Number(input.averageFundingRate);
  const intervalSeconds = input.averageIntervalSeconds ?? Number.NaN;
  const holdingHours = Number(input.holdingHours);
  const notional = Number(input.notional);
  const nextFundingAt = input.nextFundingAt === null ? Number.NaN : Date.parse(input.nextFundingAt);
  const sourceCutoff = input.sourceCutoff === null ? Number.NaN : Date.parse(input.sourceCutoff);
  if (
    !Number.isFinite(averageRate)
    || !Number.isFinite(intervalSeconds)
    || intervalSeconds <= 0
    || !Number.isFinite(holdingHours)
    || holdingHours < 0
    || !Number.isFinite(notional)
    || notional < 0
    || !Number.isFinite(nextFundingAt)
    || !Number.isFinite(sourceCutoff)
  ) {
    return null;
  }
  const hoursUntilNext = (nextFundingAt - sourceCutoff) / 3_600_000;
  if (!Number.isFinite(hoursUntilNext) || hoursUntilNext < -1 / 60) return null;
  const normalizedHoursUntilNext = Math.max(0, hoursUntilNext);
  const intervalHours = intervalSeconds / 3_600;
  const settlements = holdingHours < normalizedHoursUntilNext
    ? 0
    : 1 + Math.floor((holdingHours - normalizedHoursUntilNext) / intervalHours);
  const sideMultiplier = input.direction === "LONG" ? -1 : 1;
  const signedAmount = notional * averageRate * settlements * sideMultiplier;
  if (!Number.isFinite(signedAmount)) return null;
  return { settlements, signedAmount: signedAmount.toFixed(8) };
}
