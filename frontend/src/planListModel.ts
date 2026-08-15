import type {
  ActivationSummary,
  OrderSchedulePreview,
  PlanSummary,
} from "./api/client";
import { marketPrice, tradingPrice } from "./format";

export type PlanWorkbenchSections = {
  currentActivations: ActivationSummary[];
  currentPlans: PlanSummary[];
  historicalPlans: PlanSummary[];
};

const priceMatchLabels: Record<string, string> = {
  OPPONENT: "对手价",
  OPPONENT_5: "对手价 5 档",
  OPPONENT_10: "对手价 10 档",
  OPPONENT_20: "对手价 20 档",
  QUEUE: "同向队列价",
  QUEUE_5: "同向队列 5 档",
  QUEUE_10: "同向队列 10 档",
  QUEUE_20: "同向队列 20 档",
};

export function orderScheduleIntent(
  spec: PlanSummary["order_schedule_spec"],
  frozenSnapshot?: OrderSchedulePreview | null,
): string | null {
  const effectiveSpec = frozenSnapshot?.schedule_spec ?? spec;
  if (!effectiveSpec) return null;
  const entryProgram = effectiveSpec.entry_program;
  const entryPrefix = entryProgram?.kind === "TIME_SLICED"
    ? `时间分批 · ${entryProgram.slice_count} 笔`
    : entryProgram?.kind === "EVENT_TRIGGERED"
      ? "事件触发入场"
      : entryProgram?.kind === "PRICE_LADDER"
        ? "价格区间分批"
        : "";
  if (effectiveSpec.venue_policy.order_type === "MARKET") {
    return [entryPrefix, "市价"].filter(Boolean).join(" · ");
  }

  const makerOnly = effectiveSpec.venue_policy.post_only;
  const priceMatch = effectiveSpec.venue_policy.price_match;
  if (priceMatch) {
    return [
      entryPrefix,
      "限价",
      priceMatchLabels[priceMatch] ?? "场所匹配价",
    ].filter(Boolean).join(" · ");
  }

  const tickSize = frozenSnapshot?.instrument_rules.price_tick_size;
  const normalizedPrices = (frozenSnapshot?.normalized_legs ?? [])
    .flatMap((leg) => leg.price === null ? [] : [leg.price])
    .filter((price) => Number.isFinite(Number(price)));
  const displayPrice = (price: string) => (
    tickSize ? tradingPrice(price, tickSize) : marketPrice(price)
  );

  if (effectiveSpec.price_distribution.kind === "SINGLE") {
    const price = normalizedPrices[0]
      ?? effectiveSpec.price_distribution.limit_price;
    const instruction = price
      ? `${makerOnly ? "Maker only · " : ""}限价 ${displayPrice(price)} USDT`
      : `${makerOnly ? "Maker only · " : ""}限价待确认`;
    return [entryPrefix, instruction].filter(Boolean).join(" · ");
  }

  const normalizedBounds = normalizedPrices.length > 0
    ? [
        normalizedPrices.reduce((lowest, price) => (
          Number(price) < Number(lowest) ? price : lowest
        )),
        normalizedPrices.reduce((highest, price) => (
          Number(price) > Number(highest) ? price : highest
        )),
      ] as const
    : null;
  const lowerPrice = normalizedBounds?.[0]
    ?? effectiveSpec.price_distribution.lower_price;
  const upperPrice = normalizedBounds?.[1]
    ?? effectiveSpec.price_distribution.upper_price;
  return [
    entryPrefix,
    makerOnly ? "Maker only" : "",
    `区间限价 ${displayPrice(lowerPrice)}–${displayPrice(upperPrice)} USDT`,
    `${effectiveSpec.price_distribution.level_count} 档`,
  ].filter(Boolean).join(" · ");
}

export function orderScheduleConditionIntent(
  spec: PlanSummary["order_schedule_spec"],
): string | null {
  if (!spec) return null;
  const marketConditions = spec.entry_conditions.items.filter(
    (item) => item.kind !== "DECISION_BASIS_READY",
  );
  if (marketConditions.length === 0) return "无附加入场条件";
  const operator = spec.entry_conditions.operator === "ANY"
    ? "任一成立"
    : "全部成立";
  return `${operator} · ${marketConditions.length} 个条件`;
}

export function latestActivationsByPlanVersion(
  activations: ActivationSummary[],
): Map<string, ActivationSummary> {
  const result = new Map<string, ActivationSummary>();
  for (const activation of activations) {
    const current = result.get(activation.plan_version_ref);
    const activationTime = Date.parse(activation.created_at || activation.updated_at);
    const currentTime = current
      ? Date.parse(current.created_at || current.updated_at)
      : Number.NEGATIVE_INFINITY;
    if (!current || activationTime > currentTime) {
      result.set(activation.plan_version_ref, activation);
    }
  }
  return result;
}

export function planWorkbenchSections(
  plans: PlanSummary[],
  activations: ActivationSummary[],
  _nowMs: number,
): PlanWorkbenchSections {
  const currentActivations = activations.filter(
    (activation) => activation.lifecycle !== "COMPLETED",
  );
  return {
    currentActivations,
    // A plan is either an editable draft, represented by its current
    // activation, or an ended plan.  Snapshot metadata never forms a fourth
    // list state.
    currentPlans: plans.filter((plan) => plan.status === "DRAFT"),
    historicalPlans: plans.filter((plan) => plan.status === "ENDED"),
  };
}
