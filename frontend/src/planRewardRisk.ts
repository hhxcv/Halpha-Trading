export const DEFAULT_MINIMUM_REWARD_RISK_RATIO = 1;

function finitePositive(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

export function weightedTakeProfitRewardRisk(
  levels: readonly { trigger_r: unknown; quantity_fraction: unknown }[],
): number | null {
  if (levels.length === 0) return null;
  let weighted = 0;
  for (const level of levels) {
    const trigger = finitePositive(level.trigger_r);
    const fraction = finitePositive(level.quantity_fraction);
    if (trigger === null || fraction === null || fraction > 1) return null;
    weighted += trigger * fraction;
  }
  return Number.isFinite(weighted) && weighted > 0 ? weighted : null;
}

export function strategyRewardRisk(parameters: {
  take_profit_1_r: unknown;
  take_profit_1_fraction: unknown;
  take_profit_2_r: unknown;
}): number | null {
  const first = finitePositive(parameters.take_profit_1_r);
  const fraction = finitePositive(parameters.take_profit_1_fraction);
  const second = finitePositive(parameters.take_profit_2_r);
  if (first === null || fraction === null || fraction > 1 || second === null) {
    return null;
  }
  const weighted = first * fraction + second * (1 - fraction);
  return Number.isFinite(weighted) && weighted > 0 ? weighted : null;
}

export function minimumRewardRisk(
  configured: string | null | undefined,
): number {
  return finitePositive(configured) ?? DEFAULT_MINIMUM_REWARD_RISK_RATIO;
}

export function rewardRiskDisciplineBlocked(
  ratio: number | null,
  minimum: number,
): boolean {
  return ratio === null || ratio + Number.EPSILON < minimum;
}
