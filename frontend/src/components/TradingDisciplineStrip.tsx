import { Alert, Box, Chip, Stack, Typography } from "@mui/material";

import type { Overview } from "../api/client";
import { formatUserVisibleTime, quoteAmount } from "../format";
import { surfaceFrameSx } from "../theme";


const blockerLabels: Record<string, string> = {
  ACCOUNT_EQUITY_SNAPSHOT_UNAVAILABLE: "账户权益事实不可用",
  ACCOUNT_EQUITY_SNAPSHOT_STALE: "账户权益事实已过期",
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

function percent(value: string): string {
  const number = Number(value);
  if (!Number.isFinite(number)) return "未知";
  return `${(number * 100).toFixed(2).replace(/\.?0+$/, "")}%`;
}

function amount(value: string | null | undefined): string {
  return value === null || value === undefined
    ? "未知"
    : `${quoteAmount(value)} USDT`;
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
    return "当前为实盘只读观察：风险纪律只说明账户事实与额度计算结果，不能保存、固定或激活计划，也不能提交交易所动作。";
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
  const blockerText = discipline.blocker_codes
    .map((code) => blockerLabels[code] ?? code)
    .join("；");
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
      {blockerText && (
        <Alert severity={severity === "success" ? "info" : severity} variant="outlined" sx={{ mt: 1.5 }}>
          {blockerText}。这只阻止新的风险计划；已有风险仍可保护、撤单、减仓、退出或接管。
        </Alert>
      )}
    </Box>
  );
}
