import {
  ApiFailure,
  type OrderSchedulePreview,
  type OrderScheduleTransportSpec,
} from "../api/client";
import {
  finiteNumber,
  isNonNegative,
  isPositive,
  resolvedEntryProgram,
} from "./orderScheduleEditorModel";

export type ScheduleMilestoneStage = 0 | 1 | 2;

export type ScheduleServerProblem = {
  code: string;
  field: string | null;
  stages: readonly ScheduleMilestoneStage[];
};

export function localOrderScheduleProblems(
  spec: OrderScheduleTransportSpec,
  instrumentRef: string,
  maxNotional: string,
  scheduleRef: string,
): string[] {
  const problems: string[] = [];
  if (!instrumentRef.trim()) problems.push("请选择交易对象。");
  if (!scheduleRef.trim()) problems.push("缺少订单计划标识。");
  if (!isPositive(maxNotional)) problems.push("计划交易金额必须大于 0。");

  const price = spec.price_distribution;
  if (price.kind === "SINGLE") {
    if (price.limit_price !== null && !isPositive(price.limit_price)) {
      problems.push("限价必须大于 0。");
    }
  } else {
    if (!isPositive(price.lower_price)) problems.push("区间下限必须大于 0。");
    if (!isPositive(price.upper_price)) problems.push("区间上限必须大于 0。");
    if (
      !Number.isInteger(price.level_count)
      || price.level_count < 2
      || price.level_count > 50
    ) {
      problems.push("价格档位数必须为 2–50 的整数。");
    }
  }

  const amount = spec.amount_distribution;
  if (!isPositive(amount.base_notional)) {
    problems.push("起始下单金额必须大于 0。");
  }
  if (amount.mode === "LINEAR" && !isNonNegative(amount.linear_step)) {
    problems.push("线性下单额步长不能为负。");
  }
  if (amount.mode === "EXPONENTIAL" && !isPositive(amount.exponential_ratio)) {
    problems.push("指数下单额比例必须大于 0。");
  }
  if (
    amount.mode === "CUSTOM"
    && amount.custom_notionals.some((notional) => !isPositive(notional))
  ) {
    problems.push("自定义下单额必须全部大于 0。");
  }

  const entryProgram = resolvedEntryProgram(spec);
  if (
    !Number.isInteger(entryProgram.slice_count)
    || entryProgram.slice_count < 1
    || !Number.isInteger(entryProgram.first_slice_delay_seconds)
    || entryProgram.first_slice_delay_seconds < 0
    || !Number.isInteger(entryProgram.slice_interval_seconds)
    || entryProgram.slice_interval_seconds < 0
  ) {
    problems.push("分批数量、首次延迟和间隔必须填写有效整数。");
  }
  if (
    entryProgram.kind === "EVENT_TRIGGERED"
    && !spec.entry_conditions.items.some(
      (condition) => condition.kind !== "DECISION_BASIS_READY",
    )
  ) {
    problems.push("事件触发入场必须至少配置一个价格、K 线收盘或短时变动事件。");
  }

  for (const condition of spec.entry_conditions.items) {
    if (condition.kind === "MARK_PRICE" && !isPositive(condition.price)) {
      problems.push("标记价格条件必须大于 0。");
    }
    if (
      condition.kind === "CLOSED_BAR_PRICE_15M"
      && !isPositive(condition.price)
    ) {
      problems.push("15m 已闭合 K 线收盘阈值必须大于 0。");
    }
    if (condition.kind === "SPREAD_BPS" && !isNonNegative(condition.maximum_bps)) {
      problems.push("最大价差不能为负。");
    }
    if (
      condition.kind === "PRICE_MOVE_BPS"
      && (
        !isPositive(condition.threshold_bps)
        || !Number.isInteger(condition.window_seconds)
        || condition.window_seconds < 1
      )
    ) {
      problems.push("短时变动条件需要正阈值和正整数时间窗口。");
    }
  }

  const protection = spec.protection_policy;
  if (protection !== null) {
    const stopDistance = finiteNumber(protection.initial_stop.distance_bps);
    if (stopDistance === null || stopDistance <= 0 || stopDistance >= 10_000) {
      problems.push("初始止损距离必须大于 0 且低于 10000 bps。");
    }
    for (const level of protection.take_profit_ladder?.levels ?? []) {
      if (!isPositive(level.trigger_r)) problems.push("止盈目标 R 必须大于 0。");
      const fraction = finiteNumber(level.quantity_fraction);
      if (fraction === null || fraction <= 0 || fraction > 1) {
        problems.push("每档止盈比例必须大于 0 且不超过 100%。");
      }
    }
    if (
      protection.time_exit_seconds !== null
      && (
        !Number.isInteger(protection.time_exit_seconds)
        || protection.time_exit_seconds < 1
      )
    ) {
      problems.push("时间退出必须填写正整数秒数。");
    }
  }

  const venue = spec.venue_policy;
  if (venue.expire_at !== null && !Number.isFinite(Date.parse(venue.expire_at))) {
    problems.push("订单到期时间格式无效。");
  }
  return [...new Set(problems)];
}

function recordOf(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function dynamicRuleStages(
  spec: OrderScheduleTransportSpec,
  index: number | null,
): readonly ScheduleMilestoneStage[] {
  const kind = index === null ? null : spec.dynamic_rules[index]?.kind;
  return kind === "STEPPED_PROTECTION" || kind === "PROFIT_LOCK"
    ? [1, 2]
    : kind === null
      ? [0, 2]
      : [0];
}

function stagesForField(
  field: string | null,
  spec: OrderScheduleTransportSpec,
): readonly ScheduleMilestoneStage[] {
  if (!field) return [0, 1, 2];
  const parts = field.split(".");
  const protectionIndex = parts.indexOf("protection_policy");
  if (protectionIndex >= 0) {
    const child = parts[protectionIndex + 1];
    if (child === "initial_stop") return [1];
    if (child === "take_profit_ladder" || child === "time_exit_seconds") return [2];
    return [1, 2];
  }
  const dynamicIndex = parts.indexOf("dynamic_rules");
  if (dynamicIndex >= 0) {
    const rawIndex = Number(parts[dynamicIndex + 1]);
    return dynamicRuleStages(
      spec,
      Number.isInteger(rawIndex) && rawIndex >= 0 ? rawIndex : null,
    );
  }
  return [0];
}

function stagesForCode(code: string): readonly ScheduleMilestoneStage[] {
  if (
    code.includes("AUTOMATIC_EXIT")
    || code.includes("TAKE_PROFIT")
    || code.includes("PROFIT_LOCK")
  ) return [2];
  if (code.includes("PROTECTION_REQUIRED")) return [1];
  if (code === "PROTECTION_PRICE_INVALID") return [1, 2];
  if (
    code.startsWith("DIRECT_EXECUTION_")
    || code.startsWith("ORDER_SCHEDULE_")
    || code.startsWith("MARKET_ORDER_")
    || code.startsWith("POST_ONLY_")
    || code.startsWith("LIMIT_")
    || code.startsWith("GTD_")
    || code.startsWith("EXPIRE_REMAINING_")
    || code.startsWith("REPRICE_ENTRY_")
  ) return [0];
  return [0, 1, 2];
}

function validationProblems(
  error: unknown,
  spec: OrderScheduleTransportSpec,
): ScheduleServerProblem[] {
  if (!(error instanceof ApiFailure)) return [];
  const envelope = recordOf(error.detail);
  const detail = envelope?.detail;
  if (!Array.isArray(detail)) {
    return error.status === 409
      ? [{ code: error.code, field: null, stages: stagesForCode(error.code) }]
      : [];
  }
  return detail.flatMap((item) => {
    const validation = recordOf(item);
    const rawLocation = validation?.loc;
    if (!Array.isArray(rawLocation)) return [];
    const location = rawLocation
      .filter((part): part is string | number => (
        typeof part === "string" || typeof part === "number"
      ))
      .filter((part) => part !== "body" && part !== "spec")
      .map(String);
    const field = location.length > 0 ? location.join(".") : null;
    const rawCode = validation?.ctx;
    const context = recordOf(rawCode);
    const contextError = context?.error;
    const code = typeof contextError === "string"
      ? contextError
      : typeof validation?.type === "string"
        ? validation.type
        : "INPUT_VALIDATION_FAILED";
    return [{ code, field, stages: stagesForField(field, spec) }];
  });
}

export function scheduleServerProblems(
  preview: OrderSchedulePreview | undefined,
  error: unknown,
  spec: OrderScheduleTransportSpec,
): ScheduleServerProblem[] {
  const result = preview?.issues.map((issue) => ({
    code: issue.code,
    field: issue.field,
    stages: stagesForField(issue.field, spec),
  })) ?? validationProblems(error, spec);
  const unique = new Map<string, ScheduleServerProblem>();
  for (const problem of result) {
    unique.set(
      `${problem.code}:${problem.field ?? ""}:${problem.stages.join(",")}`,
      problem,
    );
  }
  return [...unique.values()];
}

export function serverScheduleWasAssessed(
  preview: OrderSchedulePreview | undefined,
  error: unknown,
): boolean {
  return preview !== undefined
    || (error instanceof ApiFailure && (error.status === 409 || error.status === 422));
}

export function stageHasServerProblem(
  problems: readonly ScheduleServerProblem[],
  stage: ScheduleMilestoneStage,
): boolean {
  return problems.some((problem) => problem.stages.includes(stage));
}

export function milestoneConfigurationReady(
  localReady: boolean,
  serverAssessed: boolean,
  serverHasProblem: boolean,
): boolean {
  return localReady && !(serverAssessed && serverHasProblem);
}
