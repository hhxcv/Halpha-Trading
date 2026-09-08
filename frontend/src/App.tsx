import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Alert,
  AppBar,
  Box,
  Button,
  Checkbox,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  Divider,
  Drawer,
  FormControlLabel,
  FormGroup,
  IconButton,
  LinearProgress,
  List,
  ListItem,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  MenuItem,
  Stack,
  Tab,
  Tabs,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  Toolbar,
  Tooltip,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
  useMediaQuery,
  useTheme,
} from "@mui/material";
import AssignmentOutlined from "@mui/icons-material/AssignmentOutlined";
import ArrowBackOutlined from "@mui/icons-material/ArrowBackOutlined";
import ChevronLeftOutlined from "@mui/icons-material/ChevronLeftOutlined";
import ChevronRightOutlined from "@mui/icons-material/ChevronRightOutlined";
import DashboardOutlined from "@mui/icons-material/DashboardOutlined";
import BoltOutlined from "@mui/icons-material/BoltOutlined";
import InfoOutlined from "@mui/icons-material/InfoOutlined";
import MenuOutlined from "@mui/icons-material/MenuOutlined";
import OpenInNewOutlined from "@mui/icons-material/OpenInNewOutlined";
import ReviewsOutlined from "@mui/icons-material/ReviewsOutlined";
import SettingsOutlined from "@mui/icons-material/SettingsOutlined";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Link,
  Navigate,
  Outlet,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useOutletContext,
  useParams,
} from "react-router";

import {
  ApiFailure,
  createPlan,
  createActivation,
  completeReview,
  deletePlan,
  getActivation,
  getActivations,
  getActivationTimeline,
  getActivationPreview,
  getMarketContext,
  getMarketWindow,
  getOverview,
  getPlans,
  getReview,
  getReviewPricePathEvidence,
  getReviewSequenceEvidence,
  getReviews,
  getSettingsStatus,
  getStrategies,
  isUnknownMutationResult,
  previewControl,
  previewAccountPositionOperation,
  previewSystemStopRelease,
  refreshReview,
  releaseSystemStop,
  sendTestEmail,
  type ControlIntent,
  type ActivationDetail,
  type ActivationSummary,
  type MarketInterval,
  type OrderSchedulePreview,
  type OrderSchedulePreviewLeg,
  type OrderScheduleSpec,
  type PlanKeyParameterDefinition,
  type PlanSummary,
  type PlanCreatePayload,
  type Overview,
  type ReviewCompletionPayload,
  type ReviewPricePathInterval,
  type SettingsStatus,
} from "./api/client";
import { submitActivationControlWithFreshRiskReducingRetry } from "./api/controlSubmission";
import PageHeader from "./components/PageHeader";
import FactGrid from "./components/FactGrid";
import TradingDisciplineStrip, {
  NewRiskDisciplineBlockNotice,
  newRiskDisciplineBlockerCodesFromFailureCode,
} from "./components/TradingDisciplineStrip";
import PlanAiReviewRecord from "./components/PlanAiReviewRecord";
import ReviewEntrySetupEvidencePanel from "./components/ReviewEntrySetupEvidencePanel";
import ReviewPricePathEvidencePanel from "./components/ReviewPricePathEvidencePanel";
import type { OrderChartPriceAnnotation } from "./components/orderScheduleChartModel";
import {
  tradingAccountLabel,
  tradingContextLabel,
  tradingContextSwitchTarget,
  type TradingContextTarget,
  type VenueAccountType,
} from "./environmentSwitch";
import {
  readChartIntervalPreference,
  writeChartIntervalPreference,
} from "./chartIntervalPreference";
import {
  defaultExecutionWindowInterval,
  defaultReviewChartInterval,
  executionWindowEndAt,
  executionWindowFitsInterval,
  reviewWindowFitsInterval,
  type ReviewChartInterval,
} from "./reviewChartInterval";
import { runtimeChartOperationMarkers } from "./runtimeChartMarkers";
import {
  latestActivationsByPlanVersion,
  orderScheduleConditionIntent,
  orderScheduleIntent,
  planWorkbenchSections,
} from "./planListModel";
import { buildPlanPnlTrend } from "./planPnlTrend";
import {
  liveProfitQualificationBlockerLabels,
  liveProfitQualificationPresentation,
  liveProfitQualificationSatisfied,
  qualificationFractionPercent,
} from "./liveProfitQualification";
import {
  basisPoints,
  closedBarBreakoutGapPercent,
  entryExtensionBoundary,
  estimateImmediateExit,
  estimateMarkedNetResult,
  formatCompactUserVisibleTime,
  formatUserVisibleTime,
  gapPercent,
  latestUtc,
  marketPrice,
  marketVolume,
  pendingBreakoutNote,
  positiveFiniteNumber,
  planEventSummary,
  quoteAmount,
  quoteCurrencyAmount,
  shortDigest,
  subtractDecimal,
  tradingPrice,
  tradingQuantity,
  unknownExecutionReasonText,
  venueRejectionKind,
  venueReasonText,
} from "./format";
import {
  applyMarketColorScheme,
  DEFAULT_MARKET_COLOR_SCHEME,
  FinancialToneText,
  MarketToneText,
  financialToneForSignedValue,
  marketToneClassName,
  marketToneForDirection,
  readMarketColorScheme,
  saveMarketColorScheme,
  type MarketColorScheme,
} from "./marketColors";
import {
  expectedMarketSourceForEnvironment,
  isMarketSourceForEnvironment,
  isUsableMarketStreamFunding,
  marketEnvironmentScopeKey,
  usePublicMarketStream,
} from "./marketStream";
import { formatFundingRatePercent } from "./fundingEstimate";
import {
  clearPersistentRequestIdentity,
  persistentRequestIdentity,
  type StableRequestIdentity,
} from "./requestIdentity";
import {
  LOSS_STREAK_ALERT_THRESHOLD,
} from "./reviewPerformanceSummary";
import {
  boundedProgressPercent,
  activationSummaryCloseReason,
  compactRuntimeTimeline,
  currentRuntimeProtectionPrice,
  currentAccountSystemStop,
  elapsedDurationLabel,
  evaluateRuntimeEntryConditions,
  groupRuntimeTakeProfits,
  nextRuntimeProtectionStep,
  relativeAgeLabel,
  remainingTimeLabel,
  reviewExitReason,
  runtimeActionHasCurrentResponsibility,
  runtimeFilledEntryLegCount,
  runtimeDynamicCancelPresentation,
  runtimeNotSubmittedEntryPresentation,
  runtimeEntryInterruptionPresentation,
  runtimeConditionPendingPresentation,
  runtimeEntryOrderDeadline,
  runtimeNoActionPresentation,
  runtimePlanEventDynamicCancelPresentation,
  runtimeHasEntryFill,
  runtimeHasPendingVenueAction,
  runtimeProtectionAttention,
  runtimeEntryConditionClauses,
  runtimeExecutorConditionStatus,
  runtimeEventCategory,
  runtimeEntryPolicyRetryState,
  runtimeEntryOrderAttemptedBefore,
  runtimeSignedMovePresentation,
  terminalEntryResultRequiresReview,
  runtimeWorkingEntryOrders,
  type CompactRuntimeTimelineItem,
  type RuntimeEventCategory,
  type RuntimeEntryConditionEvaluation,
  type RuntimeEntryConditionState,
  type RuntimeEntryOrderDeadline,
  type RuntimeExecutorConditionStatus,
} from "./runtimePresentation";
import { surfaceFrameSx } from "./theme";

const DRAWER_WIDTH = 236;
const COLLAPSED_DRAWER_WIDTH = 72;
const NAVIGATION_COLLAPSED_STORAGE_KEY = "halpha.navigation-collapsed.v1";
const STATUS_QUERY_KEY = ["settings-status"] as const;
const DIRECT_EXECUTION_KIND = "DIRECT_EXECUTION";
const DIRECT_EXECUTION_LABEL = "直接执行订单计划";
const NewPlanPage = lazy(() => import("./pages/NewPlanPage"));
const ScalpingPage = lazy(() => import("./pages/ScalpingPage"));

const OrderScheduleChart = lazy(() => import("./components/OrderScheduleChart"));
const ReviewPriceChart = lazy(() => import("./components/ReviewCharts").then((module) => ({ default: module.ReviewPriceChart })));
const PlanPnlChart = lazy(() => import("./components/ReviewCharts").then((module) => ({ default: module.PlanPnlChart })));
const ReviewPerformanceOverview = lazy(() => import("./components/ReviewPerformanceOverview"));
const ReviewSequenceEvidencePanel = lazy(() => import("./components/ReviewSequenceEvidencePanel"));
const StageReviewPanel = lazy(() => import("./components/StageReviewPanel"));
const visuallyHiddenSx = {
  position: "absolute",
  width: "1px",
  height: "1px",
  p: 0,
  m: "-1px",
  overflow: "hidden",
  clip: "rect(0 0 0 0)",
  whiteSpace: "nowrap",
  border: 0,
} as const;

function readNavigationCollapsed(): boolean {
  if (typeof window === "undefined") return true;
  try {
    return window.localStorage.getItem(NAVIGATION_COLLAPSED_STORAGE_KEY) !== "expanded";
  } catch {
    return true;
  }
}

function saveNavigationCollapsed(collapsed: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(NAVIGATION_COLLAPSED_STORAGE_KEY, collapsed ? "collapsed" : "expanded");
  } catch {
    // A blocked preference store must not prevent navigation.
  }
}

function valueOf(record: Record<string, unknown> | undefined, key: string, fallback = "UNKNOWN"): string {
  const value = record?.[key];
  return value === null || value === undefined ? fallback : String(value);
}

function recordOf(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function finiteNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

const POST_ONLY_RETRY_MAX_ATTEMPTS = 5;

function recordsOf(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.map(recordOf) : [];
}

function isDirectExecution(value: unknown): boolean {
  return String(value ?? "").split("@", 1)[0] === DIRECT_EXECUTION_KIND;
}

function reviewDecisionBasisKind(context: Record<string, unknown>): string {
  const decisionBasisRef = valueOf(context, "decision_basis_ref", "");
  if (isDirectExecution(decisionBasisRef)) return DIRECT_EXECUTION_KIND;
  return valueOf(context, "strategy_id", decisionBasisRef.split("@", 1)[0]);
}

function orderScheduleSpecOf(value: unknown): Record<string, unknown> {
  const schedule = recordOf(value);
  const compiledSpec = recordOf(schedule.schedule_spec);
  return Object.keys(compiledSpec).length > 0 ? compiledSpec : schedule;
}

function orderSchedulePreviewOf(value: unknown): OrderSchedulePreview | null {
  const snapshot = recordOf(value);
  return (
    Object.keys(recordOf(snapshot.schedule_spec)).length > 0
    && Array.isArray(snapshot.normalized_legs)
    && Object.keys(recordOf(snapshot.instrument_rules)).length > 0
  )
    ? snapshot as unknown as OrderSchedulePreview
    : null;
}

const RUNTIME_CHART_FALLBACK_SPEC: OrderScheduleSpec = {
  entry_program: {
    kind: "ONE_TIME",
    slice_count: 1,
    first_slice_delay_seconds: 0,
    slice_interval_seconds: 0,
  },
  price_distribution: { kind: "SINGLE", limit_price: null },
  amount_distribution: {
    mode: "FIXED",
    direction: "LOW_TO_HIGH",
    base_notional: "0",
    linear_step: "0",
    exponential_ratio: "1",
    custom_notionals: [],
  },
  venue_policy: {
    order_type: "MARKET",
    time_in_force: null,
    post_only: false,
    price_match: null,
    expire_at: null,
  },
  submission_mode: "SERIAL_PROTECTED",
  submission_order: "LOW_TO_HIGH",
  entry_conditions: { operator: "ALL", items: [] },
  protection_policy: {
    initial_stop: {
      distance_bps: "0",
      trigger_source: "MARK_PRICE",
      coverage: "EACH_CONFIRMED_FILL",
    },
    full_fill_loss_budget: null,
    take_profit_ladder: null,
    time_exit_seconds: null,
  },
  dynamic_rules: [],
};

function orderScheduleSummary(value: unknown): string {
  const snapshot = orderSchedulePreviewOf(value);
  if (snapshot) {
    return orderScheduleIntent(snapshot.schedule_spec, snapshot)
      ?? "订单计划不可读";
  }
  const spec = orderScheduleSpecOf(value);
  if (Object.keys(spec).length === 0) return "订单计划不可读";
  return orderScheduleIntent(spec as unknown as OrderScheduleSpec)
    ?? "订单计划不可读";
}

function orderConditionSummary(value: unknown): string {
  const spec = orderScheduleSpecOf(value);
  return orderScheduleConditionIntent(spec as unknown as OrderScheduleSpec)
    ?? "无附加入场条件";
}

function positionAlignmentIntent(value: unknown): string | null {
  const alignment = recordOf(value);
  if (Object.keys(alignment).length === 0) return null;
  const operation = valueOf(alignment, "operation");
  const reduction = marketVolume(valueOf(alignment, "requested_reduction_quantity"));
  const target = marketVolume(valueOf(alignment, "target_quantity_after"));
  return operation === "CLOSE"
    ? `平仓 ${reduction} → 目标 0`
    : `减仓 ${reduction} → 剩余 ${target}`;
}

function positionAlignmentOperationLabel(value: unknown): string {
  return valueOf(recordOf(value), "operation") === "CLOSE"
    ? "外部持仓平仓"
    : "外部持仓减仓";
}

function directEntryConditionDetail(value: unknown): string {
  const spec = orderScheduleSpecOf(value);
  if (Object.keys(spec).length === 0) return "正在读取运行计划条件";
  const conditions = recordOf(spec.entry_conditions);
  const clauses = recordsOf(conditions.items).flatMap((condition) => {
    const kind = valueOf(condition, "kind");
    if (kind === "DECISION_BASIS_READY") return [];
    if (kind === "MARK_PRICE") {
      const comparator = valueOf(condition, "comparator") === "LTE" ? "≤" : "≥";
      return [`标记价 ${comparator} ${marketPrice(valueOf(condition, "price"))} USDT`];
    }
    if (kind === "CLOSED_BAR_PRICE_15M") {
      const comparator = valueOf(condition, "comparator") === "LTE" ? "≤" : "≥";
      return [`15m 已闭合 K 线收盘 ${comparator} ${marketPrice(valueOf(condition, "price"))} USDT`];
    }
    if (kind === "SPREAD_BPS") {
      return [`买卖价差 ≤ ${quoteAmount(valueOf(condition, "maximum_bps"))} bps`];
    }
    if (kind === "PRICE_MOVE_BPS") {
      const comparator = valueOf(condition, "comparator");
      const move = comparator === "DROP_GTE"
        ? "下跌 ≥"
        : comparator === "ABS_GTE"
          ? "绝对变动 ≥"
          : comparator === "LTE"
            ? "有符号变动 ≤"
            : "上涨 ≥";
      return [
        `${valueOf(condition, "window_seconds")} 秒价格${move} ${quoteAmount(valueOf(condition, "threshold_bps"))} bps`,
      ];
    }
    return [];
  });
  if (clauses.length === 0) return "启动后按已提交计划入场";
  const operator = valueOf(conditions, "operator", "ALL") === "ANY"
    ? "任一条件成立"
    : "以下条件同时成立";
  return `${operator}：${clauses.join("；")}`;
}

function runtimeConditionLabel(condition: RuntimeEntryConditionState): string {
  if (condition.kind === "DECISION_BASIS_READY") return "执行依据";
  if (condition.kind === "MARK_PRICE") return "标记价";
  if (condition.kind === "CLOSED_BAR_PRICE_15M") return "15m 收盘";
  if (condition.kind === "SPREAD_BPS") return "买卖价差";
  return `${condition.windowSeconds ?? "?"} 秒价格变动`;
}

function runtimeConditionRule(condition: RuntimeEntryConditionState): string {
  if (condition.kind === "DECISION_BASIS_READY") return "直接执行计划已提交";
  if (condition.kind === "MARK_PRICE") {
    return `${condition.comparator === "GTE" ? "≥" : "≤"} ${marketPrice(condition.threshold)} USDT`;
  }
  if (condition.kind === "CLOSED_BAR_PRICE_15M") {
    return `${condition.comparator === "GTE" ? "≥" : "≤"} ${marketPrice(condition.threshold)} USDT`;
  }
  if (condition.kind === "SPREAD_BPS") {
    return `≤ ${quoteAmount(condition.threshold)} bps`;
  }
  const comparator = condition.comparator === "GTE"
    ? "上涨 ≥"
    : condition.comparator === "DROP_GTE"
      ? "下跌 ≥"
      : condition.comparator === "ABS_GTE"
        ? "绝对变动 ≥"
        : "有符号变动 ≤";
  return `${comparator} ${quoteAmount(condition.threshold)} bps`;
}

function runtimeConditionCurrentValue(
  condition: RuntimeEntryConditionState,
): string {
  if (condition.currentValue === null) {
    return runtimeConditionPendingPresentation(condition);
  }
  if (condition.kind === "DECISION_BASIS_READY") return condition.currentValue;
  if (condition.kind === "MARK_PRICE") {
    return `${marketPrice(condition.currentValue)} USDT`;
  }
  if (condition.kind === "CLOSED_BAR_PRICE_15M") {
    return `${marketPrice(condition.currentValue)} USDT`;
  }
  if (condition.kind === "PRICE_MOVE_BPS") {
    const move = runtimeSignedMovePresentation(condition.currentValue);
    return `${move.direction} ${basisPoints(move.magnitude)} bps`;
  }
  return `${basisPoints(condition.currentValue)} bps`;
}

function runtimeConditionResultLabel(
  result: RuntimeEntryConditionState["result"],
): string {
  return result === "TRUE" ? "通过" : result === "FALSE" ? "未通过" : "未知";
}

function DirectConditionStatusPanel({
  evaluation,
  executorStatus,
}: {
  evaluation: RuntimeEntryConditionEvaluation;
  executorStatus: RuntimeExecutorConditionStatus | null;
}) {
  if (evaluation.items.length === 0) return null;
  const blockingPresentation = executorStatus?.blockingReason === "DIRECT_POST_ONLY_WOULD_TAKE"
    ? {
        label: "等待 Maker 可挂",
        detail: "固定限价当前会立即成交。系统保持原价格和 Maker only，不会偷偷改为 Taker；盘口允许挂单后自动重试。",
      }
    : executorStatus?.blockingReason === "DIRECT_POST_ONLY_BOOK_UNKNOWN"
      ? {
          label: "等待盘口",
          detail: "买一卖一事实暂不可确认，系统不会盲目提交 Maker only 委托。",
        }
      : executorStatus?.submissionReady === false
        ? {
            label: "等待账户核对",
            detail: "入场条件已通过，但当前账户、持仓、挂单或交易规则事实尚未完成提交前核对；系统会有限重试，核对通过前不会下单。",
          }
      : null;
  const statusLabel = blockingPresentation?.label
    ?? (executorStatus
      ? evaluation.result === "TRUE"
        ? executorStatus.submissionReady === true
          ? "执行器已通过"
          : "执行器条件已通过"
        : evaluation.result === "FALSE"
          ? "执行器等待"
          : "执行器数据不足"
      : evaluation.result === "TRUE"
        ? "页面估算已通过"
        : evaluation.result === "FALSE"
          ? "页面估算等待"
          : "页面数据不足");
  return (
    <Box sx={{ ...surfaceFrameSx, p: 1.5 }}>
      <Stack
        direction="row"
        spacing={1}
        sx={{ justifyContent: "space-between", alignItems: "center" }}
      >
        <Typography sx={{ fontWeight: 750 }}>
          {evaluation.operator === "ALL" ? "入场条件 · 全部需通过" : "入场条件 · 任一通过"}
        </Typography>
        <Tooltip
          arrow
          title={executorStatus
            ? "这里显示 Executor 已持久化的最近一次判断，是解释是否会提交订单的权威状态。"
            : "Executor 尚未形成可读判断；当前仅显示页面实时盘口估算，不授权下单。"}
        >
          <Chip
            size="small"
            variant="outlined"
            color={evaluation.result === "TRUE"
              ? "success"
              : evaluation.result === "FALSE" ? "warning" : "default"}
            label={statusLabel}
          />
        </Tooltip>
      </Stack>
      {blockingPresentation && (
        <Alert severity="info" variant="outlined" sx={{ mt: 1 }}>
          {blockingPresentation.detail}
        </Alert>
      )}
      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.75 }}>
        {executorStatus
          ? `执行器判断 · ${relativeAgeLabel(executorStatus.sourceCutoff, Date.now())}`
          : "页面估算 · 等待执行器首次判断"}
      </Typography>
      <Box
        sx={{
          display: "grid",
          gridTemplateColumns: {
            xs: "1fr",
            sm: "repeat(auto-fit, minmax(9rem, 1fr))",
          },
          gap: 1,
          mt: 1.25,
        }}
      >
        {evaluation.items.map((condition, index) => (
          <Box
            key={`${condition.kind}:${condition.windowSeconds ?? ""}:${index}`}
            sx={{ border: 1, borderColor: "divider", borderRadius: 1.5, p: 1.25 }}
          >
            <Stack direction="row" spacing={1} sx={{ justifyContent: "space-between", alignItems: "center" }}>
              <Typography variant="caption" color="text.secondary">
                {runtimeConditionLabel(condition)}
              </Typography>
              <Chip
                size="small"
                color={condition.result === "TRUE"
                  ? "success"
                  : condition.result === "FALSE" ? "warning" : "default"}
                label={runtimeConditionResultLabel(condition.result)}
              />
            </Stack>
            <Typography sx={{ fontWeight: 750, mt: .75 }}>
              {runtimeConditionRule(condition)}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              当前：{runtimeConditionCurrentValue(condition)}
            </Typography>
          </Box>
        ))}
      </Box>
    </Box>
  );
}

function orderProtectionSummary(value: unknown): string {
  const spec = orderScheduleSpecOf(value);
  const protection = recordOf(spec.protection_policy);
  if (Object.keys(protection).length === 0) return "保护配置不可读";
  const stop = recordOf(protection.initial_stop);
  const takeProfit = recordOf(protection.take_profit_ladder);
  const levels = recordsOf(takeProfit.levels);
  const stopDistance = valueOf(stop, "distance_bps", "");
  const timeExitSeconds = finiteNumber(protection.time_exit_seconds);
  return [
    stopDistance ? `止损 ${quoteAmount(stopDistance)} bps` : "",
    levels.length > 0 ? `${levels.length} 档止盈` : "",
    timeExitSeconds !== null ? `${Math.round(timeExitSeconds)} 秒时间退出` : "",
  ].filter(Boolean).join(" · ") || "未配置保护";
}

const actionProfileLabels: Record<string, string> = {
  ENTRY_MARKET: "市价入场",
  ENTRY_LIMIT: "限价入场",
  PROTECTIVE_STOP_REDUCE_ONLY: "只减仓保护止损",
  TAKE_PROFIT_1: "第一档止盈",
  TAKE_PROFIT_2: "第二档止盈",
  CANCEL_ORDER: "撤单",
  REDUCE_OR_CLOSE_MARKET: "市价减仓或退出",
};

function orderDynamicSummary(value: unknown): string {
  const spec = orderScheduleSpecOf(value);
  const rules = recordsOf(spec.dynamic_rules);
  if (rules.length === 0) return "未启用动态入场管理";
  return rules.map((rule) => {
    const kind = valueOf(rule, "kind", "");
    if (kind === "EXPIRE_REMAINING") {
      return `首档真实提交后 ${valueOf(rule, "after_seconds")} 秒终止并撤销剩余入场`;
    }
    if (kind === "CANCEL_ON_SHOCK") {
      const invalidationPrice = valueOf(rule, "invalidation_price", "");
      const opportunityMissedPrice = valueOf(
        rule,
        "opportunity_missed_price",
        "",
      );
      const windowSeconds = valueOf(rule, "window_seconds", "");
      const adverseMoveBps = valueOf(rule, "adverse_move_bps", "");
      const cancellationPaths = [
        invalidationPrice
          ? `标记价达到失效边界 ${quoteAmount(invalidationPrice)} 时永久取消未成交入场`
          : "",
        opportunityMissedPrice
          ? `标记价达到机会错过边界 ${quoteAmount(opportunityMissedPrice)} 时永久取消未成交入场`
          : "",
        windowSeconds && adverseMoveBps
          ? `${windowSeconds} 秒内不利变动 ${quoteAmount(adverseMoveBps)} bps：事实未知时暂停并撤开放档，首次触发后终止剩余档`
          : "",
      ].filter(Boolean);
      return cancellationPaths.join("；") || "行情失效时取消未成交入场";
    }
    if (kind === "REPRICE_ENTRY") {
      return [
        `偏离 ${valueOf(rule, "trigger_distance_bps")} bps 后跟随同侧盘口`,
        `盘口外留 ${valueOf(rule, "book_offset_bps")} bps`,
        `最多 ${valueOf(rule, "max_adjustments")} 次`,
        `总移动不超过 ${valueOf(rule, "maximum_total_move_bps")} bps`,
      ].join(" · ");
    }
    return kind || "未知动态规则";
  }).join("；");
}

function orderedCompiledLegs(snapshot: unknown): Array<Record<string, unknown>> {
  const schedule = recordOf(snapshot);
  const spec = orderScheduleSpecOf(schedule);
  const legs = recordsOf(schedule.normalized_legs);
  return valueOf(spec, "submission_order", "LOW_TO_HIGH") === "HIGH_TO_LOW"
    ? [...legs].reverse()
    : legs;
}

function scheduleSubmissionSummary(value: unknown): string {
  const spec = orderScheduleSpecOf(value);
  const mode = valueOf(spec, "submission_mode", "SERIAL_PROTECTED") === "SERIAL_PROTECTED"
    ? "串行保护"
    : valueOf(spec, "submission_mode");
  const order = valueOf(spec, "submission_order", "LOW_TO_HIGH") === "HIGH_TO_LOW"
    ? "高价 → 低价"
    : "低价 → 高价";
  return `${mode} · ${order}`;
}

function TradingViewAttribution() {
  return (
    <Typography variant="caption" color="text.secondary" sx={{ display: "block", fontSize: 10 }}>
      TradingView Lightweight Charts™ · Copyright © 2025 TradingView, Inc. ·{" "}
      <Box component="a" href="https://www.tradingview.com/" target="_blank" rel="noreferrer" sx={{ color: "inherit" }}>TradingView</Box>
    </Typography>
  );
}

function ClampedTooltipText({ text, lines = 2 }: { text: string; lines?: number }) {
  const textRef = useRef<HTMLSpanElement | null>(null);
  const [truncated, setTruncated] = useState(false);
  const updateTruncation = useCallback(() => {
    const element = textRef.current;
    if (!element) return;
    setTruncated(element.scrollHeight > element.clientHeight + 1 || element.scrollWidth > element.clientWidth + 1);
  }, []);

  useLayoutEffect(() => {
    updateTruncation();
    const element = textRef.current;
    if (!element || typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(updateTruncation);
    observer.observe(element);
    return () => observer.disconnect();
  }, [text, lines, updateTruncation]);

  return (
    <Tooltip
      title={text}
      placement="top"
      arrow
      disableHoverListener={!truncated}
      disableFocusListener={!truncated}
      disableTouchListener={!truncated}
      slotProps={{ tooltip: { sx: { maxWidth: 440, fontSize: 13, lineHeight: 1.55 } } }}
    >
      <Typography
        ref={textRef}
        component="span"
        tabIndex={truncated ? 0 : undefined}
        variant="caption"
        color="text.secondary"
        sx={{
          display: "-webkit-box",
          WebkitBoxOrient: "vertical",
          WebkitLineClamp: lines,
          maxHeight: `${lines * 1.45}em`,
          overflow: "hidden",
          lineHeight: 1.45,
          overflowWrap: "anywhere",
          cursor: truncated ? "help" : "inherit",
          outlineOffset: 2,
        }}
      >
        {text}
      </Typography>
    </Tooltip>
  );
}

function ExpandableList<T>({
  items,
  renderItem,
  initialCount = 8,
  step = 8,
  spacing = 1.5,
}: {
  items: T[];
  renderItem: (item: T, index: number) => ReactNode;
  initialCount?: number;
  step?: number;
  spacing?: number;
}) {
  const [visibleCount, setVisibleCount] = useState(initialCount);
  useEffect(() => { setVisibleCount(initialCount); }, [initialCount, items.length]);
  const remaining = Math.max(0, items.length - visibleCount);
  return (
    <>
      <Stack spacing={spacing}>{items.slice(0, visibleCount).map(renderItem)}</Stack>
      {remaining > 0 && (
        <Button variant="text" sx={{ mt: 1.5 }} onClick={() => setVisibleCount((count) => count + step)}>
          显示更多（剩余 {remaining} 条）
        </Button>
      )}
    </>
  );
}

function signedUsdt(value: unknown): string {
  const amount = finiteNumber(value);
  if (amount === null) return "未知";
  const normalized = Math.abs(amount) < 0.0000005 ? 0 : amount;
  return `${new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
    signDisplay: "exceptZero",
  }).format(normalized)} USDT`;
}

function signedSettledUsdt(value: unknown): string {
  const amount = finiteNumber(value);
  if (amount === null) return "未知";
  const normalized = Math.abs(amount) < 0.000000005 ? 0 : amount;
  const absolute = quoteCurrencyAmount(String(Math.abs(normalized)));
  return `${normalized > 0 ? "+" : normalized < 0 ? "-" : ""}${absolute} USDT`;
}

function PlanPnlPanel({
  activation,
  activationDetail,
  environmentKind,
  environmentScope,
  marketColorScheme,
  positionDisposition = false,
}: {
  activation: ActivationSummary;
  activationDetail?: ActivationDetail;
  environmentKind: string;
  environmentScope: string;
  marketColorScheme: MarketColorScheme;
  positionDisposition?: boolean;
}) {
  const startedAt = activation.created_at ?? "";
  const startMs = Date.parse(startedAt);
  const completed = activation.lifecycle === "COMPLETED";
  const endedAt = completed ? activation.updated_at : null;
  const endMs = completed ? Date.parse(endedAt ?? "") : Date.now();
  const interval = defaultExecutionWindowInterval(startMs, endMs);
  const summaryTradeResult = recordOf(activation.trade_result);
  const hasAttributedFill = activation.has_entry_fill
    || recordsOf(summaryTradeResult.fills).length > 0;
  const expectedMarketSource = expectedMarketSourceForEnvironment(environmentKind);
  const detailQuery = useQuery({
    queryKey: ["plan-card-activation", environmentScope, activation.activation_id],
    queryFn: () => getActivation(activation.activation_id),
    enabled: hasAttributedFill && !positionDisposition && !activationDetail,
    refetchInterval: completed ? false : 30_000,
    staleTime: completed ? 5 * 60_000 : 15_000,
  });
  const marketWindowQuery = useQuery({
    queryKey: [
      "plan-card-pnl-window",
      environmentScope,
      activation.activation_id,
      activation.instrument_ref,
      interval,
      startedAt,
      endedAt,
    ],
    queryFn: () => getMarketWindow(
      activation.instrument_ref,
      startedAt,
      endedAt ?? new Date().toISOString(),
      interval,
      "EXECUTION_REVIEW",
    ),
    enabled: Boolean(
      hasAttributedFill
      && !positionDisposition
      && expectedMarketSource
      && Number.isFinite(startMs)
      && Number.isFinite(endMs),
    ),
    refetchInterval: completed ? false : 30_000,
    staleTime: completed ? 30 * 60_000 : 15_000,
  });
  const sourceMismatch = Boolean(
    marketWindowQuery.data
    && !isMarketSourceForEnvironment(marketWindowQuery.data.source, environmentKind),
  );
  const marketWindow = sourceMismatch ? undefined : marketWindowQuery.data;
  const detail = activationDetail ?? detailQuery.data;
  const tradeResult = recordOf(detail?.trade_result);
  const authoritativeNetPnl = completed
    && !positionDisposition
    && tradeResult.calculation_complete === true
    && tradeResult.closed === true
    ? finiteNumber(tradeResult.net_pnl)
    : null;
  const points = useMemo(() => {
    return buildPlanPnlTrend({
      bars: marketWindow?.bars ? Array.from(marketWindow.bars) : [],
      direction: activation.direction,
      fills: recordsOf(tradeResult.fills),
      fundingFacts: recordsOf(detail?.venue_facts),
      settledAt: typeof tradeResult.last_fill_time === "string"
        ? tradeResult.last_fill_time
        : endedAt ?? undefined,
      settledNetPnl: authoritativeNetPnl ?? undefined,
      sourceCutoff: marketWindow?.source_cutoff ?? "",
      startedAt,
    });
  }, [
    activation.direction,
    authoritativeNetPnl,
    detail?.trade_result,
    detail?.venue_facts,
    endedAt,
    marketWindow,
    startedAt,
  ]);
  const displayedPnl = completed
    ? authoritativeNetPnl
    : points.at(-1)?.value;
  const settlementPending = completed && authoritativeNetPnl === null;
  const costAccountingComplete = (
    tradeResult.commission_complete === true
    && tradeResult.funding_complete === true
    && tradeResult.funding_included === true
  );
  const pnlExplanation = positionDisposition
    ? "该计划处置的是账户既有外部持仓，不把账户盈亏归属为本计划收益。"
    : completed
    ? settlementPending
      ? "本计划归属的交易所结算事实尚未完整核对，因此不显示历史盈亏曲线和最终值。"
      : "曲线中间过程按本计划归属成交、手续费、已归属资金费和同期行情重算；末点直接采用本计划交易所事实结算净盈亏。"
    : costAccountingComplete
      ? "已计入本计划归属成交、全部已确认手续费和已发生资金费；未平仓部分按最新行情估值，不含未来退出滑点。"
      : "已计入当前已归属成交、已取得手续费和资金费；部分费用记录尚未确认完整，未平仓部分按最新行情估值。";
  const loading = hasAttributedFill
    && ((!activationDetail && detailQuery.isPending) || marketWindowQuery.isPending);
  const unavailable = hasAttributedFill
    && ((!activationDetail && detailQuery.isError) || marketWindowQuery.isError || sourceMismatch);

  return (
    <Box
      aria-label={completed ? "历史费用后盈亏走势" : "费用后盈亏估算走势"}
      sx={{
        minWidth: 0,
        borderTop: { xs: 1, md: 0 },
        borderLeft: { xs: 0, md: 1 },
        borderColor: "divider",
        pt: { xs: 1.25, md: 0 },
        pl: { xs: 0, md: 2 },
      }}
    >
      <Stack direction="row" spacing={1} sx={{ alignItems: "baseline", justifyContent: "space-between", mb: 0.25 }}>
        <Tooltip arrow title={pnlExplanation}>
          <Typography
            variant="body2"
            sx={{ fontWeight: 700, cursor: "help", textDecoration: "underline dotted", textUnderlineOffset: 2 }}
          >
            {completed ? "历史费用后盈亏" : "费用后盈亏估算"}
          </Typography>
        </Tooltip>
        {displayedPnl !== undefined && displayedPnl !== null && (
          <Typography className="mono" variant="body2" sx={{ fontWeight: 750 }}>
            <FinancialToneText tone={financialToneForSignedValue(displayedPnl)}>
              {completed ? signedSettledUsdt(tradeResult.net_pnl) : signedUsdt(displayedPnl)}
            </FinancialToneText>
          </Typography>
        )}
      </Stack>
      {positionDisposition
        ? <Typography variant="body2" color="text.secondary" sx={{ py: 5, textAlign: "center" }}>外部持仓处置不归属本计划盈亏</Typography>
        : loading
        ? <LinearProgress aria-label="正在读取费用后盈亏估算走势" sx={{ my: 6 }} />
        : settlementPending
          ? <Typography variant="body2" color="text.secondary" sx={{ py: 5, textAlign: "center" }}>最终结算待核对，暂不显示历史盈亏曲线</Typography>
        : unavailable
          ? <Typography variant="body2" color="text.secondary" sx={{ py: 5, textAlign: "center" }}>盈亏曲线暂不可用</Typography>
          : points.length < 2
            ? <Typography variant="body2" color="text.secondary" sx={{ py: 5, textAlign: "center" }}>{completed ? "未形成交易，无盈亏曲线" : "尚未成交，无持仓盈亏"}</Typography>
            : (
              <Suspense fallback={<LinearProgress aria-label="正在绘制费用后盈亏估算曲线" sx={{ my: 6 }} />}>
                <PlanPnlChart points={points} marketColorScheme={marketColorScheme} />
              </Suspense>
            )}
      {points.length >= 2 && marketWindow && (
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.25 }}>
          {formatCompactUserVisibleTime(startedAt)} → {formatCompactUserVisibleTime(endedAt ?? marketWindow.source_cutoff)}
        </Typography>
      )}
    </Box>
  );
}

function EmptyHistoricalPnlPanel() {
  return (
    <Box
      aria-label="历史费用后盈亏走势"
      sx={{
        minWidth: 0,
        borderTop: { xs: 1, md: 0 },
        borderLeft: { xs: 0, md: 1 },
        borderColor: "divider",
        pt: { xs: 1.25, md: 0 },
        pl: { xs: 0, md: 2 },
      }}
    >
      <Typography variant="body2" sx={{ fontWeight: 700 }}>历史费用后盈亏</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ py: 5, textAlign: "center" }}>
        计划未运行，无盈亏曲线
      </Typography>
    </Box>
  );
}

function usdt(value: unknown): string {
  const amount = finiteNumber(value);
  if (amount === null) return "未知";
  return `${new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(amount)} USDT`;
}

function planDurationMinutes(validFrom: string, validUntil: string): string {
  const minutes = Math.round((Date.parse(validUntil) - Date.parse(validFrom)) / 60_000);
  return Number.isFinite(minutes) && minutes > 0 ? `${minutes} 分钟` : "未知";
}

function formatPlanKeyParameter(
  definition: PlanKeyParameterDefinition,
  value: unknown,
): string {
  if (definition.display_format === "BOOLEAN_LABEL") {
    if (value === true) return definition.true_label ?? "是";
    if (value === false) return definition.false_label ?? "否";
    return "未配置";
  }
  if (definition.display_format === "PERCENT") {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? percent(numeric * 100) : "未配置";
  }
  if (value === null || value === undefined || value === "") return "未配置";
  const rendered = String(value);
  return definition.unit ? `${rendered} ${definition.unit}` : rendered;
}

function durationText(value: unknown): string {
  const seconds = finiteNumber(value);
  if (seconds === null || seconds < 0) return "未知";
  const whole = Math.round(seconds);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const remainder = whole % 60;
  return [hours ? `${hours} 小时` : "", minutes ? `${minutes} 分钟` : "", `${remainder} 秒`].filter(Boolean).join(" ");
}

function liquidityText(value: unknown): string {
  const token = String(value ?? "").toUpperCase();
  if (token === "1" || token.includes("MAKER")) return "挂单成交（Maker）";
  if (token === "2" || token.includes("TAKER")) return "吃单成交（Taker）";
  return "未知";
}

function reviewConclusion(review: Record<string, unknown>): string {
  if (valueOf(review, "status") === "DRAFT") return "PENDING";
  const owner = recordOf(recordOf(review.evaluations).owner_conclusion);
  return valueOf(owner, "result", "UNKNOWN");
}

function percent(value: number): string {
  return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(value)}%`;
}

const reviewResultLabels: Record<string, string> = {
  NO_ACTION: "未发生交易",
  COMPLETED: "已完成交易",
  PARTIAL: "部分完成",
  RESULT_UNKNOWN: "结果未知",
  HANDED_OVER: "已由用户接管",
};

const reviewStatusLabels: Record<string, string> = {
  DRAFT: "待评价",
  COMPLETE: "已完成",
};

const reviewRevisionReasonLabels: Record<string, string> = {
  INITIAL_DERIVATION: "首次形成",
  AUTHORITATIVE_FACTS_CHANGED: "权威事实变化",
  OWNER_EVALUATION_CHANGED: "用户评价变化",
};

const planRuntimeIncompatibilityLabels: Record<string, string> = {
  PLAN_ORDER_SCHEDULE_RUNTIME_INCOMPATIBLE: "订单计划规则已不受当前运行时支持",
  PLAN_STRATEGY_RUNTIME_UNAVAILABLE: "固定策略当前不可用",
  PLAN_STRATEGY_RUNTIME_INCOMPATIBLE: "固定策略实现与当前运行时不一致",
  PLAN_STRATEGY_PARAMETERS_CORRUPT: "固定策略参数摘要无法核对",
  PLAN_STRATEGY_PARAMETERS_INCOMPATIBLE: "固定策略参数已不受当前实现支持",
  PLAN_FIXED_CONTENT_UNREADABLE: "运行快照内容无法完整读取",
};

const protectionStateLabels: Record<string, string> = {
  NONE: "未入场",
  WORKING: "完整有效",
  UNKNOWN: "未知",
  GAP: "存在缺口",
  CLOSED: "已闭合",
};

const evaluationResultLabels: Record<string, string> = {
  PENDING: "待评价",
  USABLE_SAMPLE: "可用交易样本",
  TRADE_DECISION_ISSUE: "交易决策需改进",
  TOOLING_ISSUE: "工具问题影响",
  VALIDATION_TRADE: "验证性交易",
  NO_TRADE: "未形成交易",
  INSUFFICIENT_EVIDENCE: "证据不足",
  AS_EXPECTED: "旧分类：符合预期",
  ISSUE_FOUND: "旧分类：问题未分类",
  UNKNOWN: "证据不足（历史结论）",
  NOT_APPLICABLE: "旧分类：不适用",
};

const planDecisionIntentLabels: Record<string, string> = {
  PROFIT_SEEKING: "盈利导向交易",
  VALIDATION: "机制 / 软件验证",
};

const planSetupFamilyLabels: Record<string, string> = {
  BREAKOUT_CONTINUATION: "突破延续",
  PULLBACK_CONTINUATION: "回调延续",
  RANGE_MEAN_REVERSION: "区间均值回归",
  REVERSAL: "趋势反转",
  EVENT_DRIVEN: "事件驱动",
  OTHER: "其他",
};

type ReviewClassificationValue = ReviewCompletionPayload["conclusion"];

type ReviewClassificationOption = {
  value: ReviewClassificationValue;
  label: string;
  definition: string;
  consumption: string;
  reasonRequired: boolean;
};

const reviewClassificationOptions: ReviewClassificationOption[] = [
  {
    value: "USABLE_SAMPLE",
    label: "可用交易样本",
    definition: "结果可归因，且没有工具问题实质改变本次交易；盈利和亏损都可以属于此类。",
    consumption: "策略、执行质量、成本和盈亏统计的可比较样本。",
    reasonRequired: false,
  },
  {
    value: "TRADE_DECISION_ISSUE",
    label: "交易决策需改进",
    definition: "主要问题来自判断、入场、退出、仓位、风险或纪律，而不是工具故障。",
    consumption: "交易规则与决策流程的定向改进。",
    reasonRequired: true,
  },
  {
    value: "TOOLING_ISSUE",
    label: "工具问题影响",
    definition: "软件缺陷、能力缺口、数据或交互问题实质影响了执行、结果或复盘判断。",
    consumption: "产品修复、能力补齐与回归验证的需求输入。",
    reasonRequired: true,
  },
  {
    value: "VALIDATION_TRADE",
    label: "验证性交易",
    definition: "已形成交易，但主要目的是验证软件、机制或执行闭环，不是按冻结交易规则获取市场收益。",
    consumption: "产品与执行闭环验证；保留实际盈亏，但不进入策略胜率和收益期望。",
    reasonRequired: true,
  },
  {
    value: "NO_TRADE",
    label: "未形成交易",
    definition: "计划没有形成可归属成交或持仓，且工具问题不是主要原因。",
    consumption: "入场选择、取消、过期和错失机会分析。",
    reasonRequired: false,
  },
  {
    value: "INSUFFICIENT_EVIDENCE",
    label: "证据不足",
    definition: "评价窗口已经结束，但关键证据永久缺失或相互冲突，无法可靠判断。",
    consumption: "数据质量与事实链修复；不进入策略有效性结论。",
    reasonRequired: true,
  },
];

const reviewClassificationFilterValues = [
  "PENDING",
  ...reviewClassificationOptions.map((item) => item.value),
  "AS_EXPECTED",
  "ISSUE_FOUND",
  "UNKNOWN",
  "NOT_APPLICABLE",
];

type ReviewPnlFilter = "ALL" | "PROFIT" | "LOSS" | "BREAKEVEN" | "UNKNOWN" | "NOT_APPLICABLE";

type ReviewListFilters = {
  strategyId: string;
  instrumentRef: string;
  direction: string;
  pnl: ReviewPnlFilter;
  primaryResult: string;
  ownerConclusion: string;
};

const emptyReviewListFilters: ReviewListFilters = {
  strategyId: "ALL",
  instrumentRef: "ALL",
  direction: "ALL",
  pnl: "ALL",
  primaryResult: "ALL",
  ownerConclusion: "ALL",
};

const pnlFilterLabels: Record<ReviewPnlFilter, string> = {
  ALL: "全部盈亏",
  PROFIT: "盈利",
  LOSS: "亏损",
  BREAKEVEN: "持平",
  UNKNOWN: "盈亏未知",
  NOT_APPLICABLE: "不适用（未交易）",
};

function tradeResultForReview(review: Record<string, unknown>): Record<string, unknown> {
  return recordOf(review.resolved_trade_result);
}

function reviewClosedAt(review: Record<string, unknown>): string {
  const result = tradeResultForReview(review);
  const context = recordOf(review.trade_context);
  return valueOf(
    result,
    "last_fill_time",
    valueOf(context, "activation_updated_at", valueOf(review, "fact_cutoff", "")),
  );
}

function reviewPnlClass(review: Record<string, unknown>): Exclude<ReviewPnlFilter, "ALL"> {
  if (valueOf(review, "primary_result", "") === "NO_ACTION") return "NOT_APPLICABLE";
  const result = tradeResultForReview(review);
  const netPnl = finiteNumber(result.net_pnl);
  if (result.calculation_complete !== true || result.closed !== true || netPnl === null) return "UNKNOWN";
  if (netPnl > 0) return "PROFIT";
  if (netPnl < 0) return "LOSS";
  return "BREAKEVEN";
}

function reviewMatchesFilters(review: Record<string, unknown>, filters: ReviewListFilters): boolean {
  const context = recordOf(review.trade_context);
  return (
    (filters.strategyId === "ALL" || reviewDecisionBasisKind(context) === filters.strategyId)
    && (filters.instrumentRef === "ALL" || valueOf(context, "instrument_ref", "") === filters.instrumentRef)
    && (filters.direction === "ALL" || valueOf(context, "direction", "") === filters.direction)
    && (filters.pnl === "ALL" || reviewPnlClass(review) === filters.pnl)
    && (filters.primaryResult === "ALL" || valueOf(review, "primary_result", "") === filters.primaryResult)
    && (filters.ownerConclusion === "ALL" || reviewConclusion(review) === filters.ownerConclusion)
  );
}

const gateStateLabels: Record<string, string> = {
  OPEN: "已开启",
  CLOSED: "已关闭",
};

const executorStatusLabels: Record<string, string> = {
  READY: "已就绪",
  STARTING: "正在启动与核对",
  UNAVAILABLE: "未运行",
  BUILD_MISMATCH: "产品版本不一致",
  AMBIGUOUS: "存在多个执行器",
  UNKNOWN: "无法核对",
};

const lifecycleLabels: Record<string, string> = {
  RUNNING: "计划运行中",
  EXITING: "正在退出",
  USER_TAKEOVER: "用户已接管",
  COMPLETED: "已闭合",
  UNKNOWN: "未知",
};

const runStateLabels: Record<string, string> = {
  ACTIVE: "执行正常",
  PAUSED: "新增入场暂停",
};

const pauseReasonLabels: Record<string, string> = {
  WRITER_CONTINUITY_LOST: "执行连续性中断",
};

function activationEntryPhaseClosed(activationValue: unknown, nowMs: number): boolean {
  const activation = recordOf(activationValue);
  if (activation.entry_opportunity_consumed === true) return true;
  const ruleState = recordOf(activation.rule_state);
  const deadlines = recordOf(ruleState.deadlines);
  const entryValidUntil = valueOf(deadlines, "entry_valid_until", "");
  const entryDeadlineMs = Date.parse(entryValidUntil);
  return Number.isFinite(entryDeadlineMs) && entryDeadlineMs <= nowMs;
}

const systemStopReleaseDenialLabels: Record<string, string> = {
  ACCOUNT_SYSTEM_STOP_NOT_ACTIVE: "当前没有可释放的账户级系统停止。",
  USER_TAKEOVER_CLOSURE_REQUIRED: "请先完成用户接管，并等待服务端以账户空仓、无开放委托事实闭合本次激活。",
  SYSTEM_STOP_RELEASE_CLOSURE_PREDATES_STOP: "当前闭合证据早于系统停止，不能用于释放。",
  SYSTEM_STOP_RELEASE_ACCOUNT_SNAPSHOT_MISSING: "尚未收到接管后的账户级闭合快照。",
  SYSTEM_STOP_RELEASE_ACCOUNT_SNAPSHOT_INVALID: "账户快照缺少全账户持仓或普通/条件委托证据。",
  SYSTEM_STOP_RELEASE_ACCOUNT_NOT_FLAT: "账户仍有持仓，不能释放新增风险停止。",
  SYSTEM_STOP_RELEASE_OPEN_ORDERS_REMAIN: "账户仍有普通或条件委托，不能释放新增风险停止。",
  SYSTEM_STOP_RELEASE_ACCOUNT_ACTIVATIONS_OPEN:
    "账户仍有未完成计划；释放会使其隐式恢复新增风险，因此已阻止。",
  SYSTEM_STOP_RELEASE_ACCOUNT_ACTIONS_OPEN:
    "账户仍有未闭合执行责任，不能释放。",
  SYSTEM_STOP_RELEASE_NEW_UNCLAIMED_FACT: "闭合快照之后出现新的未归属账户事实，必须重新核对。",
  SYSTEM_STOP_RELEASE_EVIDENCE_STALE: "账户闭合快照已过期，请由 Executor 重新产生权威核对事实。",
  SYSTEM_STOP_VERSION_CONFLICT: "系统停止版本已变化，请刷新后重新核对。",
};

const systemStopSourceLabels: Record<string, string> = {
  SYSTEM_EXTERNAL_ACTIVITY: "检测到未归属的账户外部活动",
  SYSTEM_ATTRIBUTED_ACTION_ANOMALY: "检测到已归属动作异常",
};

const actionKindLabels: Record<string, string> = {
  ENTRY: "入场",
  CANCEL: "撤单",
  PROTECTION: "保护",
  TAKE_PROFIT: "止盈",
  RISK_REDUCTION: "减仓",
  EXIT: "退出",
  EXTERNAL_ACCOUNT_CLOSURE: "外部应急平仓",
};

const actionStateLabels: Record<string, string> = {
  READY: "待提交",
  NOT_SUBMITTED: "未提交",
  SUBMITTING: "正在提交",
  UNKNOWN: "结果未决",
  OPEN: "责任开放",
  CLOSED: "已核对闭合",
  HANDED_OVER: "已交接",
};

const timelineSourceLabels: Record<string, string> = {
  ACTIVATION: "计划事件",
  PLAN_EVENT: "计划事件",
  EXECUTION_ACTION: "执行动作",
  VENUE_FACT: "交易所事实",
  CONTROL_COMMAND: "控制命令",
};

const venueOrderStateLabels: Record<string, string> = {
  WORKING: "工作中",
  PARTIALLY_FILLED: "部分成交",
  FILLED: "全部成交",
  CANCELED: "已撤销",
  CANCELLED: "已撤销",
  REJECTED: "已拒绝",
  EXPIRED: "已过期",
  UNKNOWN: "未知",
};

const directionLabels: Record<string, string> = {
  LONG: "做多",
  SHORT: "做空",
};

function translatedLabel(labels: Record<string, string>, value: string): string {
  return labels[value] ?? value;
}

type VenueFactPresentation = {
  headline: string;
  detail: string;
};

function venueFactPresentation(
  fact: Record<string, unknown>,
  action: Record<string, unknown> | undefined,
  _repeatCount = 1,
): VenueFactPresentation {
  const kind = valueOf(fact, "kind", "");
  const payload = recordOf(fact.payload);
  const actionKind = valueOf(action, "action_kind", "");
  const actionLabel = actionKind
    ? translatedLabel(actionKindLabels, actionKind)
    : "未归属";
  const actionState = valueOf(action, "state", "");
  const terms = recordOf(action?.action_terms);
  const sourceClass = valueOf(fact, "source_class", "");
  const sourceLabel = sourceClass === "VENUE_QUERY"
    ? "交易所主动核对"
    : sourceClass === "VENUE_STREAM"
      ? "交易所实时回报"
      : "系统归属事实";
  if (kind === "ORDER_STATE") {
    const status = valueOf(payload, "status", "");
    const eventType = valueOf(payload, "event_type", "");
    const statusLabel = translatedLabel(venueOrderStateLabels, status);
    const quantity = valueOf(payload, "venue_order_quantity", valueOf(terms, "quantity", ""));
    const triggerPrice = valueOf(terms, "trigger_price", "");
    const reason = venueReasonText(valueOf(payload, "reason", ""));
    const closedResponsibility = ["CLOSED", "HANDED_OVER"].includes(actionState);
    const headline = sourceClass === "VENUE_QUERY"
      ? closedResponsibility
        ? `${actionLabel}订单查询回执（当前责任已闭合）`
        : status === "WORKING"
          ? `${actionLabel}订单仍在交易所工作`
          : `${actionLabel}订单核对为${statusLabel}`
      : status === "WORKING"
        ? eventType === "OrderUpdated"
          ? `${actionLabel}订单仍在交易所工作`
          : `${actionLabel}订单已被交易所接受`
        : `${actionLabel}订单状态变为${statusLabel}`;
    const closedNote = sourceClass === "VENUE_QUERY"
      && closedResponsibility
      && status === "WORKING"
      ? "；查询曾返回工作中，当前以成交与动作闭合结论为准"
      : "";
    return {
      headline,
      detail: [
        statusLabel,
        quantity ? `数量 ${marketVolume(quantity)}` : "",
        triggerPrice ? `触发价 ${marketPrice(triggerPrice)} USDT` : "",
        action?.action_terms && terms.reduce_only === true ? "只减仓" : "",
        sourceLabel,
        reason,
      ].filter(Boolean).join(" · ") + closedNote,
    };
  }
  if (kind === "FILL") {
    const quantity = valueOf(payload, "last_quantity", "");
    const price = valueOf(payload, "last_price", "");
    const side = valueOf(payload, "order_side", "") === "SELL" ? "卖出" : "买入";
    const liquidity = valueOf(payload, "liquidity_side", "");
    return {
      headline: `${actionLabel}${side}成交`,
      detail: [
        quantity ? `${marketVolume(quantity)} BTC` : "",
        price ? `@ ${marketPrice(price)} USDT` : "",
        liquidity === "MAKER" ? "Maker" : liquidity === "TAKER" ? "Taker" : "",
        valueOf(payload, "trade_id", "") ? `成交号 ${shortDigest(valueOf(payload, "trade_id"))}` : "",
      ].filter(Boolean).join(" · "),
    };
  }
  if (kind === "COMMISSION") {
    const amount = valueOf(payload, "amount", "");
    const currency = valueOf(payload, "currency", "");
    const amountParts = amount.trim().split(/\s+/, 2);
    const numericAmount = amountParts[0] ?? "";
    const amountCurrency = amountParts[1] || currency;
    return {
      headline: `${actionLabel}手续费已确认`,
      detail: [
        numericAmount ? `${quoteAmount(numericAmount)}${amountCurrency ? ` ${amountCurrency}` : ""}` : "",
        valueOf(payload, "trade_id", "") ? `对应成交 ${shortDigest(valueOf(payload, "trade_id"))}` : "",
        sourceLabel,
      ].filter(Boolean).join(" · "),
    };
  }
  if (kind === "FUNDING") {
    const income = valueOf(payload, "income", "");
    const currency = valueOf(payload, "currency", "");
    return {
      headline: "资金费已归属本计划",
      detail: [
        income ? `${quoteAmount(income)}${currency ? ` ${currency}` : ""}` : "金额未知",
        valueOf(payload, "transaction_id", "") ? `交易号 ${shortDigest(valueOf(payload, "transaction_id"))}` : "",
      ].filter(Boolean).join(" · "),
    };
  }
  if (kind === "POSITION_STATE") {
    const planQuantity = valueOf(payload, "activation_position_quantity", "");
    const venueQuantity = valueOf(payload, "position_quantity", "");
    const attributedQuantity = valueOf(payload, "attributed_account_position_quantity", "");
    return {
      headline: "计划持仓与交易所合并仓位已核对",
      detail: [
        planQuantity ? `本计划 ${marketVolume(planQuantity)} BTC` : "",
        attributedQuantity ? `全部计划合计 ${marketVolume(attributedQuantity)} BTC` : "",
        venueQuantity ? `交易所 ${marketVolume(venueQuantity)} BTC` : "",
      ].filter(Boolean).join(" · ") || "核对数量未知",
    };
  }
  return {
    headline: "交易所责任事实已记录",
    detail: [kind || "类型未知", sourceLabel].join(" · "),
  };
}

function actionTimelinePresentation(
  action: Record<string, unknown>,
  orderSchedule: Record<string, unknown>,
): VenueFactPresentation {
  const actionKind = valueOf(action, "action_kind", "");
  const state = valueOf(action, "state", "");
  const terms = recordOf(action.action_terms);
  const dynamicCancel = runtimeDynamicCancelPresentation(action, orderSchedule);
  if (dynamicCancel) return dynamicCancel;
  const notSubmittedEntry = runtimeNotSubmittedEntryPresentation(
    action,
    orderSchedule,
  );
  if (notSubmittedEntry) return notSubmittedEntry;
  const quantity = valueOf(terms, "quantity", "");
  const triggerPrice = valueOf(terms, "trigger_price", "");
  const price = valueOf(terms, "price", "");
  const stateSuffix: Record<string, string> = {
    READY: "动作待提交",
    NOT_SUBMITTED: "动作未提交",
    SUBMITTING: "订单正在提交",
    UNKNOWN: "结果未决",
    OPEN: "责任开放",
    CLOSED: "责任已核对闭合",
    HANDED_OVER: "责任已交接",
  };
  return {
    headline: `${translatedLabel(actionKindLabels, actionKind)}${stateSuffix[state] ?? translatedLabel(actionStateLabels, state)}`,
    detail: [
      quantity ? `数量 ${marketVolume(quantity)} BTC` : "",
      triggerPrice ? `触发价 ${marketPrice(triggerPrice)} USDT` : price ? `价格 ${marketPrice(price)} USDT` : "",
      terms.reduce_only === true ? "只减仓" : "",
      state === "UNKNOWN"
        ? unknownExecutionReasonText(valueOf(action, "unknown_reason", ""))
        : "",
    ].filter(Boolean).join(" · ") || "无额外交易所条款",
  };
}

function runtimeTimelinePresentation(
  entry: CompactRuntimeTimelineItem,
  actionsByRef: Map<string, Record<string, unknown>>,
  facts: Array<Record<string, unknown>>,
  orderSchedule: Record<string, unknown>,
  priorBlockingReason = "",
): VenueFactPresentation {
  const detail = recordOf(entry.item.detail);
  const source = valueOf(entry.item, "source");
  if (source === "ACTIVATION") {
    return {
      headline: "计划已启动",
      detail: "已进入条件等待与执行流程",
    };
  }
  if (source === "VENUE_FACT" && entry.fact) {
    const action = actionsByRef.get(valueOf(entry.fact, "action_ref", ""));
    return venueFactPresentation(entry.fact, action, entry.repeatCount);
  }
  if (source === "EXECUTION_ACTION") {
    const action = actionsByRef.get(valueOf(entry.item, "source_ref", ""));
    if (action) return actionTimelinePresentation(action, orderSchedule);
    return {
      headline: translatedLabel(actionStateLabels, valueOf(entry.item, "status")),
      detail: translatedLabel(actionKindLabels, valueOf(detail, "action_kind", "")),
    };
  }
  if (source === "CONTROL_COMMAND") {
    const intent = valueOf(detail, "intent");
    const label = {
      STOP_NEW_RISK: "停止新增风险",
      RESUME_ACTIVATION: "恢复新增入场",
      EXIT_STRATEGY: "退出计划",
      USER_TAKEOVER: "用户接管",
    }[intent] ?? "计划控制";
    const state = valueOf(entry.item, "status");
    return {
      headline: state === "EFFECTIVE"
        ? `${label}已生效`
        : state === "PROCESSING"
          ? `${label}正在处理`
          : state === "REJECTED"
            ? `${label}未生效`
            : `${label} · ${state || "状态未知"}`,
      detail: state === "EFFECTIVE"
        ? "计划状态与相关责任已核对"
        : state === "PROCESSING"
          ? "仍在处理订单、持仓或保护责任"
          : state === "REJECTED"
            ? "计划未发生该项变更"
            : "",
    };
  }
  const ruleId = valueOf(detail, "rule_id", "");
  const noActionReason = valueOf(detail, "no_action_reason", "");
  const status = valueOf(entry.item, "status");
  const dynamicCancelPresentation = runtimePlanEventDynamicCancelPresentation(
    detail,
    orderSchedule,
  );
  const priceTickSize = valueOf(
    recordOf(orderSchedule.instrument_rules),
    "price_tick_size",
    "",
  ) || null;
  const noActionPresentation = noActionReason
    ? runtimeNoActionPresentation(
      noActionReason,
      detail,
      priceTickSize,
      {
        entryOrderAttempted: runtimeEntryOrderAttemptedBefore(
          [...actionsByRef.values()],
          facts,
          valueOf(entry.item, "at", ""),
        ),
        priorBlockingReason,
        entryConditionsConfigured: recordsOf(
          recordOf(orderScheduleSpecOf(orderSchedule).entry_conditions).items,
        ).some((item) => valueOf(item, "kind", "") !== "DECISION_BASIS_READY"),
      },
    )
    : null;
  return {
    headline: dynamicCancelPresentation
      ? dynamicCancelPresentation.headline
      : noActionPresentation
      ? noActionPresentation.headline
      : planEventSummary(status, ruleId),
    detail: dynamicCancelPresentation
      ? dynamicCancelPresentation.detail
      : noActionPresentation
      ? noActionPresentation.detail
      : status === "PROPOSED_ACTION_CAP_ACCEPTED"
        ? "资金检查已通过；是否提交或成交仍以下游动作与交易所事实为准"
        : status === "PROPOSED_ACTION_CAP_REJECTED"
          ? "资金检查未通过；没有形成交易所变更"
          : "没有额外交易所动作",
  };
}

function FreshnessStrip({
  marketCutoff,
  positionCutoff,
  orderCutoff,
  positionApplicable,
  orderApplicable,
}: {
  marketCutoff: string;
  positionCutoff: string;
  orderCutoff: string | null;
  positionApplicable: boolean;
  orderApplicable: boolean;
}) {
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const intervalId = window.setInterval(() => setNowMs(Date.now()), 1_000);
    return () => window.clearInterval(intervalId);
  }, []);
  const items = [
    { label: "行情", cutoff: marketCutoff, warningAfterMs: 30_000, eventOnly: false, applicable: true, emptyLabel: "" },
    { label: "仓位", cutoff: positionCutoff, warningAfterMs: 120_000, eventOnly: false, applicable: positionApplicable, emptyLabel: "未入场" },
    { label: "订单", cutoff: orderCutoff ?? "", warningAfterMs: 0, eventOnly: true, applicable: orderApplicable, emptyLabel: "尚无场所订单" },
  ];
  return (
    <Box
      role="group"
      aria-label="数据状态"
      sx={{
        p: 1,
        mb: 1.5,
        border: 1,
        borderColor: "divider",
        borderRadius: 1,
        bgcolor: "background.default",
      }}
    >
      <Typography variant="caption" sx={{ display: "block", fontWeight: 750, mb: 0.75 }}>
        数据状态
      </Typography>
      <Stack direction="row" spacing={0.5} useFlexGap sx={{ flexWrap: "wrap" }}>
        {items.map((item) => {
          const cutoffMs = Date.parse(item.cutoff);
          const unknown = !Number.isFinite(cutoffMs);
          const stale = !item.eventOnly && !unknown && nowMs - cutoffMs > item.warningAfterMs;
          const exact = formatUserVisibleTime(item.cutoff);
          const age = relativeAgeLabel(item.cutoff, nowMs);
          const chipLabel = !item.applicable
            ? `${item.label} ${item.emptyLabel}`
            : item.eventOnly
              ? `${item.label} ${unknown ? age : `${age}变化`}`
              : `${item.label} ${age}`;
          const tooltip = !item.applicable
            ? item.label === "仓位"
              ? "本计划尚未形成入场成交，因此没有需要刷新的计划持仓事实。"
              : "本计划尚未形成交易所动作，因此没有订单状态变化。"
            : item.eventOnly
              ? `最近一次订单状态变化：${exact}。状态未变化时不会重复生成事件；这不是订单监控心跳。`
              : `${item.label}事实截止：${exact}`;
          return (
            <Tooltip
              key={item.label}
              arrow
              title={tooltip}
            >
              <Chip
                tabIndex={0}
                size="small"
                variant="outlined"
                color={!item.applicable ? "default" : unknown || stale ? "warning" : "success"}
                label={chipLabel}
                aria-label={!item.applicable
                  ? chipLabel
                  : item.eventOnly
                    ? `${item.label}${unknown ? age : `${age}变化`}；精确时间 ${exact}；这不是监控心跳`
                    : `${item.label}数据${age}；精确截止 ${exact}`}
              />
            </Tooltip>
          );
        })}
      </Stack>
    </Box>
  );
}

function RuntimeDeadlineProgress({
  activationCreatedAt,
  entryValidUntil,
  entryOrderDeadline,
  hasOpenEntryResponsibility,
  hasPendingEntryResponsibility,
  hasEntryFill,
  firstFillAt,
  timeExitSeconds,
  exitHandoff,
  terminal,
}: {
  activationCreatedAt: string;
  entryValidUntil: string;
  entryOrderDeadline: RuntimeEntryOrderDeadline | null;
  hasOpenEntryResponsibility: boolean;
  hasPendingEntryResponsibility: boolean;
  hasEntryFill: boolean;
  firstFillAt: string;
  timeExitSeconds: number | null;
  exitHandoff: boolean;
  terminal: boolean;
}) {
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const intervalId = window.setInterval(() => setNowMs(Date.now()), 1_000);
    return () => window.clearInterval(intervalId);
  }, []);
  const showEntryOrderDeadline = !terminal
    && hasOpenEntryResponsibility
    && entryOrderDeadline !== null;
  const entryProgressStart = showEntryOrderDeadline
    ? entryOrderDeadline.submittedAt
    : activationCreatedAt;
  const entryProgressEnd = showEntryOrderDeadline
    ? entryOrderDeadline.effectiveDeadlineAt
    : entryValidUntil;
  const entryProgress = terminal || hasEntryFill
    && !hasPendingEntryResponsibility
      ? 100
      : boundedProgressPercent(entryProgressStart, entryProgressEnd, nowMs);
  const entryStatus = terminal
    ? hasEntryFill ? "已完成" : "计划已结束"
    : showEntryOrderDeadline
      ? remainingTimeLabel(entryOrderDeadline.effectiveDeadlineAt, nowMs)
      : hasEntryFill && !hasPendingEntryResponsibility ? "已完成"
      : remainingTimeLabel(entryValidUntil, nowMs);
  const entryLabel = showEntryOrderDeadline
    ? hasEntryFill ? "剩余入场委托最迟撤销" : "未成交委托最迟撤销"
    : "入场窗口";
  const entryTooltip = showEntryOrderDeadline
    ? entryOrderDeadline.limitedByPlanValidity
      ? `计划有效期先到：${formatUserVisibleTime(entryOrderDeadline.effectiveDeadlineAt)}；首次真实提交：${formatUserVisibleTime(entryOrderDeadline.submittedAt)}；提交后 ${entryOrderDeadline.expireAfterSeconds} 秒规则原到期：${formatUserVisibleTime(entryOrderDeadline.ruleExpiresAt)}`
      : `委托到期：${formatUserVisibleTime(entryOrderDeadline.effectiveDeadlineAt)}；首次真实提交：${formatUserVisibleTime(entryOrderDeadline.submittedAt)}；按提交后 ${entryOrderDeadline.expireAfterSeconds} 秒计算`
    : `入场截止：${formatUserVisibleTime(entryValidUntil)}`;
  const firstFillMs = Date.parse(firstFillAt);
  const timeExitAt = timeExitSeconds !== null && Number.isFinite(firstFillMs)
    ? new Date(firstFillMs + timeExitSeconds * 1_000).toISOString()
    : "";
  const exitProgress = !terminal && timeExitAt
    ? boundedProgressPercent(firstFillAt, timeExitAt, nowMs)
    : null;
  return (
    <Stack spacing={1} sx={{ mt: 1 }}>
      <Box>
        <Stack direction="row" sx={{ justifyContent: "space-between", gap: 1, mb: 0.5 }}>
          <Typography variant="caption">{entryLabel}</Typography>
          <Tooltip arrow title={entryTooltip}>
            <Typography tabIndex={0} variant="caption" sx={{ fontWeight: 750 }}>
              {entryStatus}
            </Typography>
          </Tooltip>
        </Stack>
        <LinearProgress
          variant="determinate"
          value={entryProgress ?? 0}
          aria-label={`${entryLabel}${entryStatus}`}
        />
        {showEntryOrderDeadline && (
          <Stack direction="row" sx={{ justifyContent: "space-between", gap: 1, mt: 0.5 }}>
            <Typography variant="caption" color="text.secondary">计划有效期</Typography>
            <Tooltip arrow title={`计划有效期：${formatUserVisibleTime(entryValidUntil)}`}>
              <Typography
                tabIndex={0}
                variant="caption"
                color="text.secondary"
                sx={{ fontWeight: 650 }}
              >
                {remainingTimeLabel(entryValidUntil, nowMs)}
              </Typography>
            </Tooltip>
          </Stack>
        )}
      </Box>
      <Box>
        <Stack direction="row" sx={{ justifyContent: "space-between", gap: 1 }}>
          <Typography variant="caption">计划自动退出</Typography>
          {terminal ? (
            <Typography variant="caption" sx={{ fontWeight: 750 }}>
              {hasEntryFill ? "计划已结束" : "未入场，计划已结束"}
            </Typography>
          ) : exitHandoff ? (
            <Typography variant="caption" color="warning.main" sx={{ fontWeight: 750 }}>
              退出交接中
            </Typography>
          ) : timeExitSeconds === null ? (
            <Chip size="small" color="warning" variant="outlined" label="未配置倒计时" />
          ) : !timeExitAt ? (
            <Typography variant="caption" sx={{ fontWeight: 750 }}>
              成交后启动 {timeExitSeconds} 秒
            </Typography>
          ) : (
            <Tooltip arrow title={`自动退出发起时点：${formatUserVisibleTime(timeExitAt)}`}>
              <Typography tabIndex={0} variant="caption" sx={{ fontWeight: 750 }}>
                {remainingTimeLabel(timeExitAt, nowMs)}
              </Typography>
            </Tooltip>
          )}
        </Stack>
        {exitProgress !== null && (
          <LinearProgress
            variant="determinate"
            value={exitProgress}
            aria-label={`计划自动退出${remainingTimeLabel(timeExitAt, nowMs)}`}
            sx={{ mt: 0.5 }}
          />
        )}
      </Box>
    </Stack>
  );
}

function planDeletionError(error: unknown): string {
  const code = error instanceof ApiFailure ? error.code : "结果未知";
  if (code === "PLAN_VERSION_CONFLICT") return "草稿已变化，请关闭弹窗并刷新后重试";
  if (code === "PLAN_ALREADY_STARTED") return "计划已经启动，不能再删除草稿";
  if (code === "PLAN_NOT_FOUND") return "草稿已不存在，请刷新计划列表";
  return `草稿未删除：${code}`;
}

function AppLoading() {
  return (
    <Box sx={{ minHeight: "100vh", display: "grid", placeItems: "center" }} role="status" aria-live="polite">
      <Stack spacing={2} sx={{ alignItems: "center" }}>
        <CircularProgress size={26} />
        <Typography variant="body2" color="text.secondary">正在核对本机服务与当前构建…</Typography>
      </Stack>
    </Box>
  );
}

function ConnectionFailure({ retry }: { retry: () => void }) {
  return (
    <Box sx={{ width: "min(620px, calc(100% - clamp(32px, 4vw, 48px)))", mx: "auto", pt: 12 }}>
      <Typography variant="overline" color="text.secondary">LOCAL APP UNAVAILABLE</Typography>
      <Typography variant="h1" sx={{ mt: 1, mb: 3 }}>无法取得当前工作台状态</Typography>
      <Alert severity="error" variant="outlined" sx={{ mb: 3 }}>
        当前结果未知。页面没有使用缓存冒充服务器事实，也没有开放任何资本或交易指令。
      </Alert>
      <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5}>
        <Button variant="contained" onClick={retry}>重新查询</Button>
        <Button component="a" href="/operations" variant="outlined" endIcon={<OpenInNewOutlined />}>
          打开故障接管
        </Button>
      </Stack>
    </Box>
  );
}

function EnvironmentChanging({ targetLabel }: { targetLabel?: string }) {
  return (
    <Box sx={{ minHeight: "100vh", display: "grid", placeItems: "center" }} role="status" aria-live="assertive">
      <Stack spacing={2} sx={{ alignItems: "center", px: 3, textAlign: "center" }}>
        <CircularProgress size={26} />
        <Typography variant="h2">
          {targetLabel
            ? `正在切换到${targetLabel}`
            : "运行环境已切换，正在清空旧数据"}
        </Typography>
        <Typography variant="body2" color="text.secondary">
          当前浏览器中的计划、激活、复盘、表单草稿、K 线与实时连接将按新上下文重新载入。
          切换不会停止其他上下文的执行器或已激活计划。
        </Typography>
      </Stack>
    </Box>
  );
}

type FrameContext = {
  status: SettingsStatus;
  marketColorScheme: MarketColorScheme;
  setMarketColorScheme: (scheme: MarketColorScheme) => void;
};

const navItems = [
  { label: "总览", path: "/overview", icon: <DashboardOutlined /> },
  { label: "剥头皮", path: "/scalping", icon: <BoltOutlined /> },
  { label: "策略计划", path: "/plans", icon: <AssignmentOutlined /> },
  { label: "复盘", path: "/reviews", icon: <ReviewsOutlined /> },
  { label: "设置", path: "/settings", icon: <SettingsOutlined /> },
];

function TradingContextSwitchControl({
  status,
  onSwitch,
}: {
  status: SettingsStatus;
  onSwitch: (target: TradingContextTarget, targetUrl: string) => void;
}) {
  const currentAccountType = status.venue_account_type as VenueAccountType;
  const contexts = status.trading_contexts as TradingContextTarget[];
  const safeTarget = (target: TradingContextTarget): string | null => (
    typeof window === "undefined"
      ? null
      : tradingContextSwitchTarget(target, window.location.href)
  );
  const unavailable = contexts.some((target) => (
    target.venue_account_type !== currentAccountType && safeTarget(target) === null
  ));
  const unavailableReason = "至少一个交易上下文入口无效；对应选项已禁用，当前上下文不变。";

  return (
    <Stack
      direction="row"
      spacing={.25}
      sx={{ alignItems: "center", flexShrink: 0 }}
      data-testid="trading-context-switch"
    >
      <TextField
        select
        size="small"
        label="交易上下文"
        value={currentAccountType}
        onChange={(event) => {
          const nextAccountType = event.target.value as VenueAccountType;
          if (nextAccountType === currentAccountType) return;
          const target = contexts.find(
            (item) => item.venue_account_type === nextAccountType,
          );
          if (!target) return;
          const targetUrl = safeTarget(target);
          if (targetUrl !== null) onSwitch(target, targetUrl);
        }}
        sx={{
          minWidth: { xs: 148, sm: 188 },
          "& .MuiInputBase-root": { minHeight: 38, fontSize: { xs: 11, sm: 12 }, fontWeight: 750 },
          "& .MuiInputLabel-root": { fontSize: 12 },
        }}
      >
        {contexts.map((target) => (
          <MenuItem
            key={target.venue_account_type}
            value={target.venue_account_type}
            disabled={
              target.venue_account_type !== currentAccountType
              && safeTarget(target) === null
            }
          >
            {tradingContextLabel(target.venue_account_type)}
          </MenuItem>
        ))}
      </TextField>
      {unavailable && (
        <Tooltip title={unavailableReason} arrow>
          <IconButton
            size="small"
            aria-label={unavailableReason}
            sx={{ width: 28, height: 28 }}
          >
            <InfoOutlined fontSize="inherit" />
          </IconButton>
        </Tooltip>
      )}
    </Stack>
  );
}

function WorkbenchFrame({ status }: { status: SettingsStatus }) {
  const theme = useTheme();
  const queryClient = useQueryClient();
  const narrow = useMediaQuery(theme.breakpoints.down("md"));
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [navigationCollapsed, setNavigationCollapsed] = useState(readNavigationCollapsed);
  const [loadedProductBuildId] = useState(status.product_build_id);
  const [marketColorScheme, setMarketColorSchemeState] = useState(readMarketColorScheme);
  const [switchingEnvironmentLabel, setSwitchingEnvironmentLabel] = useState<string | null>(null);
  const environmentNavigationStartedRef = useRef(false);
  const location = useLocation();
  const navigate = useNavigate();
  useLayoutEffect(() => { applyMarketColorScheme(marketColorScheme); }, [marketColorScheme]);
  const setMarketColorScheme = useCallback((scheme: MarketColorScheme) => {
    saveMarketColorScheme(scheme);
    setMarketColorSchemeState(scheme);
  }, []);
  const toggleNavigationCollapsed = useCallback(() => {
    setNavigationCollapsed((current) => {
      const next = !current;
      saveNavigationCollapsed(next);
      return next;
    });
  }, []);
  const switchEnvironment = useCallback((
    target: TradingContextTarget,
    targetUrl: string,
  ) => {
    if (environmentNavigationStartedRef.current) return;
    environmentNavigationStartedRef.current = true;
    setSwitchingEnvironmentLabel(tradingContextLabel(target.venue_account_type));
    queryClient.clear();
    window.sessionStorage.clear();
    window.setTimeout(() => {
      window.location.assign(targetUrl);
    }, 120);
  }, [queryClient]);
  const drawerCollapsed = !narrow && navigationCollapsed;
  const desktopDrawerWidth = navigationCollapsed ? COLLAPSED_DRAWER_WIDTH : DRAWER_WIDTH;
  const productUpdateAvailable = Boolean(
    loadedProductBuildId
    && status.product_build_id
    && loadedProductBuildId !== status.product_build_id
  );
  const currentPrimaryPath = location.pathname.startsWith("/plans")
    ? "/plans"
    : location.pathname.startsWith("/scalping")
      ? "/scalping"
    : location.pathname.startsWith("/reviews")
      ? "/reviews"
      : location.pathname.startsWith("/settings")
        ? "/settings"
        : "/overview";
  const pageTitle = location.pathname.startsWith("/activations/")
    ? "计划详情与复盘"
    : navItems.find((item) => item.path === currentPrimaryPath)?.label ?? "工作台";

  if (switchingEnvironmentLabel !== null) {
    return <EnvironmentChanging targetLabel={switchingEnvironmentLabel} />;
  }

  const drawer = (
    <Box component="aside" aria-label="工作台侧栏" sx={{ height: "100%", minHeight: 0, display: "flex", flexDirection: "column" }}>
      <Stack direction="row" spacing={.75} sx={{ height: 64, minHeight: 64, px: drawerCollapsed ? 1 : 1.5, alignItems: "center", justifyContent: drawerCollapsed ? "center" : "flex-start", borderBottom: 1, borderColor: "rgba(16,24,32,.09)" }}>
        {!drawerCollapsed && <Typography sx={{ fontSize: 22, lineHeight: 1, fontWeight: 760 }}>{narrow ? "Halpha 工作台" : "Halpha"}</Typography>}
        {!narrow && (
          <IconButton
            aria-label={drawerCollapsed ? "展开导航" : "折叠导航"}
            title={drawerCollapsed ? "展开导航" : "折叠导航"}
            onClick={toggleNavigationCollapsed}
            sx={{ ml: drawerCollapsed ? 0 : "auto", width: 36, height: 36, flexShrink: 0, border: 0, bgcolor: "transparent" }}
          >
            {drawerCollapsed ? <ChevronRightOutlined /> : <ChevronLeftOutlined />}
          </IconButton>
        )}
      </Stack>
      <Box component="nav" aria-label="工作台导航" sx={{ pt: 2, minHeight: 0, display: "flex", flexGrow: 1, flexDirection: "column" }}>
        <List aria-label="工作台主导航" sx={{ px: drawerCollapsed ? 1 : 1.75, py: 0, display: "grid", gap: .75 }}>
          {navItems.map((item) => (
            <ListItem key={item.path} disablePadding>
              <Tooltip title={drawerCollapsed ? item.label : ""} placement="right" disableHoverListener={!drawerCollapsed} disableFocusListener={!drawerCollapsed}>
                <ListItemButton
                  aria-label={item.label}
                  selected={item.path === currentPrimaryPath}
                  onClick={() => { navigate(item.path); setDrawerOpen(false); }}
                  sx={{ px: drawerCollapsed ? 0 : 1.5, justifyContent: drawerCollapsed ? "center" : "flex-start" }}
                >
                  <ListItemIcon sx={{ minWidth: drawerCollapsed ? 0 : 36, color: "inherit", justifyContent: "center" }}>{item.icon}</ListItemIcon>
                  {!drawerCollapsed && <ListItemText primary={item.label} slotProps={{ primary: { sx: { fontSize: 14, fontWeight: 700 } } }} />}
                </ListItemButton>
              </Tooltip>
            </ListItem>
          ))}
        </List>
        <Box sx={{ mt: "auto", p: drawerCollapsed ? 1 : 1.75 }}>
          <Divider sx={{ mb: 1.5 }} />
          {drawerCollapsed ? (
            <Tooltip title="故障接管" placement="right">
              <IconButton component="a" href="/operations" aria-label="故障接管" sx={{ mx: "auto", display: "flex" }}>
                <OpenInNewOutlined />
              </IconButton>
            </Tooltip>
          ) : (
            <Button component="a" href="/operations" fullWidth variant="text" endIcon={<OpenInNewOutlined />} sx={{ justifyContent: "space-between" }}>
              故障接管
            </Button>
          )}
        </Box>
      </Box>
    </Box>
  );

  return (
    <Box data-market-color-scheme={marketColorScheme} sx={{ minHeight: "100vh", bgcolor: "background.default" }}>
      <AppBar
        position="fixed"
        color="transparent"
        sx={{
          zIndex: theme.zIndex.appBar,
          left: { xs: 0, md: `${desktopDrawerWidth}px` },
          width: { xs: "100%", md: `calc(100% - ${desktopDrawerWidth}px)` },
          transition: theme.transitions.create(["left", "width"], { duration: theme.transitions.duration.shorter }),
        }}
      >
        <Toolbar
          sx={{
            minHeight: { xs: 96, md: 64 },
            alignContent: "center",
            flexWrap: { xs: "wrap", md: "nowrap" },
            columnGap: { xs: 1, sm: 2 },
            rowGap: { xs: .25, md: 0 },
            px: { xs: 1.5, sm: 2.5, md: 3 },
          }}
        >
          {narrow && <IconButton aria-label="打开导航" onClick={() => setDrawerOpen(true)} edge="start"><MenuOutlined /></IconButton>}
          {narrow ? (
            <Stack direction="row" spacing={.5} sx={{ alignItems: "baseline", minWidth: 0 }}>
              <Typography component="span" sx={{ fontSize: 15, lineHeight: 1.1, fontWeight: 750 }}>Halpha</Typography>
              <Typography component="span" sx={{ fontSize: 15, lineHeight: 1.1, fontWeight: 650 }} noWrap>· {pageTitle}</Typography>
            </Stack>
          ) : (
            <Typography component="div" sx={{ fontSize: 24, lineHeight: 1.1, fontWeight: 650 }}>{pageTitle}</Typography>
          )}
          <TradingContextSwitchControl status={status} onSwitch={switchEnvironment} />
          <Typography
            className="mono"
            variant="caption"
            color="text.secondary"
            noWrap
            sx={{
              order: { xs: 5, md: 0 },
              flexBasis: { xs: "100%", md: "auto" },
              ml: { md: "auto" },
              maxWidth: { xs: "100%", md: 300 },
              fontSize: { xs: 10, md: 12 },
            }}
          >
            账户 · {tradingAccountLabel(status.venue_account_type)} · {status.account_id}
          </Typography>
          <Typography
            variant="caption"
            color="text.secondary"
            sx={{
              display: "block",
              order: { xs: 6, md: 0 },
              flexBasis: { xs: "100%", md: "auto" },
              fontSize: { xs: 10, md: 12 },
            }}
          >
            事实截止 {formatUserVisibleTime(status.server_fact_cutoff)}
          </Typography>
          {status.environment_kind === "LIVE" && (
            <Stack direction="row" spacing={.5} sx={{ alignItems: "center" }}>
              <Chip label="LIVE" size="small" color="error" variant="outlined" />
              <Chip
                label={`实盘写门 · ${translatedLabel(gateStateLabels, status.runtime_real_write_gate)}`}
                size="small"
                color={status.runtime_real_write_gate === "OPEN" ? "error" : "warning"}
                variant="outlined"
              />
            </Stack>
          )}
        </Toolbar>
      </AppBar>
      <Drawer
        variant={narrow ? "temporary" : "permanent"}
        open={narrow ? drawerOpen : true}
        onClose={() => setDrawerOpen(false)}
        ModalProps={{ keepMounted: true }}
        slotProps={{ paper: { "aria-label": narrow ? "工作台导航抽屉" : undefined } }}
        sx={{
          width: narrow ? DRAWER_WIDTH : desktopDrawerWidth,
          flexShrink: 0,
          "& .MuiDrawer-paper": {
            width: narrow ? DRAWER_WIDTH : desktopDrawerWidth,
            top: 0,
            height: "100%",
            overflowX: "hidden",
            transition: theme.transitions.create("width", { duration: theme.transitions.duration.shorter }),
          },
        }}
      >
        {drawer}
      </Drawer>
      <Box component="main" sx={{ ml: { xs: 0, md: `${desktopDrawerWidth}px` }, pt: { xs: "100px", md: "65px" }, minHeight: "100vh", transition: theme.transitions.create("margin-left", { duration: theme.transitions.duration.shorter }) }}>
        {productUpdateAvailable && (
          <Alert
            severity="info"
            variant="standard"
            action={(
              <Button
                color="inherit"
                aria-label="刷新并加载当前产品版本"
                onClick={() => window.location.reload()}
                sx={{ flexShrink: 0, whiteSpace: "nowrap" }}
              >
                刷新页面
              </Button>
            )}
            sx={{ m: 2, mb: 0 }}
          >
            产品已更新。刷新页面后使用当前版本；尚未提交的表单内容不会自动保留。
          </Alert>
        )}
        <Outlet context={{ status, marketColorScheme, setMarketColorScheme } satisfies FrameContext} />
      </Box>
    </Box>
  );
}

type AccountPosition = Overview["account_positions"][number];
type AccountOrder = Overview["account_orders"][number];
type AccountPositionOperation = "REDUCE" | "CLOSE" | "ADD";

const accountOrderTypeLabels: Record<string, string> = {
  LIMIT: "限价",
  MARKET: "市价",
  STOP: "止损限价",
  STOP_MARKET: "止损市价",
  TAKE_PROFIT: "止盈限价",
  TAKE_PROFIT_MARKET: "止盈市价",
  TRAILING_STOP_MARKET: "跟踪止损",
};

const accountOrderStatusLabels: Record<string, string> = {
  NEW: "未成交",
  PARTIALLY_FILLED: "部分成交",
  TRIGGERED: "已触发",
  TRIGGERING: "触发中",
};

const accountPositionOperationBlockerLabels: Record<string, string> = {
  READ_ONLY_CREDENTIAL: "当前 API Key 只读，不能向交易所提交订单",
  OPEN_ORDERS_REQUIRE_RECONCILIATION: "账户仍有未结普通或条件委托，必须先逐笔核对",
  ATTRIBUTION_REQUIRES_RECONCILIATION: "同一合约已有 Halpha 归属，必须先完成账户与计划核对",
  HEDGE_MODE_POSITION_OPERATIONS_UNSUPPORTED: "双向持仓侧只开放精确减仓/平仓；追加开仓需先清空外部基线并进入受支持的新风险路径",
  EXTERNAL_POSITION_REQUIRES_ALIGNMENT: "追加开仓是独立新风险；现有外部基线必须先对齐或清空",
};

const positionAlignmentReadinessLabels: Record<string, string> = {
  ACCOUNT_POSITION_MODE_UNSUPPORTED: "账户持仓模式与计划固定的持仓侧不一致",
  POSITION_ALIGNMENT_BASELINE_UNKNOWN: "原始账户快照已不可核验",
  POSITION_ALIGNMENT_BASELINE_INVALID: "原始账户快照不完整、已有未结委托或基线不匹配",
  POSITION_ALIGNMENT_FACT_NOT_CURRENT: "最新完整账户快照已过期",
  POSITION_ALIGNMENT_FACT_CHANGED: "持仓数量、方向、入场价或未结委托已发生变化",
  POSITION_ALIGNMENT_SCOPE_CONFLICT: "同一账户与合约已有运行中的计划责任",
};

function AccountPositionOperationDialog({
  position,
  status,
  onClose,
}: {
  position: AccountPosition | null;
  status: SettingsStatus;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [operation, setOperation] = useState<AccountPositionOperation>("REDUCE");
  const [requestedQuantity, setRequestedQuantity] = useState("");
  const [requestedNotional, setRequestedNotional] = useState("");
  const createIdentityRef = useRef<StableRequestIdentity | null>(null);
  const previewMutation = useMutation({
    mutationFn: previewAccountPositionOperation,
  });
  const createMutation = useMutation({
    mutationFn: ({
      payload,
      idempotencyKey,
    }: {
      payload: PlanCreatePayload;
      idempotencyKey: string;
    }) => createPlan(payload, idempotencyKey),
    onSuccess: async () => {
      createIdentityRef.current = null;
      await queryClient.invalidateQueries({ queryKey: ["plans"] });
      onClose();
      navigate("/plans");
    },
    onError: (error) => {
      if (!isUnknownMutationResult(error)) createIdentityRef.current = null;
    },
  });
  useEffect(() => {
    if (!position) return;
    const quantity = Number(position.absolute_quantity);
    const notional = Math.abs(Number(position.notional));
    setOperation("REDUCE");
    setRequestedQuantity(
      Number.isFinite(quantity) && quantity > 0
        ? String(Number((quantity / 2).toFixed(8)))
        : "",
    );
    setRequestedNotional(
      Number.isFinite(notional) && notional > 0
        ? String(Number(Math.max(10, notional * .25).toFixed(2)))
        : "100",
    );
    createIdentityRef.current = null;
    previewMutation.reset();
    createMutation.reset();
  }, [position?.snapshot_ref, position?.position_side]);
  if (!position) return null;

  const baseline = Number(position.absolute_quantity);
  const reduction = Number(requestedQuantity);
  const targetAfter = operation === "CLOSE"
    ? 0
    : operation === "REDUCE" && Number.isFinite(baseline) && Number.isFinite(reduction)
      ? baseline - reduction
      : baseline;
  const inputValid = operation === "REDUCE"
    ? Number.isFinite(reduction) && reduction > 0 && reduction < baseline
    : operation === "ADD"
      ? Number(requestedNotional) > 0 && Number.isFinite(Number(requestedNotional))
      : true;
  const submitPreview = () => {
    previewMutation.mutate({
      operation,
      snapshot_ref: position.snapshot_ref,
      fact_cutoff: position.fact_cutoff,
      instrument_ref: position.instrument_ref,
      position_side: position.position_side,
      expected_absolute_quantity: position.absolute_quantity,
      requested_quantity: operation === "REDUCE" ? requestedQuantity : null,
      requested_notional: operation === "ADD" ? requestedNotional : null,
    });
  };
  const preview = previewMutation.data;
  const createDispositionPlan = () => {
    if (!preview || preview.plan_prefill.kind !== "POSITION_DISPOSITION") return;
    const prefill = preview.plan_prefill;
    if (!prefill.position_alignment) return;
    const payload = {
      plan_name: prefill.plan_name,
      creator_kind: "HUMAN",
      decision_context: {
        rationale: prefill.position_alignment.operation === "CLOSE"
          ? "按当前风险计划关闭交易所账户中的既有外部持仓。"
          : "按当前风险计划减少交易所账户中的既有外部持仓。",
        evidence: `依据截止 ${formatUserVisibleTime(prefill.position_alignment.fact_cutoff)} 的完整交易所账户快照，基线数量 ${prefill.position_alignment.baseline_quantity}，本次处置 ${prefill.position_alignment.requested_reduction_quantity}。`,
        limitations: "该计划只承担固定基线内的 reduce-only 处置责任，不改写既有入场来源，也不把外部持仓历史盈亏归属于 Halpha。",
      },
      decision_basis: {
        kind: "DIRECT_EXECUTION",
        decision_basis_ref: "DIRECT_EXECUTION@1",
        parameters: {},
      },
      position_alignment: prefill.position_alignment,
      venue_ref: "BINANCE_USDM",
      instrument_ref: prefill.instrument_ref,
      direction: prefill.direction,
      target_exposure: prefill.trade_amount,
      max_margin: prefill.trade_amount,
      max_notional: prefill.trade_amount,
      max_allowed_loss: prefill.trade_amount,
      valid_minutes: prefill.valid_minutes,
    } satisfies PlanCreatePayload;
    const scope = `${status.environment_id}:CREATE_POSITION_DISPOSITION:${preview.snapshot_ref}`;
    const identity = persistentRequestIdentity(
      createIdentityRef.current,
      scope,
      JSON.stringify(payload),
    );
    createIdentityRef.current = identity;
    createMutation.mutate({ payload, idempotencyKey: identity.idempotencyKey });
  };
  const openAddPlan = () => {
    if (!preview || preview.plan_prefill.kind !== "NEW_EXPOSURE") return;
    const params = new URLSearchParams({
      mode: "direct",
      positionOperation: "ADD",
      instrument: preview.plan_prefill.instrument_ref,
      direction: preview.plan_prefill.direction,
      tradeAmount: preview.plan_prefill.trade_amount,
      snapshotCutoff: preview.fact_cutoff,
    });
    onClose();
    navigate(`/plans/new?${params.toString()}`);
  };
  const previewError = previewMutation.error instanceof ApiFailure
    ? previewMutation.error.code
    : "ACCOUNT_POSITION_OPERATION_PREVIEW_FAILED";
  return (
    <Dialog open onClose={onClose} fullWidth maxWidth="sm" aria-labelledby="position-operation-title">
      <DialogTitle id="position-operation-title">策略计划对齐 · {position.instrument_ref}</DialogTitle>
      <DialogContent>
        <Typography color="text.secondary" variant="body2" sx={{ mb: 2 }}>
          基线来自 {formatUserVisibleTime(position.fact_cutoff)} 的完整账户快照。减仓和平仓只形成 reduce-only 责任；追加开仓会建立另一份新风险计划，不改写既有入场来源。
        </Typography>
        <FactGrid
          dense
          columns={3}
          facts={[
            { label: "持仓侧", value: `${position.direction === "LONG" ? "做多" : "做空"} · ${position.position_side}` },
            { label: "基线数量", value: marketVolume(position.absolute_quantity) },
            { label: "标记价", value: `${marketPrice(position.mark_price)} USDT` },
          ]}
        />
        <ToggleButtonGroup
          exclusive
          fullWidth
          value={operation}
          onChange={(_event, next: AccountPositionOperation | null) => {
            if (!next) return;
            setOperation(next);
            previewMutation.reset();
            createMutation.reset();
          }}
          aria-label="仓位操作"
          sx={{ mt: 2 }}
        >
          <ToggleButton value="REDUCE">减仓</ToggleButton>
          <ToggleButton value="CLOSE">平仓</ToggleButton>
          <ToggleButton value="ADD">追加开仓</ToggleButton>
        </ToggleButtonGroup>
        {operation === "REDUCE" && (
          <TextField
            fullWidth
            sx={{ mt: 2 }}
            label="计划减仓数量"
            value={requestedQuantity}
            onChange={(event) => {
              setRequestedQuantity(event.target.value);
              previewMutation.reset();
            }}
            error={requestedQuantity.length > 0 && !inputValid}
            helperText={`必须大于 0 且小于基线 ${marketVolume(position.absolute_quantity)}`}
            slotProps={{ htmlInput: { inputMode: "decimal" } }}
          />
        )}
        {operation === "ADD" && (
          <TextField
            fullWidth
            sx={{ mt: 2 }}
            label="追加开仓计划金额（USDT）"
            value={requestedNotional}
            onChange={(event) => {
              setRequestedNotional(event.target.value);
              previewMutation.reset();
            }}
            error={requestedNotional.length > 0 && !inputValid}
            helperText="这是独立计划的最大名义金额，不会并入外部基线"
            slotProps={{ htmlInput: { inputMode: "decimal" } }}
          />
        )}
        {operation !== "ADD" && (
          <Alert severity={operation === "CLOSE" ? "warning" : "info"} variant="outlined" sx={{ mt: 2 }}>
            计划完成后该处置责任的目标数量为 {Number.isFinite(targetAfter) && targetAfter >= 0 ? marketVolume(String(targetAfter)) : "不可确认"}。既有入场仍为外部事实，不计入 Halpha ENTRY 或策略盈亏。
          </Alert>
        )}
        {previewMutation.isError && (
          <Alert severity="error" sx={{ mt: 2 }}>
            操作预检失败（{previewError}）。账户快照可能已变化，请关闭后刷新总览再核对。
          </Alert>
        )}
        {preview && (
          <Box sx={{ mt: 2 }}>
            <Alert severity={preview.activation_allowed ? "success" : "warning"} variant="outlined">
              {preview.activation_allowed
                ? operation === "ADD"
                  ? "当前预检允许进入独立新增风险计划；提交并启动时仍会重读账户事实。"
                  : "当前预检允许创建并确认处置计划；激活时仍会重读账户事实，Executor 提交前还会再次核对。"
                : operation === "ADD"
                  ? "已形成独立新增风险计划预填，但当前不能激活或提交交易所。以下条件必须先解决。"
                  : "计划参数已对齐，但当前不能激活或提交交易所。以下条件必须先解决。"}
            </Alert>
            {preview.blockers.length > 0 && (
              <Stack spacing={.75} sx={{ mt: 1.25 }}>
                {preview.blockers.map((blocker) => (
                  <Typography key={blocker} variant="body2" color="warning.main">
                    · {accountPositionOperationBlockerLabels[blocker] ?? blocker}
                  </Typography>
                ))}
              </Stack>
            )}
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1.25 }}>
              本次仅完成预检；尚未创建执行动作，也未向 Binance 发出请求。
            </Typography>
          </Box>
        )}
        {createMutation.isError && (
          <Alert severity="error" sx={{ mt: 2 }}>
            处置计划草稿创建失败；结果不明确时请先到计划列表核对，勿重复点击。
          </Alert>
        )}
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2.5, flexWrap: "wrap" }}>
        <Button onClick={onClose} disabled={createMutation.isPending}>取消</Button>
        {!preview && (
          <Button variant="contained" onClick={submitPreview} disabled={!inputValid || previewMutation.isPending}>
            {previewMutation.isPending ? "正在核对" : "核对计划对齐"}
          </Button>
        )}
        {preview?.plan_prefill.kind === "POSITION_DISPOSITION" && (
          <Button
            variant="contained"
            onClick={createDispositionPlan}
            disabled={!preview.preparation_allowed || status.profile === "BINANCE_LIVE_READ_ONLY" || createMutation.isPending}
          >
            {createMutation.isPending ? "正在创建" : "创建处置计划草稿"}
          </Button>
        )}
        {preview?.plan_prefill.kind === "NEW_EXPOSURE" && (
          <Button variant="contained" onClick={openAddPlan}>
            {status.profile === "BINANCE_LIVE_READ_ONLY" ? "查看独立开仓计划" : "进入独立开仓计划"}
          </Button>
        )}
      </DialogActions>
    </Dialog>
  );
}

function OverviewPage() {
  const navigate = useNavigate();
  const { status, marketColorScheme } = useOutletContext<FrameContext>();
  const [activeTab, setActiveTab] = useState<"POSITIONS" | "ORDERS" | "TRADES" | null>(null);
  const [positionOperationTarget, setPositionOperationTarget] = useState<AccountPosition | null>(null);
  const environmentScope = `${status.environment_kind}:${status.environment_id}`;
  const query = useQuery({ queryKey: ["overview"], queryFn: getOverview, refetchInterval: 5_000 });
  const activationsQuery = useQuery({ queryKey: ["activations"], queryFn: getActivations, refetchInterval: 30_000 });
  const reviewsQuery = useQuery({ queryKey: ["reviews"], queryFn: getReviews, refetchInterval: 30_000 });
  const strategiesQuery = useQuery({ queryKey: ["strategies"], queryFn: getStrategies });
  useEffect(() => {
    setActiveTab(null);
    setPositionOperationTarget(null);
  }, [status.environment_id, status.environment_kind]);
  const data = query.data;
  const environmentContextMismatch = Boolean(
    data
    && (
      data.environment_kind !== status.environment_kind
      || data.account_id !== status.account_id
    )
  );
  const openActivations = (activationsQuery.data ?? []).filter((activation) => activation.lifecycle !== "COMPLETED");
  const activationDetailQueries = useQueries({
    queries: openActivations.map((activation) => ({
      queryKey: ["activation", activation.activation_id],
      queryFn: () => getActivation(activation.activation_id),
      refetchInterval: 30_000,
    })),
  });
  const recentClosedTrades = [...(reviewsQuery.data ?? [])]
    .filter((review) => {
      const result = tradeResultForReview(review);
      return ["COMPLETED", "PARTIAL"].includes(valueOf(review, "primary_result"))
        && result.calculation_complete === true
        && result.closed === true
        && ![
          "ACCOUNT_FACTS_WITH_EXTERNAL_CLOSURE",
          "EXTERNAL_POSITION_DISPOSITION",
        ].includes(String(result.result_scope ?? ""))
        && Number.isFinite(Number(result.net_pnl));
    })
    .sort((left, right) => Date.parse(valueOf(right, "fact_cutoff", "")) - Date.parse(valueOf(left, "fact_cutoff", "")))
    .slice(0, 3);
  const recentNetPnl = recentClosedTrades.reduce((total, review) => (
    total + Number(tradeResultForReview(review).net_pnl)
  ), 0);
  const recentAverageNetPnl = recentClosedTrades.length > 0
    ? recentNetPnl / recentClosedTrades.length
    : 0;
  const firstNonLossIndex = recentClosedTrades.findIndex(
    (review) => Number(tradeResultForReview(review).net_pnl) >= 0,
  );
  const recentLossStreakCount = firstNonLossIndex < 0
    ? recentClosedTrades.length
    : firstNonLossIndex;
  const positionRows = openActivations.flatMap((summary, index) => {
    const detail = activationDetailQueries[index]?.data;
    const result = recordOf(detail?.trade_result);
    const attribution = recordOf(detail?.position_attribution);
    const attributedQuantity = Number(attribution.activation_signed_position);
    const resultQuantity = Number(result.position_quantity);
    const quantity = Number.isFinite(attributedQuantity)
      ? attributedQuantity
      : resultQuantity;
    if (!detail || !Number.isFinite(quantity) || quantity === 0) return [];
    const entryPrice = Number(result.average_entry_price);
    const decisionBasis = recordOf(detail.decision_basis);
    const directExecution = isDirectExecution(valueOf(decisionBasis, "kind", "STRATEGY_SIGNAL"));
    const strategyRef = directExecution
      ? valueOf(decisionBasis, "decision_basis_ref", "DIRECT_EXECUTION@1")
      : valueOf(recordOf(detail.strategy), "strategy_ref");
    const strategyId = strategyRef.split("@", 1)[0];
    const strategy = strategiesQuery.data?.find((item) => item.strategy_id === strategyId);
    const planName = valueOf(recordOf(detail.plan), "plan_name", valueOf(summary, "plan_name", ""));
    return [{
      summary,
      detail,
      result,
      quantity,
      entryPrice,
      strategyName: directExecution ? DIRECT_EXECUTION_LABEL : strategy?.display_name ?? strategyRef,
      decisionBasisRef: directExecution ? "" : strategyRef,
      planName,
    }];
  });
  const accountSnapshotStatus = data?.account_snapshot_status ?? "UNAVAILABLE";
  const accountPositions = accountSnapshotStatus === "CURRENT" ? data?.account_positions ?? [] : [];
  const accountOrders = accountSnapshotStatus === "CURRENT" ? data?.account_orders ?? [] : [];
  const accountPositionInstruments = new Set(
    accountPositions.map((position) => position.instrument_ref),
  );
  const displayedPositionCount = accountPositions.length + positionRows.filter(
    (position) => !accountPositionInstruments.has(position.summary.instrument_ref),
  ).length;
  const ordinaryOpenOrderCount = accountSnapshotStatus === "CURRENT" ? data?.account_ordinary_open_order_count ?? 0 : 0;
  const algoOpenOrderCount = accountSnapshotStatus === "CURRENT" ? data?.account_algo_open_order_count ?? 0 : 0;
  const displayedOrderCount = ordinaryOpenOrderCount + algoOpenOrderCount;
  const overviewInstrumentRef = positionRows[0]?.summary.instrument_ref ?? "";
  const expectedOverviewMarketSource = expectedMarketSourceForEnvironment(
    status.environment_kind,
  );
  const overviewMarketStream = usePublicMarketStream(
    Boolean(overviewInstrumentRef),
    overviewInstrumentRef,
    "1m",
    marketEnvironmentScopeKey(
      status.environment_kind,
      status.environment_id,
    ),
    expectedOverviewMarketSource,
  );
  const overviewMarketQuery = useQuery({
    queryKey: [
      "overview-market-context",
      status.environment_kind,
      status.environment_id,
      overviewInstrumentRef,
    ],
    queryFn: () => getMarketContext(overviewInstrumentRef, 20),
    enabled: Boolean(overviewInstrumentRef),
    retry: 1,
    retryDelay: 2_000,
    refetchInterval: 15_000,
  });
  const overviewLiveQuote = overviewMarketStream.status === "LIVE"
    && overviewMarketStream.quote?.source === expectedOverviewMarketSource
    && overviewMarketStream.quote.instrument_ref === overviewInstrumentRef
      ? overviewMarketStream.quote
      : null;
  const overviewMarketContext = overviewMarketQuery.data
    && isMarketSourceForEnvironment(
      overviewMarketQuery.data.source,
      status.environment_kind,
    )
      ? overviewMarketQuery.data
      : null;
  const overviewReferencePrice = overviewLiveQuote?.reference_price
    ?? overviewMarketContext?.reference_price
    ?? "";
  const overviewMarketCutoff = overviewLiveQuote?.source_cutoff
    ?? overviewMarketContext?.source_cutoff
    ?? "";
  const positionActivationIds = new Set(
    positionRows.map((position) => position.summary.activation_id),
  );
  const nonPositionActivations = openActivations.filter(
    (activation) => !positionActivationIds.has(activation.activation_id),
  );
  const defaultTab = (
    !activationsQuery.isPending
    && !reviewsQuery.isPending
    && openActivations.length === 0
    && displayedPositionCount === 0
    && displayedOrderCount === 0
    && recentClosedTrades.length > 0
  ) ? "TRADES" : "POSITIONS";
  const visibleTab = activeTab ?? defaultTab;
  return (
    <Box sx={{ width: "min(1120px, calc(100% - clamp(32px, 4vw, 48px)))", mx: "auto", py: { xs: 2, sm: 3 } }}>
      <Typography
        component="h1"
        sx={visuallyHiddenSx}
      >
        账户总览
      </Typography>
      {(query.isPending || activationsQuery.isPending || reviewsQuery.isPending || (Boolean(overviewInstrumentRef) && overviewMarketQuery.isPending) || activationDetailQueries.some((item) => item.isPending)) && <LinearProgress aria-label="正在读取总览" sx={{ mb: 1 }} />}
      {query.isError && (
        <Alert severity="error" variant="outlined" sx={{ mb: 3 }}>
          服务器事实当前不可确认。工作台没有把缓存显示为当前事实；请核对 PostgreSQL，必要时使用故障接管。
        </Alert>
      )}
      {environmentContextMismatch && (
        <Alert severity="error" variant="outlined">
          页面环境与账户事实不一致，已拒绝显示总览数据。请刷新页面并核对当前运行配置。
        </Alert>
      )}
      {data && !environmentContextMismatch && (
        <>
          <TradingDisciplineStrip overview={data} />
          <Stack
            component="section"
            direction={{ xs: "column", sm: "row" }}
            spacing={{ xs: 1, sm: 2 }}
            sx={{ justifyContent: "space-between", alignItems: { xs: "stretch", sm: "center" } }}
          >
            <Tabs variant="scrollable" scrollButtons="auto" value={visibleTab} onChange={(_event, value: "POSITIONS" | "ORDERS" | "TRADES") => setActiveTab(value)} aria-label="总览内容">
              <Tab value="POSITIONS" label={`当前仓位（${displayedPositionCount}）`} />
              <Tab value="ORDERS" label={`当前委托（${displayedOrderCount}）`} />
              <Tab value="TRADES" label="最近交易结果" />
            </Tabs>
            <Stack direction="row" spacing={1} sx={{ alignItems: "center", flexWrap: "wrap" }} aria-live="polite">
              <Typography variant="caption" color="text.secondary" sx={{ whiteSpace: "nowrap" }}>
                {data.account_snapshot_cutoff
                  ? `交易所更新于 ${formatUserVisibleTime(data.account_snapshot_cutoff)} · 每 30 秒同步`
                  : "等待交易所同步"}
              </Typography>
            </Stack>
          </Stack>
          {visibleTab === "POSITIONS" && (
            <Box component="section" aria-label="当前仓位" sx={{ mt: 2 }}>
              {activationsQuery.isError && <Alert severity="error">开放激活和仓位归属当前不可读；页面不显示缓存仓位。</Alert>}
              {!activationsQuery.isError && activationDetailQueries.some((item) => item.isPending) && <LinearProgress aria-label="正在读取当前仓位" />}
              {accountSnapshotStatus !== "CURRENT" && (
                <Alert severity={accountSnapshotStatus === "STALE" ? "warning" : "info"} variant="outlined" sx={{ mb: 2 }}>
                  账户数据正在同步，当前不展示历史仓位。
                </Alert>
              )}
              {accountSnapshotStatus === "CURRENT" && accountPositions.length === 0 && positionRows.length === 0 && (
                <Alert severity="info" variant="outlined">当前无持仓。</Alert>
              )}
              {accountSnapshotStatus === "CURRENT" && accountPositions.length > 0 && (
                <TableContainer
                  component="section"
                  className="table-scroll"
                  role="region"
                  aria-label="交易所账户当前仓位"
                  tabIndex={0}
                  sx={{ ...surfaceFrameSx, overflowX: "auto", mb: positionRows.length > 0 ? 2 : 0 }}
                >
                  <Table size="small" sx={{ minWidth: 940 }}>
                    <TableHead>
                      <TableRow>
                        <TableCell>合约</TableCell>
                        <TableCell>方向</TableCell>
                        <TableCell align="right">数量</TableCell>
                        <TableCell align="right">名义价值</TableCell>
                        <TableCell align="right">入场 / 标记价</TableCell>
                        <TableCell align="right">未实现盈亏</TableCell>
                        <TableCell>杠杆 / 强平</TableCell>
                        <TableCell align="right">操作</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {accountPositions.map((position) => {
                        const external = position.origin === "EXTERNAL_UNMANAGED";
                        const quantity = Number(position.absolute_quantity);
                        const notional = Math.abs(Number(position.notional));
                        const unrealizedPnl = Number(position.unrealized_pnl);
                        const liquidationPrice = Number(position.liquidation_price);
                        const baseAsset = position.symbol.replace(/USDT$/, "") || position.symbol;
                        const attributedPlans = positionRows.filter((candidate) => (
                          candidate.summary.instrument_ref === position.instrument_ref
                          && candidate.summary.direction === position.direction
                        ));
                        return (
                          <TableRow key={`${position.symbol}-${position.position_side}`} hover>
                            <TableCell sx={{ minWidth: 210 }}>
                              <Stack spacing={.5} sx={{ alignItems: "flex-start" }}>
                                <Typography component="span" sx={{ fontWeight: 800, fontSize: "inherit" }}>{position.instrument_ref}</Typography>
                                {external ? (
                                  <Chip size="small" variant="outlined" color="warning" label="外部 · 未接管" />
                                ) : attributedPlans.length > 0 ? (
                                  <Stack spacing={.25} sx={{ alignItems: "flex-start" }}>
                                    {attributedPlans.map((candidate) => (
                                      <Typography
                                        component={Link}
                                        key={candidate.summary.activation_id}
                                        to={`/activations/${candidate.summary.activation_id}`}
                                        aria-label={`查看计划 ${candidate.planName || candidate.summary.activation_id}`}
                                        variant="caption"
                                        sx={{
                                          color: "text.primary",
                                          fontWeight: 700,
                                          lineHeight: 1.25,
                                          textDecoration: "underline",
                                          textUnderlineOffset: 2,
                                          "&:visited": { color: "text.primary" },
                                          "&:hover": { color: "text.secondary" },
                                        }}
                                      >
                                        {candidate.planName || candidate.strategyName}
                                      </Typography>
                                    ))}
                                  </Stack>
                                ) : (
                                  <Typography variant="caption" color="warning.main">计划归属待核对</Typography>
                                )}
                              </Stack>
                            </TableCell>
                            <TableCell sx={{ whiteSpace: "nowrap" }}>
                              <MarketToneText tone={marketToneForDirection(position.direction)}>{position.direction === "LONG" ? "做多" : "做空"}</MarketToneText>
                              {position.position_side !== "BOTH" && <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>{position.position_side}</Typography>}
                            </TableCell>
                            <TableCell className="mono" align="right">{Number.isFinite(quantity) ? `${marketVolume(String(quantity))} ${baseAsset}` : "—"}</TableCell>
                            <TableCell className="mono" align="right">{Number.isFinite(notional) ? usdt(notional) : "—"}</TableCell>
                            <TableCell className="mono" align="right" sx={{ whiteSpace: "nowrap" }}>
                              {marketPrice(position.entry_price)}
                              <Typography component="span" variant="caption" color="text.secondary" sx={{ display: "block" }}>{marketPrice(position.mark_price)}</Typography>
                            </TableCell>
                            <TableCell className="mono" align="right">
                              {Number.isFinite(unrealizedPnl)
                                ? <FinancialToneText tone={financialToneForSignedValue(unrealizedPnl)}>{signedUsdt(unrealizedPnl)}</FinancialToneText>
                                : "—"}
                            </TableCell>
                            <TableCell sx={{ whiteSpace: "nowrap" }}>
                              {position.leverage}× · {position.margin_mode === "CROSS" ? "全仓" : "逐仓"}
                              <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                                {Number.isFinite(liquidationPrice) && liquidationPrice > 0 ? `强平 ${marketPrice(String(liquidationPrice))}` : "强平 —"}
                              </Typography>
                            </TableCell>
                            <TableCell align="right">
                              <Button size="small" variant="outlined" onClick={() => setPositionOperationTarget(position)}>
                                策略调整
                              </Button>
                            </TableCell>
                          </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>
                </TableContainer>
              )}
              {positionRows.length > 0 && (
                <Typography variant="h2" sx={{ mb: 1 }}>
                  Halpha 计划仓位（{positionRows.length}）
                </Typography>
              )}
              <ExpandableList
                items={positionRows}
                initialCount={4}
                step={4}
                spacing={1}
                renderItem={(position) => {
                  const instrument = position.summary.instrument_ref;
                  const direction = position.quantity > 0 ? "做多" : "做空";
                  const directionTone = marketToneForDirection(position.quantity > 0 ? "LONG" : "SHORT");
                  const baseAsset = instrument.replace(/USDT(?:-PERP)?$/, "") || instrument;
                  const protectionColor = position.summary.protection_state === "GAP"
                    ? "error"
                    : position.summary.protection_state === "UNKNOWN"
                      ? "warning"
                      : position.summary.protection_state === "WORKING" || position.summary.protection_state === "CLOSED"
                        ? "success"
                        : "default";
                  const matchedAccountPosition = accountPositions.find((candidate) => (
                    candidate.instrument_ref === instrument
                    && candidate.direction === position.summary.direction
                  ));
                  const accountMarkPrice = Number(matchedAccountPosition?.mark_price);
                  const parsedCurrentPrice = Number(overviewReferencePrice);
                  const currentPrice = Number.isFinite(accountMarkPrice) && accountMarkPrice > 0
                    ? accountMarkPrice
                    : instrument === overviewInstrumentRef
                    && Number.isFinite(parsedCurrentPrice)
                    && parsedCurrentPrice > 0
                      ? parsedCurrentPrice
                      : Number.NaN;
                  const notional = Number.isFinite(currentPrice)
                    ? Math.abs(position.quantity * currentPrice)
                    : null;
                  const priceCutoff = matchedAccountPosition?.fact_cutoff
                    ?? overviewMarketCutoff;
                  const marketAge = priceCutoff
                    ? relativeAgeLabel(priceCutoff, Date.now())
                    : "等待行情";
                  return (
                    <Box
                      component={Link}
                      to={`/activations/${position.summary.activation_id}`}
                      aria-label={`查看计划详情 ${position.planName || position.strategyName}`}
                      key={position.summary.activation_id}
                      sx={{
                        ...surfaceFrameSx,
                        p: { xs: 1.5, md: 2 },
                        color: "inherit",
                        cursor: "pointer",
                        textDecoration: "none",
                        transition: "border-color 120ms ease, box-shadow 120ms ease, transform 120ms ease",
                        "&:hover": {
                          borderColor: "primary.main",
                          boxShadow: "0 5px 16px rgba(16, 24, 32, 0.08)",
                          transform: "translateY(-1px)",
                        },
                        "&:focus-visible": {
                          borderColor: "primary.main",
                          outline: "3px solid",
                          outlineColor: "primary.light",
                          outlineOffset: 2,
                        },
                      }}
                    >
                      <Box
                        sx={{
                          display: "grid",
                          gridTemplateColumns: { xs: "minmax(0, 1fr)", md: "minmax(0, 1fr) minmax(300px, 360px)" },
                          gap: 2,
                          alignItems: "stretch",
                        }}
                      >
                        <Box sx={{ minWidth: 0 }}>
                          <Stack direction="row" spacing={1} sx={{ alignItems: "center", flexWrap: "wrap" }}>
                            <Typography
                              component="div"
                              className="mono"
                              sx={{ fontSize: { xs: "1.1rem", sm: "1.2rem" }, fontWeight: 850, lineHeight: 1.2 }}
                            >
                              <MarketToneText tone={directionTone}>{instrument}</MarketToneText>
                            </Typography>
                            <Box
                              component="span"
                              className={marketToneClassName(directionTone)}
                              sx={{
                                display: "inline-flex",
                                alignItems: "center",
                                px: 0.9,
                                py: 0.2,
                                border: "1px solid currentColor",
                                borderRadius: 999,
                                bgcolor: "action.hover",
                                fontSize: "0.8125rem",
                                lineHeight: 1.35,
                              }}
                            >
                              {direction}
                            </Box>
                          </Stack>
                          <Typography component="h3" variant="body1" sx={{ mt: 0.5, fontWeight: 750, lineHeight: 1.35 }}>
                            {position.planName || position.strategyName}
                          </Typography>
                          <Stack direction="row" spacing={0.75} sx={{ mt: 0.9, alignItems: "center", flexWrap: "wrap", rowGap: 0.75 }}>
                            <Chip size="small" color="success" variant="outlined" label="计划运行中" />
                            <Chip
                              size="small"
                              color={protectionColor}
                              variant="outlined"
                              label={position.summary.protection_state === "WORKING"
                                ? "保护有效"
                                : `保护 ${translatedLabel(protectionStateLabels, position.summary.protection_state)}`}
                            />
                            <Typography variant="caption" color="text.secondary">
                              {position.strategyName}
                            </Typography>
                          </Stack>
                          <Box
                            aria-label="计划仓位摘要"
                            sx={{
                              mt: 1.1,
                              display: "grid",
                              gridTemplateColumns: { xs: "repeat(2, minmax(0, 1fr))", sm: "repeat(3, minmax(0, 1fr))" },
                              gap: 0,
                              p: 0.4,
                              bgcolor: "action.hover",
                              borderRadius: 1.25,
                            }}
                          >
                            {[
                              ["持仓", `${marketVolume(String(Math.abs(position.quantity)))} ${baseAsset}`],
                              ["入场均价", Number.isFinite(position.entryPrice) ? marketPrice(String(position.entryPrice)) : "待成交事实"],
                              [matchedAccountPosition ? "标记价" : "参考价", Number.isFinite(currentPrice) ? marketPrice(String(currentPrice)) : "等待行情"],
                              ["名义金额", notional === null ? "等待行情" : usdt(notional)],
                              ["归属手续费", usdt(position.result.commission)],
                            ].map(([label, value]) => (
                              <Box key={label} sx={{ minWidth: 0, px: 1, py: 0.65 }}>
                                <Typography variant="caption" color="text.secondary" sx={{ display: "block", lineHeight: 1.2 }}>
                                  {label}
                                </Typography>
                                <Typography className="mono" variant="body2" sx={{ mt: 0.25, fontWeight: 750, lineHeight: 1.3 }}>
                                  {value}
                                </Typography>
                                {label === (matchedAccountPosition ? "标记价" : "参考价") && (
                                  <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.15, lineHeight: 1.2 }}>
                                    {marketAge}
                                  </Typography>
                                )}
                              </Box>
                            ))}
                          </Box>
                        </Box>
                        <PlanPnlPanel
                          activation={position.summary}
                          activationDetail={position.detail}
                          environmentKind={status.environment_kind}
                          environmentScope={environmentScope}
                          marketColorScheme={marketColorScheme}
                        />
                      </Box>
                    </Box>
                  );
                }}
              />
              {nonPositionActivations.length > 0 && (
                <Box sx={{ mt: 4 }}>
                  <Typography variant="h2" sx={{ mb: 1 }}>运行中的计划</Typography>
                  <ExpandableList
                    items={nonPositionActivations}
                    initialCount={4}
                    step={4}
                    renderItem={(activation) => {
                      const detail = activationDetailQueries[
                        openActivations.findIndex(
                          (item) => item.activation_id === activation.activation_id,
                        )
                      ]?.data;
                      const activationRecord = recordOf(activation);
                      const clauses = runtimeEntryConditionClauses(activationRecord);
                      const ruleState = recordOf(activationRecord.rule_state);
                      const entryValidUntil = valueOf(
                        recordOf(ruleState.deadlines),
                        "entry_valid_until",
                      );
                      const awaitingEntry = activationRecord.has_entry_fill !== true;
                      const openEntryAction = recordsOf(detail?.execution_actions).find(
                        (action) => valueOf(action, "action_kind") === "ENTRY"
                          && valueOf(action, "state") === "OPEN",
                      );
                      const entryTerms = recordOf(openEntryAction?.action_terms);
                      const entryActionRef = valueOf(openEntryAction, "execution_action_id");
                      const workingEntryFact = recordsOf(detail?.venue_facts).find((fact) => {
                        const payload = recordOf(fact.payload);
                        return valueOf(fact, "kind") === "ORDER_STATE"
                          && valueOf(fact, "action_ref") === entryActionRef
                          && valueOf(payload, "status") === "WORKING";
                      });
                      const entryPrice = Number(valueOf(entryTerms, "price"));
                      const entryQuantity = Number(valueOf(entryTerms, "quantity"));
                      const entryNotional = entryPrice * entryQuantity;
                      const venuePolicy = recordOf(
                        recordOf(entryTerms.execution_context).venue_policy,
                      );
                      const workingEntrySummary = openEntryAction
                        && Number.isFinite(entryPrice)
                        && Number.isFinite(entryQuantity)
                        ? [
                            workingEntryFact ? "交易所工作中" : "入场委托状态待确认",
                            venuePolicy.post_only === true ? "Maker" : "",
                            `${marketPrice(String(entryPrice))} USDT`,
                            `${marketVolume(String(entryQuantity))} BTC`,
                            Number.isFinite(entryNotional) ? usdt(entryNotional) : "",
                          ].filter(Boolean).join(" · ")
                        : "";
                      const clauseLabels = clauses.map((clause) => {
                        if (clause.kind === "MARK_PRICE") {
                          const comparator = clause.comparator === "GTE" ? "≥" : "≤";
                          return `标记价 ${comparator} ${marketPrice(clause.value)}`;
                        }
                        if (clause.kind === "CLOSED_BAR_PRICE_15M") {
                          const comparator = clause.comparator === "GTE" ? "≥" : "≤";
                          return `15m 收盘 ${comparator} ${marketPrice(clause.value)}`;
                        }
                        if (clause.kind === "SPREAD_BPS") {
                          return `价差 ≤ ${quoteAmount(clause.value)} bps`;
                        }
                        const comparator = clause.comparator === "GTE"
                          ? "上涨 ≥"
                          : clause.comparator === "DROP_GTE"
                            ? "下跌 ≥"
                            : clause.comparator === "ABS_GTE"
                              ? "绝对变动 ≥"
                              : "有符号变动 ≤";
                        return `${clause.windowSeconds ?? "?"} 秒${comparator} ${quoteAmount(clause.value)} bps`;
                      });
                      return (
                        <Box key={activation.activation_id} sx={{ ...surfaceFrameSx, p: 2 }}>
                          <Stack direction={{ xs: "column", sm: "row" }} spacing={2} sx={{ justifyContent: "space-between", alignItems: { sm: "center" } }}>
                            <Box sx={{ minWidth: 0 }}>
                              <Typography sx={{ fontWeight: 750 }}>{valueOf(activation, "plan_name", "未命名计划")}</Typography>
                              <Typography variant="body2">
                                {activation.instrument_ref} · <MarketToneText tone={marketToneForDirection(activation.direction)}>{translatedLabel(directionLabels, activation.direction)}</MarketToneText>
                                 {" · "}{awaitingEntry ? "等待入场" : "正在闭合"}
                               </Typography>
                               {awaitingEntry && workingEntrySummary && (
                                 <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: .5 }}>
                                   {workingEntrySummary}
                                 </Typography>
                               )}
                               {awaitingEntry && clauseLabels.length > 0 && (
                                <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: .5 }}>
                                  {clauseLabels.join(" · ")}
                                </Typography>
                              )}
                              <Typography variant="caption" color="text.secondary">
                                {awaitingEntry
                                  ? `当前未持仓 · 入场窗口${remainingTimeLabel(entryValidUntil, Date.now()).replace("剩余 ", "还剩 ")}`
                                  : `当前无归属持仓 · 保护 ${translatedLabel(protectionStateLabels, activation.protection_state)}`}
                              </Typography>
                            </Box>
                            <Button size="small" variant="outlined" onClick={() => navigate(`/activations/${activation.activation_id}`)}>
                              {awaitingEntry ? "查看条件与控制" : "查看闭合进度"}
                            </Button>
                          </Stack>
                        </Box>
                      );
                    }}
                  />
                </Box>
              )}
            </Box>
          )}
          {visibleTab === "ORDERS" && (
            <Box component="section" aria-label="当前委托" sx={{ mt: 2 }}>
              {accountSnapshotStatus !== "CURRENT" && (
                <Alert severity={accountSnapshotStatus === "STALE" ? "warning" : "info"} variant="outlined">
                  账户数据正在同步，当前不展示历史委托。
                </Alert>
              )}
              {accountSnapshotStatus === "CURRENT" && accountOrders.length === 0 && (
                <Alert severity="info" variant="outlined">当前无未结委托。</Alert>
              )}
              {accountSnapshotStatus === "CURRENT" && accountOrders.length > 0 && (
                <TableContainer
                  className="table-scroll"
                  role="region"
                  aria-label="交易所账户当前委托"
                  tabIndex={0}
                  sx={{ ...surfaceFrameSx, overflowX: "auto" }}
                >
                  <Table size="small" sx={{ minWidth: 980 }}>
                    <TableHead>
                      <TableRow>
                        <TableCell>合约</TableCell>
                        <TableCell>委托</TableCell>
                        <TableCell>方向</TableCell>
                        <TableCell align="right">价格 / 触发价</TableCell>
                        <TableCell align="right">数量 / 已成交</TableCell>
                        <TableCell>属性</TableCell>
                        <TableCell>状态</TableCell>
                        <TableCell>更新时间</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {accountOrders.map((order: AccountOrder) => {
                        const price = Number(order.price);
                        const triggerPrice = Number(order.trigger_price);
                        const quantity = order.quantity === null ? null : Number(order.quantity);
                        const executedQuantity = order.executed_quantity === null ? null : Number(order.executed_quantity);
                        const updateTime = order.source_update_time_ms ?? order.source_create_time_ms;
                        return (
                          <TableRow key={`${order.kind}-${order.order_id}`} hover>
                            <TableCell sx={{ whiteSpace: "nowrap", fontWeight: 800 }}>{order.instrument_ref}</TableCell>
                            <TableCell sx={{ whiteSpace: "nowrap" }}>
                              {translatedLabel(accountOrderTypeLabels, order.order_type)}
                              <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>{order.kind === "ALGO" ? "条件" : "普通"} · #{order.order_id}</Typography>
                            </TableCell>
                            <TableCell sx={{ whiteSpace: "nowrap" }}>
                              <MarketToneText tone={marketToneForDirection(order.side === "BUY" ? "LONG" : "SHORT")}>{order.side === "BUY" ? "买入" : "卖出"}</MarketToneText>
                              <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>{order.position_side}</Typography>
                            </TableCell>
                            <TableCell className="mono" align="right" sx={{ whiteSpace: "nowrap" }}>
                              {Number.isFinite(price) && price > 0 ? marketPrice(String(price)) : "市价"}
                              <Typography component="span" variant="caption" color="text.secondary" sx={{ display: "block" }}>
                                {Number.isFinite(triggerPrice) && triggerPrice > 0 ? marketPrice(String(triggerPrice)) : "—"}
                              </Typography>
                            </TableCell>
                            <TableCell className="mono" align="right" sx={{ whiteSpace: "nowrap" }}>
                              {quantity !== null && Number.isFinite(quantity) ? marketVolume(String(quantity)) : "全平"}
                              <Typography component="span" variant="caption" color="text.secondary" sx={{ display: "block" }}>
                                {executedQuantity !== null && Number.isFinite(executedQuantity) ? marketVolume(String(executedQuantity)) : "—"}
                              </Typography>
                            </TableCell>
                            <TableCell sx={{ whiteSpace: "nowrap" }}>
                              {[order.reduce_only ? "只减仓" : "", order.close_position ? "全平" : "", order.time_in_force ?? ""].filter(Boolean).join(" · ") || "—"}
                            </TableCell>
                            <TableCell>{translatedLabel(accountOrderStatusLabels, order.status)}</TableCell>
                            <TableCell className="mono" sx={{ whiteSpace: "nowrap" }}>
                              {updateTime === null ? "—" : formatUserVisibleTime(new Date(updateTime).toISOString())}
                            </TableCell>
                          </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>
                </TableContainer>
              )}
            </Box>
          )}
          {visibleTab === "TRADES" && (
            <Box component="section" aria-label="最近交易结果" sx={{ mt: 2 }}>
              <Typography color="text.secondary" variant="body2" sx={{ mb: 2 }}>只统计可完整计算的最近三笔闭合交易；净结果包含已归属资金费，无可确认记录时不作估算。</Typography>
              {reviewsQuery.isError && <Alert severity="warning">最近交易结果当前不可读；不显示缓存或估算值。</Alert>}
              {!reviewsQuery.isError && recentClosedTrades.length === 0 && <Alert severity="info" variant="outlined">当前还没有可完整计算净结果的闭合交易。</Alert>}
              {recentClosedTrades.length > 0 && <>
                {recentLossStreakCount >= LOSS_STREAK_ALERT_THRESHOLD && (
                  <Alert severity="warning" variant="outlined" sx={{ mb: 2 }}>
                    连续亏损 {recentLossStreakCount} 笔，已触发连续亏损提醒；新的完整闭合交易净结果大于或等于零时自动解除。
                  </Alert>
                )}
                <FactGrid facts={[
                  { label: "已计算交易", value: `${recentClosedTrades.length} 笔` },
                  { label: "合计净结果", value: signedUsdt(recentNetPnl), tone: financialToneForSignedValue(recentNetPnl) },
                  { label: "平均净结果", value: signedUsdt(recentAverageNetPnl), tone: financialToneForSignedValue(recentAverageNetPnl) },
                ]} />
                <Stack spacing={1.25} sx={{ mt: 2 }}>
                  {recentClosedTrades.map((review) => {
                    const result = tradeResultForReview(review);
                    const tradeContext = recordOf(review.trade_context);
                    const instrumentRef = valueOf(tradeContext, "instrument_ref");
                    const direction = valueOf(tradeContext, "direction");
                    const decisionBasisRef = valueOf(tradeContext, "decision_basis_ref");
                    const directExecution = decisionBasisRef === "DIRECT_EXECUTION@1";
                    const strategyId = valueOf(tradeContext, "strategy_id");
                    const strategy = strategiesQuery.data?.find((item) => item.strategy_id === strategyId);
                    const planName = valueOf(tradeContext, "plan_name");
                    const tradeAmount = valueOf(tradeContext, "trade_amount");
                    return <Box key={valueOf(review, "review_id")} sx={{ ...surfaceFrameSx, p: 2 }}>
                      <Stack direction={{ xs: "column", sm: "row" }} spacing={2} sx={{ justifyContent: "space-between", alignItems: { sm: "center" } }}>
                        <Box>
                          {planName && (
                            <Typography sx={{ fontWeight: 750 }}>{planName}</Typography>
                          )}
                          <Typography sx={{ fontWeight: 750 }}>
                            {instrumentRef || "交易对象待恢复"} · <MarketToneText tone={marketToneForDirection(direction)}>{translatedLabel(directionLabels, direction)}</MarketToneText> · <FinancialToneText tone={financialToneForSignedValue(result.net_pnl)}>{signedUsdt(result.net_pnl)}</FinancialToneText>
                          </Typography>
                          <Typography variant="body2" color="text.secondary">
                            {directExecution
                              ? DIRECT_EXECUTION_LABEL
                              : (strategy?.display_name ?? decisionBasisRef) || "决策依据待恢复"}
                            {" · "}入场成交额 {usdt(result.entry_notional)}
                            {tradeAmount ? ` · 计划上限 ${usdt(tradeAmount)}` : ""}
                          </Typography>
                          <Typography variant="caption" color="text.secondary">
                            {formatUserVisibleTime(valueOf(review, "fact_cutoff"))} · 手续费 <FinancialToneText tone={financialToneForSignedValue(-1)}>{signedUsdt(-Math.abs(Number(result.commission)))}</FinancialToneText>
                            {result.funding_included === true ? <> · 资金费 <FinancialToneText tone={financialToneForSignedValue(result.funding)}>{signedUsdt(result.funding)}</FinancialToneText></> : ""}
                          </Typography>
                        </Box>
                        <Button size="small" variant="outlined" onClick={() => navigate(`/activations/${valueOf(review, "activation_id")}`)}>查看计划与复盘</Button>
                      </Stack>
                    </Box>;
                  })}
                </Stack>
                <Button variant="text" sx={{ mt: 1.5 }} onClick={() => navigate("/reviews")}>查看全部交易与复盘</Button>
              </>}
            </Box>
          )}
        </>
      )}
      <AccountPositionOperationDialog
        position={positionOperationTarget}
        status={status}
        onClose={() => setPositionOperationTarget(null)}
      />
    </Box>
  );
}

function SettingsPage() {
  const { status, marketColorScheme, setMarketColorScheme } = useOutletContext<FrameContext>();
  const queryClient = useQueryClient();
  const [runtimeRecoveryCopyState, setRuntimeRecoveryCopyState] = useState<"IDLE" | "COPIED" | "FAILED">("IDLE");
  const buildConsistency = status.app_executor_product_build_consistent === null
    ? "未核对"
    : status.app_executor_product_build_consistent ? "一致" : "不一致";
  const productVersionMismatch = status.executor_status === "BUILD_MISMATCH"
    || status.app_executor_product_build_consistent === false;
  const runtimeNeedsRecovery = productVersionMismatch
    || (status.executor_status !== "READY" && status.profile !== "BINANCE_LIVE_READ_ONLY");
  const runtimeConfigPath = status.venue_account_type === "USDM_DEMO"
    ? "config\\halpha.toml"
    : status.venue_account_type === "USDM_COPY_LEAD"
      ? status.profile === "BINANCE_LIVE_READ_ONLY"
        ? "config\\halpha.live-copy-read-only.toml"
        : "config\\halpha.live-copy-write.toml"
      : status.profile === "BINANCE_LIVE_READ_ONLY"
        ? "config\\halpha.live-personal-read-only.toml"
        : "config\\halpha.live-personal-write.toml";
  const runtimeRecoveryCommands = [
    `.\\.venv\\Scripts\\halpha-control.exe stop product --config ${runtimeConfigPath}`,
    `.\\.venv\\Scripts\\halpha-control.exe start product --config ${runtimeConfigPath} --timeout-seconds 60`,
  ].join("\n");
  const emailMutation = useMutation({
    mutationFn: sendTestEmail,
  });
  const copyRuntimeRecoveryCommands = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(runtimeRecoveryCommands);
      setRuntimeRecoveryCopyState("COPIED");
    } catch {
      setRuntimeRecoveryCopyState("FAILED");
    }
  }, [runtimeRecoveryCommands]);
  const refreshRuntimeStatus = useCallback(async () => {
    await queryClient.refetchQueries({ queryKey: STATUS_QUERY_KEY, exact: true });
  }, [queryClient]);
  return (
    <Box sx={{ width: "min(1120px, calc(100% - clamp(32px, 4vw, 48px)))", mx: "auto", py: { xs: 2.5, sm: 3 } }}>
      <PageHeader
        title="运行与产品版本"
        description={status.environment_kind === "LIVE"
          ? "显示服务、产品版本和交易所变更请求状态；凭据值不会进入浏览器。"
          : "显示服务和产品版本状态；凭据值不会进入浏览器。"}
      />
      {!status.database_available && <Alert severity="error" variant="outlined" sx={{ mb: 3 }}>数据库不可用；事实截止点未知。读取失败时不得向交易所提交变更请求。</Alert>}
      {productVersionMismatch && (
        <Alert severity="warning" variant="outlined" sx={{ mb: 3 }}>
          App 与执行器仍在运行不同版本的代码。通常是更新后只重启了其中一项；为避免新旧逻辑混用，Halpha 不会把新的控制请求视为已在交易所执行。
        </Alert>
      )}
      {!productVersionMismatch && status.executor_status !== "READY" && status.profile !== "BINANCE_LIVE_READ_ONLY" && (
        <Alert severity="warning" variant="outlined" sx={{ mb: 3 }}>
          执行器当前{translatedLabel(executorStatusLabels, status.executor_status)}。控制命令只能先持久化，不能视为已在交易所执行；若有持仓或挂单，请在 Binance 官方入口核对和接管。
        </Alert>
      )}
      {runtimeNeedsRecovery && (
        <Box component="section" sx={{ ...surfaceFrameSx, p: 2, mb: 3 }}>
          <Typography sx={{ fontWeight: 750 }}>恢复运行服务</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: .5 }}>
            在本机仓库目录打开 PowerShell，依次运行下面两条命令。停止期间若有持仓或挂单，请同时在 Binance 官方入口核对；启动完成后返回此页重新检查。
          </Typography>
          <Box
            component="pre"
            className="mono"
            sx={{ m: 0, mt: 1.5, p: 1.5, overflowX: "auto", bgcolor: "action.hover", borderRadius: 1, fontSize: 13 }}
          >
            {runtimeRecoveryCommands}
          </Box>
          <Stack direction={{ xs: "column", sm: "row" }} spacing={1} sx={{ mt: 1.5 }}>
            <Button variant="outlined" onClick={() => void copyRuntimeRecoveryCommands()}>
              {runtimeRecoveryCopyState === "COPIED" ? "已复制" : runtimeRecoveryCopyState === "FAILED" ? "复制失败" : "复制重启命令"}
            </Button>
            <Button variant="outlined" onClick={() => void refreshRuntimeStatus()}>重新检查</Button>
          </Stack>
        </Box>
      )}
      <FactGrid facts={[
        { label: "本机监听", value: `${status.bind}:${status.port}` },
        { label: "数据库", value: status.database_available ? "可用" : "未知" },
        { label: "执行器", value: translatedLabel(executorStatusLabels, status.executor_status), note: `核对于 ${formatUserVisibleTime(status.executor_status_checked_at)}` },
        { label: "产品版本", value: shortDigest(status.product_build_id) },
        { label: "应用 / 执行器产品版本", value: buildConsistency },
        ...(status.environment_kind === "LIVE" ? [
          { label: "真实账户交易配置", value: translatedLabel(gateStateLabels, status.configured_runtime_real_write_gate) },
          { label: "当前真实账户交易", value: translatedLabel(gateStateLabels, status.runtime_real_write_gate) },
        ] : []),
        { label: "邮件投递", value: `${status.email_configuration_status === "CONFIGURED" ? "已配置" : "未配置"} · ${status.email_delivery_enabled ? "已启用" : "已停用"}` },
        { label: "视图取得时间", value: formatUserVisibleTime(status.view_retrieved_at) },
      ]} />
      {status.environment_kind === "LIVE" && status.live_write_gate_violations.length > 0 && (
        <Box component="section" sx={{ mt: 4 }}>
          <Typography variant="h2" sx={{ mb: 2 }}>交易所变更请求边界核对结果</Typography>
          <ExpandableList
            items={status.live_write_gate_violations}
            initialCount={8}
            step={8}
            spacing={1}
            renderItem={(violation) => <Typography key={violation} className="mono" variant="body2" color="text.secondary">{violation}</Typography>}
          />
        </Box>
      )}
      <Box component="section" sx={{ mt: 4, maxWidth: 720 }}>
        <Typography variant="h2" sx={{ mb: .5 }}>涨跌配色</Typography>
        <Typography variant="body2" color="text.secondary">只改变视觉映射，不改变方向、盈亏数值或交易行为；偏好保存在当前浏览器。</Typography>
        <Stack direction={{ xs: "column", sm: "row" }} spacing={1} sx={{ mt: 1.5 }}>
          <Button
            variant={marketColorScheme === DEFAULT_MARKET_COLOR_SCHEME ? "contained" : "outlined"}
            aria-pressed={marketColorScheme === DEFAULT_MARKET_COLOR_SCHEME}
            onClick={() => setMarketColorScheme("RED_DOWN_GREEN_UP")}
          >
            红跌绿涨
          </Button>
          <Button
            variant={marketColorScheme === "RED_UP_GREEN_DOWN" ? "contained" : "outlined"}
            aria-pressed={marketColorScheme === "RED_UP_GREEN_DOWN"}
            onClick={() => setMarketColorScheme("RED_UP_GREEN_DOWN")}
          >
            红涨绿跌
          </Button>
        </Stack>
        <Stack direction={{ xs: "column", sm: "row" }} spacing={{ xs: .5, sm: 2 }} sx={{ mt: 1.5 }} aria-label="当前涨跌配色预览">
          <Typography variant="body2" className="market-tone-up">上涨 · 做多 · +1.00 USDT</Typography>
          <Typography variant="body2" className="market-tone-down">下跌 · 做空 · -1.00 USDT</Typography>
        </Stack>
      </Box>
      <Box component="section" sx={{ mt: 4, maxWidth: 720 }}>
        <Typography variant="h2" sx={{ mb: 1 }}>实际测试邮件</Typography>
        <Typography color="text.secondary" sx={{ mb: 2 }}>只发送只读连通性消息，不包含秘密、完整账户信息、资本授权或可执行命令；投递结果不改变业务状态。</Typography>
        {!status.email_delivery_enabled && <Alert severity="info" variant="outlined" sx={{ mb: 2 }}>当前配置未启用 SMTP。系统不会使用隐式或仓库内代理配置绕过。</Alert>}
        {emailMutation.isSuccess && <Alert severity="success" sx={{ mt: 2 }}>测试邮件已由 SMTP transport 投递；未改变任何交易或资本状态。</Alert>}
        {emailMutation.isError && <Alert severity="error" sx={{ mt: 2 }}>测试邮件未确认：{emailMutation.error instanceof ApiFailure ? emailMutation.error.code : "UNKNOWN"}</Alert>}
        <Button variant="outlined" sx={{ mt: 2 }} disabled={!status.email_delivery_enabled || emailMutation.isPending} onClick={() => emailMutation.mutate()}>发送测试邮件</Button>
      </Box>
    </Box>
  );
}

function PlansPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { status, marketColorScheme } = useOutletContext<FrameContext>();
  const liveReadOnly = status.profile === "BINANCE_LIVE_READ_ONLY";
  const environmentScope = `${status.environment_kind}:${status.environment_id}`;
  const [activeTab, setActiveTab] = useState<"CURRENT" | "HISTORY">("CURRENT");
  const [deleteTarget, setDeleteTarget] = useState<PlanSummary | null>(null);
  const query = useQuery({ queryKey: ["plans"], queryFn: getPlans });
  const overviewQuery = useQuery({
    queryKey: ["overview"],
    queryFn: getOverview,
    refetchInterval: 5_000,
  });
  const activationsQuery = useQuery({
    queryKey: ["activations"],
    queryFn: getActivations,
    refetchInterval: 30_000,
  });
  const strategiesQuery = useQuery({ queryKey: ["strategies"], queryFn: getStrategies });
  const deleteMutation = useMutation({
    mutationFn: (plan: PlanSummary) => deletePlan(plan.plan_id, plan.draft_version),
    onSuccess: async (_result, deletedPlan) => {
      queryClient.setQueryData<PlanSummary[]>(["plans"], (current) =>
        current?.filter((plan) => plan.plan_id !== deletedPlan.plan_id)
      );
      setDeleteTarget(null);
      await queryClient.invalidateQueries({ queryKey: ["plans"] });
    },
  });
  const plans = query.data ?? [];
  const planEntryDiscipline = overviewQuery.data?.new_risk_discipline;
  const planEntryDisciplineConsequence = liveReadOnly
    ? "当前入口也处于只读公开行情模式，不能创建或保存计划；即使日后恢复可写，提交并启动仍会以服务端当前事实重新核对。"
    : "创建入口仍可用于准备或保存草稿；提交并启动会再次核对服务端当前事实，不能借此预留或绕过新增风险容量。";
  const {
    currentActivations,
    currentPlans,
    historicalPlans,
  } = planWorkbenchSections(
    plans,
    activationsQuery.data ?? [],
    Date.now(),
  );
  const latestActivationMap = useMemo(
    () => latestActivationsByPlanVersion(activationsQuery.data ?? []),
    [activationsQuery.data],
  );
  const plansByVersion = useMemo(
    () => new Map(
      plans.flatMap((plan) => (
        plan.plan_version_id ? [[plan.plan_version_id, plan] as const] : []
      )),
    ),
    [plans],
  );
  const renderActiveActivation = (activation: ActivationSummary) => {
    const planName = activation.plan_name?.trim()
      || `运行计划 · ${shortDigest(activation.activation_id)}`;
    const entryPhaseClosed = activationEntryPhaseClosed(activation, Date.now());
    const isPaused = activation.run_state === "PAUSED" && !entryPhaseClosed;
    const directionTone = marketToneForDirection(activation.direction);
    const protectionColor = activation.protection_state === "GAP"
      ? "error"
      : activation.protection_state === "UNKNOWN"
        ? "warning"
        : activation.protection_state === "WORKING" || activation.protection_state === "CLOSED"
          ? "success"
          : "default";
    const sourcePlan = plansByVersion.get(activation.plan_version_ref);
    const positionAlignment = sourcePlan?.position_alignment
      ?? activation.position_alignment;
    const positionDisposition = Boolean(positionAlignmentIntent(positionAlignment));
    const orderIntent = positionAlignmentIntent(positionAlignment)
      ?? orderScheduleIntent(
        sourcePlan?.order_schedule_spec ?? null,
        activation.order_schedule_snapshot,
      );
    return (
      <Box
        component={Link}
        to={`/activations/${activation.activation_id}`}
        aria-label={`查看计划详情 ${planName}`}
        key={activation.activation_id}
        sx={{
          ...surfaceFrameSx,
          p: 2.5,
          borderColor: isPaused ? "warning.main" : "divider",
          color: "inherit",
          cursor: "pointer",
          textDecoration: "none",
          transition: "border-color 120ms ease, box-shadow 120ms ease, transform 120ms ease",
          "&:hover": {
            borderColor: isPaused ? "warning.dark" : "primary.main",
            boxShadow: "0 5px 16px rgba(16, 24, 32, 0.08)",
            transform: "translateY(-1px)",
          },
          "&:focus-visible": {
            borderColor: "primary.main",
            outline: "3px solid",
            outlineColor: "primary.light",
            outlineOffset: 2,
          },
        }}
      >
        <Box
          sx={{
            display: "grid",
            gridTemplateColumns: { xs: "minmax(0, 1fr)", md: "minmax(0, 1fr) minmax(300px, 360px)" },
            gap: 2,
            alignItems: "stretch",
          }}
        >
          <Box sx={{ minWidth: 0 }}>
            <Stack direction="row" spacing={1} sx={{ alignItems: "center", flexWrap: "wrap" }}>
              <Typography
                component="div"
                className="mono"
                sx={{
                  mr: 0.25,
                  fontSize: { xs: "1.2rem", sm: "1.35rem" },
                  fontWeight: 850,
                  letterSpacing: "-0.025em",
                  lineHeight: 1.2,
                }}
              >
                <MarketToneText tone={directionTone}>{activation.instrument_ref}</MarketToneText>
              </Typography>
              <Box
                component="span"
                className={marketToneClassName(directionTone)}
                sx={{
                  display: "inline-flex",
                  alignItems: "center",
                  px: 1,
                  py: 0.25,
                  border: "1px solid currentColor",
                  borderRadius: 999,
                  bgcolor: "action.hover",
                  fontSize: "0.8125rem",
                  lineHeight: 1.35,
                }}
              >
                {activation.direction === "LONG" ? "做多" : "做空"}
              </Box>
            </Stack>
            <Typography
              component="h2"
              variant="body1"
              sx={{ mt: 0.6, fontWeight: 750, lineHeight: 1.35, color: "text.primary" }}
            >
              {planName}
            </Typography>
            <Stack
              direction="row"
              spacing={0.75}
              sx={{ mt: 1.25, alignItems: "center", flexWrap: "wrap", rowGap: 0.75 }}
            >
              <Chip
                size="small"
                color={activation.lifecycle === "RUNNING" ? "success" : "warning"}
                variant="outlined"
                label={translatedLabel(lifecycleLabels, activation.lifecycle)}
              />
              <Tooltip
                arrow
                title={entryPhaseClosed
                  ? "入场阶段已按原计划结束；已有持仓继续执行保护和退出。再次开放入场属于计划变更，不会由连续性恢复代替。"
                  : isPaused
                    ? "执行器连接曾中断，系统暂停本计划继续开仓、加仓和入场重挂；已有保护、撤单、减仓和退出继续。"
                    : "计划执行链正常，可按已确认条件继续处理入场、保护和退出。"}
              >
                <Chip
                  size="small"
                  color={entryPhaseClosed ? "default" : isPaused ? "warning" : "success"}
                  variant="outlined"
                  label={entryPhaseClosed
                    ? "入场已结束"
                    : translatedLabel(runStateLabels, activation.run_state)}
                />
              </Tooltip>
              <Chip
                size="small"
                color={protectionColor}
                variant="outlined"
                label={activation.protection_state === "WORKING"
                  ? "保护有效"
                  : `保护 ${translatedLabel(protectionStateLabels, activation.protection_state)}`}
              />
              <Typography
                variant="caption"
                color="text.secondary"
                sx={{ ml: { sm: "auto !important" }, whiteSpace: "nowrap" }}
              >
                更新 {formatCompactUserVisibleTime(activation.updated_at)}
              </Typography>
            </Stack>
            {sourcePlan && (
              <Box
                sx={{
                  mt: 1.25,
                  p: 1.25,
                  display: "grid",
                  gridTemplateColumns: { xs: "minmax(0, 1fr)", sm: "minmax(0, 2fr) minmax(130px, 0.8fr)" },
                  gap: 1.25,
                  bgcolor: "action.hover",
                  borderRadius: 1.5,
                }}
              >
                <Box sx={{ minWidth: 0 }}>
                  <Typography variant="caption" color="text.secondary">计划结构</Typography>
                  <Typography variant="body2" sx={{ mt: 0.2, fontWeight: 700, lineHeight: 1.4 }}>
                    {orderIntent ?? "订单意图不可读"}
                  </Typography>
                </Box>
                <Box sx={{ minWidth: 0 }}>
                  <Typography variant="caption" color="text.secondary">
                    {positionDisposition ? "处置边界" : "计划金额"}
                  </Typography>
                  <Typography className="mono" variant="body2" sx={{ mt: 0.2, fontWeight: 750 }}>
                    {quoteCurrencyAmount(sourcePlan.max_notional)} USDT
                  </Typography>
                </Box>
              </Box>
            )}
            {sourcePlan && !positionDisposition ? (
              <PlanAiReviewRecord
                review={sourcePlan.ai_review}
                reviewRef={sourcePlan.ai_review_ref}
                required={Boolean(sourcePlan.ai_review_ref)}
                compact
                sx={{ mt: 1.25 }}
              />
            ) : null}
            {isPaused && (
              <Typography variant="body2" color="warning.main" sx={{ mt: 1 }}>
                执行器连接中断后，新的入场已暂停；现有止损、止盈和退出不受影响。可在详情中恢复。
              </Typography>
            )}
          </Box>
          <PlanPnlPanel
            activation={activation}
            environmentKind={status.environment_kind}
            environmentScope={environmentScope}
            marketColorScheme={marketColorScheme}
            positionDisposition={positionDisposition}
          />
        </Box>
      </Box>
    );
  };
  const renderPlan = (plan: (typeof plans)[number]) => {
    const planRecord = recordOf(plan);
    const decisionContext = recordOf(planRecord.decision_context);
    const decisionBasisKind = valueOf(planRecord, "decision_basis_kind", "STRATEGY_SIGNAL");
    const directExecution = isDirectExecution(decisionBasisKind);
    const positionAlignment = recordOf(planRecord.position_alignment);
    const positionDisposition = Object.keys(positionAlignment).length > 0;
    const decisionBasisRef = valueOf(
      planRecord,
      "decision_basis_ref",
      directExecution ? "DIRECT_EXECUTION@1" : String(plan.strategy_id ?? ""),
    );
    const strategy = directExecution
      ? undefined
      : strategiesQuery.data?.find((item) => item.strategy_id === plan.strategy_id);
    const planParameters = plan.parameters ?? {};
    const keyParameterFacts = strategy?.plan_key_parameters?.map((definition) => ({
      label: definition.label,
      value: formatPlanKeyParameter(definition, planParameters[definition.parameter_key]),
    })) ?? [];
    const previousProductBuild = Boolean(
      plan.status !== "DRAFT" && plan.product_build_consistent === false,
    );
    const runtimeIncompatible = plan.runtime_compatible === false;
    const expired = Boolean(
      plan.status !== "DRAFT"
      && plan.valid_until
      && Date.parse(plan.valid_until) <= Date.now(),
    );
    const latestActivation = plan.activation_id
      ? (activationsQuery.data ?? []).find(
        (activation) => activation.activation_id === plan.activation_id,
      )
      : plan.plan_version_id
        ? latestActivationMap.get(plan.plan_version_id)
        : undefined;
    const completedActivation = plan.status === "ENDED";
    const unavailable = runtimeIncompatible || (expired && !completedActivation);
    const historical = plan.status === "ENDED";
    const planState = plan.status === "DRAFT"
      ? "草稿"
      : plan.status === "RUNNING"
        ? "运行中"
        : "已结束";
    const planName = plan.plan_name?.trim() || `未命名计划 · ${shortDigest(plan.plan_id)}`;
    const creatorLabel = plan.creator_kind === "AI"
      ? "AI 创建"
      : plan.creator_kind === "HUMAN"
        ? "人工创建"
        : plan.creator_kind === "MONITOR" ? "Monitor 创建" : "创建来源未知";
    const creationTime = plan.created_at
      ? `创建于 ${formatUserVisibleTime(plan.created_at)}`
      : "创建时间未知";
    const detailPath = latestActivation
      ? `/activations/${latestActivation.activation_id}`
      : plan.activation_id
        ? `/activations/${plan.activation_id}`
        : `/plans/${plan.plan_id}/edit`;
    const tradeResult = recordOf(latestActivation?.trade_result);
    const primaryResult = latestActivation?.primary_result ?? "";
    const netPnl = finiteNumber(tradeResult.net_pnl);
    const resultAvailable = tradeResult.calculation_complete === true
      && tradeResult.closed === true
      && netPnl !== null;
    const finalResult = primaryResult === "NO_ACTION"
      ? "0"
      : resultAvailable ? String(netPnl) : null;
    const closureReason = latestActivation
      ? activationSummaryCloseReason(recordOf(latestActivation))
      : completedActivation ? "结束详情待读取" : "尚未运行";
    const orderIntent = positionAlignmentIntent(positionAlignment)
      ?? orderScheduleIntent(
        plan.order_schedule_spec,
        latestActivation?.order_schedule_snapshot,
      );
    const openDetails = () => navigate(detailPath);
    return <Box component="article" aria-label={`计划 ${planName}`} key={plan.plan_id} sx={{ ...surfaceFrameSx, p: 2.5, borderColor: unavailable ? "warning.main" : "divider", opacity: unavailable ? .72 : 1 }}>
      <Box
        sx={{
          display: "grid",
          gridTemplateColumns: {
            xs: "minmax(0, 1fr)",
            md: historical
              ? "minmax(0, 1fr) minmax(300px, 360px)"
              : "minmax(0, 1fr) auto",
          },
          gap: 2,
          alignItems: "start",
        }}
      >
        <Box sx={{ minWidth: 0 }}>
          <Typography variant="overline" color={unavailable ? "warning.main" : "text.secondary"}>{planState}</Typography>
          <Button
            variant="text"
            onClick={openDetails}
            aria-label={`查看计划详情 ${planName}`}
            sx={{
              display: "flex",
              justifyContent: "flex-start",
              minWidth: 0,
              p: 0,
              mt: .5,
              color: "text.primary",
              textAlign: "left",
              textTransform: "none",
              "&:hover": { bgcolor: "transparent", textDecoration: "underline" },
            }}
          >
            <Typography variant="h2">{planName}</Typography>
          </Button>
          <Typography variant="body2" color="text.secondary">
            {positionDisposition
              ? positionAlignmentOperationLabel(positionAlignment)
              : directExecution ? DIRECT_EXECUTION_LABEL : strategy?.display_name ?? plan.strategy_id} · <Box component="span" className="mono">{plan.instrument_ref}</Box> · <MarketToneText tone={marketToneForDirection(plan.direction)}>{plan.direction === "LONG" ? "做多" : "做空"}</MarketToneText> · {plan.status === "DRAFT" ? `草稿 v${plan.draft_version}` : plan.status === "RUNNING" ? "运行中" : "已结束"}
          </Typography>
          <Typography variant="body2" sx={{ mt: .75, fontWeight: 700 }}>
            {orderIntent ?? "订单意图不可读"}
            {" · "}
            {positionDisposition ? "处置边界" : "计划金额"} <Box component="span" className="mono">{quoteCurrencyAmount(plan.max_notional)} USDT</Box>
          </Typography>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: .25 }}>
            最终结果{" "}
            {finalResult === null
              ? completedActivation ? "待核对" : "尚未结束"
              : <FinancialToneText tone={financialToneForSignedValue(finalResult)}><Box component="span" className="mono" sx={{ fontWeight: 750 }}>{signedSettledUsdt(primaryResult === "NO_ACTION" ? "0" : tradeResult.net_pnl)}</Box></FinancialToneText>}
            {" · "}
            {closureReason}
          </Typography>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: .25 }}>
            {creatorLabel} · {creationTime}
            {previousProductBuild && plan.runtime_compatible === true ? " · 较早构建确认，当前运行兼容" : ""}
          </Typography>
          {plan.status !== "DRAFT" && !positionDisposition ? (
            <PlanAiReviewRecord
              review={plan.ai_review}
              reviewRef={plan.ai_review_ref}
              required={Boolean(plan.ai_review_ref)}
              compact
              sx={{ mt: 1 }}
            />
          ) : null}
          {runtimeIncompatible && <Typography variant="body2" color="warning.main" sx={{ mt: 1 }}>
            {planRuntimeIncompatibilityLabels[plan.runtime_incompatibility_reason ?? ""] ?? "当前运行时无法安全消费该计划"}；仍可查看原计划或沿用参数新建。
          </Typography>}
          {!runtimeIncompatible && expired && !completedActivation && <Typography variant="body2" color="warning.main" sx={{ mt: 1 }}>计划有效期已结束；仍可查看原计划或沿用参数新建。</Typography>}
        </Box>
        <Stack spacing={1.25} sx={{ minWidth: 0 }}>
          {historical && (latestActivation
            ? <PlanPnlPanel
                activation={latestActivation}
                environmentKind={status.environment_kind}
                environmentScope={environmentScope}
                marketColorScheme={marketColorScheme}
                positionDisposition={positionDisposition}
              />
            : <EmptyHistoricalPnlPanel />)}
          <Stack
            direction={{ xs: "column", sm: "row" }}
            spacing={1}
            sx={{ justifyContent: "flex-end", "& > *": { minWidth: 0 } }}
          >
            <Button variant="outlined" onClick={openDetails}>查看详情</Button>
            {plan.status === "DRAFT" && <Button variant="outlined" disabled={liveReadOnly} onClick={() => navigate(`/plans/${plan.plan_id}/edit`)}>编辑草稿</Button>}
            {plan.status === "DRAFT" && <Button variant="outlined" color="error" disabled={liveReadOnly || deleteMutation.isPending} onClick={() => { deleteMutation.reset(); setDeleteTarget(plan); }}>删除草稿</Button>}
            {plan.status !== "DRAFT" && <Button variant="outlined" disabled={liveReadOnly} onClick={() => navigate(`/plans/new?copyFrom=${encodeURIComponent(plan.plan_id)}`)}>沿用参数新建</Button>}
          </Stack>
        </Stack>
      </Box>
      <Box component="details" sx={{ mt: 1.5, borderTop: 1, borderColor: "divider", pt: 1.25 }}>
        <Box component="summary" sx={{ display: "inline-flex", cursor: "pointer", color: "info.main", fontSize: 13, fontWeight: 700 }}>
          计划配置
        </Box>
        <Box sx={{ mt: 1.5 }}>
          <FactGrid
            columns={3}
            dense
            facts={[
              { label: "交易金额", value: `${quoteCurrencyAmount(plan.max_notional)} USDT`, note: "本计划的资金边界" },
              { label: "计划有效期", value: planDurationMinutes(plan.valid_from, plan.valid_until), note: `截至 ${formatUserVisibleTime(plan.valid_until)}` },
              ...(positionDisposition ? [
                { label: "决策依据", value: positionAlignmentOperationLabel(positionAlignment), note: decisionBasisRef },
                { label: "账户快照", value: formatUserVisibleTime(valueOf(positionAlignment, "fact_cutoff")), note: shortDigest(valueOf(positionAlignment, "snapshot_ref")) },
                { label: "基线 / 处置", value: `${marketVolume(valueOf(positionAlignment, "baseline_quantity"))} / ${marketVolume(valueOf(positionAlignment, "requested_reduction_quantity"))}` },
                { label: "处置后目标", value: marketVolume(valueOf(positionAlignment, "target_quantity_after")), note: "既有入场不计为 Halpha ENTRY" },
              ] : directExecution ? [
                { label: "决策依据", value: DIRECT_EXECUTION_LABEL, note: decisionBasisRef },
                { label: "订单计划", value: orderScheduleSummary(planRecord.order_schedule_spec) },
                { label: "入场条件", value: orderConditionSummary(planRecord.order_schedule_spec) },
                { label: "保护与退出", value: orderProtectionSummary(planRecord.order_schedule_spec) },
              ] : []),
              ...keyParameterFacts,
              ...(Object.keys(decisionContext).length > 0 ? [
                { label: "交易理由", value: valueOf(decisionContext, "rationale") },
                { label: "依据与证据", value: valueOf(decisionContext, "evidence") },
                { label: "已知局限", value: valueOf(decisionContext, "limitations") },
              ] : []),
            ]}
          />
          {!directExecution && !strategy && (
            <Typography variant="caption" color="warning.main" sx={{ display: "block", mt: 1 }}>
              策略关键参数定义当前不可读；页面未猜测或重写参数含义。
            </Typography>
          )}
        </Box>
      </Box>
    </Box>;
  };
  if (query.isPending || activationsQuery.isPending) {
    return (
      <Box sx={{ width: "min(1120px, calc(100% - clamp(32px, 4vw, 48px)))", mx: "auto", py: { xs: 2, sm: 3 } }}>
        <Typography component="h1" sx={visuallyHiddenSx}>交易计划</Typography>
        <LinearProgress aria-label="正在读取计划与运行状态" />
      </Box>
    );
  }
  if (query.isError || activationsQuery.isError) {
    return (
      <Box sx={{ width: "min(1120px, calc(100% - clamp(32px, 4vw, 48px)))", mx: "auto", py: { xs: 2, sm: 3 } }}>
        <Typography component="h1" sx={visuallyHiddenSx}>交易计划</Typography>
        <Alert severity="error">
          计划或运行状态当前不可读；页面不会以零计划或历史记录替代真实事实。
        </Alert>
        <Button
          variant="outlined"
          sx={{ mt: 2 }}
          onClick={() => {
            void query.refetch();
            void activationsQuery.refetch();
          }}
        >
          重新读取
        </Button>
      </Box>
    );
  }
  return (
    <Box sx={{ width: "min(1120px, calc(100% - clamp(32px, 4vw, 48px)))", mx: "auto", py: { xs: 2, sm: 3 } }}>
      <Typography
        component="h1"
        sx={visuallyHiddenSx}
      >
        交易计划
      </Typography>
      <Stack
        direction={{ xs: "column", md: "row" }}
        spacing={{ xs: 1.5, md: 2 }}
        sx={{ justifyContent: "space-between", alignItems: { xs: "stretch", md: "center" }, mb: 2 }}
      >
        <Tabs value={activeTab} onChange={(_event, value: "CURRENT" | "HISTORY") => setActiveTab(value)} aria-label="计划范围">
          <Tab value="CURRENT" label={`当前计划（${currentActivations.length + currentPlans.length}）`} />
          <Tab value="HISTORY" label={`历史计划（${historicalPlans.length}）`} />
        </Tabs>
        <Stack direction={{ xs: "column", sm: "row" }} spacing={1}>
          <Button variant="contained" disabled={liveReadOnly} onClick={() => navigate("/plans/new?mode=direct")}>直接执行</Button>
          <Button variant="outlined" disabled={liveReadOnly} onClick={() => navigate("/plans/new")}>选择策略</Button>
          <Button
            variant="outlined"
            onClick={() => {
              void query.refetch();
              void overviewQuery.refetch();
              void activationsQuery.refetch();
              void strategiesQuery.refetch();
            }}
          >
            刷新
          </Button>
        </Stack>
      </Stack>
      {liveReadOnly && (
        <Alert severity="info" variant="outlined" sx={{ mb: 2 }}>
          当前实盘入口为只读公开行情模式；计划事实仅供查看，创建、修改、提交并启动和控制均已关闭。
        </Alert>
      )}
      {overviewQuery.isError && (
        <Alert severity="warning" variant="outlined" sx={{ mb: 2 }}>
          账户级新增风险状态当前不可读取。创建入口不能据此放行；恢复完整账户快照后，仍须以提交并启动时的服务端当前事实为准。
        </Alert>
      )}
      {planEntryDiscipline && !planEntryDiscipline.new_risk_allowed && (
        <Box sx={{ mb: 2 }}>
          <NewRiskDisciplineBlockNotice
            discipline={planEntryDiscipline}
            consequence={planEntryDisciplineConsequence}
          />
        </Box>
      )}
      {strategiesQuery.isError && <Alert severity="warning" sx={{ mb: 2 }}>策略定义当前不可读；计划身份与基础配置仍按计划事实显示，关键参数不做猜测。</Alert>}
      <Dialog
        open={Boolean(deleteTarget)}
        onClose={() => { if (!deleteMutation.isPending) { setDeleteTarget(null); deleteMutation.reset(); } }}
        aria-labelledby="delete-plan-title"
        fullWidth
        maxWidth="xs"
      >
        <DialogTitle id="delete-plan-title">删除草稿？</DialogTitle>
        <DialogContent>
          <DialogContentText>
            将永久删除“{deleteTarget?.plan_name?.trim() || (deleteTarget ? `未命名计划 · ${shortDigest(deleteTarget.plan_id)}` : "当前计划")}”。此操作不可恢复，但不会影响策略定义、其他计划或任何已确认版本。
          </DialogContentText>
          {deleteMutation.isError && <Alert severity="error" sx={{ mt: 2 }}>{planDeletionError(deleteMutation.error)}</Alert>}
        </DialogContent>
        <DialogActions>
          <Button variant="outlined" disabled={deleteMutation.isPending} onClick={() => { setDeleteTarget(null); deleteMutation.reset(); }}>取消</Button>
          <Button variant="contained" color="error" disabled={!deleteTarget || deleteMutation.isPending} onClick={() => { if (deleteTarget) deleteMutation.mutate(deleteTarget); }}>
            {deleteMutation.isPending ? "正在删除…" : "删除草稿"}
          </Button>
        </DialogActions>
      </Dialog>
      {activeTab === "CURRENT" && <>
        <ExpandableList items={currentActivations} initialCount={8} step={8} renderItem={renderActiveActivation} />
        <ExpandableList items={currentPlans} initialCount={8} step={8} renderItem={renderPlan} />
        {query.data && activationsQuery.data && currentActivations.length === 0 && currentPlans.length === 0 && <Alert severity="info" variant="outlined">当前没有可操作计划。</Alert>}
      </>}
      {activeTab === "HISTORY" && <>
        <ExpandableList items={historicalPlans} initialCount={8} step={8} renderItem={renderPlan} />
        {query.data && historicalPlans.length === 0 && <Alert severity="info" variant="outlined">还没有历史计划。</Alert>}
      </>}
    </Box>
  );
}

// Retained only to read historical implementation data during a rolling
// upgrade.  It is deliberately not routed: plans no longer have a separate
// fixed-but-not-running stage.
export function LegacyPlanActivationRoute() {
  const { planVersionId = "" } = useParams();
  const navigate = useNavigate();
  const { status } = useOutletContext<FrameContext>();
  const expectedMarketSource = expectedMarketSourceForEnvironment(
    status.environment_kind,
  );
  const environmentScope = `${status.environment_kind}:${status.environment_id}`;
  const activationIdentityScope = (
    `${environmentScope}:CREATE_ACTIVATION:${planVersionId}`
  );
  const pendingActivationIdentityRef = useRef<StableRequestIdentity | null>(
    null,
  );
  const preview = useQuery({
    queryKey: ["activation-preview", environmentScope, planVersionId],
    queryFn: () => getActivationPreview(planVersionId),
    enabled: Boolean(planVersionId),
  });
  const decisionBasisKind = valueOf(preview.data, "decision_basis_kind", "STRATEGY_SIGNAL");
  const decisionContext = recordOf(preview.data?.decision_context);
  const directExecution = isDirectExecution(decisionBasisKind);
  const decisionBasisRef = valueOf(
    preview.data,
    "decision_basis_ref",
    directExecution ? "DIRECT_EXECUTION@1" : valueOf(preview.data, "strategy_ref", ""),
  );
  const orderScheduleSpec = recordOf(preview.data?.order_schedule_spec);
  const orderScheduleSnapshot = recordOf(preview.data?.order_schedule_snapshot);
  const directProjectedMaxLoss = directExecution
    ? valueOf(
      recordOf(orderScheduleSnapshot.full_fill_protection_estimate),
      "maximum_projected_loss",
      "",
    )
    : "";
  const positionAlignment = recordOf(preview.data?.position_alignment);
  const positionDisposition = Object.keys(positionAlignment).length > 0;
  const positionAlignmentReady = preview.data?.position_alignment_ready !== false;
  const positionAlignmentBlocker = valueOf(
    preview.data,
    "position_alignment_blocker",
  );
  const newRiskDiscipline = recordOf(preview.data?.new_risk_discipline);
  const newRiskDisciplineAllowed = positionDisposition
    || newRiskDiscipline.new_risk_allowed === true;
  const newRiskDisciplineBlockers = Array.isArray(newRiskDiscipline.blocker_codes)
    ? newRiskDiscipline.blocker_codes.map(String)
    : [];
  const liveProfitQualification = preview.data?.live_profit_qualification;
  const liveProfitQualificationRequired = (
    liveProfitQualification?.required === true
  );
  const liveProfitQualificationReady = status.environment_kind === "DEMO"
    || liveProfitQualificationSatisfied(liveProfitQualification);
  const liveProfitQualificationBlockers = (
    liveProfitQualification?.blocker_codes ?? []
  );
  const liveProfitPresentation = liveProfitQualification
    ? liveProfitQualificationPresentation(liveProfitQualification)
    : null;
  const riskDisciplineAmount = (key: string): string => {
    const raw = newRiskDiscipline[key];
    return raw === null || raw === undefined
      ? "未知"
      : `${quoteCurrencyAmount(String(raw))} USDT`;
  };
  const orderInstrumentRules = recordOf(orderScheduleSnapshot.instrument_rules);
  const orderVenuePolicy = recordOf(orderScheduleSpec.venue_policy);
  const orderPriceTickSize = valueOf(orderInstrumentRules, "price_tick_size", "");
  const orderQuantityStep = valueOf(
    orderInstrumentRules,
    valueOf(orderVenuePolicy, "order_type") === "MARKET"
      ? "market_quantity_step"
      : "limit_quantity_step",
    "",
  );
  const orderedScheduleLegs = orderedCompiledLegs(orderScheduleSnapshot);
  const allowedActions = Array.isArray(preview.data?.allowed_actions)
    ? preview.data.allowed_actions.map(String)
    : [];
  const expectedScheduleDigest = valueOf(preview.data, "expected_schedule_digest", "");
  const hasCompiledSchedule = Object.keys(orderScheduleSnapshot).length > 0;
  const compiledScheduleReady = !hasCompiledSchedule || (
    orderScheduleSnapshot.valid === true
    && /^[0-9a-f]{64}$/.test(expectedScheduleDigest)
  );
  const directScheduleReady = positionDisposition
    || !directExecution
    || (hasCompiledSchedule && compiledScheduleReady);
  const parameters = recordOf(preview.data?.strategy_parameters);
  const instrumentRef = valueOf(preview.data, "instrument_ref");
  const direction = valueOf(preview.data, "direction");
  const channelLookback = Number(parameters.channel_lookback_15m) || 20;
  const market = useQuery({
    queryKey: [
      "activation-preview-market-context",
      environmentScope,
      expectedMarketSource,
      planVersionId,
      instrumentRef,
      channelLookback,
    ],
    queryFn: () => getMarketContext(instrumentRef, channelLookback),
    enabled: Boolean(preview.data && instrumentRef),
    retry: 1,
    retryDelay: 2_000,
    refetchInterval: 15_000,
  });
  const activationMarketStream = usePublicMarketStream(
    Boolean(preview.data && instrumentRef),
    instrumentRef || "BTCUSDT-PERP",
    "15m",
    environmentScope,
    expectedMarketSource,
  );
  const currentFunding = activationMarketStream.status === "LIVE"
    && isUsableMarketStreamFunding(
      activationMarketStream.funding,
      expectedMarketSource,
      Date.now(),
    )
    ? activationMarketStream.funding
    : null;
  const liveWrite = status.profile === "BINANCE_LIVE_WRITE";
  const liveReadOnly = status.profile === "BINANCE_LIVE_READ_ONLY";
  const copyLeadAccount = status.venue_account_type === "USDM_COPY_LEAD";
  const personalLiveAccount = status.venue_account_type === "USDM_PERSONAL";
  const realAccountReady = Boolean(preview.data?.live_activation_eligible);
  const currentProductVersion = preview.data?.product_build_consistent === true;
  const planRuntimeCompatible = preview.data?.runtime_compatible === true;
  const runtimeIncompatibilityReason = valueOf(
    preview.data,
    "runtime_incompatibility_reason",
  );
  const executorReady = valueOf(preview.data, "executor_status") === "READY";
  const activationRuntimeReady = status.environment_kind === "DEMO"
    ? executorReady
    : liveWrite;
  const validUntilMs = Date.parse(valueOf(preview.data, "valid_until", ""));
  const planNotExpired = Number.isFinite(validUntilMs) && Date.now() < validUntilMs;
  const activationEnabled = Boolean(
    preview.data
    && !preview.isFetching
    && !liveReadOnly
    && planNotExpired
    && planRuntimeCompatible
    && activationRuntimeReady
    && directScheduleReady
    && compiledScheduleReady
    && positionAlignmentReady
    && newRiskDisciplineAllowed
    && liveProfitQualificationReady
    && (!liveWrite || realAccountReady),
  );
  const mutation = useMutation({
    mutationFn: ({
      payload,
      idempotencyKey,
    }: {
      payload: {
        plan_version_id: string;
        expected_schedule_digest: string | null;
      };
      idempotencyKey: string;
    }) => createActivation(payload, idempotencyKey),
    onSuccess: (result) => {
      pendingActivationIdentityRef.current = null;
      clearPersistentRequestIdentity(activationIdentityScope);
      const activation = result.activation as Record<string, unknown> | undefined;
      navigate(`/activations/${valueOf(activation, "activation_id")}`);
    },
    onError: (error) => {
      if (!isUnknownMutationResult(error)) {
        pendingActivationIdentityRef.current = null;
        clearPersistentRequestIdentity(activationIdentityScope);
      }
      if (
        error instanceof ApiFailure
        && (
          error.code === "ACTIVATION_PREVIEW_STALE"
          || newRiskDisciplineBlockerCodesFromFailureCode(error.code).length > 0
        )
      ) {
        void preview.refetch();
        void market.refetch();
      }
    },
  });
  const mutationNewRiskDisciplineBlockers = mutation.error instanceof ApiFailure
    ? newRiskDisciplineBlockerCodesFromFailureCode(mutation.error.code)
    : [];
  const marketSourceMismatch = Boolean(
    market.data
    && !isMarketSourceForEnvironment(
      market.data.source,
      status.environment_kind,
    ),
  );
  const currentMarket = marketSourceMismatch ? undefined : market.data;
  const activationMarketReady = Boolean(
    currentMarket
    && !market.isError
    && !market.isFetching,
  );
  const currentSpread = currentMarket
    ? subtractDecimal(currentMarket.ask_price, currentMarket.bid_price) ?? ""
    : "";
  const currentSpreadBps = currentMarket
    ? Number(currentSpread) / Number(currentMarket.reference_price) * 10_000
    : Number.NaN;
  const longClosedBarBreakoutGap = currentMarket
    ? closedBarBreakoutGapPercent("LONG", currentMarket.latest_close_1m, currentMarket.channel_upper)
    : "";
  const shortClosedBarBreakoutGap = currentMarket
    ? closedBarBreakoutGapPercent("SHORT", currentMarket.latest_close_1m, currentMarket.channel_lower)
    : "";
  const planName = valueOf(preview.data, "plan_name", "");
  const submitActivation = () => {
    const payload = {
      plan_version_id: planVersionId,
      expected_schedule_digest: hasCompiledSchedule
        ? expectedScheduleDigest
        : null,
    };
    const requestIdentity = persistentRequestIdentity(
      pendingActivationIdentityRef.current,
      activationIdentityScope,
      JSON.stringify(payload),
    );
    pendingActivationIdentityRef.current = requestIdentity;
    mutation.mutate({
      payload,
      idempotencyKey: requestIdentity.idempotencyKey,
    });
  };
  return (
    <Box sx={{ width: "min(920px, calc(100% - clamp(32px, 4vw, 48px)))", mx: "auto", py: { xs: 2.5, sm: 3 } }}>
      <PageHeader
        eyebrow="旧版启动入口"
        title={planName || "未命名计划"}
        description={positionDisposition
          ? "该计划只处置保存的外部持仓基线，并形成精确的 reduce-only 责任。启动时会重读完整账户快照；既有入场不会被记作 Halpha 成交或策略盈亏。"
          : directExecution
          ? "交易金额和订单计划已保存。此旧入口已不用于正常流程；计划应从草稿页提交并启动，并继续经过当前事实、CAP 与 EXE。"
          : "此旧入口已不用于正常流程；计划应从草稿页提交并启动，不进行第二次资金授权，也不会立即向 Binance 下单。"}
      />
      {preview.isPending && <LinearProgress aria-label="正在读取激活复核" />}
      {preview.isError && <Alert severity="error">当前复核事实不可用，不能启动计划。</Alert>}
      {preview.data && <>
        <FactGrid facts={[
          { label: "账户", value: valueOf(preview.data, "account_ref") },
          { label: "交易对象 / 方向", value: `${valueOf(preview.data, "instrument_ref")} / ${translatedLabel(directionLabels, valueOf(preview.data, "direction"))}`, tone: marketToneForDirection(valueOf(preview.data, "direction")) },
          { label: directExecution ? "决策依据" : "策略", value: positionDisposition ? positionAlignmentOperationLabel(positionAlignment) : directExecution ? DIRECT_EXECUTION_LABEL : valueOf(preview.data, "strategy_ref"), note: directExecution ? decisionBasisRef : undefined },
          { label: positionDisposition ? "处置边界" : "交易金额", value: `${quoteCurrencyAmount(valueOf(preview.data, "trade_amount"))} USDT` },
          { label: "有效期", value: formatUserVisibleTime(valueOf(preview.data, "valid_until")) },
          ...(positionDisposition ? [
            { label: "账户快照", value: formatUserVisibleTime(valueOf(positionAlignment, "fact_cutoff")), note: shortDigest(valueOf(positionAlignment, "snapshot_ref")) },
            { label: "基线数量", value: marketVolume(valueOf(positionAlignment, "baseline_quantity")), note: `${valueOf(positionAlignment, "position_side")} 持仓侧` },
            { label: "本次处置", value: marketVolume(valueOf(positionAlignment, "requested_reduction_quantity")), note: "仅允许 reduce-only 市价责任" },
            { label: "处置后目标", value: marketVolume(valueOf(positionAlignment, "target_quantity_after")), note: "不生成历史 ENTRY" },
            { label: "基线入场 / 标记", value: `${marketPrice(valueOf(positionAlignment, "baseline_entry_price"))} / ${marketPrice(valueOf(positionAlignment, "baseline_mark_price"))} USDT` },
          ] : directExecution ? [
            { label: "订单计划", value: orderScheduleSummary(orderScheduleSpec) },
            { label: "入场条件", value: orderConditionSummary(orderScheduleSpec) },
            { label: "保护与退出", value: orderProtectionSummary(orderScheduleSpec) },
            { label: "编译摘要", value: expectedScheduleDigest ? shortDigest(expectedScheduleDigest) : "不可用", note: orderScheduleSnapshot.valid === true ? "启动时必须与本次服务端预览一致" : "订单计划未通过服务端编译" },
          ] : [
            { label: "入场方式", value: parameters.demo_immediate_entry === true ? "下单流程验证 · 下一根有效闭合 1m" : `${valueOf(parameters, "channel_lookback_15m")} × 15m 通道 / ${valueOf(parameters, "confirmation_bars_1m")} × 1m 确认` },
            { label: "保护", value: `初始止损 ${valueOf(parameters, "initial_stop_atr_multiple")} ATR / 最大追价 ${valueOf(parameters, "max_entry_extension_atr")} ATR` },
            { label: "退出", value: `最大 ${valueOf(parameters, "max_hold_bars_15m")} × 15m / TP1 ${Number(valueOf(parameters, "take_profit_1_fraction")) * 100}% @ ${valueOf(parameters, "take_profit_1_r")}R / TP2 @ ${valueOf(parameters, "take_profit_2_r")}R` },
          ]),
          ...(!positionDisposition ? [
            {
              label: "单计划风险上限",
              value: riskDisciplineAmount("max_plan_loss"),
              note: "纪律允许的单计划最大损失。",
            },
            ...(directExecution && directProjectedMaxLoss ? [{
              label: "预计最大止损损失",
              value: `-${quoteCurrencyAmount(directProjectedMaxLoss)} USDT`,
              note: "按服务端归一化订单、固定止损距离及冻结手续费估算。",
              tone: "loss" as const,
            }] : []),
            { label: "组合风险容量", value: `${riskDisciplineAmount("open_risk_after_proposal")} / ${riskDisciplineAmount("open_risk_limit")}`, note: `已启用 ${valueOf(newRiskDiscipline, "open_new_risk_activation_count", "0")} 份新增风险计划；不按订单数限制` },
            { label: "总敞口", value: `${riskDisciplineAmount("gross_exposure_after_proposal")} / ${riskDisciplineAmount("gross_exposure_limit")}`, note: `单品种 ${riskDisciplineAmount("instrument_exposure_after_proposal")} · 相关簇 ${riskDisciplineAmount("correlated_exposure_after_proposal")}` },
            { label: "亏损与回撤停止", value: `日 ${riskDisciplineAmount("daily_loss_measure")} / ${riskDisciplineAmount("daily_loss_limit")}`, note: `周 ${riskDisciplineAmount("weekly_loss_measure")} / ${riskDisciplineAmount("weekly_loss_limit")} · 回撤 ${valueOf(newRiskDiscipline, "rolling_drawdown_fraction", "未知")}` },
          ] : []),
          ...(status.environment_kind !== "DEMO" && liveProfitQualificationRequired && liveProfitQualification ? [
            {
              label: "Demo 证据准入",
              value: liveProfitPresentation?.label ?? liveProfitQualification.status,
              note: liveProfitQualificationBlockers
                .map((code) => liveProfitQualificationBlockerLabels[code] ?? code)
                .join("；") || "匹配唯一、摘要一致且仍在新鲜度内",
            },
            {
              label: "证据样本",
              value: liveProfitQualification.comparable_trade_count === null
                ? "不可用"
                : `${liveProfitQualification.comparable_trade_count} 笔`,
              note: liveProfitQualification.evidence_cutoff
                ? `可比样本截止 ${formatUserVisibleTime(liveProfitQualification.evidence_cutoff)}`
                : `最长有效 ${liveProfitQualification.maximum_evidence_age_days} 天`,
            },
            {
              label: "账户组合风险上限",
              value: `单笔 ${qualificationFractionPercent(liveProfitQualification.portfolio_max_plan_loss_fraction)}`,
              note: `总风险 ${qualificationFractionPercent(liveProfitQualification.portfolio_max_open_risk_fraction)} · 总敞口 ${qualificationFractionPercent(liveProfitQualification.portfolio_max_gross_exposure_fraction)} · 日损失停止 ${qualificationFractionPercent(liveProfitQualification.portfolio_daily_loss_stop_fraction)}`,
            },
          ] : []),
          ...(Object.keys(decisionContext).length > 0 ? [
            { label: "交易理由", value: valueOf(decisionContext, "rationale") },
            { label: "依据与证据", value: valueOf(decisionContext, "evidence") },
            { label: "已知局限", value: valueOf(decisionContext, "limitations") },
          ] : []),
          { label: "确认构建", value: shortDigest(valueOf(preview.data, "product_build_id")), note: currentProductVersion ? "由当前构建确认" : "由较早构建确认" },
          { label: "运行兼容性", value: planRuntimeCompatible ? "兼容" : "不兼容" },
          { label: "执行器", value: translatedLabel(executorStatusLabels, valueOf(preview.data, "executor_status")), note: `核对于 ${formatUserVisibleTime(valueOf(preview.data, "executor_status_checked_at"))}` },
          ...(liveWrite ? [
            { label: "交易所变更请求", value: valueOf(preview.data, "configured_runtime_real_write_gate") },
          ] : []),
          ]} />
        {!positionDisposition ? (
          <PlanAiReviewRecord
            review={preview.data.ai_review}
            required
            defaultOpen
          />
        ) : null}
        {directExecution && !positionDisposition && hasCompiledSchedule && <Box component="section" sx={{ mt: 3 }}>
          <Stack direction={{ xs: "column", sm: "row" }} spacing={1} sx={{ justifyContent: "space-between", alignItems: { xs: "flex-start", sm: "baseline" }, mb: 1.5 }}>
            <Box>
              <Typography variant="h2">实际提交复核</Typography>
              <Typography color="text.secondary" variant="body2" sx={{ mt: .5 }}>
                表格按真实提交顺序排列；同一时刻最多一个增险入场档处于提交中、未知或开放状态。
              </Typography>
            </Box>
            <Typography variant="caption" color="text.secondary">
              规则截止 {formatUserVisibleTime(valueOf(orderScheduleSnapshot, "source_cutoff"))}
            </Typography>
          </Stack>
          <FactGrid facts={[
            { label: "提交方式", value: scheduleSubmissionSummary(orderScheduleSnapshot), note: orderedScheduleLegs.length > 0 ? `首档为计划档 ${Number(orderedScheduleLegs[0]?.leg_index ?? 0) + 1}` : "没有可提交档位" },
            { label: "动态管理", value: orderDynamicSummary(orderScheduleSnapshot) },
            { label: "允许动作", value: allowedActions.map((item) => actionProfileLabels[item] ?? item).join("、") || "不可读" },
            { label: "服务端归一化", value: `${quoteCurrencyAmount(valueOf(orderScheduleSnapshot, "effective_total_notional", "0"))} USDT`, note: `${orderedScheduleLegs.length} 档 · ${valueOf(orderInstrumentRules, "source", "规则来源未知")}` },
          ]} />
          <TableContainer sx={{ mt: 1.5, border: 1, borderColor: "divider", borderRadius: 1.5 }}>
            <Table size="small" aria-label="按真实提交顺序排列的订单档位">
              <TableHead>
                <TableRow>
                  <TableCell>提交序号</TableCell>
                  <TableCell>计划档</TableCell>
                  <TableCell align="right">价格（USDT）</TableCell>
                  <TableCell align="right">数量</TableCell>
                  <TableCell align="right">有效名义额（USDT）</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {orderedScheduleLegs.map((leg, index) => <TableRow key={valueOf(leg, "leg_index", String(index))}>
                  <TableCell sx={{ fontVariantNumeric: "tabular-nums" }}>
                    {index + 1}{index === 0 ? <Chip label="首档" size="small" color="warning" variant="outlined" sx={{ ml: 1 }} /> : null}
                  </TableCell>
                  <TableCell sx={{ fontVariantNumeric: "tabular-nums" }}>{Number(leg.leg_index) + 1}</TableCell>
                  <TableCell align="right" sx={{ fontVariantNumeric: "tabular-nums" }}>{leg.price === null ? `市价（数量参考 ${tradingPrice(valueOf(leg, "sizing_price"), orderPriceTickSize)}）` : tradingPrice(valueOf(leg, "price"), orderPriceTickSize)}</TableCell>
                  <TableCell align="right" sx={{ fontVariantNumeric: "tabular-nums" }}>{tradingQuantity(valueOf(leg, "quantity"), orderQuantityStep)}</TableCell>
                  <TableCell align="right" sx={{ fontVariantNumeric: "tabular-nums" }}>{quoteAmount(valueOf(leg, "effective_notional"))}</TableCell>
                </TableRow>)}
              </TableBody>
            </Table>
          </TableContainer>
        </Box>}
        <Box component="section" sx={{ mt: 3 }}>
          <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} sx={{ justifyContent: "space-between", alignItems: { xs: "stretch", sm: "center" }, mb: 2 }}>
            <Box>
              <Typography variant="h2">当前市场位置</Typography>
              <Typography color="text.secondary" variant="body2" sx={{ mt: .75 }}>
                {positionDisposition
                  ? "公开行情仅用于辅助观察；能否启动由最新完整账户快照、精确持仓基线、未结委托和责任冲突共同决定。"
                  : directExecution
                  ? "只读公开行情用于启动前核对计划价格和当前价差；直接执行仍按运行快照条件、当前事实与 CAP / EXE 检查处理。"
                  : "只读公开行情用于决定是否启动；策略仍只按运行快照参数和闭合 K 线执行。"}
              </Typography>
            </Box>
            <Button variant="outlined" onClick={() => market.refetch()} disabled={market.isFetching}>{market.isFetching ? "正在刷新…" : "刷新行情"}</Button>
          </Stack>
          {market.isPending && <LinearProgress aria-label="正在读取启动前行情" />}
          {market.isError && currentMarket && <Alert severity="warning" variant="outlined">
            行情刷新失败；以下保留上次成功行情（截止 {formatUserVisibleTime(currentMarket.source_cutoff)}），可能已经过期。请刷新成功后再决定是否启动。
          </Alert>}
          {market.isError && !currentMarket && <Alert severity="warning" variant="outlined">
            {directExecution
              ? "当前公开行情不可用。不要在无法核对当前价格和价差时启动。"
              : "当前公开行情不可用。不要在无法判断价格、价差和突破位置时启动。"}
          </Alert>}
          {marketSourceMismatch && <Alert severity="error" variant="outlined">
            行情来源与当前 {status.environment_kind} 环境不一致，已拒绝显示和用于启动复核。
          </Alert>}
          {currentMarket && <FactGrid facts={[
            { label: "盘口中间价", value: `${marketPrice(currentMarket.reference_price)} USDT` },
            { label: "买一 / 卖一", value: `${marketPrice(currentMarket.bid_price)} / ${marketPrice(currentMarket.ask_price)} USDT` },
            { label: "买卖价差", value: `${marketPrice(currentSpread)} USDT`, note: Number.isFinite(currentSpreadBps) ? `${currentSpreadBps.toFixed(2)} bps` : undefined },
            { label: "当前资金费率", value: currentFunding
              ? formatFundingRatePercent(currentFunding.funding_rate)
              : "实时数据不可用" },
            { label: "下次资金结算", value: currentFunding
              ? formatUserVisibleTime(currentFunding.next_funding_at)
              : "未知", note: "实际结算取决于届时费率和是否仍有持仓" },
            ...(!directExecution ? [
              { label: "通道上沿 / 下沿", value: `${marketPrice(currentMarket.channel_upper)} / ${marketPrice(currentMarket.channel_lower)} USDT`, note: `${direction === "LONG" ? "计划做多" : "计划做空"}；启动前同时比较两侧机会` },
              { label: "最近闭合 1m", value: `${marketPrice(currentMarket.latest_close_1m)} USDT` },
              { label: "1m 收盘距上沿 / 下沿", value: `${gapPercent(longClosedBarBreakoutGap)} / ${gapPercent(shortClosedBarBreakoutGap)}`, note: "策略触发口径；正值表示尚未突破，负值表示已经越过" },
              { label: "盘口中间价距上沿 / 下沿", value: `${gapPercent(currentMarket.long_breakout_gap_pct)} / ${gapPercent(currentMarket.short_breakout_gap_pct)}`, note: "仅用于启动前定位，不替代闭合 K 线与执行前检查" },
            ] : []),
            { label: "行情截止", value: formatUserVisibleTime(currentMarket.source_cutoff) },
          ]} />}
          {!directExecution && Number.isFinite(currentSpreadBps) && currentSpreadBps > 10 && <Alert severity="warning" sx={{ mt: 2 }}>
            当前买卖价差约 {currentSpreadBps.toFixed(1)} bps，超过 10 bps 入场上限。可以启动策略等待，但只有价差收窄且其他固定条件同时满足时才会创建入场动作。
          </Alert>}
        </Box>
        <Alert severity="warning" variant="outlined" sx={{ mt: 3 }}>{valueOf(preview.data, "capital_notice")}</Alert>
      </>}
      {liveReadOnly && <Alert severity="warning" sx={{ mt: 3 }}>只读环境仅用于公共市场观察，不能激活计划或向交易所提交变更请求。</Alert>}
      {preview.data && positionDisposition && !positionAlignmentReady && (
        <Alert severity="error" sx={{ mt: 2 }}>
          持仓处置复核未通过：{positionAlignmentReadinessLabels[positionAlignmentBlocker] ?? positionAlignmentBlocker ?? "账户基线不可确认"}。请刷新账户事实并重新创建处置计划，不能沿用旧快照强制启动。
        </Alert>
      )}
      {preview.data && !positionDisposition && !newRiskDisciplineAllowed && (
        <Box sx={{ mt: 2 }}>
          <NewRiskDisciplineBlockNotice
            discipline={preview.data.new_risk_discipline}
            blockerCodes={newRiskDisciplineBlockers}
            consequence="当前计划不能启动；页面不会绕过服务端核对，也不会创建交易所订单。已有计划的保护、撤单、减仓、退出和接管不受影响。"
          />
        </Box>
      )}
      {preview.data && status.environment_kind !== "DEMO" && liveProfitQualificationRequired && liveProfitQualification && (
        <Alert
          severity={liveProfitPresentation?.severity ?? "warning"}
          variant="outlined"
          sx={{ mt: 2 }}
        >
          <strong>{liveProfitPresentation?.label ?? liveProfitQualification.status}。</strong>{" "}
          {liveProfitQualification.eligible_input
            ? `已核对 ${liveProfitQualification.comparable_trade_count ?? 0} 笔 Demo 同类样本、完整冻结风险基准、目标账户与固定摘要。它只是盈利导向实盘激活的必要输入；仍不授权开闸、下单、加本金或提高风险。`
            : `${liveProfitQualificationBlockers.map((code) => liveProfitQualificationBlockerLabels[code] ?? code).join("；") || "当前证据无法确认"}。请在 Demo 重新形成证据包，并由所有者固定仓库外绝对路径和摘要；不能在 Live 页面选择任意文件绕过。`}
        </Alert>
      )}
      {preview.data && directExecution && !positionDisposition && !directScheduleReady && <Alert severity="error" sx={{ mt: 2 }}>服务端订单计划预览无效或缺少完整摘要，当前不能启动。请返回计划编辑页修正后重新提交并启动。</Alert>}
      {preview.data && !planNotExpired && <Alert severity="warning" sx={{ mt: 2 }}>计划有效期已经结束，不能再启动；请基于当前事实创建、审核并提交并启动新计划。</Alert>}
      {preview.data && !currentProductVersion && planRuntimeCompatible && <Alert severity="info" variant="outlined" sx={{ mt: 2 }}>
        该计划由较早构建确认；运行快照中的决策和订单规则已重新校验兼容。启动会形成新的运行快照，不会改写原计划。
      </Alert>}
      {preview.data && !planRuntimeCompatible && <Alert severity="error" sx={{ mt: 2 }}>
        {planRuntimeIncompatibilityLabels[runtimeIncompatibilityReason] ?? "当前运行时无法安全消费该计划"}；仍可查看原计划或沿用参数新建。
      </Alert>}
      {preview.data && status.environment_kind === "DEMO" && !executorReady && <Alert severity="warning" sx={{ mt: 2 }}>执行器尚未完成连接、启动核对和历史预热，当前不能启动新计划。已有激活的退出与接管控制不受影响。</Alert>}
      {preview.data && liveWrite && <Alert severity="info" variant="outlined" sx={{ mt: 2 }}>
        {copyLeadAccount
          ? "此处只在带单员公域合约上下文创建本地激活，不会立即向交易所提交请求。开闸并完成精确账户核对后，成交可能进入带单组合并被跟单者复制。"
          : personalLiveAccount
            ? "此处只在个人 USDⓈ-M 合约上下文创建本地激活，不会立即向交易所提交请求。开闸并完成精确账户核对后，交易仅属于个人账户，不进入带单组合。"
            : "真实账户类型不可识别；当前不得启动。"}
      </Alert>}
      {liveWrite && !realAccountReady && <Alert severity="warning" sx={{ mt: 2 }}>当前 App、Executor、实盘变更门或必要证据输入尚未全部一致；当前不能启动真实账户计划。</Alert>}
      {mutation.isError && mutationNewRiskDisciplineBlockers.length > 0 && (
        <Box sx={{ mt: 2 }}>
          <NewRiskDisciplineBlockNotice
            discipline={preview.data?.new_risk_discipline}
            blockerCodes={mutationNewRiskDisciplineBlockers}
            title="启动被新增风险纪律拒绝"
            consequence="服务端未接受本次启动；请等待页面刷新后的当前事实，再重新确认。不会创建替代激活或交易所订单。"
          />
        </Box>
      )}
      {mutation.isError && mutationNewRiskDisciplineBlockers.length === 0 && <Alert severity="error" sx={{ mt: 2 }}>
        {mutation.error instanceof ApiFailure && mutation.error.code === "ACTIVATION_PREVIEW_STALE"
          ? "启动复核已过期，页面正在刷新服务端订单快照与行情；刷新完成后请重新确认启动。"
          : isUnknownMutationResult(mutation.error)
            ? "激活结果未知；再次启动会沿用同一请求身份核对原结果，不会创建替代激活。"
            : `激活未被接受：${mutation.error instanceof ApiFailure ? mutation.error.code : "UNKNOWN"}`}
      </Alert>}
      <Button
        variant="contained"
        color="warning"
        sx={{ mt: 3 }}
        disabled={!activationEnabled || (!positionDisposition && !activationMarketReady) || mutation.isPending || preview.isFetching}
        onClick={submitActivation}
      >
        {mutation.isPending
          ? "正在启动…"
          : preview.isFetching
            ? "正在刷新启动复核…"
          : positionDisposition && !positionAlignmentReady
            ? "持仓基线已变化，不能启动"
          : directExecution && !directScheduleReady
            ? "订单计划预览无效，不能启动"
          : !planNotExpired
            ? "计划已过期，不能启动"
            : !planRuntimeCompatible
              ? "计划与当前运行时不兼容"
            : status.environment_kind === "DEMO" && !executorReady
              ? "执行器未就绪，不能启动"
          : !positionDisposition && (market.isPending || market.isFetching)
            ? "正在读取启动前行情…"
              : !positionDisposition && (market.isError || !currentMarket)
                ? "行情不可用，不能启动"
              : status.environment_kind === "LIVE" && !liveProfitQualificationReady
                ? "Demo 证据准入未通过"
              : liveWrite
                ? copyLeadAccount
                  ? "在带单账户启动实盘计划"
                  : personalLiveAccount
                    ? "在个人账户启动实盘计划"
                    : "账户类型未知，不能启动"
                 : mutation.isError && isUnknownMutationResult(mutation.error)
                   ? "沿用原请求身份重试"
                   : positionDisposition
                     ? `启动${valueOf(positionAlignment, "operation") === "CLOSE" ? "平仓" : "减仓"}处置计划`
                     : directExecution ? "启动直接执行订单计划" : "启动策略"}
      </Button>
    </Box>
  );
}

function ActivationRoute() {
  const { activationId = "" } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const { status, marketColorScheme } = useOutletContext<FrameContext>();
  const [runtimeNowMs, setRuntimeNowMs] = useState(() => Date.now());
  useEffect(() => {
    const intervalId = window.setInterval(() => setRuntimeNowMs(Date.now()), 1_000);
    return () => window.clearInterval(intervalId);
  }, []);
  const expectedMarketSource = expectedMarketSourceForEnvironment(
    status.environment_kind,
  );
  const environmentScope = `${status.environment_kind}:${status.environment_id}`;
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ["activation", activationId], queryFn: () => getActivation(activationId), enabled: Boolean(activationId), refetchInterval: 2_000 });
  const timelineQuery = useQuery({ queryKey: ["activation-timeline", activationId], queryFn: () => getActivationTimeline(activationId), enabled: Boolean(activationId), refetchInterval: 2_000 });
  useEffect(() => {
    if (!query.isSuccess || location.hash !== "#stability-controls") return;
    window.requestAnimationFrame(() => {
      document.getElementById("stability-controls")?.scrollIntoView({ block: "center" });
    });
  }, [location.hash, query.isSuccess]);
  const activation = query.data?.activation as Record<string, unknown> | undefined;
  const capital = recordOf(query.data?.capital);
  const tradeResult = recordOf(query.data?.trade_result);
  const positionAttribution = recordOf(query.data?.position_attribution);
  const actions = useMemo(
    () => recordsOf(query.data?.execution_actions),
    [query.data?.execution_actions],
  );
  const facts = useMemo(
    () => recordsOf(query.data?.venue_facts),
    [query.data?.venue_facts],
  );
  const entryPolicyRetryState = runtimeEntryPolicyRetryState(actions, facts);
  const rejectedEntryFact = entryPolicyRetryState.latestRejectedFact;
  const rejectedEntryReasonRaw = rejectedEntryFact
    ? valueOf(recordOf(rejectedEntryFact.payload), "reason", "")
    : "";
  const rejectedEntryReason = rejectedEntryFact
    ? venueReasonText(rejectedEntryReasonRaw || "交易所拒绝了本次订单")
    : "";
  const rejectionKind = venueRejectionKind(rejectedEntryReasonRaw);
  const retryablePostOnlyRejection = rejectionKind === "POST_ONLY_RETRYABLE";
  const retryablePriceMatchRejection = rejectionKind === "PRICE_MATCH_RETRYABLE";
  const retryableEntryRejection = retryablePostOnlyRejection
    || retryablePriceMatchRejection;
  const entryPolicyRetryLimitReached = retryableEntryRejection
    && entryPolicyRetryState.retryCount >= POST_ONLY_RETRY_MAX_ATTEMPTS;
  const normalFokNoFill = rejectionKind === "FOK_NO_FILL";
  const receipts = useMemo(
    () => recordsOf(query.data?.receipts),
    [query.data?.receipts],
  );
  const decisionBasis = recordOf(query.data?.decision_basis);
  const directExecution = isDirectExecution(valueOf(decisionBasis, "kind", "STRATEGY_SIGNAL"));
  const decisionBasisRef = valueOf(
    decisionBasis,
    "decision_basis_ref",
    directExecution ? "DIRECT_EXECUTION@1" : "",
  );
  const positionAlignment = recordOf(activation?.position_alignment);
  const positionDisposition = Object.keys(positionAlignment).length > 0;
  const orderSchedule = recordOf(query.data?.order_schedule);
  const strategy = recordOf(query.data?.strategy);
  const plan = recordOf(query.data?.plan);
  const planId = valueOf(plan, "plan_id", "");
  const planDecisionContext = recordOf(plan.decision_context);
  const planDecisionIntent = valueOf(planDecisionContext, "intent", "");
  const planDecisionContextRows = [
    { label: "本次目的", value: translatedLabel(planDecisionIntentLabels, planDecisionIntent) },
    { label: "交易形态", value: translatedLabel(planSetupFamilyLabels, valueOf(planDecisionContext, "setup_family", "")) },
    { label: "可证伪假设", value: valueOf(planDecisionContext, "rationale") },
    { label: "入场前证据", value: valueOf(planDecisionContext, "evidence") },
    { label: "失效与放弃条件", value: valueOf(planDecisionContext, "invalidation") },
    { label: "已知局限", value: valueOf(planDecisionContext, "limitations") },
    { label: "证据截止", value: valueOf(planDecisionContext, "evidence_cutoff", "")
      ? formatUserVisibleTime(valueOf(planDecisionContext, "evidence_cutoff"))
      : "" },
  ].filter((item) => item.value.length > 0);
  const instrumentRef = valueOf(activation, "instrument_ref", "");
  const runtimeBaseAsset = instrumentRef.replace(/USDT-PERP$/, "") || instrumentRef;
  const activationStartedAt = valueOf(
    activation,
    "created_at",
    valueOf(plan, "created_at", ""),
  );
  const activationEndedAt = valueOf(activation, "updated_at", "");
  const activationStartMs = Date.parse(activationStartedAt);
  const activationEndMs = Date.parse(activationEndedAt);
  const [runtimeChartInterval, setRuntimeChartInterval] = useState<MarketInterval>(() => (
    readChartIntervalPreference(
      status.environment_id,
      "BTCUSDT-PERP",
    )
  ));
  const [runtimeChartWindowEndAt, setRuntimeChartWindowEndAt] = useState<string | null>(null);
  const liveIntervalBeforeFocusRef = useRef<MarketInterval | null>(null);
  useEffect(() => {
    if (!instrumentRef) return;
    setRuntimeChartInterval(readChartIntervalPreference(
      status.environment_id,
      instrumentRef,
    ));
    setRuntimeChartWindowEndAt(null);
    liveIntervalBeforeFocusRef.current = null;
  }, [instrumentRef, status.environment_id]);
  const handleRuntimeChartIntervalChange = useCallback((interval: MarketInterval) => {
    setRuntimeChartInterval(interval);
    if (runtimeChartWindowEndAt !== null) {
      const nextWindowEndAt = executionWindowEndAt(activationEndMs, interval);
      if (nextWindowEndAt) setRuntimeChartWindowEndAt(nextWindowEndAt);
      return;
    }
    writeChartIntervalPreference(status.environment_id, instrumentRef, interval);
  }, [
    activationEndMs,
    instrumentRef,
    runtimeChartWindowEndAt,
    status.environment_id,
  ]);
  const [hiddenRuntimeAnnotationIds, setHiddenRuntimeAnnotationIds] = useState(
    () => new Set<string>(),
  );
  const [runtimeEventFilter, setRuntimeEventFilter] = useState<
    "ALL" | RuntimeEventCategory
  >("ALL");
  const directPriceMoveWindows = runtimeEntryConditionClauses(
    activation ?? {},
  ).flatMap((condition) => (
    condition.kind === "PRICE_MOVE_BPS" && condition.windowSeconds !== null
      ? [condition.windowSeconds]
      : []
  ));
  const marketStream = usePublicMarketStream(
    Boolean(activation && instrumentRef),
    instrumentRef,
    runtimeChartInterval,
    environmentScope,
    expectedMarketSource,
    directPriceMoveWindows,
  );
  const parameters = recordOf(strategy.parameters);
  const demoImmediateEntry = parameters.demo_immediate_entry === true;
  const stopped = Array.isArray(query.data?.stopped_categories) ? query.data.stopped_categories.map(String) : [];
  const newRiskStopped = stopped.includes("NEW_RISK");
  const ruleState = recordOf(activation?.rule_state);
  const deadlines = recordOf(ruleState.deadlines);
  const entryValidUntil = valueOf(deadlines, "entry_valid_until", "");
  const entryWindowExpired = Boolean(
    entryValidUntil && Date.parse(entryValidUntil) <= Date.now()
  );
  const entryPhaseClosed = activationEntryPhaseClosed(activation, Date.now());
  const stopEvidence = recordsOf(query.data?.stop_evidence);
  const activeAccountSystemStop = currentAccountSystemStop(stopEvidence);
  const liveReadOnly = status.profile === "BINANCE_LIVE_READ_ONLY";
  const openEntryActions = actions.filter((action) =>
    valueOf(action, "action_kind") === "ENTRY"
    && runtimeActionHasCurrentResponsibility(action)
  );
  const pendingVenueAction = runtimeHasPendingVenueAction(actions);
  const hasOpenEntryResponsibility = openEntryActions.length > 0;
  const hasPendingEntryResponsibility = openEntryActions.some(
    (action) => ["READY", "SUBMITTING", "UNKNOWN", "OPEN"].includes(
      valueOf(action, "state"),
    ),
  );
  const unknownEntryCount = openEntryActions.filter(
    (action) => valueOf(action, "state") === "UNKNOWN"
  ).length;
  const unknownEntryReason = openEntryActions
    .filter((action) => valueOf(action, "state") === "UNKNOWN")
    .map((action) => valueOf(action, "unknown_reason", ""))
    .find(Boolean);
  const channelLookback = Number(parameters.channel_lookback_15m) || 20;
  const market = useQuery({
    queryKey: [
      "activation-market-context",
      environmentScope,
      expectedMarketSource,
      activationId,
      channelLookback,
    ],
    queryFn: () => getMarketContext(valueOf(activation, "instrument_ref"), channelLookback),
    enabled: Boolean(activationId && activation && valueOf(activation, "lifecycle") === "RUNNING"),
    retry: 1,
    retryDelay: 2_000,
    refetchInterval: 15_000,
  });
  const [intent, setIntent] = useState<ControlIntent | null>(null);
  const [idempotencyKey, setIdempotencyKey] = useState<string | null>(null);
  const preview = useMutation({
    mutationFn: (next: ControlIntent) => previewControl(activationId, next),
    onSuccess: (_result, next) => {
      setIntent(next);
      setIdempotencyKey(crypto.randomUUID());
    },
  });
  const submit = useMutation({
    mutationFn: (next: ControlIntent) => {
      if (!idempotencyKey) throw new ApiFailure(409, "CONTROL_PREVIEW_REQUIRED");
      return submitActivationControlWithFreshRiskReducingRetry(
        activationId,
        next,
        {
          expected_version: Number(activation?.state_version ?? 0),
          takeover_scope: {},
        },
        idempotencyKey,
      );
    },
    onSuccess: async () => {
      setIntent(null);
      setIdempotencyKey(null);
      await queryClient.invalidateQueries({ queryKey: ["activation", activationId] });
      await queryClient.invalidateQueries({ queryKey: ["activation-timeline", activationId] });
    },
  });
  const [systemStopReleaseKey, setSystemStopReleaseKey] = useState<string | null>(null);
  const systemStopReleasePreview = useMutation({
    mutationFn: () => previewSystemStopRelease(activationId),
    onSuccess: () => {
      setSystemStopReleaseKey(crypto.randomUUID());
    },
  });
  const systemStopRelease = useMutation({
    mutationFn: () => {
      if (!systemStopReleaseKey || systemStopReleasePreview.data?.eligible !== true) {
        throw new ApiFailure(409, "SYSTEM_STOP_RELEASE_PREVIEW_REQUIRED");
      }
      return releaseSystemStop(
        activationId,
        {
          expected_stop_version: Number(activeAccountSystemStop?.version ?? 0),
          confirmation: "USER_CONFIRMED_SYSTEM_STOP_RELEASE",
        },
        systemStopReleaseKey,
      );
    },
    onSuccess: async () => {
      setSystemStopReleaseKey(null);
      systemStopReleasePreview.reset();
      await queryClient.invalidateQueries({ queryKey: ["activation", activationId] });
      await queryClient.invalidateQueries({ queryKey: ["activations"] });
    },
  });
  const systemStopReleaseDenials = Array.isArray(
    systemStopReleasePreview.data?.denial_reasons,
  )
    ? systemStopReleasePreview.data.denial_reasons.map(String)
    : [];
  const resumeEligible =
    intent !== "RESUME_ACTIVATION" || preview.data?.resume_eligible === true;
  const controls: Array<{
    intent: ControlIntent;
    label: string;
    description: string;
    color?: "warning" | "error" | "primary";
  }> = [
    {
      intent: "STOP_NEW_RISK",
      label: "停止新增风险",
      color: "warning",
      description:
        "永久停止本次激活继续开仓、加仓或重挂入场；不会自动撤单或平仓，已有保护、撤单和退出责任继续处理。",
    },
    {
      intent: "RESUME_ACTIVATION",
      label: "恢复新增入场",
      description:
        "仅解除因执行器连续性中断导致的开仓、加仓和入场重挂暂停；系统必须先确认执行器连接唯一且最新仓位一致。已有保护、撤单、减仓和退出始终继续。",
    },
    {
      intent: "EXIT_STRATEGY",
      label: directExecution ? "退出订单计划" : "退出策略",
      color: "error",
      description:
        "停止新增风险，撤销仍开放的增险订单，并按当前可归属持仓形成只减仓退出；命令被接受不表示撤单或平仓已经成交。",
    },
    {
      intent: "USER_TAKEOVER",
      label: "用户接管",
      color: "error",
      description:
        "Halpha 不再为本次激活发起新的交易所变更；你必须转到 Binance 官方入口处理持仓和挂单，已有未知动作仍会只读核对。",
    },
  ];
  const controlsForResponsibility = positionDisposition
    ? controls.filter((control) => ["RESUME_ACTIVATION", "USER_TAKEOVER"].includes(control.intent))
    : controls;
  const visibleControls = controlsForResponsibility.filter(
    (control) => control.intent !== "RESUME_ACTIVATION"
      || (
        valueOf(activation, "run_state") === "PAUSED"
        && !entryPhaseClosed
      ),
  );
  const selectedControlLabel = visibleControls.find((control) => control.intent === intent)?.label;
  const lifecycle = valueOf(activation, "lifecycle");
  const takeover = lifecycle === "USER_TAKEOVER";
  const terminal = takeover || lifecycle === "COMPLETED";
  useEffect(() => {
    setIntent(null);
    setIdempotencyKey(null);
    preview.reset();
    submit.reset();
  }, [activationId]);
  useEffect(() => {
    if (!terminal) return;
    setIntent(null);
    setIdempotencyKey(null);
    preview.reset();
  }, [terminal]);
  const terminalResultUnknown = terminal && terminalEntryResultRequiresReview(actions);
  const resultRef = valueOf(activation, "result_ref", "");
  const executorCanExecute = status.executor_status === "READY"
    && status.app_executor_product_build_consistent !== false;
  const runState = translatedLabel(runStateLabels, valueOf(activation, "run_state"));
  const pauseReason = valueOf(activation, "pause_reason", "");
  const runStateDisplay = lifecycle === "COMPLETED"
    ? "已闭合（无需运行）"
    : takeover
      ? "用户接管（机器不再运行）"
      : entryPhaseClosed
        ? "入场已结束"
        : newRiskStopped
          ? `${runState} / 新增风险已停止`
          : pauseReason
            ? `${runState} / ${translatedLabel(pauseReasonLabels, pauseReason)}`
            : runState;
  const submittedReceipt = recordOf(submit.data);
  const submittedReceiptId = valueOf(submittedReceipt, "receipt_id", "");
  const currentSubmittedReceipt = receipts.find(
    (receipt) => valueOf(receipt, "receipt_id", "") === submittedReceiptId,
  ) ?? submittedReceipt;
  const submittedReceiptState = valueOf(currentSubmittedReceipt, "state", "");
  const protectionState = valueOf(activation, "protection_state", "NONE");
  const fillCount = Number(tradeResult.fill_count ?? 0);
  const positionQuantity = Number(
    positionAttribution.activation_signed_position
      ?? tradeResult.position_quantity,
  );
  const hasEntryFill = !positionDisposition && runtimeHasEntryFill({
    projectedHasEntryFill: activation?.has_entry_fill === true,
    fillCount,
    attributedPositionQuantity: positionQuantity,
  });
  const protectionGap = hasEntryFill && tradeResult.closed !== true && !["WORKING", "CLOSED"].includes(protectionState);
  const direction = valueOf(activation, "direction");
  const marketSourceMismatch = Boolean(
    market.data
    && !isMarketSourceForEnvironment(
      market.data.source,
      status.environment_kind,
    ),
  );
  const currentMarket = marketSourceMismatch ? undefined : market.data;
  const liveQuote = marketStream.status === "LIVE"
    && marketStream.quote?.source === expectedMarketSource
    && marketStream.quote.instrument_ref === instrumentRef
      ? marketStream.quote
      : null;
  const visibleReferencePrice = liveQuote?.reference_price
    ?? currentMarket?.reference_price
    ?? "";
  const visibleBidPrice = liveQuote?.bid_price ?? currentMarket?.bid_price ?? "";
  const visibleAskPrice = liveQuote?.ask_price ?? currentMarket?.ask_price ?? "";
  const visibleMarketCutoff = liveQuote?.source_cutoff
    ?? currentMarket?.source_cutoff
    ?? "";
  const currentSpread = visibleAskPrice && visibleBidPrice
    ? subtractDecimal(visibleAskPrice, visibleBidPrice) ?? ""
    : "";
  const currentSpreadBps = visibleReferencePrice
    ? Number(currentSpread) / Number(visibleReferencePrice) * 10_000
    : Number.NaN;
  const estimatedDirectConditionEvaluation = evaluateRuntimeEntryConditions(
    activation ?? {},
    {
      basisReady: directExecution ? true : null,
      referencePrice: visibleReferencePrice || null,
      closedBar15mClose: currentMarket?.latest_close_15m ?? null,
      spreadBps: Number.isFinite(currentSpreadBps) ? currentSpreadBps : null,
      priceMoveBpsByWindow: marketStream.priceMoveBpsByWindow,
    },
  );
  const executorConditionStatus = runtimeExecutorConditionStatus(
    activation ?? {},
  );
  const directConditionEvaluation = executorConditionStatus?.evaluation
    ?? estimatedDirectConditionEvaluation;
  const latestClose1m = Number(currentMarket?.latest_close_1m);
  const breakoutBoundary = Number(
    direction === "LONG" ? currentMarket?.channel_upper : currentMarket?.channel_lower
  );
  const longClosedBarBreakoutGap = currentMarket
    ? closedBarBreakoutGapPercent("LONG", currentMarket.latest_close_1m, currentMarket.channel_upper)
    : "";
  const shortClosedBarBreakoutGap = currentMarket
    ? closedBarBreakoutGapPercent("SHORT", currentMarket.latest_close_1m, currentMarket.channel_lower)
    : "";
  const latestClosedBarBeyondBoundary = Number.isFinite(latestClose1m)
    && Number.isFinite(breakoutBoundary)
    && (direction === "LONG"
      ? latestClose1m > breakoutBoundary
      : latestClose1m < breakoutBoundary);
  const maxEntryExtensionAtr = valueOf(parameters, "max_entry_extension_atr");
  const entryExtensionLimit = currentMarket
    ? entryExtensionBoundary(
      direction === "SHORT" ? "SHORT" : "LONG",
      direction === "LONG" ? currentMarket.channel_upper : currentMarket.channel_lower,
      currentMarket.atr_14,
      maxEntryExtensionAtr,
    )
    : null;
  const latestClosedBarBeyondExtension = Number.isFinite(latestClose1m)
    && entryExtensionLimit !== null
    && (direction === "LONG"
      ? latestClose1m > entryExtensionLimit
      : latestClose1m < entryExtensionLimit);
  const confirmationBars = Number(parameters.confirmation_bars_1m) || 1;
  const latestNoActionEvent = [...(timelineQuery.data ?? [])].reverse().find((item) => {
    const detail = recordOf(item.detail);
    return valueOf(item, "source", "") === "PLAN_EVENT"
      && Boolean(valueOf(detail, "no_action_reason", ""))
      && valueOf(detail, "rule_id", "") !== "EXECUTOR_RUNTIME_CONTINUITY";
  });
  const latestNoActionDetail = recordOf(latestNoActionEvent?.detail);
  const latestNoActionCode = valueOf(latestNoActionDetail, "no_action_reason", "");
  const latestNoActionRuleId = valueOf(latestNoActionDetail, "rule_id", "");
  const latestNoActionText = latestNoActionCode
    ? runtimeNoActionPresentation(latestNoActionCode).headline
    : "";
  const latestNoActionAt = Date.parse(valueOf(latestNoActionEvent, "at", ""));
  const hasEntryActionAtOrAfterLatestNoAction = Number.isFinite(latestNoActionAt)
    && (timelineQuery.data ?? []).some((item) => {
      const detail = recordOf(item.detail);
      return valueOf(item, "source", "") === "EXECUTION_ACTION"
        && valueOf(detail, "action_kind", "") === "ENTRY"
        && Date.parse(valueOf(item, "at", "")) >= latestNoActionAt;
    });
  const directPreSubmitBlocked = directExecution
    && latestNoActionRuleId === "DIRECT_PRE_SUBMIT"
    && lifecycle === "RUNNING"
    && !hasEntryFill
    && !hasEntryActionAtOrAfterLatestNoAction;
  const fillCashFlow = Number(tradeResult.fill_cash_flow);
  const venueAccountPosition = Number(
    positionAttribution.venue_account_signed_position,
  );
  const attributedAccountPosition = Number(
    positionAttribution.attributed_account_signed_position,
  );
  const positionReconciliationStatus = valueOf(
    positionAttribution,
    "reconciliation_status",
    "UNKNOWN",
  );
  const positionFactsAwaitingSameCutoff = positionReconciliationStatus === "STALE";
  const venuePositionIsShared = Number.isFinite(positionQuantity)
    && Number.isFinite(attributedAccountPosition)
    && Math.abs(attributedAccountPosition - positionQuantity) > 1e-12;
  const attributedCommission = Number(tradeResult.commission);
  const attributedFunding = Number(tradeResult.funding ?? 0);
  const attributedFundingConfirmed = tradeResult.funding_included === true;
  const referencePrice = positiveFiniteNumber(visibleReferencePrice) ?? Number.NaN;
  const firstFill = recordOf(ruleState.first_fill);
  const entryRiskContext = recordOf(firstFill.entry_risk_context);
  const immediateExitEstimate = tradeResult.calculation_complete === true
    ? estimateImmediateExit(
      positionQuantity,
      fillCashFlow,
      attributedCommission,
      Number(visibleBidPrice),
      Number(visibleAskPrice),
      Number(entryRiskContext.sizing_taker_fee_rate),
    )
    : null;
  const immediateExitNetWithFunding = immediateExitEstimate
    && Number.isFinite(attributedFunding)
      ? immediateExitEstimate.netResult
        + (attributedFundingConfirmed ? attributedFunding : 0)
      : null;
  const averageEntryPrice = Number(tradeResult.average_entry_price);
  const protectionTriggerPrice = currentRuntimeProtectionPrice(actions, direction);
  const takeProfitActions = actions
    .filter((action) => valueOf(action, "action_kind") === "TAKE_PROFIT");
  const takeProfitGroups = groupRuntimeTakeProfits(takeProfitActions, direction);
  const workingTakeProfitCount = takeProfitGroups
    .reduce((total, group) => total + group.workingOrderCount, 0);
  const takeProfitOrderCount = takeProfitGroups
    .reduce((total, group) => total + group.orderCount, 0);
  const closedNetAvailable = tradeResult.calculation_complete === true
    && tradeResult.closed === true
    && Number.isFinite(Number(tradeResult.net_pnl));
  const terminalWithoutVenueAction = terminal
    && actions.length === 0
    && facts.length === 0;
  const zeroExposureReconciliation = Number.isFinite(positionQuantity)
    && positionQuantity === 0
    && Number.isFinite(venueAccountPosition)
    && venueAccountPosition === 0
    && Number.isFinite(attributedAccountPosition)
    && attributedAccountPosition === 0;
  const terminalNoFill = terminal
    && !terminalResultUnknown
    && !hasEntryFill
    && fillCount === 0
    && (
      terminalWithoutVenueAction
      || (
        Number.isFinite(positionQuantity)
        && positionQuantity === 0
        && positionReconciliationStatus === "MATCH"
      )
    );
  const venueFactCutoff = latestUtc([
    valueOf(activation, "latest_venue_cutoff", ""),
    ...facts.map((fact) => valueOf(fact, "cutoff", "")),
  ]);
  const orderFactCutoff = latestUtc(
    facts
      .filter((fact) => valueOf(fact, "kind", "") === "ORDER_STATE")
      .map((fact) => valueOf(fact, "cutoff", "")),
  );
  const planName = valueOf(plan, "plan_name", "") || "未命名计划";
  const scheduleSpecRecord = orderScheduleSpecOf(orderSchedule);
  const entryOrderDeadline = runtimeEntryOrderDeadline(
    actions,
    scheduleSpecRecord,
    entryValidUntil,
  );
  const scheduleProjectionReady = Object.keys(scheduleSpecRecord).length > 0;
  const hasSteppedProtection = recordsOf(scheduleSpecRecord.dynamic_rules)
    .some((rule) => valueOf(rule, "kind") === "STEPPED_PROTECTION");
  const protectionPolicy = recordOf(scheduleSpecRecord.protection_policy);
  const takeProfitPolicy = recordOf(protectionPolicy.take_profit_ladder);
  const configuredTakeProfitLevels = recordsOf(takeProfitPolicy.levels);
  const configuredTimeExitSeconds = finiteNumber(protectionPolicy.time_exit_seconds);
  const nextProtectionStep = nextRuntimeProtectionStep(
    scheduleSpecRecord,
    ruleState,
    actions,
    facts,
    direction,
    referencePrice,
  );
  const firstFillAt = facts
    .filter((fact) =>
      valueOf(fact, "kind", "") === "FILL"
      && valueOf(fact, "attribution_class", "") === "HALPHA_EXECUTION"
    )
    .map((fact) => valueOf(fact, "source_time", valueOf(fact, "cutoff", "")))
    .filter((value) => Number.isFinite(Date.parse(value)))
    .sort((left, right) => Date.parse(left) - Date.parse(right))[0] ?? "";
  const exitActionStarted = actions.some((action) =>
    valueOf(action, "action_kind") === "EXIT"
    && ["OPEN", "CLOSED"].includes(valueOf(action, "state")),
  );
  const protectionAttention = runtimeProtectionAttention({
    hasEntryFill,
    tradeClosed: tradeResult.closed === true,
    protectionState,
    lifecycle,
    exitActionStarted,
    firstFillAt,
    timeExitSeconds: configuredTimeExitSeconds,
    nowMs: runtimeNowMs,
  });
  const exitHandoff = protectionAttention === "EXIT_HANDOFF";
  const unexpectedProtectionGap = protectionAttention === "UNEXPECTED_GAP";
  const protectionDisplay = exitHandoff
    ? "退出交接中"
    : positionDisposition
      ? "外部基线 · 仅处置责任"
      : hasEntryFill
      ? translatedLabel(protectionStateLabels, protectionState)
      : unknownEntryCount > 0
        ? "成交未知（保护不可证明）"
        : "未入场（无需保护）";
  const runtimeChartSpec = scheduleProjectionReady
    ? scheduleSpecRecord as unknown as OrderScheduleSpec
    : RUNTIME_CHART_FALLBACK_SPEC;
  const runtimeChartLegs = recordsOf(
    orderSchedule.normalized_legs,
  ) as unknown as OrderSchedulePreviewLeg[];
  const entryInterruption = useMemo(
    () => runtimeEntryInterruptionPresentation(
      timelineQuery.data ?? [],
      orderSchedule,
    ),
    [orderSchedule, timelineQuery.data],
  );
  const plannedEntryLegCount = runtimeChartLegs.length;
  const filledEntryLegCount = runtimeFilledEntryLegCount(
    actions,
    facts,
    plannedEntryLegCount,
  );
  const allPlannedEntryLegsFilled = plannedEntryLegCount > 0
    && filledEntryLegCount >= plannedEntryLegCount;
  const showRuntimePlanEntryAnnotations = lifecycle === "RUNNING"
    && !newRiskStopped
    && !entryWindowExpired
    && entryInterruption === null
    && !allPlannedEntryLegsFilled;
  const instrumentRules = recordOf(orderSchedule.instrument_rules);
  const runtimePriceTickSize = valueOf(instrumentRules, "price_tick_size", "") || null;
  const floatingPnl = Number.isFinite(positionQuantity)
    && positionQuantity !== 0
    && Number.isFinite(averageEntryPrice)
    && Number.isFinite(referencePrice)
      ? (direction === "SHORT"
          ? averageEntryPrice - referencePrice
          : referencePrice - averageEntryPrice) * Math.abs(positionQuantity)
      : null;
  const attributedMarkedNet = tradeResult.calculation_complete === true
    ? estimateMarkedNetResult(
      positionQuantity,
      fillCashFlow,
      attributedCommission,
      referencePrice,
      attributedFundingConfirmed ? attributedFunding : 0,
    )
    : null;
  const runtimeChartAnnotations = useMemo<OrderChartPriceAnnotation[]>(() => {
    const result: OrderChartPriceAnnotation[] = [];
    if (
      Number.isFinite(averageEntryPrice)
      && averageEntryPrice > 0
      && fillCount > 0
    ) {
      result.push({
        id: "halpha-runtime-position-average",
        role: "POSITION",
        label: terminal ? "本次平均入场价" : "本计划持仓均价",
        detail: Number.isFinite(positionQuantity) && positionQuantity !== 0
          ? `本计划虚拟持仓 ${marketVolume(String(Math.abs(positionQuantity)))} ${runtimeBaseAsset}`
          : "本次已归属成交的平均入场价格",
        price: averageEntryPrice,
        authority: "SERVER_FACT",
        lineStyle: "solid",
        draggable: false,
      });
    }
    actions.forEach((action) => {
      const actionKind = valueOf(action, "action_kind", "");
      const role = actionKind === "PROTECTION"
        ? "PROTECTION"
        : actionKind === "TAKE_PROFIT"
          ? "TAKE_PROFIT"
          : actionKind === "ENTRY"
            ? "RUNTIME_ENTRY"
            : null;
      if (role === null) return;
      const terms = recordOf(action.action_terms);
      const priceValue = Number(terms.trigger_price ?? terms.price);
      if (!Number.isFinite(priceValue) || priceValue <= 0) return;
      const state = valueOf(action, "state", "");
      if (!runtimeActionHasCurrentResponsibility(action)) {
        return;
      }
      const actionId = valueOf(action, "execution_action_id", "");
      const actionLabel = role === "PROTECTION"
        ? hasSteppedProtection
          ? "移动止损"
          : "止损"
        : role === "TAKE_PROFIT"
          ? "止盈"
          : translatedLabel(actionKindLabels, actionKind);
      result.push({
        id: `halpha-runtime-action-${actionId}`,
        role,
        label: `${actionLabel} · ${translatedLabel(actionStateLabels, state)}`,
        detail: `${terms.reduce_only === true ? "只减仓 · " : ""}数量 ${marketVolume(valueOf(terms, "quantity", ""))} ${runtimeBaseAsset}`,
        price: priceValue,
        authority: "SERVER_FACT",
        lineStyle: state === "OPEN" ? "solid" : "dashed",
        draggable: false,
      });
    });
    return result;
  }, [
    actions,
    averageEntryPrice,
    fillCount,
    hasSteppedProtection,
    positionQuantity,
    runtimeBaseAsset,
    terminal,
  ]);
  const visibleRuntimeAnnotationIds = useMemo(
    () => new Set(
      runtimeChartAnnotations
        .filter((annotation) => !hiddenRuntimeAnnotationIds.has(annotation.id))
        .map((annotation) => annotation.id),
    ),
    [hiddenRuntimeAnnotationIds, runtimeChartAnnotations],
  );
  const runtimeAnnotationsByRole = useMemo(() => {
    const result = new Map<
      OrderChartPriceAnnotation["role"],
      OrderChartPriceAnnotation[]
    >();
    runtimeChartAnnotations.forEach((annotation) => {
      const current = result.get(annotation.role) ?? [];
      current.push(annotation);
      result.set(annotation.role, current);
    });
    return result;
  }, [runtimeChartAnnotations]);
  const availableRuntimeAnnotationRoles = useMemo(
    () => new Set(runtimeChartAnnotations.map((annotation) => annotation.role)),
    [runtimeChartAnnotations],
  );
  const setRuntimeAnnotationVisibility = useCallback((annotationId: string, visible: boolean) => {
    setHiddenRuntimeAnnotationIds((current) => {
      const next = new Set(current);
      if (visible) next.delete(annotationId);
      else next.add(annotationId);
      return next;
    });
  }, []);
  const setRuntimeAnnotationRoleVisibility = useCallback((
    role: OrderChartPriceAnnotation["role"],
    visible: boolean,
  ) => {
    setHiddenRuntimeAnnotationIds((current) => {
      const next = new Set(current);
      (runtimeAnnotationsByRole.get(role) ?? []).forEach((annotation) => {
        if (visible) next.delete(annotation.id);
        else next.add(annotation.id);
      });
      return next;
    });
  }, [runtimeAnnotationsByRole]);
  const actionsByRef = useMemo(
    () => new Map(actions.map((action) => [
      valueOf(action, "execution_action_id", ""),
      action,
    ])),
    [actions],
  );
  const workingEntryOrders = useMemo(
    () => runtimeWorkingEntryOrders(actions, facts),
    [actions, facts],
  );
  const compactedTimeline = useMemo(
    () => compactRuntimeTimeline(timelineQuery.data ?? [], facts),
    [timelineQuery.data, facts],
  );
  const priorBlockingReasonByTimelineEntry = useMemo(() => {
    const result = new Map<CompactRuntimeTimelineItem, string>();
    let latestReason = "";
    compactedTimeline.forEach((entry) => {
      result.set(entry, latestReason);
      const item = entry.item;
      const detail = recordOf(item.detail);
      const reason = valueOf(detail, "no_action_reason", "");
      if (
        valueOf(item, "source", "") === "PLAN_EVENT"
        && reason
        && reason !== "ENTRY_WINDOW_EXPIRED"
        && reason !== "ENTRY_REMAINING_EXPIRED"
      ) {
        latestReason = reason;
      }
    });
    return result;
  }, [compactedTimeline]);
  const runtimeOperationMarkers = useMemo(
    () => runtimeChartOperationMarkers({
      activationStartedAt,
      actions,
      facts,
      timeline: timelineQuery.data ?? [],
    }),
    [activationStartedAt, actions, facts, timelineQuery.data],
  );
  const categorizedTimeline = useMemo(
    () => compactedTimeline.map((entry) => {
      const actionRef = entry.fact
        ? valueOf(entry.fact, "action_ref", "")
        : valueOf(entry.item, "source") === "EXECUTION_ACTION"
          ? valueOf(entry.item, "source_ref", "")
          : "";
      return {
        entry,
        category: runtimeEventCategory(entry, actionsByRef.get(actionRef)),
      };
    }),
    [actionsByRef, compactedTimeline],
  );
  const filteredTimeline = categorizedTimeline
    .filter(({ category }) => runtimeEventFilter === "ALL" || category === runtimeEventFilter)
    .map(({ entry }) => entry);
  const latestRecordedFactAt = latestUtc(
    [
      ...facts.map((fact) => valueOf(fact, "cutoff", "")),
      ...actions.map((action) => valueOf(action, "updated_at", "")),
    ],
  );
  const latestFilteredEventAt = latestUtc(
    filteredTimeline.map((entry) => valueOf(entry.item, "at", "")),
  );
  const executionWindowFullyVisible = executionWindowFitsInterval(
    activationStartMs,
    activationEndMs,
    runtimeChartInterval,
  );
  const toggleExecutionWindow = () => {
    if (runtimeChartWindowEndAt !== null) {
      const previousInterval = liveIntervalBeforeFocusRef.current;
      setRuntimeChartWindowEndAt(null);
      liveIntervalBeforeFocusRef.current = null;
      if (previousInterval) setRuntimeChartInterval(previousInterval);
      return;
    }
    const focusInterval = defaultExecutionWindowInterval(
      activationStartMs,
      activationEndMs,
    );
    const focusEndAt = executionWindowEndAt(activationEndMs, focusInterval);
    if (!focusEndAt) return;
    liveIntervalBeforeFocusRef.current = runtimeChartInterval;
    setRuntimeChartInterval(focusInterval);
    setRuntimeChartWindowEndAt(focusEndAt);
  };
  const returnFromActivation = () => {
    if (location.key !== "default") {
      navigate(-1);
      return;
    }
    navigate(lifecycle === "COMPLETED" ? "/reviews" : "/plans", { replace: true });
  };
  if (!activation) {
    return (
      <Box sx={{ width: "min(1480px, calc(100% - clamp(24px, 3vw, 48px)))", mx: "auto", py: { xs: 2, sm: 2.5 } }}>
        <Typography variant="h1" sx={{ mb: 2 }}>计划详情与复盘</Typography>
        {query.isPending
          ? <LinearProgress aria-label="正在读取计划详情" />
          : <Alert severity="error">计划详情暂不可用；页面不会用占位内容冒充真实计划。</Alert>}
      </Box>
    );
  }
  return (
    <Box sx={{ width: "min(1480px, calc(100% - clamp(24px, 3vw, 48px)))", mx: "auto", py: { xs: 2, sm: 2.5 } }}>
      <Box component="header" sx={{ mb: 2.5 }}>
        <Stack direction="row" spacing={1} sx={{ alignItems: "center", minWidth: 0 }}>
          <Tooltip title="返回上一页" arrow>
            <IconButton
              aria-label="返回上一页"
              onClick={returnFromActivation}
              sx={{ width: 40, height: 40, flexShrink: 0 }}
            >
              <ArrowBackOutlined />
            </IconButton>
          </Tooltip>
          <Typography variant="h1" sx={{ minWidth: 0 }}>{planName}</Typography>
        </Stack>
        <Typography
          variant="body2"
          color="text.secondary"
          sx={{ ml: 6, mt: 0.5, fontWeight: 700 }}
        >
          {instrumentRef} / {translatedLabel(directionLabels, direction)}
        </Typography>
      </Box>
      {planDecisionContextRows.length > 0 && <Box
        component="section"
        aria-label="决策记录"
        sx={{ ...surfaceFrameSx, mb: 2, p: 2 }}
      >
        <Stack
          direction={{ xs: "column", sm: "row" }}
          spacing={0.5}
          sx={{ justifyContent: "space-between", alignItems: { xs: "flex-start", sm: "baseline" }, mb: 1.25 }}
        >
          <Typography component="h2" variant="subtitle2">决策记录</Typography>
          <Typography variant="caption" color="text.secondary">
            随运行快照保存；仅用于执行前核对与事后复盘，不构成触发或下单条件。
          </Typography>
        </Stack>
        <Box sx={{
          display: "grid",
          gridTemplateColumns: { xs: "1fr", md: "repeat(3, minmax(0, 1fr))" },
          gap: 1,
        }}>
          {planDecisionContextRows.map((item) => <Box
            key={item.label}
            sx={{ p: 1.25, border: 1, borderColor: "divider", borderRadius: 1, minWidth: 0 }}
          >
            <Typography variant="caption" color="text.secondary">{item.label}</Typography>
            <Typography variant="body2" sx={{ mt: 0.5, overflowWrap: "anywhere" }}>{item.value}</Typography>
          </Box>)}
        </Box>
      </Box>}
      {!positionDisposition ? (
        <PlanAiReviewRecord
          review={query.data?.ai_review}
          reviewRef={query.data?.ai_review_ref}
          required={Boolean(query.data?.ai_review_ref)}
          sx={{ mb: 2 }}
        />
      ) : null}
      {planDecisionIntent === "VALIDATION" && <Alert severity="info" variant="outlined" sx={{ mb: 2 }}>
        本计划在交易前已记录为机制 / 软件验证。实际盈亏仍计入账户结果；若形成交易且工具问题未实质影响结果，复盘应归为“验证性交易”，不能因盈利改成策略可用样本。未成交则归为“未形成交易”。
      </Alert>}
      {(query.isPending || timelineQuery.isPending) && <LinearProgress aria-label="正在读取激活与时间线" />}
      {(query.isError || timelineQuery.isError) && <Alert severity="error" sx={{ mb: 2 }}>当前服务器事实不可确认；页面不会把旧缓存冒充当前事实，也不会开放离线资本命令。</Alert>}
      {marketSourceMismatch && <Alert severity="error" variant="outlined" sx={{ mb: 2 }}>
        行情来源与当前 {status.environment_kind} 环境不一致，已拒绝显示；执行器不会使用页面缓存替代当前环境事实。
      </Alert>}
      {positionDisposition && <Alert severity="info" variant="outlined" sx={{ mb: 3 }}>
        这是{positionAlignmentOperationLabel(positionAlignment)}计划：原始基线 {marketVolume(valueOf(positionAlignment, "baseline_quantity"))} {runtimeBaseAsset}，本次只处置 {marketVolume(valueOf(positionAlignment, "requested_reduction_quantity"))}，目标剩余 {marketVolume(valueOf(positionAlignment, "target_quantity_after"))}。既有入场、历史资金费和策略盈亏不归属 Halpha。
      </Alert>}
      {exitHandoff && <Alert severity="warning" variant="outlined" sx={{ mb: 3 }}>
        退出交接中：系统正在逐一撤销本计划的保护与止盈，重新核对最新归属仓位，并提交只减仓退出。交易所确认平仓前仍有价格风险；无需重复点击退出。
      </Alert>}
      {unexpectedProtectionGap && <Alert severity="error" variant="filled" sx={{ mb: 3 }}>存在已确认敞口，但交易所原生保护尚未证明为工作中。保持在线并核对 Binance 官方入口；任何“停止”或回执都不代表已经安全。</Alert>}
      {!positionDisposition && positionReconciliationStatus === "MISMATCH" && <Alert severity="error" variant="filled" sx={{ mb: 3 }}>
        交易所合并仓位与全部计划虚拟持仓合计不一致。系统已停止依赖该归因扩大或新建风险；请核对外部交易、迟到成交和 Binance 官方仓位。
      </Alert>}
      {!positionDisposition && positionReconciliationStatus === "STALE" && <Alert severity="info" variant="outlined" sx={{ mb: 3 }}>
        当前计划归因已产生新事实，交易所合并仓位快照尚未刷新；页面不会把不同截止点的数据误报为一致或异常。
      </Alert>}
      {!positionDisposition && venuePositionIsShared && <Alert severity="info" variant="outlined" sx={{ mb: 3 }}>
        当前为交易所单向合并仓位：Binance 不提供计划级物理子仓。Halpha 只按本计划订单身份和虚拟剩余量撤单、止盈、止损或退出；若撤单与保护触发发生竞争，将按迟到成交重新核对并停止新增风险。
      </Alert>}
      {unknownEntryCount > 0 && (
        <Alert severity="error" variant="filled" sx={{ mb: 3 }}>
          有 {unknownEntryCount} 个入场动作结果未决：
          {unknownExecutionReasonText(unknownEntryReason ?? "")}。
          系统只查询原订单 UUID，并暂停新的入场动作。
        </Alert>
      )}
      {retryablePostOnlyRejection
        && !entryPolicyRetryLimitReached
        && rejectedEntryReason
        && !hasEntryFill
        && lifecycle === "RUNNING" && (
        <Alert severity="info" variant="outlined" sx={{ mb: 3 }}>
          上一次 Maker only 尝试因会立即成交而被交易所正常拒绝；未成交、未建仓。系统保持原价格和 Maker only，并在入场条件、有效期及行情失效规则仍允许时自动重挂，最多 {POST_ONLY_RETRY_MAX_ATTEMPTS} 次。
        </Alert>
      )}
      {retryablePriceMatchRejection
        && !entryPolicyRetryLimitReached
        && rejectedEntryReason
        && !hasEntryFill
        && lifecycle === "RUNNING" && (
        <Alert severity="info" variant="outlined" sx={{ mb: 3 }}>
          上一次盘口价尝试被交易所拒绝；订单未创建、未成交、未建仓。系统保持原盘口价档位，并在入场条件、有效期及行情失效规则仍允许时自动重试，最多 {POST_ONLY_RETRY_MAX_ATTEMPTS} 次。
        </Alert>
      )}
      {entryPolicyRetryLimitReached && rejectedEntryReason && !hasEntryFill && lifecycle === "RUNNING" && (
        <Alert severity="info" variant="outlined" sx={{ mb: 3 }}>
          本次入场指令已完成 {POST_ONLY_RETRY_MAX_ATTEMPTS} 次有界重试，仍均被交易所拒绝；未成交、未建仓。系统不会继续提交，本计划等待入场截止或行情失效条件结束，不视为系统故障。
        </Alert>
      )}
      {normalFokNoFill && rejectedEntryReason && !hasEntryFill && (
        <Alert severity="info" variant="outlined" sx={{ mb: 3 }}>
          {rejectedEntryReason}。该档已正常结束；若计划还有独立的后续档位，仍按原计划继续。
        </Alert>
      )}
      {!retryableEntryRejection && !normalFokNoFill && rejectedEntryReason && !hasEntryFill && (
        <Alert severity="warning" variant="outlined" sx={{ mb: 3 }}>
          本次入场被交易所拒绝：{rejectedEntryReason} 系统不会把未知拒绝自动重试为新的风险。
        </Alert>
      )}
      {!positionDisposition && entryWindowExpired && !terminal && !hasEntryFill && !hasOpenEntryResponsibility && <Alert severity="warning" variant="outlined" sx={{ mb: 3 }}>入场窗口已经到期，不能再产生新的入场动作；Executor 正在闭合本次无入场激活。</Alert>}
      {demoImmediateEntry && !terminal && !hasEntryFill && !hasOpenEntryResponsibility && !newRiskStopped && <Alert severity="warning" variant="outlined" sx={{ mb: 3 }}>本次为下单流程验证：下一根有效闭合 1m 将触发一次入场，不代表策略出现突破信号。</Alert>}
      {directPreSubmitBlocked && <Alert severity="warning" variant="filled" sx={{ mb: 3 }}>
        入场尚未创建：{latestNoActionText}。Executor 保持失败关闭并按节奏重试；修正当前环境的账户设置或事实后无需重建计划。
      </Alert>}
      {takeover && <Alert severity="warning" variant="outlined" sx={{ mb: 3 }}>用户接管已持久化。Halpha 不再提交新的待执行动作，也不会自动撤单、补保护或平仓；请在 Binance 官方入口处理，页面仅只读核对迟到事实与开放责任。</Alert>}
      {activation && <Box
        component="section"
        aria-label="计划运行主视图"
        sx={{
          display: "grid",
          gridTemplateColumns: "minmax(0, 1fr)",
          "@media (min-width: 1100px)": {
            gridTemplateColumns: "minmax(0, 1fr) 380px",
          },
          gap: 1.5,
          alignItems: "start",
        }}
      >
        <Box
          sx={{
            minWidth: 0,
            "@media (min-width: 1100px)": {
              height: "clamp(620px, calc(100vh - 180px), 760px)",
            },
          }}
        >
          <Suspense fallback={<LinearProgress aria-label="正在加载计划运行 K 线" />}>
            <OrderScheduleChart
              workspaceMode
              displayMode="RUNTIME"
              runtimePhase={lifecycle === "COMPLETED" ? "REVIEW" : "RUNNING"}
              showPlanEntryAnnotations={showRuntimePlanEntryAnnotations}
              additionalPriceAnnotations={runtimeChartAnnotations}
              visibleAdditionalPriceAnnotationIds={visibleRuntimeAnnotationIds}
              onAdditionalPriceAnnotationVisibilityChange={setRuntimeAnnotationVisibility}
              operationMarkers={runtimeOperationMarkers}
              timeWindowMode={runtimeChartWindowEndAt ? "EXECUTION" : "LIVE"}
              timeWindowEndAt={runtimeChartWindowEndAt}
              executionWindowStartAt={activationStartedAt}
              executionWindowEndAt={lifecycle === "COMPLETED"
                ? activationEndedAt
                : null}
              executionWindowFullyVisible={executionWindowFullyVisible}
              onToggleTimeWindow={lifecycle === "COMPLETED"
                ? toggleExecutionWindow
                : undefined}
              environmentId={status.environment_id}
              environmentKind={status.environment_kind}
              instrumentRef={instrumentRef}
              direction={direction === "SHORT" ? "SHORT" : "LONG"}
              marketColorScheme={marketColorScheme}
              interval={runtimeChartInterval}
              onIntervalChange={handleRuntimeChartIntervalChange}
              liveBar={marketStream.liveBar}
              streamStatus={marketStream.status}
              streamGeneration={marketStream.generation}
              priceProjectionReady={scheduleProjectionReady}
              priceTickSize={runtimePriceTickSize}
              referencePrice={visibleReferencePrice || null}
              spec={runtimeChartSpec}
              previewLegs={runtimeChartLegs}
              previewState={scheduleProjectionReady ? "READY" : "BLOCKED"}
              onRangeChange={() => undefined}
              onSingleLimitPriceChange={() => undefined}
            />
          </Suspense>
        </Box>
        <Box sx={{
          ...surfaceFrameSx,
          p: 2,
          minWidth: 0,
          "@media (min-width: 1100px)": {
            height: "clamp(620px, calc(100vh - 180px), 760px)",
            overflowY: "auto",
            overscrollBehavior: "contain",
          },
        }}>
          <Stack direction="row" spacing={0.75} useFlexGap sx={{ flexWrap: "wrap", mb: 1.5 }}>
            <Chip size="small" label={translatedLabel(lifecycleLabels, lifecycle)} color={terminal ? "default" : "primary"} />
            {!terminal && runStateDisplay !== translatedLabel(lifecycleLabels, lifecycle) && (
              <Chip size="small" variant="outlined" label={runStateDisplay} />
            )}
            <Chip
              size="small"
              variant="outlined"
              color={positionDisposition ? "warning" : exitHandoff ? "warning" : protectionGap ? "error" : protectionState === "WORKING" ? "success" : "default"}
              label={positionDisposition ? protectionDisplay : `保护 · ${protectionDisplay}`}
            />
          </Stack>
          <FreshnessStrip
            marketCutoff={visibleMarketCutoff}
            positionCutoff={valueOf(positionAttribution, "fact_cutoff", "")}
            orderCutoff={orderFactCutoff}
            positionApplicable={(positionDisposition || hasEntryFill) && !terminalNoFill}
            orderApplicable={
              Boolean(orderFactCutoff)
              || (!terminalNoFill && pendingVenueAction)
            }
          />
          {entryInterruption && (
            <Box
              sx={{
                mb: 1.5,
                p: 1.25,
                border: 1,
                borderColor: "warning.main",
                borderRadius: 1,
                bgcolor: "action.hover",
              }}
            >
              <Typography variant="caption" color="text.secondary">
                入场进度
              </Typography>
              <Typography variant="body2" sx={{ fontWeight: 780 }}>
                {plannedEntryLegCount > 0
                  ? `${filledEntryLegCount}/${plannedEntryLegCount} 档有成交 · 后续入场已停止`
                  : "后续入场已停止"}
              </Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                {entryInterruption.detail}
                {hasEntryFill ? "；已有持仓的保护与退出继续执行" : ""}
              </Typography>
              {entryInterruption.at && (
                <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                  {formatUserVisibleTime(entryInterruption.at)}
                </Typography>
              )}
            </Box>
          )}
          {directExecution
            && lifecycle === "RUNNING"
            && !hasEntryFill
            && !hasOpenEntryResponsibility
            && !newRiskStopped
            && (
              <Box sx={{ mb: 1.5 }}>
                <DirectConditionStatusPanel
                  evaluation={directConditionEvaluation}
                  executorStatus={executorConditionStatus}
                />
              </Box>
            )}
          <Stack direction="row" spacing={0.5} sx={{ alignItems: "center", mb: 1.5 }}>
            <Typography variant="h2">{positionDisposition ? "持仓处置进度" : "当前敞口与关键价格"}</Typography>
            <Tooltip
              arrow
              title={(
                <Box>
                  {positionDisposition ? <>
                    <Typography variant="caption" sx={{ display: "block" }}>
                      剩余处置责任从固定减仓数量开始，仅由本计划的 reduce-only 成交递减。
                    </Typography>
                    <Typography variant="caption" sx={{ display: "block" }}>
                      原始外部入场、历史资金费和盈亏不计入 Halpha 结果。
                    </Typography>
                  </> : <>
                    <Typography variant="caption" sx={{ display: "block" }}>
                      未实现盈亏只计算本计划剩余虚拟仓位。
                    </Typography>
                    <Typography variant="caption" sx={{ display: "block" }}>
                      标记净额包含本计划已实现与未实现价差、已归属手续费
                      {attributedFundingConfirmed ? "和资金费" : "；尚无可确认资金费时不计资金费"}。
                    </Typography>
                    <Typography variant="caption" sx={{ display: "block" }}>
                      两者都不含未来退出滑点与费用，也不参与执行控制。
                    </Typography>
                  </>}
                </Box>
              )}
            >
              <IconButton size="small" aria-label={positionDisposition ? "持仓处置口径说明" : "盈亏估算口径说明"}>
                <InfoOutlined fontSize="small" />
              </IconButton>
            </Tooltip>
          </Stack>
          {availableRuntimeAnnotationRoles.size > 0 && (
            <Box sx={{ mb: 1.5 }}>
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.25 }}>
                图上标记
              </Typography>
              <FormGroup row sx={{ columnGap: 0.75, rowGap: 0 }}>
                {([
                  ["POSITION", "入场均价"],
                  ["RUNTIME_ENTRY", "入场委托"],
                  ["PROTECTION", hasSteppedProtection ? "移动止损" : "止损"],
                  ["TAKE_PROFIT", "止盈"],
                ] as Array<[OrderChartPriceAnnotation["role"], string]>)
                  .filter(([role]) => availableRuntimeAnnotationRoles.has(role))
                  .map(([role, label]) => {
                    const roleAnnotations = runtimeAnnotationsByRole.get(role) ?? [];
                    const visibleCount = roleAnnotations.filter(
                      (annotation) => visibleRuntimeAnnotationIds.has(annotation.id),
                    ).length;
                    return (
                      <FormControlLabel
                        key={role}
                        sx={{ m: 0 }}
                        control={(
                          <Checkbox
                            size="small"
                            checked={roleAnnotations.length > 0 && visibleCount === roleAnnotations.length}
                            indeterminate={visibleCount > 0 && visibleCount < roleAnnotations.length}
                            onChange={(event) => {
                              setRuntimeAnnotationRoleVisibility(role, event.target.checked);
                            }}
                            slotProps={{
                              input: {
                                "aria-label": `图上${label}`,
                                "aria-checked": visibleCount > 0 && visibleCount < roleAnnotations.length
                                  ? "mixed"
                                  : roleAnnotations.length > 0 && visibleCount === roleAnnotations.length,
                              },
                            }}
                          />
                        )}
                        label={<Typography variant="caption">{label}</Typography>}
                      />
                    );
                  })}
              </FormGroup>
            </Box>
          )}
          <Box sx={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 1, mb: 2 }}>
            {(positionDisposition ? [
              {
                label: "剩余处置责任",
                value: Number.isFinite(positionQuantity) && positionQuantity !== 0
                  ? `${marketVolume(String(Math.abs(positionQuantity)))} ${runtimeBaseAsset}`
                  : "已完成或待最终核对",
              },
              {
                label: "原始外部基线",
                value: `${marketVolume(valueOf(positionAlignment, "baseline_quantity"))} ${runtimeBaseAsset}`,
              },
              {
                label: "本次处置 / 目标剩余",
                value: `${marketVolume(valueOf(positionAlignment, "requested_reduction_quantity"))} / ${marketVolume(valueOf(positionAlignment, "target_quantity_after"))} ${runtimeBaseAsset}`,
              },
              {
                label: "交易所当前仓位",
                value: Number.isFinite(venueAccountPosition)
                  ? `${marketVolume(String(Math.abs(venueAccountPosition)))} ${runtimeBaseAsset}`
                  : "等待同点账户事实",
              },
              {
                label: "处置动作",
                value: actions.some((action) => ["RISK_REDUCTION", "EXIT"].includes(valueOf(action, "action_kind")))
                  ? "已形成 · 等待场所事实闭合"
                  : "尚未形成场所动作",
              },
              {
                label: "既有入场与历史盈亏",
                value: "外部事实 · 不归属 Halpha",
              },
            ] : [
              {
                label: "本计划虚拟持仓",
                value: Number.isFinite(positionQuantity) && positionQuantity !== 0
                  ? `${marketVolume(String(Math.abs(positionQuantity)))} ${runtimeBaseAsset}`
                  : "无已归属持仓",
              },
              {
                label: "交易所合并仓位",
                value: Number.isFinite(venueAccountPosition)
                  ? `${marketVolume(String(Math.abs(venueAccountPosition)))} ${runtimeBaseAsset}`
                  : "未知",
              },
              {
                label: "全部计划归因合计",
                value: Number.isFinite(attributedAccountPosition)
                  ? `${marketVolume(String(Math.abs(attributedAccountPosition)))} ${runtimeBaseAsset}`
                  : "未知",
              },
              {
                label: "仓位归因核对",
                value: terminalResultUnknown
                  ? "订单结果待核对"
                  : terminalWithoutVenueAction
                  ? "无需核对 · 未形成场所动作"
                  : zeroExposureReconciliation
                    ? "无持仓 · 无需核对"
                  : positionReconciliationStatus === "MATCH"
                    ? "一致"
                  : positionReconciliationStatus === "MISMATCH"
                    ? "不一致 · 已失败关闭"
                    : positionReconciliationStatus === "STALE"
                      ? "待同点刷新"
                    : "未知",
                tone: positionReconciliationStatus === "MISMATCH"
                  ? "down" as const
                  : positionReconciliationStatus === "MATCH"
                    && !zeroExposureReconciliation
                    ? "up" as const
                    : undefined,
              },
              {
                label: "本计划未实现盈亏",
                value: terminal
                  ? "—"
                  : positionFactsAwaitingSameCutoff
                    ? "待同点刷新"
                    : floatingPnl === null
                      ? "—"
                  : signedUsdt(floatingPnl),
                tone: terminal || positionFactsAwaitingSameCutoff || floatingPnl === null
                  ? undefined
                  : financialToneForSignedValue(floatingPnl),
              },
              {
                label: terminalResultUnknown
                  ? "本次结果"
                  : terminalNoFill
                  ? "本次结果（未成交）"
                  : terminal ? "本次净结果" : "本计划标记净额估算",
                value: terminalResultUnknown
                  ? "待核对"
                  : terminal
                  ? terminalNoFill
                    ? "0.000000 USDT"
                    : closedNetAvailable ? signedUsdt(tradeResult.net_pnl) : "未知"
                  : positionFactsAwaitingSameCutoff
                    ? "待同点刷新"
                  : attributedMarkedNet === null
                    ? "未知"
                    : signedUsdt(attributedMarkedNet),
                tone: terminalResultUnknown
                  ? undefined
                  : terminal
                  ? terminalNoFill
                    ? undefined
                    : closedNetAvailable ? financialToneForSignedValue(tradeResult.net_pnl) : undefined
                  : positionFactsAwaitingSameCutoff || attributedMarkedNet === null
                    ? undefined
                    : financialToneForSignedValue(attributedMarkedNet),
              },
            ]).map((item) => (
              <Box key={item.label} sx={{ p: 1.25, borderRadius: 1, bgcolor: "action.hover", minWidth: 0 }}>
                <Typography variant="caption" color="text.secondary">{item.label}</Typography>
                <Typography sx={{ fontWeight: 780, overflowWrap: "anywhere" }}>
                  {item.tone === "up" || item.tone === "down"
                    ? <MarketToneText tone={item.tone}>{item.value}</MarketToneText>
                    : item.tone
                      ? <FinancialToneText tone={item.tone}>{item.value}</FinancialToneText>
                      : item.value}
                </Typography>
              </Box>
            ))}
          </Box>
          {!positionDisposition && <Box sx={{ mb: 2 }}>
            <Stack direction="row" spacing={0.5} sx={{ alignItems: "center", mb: 1 }}>
                <Typography component="h2" variant="subtitle2">保护与退出</Typography>
              <Tooltip
                arrow
                title="自动止盈和时间退出只有在运行快照中已配置才会执行；“退出订单计划”可从下方稳定控制手动发起，并且只处理本计划可归属持仓与订单。"
              >
                <IconButton size="small" aria-label="保护与退出路径说明">
                  <InfoOutlined fontSize="small" />
                </IconButton>
              </Tooltip>
            </Stack>
            {configuredTakeProfitLevels.length === 0 && configuredTimeExitSeconds === null && !terminal && (
              <Alert severity="warning" variant="outlined" sx={{ mb: 1 }}>
                本计划没有自动止盈或时间退出；盈利不会自动锁定，可由保护止损或手动退出结束。
              </Alert>
            )}
            <Box sx={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 0.75 }}>
              {nextProtectionStep && !terminal && (
                <Box sx={{ gridColumn: "1 / -1", p: 1, border: 1, borderColor: nextProtectionStep.crossed ? "warning.main" : "divider", borderRadius: 1 }}>
                  <Typography variant="caption" color="text.secondary">下一移动保护</Typography>
                  <Typography variant="body2" sx={{ fontWeight: 750 }}>
                    到 {marketPrice(String(nextProtectionStep.triggerPrice))} 时，止损移至 {marketPrice(String(nextProtectionStep.stopPrice))}
                    {Number(nextProtectionStep.stopR) === 0 ? "（成本价）" : `（锁定 ${nextProtectionStep.stopR}R）`}
                  </Typography>
                  <Typography variant="caption" color={nextProtectionStep.crossed ? "warning.main" : "text.secondary"}>
                    {nextProtectionStep.crossed
                      ? "价格已到达触发位；等待交易所确认更紧保护，原保护继续工作到新单被接受"
                      : `第 ${nextProtectionStep.stepIndex + 1}/${nextProtectionStep.stepCount} 档 · 尚未触发`}
                  </Typography>
                </Box>
              )}
              <Box sx={{ p: 1, border: 1, borderColor: "divider", borderRadius: 1 }}>
                <Typography variant="caption" color="text.secondary">保护止损</Typography>
                <Typography variant="body2" sx={{ fontWeight: 750 }}>
                  {Number.isFinite(protectionTriggerPrice)
                    ? `${marketPrice(String(protectionTriggerPrice))} USDT`
                    : "尚无场所动作价"}
                </Typography>
                <Typography variant="caption" color={protectionState === "WORKING" ? "success.main" : "text.secondary"}>
                  {exitHandoff ? "退出交接中" : protectionState === "WORKING" ? "交易所工作中" : protectionDisplay}
                </Typography>
              </Box>
              <Box sx={{ p: 1, border: 1, borderColor: configuredTakeProfitLevels.length === 0 ? "warning.main" : "divider", borderRadius: 1 }}>
                <Typography variant="caption" color="text.secondary">自动止盈</Typography>
                <Typography variant="body2" sx={{ fontWeight: 750 }}>
                  {terminalResultUnknown
                    ? "入场结果待核对"
                    : terminalNoFill
                    ? "未入场，未建立"
                    : takeProfitGroups.length > 0
                    ? takeProfitGroups.map((group, index) => {
                      const formattedPrices = group.prices
                        .map((price) => marketPrice(String(price)));
                      return `TP${index + 1} ${
                        formattedPrices.length > 1
                          ? `${formattedPrices[0]}–${formattedPrices.at(-1)}`
                          : formattedPrices[0] ?? "价格待核对"
                      }`;
                    }).join(" · ")
                    : configuredTakeProfitLevels.length > 0
                      ? "已配置，尚无场所动作"
                      : "未配置"}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  {terminalResultUnknown
                    ? "无法确认是否需要止盈"
                    : terminalNoFill
                    ? "无止盈动作"
                    : terminal
                      ? takeProfitGroups.length > 0
                        ? `${takeProfitGroups.length} 个收益目标 · ${takeProfitOrderCount} 张订单已闭合`
                        : "已闭合"
                    : workingTakeProfitCount > 0
                    ? `${takeProfitGroups.length} 个收益目标 · ${workingTakeProfitCount} 张只减仓订单在交易所工作中`
                    : takeProfitGroups.length > 0
                      ? "等待交易所状态核对"
                    : configuredTakeProfitLevels.length > 0
                      ? `入场成交后建立 ${configuredTakeProfitLevels.length} 个收益目标`
                      : "不会自动锁定盈利"}
                </Typography>
              </Box>
              <Box sx={{ p: 1, border: 1, borderColor: "divider", borderRadius: 1 }}>
                <Typography variant="caption" color="text.secondary">时间退出</Typography>
                <Typography variant="body2" sx={{ fontWeight: 750 }}>
                  {configuredTimeExitSeconds === null ? "未配置" : `${Math.round(configuredTimeExitSeconds)} 秒`}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  {configuredTimeExitSeconds === null ? "无自动结束倒计时" : "从首次成交开始计算"}
                </Typography>
              </Box>
              <Box sx={{ p: 1, border: 1, borderColor: "divider", borderRadius: 1 }}>
                <Typography variant="caption" color="text.secondary">手动退出</Typography>
                <Typography variant="body2" sx={{ fontWeight: 750 }}>
                  {terminal ? "计划已结束" : executorCanExecute ? "可发起" : "执行器不可确认"}
                </Typography>
                <Typography variant="caption" color="text.secondary">仅本计划归属仓位</Typography>
              </Box>
            </Box>
            <RuntimeDeadlineProgress
              activationCreatedAt={valueOf(activation, "created_at", "")}
              entryValidUntil={entryValidUntil}
              entryOrderDeadline={entryOrderDeadline}
              hasOpenEntryResponsibility={hasOpenEntryResponsibility}
              hasPendingEntryResponsibility={hasPendingEntryResponsibility}
              hasEntryFill={hasEntryFill}
              firstFillAt={firstFillAt}
              timeExitSeconds={configuredTimeExitSeconds}
              exitHandoff={exitHandoff}
              terminal={terminal}
            />
          </Box>}
          {!terminal && <>
            <Divider sx={{ my: 1.5 }} />
              <Typography id="stability-controls" component="h2" variant="subtitle2" sx={{ mb: 1 }}>稳定控制</Typography>
            {!executorCanExecute && <Alert severity="error" variant="outlined" sx={{ mb: 1.5 }}>
              Executor 当前不能确认执行；命令回执不代表交易所效果。
            </Alert>}
            <Box sx={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 0.75 }}>
              {visibleControls.map((control) => (
                <Tooltip key={control.intent} arrow enterDelay={350} title={control.description}>
                  <Button
                    size="small"
                    variant="outlined"
                    color={control.color}
                    aria-label={`${control.label}；${control.description}`}
                    disabled={preview.isPending}
                    onClick={() => preview.mutate(control.intent)}
                    sx={{ minWidth: 0 }}
                  >
                    {control.label}
                  </Button>
                </Tooltip>
              ))}
              {entryPhaseClosed && directExecution && !positionDisposition && (
                <Tooltip
                  arrow
                  enterDelay={350}
                  title="沿用原配置创建新的计划草稿；确认新的入场窗口后才能启动。原计划及其持仓、保护和退出保持不变。"
                >
                  <span>
                    <Button
                      size="small"
                      variant="outlined"
                      disabled={liveReadOnly || !planId}
                      onClick={() => navigate(`/plans/new?copyFrom=${encodeURIComponent(planId)}`)}
                      aria-label="重新开放入场；沿用原配置创建新计划，原计划保持不变"
                      sx={{ minWidth: 0, width: "100%" }}
                    >
                      重新开放入场
                    </Button>
                  </span>
                </Tooltip>
              )}
            </Box>
          </>}
        </Box>
      </Box>}
      {intent && preview.data && <Box sx={{ ...surfaceFrameSx, mt: 1.5, p: 2.25, borderColor: intent === "EXIT_STRATEGY" || intent === "USER_TAKEOVER" ? "error.main" : "divider" }}>
        <Typography variant="overline">{selectedControlLabel}</Typography>
        <Typography sx={{ mt: .75, mb: 1.5 }}>{valueOf(preview.data, "consequence")}</Typography>
        {intent === "EXIT_STRATEGY" && (
          <Alert severity="info" variant="outlined" sx={{ mb: 1.5 }}>
            当前范围：撤销本计划
            {workingEntryOrders.complete
              ? ` ${workingEntryOrders.count} 张工作中入场订单`
              : "尚待核对的入场订单"}
            ；退出本计划
            {Number.isFinite(positionQuantity)
              ? ` ${marketVolume(String(Math.abs(positionQuantity)))} ${runtimeBaseAsset}`
              : "尚待核对的持仓"}
            ；不会操作其他计划的订单或仓位。
          </Alert>
        )}
        {intent === "RESUME_ACTIVATION" && !resumeEligible && <Alert severity="warning" sx={{ mb: 1.5 }}>尚未完成最新仓位核对，因此暂时不能恢复新增入场。保护、撤单、减仓和退出不受影响。</Alert>}
        <Stack direction="row" spacing={1}>
          <Button variant="contained" color={intent === "EXIT_STRATEGY" || intent === "USER_TAKEOVER" ? "error" : "primary"} disabled={submit.isPending || !resumeEligible} onClick={() => submit.mutate(intent)}>确认{selectedControlLabel}</Button>
          <Button onClick={() => { setIntent(null); setIdempotencyKey(null); }}>取消</Button>
        </Stack>
      </Box>}
      <Box
        component="details"
        sx={{
          mt: 2,
          ...surfaceFrameSx,
          "& > summary": {
            cursor: "pointer",
            px: 2,
            py: 1.5,
            fontWeight: 750,
          },
        }}
      >
        <Box component="summary">补充交易数据与诊断</Box>
        <Box sx={{ px: { xs: 1.5, sm: 2 }, pb: 2 }}>
      {activation && <FactGrid facts={[
        { label: positionDisposition ? "处置边界" : "计划交易金额", value: `${quoteCurrencyAmount(valueOf(capital, "max_notional"))} USDT` },
        ...(!directExecution ? [{
          label: "策略建仓计算参数：允许损失",
          value: `${quoteCurrencyAmount(valueOf(capital, "max_allowed_loss"))} USDT`,
          note: "仅供策略计算建议仓位；不是止损、最大亏损保证或运行中盈亏熔断",
        }] : []),
        ...(!positionDisposition && fillCount > 0 ? [{
          label: terminal ? "本次净结果" : "立即市价退出估算净结果",
          value: terminal
            ? closedNetAvailable ? signedUsdt(tradeResult.net_pnl) : "未知"
            : immediateExitNetWithFunding === null
              ? "未知"
              : signedUsdt(immediateExitNetWithFunding),
          tone: terminal
            ? closedNetAvailable ? financialToneForSignedValue(tradeResult.net_pnl) : undefined
            : immediateExitNetWithFunding === null
              ? undefined
              : financialToneForSignedValue(immediateExitNetWithFunding),
          note: terminal
            ? tradeResult.funding_included === true
              ? "按本计划成交价差、已归属手续费与资金费计算"
              : "按本计划成交价差与已归属手续费计算；尚无资金费记录"
            : immediateExitEstimate
              ? `按${positionQuantity > 0 ? "买一" : "卖一"} ${marketPrice(String(immediateExitEstimate.exitPrice))} USDT，扣除预计退出手续费 ${quoteCurrencyAmount(immediateExitEstimate.exitCommission)} USDT，${attributedFundingConfirmed ? "并计入已归属资金费" : "尚无可确认资金费记录，暂不计资金费"}；不含滑点`
              : "缺少当前买卖价或入场时冻结的手续费率，不能可靠估算",
        }] : []),
        ...(terminalNoFill ? [{
          label: "本次结果",
          value: "0.000000 USDT",
          note: "未成交；不计入交易次数或收益率",
        }] : []),
        ...(terminalResultUnknown ? [{
          label: "本次结果",
          value: "待核对",
          note: "仍有入场订单结果未决；不能按未成交或 0 盈亏归档",
        }] : []),
        {
          label: "已归属手续费",
          value: `${quoteCurrencyAmount(valueOf(tradeResult, "commission", "0"))} USDT`,
          note: terminalResultUnknown
            ? "尚无已确认成交；订单结果仍待核对"
            : tradeResult.commission_complete === true
              ? "当前成交手续费已齐全"
              : fillCount > 0 ? "仍有手续费待核对" : "尚无成交",
        },
        {
          label: "已归属资金费",
          value: positionDisposition
            ? "外部历史不归属"
            : terminalNoFill
            ? "0 USDT"
            : attributedFundingConfirmed
            ? signedUsdt(valueOf(tradeResult, "funding", "0"))
            : "尚无可确认记录",
          note: positionDisposition
            ? "本计划不认领外部持仓建立以来的资金费"
            : terminalNoFill
            ? "本计划从未持仓，不产生资金费"
            : attributedFundingConfirmed
            ? "按事件时点的本计划虚拟持仓归属"
            : "未知不按 0 处理",
        },
        { label: "保证金模式 / 杠杆", value: `${valueOf(positionAttribution, "margin_mode", "未知")} / ${valueOf(positionAttribution, "leverage", "—")}x`, note: "全仓与逐仓均按本计划虚拟持仓归属" },
      ]} />}
      {activation && <Box
        component="details"
        sx={{
          mt: 2,
          "& > summary": {
            cursor: "pointer",
            color: "text.secondary",
            fontSize: 12,
            fontWeight: 700,
          },
        }}
      >
        <Box component="summary">诊断标识</Box>
        <Box sx={{ mt: 1.25 }}>
          <FactGrid facts={[
            { label: "激活 ID", value: valueOf(activation, "activation_id") },
            { label: "计划版本", value: valueOf(activation, "plan_version_ref") },
            { label: "状态版本", value: valueOf(activation, "state_version") },
            { label: "决策依据", value: decisionBasisRef || valueOf(decisionBasis, "kind", "未知") },
            { label: "编译器版本", value: valueOf(orderSchedule, "compiler_version", "未知") },
            { label: "事实截止", value: venueFactCutoff ? formatUserVisibleTime(venueFactCutoff) : "尚无交易所事实" },
            ...(latestNoActionCode ? [{
              label: "最近未下单原因码",
              value: latestNoActionCode,
              note: latestNoActionRuleId
                ? `规则 ${latestNoActionRuleId}；用于技术排障`
                : "用于技术排障",
            }] : []),
          ]} />
        </Box>
      </Box>}

      {directExecution && activation && lifecycle === "RUNNING" && !hasEntryFill && !hasOpenEntryResponsibility && !newRiskStopped && <Box component="section" sx={{ mt: 4 }}>
        <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} sx={{ justifyContent: "space-between", alignItems: { xs: "stretch", sm: "center" }, mb: 2 }}>
          <Box>
            <Typography variant="h2">等待直接执行条件</Typography>
            <Typography color="text.secondary" variant="body2" sx={{ mt: .75 }}>本计划不等待策略信号；Executor 按运行快照条件、订单档位和当前事实形成动作，并继续经过 CAP 与 EXE。</Typography>
          </Box>
          <Button variant="outlined" onClick={() => market.refetch()} disabled={market.isFetching}>{market.isFetching ? "正在刷新…" : "刷新行情"}</Button>
        </Stack>
        {market.isPending && <LinearProgress aria-label="正在读取激活行情" />}
        {market.isError && currentMarket && <Alert severity="warning" variant="outlined">行情刷新失败；以下保留上次成功行情（截止 {formatUserVisibleTime(currentMarket.source_cutoff)}），可能已经过期，仅用于定位。Executor 不会用页面缓存代替当前事实。</Alert>}
        {market.isError && !currentMarket && <Alert severity="warning" variant="outlined">当前行情不可用，页面无法定位相对于运行快照档位的市场位置；Executor 不会因此放宽事实和风险检查。</Alert>}
        {latestNoActionText && <Alert severity={directPreSubmitBlocked ? "warning" : "info"} variant="outlined" sx={{ mb: 2 }}>
          最近一次入场判断没有形成下单动作：{latestNoActionText}（{formatUserVisibleTime(valueOf(latestNoActionEvent, "at"))}）。
          {directPreSubmitBlocked
            ? " Executor 保持失败关闭并等待前置条件恢复，不会绕过校验。"
            : " 计划仍在有效期内等待运行快照条件。"}
        </Alert>}
        {visibleReferencePrice && <FactGrid facts={[
          { label: "订单计划", value: orderScheduleSummary(orderSchedule) },
          { label: "盘口中间价", value: `${marketPrice(visibleReferencePrice)} USDT`, note: "页面只读定位；执行前重新读取服务端事实" },
          { label: "买一 / 卖一", value: `${marketPrice(visibleBidPrice)} / ${marketPrice(visibleAskPrice)} USDT` },
          { label: "买卖价差", value: `${marketPrice(currentSpread)} USDT`, note: Number.isFinite(currentSpreadBps) ? `${currentSpreadBps.toFixed(2)} bps` : undefined },
          { label: "行情截止", value: formatUserVisibleTime(visibleMarketCutoff) },
        ]} />}
      </Box>}

      {!directExecution && activation && lifecycle === "RUNNING" && !hasEntryFill && !hasOpenEntryResponsibility && !newRiskStopped && <Box component="section" sx={{ mt: 4 }}>
        <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} sx={{ justifyContent: "space-between", alignItems: { xs: "stretch", sm: "center" }, mb: 2 }}>
          <Box>
            <Typography variant="h2">{demoImmediateEntry ? "等待验证入场" : "等待入场"}</Typography>
            <Typography color="text.secondary" variant="body2" sx={{ mt: .75 }}>公开行情每 15 秒更新；策略仍只按闭合 K 线和运行快照参数判断。</Typography>
          </Box>
          <Button variant="outlined" onClick={() => market.refetch()} disabled={market.isFetching}>{market.isFetching ? "正在刷新…" : "刷新行情"}</Button>
        </Stack>
        {market.isPending && <LinearProgress aria-label="正在读取激活行情" />}
        {market.isError && currentMarket && <Alert severity="warning" variant="outlined">行情刷新失败；以下保留上次成功行情（截止 {formatUserVisibleTime(currentMarket.source_cutoff)}），可能已经过期，仅用于定位。Executor 继续按框架收到的当前市场事件和运行快照规则运行。</Alert>}
        {market.isError && !currentMarket && <Alert severity="warning" variant="outlined">当前行情不可用，页面不能判断距离入场条件还有多远；Executor 不会因此放宽运行快照规则。</Alert>}
        {latestNoActionText && <Alert severity="info" variant="outlined" sx={{ mb: 2 }}>
          最近一次入场意图没有下单：{latestNoActionText}（{formatUserVisibleTime(valueOf(latestNoActionEvent, "at"))}）。策略仍在有效期内等待下一次满足条件的闭合 K 线。
        </Alert>}
        {Number.isFinite(currentSpreadBps) && currentSpreadBps > 10 && <Alert severity="warning" sx={{ mb: 2 }}>
          当前买卖价差约 {currentSpreadBps.toFixed(1)} bps，超过 10 bps 入场上限。系统正在等待价差收窄，不会在当前盘口创建入场动作。
        </Alert>}
        {latestClosedBarBeyondBoundary && latestClosedBarBeyondExtension && entryExtensionLimit !== null && <Alert severity="info" variant="outlined" sx={{ mb: 2 }}>
          最近闭合 1m 虽已突破通道，但超过最大追价边界 {marketPrice(String(entryExtensionLimit))} USDT。策略不会追单；等待价格回到允许范围或入场窗口结束。
        </Alert>}
        {currentMarket && <FactGrid facts={[
          { label: "盘口中间价", value: `${marketPrice(currentMarket.reference_price)} USDT`, note: "仅用于页面定位；执行前会同时读取标记价格，并按方向采用更保守的价格" },
          { label: "最新闭合 1m", value: `${marketPrice(currentMarket.latest_close_1m)} USDT`, note: demoImmediateEntry ? "下一根有效闭合 1m 将用于验证入场" : latestClosedBarBeyondExtension ? "已突破，但超过最大追价边界；策略不会追单" : latestClosedBarBeyondBoundary ? confirmationBars > 1 ? "已突破；连续确认和执行前检查仍按闭合 K 线判断" : "已突破；仍需通过执行前价格、价差与账户检查" : pendingBreakoutNote(direction === "SHORT" ? "SHORT" : "LONG") },
          { label: "最近闭合 1m 成交量 / 笔数", value: `${marketVolume(currentMarket.latest_volume_1m)} ${runtimeBaseAsset} / ${currentMarket.latest_trade_count_1m} 笔`, note: "用于判断当前行情活跃度，不参与策略触发" },
          { label: "买一 / 卖一", value: `${marketPrice(currentMarket.bid_price)} / ${marketPrice(currentMarket.ask_price)} USDT` },
          { label: "买卖价差", value: `${marketPrice(currentSpread)} USDT`, note: Number.isFinite(currentSpreadBps) ? `${currentSpreadBps.toFixed(2)} bps` : undefined },
          { label: "通道上沿 / 下沿", value: `${marketPrice(currentMarket.channel_upper)} / ${marketPrice(currentMarket.channel_lower)} USDT`, note: `${direction === "LONG" ? "做多观察上沿" : "做空观察下沿"}；另一侧用于判断机会是否已经迁移` },
          ...(entryExtensionLimit !== null ? [{ label: "最大追价边界", value: `${marketPrice(String(entryExtensionLimit))} USDT`, note: `通道边界 ± ${maxEntryExtensionAtr} ATR（ATR ${marketPrice(currentMarket.atr_14)} USDT）` }] : []),
          { label: "1m 收盘距上沿 / 下沿", value: `${gapPercent(longClosedBarBreakoutGap)} / ${gapPercent(shortClosedBarBreakoutGap)}`, note: "策略触发口径；正值表示尚未突破，负值表示已越过" },
          { label: "盘口中间价距上沿 / 下沿", value: `${gapPercent(currentMarket.long_breakout_gap_pct)} / ${gapPercent(currentMarket.short_breakout_gap_pct)}`, note: "仅反映当前市场位置，不用于触发或替代执行前价格检查" },
          { label: "确认条件", value: demoImmediateEntry ? "1 × 1m 收盘（流程验证）" : `${valueOf(parameters, "confirmation_bars_1m")} × 1m 收盘` },
          { label: "1m 收盘时间", value: formatUserVisibleTime(currentMarket.latest_closed_1m_at) },
          { label: "行情截止", value: formatUserVisibleTime(currentMarket.source_cutoff) },
        ]} />}
      </Box>}

        </Box>
      </Box>

      {activeAccountSystemStop && <Box sx={{ ...surfaceFrameSx, mt: 2, p: 2, borderColor: "warning.main" }}>
          <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} sx={{ justifyContent: "space-between", alignItems: { sm: "center" } }}>
            <Box>
              <Stack direction="row" spacing={0.5} sx={{ alignItems: "center" }}>
                <Typography sx={{ fontWeight: 750 }}>本账户已停止新增风险</Typography>
                <Tooltip
                  arrow
                  title="当账户持仓或订单无法归属到 Halpha 计划时，系统暂停本账户继续开仓，避免计划基于错误敞口扩大风险；保护、撤单、减仓和退出仍继续处理。"
                >
                  <IconButton size="small" aria-label="账户停止机制说明">
                    <InfoOutlined fontSize="small" />
                  </IconButton>
                </Tooltip>
              </Stack>
              <Typography variant="body2" color="text.secondary">
                {translatedLabel(systemStopSourceLabels, valueOf(activeAccountSystemStop, "source"))}
                {" · "}开始于 {formatUserVisibleTime(valueOf(activeAccountSystemStop, "started_at"))}
                {"（"}{relativeAgeLabel(valueOf(activeAccountSystemStop, "started_at"), Date.now())}{"）"}
              </Typography>
            </Box>
            <Tooltip
              arrow
              title="重新读取账户持仓、普通/条件委托和未闭合计划责任，判断停止是否可以解除；不会恢复计划、启动策略或产生任何交易所订单。"
            >
              <span>
                <Button
                  variant="outlined"
                  color="warning"
                  disabled={liveReadOnly || systemStopReleasePreview.isPending || systemStopRelease.isPending}
                  onClick={() => {
                    systemStopRelease.reset();
                    systemStopReleasePreview.mutate();
                  }}
                  aria-label="重新核对账户停止；不会恢复计划、启动策略或下单"
                >
                  {systemStopReleasePreview.isPending ? "正在核对账户…" : "重新核对"}
                </Button>
              </span>
            </Tooltip>
          </Stack>
          {liveReadOnly && <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
            Live Read Only 仅显示停止事实；释放必须切换到经部署核验的 Live Write App。
          </Typography>}
        </Box>}
      {submit.isSuccess && submittedReceiptState === "PROCESSING" && <Alert severity="info" sx={{ mt: 2 }}>命令已接受，正在核对执行与闭合责任；请勿重复提交。</Alert>}
      {submit.isSuccess && submittedReceiptState === "EFFECTIVE" && <Alert severity="success" sx={{ mt: 2 }}>命令已生效，当前执行责任已经核对。</Alert>}
      {submit.isSuccess && !["PROCESSING", "EFFECTIVE", "REJECTED"].includes(submittedReceiptState) && <Alert severity="success" sx={{ mt: 2 }}>命令已持久化并返回“{submittedReceiptState || "UNKNOWN"}”回执；请按状态继续核对。</Alert>}
      {submit.isSuccess && submittedReceiptState === "REJECTED" && <Alert severity="error" sx={{ mt: 2 }}>命令被拒绝：{valueOf(currentSubmittedReceipt, "reason_code")}。页面未把已持久化的拒绝回执显示为成功效果。</Alert>}
      {submit.isError && <Alert severity="error" sx={{ mt: 2 }}>命令未确认：{submit.error instanceof ApiFailure ? submit.error.code : "结果未知"}</Alert>}

      {systemStopReleasePreview.isError && <Alert severity="error" sx={{ mt: 2 }}>
        系统停止核对失败：{systemStopReleasePreview.error instanceof ApiFailure ? systemStopReleasePreview.error.code : "结果未知"}。停止仍保持有效。
      </Alert>}
      {systemStopReleasePreview.data && <Box sx={{ ...surfaceFrameSx, mt: 2, p: 2.5, borderColor: systemStopReleasePreview.data.eligible === true ? "warning.main" : "divider" }}>
        <Typography variant="overline">解除账户停止核对</Typography>
        <Typography sx={{ mt: .75 }}>{valueOf(systemStopReleasePreview.data, "consequence")}</Typography>
        {valueOf(systemStopReleasePreview.data, "evidence_cutoff", "") && <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
          服务端账户事实截止 {formatUserVisibleTime(valueOf(systemStopReleasePreview.data, "evidence_cutoff"))}
        </Typography>}
        {systemStopReleaseDenials.length > 0 && <Alert severity="warning" variant="outlined" sx={{ mt: 2 }}>
          {systemStopReleaseDenials.map((reason) => (
            <Typography key={reason} variant="body2">
              {systemStopReleaseDenialLabels[reason] ?? reason}
            </Typography>
          ))}
        </Alert>}
        {systemStopReleasePreview.data.eligible === true && <Stack direction={{ xs: "column", sm: "row" }} spacing={1} sx={{ mt: 2 }}>
          <Button
            variant="contained"
            color="warning"
            disabled={systemStopRelease.isPending}
            onClick={() => systemStopRelease.mutate()}
          >
            {systemStopRelease.isPending
              ? "正在解除…"
              : systemStopRelease.isError && isUnknownMutationResult(systemStopRelease.error)
                ? "沿用原请求身份重试释放"
                : "释放账户级系统停止"}
          </Button>
          <Button
            disabled={systemStopRelease.isPending}
            onClick={() => {
              setSystemStopReleaseKey(null);
              systemStopReleasePreview.reset();
            }}
          >
            取消
          </Button>
        </Stack>}
      </Box>}
      {systemStopRelease.isSuccess && <Alert severity="success" sx={{ mt: 2 }}>
        账户新增风险停止已解除。不会自动启动计划或产生交易所订单；后续新增风险仍需重新通过当前检查。
      </Alert>}
      {systemStopRelease.isError && <Alert severity="error" sx={{ mt: 2 }}>
        释放未确认：{systemStopRelease.error instanceof ApiFailure ? systemStopRelease.error.code : "结果未知"}。系统停止不会因页面错误而自动解除。
      </Alert>}

      <Box
        component="section"
        aria-labelledby="runtime-events-title"
        sx={{
          mt: 2,
          ...surfaceFrameSx,
          p: { xs: 1.5, sm: 2 },
        }}
      >
        <Stack
          direction={{ xs: "column", md: "row" }}
          spacing={1.25}
          sx={{ justifyContent: "space-between", alignItems: { xs: "stretch", md: "center" }, mb: 1.5 }}
        >
          <Box>
            <Typography id="runtime-events-title" variant="h2">事件记录</Typography>
            <Typography variant="caption" color="text.secondary">
              {runtimeEventFilter === "ALL"
                ? `${compactedTimeline.length} 条`
                : `${filteredTimeline.length} / ${compactedTimeline.length} 条`}
              {(latestFilteredEventAt || (runtimeEventFilter === "ALL" ? latestRecordedFactAt : ""))
                ? ` · 最近事件 ${formatUserVisibleTime(latestFilteredEventAt || latestRecordedFactAt)}（${relativeAgeLabel(latestFilteredEventAt || latestRecordedFactAt, Date.now())}）`
                : " · 尚无事件"}
            </Typography>
          </Box>
          <ToggleButtonGroup
            exclusive
            size="small"
            value={runtimeEventFilter}
            aria-label="筛选事件记录"
            onChange={(_event, next: "ALL" | RuntimeEventCategory | null) => {
              if (next) setRuntimeEventFilter(next);
            }}
            sx={{
              alignSelf: { xs: "flex-start", md: "center" },
              "& .MuiToggleButton-root": {
                minHeight: 30,
                px: 1.25,
                py: 0.25,
                fontWeight: 700,
              },
            }}
          >
            {([
              ["ALL", "全部"],
              ["PLAN", "计划"],
              ["TRADING", "交易"],
              ["PROTECTION", "保护"],
              ["RECONCILIATION", "核对"],
            ] as Array<["ALL" | RuntimeEventCategory, string]>).map(([value, label]) => (
              <ToggleButton key={value} value={value}>{label}</ToggleButton>
            ))}
          </ToggleButtonGroup>
        </Stack>
        <ExpandableList
          items={[...filteredTimeline].reverse()}
          initialCount={10}
          step={10}
          spacing={0}
          renderItem={(entry) => {
            const item = entry.item;
            const source = valueOf(item, "source");
            const presentation = runtimeTimelinePresentation(
              entry,
              actionsByRef,
              facts,
              orderSchedule,
              priorBlockingReasonByTimelineEntry.get(entry) ?? "",
            );
            return (
              <Box key={`${source}:${valueOf(item, "source_ref")}`} sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", sm: "150px 110px minmax(0,1fr)" }, gap: 1, py: 1.25, borderTop: 1, borderColor: "divider" }}>
                <Typography variant="caption" color="text.secondary">{formatUserVisibleTime(valueOf(item, "at"))}</Typography>
                <Typography variant="caption" sx={{ fontWeight: 750 }}>{translatedLabel(timelineSourceLabels, source)}</Typography>
                <Box sx={{ minWidth: 0 }}>
                  <Typography variant="body2">{presentation.headline}</Typography>
                  {presentation.detail && (
                    <Typography variant="caption" color="text.secondary" sx={{ display: "block", overflowWrap: "anywhere" }}>
                      {presentation.detail}
                    </Typography>
                  )}
                </Box>
              </Box>
            );
          }}
        />
        {filteredTimeline.length === 0 && (
          <Typography variant="body2" color="text.secondary" sx={{ py: 1 }}>
            当前筛选下没有事件。
          </Typography>
        )}
      </Box>
      {lifecycle === "COMPLETED" && resultRef && (
        <ReviewDetails
          reviewId={resultRef}
          embedded
          pricePathInterval={runtimeChartInterval}
        />
      )}
    </Box>
  );
}

function ReviewsPage() {
  const navigate = useNavigate();
  const { marketColorScheme, status: settingsStatus } = useOutletContext<FrameContext>();
  const [filter, setFilter] = useState<"TRADED" | "DRAFT" | "ALL" | "STAGE">("TRADED");
  const [listFilters, setListFilters] = useState<ReviewListFilters>(emptyReviewListFilters);
  const [visibleCount, setVisibleCount] = useState(20);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const query = useQuery({ queryKey: ["reviews"], queryFn: getReviews, refetchInterval: 30_000 });
  const plansQuery = useQuery({ queryKey: ["plans"], queryFn: getPlans });
  const strategiesQuery = useQuery({ queryKey: ["strategies"], queryFn: getStrategies });
  const accountSequenceQuery = useQuery({
    queryKey: [
      "review-sequence-evidence",
      settingsStatus.environment_id,
      "ACCOUNT_RESULTS",
      "ALL",
      "ALL",
    ],
    queryFn: () => getReviewSequenceEvidence({ scope: "ACCOUNT_RESULTS" }),
    staleTime: 60_000,
  });
  const profitSeekingSequenceQuery = useQuery({
    queryKey: [
      "review-sequence-evidence",
      settingsStatus.environment_id,
      "PROFIT_SEEKING",
      "ALL",
      "ALL",
    ],
    queryFn: () => getReviewSequenceEvidence({ scope: "PROFIT_SEEKING" }),
    staleTime: 60_000,
  });
  const reviewPlansByVersion = useMemo(
    () => new Map(
      (plansQuery.data ?? []).flatMap((plan) => (
        plan.plan_version_id ? [[plan.plan_version_id, plan] as const] : []
      )),
    ),
    [plansQuery.data],
  );
  const reviews = [...(query.data ?? [])].sort((left, right) => {
    const rightClosedAt = Date.parse(reviewClosedAt(right));
    const leftClosedAt = Date.parse(reviewClosedAt(left));
    return (Number.isFinite(rightClosedAt) ? rightClosedAt : 0) - (Number.isFinite(leftClosedAt) ? leftClosedAt : 0);
  });
  const tradedReviews = reviews.filter((review) => ["COMPLETED", "PARTIAL"].includes(valueOf(review, "primary_result")));
  const scopeReviews = filter === "DRAFT"
    ? reviews.filter((review) => valueOf(review, "status") === "DRAFT")
    : filter === "TRADED"
      ? tradedReviews
      : reviews;
  const visibleReviews = scopeReviews.filter((review) => reviewMatchesFilters(review, listFilters));
  const strategyOptions = [...new Set(reviews.map((review) => reviewDecisionBasisKind(recordOf(review.trade_context))).filter(Boolean))].sort();
  const instrumentOptions = [...new Set(reviews.map((review) => valueOf(recordOf(review.trade_context), "instrument_ref", "")).filter(Boolean))].sort();
  const activeFilterCount = Object.values(listFilters).filter((value) => value !== "ALL").length;
  const profitSeekingRefs = new Set(
    (profitSeekingSequenceQuery.data?.trades ?? []).map(
      (trade) => `${trade.review_id}:${trade.review_version}`,
    ),
  );
  const performanceTrades = (accountSequenceQuery.data?.trades ?? []).map((trade) => {
    const entryNotional = finiteNumber(trade.entry_notional);
    return {
      netPnl: finiteNumber(trade.net_pnl) ?? Number.NaN,
      commission: finiteNumber(trade.commission) ?? Number.NaN,
      entryNotional,
      classification: trade.classification ?? undefined,
      profitSeekingEligible: profitSeekingRefs.has(
        `${trade.review_id}:${trade.review_version}`,
      ),
      closedAt: trade.closed_at,
    };
  });
  const latestReliableClosedAt = accountSequenceQuery.data?.trades.at(-1)?.closed_at ?? null;
  useEffect(() => {
    const intervalId = window.setInterval(() => setNowMs(Date.now()), 30_000);
    return () => window.clearInterval(intervalId);
  }, []);
  useEffect(() => { setVisibleCount(20); }, [filter, listFilters]);
  const updateListFilter = <Key extends keyof ReviewListFilters>(key: Key, value: ReviewListFilters[Key]) => {
    setListFilters((current) => ({ ...current, [key]: value }));
  };
  const shownReviews = visibleReviews.slice(0, visibleCount);
  const remaining = Math.max(0, visibleReviews.length - visibleCount);
  if (query.isPending || plansQuery.isPending || strategiesQuery.isPending) {
    return (
      <Box sx={{ width: "min(1320px, calc(100% - clamp(24px, 4vw, 48px)))", mx: "auto", py: { xs: 2, sm: 2.5 } }}>
        <LinearProgress aria-label="正在读取复盘事实" />
      </Box>
    );
  }
  if (query.isError || plansQuery.isError || strategiesQuery.isError) {
    return (
      <Box sx={{ width: "min(1320px, calc(100% - clamp(24px, 4vw, 48px)))", mx: "auto", py: { xs: 2, sm: 2.5 } }}>
        <Alert severity="error">复盘事实暂不可用；页面不会用零值或占位内容冒充真实结果。</Alert>
      </Box>
    );
  }
  return (
    <Box sx={{ width: "min(1320px, calc(100% - clamp(24px, 4vw, 48px)))", mx: "auto", py: { xs: 2, sm: 2.5 } }}>
      <Typography component="h1" sx={visuallyHiddenSx}>激活复盘</Typography>
      {(accountSequenceQuery.isPending || profitSeekingSequenceQuery.isPending) && (
        <LinearProgress aria-label="正在加载交易表现" sx={{ mb: 2 }} />
      )}
      {(accountSequenceQuery.isError || profitSeekingSequenceQuery.isError) && (
        <Alert severity="error" variant="outlined" sx={{ mb: 2 }}>
          账户表现当前不可读；不会退回到近似筛选或把缺失结果显示为零。
        </Alert>
      )}
      {accountSequenceQuery.data && profitSeekingSequenceQuery.data && (
        <Suspense fallback={<LinearProgress aria-label="正在加载交易表现" sx={{ mb: 2 }} />}>
          <ReviewPerformanceOverview
            tradesInClosingOrder={performanceTrades}
            marketColorScheme={marketColorScheme}
            chartAttribution={<TradingViewAttribution />}
          />
        </Suspense>
      )}

      <Suspense fallback={<LinearProgress aria-label="正在加载连续交易证据" sx={{ mb: 2 }} />}>
        <ReviewSequenceEvidencePanel
          environmentId={settingsStatus.environment_id}
          latestClosedAt={latestReliableClosedAt}
          onOpenReview={(reviewId) => navigate(`/reviews/${reviewId}`)}
        />
      </Suspense>

      <Tabs value={filter} onChange={(_event, value: typeof filter) => setFilter(value)} aria-label="复盘筛选" variant="scrollable" scrollButtons="auto" allowScrollButtonsMobile sx={{ mb: 1.5 }}>
        <Tab value="TRADED" label={`交易记录（${tradedReviews.length}）`} />
        <Tab value="DRAFT" label={`待评价（${reviews.filter((review) => valueOf(review, "status") === "DRAFT").length}）`} />
        <Tab value="ALL" label={`全部记录（${reviews.length}）`} />
        <Tab value="STAGE" label="阶段性复盘" />
      </Tabs>

      {filter === "STAGE" && (
        <Suspense fallback={<LinearProgress aria-label="正在加载阶段性复盘" />}>
          <StageReviewPanel
            environmentId={settingsStatus.environment_id}
            liveReadOnly={settingsStatus.profile === "BINANCE_LIVE_READ_ONLY"}
            reviewFactCutoffs={reviews.map((review) => valueOf(review, "fact_cutoff", ""))}
          />
        </Suspense>
      )}

      <Box component="section" aria-label="交易记录联合筛选" sx={{ ...surfaceFrameSx, p: 1.25, mb: 1.5, display: filter === "STAGE" ? "none" : "block" }}>
        <Box sx={{ display: "grid", gridTemplateColumns: { xs: "repeat(2, minmax(0, 1fr))", md: "repeat(3, minmax(150px, 1fr))", lg: "repeat(6, minmax(130px, 1fr))" }, gap: 1 }}>
          <TextField select size="small" label="决策依据 / 策略" value={listFilters.strategyId} onChange={(event) => updateListFilter("strategyId", event.target.value)}>
            <MenuItem value="ALL">全部决策依据</MenuItem>
            {strategyOptions.map((strategyId) => {
              const strategy = strategiesQuery.data?.find((item) => item.strategy_id === strategyId);
              return <MenuItem key={strategyId} value={strategyId}>{isDirectExecution(strategyId) ? DIRECT_EXECUTION_LABEL : strategy?.display_name ?? strategyId}</MenuItem>;
            })}
          </TextField>
          <TextField select size="small" label="交易对象" value={listFilters.instrumentRef} onChange={(event) => updateListFilter("instrumentRef", event.target.value)}>
            <MenuItem value="ALL">全部交易对象</MenuItem>
            {instrumentOptions.map((instrumentRef) => <MenuItem key={instrumentRef} value={instrumentRef}>{instrumentRef}</MenuItem>)}
          </TextField>
          <TextField select size="small" label="方向" value={listFilters.direction} onChange={(event) => updateListFilter("direction", event.target.value)}>
            <MenuItem value="ALL">全部方向</MenuItem>
            {Object.entries(directionLabels).map(([value, label]) => <MenuItem key={value} value={value}>{label}</MenuItem>)}
          </TextField>
          <TextField select size="small" label="盈亏" value={listFilters.pnl} onChange={(event) => updateListFilter("pnl", event.target.value as ReviewPnlFilter)}>
            {(Object.entries(pnlFilterLabels) as Array<[ReviewPnlFilter, string]>).map(([value, label]) => <MenuItem key={value} value={value}>{label}</MenuItem>)}
          </TextField>
          <TextField select size="small" label="交易结果" value={listFilters.primaryResult} onChange={(event) => updateListFilter("primaryResult", event.target.value)}>
            <MenuItem value="ALL">全部结果</MenuItem>
            {Object.entries(reviewResultLabels).map(([value, label]) => <MenuItem key={value} value={value}>{label}</MenuItem>)}
          </TextField>
          <TextField select size="small" label="复盘分类" value={listFilters.ownerConclusion} onChange={(event) => updateListFilter("ownerConclusion", event.target.value)}>
            <MenuItem value="ALL">全部分类</MenuItem>
            {reviewClassificationFilterValues.map((value) => <MenuItem key={value} value={value}>{evaluationResultLabels[value]}</MenuItem>)}
          </TextField>
        </Box>
        <Stack direction="row" spacing={1.5} sx={{ mt: 1, alignItems: "center", justifyContent: "space-between" }}>
          <Typography variant="caption" color="text.secondary">
            条件同时满足 · 匹配 {visibleReviews.length} / {scopeReviews.length} 条{activeFilterCount > 0 ? ` · 已选 ${activeFilterCount} 项` : ""}
          </Typography>
          <Button size="small" variant="text" disabled={activeFilterCount === 0} onClick={() => setListFilters(emptyReviewListFilters)}>重置筛选</Button>
        </Stack>
      </Box>

      <TableContainer className="table-scroll" role="region" aria-label="交易与复盘记录横向滚动区域" tabIndex={0} sx={{ ...surfaceFrameSx, overflowX: "auto", display: filter === "STAGE" ? "none" : "block" }}>
        <Table size="small" aria-label="交易与复盘记录" sx={{ minWidth: 1120, "& th": { whiteSpace: "nowrap" }, "& td": { verticalAlign: "top" } }}>
          <TableHead>
            <TableRow>
              <TableCell>闭合时间</TableCell>
              <TableCell>交易</TableCell>
              <TableCell>计划 / 策略</TableCell>
              <TableCell align="right">入场 / 出场</TableCell>
              <TableCell align="right">成交额</TableCell>
              <TableCell align="right">净盈亏</TableCell>
              <TableCell align="right">手续费</TableCell>
              <TableCell>持仓 / 退出</TableCell>
              <TableCell>结果</TableCell>
              <TableCell>复盘分类</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {shownReviews.map((review) => {
              const result = tradeResultForReview(review);
              const context = recordOf(review.trade_context);
              const ownerConclusion = recordOf(recordOf(review.evaluations).owner_conclusion);
              const decisionBasisKind = reviewDecisionBasisKind(context);
              const directExecution = isDirectExecution(decisionBasisKind);
              const strategy = directExecution
                ? undefined
                : strategiesQuery.data?.find((item) => item.strategy_id === decisionBasisKind);
              const decisionBasisLabel = directExecution
                ? DIRECT_EXECUTION_LABEL
                : strategy?.display_name ?? decisionBasisKind;
              const planName = valueOf(context, "plan_name", "");
              const primaryResult = valueOf(review, "primary_result");
              const noAction = primaryResult === "NO_ACTION";
              const resultAvailable = result.calculation_complete === true && result.closed === true && finiteNumber(result.net_pnl) !== null;
              const externalAccountResult = result.result_scope === "ACCOUNT_FACTS_WITH_EXTERNAL_CLOSURE";
              const alignment = recordOf(context.position_alignment);
              const positionDisposition = result.result_scope === "EXTERNAL_POSITION_DISPOSITION"
                || Object.keys(alignment).length > 0;
              const dispositionCostComplete = positionDisposition
                && result.execution_cost_complete === true;
              const averageEntry = finiteNumber(result.average_entry_price);
              const averageExit = finiteNumber(result.average_exit_price);
              const openResponsibilities = recordOf(review.open_responsibilities);
              const openActionCount = Array.isArray(openResponsibilities.execution_action_refs)
                ? openResponsibilities.execution_action_refs.length
                : 0;
              const unknownActionCount = Array.isArray(openResponsibilities.unknown_action_refs)
                ? openResponsibilities.unknown_action_refs.length
                : 0;
              const reviewId = valueOf(review, "review_id");
              const activationId = valueOf(review, "activation_id");
              const closedAtValue = reviewClosedAt(review);
              const closedAt = formatUserVisibleTime(closedAtValue);
              const pendingEvaluation = valueOf(review, "status") === "DRAFT";
              const ownerConclusionReason = valueOf(ownerConclusion, "reason", "");
              const planVersionId = valueOf(
                recordOf(recordOf(review.input_refs).plan_version),
                "plan_version_id",
              );
              const sourcePlan = reviewPlansByVersion.get(planVersionId);
              const frozenSnapshot = orderSchedulePreviewOf(
                context.order_schedule_snapshot,
              );
              const orderIntent = positionAlignmentIntent(
                sourcePlan?.position_alignment ?? alignment,
              ) ?? orderScheduleIntent(
                sourcePlan?.order_schedule_spec ?? null,
                frozenSnapshot,
              );
              const openReview = () => navigate(`/activations/${activationId}`);
              return (
                <TableRow
                  key={reviewId}
                  hover
                  tabIndex={0}
                  aria-label={`查看 ${valueOf(context, "instrument_ref")} ${translatedLabel(directionLabels, valueOf(context, "direction"))} ${closedAt} 复盘`}
                  onClick={openReview}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      openReview();
                    }
                  }}
                  sx={{ cursor: "pointer" }}
                >
                  <TableCell className="mono">
                    {closedAt}
                    {pendingEvaluation && (
                      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.25, whiteSpace: "nowrap" }}>
                        待评价 · 已过 {elapsedDurationLabel(closedAtValue, nowMs)}
                      </Typography>
                    )}
                  </TableCell>
                  <TableCell>
                    <Typography variant="body2" sx={{ fontWeight: 700 }}>{valueOf(context, "instrument_ref")}</Typography>
                    <MarketToneText tone={marketToneForDirection(valueOf(context, "direction"))}>{translatedLabel(directionLabels, valueOf(context, "direction"))}</MarketToneText>
                  </TableCell>
                  <TableCell>
                    <Typography variant="body2" sx={{ fontWeight: planName ? 700 : 400 }}>{planName || (positionDisposition ? positionAlignmentOperationLabel(alignment) : decisionBasisLabel)}</Typography>
                    {planName && <Typography variant="caption" color="text.secondary">{positionDisposition ? positionAlignmentOperationLabel(alignment) : decisionBasisLabel}</Typography>}
                    {sourcePlan && (
                      <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                        {orderIntent ?? "订单意图不可读"} · {positionDisposition ? "处置边界" : "计划金额"} {quoteCurrencyAmount(sourcePlan.max_notional)} USDT
                      </Typography>
                    )}
                  </TableCell>
                  <TableCell className="mono" align="right">
                    {noAction ? "不适用" : positionDisposition ? `外部 / ${averageExit === null ? "未知" : marketPrice(String(averageExit))}` : `${averageEntry === null ? "未知" : marketPrice(String(averageEntry))} / ${averageExit === null ? "未知" : marketPrice(String(averageExit))}`}
                  </TableCell>
                  <TableCell className="mono" align="right">{noAction ? "不适用" : positionDisposition ? "外部基线" : finiteNumber(result.entry_notional) === null ? "未知" : usdt(result.entry_notional)}</TableCell>
                  <TableCell className="mono" align="right">
                    {noAction ? "不适用" : positionDisposition ? "不归属" : resultAvailable ? <FinancialToneText tone={financialToneForSignedValue(result.net_pnl)}>{signedUsdt(result.net_pnl)}</FinancialToneText> : "未知"}
                    {externalAccountResult && <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>账户结果</Typography>}
                  </TableCell>
                  <TableCell className="mono" align="right">{noAction ? "不适用" : dispositionCostComplete || resultAvailable ? usdt(result.commission) : "未知"}</TableCell>
                  <TableCell>
                    {noAction ? "不适用" : durationText(result.holding_duration_seconds)}
                    <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                      {noAction
                        ? "未发生交易"
                        : positionDisposition && result.closed === true
                          ? "处置责任已闭合 · 不计算策略盈亏"
                        : resultAvailable
                          ? reviewExitReason(result)
                          : unknownActionCount > 0
                            ? `${unknownActionCount} 个动作结果未决`
                            : openActionCount > 0
                              ? `${openActionCount} 个责任尚未闭合`
                              : "结果事实尚不完整"}
                    </Typography>
                  </TableCell>
                  <TableCell>{translatedLabel(reviewResultLabels, primaryResult)}</TableCell>
                  <TableCell sx={{ minWidth: 160, maxWidth: 200, verticalAlign: "top" }}>
                    {translatedLabel(evaluationResultLabels, reviewConclusion(review))}
                    {ownerConclusionReason && <ClampedTooltipText text={ownerConclusionReason} />}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </TableContainer>
      {filter !== "STAGE" && remaining > 0 && <Button variant="text" sx={{ mt: 1.5 }} onClick={() => setVisibleCount((count) => count + 20)}>显示更多（剩余 {remaining} 条）</Button>}
      {filter !== "STAGE" && visibleReviews.length === 0 && <Alert severity="info" variant="outlined">当前筛选下没有复盘记录。</Alert>}
    </Box>
  );
}

function ReviewDetails({
  reviewId: reviewIdOverride,
  embedded = false,
  pricePathInterval,
}: {
  reviewId?: string;
  embedded?: boolean;
  pricePathInterval?: ReviewPricePathInterval;
} = {}) {
  const { reviewId: routeReviewId = "" } = useParams();
  const reviewId = reviewIdOverride ?? routeReviewId;
  const navigate = useNavigate();
  const { marketColorScheme, status: settingsStatus } = useOutletContext<FrameContext>();
  const expectedMarketSource = expectedMarketSourceForEnvironment(
    settingsStatus.environment_kind,
  );
  const environmentScope = `${settingsStatus.environment_kind}:${settingsStatus.environment_id}`;
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ["review", reviewId], queryFn: () => getReview(reviewId), enabled: Boolean(reviewId), refetchInterval: 30_000 });
  const latestReview = recordOf(query.data?.review);
  const availableVersions = recordsOf(query.data?.versions).length > 0
    ? recordsOf(query.data?.versions)
    : Object.keys(latestReview).length > 0
      ? [latestReview]
      : [];
  const latestReviewVersion = Number(latestReview.review_version ?? 0);
  const [selectedReviewVersion, setSelectedReviewVersion] = useState<number | null>(null);
  const review = selectedReviewVersion === null
    ? latestReview
    : availableVersions.find((item) => Number(item.review_version ?? 0) === selectedReviewVersion) ?? latestReview;
  const viewingLatestReview = Number(review.review_version ?? 0) === latestReviewVersion;
  const liveReadOnly = settingsStatus.profile === "BINANCE_LIVE_READ_ONLY";
  const activationId = valueOf(review, "activation_id", "");
  const activationQuery = useQuery({
    queryKey: ["activation", activationId],
    queryFn: () => getActivation(activationId),
    enabled: Boolean(activationId),
    refetchInterval: 30_000,
  });
  const timelineQuery = useQuery({
    queryKey: ["activation-timeline", activationId],
    queryFn: () => getActivationTimeline(activationId),
    enabled: Boolean(activationId),
  });
  const strategiesQuery = useQuery({ queryKey: ["strategies"], queryFn: getStrategies });
  const [conclusion, setConclusion] = useState<ReviewClassificationValue | "">("");
  const [reviewNote, setReviewNote] = useState("");
  const [correctingConclusion, setCorrectingConclusion] = useState(false);
  const [chartIntervalOverride, setChartIntervalOverride] = useState<ReviewChartInterval | null>(null);

  useEffect(() => {
    setChartIntervalOverride(null);
  }, [reviewId]);

  useEffect(() => {
    const ownerConclusion = recordOf(recordOf(review.evaluations).owner_conclusion);
    const storedConclusion = valueOf(ownerConclusion, "result", "");
    setConclusion(
      reviewClassificationOptions.some((item) => item.value === storedConclusion)
        ? storedConclusion as ReviewClassificationValue
        : "",
    );
    setReviewNote(valueOf(ownerConclusion, "reason", ""));
    setCorrectingConclusion(false);
  }, [review.content_digest]);

  const refreshMutation = useMutation({
    mutationFn: () => refreshReview(reviewId, Number(review.review_version ?? 0)),
    onSuccess: async () => {
      setSelectedReviewVersion(null);
      await queryClient.invalidateQueries({ queryKey: ["review", reviewId] });
      await queryClient.invalidateQueries({ queryKey: ["reviews"] });
    },
  });
  const completionMutation = useMutation({
    mutationFn: () => {
      const payload: ReviewCompletionPayload = {
        expected_version: Number(review.review_version ?? 0),
        conclusion: conclusion as ReviewCompletionPayload["conclusion"],
        note: reviewNote,
      };
      return completeReview(reviewId, payload);
    },
    onSuccess: async () => {
      setSelectedReviewVersion(null);
      setCorrectingConclusion(false);
      await queryClient.invalidateQueries({ queryKey: ["review", reviewId] });
      await queryClient.invalidateQueries({ queryKey: ["reviews"] });
    },
  });

  const inputRefs = recordOf(review.input_refs);
  const reviewTradeResult = tradeResultForReview(review);
  const tradeContext = recordOf(review.trade_context);
  const reviewPlan = recordOf(activationQuery.data?.plan);
  const planName = valueOf(tradeContext, "plan_name", valueOf(reviewPlan, "plan_name", ""));
  const decisionBasisRef = valueOf(
    tradeContext,
    "decision_basis_ref",
    valueOf(recordOf(activationQuery.data?.activation), "decision_basis_ref", valueOf(tradeContext, "strategy_id", "")),
  );
  const directExecution = isDirectExecution(decisionBasisRef);
  const strategyRef = directExecution
    ? decisionBasisRef
    : valueOf(recordOf(activationQuery.data?.strategy), "strategy_ref", decisionBasisRef);
  const strategyId = strategyRef.split("@", 1)[0];
  const strategy = directExecution
    ? undefined
    : strategiesQuery.data?.find((item) => item.strategy_id === strategyId);
  const decisionBasisLabel = directExecution
    ? DIRECT_EXECUTION_LABEL
    : strategy?.display_name ?? strategyRef;
  const fills = recordsOf(reviewTradeResult.fills);
  const planEvents = (timelineQuery.data ?? []).filter((item) => valueOf(item, "source") === "PLAN_EVENT");
  const acceptedTriggerEvents = planEvents.filter((item) => {
    const detail = recordOf(item.detail);
    return valueOf(detail, "rule_id") === (directExecution ? "DIRECT_ORDER_SCHEDULE_LEG" : "ENTRY_BREAKOUT")
      && ["PROPOSED_ACTION_CAP_ACCEPTED", "DEMO_ORDER_FLOW_CHECK"].includes(valueOf(item, "status"));
  });
  const triggerEvent = acceptedTriggerEvents[0]
    ?? planEvents.find((item) => valueOf(item, "status") === "PROPOSAL_CREATED");
  const triggerDetail = recordOf(triggerEvent?.detail);
  const triggerEntryRiskContext = recordOf(triggerDetail.entry_risk_context);
  const reviewPrimaryResult = valueOf(review, "primary_result");
  const openResponsibilities = recordOf(review.open_responsibilities);
  const unknownActionRefs = Array.isArray(openResponsibilities.unknown_action_refs) ? openResponsibilities.unknown_action_refs : [];
  const openActionRefs = Array.isArray(openResponsibilities.execution_action_refs) ? openResponsibilities.execution_action_refs : [];
  const unresolvedResultRefs = Array.isArray(reviewTradeResult.unresolved_refs) ? reviewTradeResult.unresolved_refs : [];
  const actions = recordsOf(activationQuery.data?.execution_actions);
  const firstFillAt = valueOf(reviewTradeResult, "first_fill_time", "");
  const lastFillAt = valueOf(reviewTradeResult, "last_fill_time", "");
  const activation = recordOf(activationQuery.data?.activation);
  const reviewEntryConditions = directExecution
    ? directEntryConditionDetail(recordOf(activation.order_schedule_snapshot))
    : "";
  const activationClosedAt = valueOf(
    tradeContext,
    "activation_updated_at",
    valueOf(activation, "updated_at", valueOf(review, "fact_cutoff", "")),
  );
  const fallbackAt = activationClosedAt;
  const baseStartMs = Date.parse(firstFillAt || fallbackAt);
  const baseEndMs = Date.parse(lastFillAt || fallbackAt);
  const oneMinuteWindowAvailable = reviewWindowFitsInterval(baseStartMs, baseEndMs, "1m");
  const chartInterval = chartIntervalOverride
    ?? defaultReviewChartInterval(baseStartMs, baseEndMs);
  const pricePathEvidenceInterval = pricePathInterval ?? chartInterval;
  const intervalMs = chartInterval === "1m" ? 60_000 : 15 * 60_000;
  const paddingBars = chartInterval === "1m" ? 24 : 12;
  const latestCompleteBarOpenMs = Math.floor(Date.now() / intervalMs) * intervalMs - intervalMs;
  const chartStart = Number.isFinite(baseStartMs) ? new Date(baseStartMs - intervalMs * paddingBars).toISOString() : "";
  const chartEnd = Number.isFinite(baseEndMs)
    ? new Date(Math.min(baseEndMs + intervalMs * paddingBars, latestCompleteBarOpenMs)).toISOString()
    : "";
  const marketWindowQuery = useQuery({
    queryKey: [
      "review-market-window",
      environmentScope,
      expectedMarketSource,
      valueOf(tradeContext, "instrument_ref", ""),
      chartInterval,
      chartStart,
      chartEnd,
    ],
    queryFn: () => getMarketWindow(
      valueOf(tradeContext, "instrument_ref"),
      chartStart,
      chartEnd,
      chartInterval,
      "EXECUTION_REVIEW",
    ),
    enabled: Boolean(
      !embedded
      &&
      expectedMarketSource
      && valueOf(tradeContext, "instrument_ref", "")
      && chartStart
      && chartEnd
    ),
    staleTime: 10 * 60_000,
  });
  const instrumentRef = valueOf(tradeContext, "instrument_ref");
  const direction = valueOf(tradeContext, "direction");
  const reviewClosed = reviewTradeResult.calculation_complete === true && reviewTradeResult.closed === true;
  const externalAccountResult = reviewTradeResult.result_scope === "ACCOUNT_FACTS_WITH_EXTERNAL_CLOSURE";
  const reviewPositionAlignment = recordOf(tradeContext.position_alignment);
  const positionDisposition = reviewTradeResult.result_scope === "EXTERNAL_POSITION_DISPOSITION"
    || Object.keys(reviewPositionAlignment).length > 0;
  const dispositionOperationallyClosed = positionDisposition
    && reviewTradeResult.closed === true
    && reviewTradeResult.execution_cost_complete === true;
  const tradeExpected = reviewPrimaryResult !== "NO_ACTION";
  const hasAttributedFills = fills.length > 0;
  const pricePathQuery = useQuery({
    queryKey: [
      "review-price-path-evidence",
      environmentScope,
      expectedMarketSource,
      reviewId,
      Number(review.review_version ?? 0),
      pricePathEvidenceInterval,
    ],
    queryFn: () => getReviewPricePathEvidence(
      reviewId,
      Number(review.review_version ?? 0),
      pricePathEvidenceInterval,
    ),
    enabled: Boolean(
      reviewId
      && review.review_version
      && tradeExpected
      && !positionDisposition
      && hasAttributedFills
    ),
    staleTime: 10 * 60_000,
  });
  const averageEntryPrice = finiteNumber(reviewTradeResult.average_entry_price);
  const averageExitPrice = finiteNumber(reviewTradeResult.average_exit_price);
  const factIssueMessages = [
    ...(unknownActionRefs.length > 0 ? [`${unknownActionRefs.length} 个执行动作的结果仍未决`] : []),
    ...(openActionRefs.length > 0 ? [`${openActionRefs.length} 个执行责任在复盘截止时尚未闭合`] : []),
    ...(unresolvedResultRefs.length > 0 ? [`${unresolvedResultRefs.length} 个复盘引用当前不可解析`] : []),
    ...(hasAttributedFills && reviewTradeResult.commission_complete !== true ? ["成交手续费事实尚未齐全"] : []),
    ...(hasAttributedFills && reviewTradeResult.fill_times_complete !== true ? ["成交时间事实尚未齐全"] : []),
  ];
  if (
    tradeExpected
    && !reviewClosed
    && !dispositionOperationallyClosed
    && factIssueMessages.length === 0
  ) {
    factIssueMessages.push("交易结果在复盘截止时尚未完整闭合");
  }
  const ownerConclusion = recordOf(recordOf(review.evaluations).owner_conclusion);
  const status = valueOf(review, "status");
  const reviewMutationAllowed = viewingLatestReview && !liveReadOnly;
  const conclusionEditable = reviewMutationAllowed && (status === "DRAFT" || correctingConclusion);
  const availableClassificationOptions = reviewClassificationOptions.filter((item) => (
    ["USABLE_SAMPLE", "VALIDATION_TRADE"].includes(item.value)
      ? ["COMPLETED", "PARTIAL"].includes(reviewPrimaryResult)
      : item.value === "NO_TRADE"
        ? reviewPrimaryResult === "NO_ACTION"
        : true
  ));
  const selectedClassification = reviewClassificationOptions.find((item) => item.value === conclusion);
  const classificationReasonMissing = Boolean(selectedClassification?.reasonRequired && !reviewNote.trim());
  const completionDisabled = completionMutation.isPending || !selectedClassification || classificationReasonMissing;
  const marketWindowSourceMismatch = Boolean(
    marketWindowQuery.data
    && !isMarketSourceForEnvironment(
      marketWindowQuery.data.source,
      settingsStatus.environment_kind,
    ),
  );
  const currentMarketWindow = marketWindowSourceMismatch
    ? undefined
    : marketWindowQuery.data;
  const marketBars = currentMarketWindow?.bars
    ? Array.from(currentMarketWindow.bars)
    : [];

  return (
    <Box sx={embedded
      ? { width: "100%", mt: 2 }
      : { width: "min(1320px, calc(100% - clamp(24px, 4vw, 48px)))", mx: "auto", py: { xs: 2, sm: 2.5 } }}>
      {embedded
        ? <PageHeader eyebrow="计划已结束" title="复盘与结果" description="保留运行期全部事实，并补充交易结果、成交明细和可沉淀的复盘结论。" />
        : <Typography component="h1" sx={visuallyHiddenSx}>一次激活复盘</Typography>}
      {query.isPending && <LinearProgress aria-label="正在读取复盘详情" />}
      {query.isError && <Alert severity="error">复盘身份或输入当前不可读；不会自动创建替代版本。</Alert>}
      {Object.keys(review).length > 0 && <>
        {!embedded && <Stack direction={{ xs: "column", md: "row" }} spacing={1.5} sx={{ justifyContent: "space-between", alignItems: { md: "flex-end" }, mb: 2 }}>
          <Box>
            <Button variant="text" size="small" onClick={() => navigate("/reviews")} sx={{ px: 0, minWidth: 0, mb: 0.5 }}>← 交易记录</Button>
            {planName && <Typography variant="h1" sx={{ mb: .25 }}>{planName}</Typography>}
            <Stack direction="row" spacing={1} sx={{ alignItems: "baseline", flexWrap: "wrap" }}>
              <Typography variant={planName ? "h2" : "h1"}>{instrumentRef}</Typography>
              <Typography sx={{ fontWeight: 750 }}><MarketToneText tone={marketToneForDirection(direction)}>{translatedLabel(directionLabels, direction)}</MarketToneText></Typography>
            </Stack>
            <Typography variant="body2" color="text.secondary">{positionDisposition ? positionAlignmentOperationLabel(reviewPositionAlignment) : decisionBasisLabel} · 闭合于 {formatUserVisibleTime(valueOf(reviewTradeResult, "last_fill_time", activationClosedAt))}</Typography>
            {directExecution && <Typography variant="caption" color="text.secondary" className="mono">{decisionBasisRef}</Typography>}
          </Box>
          <Stack direction="row" spacing={1} sx={{ alignItems: "center", flexWrap: "wrap" }}>
            {availableVersions.length > 1 && <TextField
              select
              size="small"
              label="复盘版本"
              value={Number(review.review_version ?? latestReviewVersion)}
              onChange={(event) => {
                const version = Number(event.target.value);
                setSelectedReviewVersion(version === latestReviewVersion ? null : version);
              }}
              sx={{ minWidth: 132 }}
            >
              {availableVersions.map((item) => {
                const version = Number(item.review_version ?? 0);
                return <MenuItem key={version} value={version}>v{version}{version === latestReviewVersion ? " · 当前" : " · 历史"}</MenuItem>;
              })}
            </TextField>}
            <Chip size="small" label={translatedLabel(reviewResultLabels, reviewPrimaryResult)} />
            <Chip size="small" variant="outlined" label={translatedLabel(reviewStatusLabels, status)} />
          </Stack>
        </Stack>}
        {!viewingLatestReview && <Alert severity="info" variant="outlined" sx={{ mb: 2 }}>
          正在查看不可变历史版本 v{valueOf(review, "review_version")}；当前版本为 v{latestReviewVersion}。历史版本不能刷新或修改。
        </Alert>}

        {!embedded && !positionDisposition ? (
          <PlanAiReviewRecord
            review={activationQuery.data?.ai_review}
            reviewRef={activationQuery.data?.ai_review_ref}
            required={Boolean(activationQuery.data?.ai_review_ref)}
            sx={{ mb: 2 }}
          />
        ) : null}

        {!embedded && <Box component="section" sx={{ ...surfaceFrameSx, p: { xs: 1.5, md: 2 }, mb: 2 }}>
          <Stack direction={{ xs: "column", sm: "row" }} spacing={1} sx={{ justifyContent: "space-between", alignItems: { sm: "center" }, mb: 1 }}>
            <Box>
              <Typography variant="h2">交易价格回看</Typography>
              <Typography variant="caption" color="text.secondary">成交与盈亏只由该复盘明确引用的交易所成交和手续费事实还原；引用缺失或冲突时保持未知。止损和止盈线来自当前激活记录。</Typography>
            </Box>
            <ToggleButtonGroup
              exclusive
              size="small"
              value={chartInterval}
              onChange={(_event, value: ReviewChartInterval | null) => {
                if (value) setChartIntervalOverride(value);
              }}
              aria-label="K 线周期"
              sx={{
                flexShrink: 0,
                "& .MuiToggleButton-root": {
                  minWidth: 72,
                  whiteSpace: "nowrap",
                },
              }}
            >
              <ToggleButton
                value="1m"
                disabled={!oneMinuteWindowAvailable}
                title={oneMinuteWindowAvailable
                  ? "显示 1 分钟 K 线"
                  : "本次交易跨度超过 1 分钟行情窗口上限，请使用 15 分钟完整回看"}
              >
                1 分钟
              </ToggleButton>
              <ToggleButton value="15m">15 分钟</ToggleButton>
            </ToggleButtonGroup>
          </Stack>
          {marketWindowQuery.isPending && <LinearProgress aria-label="正在读取复盘行情" />}
          {marketWindowQuery.isError && <Alert severity="warning" variant="outlined" sx={{ my: 1 }}>当前环境行情回看暂时不可用；成交、费用和复盘事实仍可核对。</Alert>}
          {marketWindowSourceMismatch && <Alert severity="error" variant="outlined" sx={{ my: 1 }}>
            K 线来源与当前 {settingsStatus.environment_kind} 环境不一致，已拒绝显示；不会用其他环境行情补齐复盘图。
          </Alert>}
          {marketBars.length > 0 && <Suspense fallback={<LinearProgress aria-label="正在加载 K 线图" />}><ReviewPriceChart bars={marketBars} fills={fills} actions={actions} interval={chartInterval} direction={direction} marketColorScheme={marketColorScheme} /></Suspense>}
          {!marketWindowQuery.isPending && !marketWindowQuery.isError && !marketWindowSourceMismatch && marketBars.length === 0 && <Alert severity="info" variant="outlined">当前时间窗没有可展示的 K 线。</Alert>}
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
            {currentMarketWindow
              ? `${settingsStatus.environment_kind} · ${currentMarketWindow.source} · 截止 ${formatUserVisibleTime(currentMarketWindow.source_cutoff)}`
              : `${settingsStatus.environment_kind} 行情仅提供事后价格上下文`}；图表不替代持久成交事实，也不证明当时可按图示价格成交。
          </Typography>
          <TradingViewAttribution />
        </Box>}

        {tradeExpected && !directExecution && !positionDisposition && <ReviewEntrySetupEvidencePanel
          entryRiskContext={Object.keys(triggerEntryRiskContext).length > 0 ? triggerEntryRiskContext : null}
          direction={direction}
          averageEntryPrice={valueOf(reviewTradeResult, "average_entry_price", "") || null}
          timelineFailed={timelineQuery.isError}
        />}

        {tradeExpected && !positionDisposition && hasAttributedFills && <ReviewPricePathEvidencePanel
          evidence={pricePathQuery.data}
          pending={pricePathQuery.isPending}
          failed={pricePathQuery.isError}
        />}

        {externalAccountResult && <Alert severity="warning" variant="outlined" sx={{ mb: 2 }}>
          出场由明确选定的 Binance 只减仓应急订单完成。以下盈亏是交易所成交与手续费形成的账户结果，不记作 Halpha 计划退出。
        </Alert>}
        {positionDisposition && <Alert severity="info" variant="outlined" sx={{ mb: 2 }}>
          这是外部持仓处置记录。Halpha 只归属本次 reduce-only 动作及其手续费；原始入场、历史资金费、毛盈亏和净盈亏均不归属本计划，也不会按 0 补齐。
        </Alert>}
        {factIssueMessages.length > 0 && <Alert
          severity={unknownActionRefs.length > 0 || unresolvedResultRefs.length > 0 ? "warning" : "info"}
          variant="outlined"
          sx={{ mb: 2 }}
          action={(
            <Button color="inherit" size="small" disabled={!reviewMutationAllowed || refreshMutation.isPending} onClick={() => refreshMutation.mutate()}>
              {refreshMutation.isPending ? "正在刷新…" : "刷新事实"}
            </Button>
          )}
        >
          {factIssueMessages.join("；")}。以上状态冻结于 {formatUserVisibleTime(valueOf(review, "fact_cutoff"))}，不会用 0 或推测值替代。
        </Alert>}
        <FactGrid facts={[
          { label: externalAccountResult ? "账户净盈亏" : "净盈亏", value: positionDisposition ? "不归属" : tradeExpected && reviewClosed ? signedUsdt(reviewTradeResult.net_pnl) : tradeExpected ? "未知" : "不适用", tone: !positionDisposition && tradeExpected && reviewClosed ? financialToneForSignedValue(reviewTradeResult.net_pnl) : undefined },
          { label: externalAccountResult ? "账户毛盈亏" : "毛盈亏", value: positionDisposition ? "不归属" : tradeExpected && reviewClosed ? signedUsdt(reviewTradeResult.gross_pnl) : tradeExpected ? "未知" : "不适用", tone: !positionDisposition && tradeExpected && reviewClosed ? financialToneForSignedValue(reviewTradeResult.gross_pnl) : undefined },
          { label: "手续费", value: tradeExpected && hasAttributedFills ? usdt(reviewTradeResult.commission) : tradeExpected ? "未知" : "不适用" },
          {
            label: "资金费",
            value: positionDisposition
              ? "历史不归属"
              : tradeExpected && hasAttributedFills
              ? reviewTradeResult.funding_included === true
                ? signedUsdt(reviewTradeResult.funding)
                : "尚无可确认记录"
              : tradeExpected ? "未知" : "不适用",
            note: positionDisposition
              ? "本计划不认领外部持仓建立以来的资金费"
              : tradeExpected && hasAttributedFills
              ? reviewTradeResult.funding_included === true
                ? "已计入净盈亏"
                : "净盈亏暂未计入资金费"
              : undefined,
          },
          { label: "持仓周期", value: positionDisposition ? "外部历史 · 不归属" : tradeExpected ? durationText(reviewTradeResult.holding_duration_seconds) : "不适用" },
          { label: "平均入场价", value: positionDisposition ? "外部事实" : tradeExpected && averageEntryPrice !== null ? `${marketPrice(String(averageEntryPrice))} USDT` : tradeExpected ? "未知" : "不适用" },
          { label: "平均出场价", value: tradeExpected && averageExitPrice !== null ? `${marketPrice(String(averageExitPrice))} USDT` : tradeExpected ? "未知" : "不适用" },
          { label: "入场成交额", value: positionDisposition ? "外部基线" : tradeExpected && hasAttributedFills ? usdt(reviewTradeResult.entry_notional) : tradeExpected ? "未知" : "不适用" },
          { label: "退出原因", value: positionDisposition ? positionAlignmentOperationLabel(reviewPositionAlignment) : tradeExpected ? reviewExitReason(reviewTradeResult, actions) : "未发生交易" },
        ]} />

        <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", lg: "minmax(0,1fr) minmax(360px,.8fr)" }, gap: 2, mt: 2 }}>
          <Box component="section" sx={{ ...surfaceFrameSx, p: 2 }}>
            <Typography variant="h2" sx={{ mb: 1.5 }}>机器为何交易</Typography>
            {timelineQuery.isError && <Alert severity="warning" variant="outlined" sx={{ mb: 1.5 }}>激活时间线读取失败；以下不会把读取失败伪装成业务事实未知。</Alert>}
            <FactGrid facts={[
              {
                label: "操作理由",
                value: positionDisposition
                  ? positionAlignmentIntent(reviewPositionAlignment) ?? "按固定外部持仓基线执行处置"
                  : triggerEvent
                  ? directExecution && acceptedTriggerEvents.length > 0
                    ? `直接执行订单计划的 ${acceptedTriggerEvents.length} 个入场档位已通过资金检查`
                    : planEventSummary(valueOf(triggerEvent, "status"), valueOf(triggerDetail, "rule_id", ""))
                  : reviewPrimaryResult === "NO_ACTION" ? "未发生交易" : timelineQuery.isError ? "时间线读取失败" : "未知",
              },
              ...(directExecution && !positionDisposition ? [{
                label: "入场条件",
                value: reviewEntryConditions,
                note: triggerEvent
                  ? "执行器仅在该条件组成立后发起入场；成交价格仍由交易所决定"
                  : "本次没有可归属的入场触发事件",
              }] : []),
              {
                label: "触发来源",
                value: positionDisposition
                  ? "完整账户快照"
                  : triggerEvent
                  ? directExecution ? DIRECT_EXECUTION_LABEL : valueOf(triggerDetail, "source_identity")
                  : reviewPrimaryResult === "NO_ACTION" ? "不适用" : timelineQuery.isError ? "时间线读取失败" : "未知",
                note: positionDisposition
                  ? `${formatUserVisibleTime(valueOf(reviewPositionAlignment, "fact_cutoff"))} · ${shortDigest(valueOf(reviewPositionAlignment, "snapshot_ref"))}`
                  : triggerEvent
                  ? `${directExecution ? `${decisionBasisRef} · ` : ""}${formatUserVisibleTime(valueOf(triggerDetail, "source_cutoff"))} · ${shortDigest(valueOf(triggerEvent, "source_ref"))}`
                  : reviewPrimaryResult === "NO_ACTION" ? "没有入场触发事件" : timelineQuery.isError ? "请恢复时间线读取后再核对触发事实" : "没有可归属的入场触发事件",
              },
              { label: "首次成交", value: tradeExpected && hasAttributedFills ? formatUserVisibleTime(valueOf(reviewTradeResult, "first_fill_time")) : tradeExpected ? "未知" : "不适用" },
              { label: "末次成交", value: tradeExpected && hasAttributedFills ? formatUserVisibleTime(valueOf(reviewTradeResult, "last_fill_time")) : tradeExpected ? "未知" : "不适用" },
            ]} />
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1.5 }}>触发说明取自同一激活的时间线；成交结果仅使用该复盘明确引用的权威事实，缺失不补猜。</Typography>
          </Box>

          <Box component="section" sx={{ ...surfaceFrameSx, p: 2 }}>
            <Stack direction="row" spacing={1} sx={{ justifyContent: "space-between", alignItems: "center", mb: 1.5 }}>
              <Box><Typography variant="h2">复盘判断</Typography><Typography variant="caption" color="text.secondary">选择主要原因，用于后续筛选与改进。</Typography></Box>
              <Button variant="text" size="small" disabled={!reviewMutationAllowed || refreshMutation.isPending || !review.review_version} onClick={() => refreshMutation.mutate()}>刷新事实</Button>
            </Stack>
            {refreshMutation.isError && <Alert severity="error" sx={{ mb: 1.5 }}>刷新失败；旧版本保持不变。</Alert>}
            {conclusionEditable && <>
              <Stack spacing={1.5}>
                <TextField
                  select
                  size="small"
                  label="复盘分类"
                  value={conclusion}
                  onChange={(event) => setConclusion(event.target.value as ReviewClassificationValue)}
                >
                  <MenuItem value="" disabled>请选择复盘分类</MenuItem>
                  {availableClassificationOptions.map((item) => <MenuItem key={item.value} value={item.value}>{item.label}</MenuItem>)}
                </TextField>
                {selectedClassification && (
                  <Box sx={{ px: 1.25, py: 1, bgcolor: "action.hover", borderRadius: 1 }}>
                    <Typography variant="body2">{selectedClassification.definition}</Typography>
                    <Typography variant="caption" color="text.secondary">后续用于：{selectedClassification.consumption}</Typography>
                  </Box>
                )}
                <TextField
                  size="small"
                  label={selectedClassification?.reasonRequired ? "具体说明 *" : "补充说明（可选）"}
                  value={reviewNote}
                  onChange={(event) => setReviewNote(event.target.value)}
                  multiline
                  minRows={3}
                  error={classificationReasonMissing}
                  helperText={classificationReasonMissing ? "此分类必须说明具体原因，才能成为可执行的改进输入。" : ""}
                  slotProps={{ htmlInput: { maxLength: 2000 } }}
                />
              </Stack>
              {completionMutation.isError && <Alert severity="error" sx={{ mt: 1.5 }}>评价未完成：{completionMutation.error instanceof ApiFailure ? completionMutation.error.code : "结果未知"}</Alert>}
              <Stack direction="row" spacing={1} sx={{ mt: 1.5 }}>
                <Button variant="contained" disabled={completionDisabled} onClick={() => completionMutation.mutate()}>{status === "DRAFT" ? "完成复盘" : "保存为新版本"}</Button>
                {status === "COMPLETE" && <Button
                  variant="text"
                  disabled={completionMutation.isPending}
                  onClick={() => {
                    const storedConclusion = valueOf(ownerConclusion, "result", "");
                    setConclusion(
                      reviewClassificationOptions.some((item) => item.value === storedConclusion)
                        ? storedConclusion as ReviewClassificationValue
                        : "",
                    );
                    setReviewNote(valueOf(ownerConclusion, "reason", ""));
                    setCorrectingConclusion(false);
                  }}
                >取消</Button>}
              </Stack>
            </>}
            {status === "COMPLETE" && !correctingConclusion && <>
              <Typography variant="body2" sx={{ mt: 1.5 }}>{translatedLabel(evaluationResultLabels, valueOf(ownerConclusion, "result"))}{valueOf(ownerConclusion, "reason", "") ? ` · ${valueOf(ownerConclusion, "reason")}` : ""}</Typography>
              {reviewMutationAllowed && <Button variant="outlined" size="small" sx={{ mt: 1.5 }} onClick={() => setCorrectingConclusion(true)}>重新分类并创建新版本</Button>}
            </>}
            {liveReadOnly && <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1.5 }}>实盘只读模式仅允许查看复盘，不允许刷新事实或修改结论。</Typography>}
          </Box>
        </Box>

        <Box component="section" sx={{ mt: 3 }}>
          <Typography variant="h2" sx={{ mb: 1.5 }}>成交明细</Typography>
          {fills.length > 0 ? <TableContainer className="table-scroll" role="region" aria-label="成交明细横向滚动区域" tabIndex={0} sx={{ ...surfaceFrameSx, overflowX: "auto" }}>
            <Table size="small" aria-label="本次复盘成交明细" sx={{ minWidth: 860 }}>
              <TableHead><TableRow>
                <TableCell>时间</TableCell><TableCell>行为</TableCell><TableCell align="right">数量</TableCell><TableCell align="right">价格</TableCell><TableCell align="right">成交额</TableCell><TableCell>流动性</TableCell><TableCell align="right">手续费</TableCell>
              </TableRow></TableHead>
              <TableBody>{fills.map((fill) => <TableRow key={valueOf(fill, "trade_id")} hover>
                <TableCell className="mono">{formatUserVisibleTime(valueOf(fill, "fill_time"))}</TableCell>
                <TableCell>{translatedLabel(actionKindLabels, valueOf(fill, "action_kind"))}</TableCell>
                <TableCell className="mono" align="right">{marketVolume(valueOf(fill, "quantity"))}</TableCell>
                <TableCell className="mono" align="right">{marketPrice(valueOf(fill, "price"))}</TableCell>
                <TableCell className="mono" align="right">{usdt(fill.notional)}</TableCell>
                <TableCell>{liquidityText(fill.liquidity_side)}</TableCell>
                <TableCell className="mono" align="right">{quoteAmount(valueOf(fill, "fee", "未知"))} {valueOf(fill, "fee_currency", "")}</TableCell>
              </TableRow>)}</TableBody>
            </Table>
          </TableContainer> : <Alert severity="info" variant="outlined">本次没有可归属的成交明细。</Alert>}
        </Box>

        <Box component="details" sx={{ ...surfaceFrameSx, mt: 3, p: 2 }}>
          <Box component="summary" sx={{ cursor: "pointer", fontWeight: 750 }}>系统机制与原始证据</Box>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 1.5, mb: 1.5 }}>用于故障定位和责任核对；不会把当前环境结果外推为其他环境中的收益能力。</Typography>
          <FactGrid facts={[
            { label: "复盘 / 版本", value: `${valueOf(review, "review_id")} / v${valueOf(review, "review_version")}` },
            { label: "版本原因", value: translatedLabel(reviewRevisionReasonLabels, valueOf(review, "revision_reason")) },
            { label: "激活", value: valueOf(review, "activation_id") },
            { label: "计划事件", value: String(recordsOf(inputRefs.plan_events).length) },
            { label: "执行动作", value: String(recordsOf(inputRefs.execution_actions).length) },
            { label: "交易所事实", value: String(recordsOf(inputRefs.venue_facts).length) },
            { label: "命令与回执", value: String(recordsOf(inputRefs.commands_and_receipts).length) },
            { label: "开放执行责任", value: String(openActionRefs.length) },
            { label: "结果未知责任", value: String(unknownActionRefs.length) },
          ]} />
          <Box component="details" sx={{ mt: 1.5 }}>
            <Box component="summary" sx={{ cursor: "pointer", fontWeight: 700 }}>查看 JSON 输入</Box>
            <Box component="pre" className="mono" role="region" aria-label="复盘权威输入与开放责任" tabIndex={0} sx={{ mt: 1.5, mb: 0, overflowX: "auto", fontSize: 11 }}>{JSON.stringify({ input_refs: review.input_refs, open_responsibilities: review.open_responsibilities }, null, 2)}</Box>
          </Box>
        </Box>
      </>}
    </Box>
  );
}


function ReviewRoute() {
  const { reviewId = "" } = useParams();
  const query = useQuery({
    queryKey: ["review", reviewId],
    queryFn: () => getReview(reviewId),
    enabled: Boolean(reviewId),
  });
  const review = recordOf(query.data?.review);
  const activationId = valueOf(review, "activation_id", "");

  if (query.isPending) {
    return <LinearProgress aria-label="正在进入计划详情" />;
  }
  if (query.isError || !activationId) {
    return (
      <Box sx={{ width: "min(880px, calc(100% - 32px))", mx: "auto", py: 3 }}>
        <Alert severity="error">复盘对应的计划当前不可读，无法进入统一计划详情。</Alert>
      </Box>
    );
  }
  return <Navigate to={`/activations/${activationId}`} replace />;
}


function WorkbenchRoutes({ status }: { status: SettingsStatus }) {
  return (
    <Routes>
      <Route element={<WorkbenchFrame status={status} />}>
        <Route path="/overview" element={<OverviewPage />} />
        <Route
          path="/scalping"
          element={(
            <Suspense fallback={<AppLoading />}>
              <ScalpingPage key={`${status.environment_id}:${status.account_id}`} />
            </Suspense>
          )}
        />
        <Route path="/plans" element={<PlansPage />} />
        <Route
          path="/plans/new"
          element={(
            <Suspense fallback={<AppLoading />}>
              <NewPlanPage />
            </Suspense>
          )}
        />
        <Route
          path="/plans/:planId/edit"
          element={(
            <Suspense fallback={<AppLoading />}>
              <NewPlanPage />
            </Suspense>
          )}
        />
        <Route path="/plans/:planVersionId/activate" element={<Navigate to="/plans" replace />} />
        <Route path="/activations/:activationId" element={<ActivationRoute />} />
        <Route path="/reviews" element={<ReviewsPage />} />
        <Route path="/reviews/:reviewId" element={<ReviewRoute />} />
        <Route path="/settings" element={<SettingsPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/overview" replace />} />
    </Routes>
  );
}

export default function App() {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: STATUS_QUERY_KEY,
    queryFn: getSettingsStatus,
    refetchInterval: 30_000,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    refetchOnReconnect: true,
  });
  const environmentScope = query.data
    ? marketEnvironmentScopeKey(
      query.data.environment_kind,
      query.data.environment_id,
    )
    : null;
  const initialEnvironmentScopeRef = useRef<string | null>(null);
  if (
    initialEnvironmentScopeRef.current === null
    && environmentScope !== null
  ) {
    initialEnvironmentScopeRef.current = environmentScope;
  }
  const environmentChanged = environmentScope !== null
    && initialEnvironmentScopeRef.current !== null
    && environmentScope !== initialEnvironmentScopeRef.current;

  useEffect(() => {
    if (!environmentChanged) return;
    queryClient.removeQueries({
      predicate: (cachedQuery) => cachedQuery.queryKey[0] !== STATUS_QUERY_KEY[0],
    });
    window.location.reload();
  }, [environmentChanged, environmentScope, queryClient]);

  if (query.isPending) return <AppLoading />;
  if (query.isError || !query.data) {
    return <ConnectionFailure retry={() => void query.refetch()} />;
  }
  if (environmentChanged) return <EnvironmentChanging />;
  return <WorkbenchRoutes status={query.data} />;
}
