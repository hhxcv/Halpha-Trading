import { Alert, Box, Chip, Stack, Typography } from "@mui/material";

import type { Overview } from "../api/client";
import { formatUserVisibleTime, quoteCurrencyAmount } from "../format";
import { surfaceFrameSx } from "../theme";


export const newRiskDisciplineBlockerLabels: Record<string, string> = {
  ACCOUNT_EQUITY_SNAPSHOT_UNAVAILABLE: "账户权益事实不可用",
  ACCOUNT_EQUITY_SNAPSHOT_STALE: "账户权益快照未刷新",
  ACCOUNT_EQUITY_SNAPSHOT_TIME_INVALID: "账户权益事实时间异常",
  ACCOUNT_RISK_EQUITY_NOT_POSITIVE: "风险权益不为正数",
  ACCOUNT_TRADING_DISABLED: "交易所账户当前不允许交易",
  NEW_RISK_PROPOSED_LOSS_INVALID: "计划最大允许损失不可读",
  NEW_RISK_PLAN_LOSS_LIMIT_EXCEEDED: "本计划损失超过单计划上限",
  NEW_RISK_OPEN_RISK_LIMIT_EXCEEDED: "计划最坏损失将超过组合风险容量",
  NEW_RISK_GROSS_EXPOSURE_LIMIT_EXCEEDED: "计划将超过账户总名义敞口上限",
  NEW_RISK_INSTRUMENT_EXPOSURE_LIMIT_EXCEEDED: "计划将超过单品种集中度上限",
  NEW_RISK_CORRELATED_EXPOSURE_LIMIT_EXCEEDED: "计划将超过相关风险簇敞口上限",
  NEW_RISK_CORRELATION_CLUSTER_UNKNOWN: "该品种没有已定义的相关风险簇",
  NEW_RISK_PROPOSED_NOTIONAL_INVALID: "计划最大名义金额不可读",
  NEW_RISK_PROPOSED_INSTRUMENT_INVALID: "计划交易品种不可读",
  NEW_RISK_PROPOSED_DIRECTION_INVALID: "计划交易方向不可读",
  NEW_RISK_ENTRY_DIRECTION_INVALID: "待提交入场方向不可读",
  NEW_RISK_LOSING_POSITION_ADD_PROHIBITED: "同品种同向持仓浮亏，禁止追加仓位",
  NEW_RISK_DAILY_LOSS_STOP_REACHED: "当日已实现亏损已触发新增风险停止",
  NEW_RISK_WEEKLY_LOSS_STOP_REACHED: "本周已实现亏损已触发新增风险停止",
  NEW_RISK_ROLLING_DRAWDOWN_STOP_REACHED: "滚动账户回撤已触发新增风险停止",
};

export type NewRiskDiscipline = Overview["new_risk_discipline"];

type NewRiskDisciplineRecoveryInput = Partial<Pick<
  NewRiskDiscipline,
  | "day_window_started_at"
  | "week_window_started_at"
  | "rolling_drawdown_lookback_days"
>> & {
  blocker_codes: readonly string[];
};

export type NewRiskDisciplineRecoveryCue = {
  key: string;
  text: string;
};

export function newRiskDisciplineBlockerText(codes: readonly string[]): string {
  return codes
    .map((code) => newRiskDisciplineBlockerLabels[code] ?? code)
    .join("；");
}

export function newRiskDisciplineBlockerCodesFromFailureCode(
  code: string,
): string[] {
  return code
    .split(/[;,]/u)
    .map((item) => item.trim())
    .filter((item) => item in newRiskDisciplineBlockerLabels);
}

type AccountObservationFacts = Pick<
  Overview,
  | "account_snapshot_status"
  | "account_snapshot_age_seconds"
  | "account_observation_failure_code"
>;

function accountObservationFailureLabel(code: string): string {
  if (code.startsWith("ACCOUNT_SNAPSHOT_QUERY_FAILED_")) {
    if (code.includes("TIMEOUT")) return "交易所账户查询超时";
    if (code.includes("CONNECTION") || code.includes("OSERROR")) {
      return "交易所账户查询连接失败";
    }
    if (code.includes("HTTPERROR")) return "交易所账户查询返回错误";
    return "交易所账户查询失败";
  }
  if (code.startsWith("ACCOUNT_SNAPSHOT_PERSIST_FAILED_")) {
    return "账户快照保存失败";
  }
  if (code === "ACCOUNT_SNAPSHOT_IDENTITY_CONFLICT") {
    return "账户快照写入冲突";
  }
  return "账户快照刷新失败";
}

/**
 * Separates a missing snapshot from its known operational cause.  A stale
 * snapshot is an age result, not evidence of a network fault.
 */
export function accountObservationBlockerText(
  facts: AccountObservationFacts | null | undefined,
  executorStatus?: string,
): string | null {
  if (!facts) return null;
  const failureCode = facts.account_observation_failure_code;
  if (failureCode) return accountObservationFailureLabel(failureCode);
  if (facts.account_snapshot_status === "STALE") {
    const age = facts.account_snapshot_age_seconds;
    return Number.isInteger(age) && age !== null
      ? `账户权益快照未刷新（${age} 秒）`
      : "账户权益快照未刷新";
  }
  if (facts.account_snapshot_status === "UNAVAILABLE") {
    if (executorStatus === "UNAVAILABLE") return "账户观察服务未运行";
    if (executorStatus === "STARTING") return "账户观察服务正在启动";
    if (executorStatus === "BUILD_MISMATCH") return "账户观察服务版本不一致";
    return "账户权益快照不可用";
  }
  if (facts.account_snapshot_status === "UNKNOWN") {
    return "账户权益快照状态异常";
  }
  return null;
}

function nextWindowStart(
  windowStartedAt: string | undefined,
  windowMilliseconds: number,
): string | null {
  if (!windowStartedAt) return null;
  const start = Date.parse(windowStartedAt);
  if (!Number.isFinite(start)) return null;
  return new Date(start + windowMilliseconds).toISOString();
}

function earliestWindowReviewText(
  nextWindowAt: string | null,
  label: string,
): string {
  return nextWindowAt
    ? `最早可在 ${formatUserVisibleTime(nextWindowAt)}（${label}重新开始）后重新核对；这不是恢复承诺，仍须以届时完整账户事实和全部阻断项为准。`
    : `等待${label}重新开始后重新核对；当前无法计算确切时刻，仍须以完整账户事实和全部阻断项为准。`;
}

export function newRiskDisciplineRecoveryGuidance(
  discipline: NewRiskDisciplineRecoveryInput,
): NewRiskDisciplineRecoveryCue[] {
  const codes = new Set(discipline.blocker_codes);
  const cues: NewRiskDisciplineRecoveryCue[] = [];
  const add = (key: string, text: string) => {
    if (!cues.some((item) => item.key === key)) cues.push({ key, text });
  };
  const hasAny = (...values: string[]) => values.some((value) => codes.has(value));

  if (hasAny(
    "ACCOUNT_EQUITY_SNAPSHOT_UNAVAILABLE",
    "ACCOUNT_EQUITY_SNAPSHOT_STALE",
    "ACCOUNT_EQUITY_SNAPSHOT_TIME_INVALID",
  )) {
    add(
      "fresh-account-facts",
      "等待下一次完整账户快照后重新核对；如账户观察已报告原因，请先处理该原因。",
    );
  }
  if (codes.has("ACCOUNT_TRADING_DISABLED")) {
    add(
      "trading-enabled",
      "需要交易所账户恢复可交易，并由新的完整账户快照证明；当前没有可承诺的恢复时刻。",
    );
  }
  if (codes.has("ACCOUNT_RISK_EQUITY_NOT_POSITIVE")) {
    add(
      "positive-risk-equity",
      "钱包余额与保证金余额的较小值需要恢复为正数，并由新的完整账户快照重新计算；这不是资金保证或自动入金提示。",
    );
  }
  if (hasAny(
    "NEW_RISK_PROPOSED_LOSS_INVALID",
    "NEW_RISK_PROPOSED_NOTIONAL_INVALID",
    "NEW_RISK_PROPOSED_INSTRUMENT_INVALID",
    "NEW_RISK_PROPOSED_DIRECTION_INVALID",
    "NEW_RISK_ENTRY_DIRECTION_INVALID",
  )) {
    add(
      "plan-input",
      "修正草稿的金额、品种或方向后重新形成服务端预览；运行中计划的实质修改应创建新草稿，不能绕过检查。",
    );
  }
  if (codes.has("NEW_RISK_PLAN_LOSS_LIMIT_EXCEEDED")) {
    add(
      "plan-loss-limit",
      "将草稿最大预计损失收窄到当前单计划上限以内，再重新提交并启动；不以等待时间解除。",
    );
  }
  if (hasAny(
    "NEW_RISK_OPEN_RISK_LIMIT_EXCEEDED",
    "NEW_RISK_GROSS_EXPOSURE_LIMIT_EXCEEDED",
    "NEW_RISK_INSTRUMENT_EXPOSURE_LIMIT_EXCEEDED",
    "NEW_RISK_CORRELATED_EXPOSURE_LIMIT_EXCEEDED",
  )) {
    add(
      "portfolio-capacity",
      "等待相关未闭合计划、持仓、开放委托或在途责任真实变化后，以新的完整账户快照重算容量；没有固定等待时长，也不应为了清除门槛而自动平仓。",
    );
  }
  if (codes.has("NEW_RISK_DAILY_LOSS_STOP_REACHED")) {
    add(
      "daily-loss-window",
      earliestWindowReviewText(
        nextWindowStart(discipline.day_window_started_at, 24 * 60 * 60 * 1_000),
        "纪律日窗口",
      ),
    );
  }
  if (codes.has("NEW_RISK_WEEKLY_LOSS_STOP_REACHED")) {
    add(
      "weekly-loss-window",
      earliestWindowReviewText(
        nextWindowStart(discipline.week_window_started_at, 7 * 24 * 60 * 60 * 1_000),
        "纪律周窗口",
      ),
    );
  }
  if (codes.has("NEW_RISK_ROLLING_DRAWDOWN_STOP_REACHED")) {
    const lookbackDays = discipline.rolling_drawdown_lookback_days;
    add(
      "rolling-drawdown",
      `没有固定解锁时刻。刷新后账户回撤必须低于阈值；${Number.isInteger(lookbackDays) ? `${lookbackDays} 日` : "滚动"}权益高水位何时移出窗口及届时权益都会影响结果。`,
    );
  }
  if (codes.has("NEW_RISK_LOSING_POSITION_ADD_PROHIBITED")) {
    add(
      "losing-position",
      "同品种同方向持仓需要不再浮亏或已不存在，并由新的完整账户快照确认；普通新计划没有按时间自动恢复的例外。",
    );
  }
  if (codes.has("NEW_RISK_CORRELATION_CLUSTER_UNKNOWN")) {
    add(
      "correlation-cluster",
      "当前品种没有受支持的相关风险簇。这不是等待可解决的状态；请改用受支持品种，不能把未知风险簇当作零风险。",
    );
  }
  for (const code of codes) {
    if (!(code in newRiskDisciplineBlockerLabels)) continue;
    if (!cues.some((item) => item.key === code)) {
      add(
        code,
        "需要在下一次服务端当前事实复核中确认该原因已解除；当前没有可承诺的恢复时刻。",
      );
    }
  }
  return cues;
}

export function NewRiskDisciplineBlockNotice({
  discipline,
  blockerCodes = discipline?.blocker_codes ?? [],
  title = "新增风险纪律未通过",
  consequence = "这只阻止新的风险计划；已有风险仍可保护、撤单、减仓、退出或接管。",
}: {
  discipline?: NewRiskDiscipline | null;
  blockerCodes?: readonly string[];
  title?: string;
  consequence?: string;
}) {
  const effectiveBlockerCodes = blockerCodes.length > 0
    ? blockerCodes
    : discipline?.blocker_codes ?? [];
  if (effectiveBlockerCodes.length === 0 && discipline?.new_risk_allowed !== false) {
    return null;
  }
  const recovery = newRiskDisciplineRecoveryGuidance({
    blocker_codes: effectiveBlockerCodes,
    day_window_started_at: discipline?.day_window_started_at,
    week_window_started_at: discipline?.week_window_started_at,
    rolling_drawdown_lookback_days: discipline?.rolling_drawdown_lookback_days,
  });
  if (recovery.length === 0 && discipline?.new_risk_allowed === false) {
    recovery.push({
      key: "current-facts",
      text: "重新读取服务端当前事实后再核对；当前没有可承诺的恢复时刻，也不能由页面确认覆盖。",
    });
  }
  const severity = discipline?.status === "UNKNOWN" ? "warning" : "error";

  return (
    <Alert severity={severity} variant="outlined" aria-live="polite">
      <Typography component="div" sx={{ fontWeight: 800 }}>{title}</Typography>
      <Typography component="div" variant="body2" sx={{ mt: .35 }}>
        {newRiskDisciplineBlockerText(effectiveBlockerCodes) || "当前事实无法确认"}。
      </Typography>
      {recovery.length > 0 && (
        <Box component="ul" sx={{ pl: 2.25, my: .75, "& li + li": { mt: .35 } }}>
          {recovery.map((item) => <li key={item.key}>
            <Typography component="span" variant="body2">{item.text}</Typography>
          </li>)}
        </Box>
      )}
      <Typography component="div" variant="body2">{consequence}</Typography>
    </Alert>
  );
}

function percent(value: string): string {
  const number = Number(value);
  if (!Number.isFinite(number)) return "未知";
  return `${(number * 100).toFixed(2).replace(/\.?0+$/, "")}%`;
}

function amount(value: string | null | undefined): string {
  return value === null || value === undefined
    ? "未知"
    : `${quoteCurrencyAmount(value)} USDT`;
}

export function maxPlanLossFractionNote(
  riskEquity: string | null | undefined,
  maxPlanLoss: string | null | undefined,
): string {
  const equity = Number(riskEquity);
  const loss = Number(maxPlanLoss);
  if (
    !Number.isFinite(equity)
    || !Number.isFinite(loss)
    || equity <= 0
    || loss < 0
  ) {
    return "由当前环境配置和纪律权益计算";
  }
  return `当前上限为纪律权益的 ${percent(String(loss / equity))}`;
}

type TradingAccessFacts = Pick<
  Overview,
  "environment_kind" | "profile" | "runtime_real_write_gate"
>;

export function tradingAccessNotice(facts: TradingAccessFacts): string | null {
  if (facts.profile === "BINANCE_LIVE_READ_ONLY") {
    return "当前为实盘只读观察：风险纪律只说明账户事实与额度计算结果，不能保存或提交并启动计划，也不能提交交易所动作。";
  }
  if (
    facts.environment_kind === "LIVE"
    && facts.runtime_real_write_gate !== "OPEN"
  ) {
    return "当前实盘写门关闭：可以准备计划，但不能启动新增风险或提交交易所动作。";
  }
  return null;
}

export default function TradingDisciplineStrip({ overview }: { overview: Overview }) {
  const discipline = overview.new_risk_discipline;
  const summary = overview.account_summary;
  const severity = discipline.status === "UNKNOWN"
    ? "warning"
    : discipline.status === "BLOCKED"
      ? "error"
      : "success";
  const statusLabel = discipline.status === "UNKNOWN"
    ? "事实未知"
    : discipline.status === "BLOCKED"
      ? "暂停新增风险"
      : "风险纪律允许";
  const accessNotice = tradingAccessNotice(overview);
  const facts = [
    {
      label: "账户权益",
      value: amount(summary?.margin_balance),
      note: summary
        ? `钱包 ${amount(summary.wallet_balance)} · 未实现 ${amount(summary.unrealized_pnl)}`
        : "等待 V3 完整账户快照",
    },
    {
      label: "纪律权益基准",
      value: amount(discipline.risk_equity),
      note: "取当前账户快照中钱包余额与保证金余额的较小值",
    },
    {
      label: "单计划最大损失",
      value: amount(discipline.max_plan_loss),
      note: maxPlanLossFractionNote(
        discipline.risk_equity,
        discipline.max_plan_loss,
      ),
    },
    {
      label: "最低计划收益 / 风险",
      value: `${discipline.minimum_reward_risk_ratio}R`,
      note: "按价格止盈数量比例加权；时间退出不能替代该新增风险纪律",
    },
    {
      label: "组合风险容量",
      value: `${amount(discipline.open_risk_committed)} / ${amount(discipline.open_risk_limit)}`,
      note: `未闭合新增风险计划 ${discipline.open_new_risk_activation_count} 份；按最坏损失聚合，不按订单数限制`,
    },
    {
      label: "账户总敞口",
      value: `${amount(discipline.gross_exposure)} / ${amount(discipline.gross_exposure_limit)}`,
      note: `单品种 ${amount(discipline.instrument_exposure)} · 相关簇 ${amount(discipline.correlated_exposure)}`,
    },
    {
      label: "亏损与回撤停止",
      value: `日 ${amount(discipline.daily_loss_measure)} / ${amount(discipline.daily_loss_limit)}`,
      note: `周 ${amount(discipline.weekly_loss_measure)} / ${amount(discipline.weekly_loss_limit)} · ${discipline.rolling_drawdown_lookback_days} 日回撤 ${percent(discipline.rolling_drawdown_fraction ?? "")}`,
    },
  ];

  return (
    <Box
      component="section"
      aria-label="账户权益与新增风险纪律"
      sx={{ ...surfaceFrameSx, p: { xs: 1.5, md: 2 }, mb: 2 }}
    >
      <Stack
        direction={{ xs: "column", sm: "row" }}
        spacing={1}
        sx={{ justifyContent: "space-between", alignItems: { xs: "flex-start", sm: "center" }, mb: 1.5 }}
      >
        <Box>
          <Typography variant="h2">账户纪律</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: .35 }}>
            以计划最坏损失、账户敞口、集中度、相关风险和损失停止共同决定新增风险容量；保护与退出不受阻。
          </Typography>
        </Box>
        <Stack direction="row" spacing={1} sx={{ alignItems: "center", flexWrap: "wrap" }}>
          <Chip size="small" color={severity} variant="outlined" label={statusLabel} />
          <Typography variant="caption" color="text.secondary">
            {discipline.account_snapshot_cutoff
              ? `权益截止 ${formatUserVisibleTime(discipline.account_snapshot_cutoff)}`
              : "等待账户权益事实"}
          </Typography>
        </Stack>
      </Stack>
      <Box
        sx={{
          display: "grid",
          gridTemplateColumns: {
            xs: "minmax(0, 1fr)",
            sm: "repeat(2, minmax(0, 1fr))",
            lg: "repeat(3, minmax(0, 1fr))",
          },
          gap: 1.5,
        }}
      >
        {facts.map((fact) => (
          <Box key={fact.label} sx={{ minWidth: 0 }}>
            <Typography variant="caption" color="text.secondary">{fact.label}</Typography>
            <Typography className="mono" sx={{ fontWeight: 800, mt: .2 }}>{fact.value}</Typography>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: .2 }}>
              {fact.note}
            </Typography>
          </Box>
        ))}
      </Box>
      {accessNotice && (
        <Alert severity="info" variant="outlined" sx={{ mt: 1.5 }}>
          {accessNotice}
        </Alert>
      )}
      {discipline.blocker_codes.length > 0 && (
        <Box sx={{ mt: 1.5 }}>
          <NewRiskDisciplineBlockNotice discipline={discipline} />
        </Box>
      )}
    </Box>
  );
}
