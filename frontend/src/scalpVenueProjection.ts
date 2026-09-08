import type { ActivationDetail } from "./api/client";
import type { OrderChartPriceAnnotation } from "./components/orderScheduleChartModel";

type JsonRecord = Record<string, unknown>;

type ExecutionAction = Readonly<{
  executionActionId: string;
  kind: string;
  state: string;
  terms: JsonRecord;
}>;

type VenueFact = Readonly<{
  kind: string;
  actionRef: string | null;
  payload: JsonRecord;
  cutoff: string | null;
  receivedAt: string | null;
  sourceTime: string | null;
  id: string;
}>;

export type ScalpVenueProjection = Readonly<{
  status: "WAITING" | "PENDING_VENUE" | "WORKING" | "PARTIALLY_FILLED" | "FILLED" | "UNKNOWN" | "CLOSED";
  label: string;
  detail: string;
  factCutoff: string | null;
  priceAnnotations: OrderChartPriceAnnotation[];
}>;

function record(value: unknown): JsonRecord | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as JsonRecord
    : null;
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function price(value: unknown): number | null {
  const candidate = typeof value === "number" ? value : Number(value);
  return Number.isFinite(candidate) && candidate > 0 ? candidate : null;
}

function toActions(detail: ActivationDetail | null | undefined): ExecutionAction[] {
  return (detail?.execution_actions ?? []).flatMap((value) => {
    const item = record(value);
    const actionTerms = record(item?.action_terms);
    const executionActionId = text(item?.execution_action_id);
    const kind = text(item?.action_kind);
    const state = text(item?.state);
    if (!item || !actionTerms || !executionActionId || !kind || !state) return [];
    return [{
      executionActionId,
      kind: kind.toUpperCase(),
      state: state.toUpperCase(),
      terms: actionTerms,
    }];
  });
}

function toFacts(detail: ActivationDetail | null | undefined): VenueFact[] {
  return (detail?.venue_facts ?? []).flatMap((value) => {
    const item = record(value);
    const payload = record(item?.payload);
    const id = text(item?.venue_fact_id);
    const kind = text(item?.kind);
    if (!item || !payload || !id || !kind) return [];
    return [{
      id,
      kind: kind.toUpperCase(),
      actionRef: text(item.action_ref),
      payload,
      cutoff: text(item.cutoff),
      receivedAt: text(item.received_at),
      sourceTime: text(item.source_time),
    }];
  });
}

function observationTime(fact: VenueFact): number {
  const value = fact.sourceTime ?? fact.cutoff ?? fact.receivedAt;
  const parsed = value ? Date.parse(value) : Number.NaN;
  return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY;
}

function factsForAction(facts: VenueFact[], action: ExecutionAction): VenueFact[] {
  return facts.filter((fact) => fact.actionRef === action.executionActionId);
}

function orderStatus(facts: VenueFact[]): string | null {
  const states = facts
    .filter((fact) => fact.kind === "ORDER_STATE")
    .sort((left, right) => observationTime(left) - observationTime(right));
  const latest = states.at(-1);
  const value = text(latest?.payload.status)?.toUpperCase();
  if (!value) return null;
  if (["ACCEPTED", "ACKNOWLEDGED", "NEW"].includes(value)) return "WORKING";
  if (value === "CANCELED") return "CANCELLED";
  return value;
}

function venueWorking(facts: VenueFact[], action: ExecutionAction): boolean {
  return orderStatus(factsForAction(facts, action)) === "WORKING";
}

function factCutoff(facts: VenueFact[]): string | null {
  return facts
    .filter((fact) => fact.cutoff !== null)
    .sort((left, right) => observationTime(left) - observationTime(right))
    .at(-1)?.cutoff ?? null;
}

function weightedEntryPrice(facts: VenueFact[], entryIds: Set<string>): number | null {
  const seen = new Set<string>();
  const fills = facts.flatMap((fact) => {
    if (fact.kind !== "FILL" || fact.actionRef === null || !entryIds.has(fact.actionRef)) {
      return [];
    }
    const fillPrice = price(fact.payload.last_price);
    const quantity = price(fact.payload.last_quantity);
    const stableFillId = text(fact.payload.trade_id) ?? fact.id;
    if (fillPrice === null || quantity === null || seen.has(stableFillId)) return [];
    seen.add(stableFillId);
    return [{ fillPrice, quantity }];
  });
  const totalQuantity = fills.reduce((sum, fill) => sum + fill.quantity, 0);
  return totalQuantity > 0
    ? fills.reduce((sum, fill) => sum + fill.fillPrice * fill.quantity, 0) / totalQuantity
    : null;
}

function hasPartialEntryFill(facts: VenueFact[], entryIds: Set<string>): boolean {
  return [...entryIds].some((actionId) => {
    const actionFacts = facts.filter((fact) => fact.actionRef === actionId);
    if (orderStatus(actionFacts) === "FILLED") return false;
    const latestFill = actionFacts.filter((fact) => fact.kind === "FILL")
      .sort((left, right) => observationTime(left) - observationTime(right)).at(-1);
    const leaves = latestFill ? Number(latestFill.payload.leaves_quantity) : Number.NaN;
    return Number.isFinite(leaves) && leaves > 0;
  });
}

function orderPrice(action: ExecutionAction): number | null {
  return price(action.terms.trigger_price) ?? price(action.terms.price);
}

function activationLifecycle(detail: ActivationDetail | null | undefined): string | null {
  const activation = record(detail?.activation);
  return text(activation?.lifecycle)?.toUpperCase() ?? null;
}

function factualLine(
  action: ExecutionAction,
  role: "SINGLE_LIMIT" | "PROTECTION" | "TAKE_PROFIT",
  label: string,
): OrderChartPriceAnnotation | null {
  const value = orderPrice(action);
  if (value === null) return null;
  return {
    id: `scalp-venue-${action.executionActionId}`,
    role,
    label,
    detail: "交易所已确认工作中",
    price: value,
    authority: "SERVER_FACT",
    lineStyle: "solid",
    draggable: false,
  };
}

/**
 * Keeps exchange-confirmed runtime facts separate from local action intent.
 * An OPEN action alone never produces an order line: the venue must have
 * emitted a working order state. Fills alone establish the cost line.
 */
export function scalpVenueProjection(
  detail: ActivationDetail | null | undefined,
): ScalpVenueProjection {
  const actions = toActions(detail);
  const facts = toFacts(detail);
  const entries = actions.filter((action) => action.kind === "ENTRY");
  const entryIds = new Set(entries.map((action) => action.executionActionId));
  const averageEntry = weightedEntryPrice(facts, entryIds);
  const annotations: OrderChartPriceAnnotation[] = [];
  const completed = activationLifecycle(detail) === "COMPLETED";

  entries.filter((action) => venueWorking(facts, action)).forEach((action) => {
    const line = factualLine(action, "SINGLE_LIMIT", "交易所挂单");
    if (line) annotations.push(line);
  });

  if (averageEntry !== null) {
    annotations.push({
      id: "scalp-confirmed-entry-cost",
      role: "RUNTIME_ENTRY",
      label: "成交成本",
      detail: "本周期已确认成交的数量加权均价",
      price: averageEntry,
      authority: "SERVER_FACT",
      lineStyle: "solid",
      draggable: false,
    });
  }

  actions.filter((action) => action.kind === "PROTECTION" && venueWorking(facts, action))
    .forEach((action) => {
      const line = factualLine(action, "PROTECTION", "交易所止损");
      if (line) annotations.push(line);
    });
  actions.filter((action) => action.kind === "TAKE_PROFIT" && venueWorking(facts, action))
    .forEach((action) => {
      const line = factualLine(action, "TAKE_PROFIT", "交易所止盈");
      if (line) annotations.push(line);
    });

  const cutoff = factCutoff(facts);
  if (entries.some((action) => action.state === "UNKNOWN")) {
    return {
      status: "UNKNOWN",
      label: "交易所结果待确认",
      detail: "执行状态存在未知结果",
      factCutoff: cutoff,
      priceAnnotations: annotations,
    };
  }
  if (averageEntry !== null) {
    if (completed) {
      return {
        status: "CLOSED",
        label: "周期已结束",
        detail: "本周期的成交与退出已完成",
        factCutoff: cutoff,
        priceAnnotations: annotations,
      };
    }
    return {
      status: hasPartialEntryFill(facts, entryIds) ? "PARTIALLY_FILLED" : "FILLED",
      label: hasPartialEntryFill(facts, entryIds) ? "部分成交" : "已成交",
      detail: annotations.some((item) => item.role === "PROTECTION")
        ? "已成交，交易所止损已生效"
        : "已成交，等待保护委托回报",
      factCutoff: cutoff,
      priceAnnotations: annotations,
    };
  }
  if (entries.some((action) => venueWorking(facts, action))) {
    return {
      status: "WORKING",
      label: "交易所已挂单",
      detail: "等待成交或撤单回报",
      factCutoff: cutoff,
      priceAnnotations: annotations,
    };
  }
  if (entries.some((action) => action.state === "SUBMITTING" || action.state === "OPEN")) {
    return {
      status: "PENDING_VENUE",
      label: "等待交易所回报",
      detail: "请求已发送，尚未确认挂单或成交",
      factCutoff: cutoff,
      priceAnnotations: annotations,
    };
  }
  if (entries.some((action) => action.state === "CLOSED" || action.state === "NOT_SUBMITTED")) {
    return {
      status: "CLOSED",
      label: "未形成成交",
      detail: "入场动作已结束",
      factCutoff: cutoff,
      priceAnnotations: annotations,
    };
  }
  return {
    status: "WAITING",
    label: "等待执行",
    detail: "周期已创建，等待执行器处理",
    factCutoff: cutoff,
    priceAnnotations: annotations,
  };
}
