import type {
  OrderScheduleCondition,
  OrderScheduleDirection,
} from "../api/client";

export type CurrentEntryBoundaryBreach = {
  kind: "ENTRY_INVALIDATED" | "OPPORTUNITY_MISSED";
  currentPrice: number;
  boundaryPrice: number;
};

function finitePositive(value: unknown): number | null {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}

export function currentEntryBoundaryBreach(input: {
  direction: OrderScheduleDirection;
  referencePrice: unknown;
  invalidationPrice: unknown;
  opportunityMissedPrice: unknown;
}): CurrentEntryBoundaryBreach | null {
  const currentPrice = finitePositive(input.referencePrice);
  if (currentPrice === null) {
    return null;
  }

  const invalidationPrice = finitePositive(input.invalidationPrice);
  if (
    invalidationPrice !== null
    && (
      (input.direction === "LONG" && currentPrice <= invalidationPrice)
      || (input.direction === "SHORT" && currentPrice >= invalidationPrice)
    )
  ) {
    return {
      kind: "ENTRY_INVALIDATED",
      currentPrice,
      boundaryPrice: invalidationPrice,
    };
  }

  const opportunityMissedPrice = finitePositive(input.opportunityMissedPrice);
  if (
    opportunityMissedPrice !== null
    && (
      (input.direction === "LONG" && currentPrice >= opportunityMissedPrice)
      || (input.direction === "SHORT" && currentPrice <= opportunityMissedPrice)
    )
  ) {
    return {
      kind: "OPPORTUNITY_MISSED",
      currentPrice,
      boundaryPrice: opportunityMissedPrice,
    };
  }

  return null;
}

function conditionByKind<TKind extends OrderScheduleCondition["kind"]>(
  items: OrderScheduleCondition[],
  kind: TKind,
): Extract<OrderScheduleCondition, { kind: TKind }> | null {
  return (
    items.find((item): item is Extract<OrderScheduleCondition, { kind: TKind }> => (
      item.kind === kind
    )) ?? null
  );
}

export function entrySignalQualityWarning(
  direction: OrderScheduleDirection,
  operator: "ALL" | "ANY",
  items: OrderScheduleCondition[],
): string | null {
  const marketConditions = items.filter((item) => item.kind !== "DECISION_BASIS_READY");
  if (marketConditions.length === 0) {
    return null;
  }
  if (operator === "ANY" && marketConditions.length > 1) {
    return "当前使用 ANY：价格、K 线收盘、价差或短时变动任一成立即可入场，并不构成组合确认。若交易依据要求组合确认，请改用 ALL。";
  }

  const mark = conditionByKind(items, "MARK_PRICE");
  const closedBar = conditionByKind(items, "CLOSED_BAR_PRICE_15M");
  const spread = conditionByKind(items, "SPREAD_BPS");
  const move = conditionByKind(items, "PRICE_MOVE_BPS");
  if (spread && !mark && !closedBar && !move) {
    return "买卖价差只约束执行成本，不提供交易方向或入场时机；计划名称与图上支撑、阻力或趋势线不会自动成为执行条件。若交易依据是支撑、突破或动量，请启用到价、15m 收盘确认和/或短时价格变动。";
  }
  const directionallyAligned = move
    ? (
      (direction === "LONG" && move.comparator === "GTE")
      || (direction === "SHORT" && move.comparator === "DROP_GTE")
    )
    : false;

    if (move && !directionallyAligned) {
      return direction === "LONG"
        ? "当前短时变动条件不确认上涨；若交易依据是反弹或向上突破，请选择“上涨至少”。"
        : "当前短时变动条件不确认下跌；若交易依据是回落或向下破位，请选择“下跌至少”。";
    }

    if (operator === "ALL" && mark && move && directionallyAligned) {
      const markPrice = finitePositive(mark.price);
      const movementBps = finitePositive(move.threshold_bps);
      if (
        markPrice !== null
        && movementBps !== null
        && movementBps < 10_000
        && direction === "SHORT"
        && mark.comparator === "GTE"
        && move.comparator === "DROP_GTE"
      ) {
        const requiredWindowStart = markPrice / (1 - movementBps / 10_000);
        return `当前组合并不表示从 ${markPrice} 附近开始回落：`
          + `下跌 ${movementBps} bps 后仍保持不低于 ${markPrice}，`
          + `窗口起点必须至少约 ${requiredWindowStart.toFixed(2)}。请确认这是预期的先上破再回落，而不是把阻力位误作当前价下界。`;
      }
      if (
        markPrice !== null
        && movementBps !== null
        && direction === "LONG"
        && mark.comparator === "LTE"
        && move.comparator === "GTE"
      ) {
        const requiredWindowStart = markPrice / (1 + movementBps / 10_000);
        return `当前组合并不表示从 ${markPrice} 附近开始反弹：`
          + `上涨 ${movementBps} bps 后仍保持不高于 ${markPrice}，`
          + `窗口起点必须至多约 ${requiredWindowStart.toFixed(2)}。请确认这是预期的先跌破再反弹，而不是把支撑位误作当前价上界。`;
      }
    }

    if (move && directionallyAligned && spread) {
    const movementBps = finitePositive(move.threshold_bps);
    const maximumSpreadBps = finitePositive(spread.maximum_bps);
    if (
      movementBps !== null
      && maximumSpreadBps !== null
      && movementBps <= maximumSpreadBps
    ) {
      return `短时方向阈值 ${movementBps} bps 不高于允许价差 ${maximumSpreadBps} bps，信号可能只覆盖盘口噪声。若依赖方向确认，请提高变动阈值或降低价差上限。`;
    }
  }

  if (mark && !move && !closedBar) {
    return "标记价格条件只判断当前价格位于阈值哪一侧，不确认触达顺序、反转或突破延续；若交易依据依赖方向变化，请叠加短时价格变动条件。";
  }

  return null;
}

function compactBps(value: number): string {
  return Number(value.toFixed(2)).toString();
}

function finiteNonNegative(value: unknown): number | null {
  if (
    value === null
    || value === undefined
    || (typeof value === "string" && value.trim().length === 0)
  ) {
    return null;
  }
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : null;
}

type TakeProfitSpreadInput = {
  initialStopDistanceBps: unknown;
  levels: Array<{ trigger_r: unknown; quantity_fraction?: unknown }>;
  bidPrice: unknown;
  askPrice: unknown;
  orderType: "MARKET" | "LIMIT";
  postOnly: boolean;
  effectiveNotional?: unknown;
  makerFeeRateBps?: unknown;
  takerFeeRateBps?: unknown;
};

type SpreadExecutionCost = {
  bps: number;
  kind: "退出价差" | "往返价差";
};

function spreadExecutionCost(
  input: TakeProfitSpreadInput,
): SpreadExecutionCost | null {
  const bidPrice = finitePositive(input.bidPrice);
  const askPrice = finitePositive(input.askPrice);
  if (
    bidPrice === null
    || askPrice === null
    || askPrice <= bidPrice
  ) {
    return null;
  }

  const midpoint = (bidPrice + askPrice) / 2;
  const spreadBps = ((askPrice - bidPrice) / midpoint) * 10_000;
  const estimatedExecutionCostBps = (
    input.orderType === "LIMIT" && input.postOnly
      ? spreadBps / 2
      : spreadBps
  );
  if (!Number.isFinite(estimatedExecutionCostBps) || estimatedExecutionCostBps <= 0) {
    return null;
  }
  return {
    bps: estimatedExecutionCostBps,
    kind: input.orderType === "LIMIT" && input.postOnly
      ? "退出价差"
      : "往返价差",
  };
}

type ExecutionFeeCost = {
  bps: number;
  entryBps: number;
  exitBps: number;
  entryLiquidity: "MAKER" | "TAKER";
};

function executionFeeCost(input: TakeProfitSpreadInput): ExecutionFeeCost | null {
  const takerBps = finiteNonNegative(input.takerFeeRateBps);
  const makerBps = finiteNonNegative(input.makerFeeRateBps);
  const makerEntry = input.orderType === "LIMIT" && input.postOnly;
  const entryBps = makerEntry ? makerBps : takerBps;
  if (entryBps === null || takerBps === null) {
    return null;
  }
  return {
    bps: entryBps + takerBps,
    entryBps,
    exitBps: takerBps,
    entryLiquidity: makerEntry ? "MAKER" : "TAKER",
  };
}

export type TakeProfitAfterCostEstimate = {
  effectiveNotional: number;
  grossRisk: number;
  grossReward: number;
  estimatedFee: number;
  estimatedSpreadCost: number;
  netReward: number;
  netRisk: number;
  netRiskReward: number;
  breakEvenBps: number;
  entryFeeRateBps: number;
  exitFeeRateBps: number;
  entryLiquidity: "MAKER" | "TAKER";
};

export function takeProfitAfterCostEstimate(
  input: TakeProfitSpreadInput,
): TakeProfitAfterCostEstimate | null {
  const stopDistanceBps = finitePositive(input.initialStopDistanceBps);
  const effectiveNotional = finitePositive(input.effectiveNotional);
  const spreadCost = spreadExecutionCost(input);
  const feeCost = executionFeeCost(input);
  if (
    stopDistanceBps === null
    || effectiveNotional === null
    || spreadCost === null
    || feeCost === null
  ) {
    return null;
  }
  let coveredFraction = 0;
  let weightedTargetR = 0;
  for (const level of input.levels) {
    const triggerR = finitePositive(level.trigger_r);
    const quantityFraction = finitePositive(level.quantity_fraction);
    if (triggerR === null || quantityFraction === null) {
      return null;
    }
    coveredFraction += quantityFraction;
    weightedTargetR += triggerR * quantityFraction;
  }
  if (
    input.levels.length === 0
    || Math.abs(coveredFraction - 1) > 0.000001
  ) {
    return null;
  }
  const grossRisk = effectiveNotional * stopDistanceBps / 10_000;
  const grossReward = grossRisk * weightedTargetR;
  const estimatedFee = effectiveNotional * feeCost.bps / 10_000;
  const estimatedSpreadCost = effectiveNotional * spreadCost.bps / 10_000;
  const totalEstimatedCost = estimatedFee + estimatedSpreadCost;
  const netReward = grossReward - totalEstimatedCost;
  const netRisk = grossRisk + totalEstimatedCost;
  return {
    effectiveNotional,
    grossRisk,
    grossReward,
    estimatedFee,
    estimatedSpreadCost,
    netReward,
    netRisk,
    netRiskReward: netRisk > 0 ? Math.max(netReward, 0) / netRisk : 0,
    breakEvenBps: feeCost.bps + spreadCost.bps,
    entryFeeRateBps: feeCost.entryBps,
    exitFeeRateBps: feeCost.exitBps,
    entryLiquidity: feeCost.entryLiquidity,
  };
}

export function takeProfitSpreadCoverageWarning(
  input: TakeProfitSpreadInput,
): string | null {
  const stopDistanceBps = finitePositive(input.initialStopDistanceBps);
  const spreadCost = spreadExecutionCost(input);
  if (stopDistanceBps === null || spreadCost === null) {
    return null;
  }
  const feeCost = executionFeeCost(input);
  const coveredCostBps = spreadCost.bps + (feeCost?.bps ?? 0);

  const uncovered = input.levels.flatMap((level, index) => {
    const triggerR = finitePositive(level.trigger_r);
    if (triggerR === null) {
      return [];
    }
    const targetDistanceBps = stopDistanceBps * triggerR;
    return targetDistanceBps <= coveredCostBps
      ? [`TP${index + 1} ${compactBps(targetDistanceBps)} bps`]
      : [];
  });
  if (uncovered.length === 0) {
    return null;
  }

  return feeCost
    ? `${uncovered.join("、")} 不高于近期实付手续费与当前盘口合计约 `
      + `${compactBps(coveredCostBps)} bps；未计资金费和触发后滑点。`
      + "该档即使方向正确也可能费用后亏损。"
    : `${uncovered.join("、")} 不高于当前盘口约 `
      + `${compactBps(spreadCost.bps)} bps 的${spreadCost.kind}成本；`
      + "手续费仍未知，且未计资金费和触发后滑点。该档即使方向正确也可能费用后亏损。";
}
