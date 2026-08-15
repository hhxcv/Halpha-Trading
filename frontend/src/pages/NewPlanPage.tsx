import {
  Fragment,
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  Collapse,
  FormControlLabel,
  IconButton,
  LinearProgress,
  MenuItem,
  Slider,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  Tooltip,
  Typography,
} from "@mui/material";
import { ErrorOutlined, InfoOutlined, RefreshOutlined } from "@mui/icons-material";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useNavigate, useOutletContext, useParams, useSearchParams } from "react-router";

import {
  ApiFailure,
  createPlan,
  exportPlaybookQualification,
  getExecutionFeeEvidence,
  getMarketContext,
  getMarketFundingRateHistory,
  getLatestPlanAiReview,
  getOverview,
  getOverviewForEntry,
  getPlan,
  getStrategies,
  isUnknownMutationResult,
  previewDecisionEvidence,
  requestPlanAiReview,
  submitAndStartPlan,
  type DecisionEvidencePreviewPayload,
  type PlanCreatePayload,
  type PlanAiReview,
  type PlanAiReviewConfiguration,
  type PlanDraft,
  type PlanDraftPayload,
  type OrderScheduleSpec,
  type SettingsStatus,
  type StrategySummary,
  updatePlan,
} from "../api/client";
import PageHeader from "../components/PageHeader";
import FactGrid from "../components/FactGrid";
import OrderScheduleEditor, { createDefaultOrderScheduleSpec } from "../components/OrderScheduleEditor";
import PlanAiReviewPanel from "../components/PlanAiReviewPanel";
import {
  hydrateOrderScheduleSpec,
  isPositive,
} from "../components/orderScheduleEditorModel";
import OrderScheduleChart from "../components/OrderScheduleChart";
import { currentEntryBoundaryBreach } from "../components/orderScheduleDecisionAid";
import { retargetGeneratedEventCondition } from "../components/orderScheduleDirectionModel";
import type { OrderChartPriceAnnotation } from "../components/orderScheduleChartModel";
import StrategyIntroduction from "../components/StrategyIntroduction";
import DecisionEvidencePanel from "../components/DecisionEvidencePanel";
import {
  accountObservationBlockerText,
  NewRiskDisciplineBlockNotice,
  newRiskDisciplineBlockerText,
  newRiskDisciplineBlockerCodesFromFailureCode,
} from "../components/TradingDisciplineStrip";
import {
  directFundingCap,
  fundingAmountAtPercentage,
  fundingPercentageForAmount,
} from "../directFundingCap";
import {
  lossLimitedDirectEntry,
  scaleScheduleToEntryNotional,
} from "../directLossLimit";
import {
  formatFundingRatePercent,
  formatFundingSettlementInterval,
} from "../fundingEstimate";
import {
  closedBarBreakoutGapPercent,
  entryExtensionBoundary,
  formatUserVisibleTime,
  gapPercent,
  marketPrice,
  marketVolume,
  quoteAmount,
  quoteCurrencyAmount,
  subtractDecimal,
  tradingPrice,
} from "../format";
import {
  FinancialToneText,
  MarketToneText,
  financialToneForSignedValue,
  marketToneClassName,
  marketToneForDirection,
  type MarketColorScheme,
} from "../marketColors";
import {
  expectedMarketSourceForEnvironment,
  isMarketSourceForEnvironment,
  isUsableExecutionQuote,
  isUsableMarketStreamFunding,
  usePublicMarketStream,
  type MarketInterval,
  type MarketStreamClientStatus,
} from "../marketStream";
import {
  clearPersistentRequestIdentity,
  persistentRequestIdentity,
  type StableRequestIdentity,
} from "../requestIdentity";
import {
  defaultStrategyPlanName,
  shouldReplaceAutomaticPlanName,
} from "../planNaming";
import {
  readChartIntervalPreference,
  writeChartIntervalPreference,
} from "../chartIntervalPreference";
import { surfaceFrameSx } from "../theme";
import {
  initialStrategyPlanIntent,
  strategyAllowsPlanIntent,
} from "../strategyIntentQualification";
import {
  playbookQualificationFilename,
  serializePlaybookQualification,
  type PlaybookQualificationTarget,
} from "../playbookQualificationArtifact";
import {
  minimumRewardRisk,
  rewardRiskDisciplineBlocked,
  strategyRewardRisk,
  weightedTakeProfitRewardRisk,
} from "../planRewardRisk";


type Direction = "LONG" | "SHORT";
type PlanCreatorKind = "HUMAN" | "AI";
type PlanDecisionIntent = NonNullable<PlanCreatePayload["decision_context"]["intent"]>;
type PlanSetupFamily = NonNullable<PlanCreatePayload["decision_context"]["setup_family"]>;
type PlanDecisionContextInput = {
  rationale: string;
  evidence: string;
  limitations: string;
  intent: PlanDecisionIntent | "";
  setup_family: PlanSetupFamily | "";
  playbook_ref: string;
  invalidation: string;
  evidence_cutoff: string | null;
};
type StrategyDirectionFilter = "ALL" | Direction;
type StrategySort = "NAME_ASC" | "NAME_DESC" | "VERSION_DESC";
type DraftSaveState =
  | { status: "IDLE" }
  | { status: "SAVING" }
  | { status: "SAVED"; at: number }
  | { status: "FAILED"; message: string };
type SubmitAndStartAttempt = {
  planId: string;
  draftVersion: number;
};
type SubmitAndStartPhase = "IDLE" | "SUBMITTING";

function qualificationExportError(error: unknown): string {
  const code = error instanceof ApiFailure
    ? error.code
    : "PLAYBOOK_QUALIFICATION_EXPORT_FAILED";
  const labels: Record<string, string> = {
    PLAYBOOK_QUALIFICATION_EVIDENCE_NOT_CANDIDATE: "服务端最新证据已不再满足可重复性筛查",
    PLAYBOOK_QUALIFICATION_TRACEABILITY_INCOMPLETE: "样本复盘身份或事实截止不完整",
    PLAYBOOK_QUALIFICATION_SAMPLE_DIGEST_MISMATCH: "样本身份摘要与当前复盘集合不一致",
    PLAYBOOK_QUALIFICATION_EVIDENCE_INCOMPLETE: "筛查指标不完整",
    PLAYBOOK_QUALIFICATION_EXPORT_REQUIRES_DEMO: "只能在 Demo 上下文生成证据包",
  };
  return labels[code] ?? code;
}

function aiReviewRequestErrorMessage(
  error: unknown,
  requestFailed: boolean,
): string | null {
  if (error instanceof ApiFailure) {
    if (
      error.code === "PLAN_AI_REVIEW_CONTEXT_UNAVAILABLE"
      || error.code === "PLAN_AI_REVIEW_CONTEXT_TEMPORARILY_UNAVAILABLE"
      || error.code === "PLAN_AI_REVIEW_CONTEXT_INVALID"
    ) {
      return "AI 审核尚未开始：服务端未能形成当前审核上下文（非 AI 审核结论）。";
    }
    if (error.code === "PLAN_VERSION_CONFLICT") {
      return "草稿已更新；请等待页面读取最新版本后重新提交 AI 审核。";
    }
    return `AI 审核尚未开始（${error.code}，非 AI 审核结论）。`;
  }
  return requestFailed
    ? "AI 审核提交结果未知；请先等待页面重新读取状态。"
    : null;
}

function overviewReadFailureMessage(error: unknown): string {
  if (error instanceof ApiFailure) {
    if (error.code === "OVERVIEW_CONNECTION_FAILED") {
      return "本机账户纪律服务连接失败。";
    }
    if (error.code === "DATABASE_FACTS_UNAVAILABLE") {
      return "账户纪律服务无法读取当前账户事实。";
    }
    if (error.status >= 500) {
      return "账户纪律服务暂不可用。";
    }
  }
  return "账户纪律状态读取失败。";
}

const DIRECT_EXECUTION_REF = "DIRECT_EXECUTION@1";
// Must move with src/halpha/app/plan_ai_review.py.  A changed fixed prompt
// changes the meaning of an approval/rejection, so an earlier conclusion is
// retained for audit but cannot authorize the current plan.
const CURRENT_PLAN_AI_REVIEW_PROMPT_VERSION = "HALPHA_PLAN_AI_REVIEW_V4";
const DIRECT_DECISION_CONTEXT: PlanDecisionContextInput = {
  rationale: "",
  evidence: "",
  limitations: "",
  intent: "",
  setup_family: "",
  playbook_ref: "",
  invalidation: "",
  evidence_cutoff: null,
};
const ignoreStrategyChartRangeChange = () => undefined;
const ignoreStrategyChartPriceChange = () => undefined;

function strategyDecisionContext(
  strategy: StrategySummary,
): PlanDecisionContextInput {
  const evidenceState = String(
    strategy.economic_scope.profitability_evidence ?? "UNKNOWN",
  );
  const evidenceLimit = typeof strategy.economic_scope.evidence_limit === "string"
    ? strategy.economic_scope.evidence_limit
    : "策略证据边界当前未提供，不能据此推断盈利能力。";
  return {
    rationale: strategy.value_logic,
    evidence: `执行依据为 ${strategy.display_name}（${strategy.strategy_id}@${strategy.strategy_version}）；当前盈利证据状态为 ${evidenceState}。`,
    limitations: evidenceLimit,
    intent: initialStrategyPlanIntent(strategy.economic_scope),
    setup_family: "BREAKOUT_CONTINUATION",
    playbook_ref: "",
    invalidation: "未形成固定的闭合 K 线确认、超过最大追价边界、执行前价差不合格或保护边界无法成立时，不入场；入场后由固定止损与时间退出证伪。",
    evidence_cutoff: null,
  };
}

const setupFamilyLabels: Record<PlanSetupFamily, string> = {
  BREAKOUT_CONTINUATION: "突破延续",
  PULLBACK_CONTINUATION: "回调延续",
  RANGE_MEAN_REVERSION: "区间均值回归",
  REVERSAL: "趋势反转",
  EVENT_DRIVEN: "事件驱动",
  OTHER: "其他（需写清规则）",
};

function stableInputFingerprint(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map(stableInputFingerprint).join(",")}]`;
  }
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => (
      `${JSON.stringify(key)}:${stableInputFingerprint(record[key])}`
    )).join(",")}}`;
  }
  return JSON.stringify(value);
}

function decisionContextWithoutSystemEvidenceCutoff(
  context: PlanDraftPayload["decision_context"],
): Record<string, unknown> {
  const { evidence_cutoff: _systemEvidenceCutoff, ...userContext } = context;
  return userContext;
}

function reviewInputFingerprint(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return stableInputFingerprint(value);
  }
  const payload = value as Record<string, unknown>;
  const context = payload.decision_context;
  if (!context || typeof context !== "object" || Array.isArray(context)) {
    return stableInputFingerprint(payload);
  }
  const { evidence_cutoff: _systemEvidenceCutoff, ...userContext } = (
    context as Record<string, unknown>
  );
  return stableInputFingerprint({
    ...payload,
    decision_context: userContext,
  });
}

function newestDraft(
  first: PlanDraft | null,
  second: PlanDraft | null,
): PlanDraft | null {
  if (!first) return second;
  if (!second) return first;
  return first.draft_version >= second.draft_version ? first : second;
}

function savedDraftInput(draft: PlanDraft): Record<string, unknown> {
  const content = draft.content;
  const durationMinutes = Math.round(
    (Date.parse(content.valid_until) - Date.parse(content.valid_from)) / 60_000,
  );
  return {
    plan_name: content.plan_name,
    decision_context: content.decision_context
      ? decisionContextWithoutSystemEvidenceCutoff(content.decision_context)
      : null,
    decision_basis: content.decision_basis,
    ...(content.order_schedule_spec
      ? { order_schedule_spec: content.order_schedule_spec }
      : {}),
    ...(content.position_alignment
      ? { position_alignment: content.position_alignment }
      : {}),
    venue_ref: content.venue_ref,
    instrument_ref: content.instrument_ref,
    direction: content.direction,
    target_exposure: content.target_exposure,
    max_margin: content.requested_limits.max_margin,
    max_notional: content.requested_limits.max_notional,
    max_allowed_loss: content.requested_limits.max_allowed_loss,
    valid_minutes: durationMinutes,
  };
}

function scheduleInputWithoutDerivedFeeBudget(
  schedule: OrderScheduleSpec,
): OrderScheduleSpec {
  return {
    ...schedule,
    protection_policy: {
      ...schedule.protection_policy,
      // The fee budget is filled from the current venue fee evidence. It is
      // part of a persisted schedule, but is not a trader edit and must not
      // silently invalidate an AI review when that evidence refreshes.
      full_fill_loss_budget: null,
    },
  };
}

function directReviewInputFingerprint(input: {
  planName: string;
  decisionContext: PlanDraftPayload["decision_context"];
  instrument: string;
  direction: Direction;
  requestedNotional: string;
  validMinutes: string;
  orderSchedule: OrderScheduleSpec;
}): string {
  return stableInputFingerprint({
    plan_name: input.planName,
    decision_context: decisionContextWithoutSystemEvidenceCutoff(
      input.decisionContext,
    ),
    decision_basis: {
      kind: "DIRECT_EXECUTION",
      decision_basis_ref: DIRECT_EXECUTION_REF,
      parameters: {},
    },
    order_schedule_spec: scheduleInputWithoutDerivedFeeBudget(input.orderSchedule),
    venue_ref: "BINANCE_USDM",
    instrument_ref: input.instrument,
    direction: input.direction,
    requested_notional: input.requestedNotional,
    valid_minutes: input.validMinutes,
  });
}

function savedDirectReviewInputFingerprint(draft: PlanDraft): string {
  const content = draft.content;
  const durationMinutes = Math.round(
    (Date.parse(content.valid_until) - Date.parse(content.valid_from)) / 60_000,
  );
  if (!content.order_schedule_spec) return "";
  return directReviewInputFingerprint({
    planName: content.plan_name ?? "",
    decisionContext: content.decision_context ?? {
      rationale: "",
      evidence: "",
      limitations: "",
      intent: null,
      setup_family: null,
      playbook_ref: null,
      invalidation: "",
      evidence_cutoff: null,
    },
    instrument: content.instrument_ref,
    direction: content.direction,
    requestedNotional: content.requested_limits.max_notional,
    validMinutes: String(durationMinutes),
    orderSchedule: hydrateOrderScheduleSpec(content.order_schedule_spec),
  });
}

function marketStreamStatusText(status: MarketStreamClientStatus): string {
  if (status === "LIVE") return "实时";
  if (status === "STALE") return "已过期";
  if (status === "RECONNECTING") return "重连中";
  if (status === "CONNECTING") return "连接中";
  if (status === "FAILED") return "实时流不可用";
  return "实时流未启用";
}

function marketStreamStatusColor(
  status: MarketStreamClientStatus,
): "success" | "warning" | "error" | "default" {
  if (status === "LIVE") return "success";
  if (status === "STALE" || status === "RECONNECTING") return "warning";
  if (status === "FAILED") return "error";
  return "default";
}

type StrategyParameters = {
  direction: Direction;
  demo_immediate_entry: boolean;
  channel_lookback_15m: number;
  confirmation_bars_1m: number;
  entry_valid_minutes: number;
  initial_stop_atr_multiple: string;
  max_entry_extension_atr: string;
  max_hold_bars_15m: number;
  take_profit_1_fraction: string;
  take_profit_1_r: string;
  take_profit_2_r: string;
};

const DEFAULT_PARAMETERS: StrategyParameters = {
  direction: "LONG",
  demo_immediate_entry: false,
  channel_lookback_15m: 20,
  confirmation_bars_1m: 2,
  entry_valid_minutes: 60,
  initial_stop_atr_multiple: "1.5",
  max_entry_extension_atr: "0.5",
  max_hold_bars_15m: 4,
  take_profit_1_fraction: "0.5",
  take_profit_1_r: "1.5",
  take_profit_2_r: "3.0",
};


const strategySortLabels: Record<StrategySort, string> = {
  NAME_ASC: "策略名称 A–Z",
  NAME_DESC: "策略名称 Z–A",
  VERSION_DESC: "策略版本（新到旧）",
};


function StrategySelection({
  strategies,
  loading,
  failed,
  readOnly,
  disciplineNotice,
  onSelect,
  onSelectDirect,
  onCancel,
}: {
  strategies: StrategySummary[];
  loading: boolean;
  failed: boolean;
  readOnly: boolean;
  disciplineNotice?: ReactNode;
  onSelect: (strategyId: string) => void;
  onSelectDirect: () => void;
  onCancel: () => void;
}) {
  const [search, setSearch] = useState("");
  const [direction, setDirection] = useState<StrategyDirectionFilter>("ALL");
  const [sort, setSort] = useState<StrategySort>("NAME_ASC");
  const [expandedStrategyId, setExpandedStrategyId] = useState<string | null>(null);
  const normalizedSearch = search.trim().toLocaleLowerCase("zh-CN");
  const visibleStrategies = strategies
    .filter((strategy) => {
      const matchesSearch = normalizedSearch.length === 0 || [
        strategy.display_name,
        strategy.strategy_id,
        strategy.value_logic,
        strategy.applicable_scenarios,
        strategy.execution_behavior,
      ].some((value) => value.toLocaleLowerCase("zh-CN").includes(normalizedSearch));
      const matchesDirection = direction === "ALL"
        || strategy.supported_directions.includes(direction);
      return matchesSearch && matchesDirection;
    })
    .sort((left, right) => {
      if (sort === "VERSION_DESC") {
        const byVersion = right.strategy_version.localeCompare(left.strategy_version, "zh-CN", { numeric: true });
        if (byVersion !== 0) return byVersion;
      }
      const byName = left.display_name.localeCompare(right.display_name, "zh-CN", { numeric: true });
      return sort === "NAME_DESC" ? -byName : byName;
    });
  const filtersActive = normalizedSearch.length > 0 || direction !== "ALL" || sort !== "NAME_ASC";
  const hasProfitQualifiedStrategy = strategies.some(
    (strategy) => strategy.economic_scope.profitability_evidence === "POSITIVE_EXPECTANCY_SUPPORTED",
  );
  const resetFilters = () => {
    setSearch("");
    setDirection("ALL");
    setSort("NAME_ASC");
  };

  return (
    <Box sx={{ width: "min(1040px, calc(100% - clamp(32px, 4vw, 48px)))", mx: "auto", py: { xs: 2.5, sm: 3 } }}>
      <PageHeader
        eyebrow="新建交易计划"
        title="选择执行依据"
        description="可以让策略产生入场决定，也可以直接定义一组不可变订单；两种方式都经过相同的纪律与执行保护。"
      />
      {readOnly && (
        <Alert severity="info" variant="outlined" sx={{ mb: 2 }}>
          当前实盘入口为只读公开行情模式；可以查看策略说明，但不能配置、保存或提交并启动计划。
        </Alert>
      )}
      {disciplineNotice}
      <Box
        component="section"
        aria-labelledby="direct-execution-title"
        sx={{ ...surfaceFrameSx, p: { xs: 1.75, sm: 2 }, mb: 2, borderColor: "primary.main" }}
      >
        <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} sx={{ justifyContent: "space-between", alignItems: { xs: "stretch", sm: "center" } }}>
          <Box>
            <Typography id="direct-execution-title" variant="h2">直接执行订单计划</Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: .5, maxWidth: 680 }}>
              不等待策略信号。自行配置市价或限价、区间档位、金额分布、组合条件、逐成交止损止盈，以及到期或短时异动撤单。
            </Typography>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: .75 }}>
              当前仅开放已具备运行时消费者的串行受保护模式；预览不会提交订单。
            </Typography>
          </Box>
          <Button variant="contained" disabled={readOnly} onClick={onSelectDirect}>配置订单计划</Button>
        </Stack>
      </Box>
      {loading && <LinearProgress aria-label="正在读取策略列表" />}
      {failed && <Alert severity="warning">策略列表当前不可用；仍可使用上方的直接执行订单计划。</Alert>}

      {!failed && <>
        {!loading && strategies.length > 0 && !hasProfitQualifiedStrategy && (
          <Alert severity="warning" variant="outlined" sx={{ mb: 1.5 }}>
            当前没有通过费用后收益证据门槛的内置策略。下列策略只适合验证计划、成交、保护和退出链路，不适合以盈利为目标启动。
          </Alert>
        )}
        <Box component="section" aria-label="策略筛选与排序" sx={{ ...surfaceFrameSx, p: { xs: 1.5, sm: 2 }, mb: 1.5 }}>
          <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", sm: "minmax(240px, 1.6fr) minmax(150px, .8fr) minmax(190px, 1fr)" }, gap: 1.25 }}>
            <TextField
              size="small"
              label="筛选策略"
              placeholder="名称、标识、逻辑或适用场景"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
            <TextField
              select
              size="small"
              label="支持方向"
              value={direction}
              onChange={(event) => setDirection(event.target.value as StrategyDirectionFilter)}
            >
              <MenuItem value="ALL">全部方向</MenuItem>
              <MenuItem value="LONG">做多</MenuItem>
              <MenuItem value="SHORT">做空</MenuItem>
            </TextField>
            <TextField
              select
              size="small"
              label="排序"
              value={sort}
              onChange={(event) => setSort(event.target.value as StrategySort)}
            >
              {(Object.entries(strategySortLabels) as Array<[StrategySort, string]>).map(([value, label]) => (
                <MenuItem key={value} value={value}>{label}</MenuItem>
              ))}
            </TextField>
          </Box>
          <Stack direction="row" spacing={1.5} sx={{ mt: 1, alignItems: "center", justifyContent: "space-between" }}>
            <Typography variant="caption" color="text.secondary" role="status">
              匹配 {visibleStrategies.length} / {strategies.length} 个策略
            </Typography>
            <Button size="small" variant="outlined" disabled={!filtersActive} onClick={resetFilters}>重置筛选</Button>
          </Stack>
        </Box>

        <TableContainer className="table-scroll" role="region" aria-label="可用策略列表" tabIndex={0} sx={{ ...surfaceFrameSx, overflowX: "auto" }}>
          <Table size="small" aria-label="选择交易策略">
            <TableHead>
              <TableRow>
                <TableCell sx={{ width: 40, px: .5 }}>
                  <Box component="span" sx={{ position: "absolute", width: "1px", height: "1px", p: 0, m: -1, overflow: "hidden", clip: "rect(0 0 0 0)", whiteSpace: "nowrap", border: 0 }}>
                    策略介绍
                  </Box>
                </TableCell>
                <TableCell>策略</TableCell>
                <TableCell align="right" sx={{ width: { xs: 112, sm: 128 } }}>操作</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {visibleStrategies.map((strategy) => {
                const expanded = expandedStrategyId === strategy.strategy_id;
                const evidenceUnsupported = strategy.economic_scope.profitability_evidence === "NO_POSITIVE_EXPECTANCY_EVIDENCE";
                const directionLabels = strategy.supported_directions
                  .map((item) => item === "LONG" ? "做多" : item === "SHORT" ? "做空" : item)
                  .join(" / ");
                return <Fragment key={strategy.strategy_id}>
                  <TableRow
                    hover
                    tabIndex={readOnly ? -1 : 0}
                    aria-label={`选择策略：${strategy.display_name}`}
                    onClick={() => {
                      if (!readOnly) onSelect(strategy.strategy_id);
                    }}
                    onKeyDown={(event) => {
                      if (!readOnly && event.target === event.currentTarget && (event.key === "Enter" || event.key === " ")) {
                        event.preventDefault();
                        onSelect(strategy.strategy_id);
                      }
                    }}
                    sx={{ cursor: readOnly ? "default" : "pointer", "&:focus-visible": { bgcolor: "action.hover" } }}
                  >
                    <TableCell sx={{ width: 40, px: .5 }}>
                      <IconButton
                        size="small"
                        aria-label={`${expanded ? "收起" : "展开"}${strategy.display_name}策略介绍`}
                        aria-expanded={expanded}
                        aria-controls={`strategy-introduction-${strategy.strategy_id}`}
                        onClick={(event) => {
                          event.stopPropagation();
                          setExpandedStrategyId(expanded ? null : strategy.strategy_id);
                        }}
                        sx={{ width: 32, height: 32, border: 0, bgcolor: "transparent", fontSize: 18 }}
                      >
                        <Box component="span" aria-hidden="true" sx={{ lineHeight: 1 }}>{expanded ? "▾" : "▸"}</Box>
                      </IconButton>
                    </TableCell>
                    <TableCell sx={{ py: 1.5 }}>
                      <Stack direction="row" spacing={1} sx={{ alignItems: "center", flexWrap: "wrap" }}>
                        <Typography sx={{ fontWeight: 750 }}>{strategy.display_name}</Typography>
                        {evidenceUnsupported && (
                          <Chip size="small" color="warning" variant="outlined" label="仅流程验证" />
                        )}
                      </Stack>
                      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: .25, overflowWrap: "anywhere" }}>
                        {strategy.strategy_id} · v{strategy.strategy_version} · {directionLabels || "方向未声明"}
                      </Typography>
                    </TableCell>
                    <TableCell align="right" sx={{ py: 1 }}>
                      <Stack direction="row" spacing={.5} sx={{ justifyContent: "flex-end" }}>
                        <Button
                          size="small"
                          variant="outlined"
                          disabled={readOnly}
                          onClick={(event) => {
                            event.stopPropagation();
                            onSelect(strategy.strategy_id);
                          }}
                        >
                          {evidenceUnsupported ? "配置流程验证" : "配置策略"}
                        </Button>
                      </Stack>
                    </TableCell>
                  </TableRow>
                  <TableRow>
                    <TableCell colSpan={3} sx={{ p: 0, borderBottom: expanded ? undefined : 0 }}>
                      <Collapse in={expanded} timeout="auto" unmountOnExit>
                        <Box id={`strategy-introduction-${strategy.strategy_id}`} sx={{ px: { xs: 1.5, sm: 2 }, pb: 2, bgcolor: "background.default" }}>
                          <StrategyIntroduction strategy={strategy} embedded />
                        </Box>
                      </Collapse>
                    </TableCell>
                  </TableRow>
                </Fragment>;
              })}
              {!loading && visibleStrategies.length === 0 && (
                <TableRow>
                  <TableCell colSpan={3} sx={{ py: 5, textAlign: "center" }}>
                    <Typography sx={{ fontWeight: 700 }}>没有匹配的策略</Typography>
                    <Typography variant="body2" color="text.secondary" sx={{ mt: .5, mb: 1 }}>调整关键词或方向后重试。</Typography>
                    <Button size="small" onClick={resetFilters}>清除筛选</Button>
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </TableContainer>
      </>}

      <Button variant="outlined" onClick={onCancel} sx={{ mt: 2 }}>取消</Button>
    </Box>
  );
}


function numberInRange(value: string | number, minimum: number, maximum: number): boolean {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= minimum && parsed <= maximum;
}


function integerInRange(value: number, minimum: number, maximum: number): boolean {
  return Number.isInteger(value) && value >= minimum && value <= maximum;
}


export default function NewPlanPage() {
  const navigate = useNavigate();
  const { status, marketColorScheme } = useOutletContext<{
    status: SettingsStatus;
    marketColorScheme: MarketColorScheme;
  }>();
  const liveReadOnly = status.profile === "BINANCE_LIVE_READ_ONLY";
  const { planId } = useParams();
  const [searchParams] = useSearchParams();
  const sourcePlanId = searchParams.get("copyFrom");
  const directModeRequested = searchParams.get("mode") === "direct";
  const addPositionRequested = !planId
    && !sourcePlanId
    && directModeRequested
    && searchParams.get("positionOperation") === "ADD";
  const requestedInstrument = /^[A-Z0-9]+-PERP$/.test(searchParams.get("instrument") ?? "")
    ? searchParams.get("instrument") ?? "BTCUSDT-PERP"
    : "BTCUSDT-PERP";
  const requestedDirection: Direction = searchParams.get("direction") === "SHORT"
    ? "SHORT"
    : "LONG";
  // A draft's creator is immutable once its first version is saved.  The
  // assisted creation entry point therefore supplies its origin before the
  // periodic draft saver can allocate the plan id.
  const requestedCreatorKind: PlanCreatorKind = searchParams.get("creator_kind") === "AI"
    ? "AI"
    : "HUMAN";
  const requestedTradeAmountNumber = Number(searchParams.get("tradeAmount"));
  const requestedTradeAmountExplicit = requestedTradeAmountNumber > 0
    && Number.isFinite(requestedTradeAmountNumber);
  const requestedTradeAmount = requestedTradeAmountExplicit
    ? searchParams.get("tradeAmount") ?? "500"
    : "500";
  const sourcePositionSnapshotCutoff = searchParams.get("snapshotCutoff");
  const editing = Boolean(planId);
  const copying = Boolean(!editing && sourcePlanId);
  const loadedPlanId = planId ?? sourcePlanId;
  const [creationStep, setCreationStep] = useState<"strategy" | "configuration">(
    directModeRequested ? "configuration" : "strategy",
  );
  const [selectedStrategyId, setSelectedStrategyId] = useState<string | null>(
    directModeRequested ? DIRECT_EXECUTION_REF : null,
  );
  const strategies = useQuery({ queryKey: ["strategies"], queryFn: getStrategies });
  const accountOverview = useQuery({
    queryKey: ["overview"],
    queryFn: getOverview,
    refetchInterval: 5_000,
  });
  const draft = useQuery({
    queryKey: ["plan", loadedPlanId],
    queryFn: () => getPlan(loadedPlanId ?? ""),
    enabled: Boolean(loadedPlanId),
  });
  const draftBasis = draft.data?.content.decision_basis;
  const selectedBasisRef = draftBasis?.decision_basis_ref
    ?? selectedStrategyId
    ?? "";
  const directExecution = draftBasis?.kind === "DIRECT_EXECUTION"
    || selectedBasisRef === DIRECT_EXECUTION_REF;
  const strategyId = directExecution ? "" : selectedBasisRef;
  const selectingStrategy = !editing && !copying && creationStep === "strategy";
  const selectedStrategy = strategies.data?.find((strategy) => strategy.strategy_id === strategyId);
  const profitSeekingIntentAllowed = directExecution || Boolean(
    selectedStrategy
    && strategyAllowsPlanIntent(selectedStrategy.economic_scope, "PROFIT_SEEKING"),
  );
  const validationIntentAllowed = directExecution || Boolean(
    selectedStrategy
    && strategyAllowsPlanIntent(selectedStrategy.economic_scope, "VALIDATION"),
  );
  const initialPlanNameRef = useRef(
    directModeRequested
      ? `${requestedInstrument.replace(/-PERP$/, "")} ${addPositionRequested ? "独立追加开仓" : "直接执行"} ${formatUserVisibleTime(new Date().toISOString())}`.slice(0, 80)
      : "",
  );
  const [planName, setPlanName] = useState(initialPlanNameRef.current);
  const [decisionContext, setDecisionContext] = useState<PlanDecisionContextInput>(
    DIRECT_DECISION_CONTEXT,
  );
  const automaticPlanNameRef = useRef<string | null>(
    initialPlanNameRef.current || null,
  );
  const [creatorKind, setCreatorKind] = useState<PlanCreatorKind>(requestedCreatorKind);
  const [parameters, setParameters] = useState<StrategyParameters>(() => ({
    ...DEFAULT_PARAMETERS,
    direction: requestedDirection,
  }));
  const [instrument, setInstrument] = useState(requestedInstrument);
  const [tradeAmount, setTradeAmount] = useState(requestedTradeAmount);
  const [validMinutes, setValidMinutes] = useState("60");
  const [orderSchedule, setOrderSchedule] = useState<OrderScheduleSpec>(() => {
    const initialSchedule = createDefaultOrderScheduleSpec();
    if (!addPositionRequested) return initialSchedule;
    return {
      ...initialSchedule,
      amount_distribution: {
        ...initialSchedule.amount_distribution,
        base_notional: requestedTradeAmount,
      },
    };
  });
  const currentOrderScheduleRef = useRef(orderSchedule);
  useEffect(() => {
    currentOrderScheduleRef.current = orderSchedule;
  }, [orderSchedule]);
  const [chartInterval, setChartInterval] = useState<MarketInterval>(() => (
    readChartIntervalPreference(
      status.environment_id,
      "BTCUSDT-PERP",
    )
  ));
  const [stopReferenceInterval, setStopReferenceInterval] =
    useState<MarketInterval>("15m");
  const directReferenceSeededRef = useRef(false);
  const directReferenceSeedValueRef = useRef<string | null>(null);
  const pendingCreateIdentityRef = useRef<StableRequestIdentity | null>(null);
  const pendingAiReviewIdentityRef = useRef<StableRequestIdentity | null>(null);
  const pendingSubmitAndStartIdentityRef = useRef<StableRequestIdentity | null>(null);
  const [reviewDraft, setReviewDraft] = useState<PlanDraft | null>(null);
  const latestPersistedDraftRef = useRef<PlanDraft | null>(null);
  const [draftSaveState, setDraftSaveState] = useState<DraftSaveState>({ status: "IDLE" });
  const [submitAndStartPhase, setSubmitAndStartPhase] =
    useState<SubmitAndStartPhase>("IDLE");
  const [lastSavedInputFingerprint, setLastSavedInputFingerprint] = useState<string | null>(null);
  const autoSaveAttemptFingerprintRef = useRef<string | null>(null);
  const initializedSavedDraftRef = useRef<string | null>(null);
  const currentInputFingerprintRef = useRef("");
  const saveCurrentDraftRef = useRef<() => void>(() => undefined);
  const [aiReview, setAiReview] = useState<PlanAiReview | null>(null);
  const [aiReviewStarting, setAiReviewStarting] = useState(false);
  const [aiReviewConfiguration, setAiReviewConfiguration] =
    useState<PlanAiReviewConfiguration>({
      model: "gpt-5.6-terra",
      reasoning_effort: "medium",
    });
  const aiReviewInProgress = aiReviewStarting
    || aiReview?.status === "QUEUED"
    || aiReview?.status === "RUNNING";
  const [reviewedInputFingerprint, setReviewedInputFingerprint] =
    useState<string | null>(null);
  const [aiReviewClock, setAiReviewClock] = useState(() => Date.now());
  const [orderScheduleReady, setOrderScheduleReady] = useState(false);
  const [directMaximumProjectedLoss, setDirectMaximumProjectedLoss] =
    useState<string | null>(null);
  const [directEffectiveNotional, setDirectEffectiveNotional] =
    useState<string | null>(null);
  const [directRequestedNotional, setDirectRequestedNotional] =
    useState<string | null>(null);
  const [directScheduleProblems, setDirectScheduleProblems] = useState<string[]>([]);
  const directFundingDiscipline = useQuery({
    queryKey: [
      "overview",
      "direct-execution-funding",
      instrument,
      parameters.direction,
    ],
    queryFn: () => getOverviewForEntry({
      entryInstrumentRef: instrument,
      entryDirection: parameters.direction,
    }),
    enabled: directExecution && !selectingStrategy,
    refetchInterval: 5_000,
  });
  const directFundingDisciplineState = directFundingDiscipline.data?.new_risk_discipline;
  const directFundingCapState = directFundingCap(directFundingDisciplineState);
  const strategyFundingDiscipline = useQuery({
    queryKey: [
      "overview",
      "strategy-funding",
      instrument,
      parameters.direction,
      strategyId,
    ],
    queryFn: () => getOverviewForEntry({
      entryInstrumentRef: instrument,
      entryDirection: parameters.direction,
    }),
    enabled: !directExecution && !selectingStrategy && Boolean(strategyId),
    refetchInterval: 5_000,
  });
  const strategyFundingDisciplineState = strategyFundingDiscipline.data?.new_risk_discipline;
  const strategyFundingCapState = directFundingCap(strategyFundingDisciplineState);
  const directFundingCapSeededRef = useRef(false);
  const directFundingCapTouchedRef = useRef(
    requestedTradeAmountExplicit || editing || copying,
  );
  const handleOrderScheduleValidation = useCallback((ready: boolean) => {
    setOrderScheduleReady(ready);
  }, []);
  const handleMaximumProjectedLoss = useCallback((value: string | null) => {
    setDirectMaximumProjectedLoss(value);
  }, []);
  const handleEffectiveNotional = useCallback((value: string | null) => {
    setDirectEffectiveNotional(value);
  }, []);
  const handleRequestedNotional = useCallback((value: string | null) => {
    setDirectRequestedNotional(value);
  }, []);
  const handleChartIntervalChange = useCallback((interval: MarketInterval) => {
    setChartInterval(interval);
    writeChartIntervalPreference(status.environment_id, instrument, interval);
  }, [instrument, status.environment_id]);
  const channelLookbackValid = integerInRange(parameters.channel_lookback_15m, 4, 96);
  const expectedMarketSource = expectedMarketSourceForEnvironment(
    status.environment_kind,
  );
  const environmentScope = `${status.environment_kind}:${status.environment_id}`;
  useEffect(() => {
    setChartInterval(readChartIntervalPreference(
      status.environment_id,
      instrument,
    ));
  }, [instrument, status.environment_id]);
  const createIdentityScope = `${environmentScope}:CREATE_PLAN`;
  const market = useQuery({
    queryKey: [
      "market-context",
      environmentScope,
      expectedMarketSource,
      instrument,
      parameters.channel_lookback_15m,
    ],
    queryFn: () => getMarketContext(instrument, parameters.channel_lookback_15m),
    enabled: !selectingStrategy
      && (directExecution || Boolean(strategyId))
      && channelLookbackValid,
    retry: 1,
    retryDelay: 2_000,
  });
  const fundingHistory = useQuery({
    queryKey: ["market-funding-history", environmentScope, expectedMarketSource, instrument],
    queryFn: () => getMarketFundingRateHistory(instrument),
    enabled: !selectingStrategy && directExecution,
    retry: 1,
    staleTime: 60_000,
  });
  const stopReferenceMarket = useQuery({
    queryKey: [
      "stop-reference-market-context",
      environmentScope,
      expectedMarketSource,
      instrument,
      parameters.channel_lookback_15m,
      stopReferenceInterval,
    ],
    queryFn: () => getMarketContext(
      instrument,
      parameters.channel_lookback_15m,
      stopReferenceInterval,
    ),
    enabled: !selectingStrategy
      && directExecution
      && channelLookbackValid
      && stopReferenceInterval !== "15m",
    retry: 1,
    retryDelay: 2_000,
  });
  const executionFeeEvidence = useQuery({
    queryKey: ["execution-fee-evidence", environmentScope, instrument],
    queryFn: () => getExecutionFeeEvidence(instrument),
    enabled: !selectingStrategy && directExecution,
    retry: 1,
    staleTime: 60_000,
  });
  const marketStream = usePublicMarketStream(
    !selectingStrategy && (directExecution || Boolean(strategyId)),
    instrument,
    chartInterval,
    environmentScope,
    expectedMarketSource,
  );
  const recoveredMarketGenerationRef = useRef(0);
  useEffect(() => {
    recoveredMarketGenerationRef.current = 0;
    directReferenceSeededRef.current = false;
    const seededPrice = directReferenceSeedValueRef.current;
    directReferenceSeedValueRef.current = null;
    setOrderScheduleReady(false);
    if (seededPrice === null) return;
    setOrderSchedule((current) => (
      current.price_distribution.kind === "SINGLE"
      && current.price_distribution.limit_price === seededPrice
        ? {
          ...current,
          price_distribution: {
            ...current.price_distribution,
            limit_price: "",
          },
        }
        : current
    ));
  }, [environmentScope]);
  useEffect(() => {
    if (!directExecution) {
      recoveredMarketGenerationRef.current = 0;
      return;
    }
    if (marketStream.generation <= recoveredMarketGenerationRef.current) return;
    recoveredMarketGenerationRef.current = marketStream.generation;
    void market.refetch();
  }, [directExecution, market.refetch, marketStream.generation]);

  useEffect(() => {
    const source = draft.data?.content;
    if (!source) return;
    const sourceParameters = source.decision_basis.parameters;
    setParameters({
      ...DEFAULT_PARAMETERS,
      ...sourceParameters,
      direction: source.direction as Direction,
    } as StrategyParameters);
    if (source.order_schedule_spec) {
      const scheduleChanged = currentOrderScheduleRef.current
        !== source.order_schedule_spec;
      setOrderSchedule(hydrateOrderScheduleSpec(source.order_schedule_spec));
      if (scheduleChanged) setOrderScheduleReady(false);
    }
    setInstrument(source.instrument_ref);
    setTradeAmount(source.requested_limits.max_notional);
    setPlanName(editing
      ? source.plan_name ?? ""
      : `${source.plan_name?.trim() || "未命名计划"} 副本`.slice(0, 80));
    if (source.decision_context) {
      setDecisionContext({
        rationale: source.decision_context.rationale ?? "",
        evidence: source.decision_context.evidence ?? "",
        limitations: source.decision_context.limitations ?? "",
        intent: source.decision_context.intent ?? "",
        setup_family: source.decision_context.setup_family ?? "",
        playbook_ref: source.decision_context.playbook_ref ?? "",
        invalidation: source.decision_context.invalidation ?? "",
        evidence_cutoff: source.decision_context.evidence_cutoff ?? null,
      });
    }
    const duration = Math.round(
      (Date.parse(source.valid_until) - Date.parse(source.valid_from)) / 60_000,
    );
    if (Number.isFinite(duration) && duration > 0) setValidMinutes(String(duration));
  }, [draft.data?.content_digest, editing]);

  const update = <K extends keyof StrategyParameters>(key: K, value: StrategyParameters[K]) => {
    setParameters((current) => ({ ...current, [key]: value }));
  };
  const updateDirectTradeAmount = (
    nextAmount: string,
    options: { userInitiated?: boolean } = {},
  ) => {
    const { userInitiated = true } = options;
    if (userInitiated) directFundingCapTouchedRef.current = true;
    const previousAmount = Number(tradeAmount);
    const distribution = orderSchedule.amount_distribution;
    const pricePlan = orderSchedule.price_distribution;
    const baseNotional = Number(distribution.base_notional);
    const shouldSyncSingle = pricePlan.kind === "SINGLE"
      && distribution.mode === "FIXED"
      && Number.isFinite(previousAmount)
      && Math.abs(baseNotional - previousAmount) <= Math.max(1, Math.abs(previousAmount)) * 1e-9;
    const shouldSyncLadder = pricePlan.kind === "LADDER"
      && distribution.mode === "FIXED"
      && Number.isFinite(previousAmount)
      && Math.abs(baseNotional * pricePlan.level_count - previousAmount)
        <= Math.max(1, Math.abs(previousAmount)) * 1e-9;
    setTradeAmount(nextAmount);
    if (!shouldSyncSingle && !shouldSyncLadder) return;
    const nextNumeric = Number(nextAmount);
    const nextBase = shouldSyncLadder && Number.isFinite(nextNumeric)
      && pricePlan.kind === "LADDER"
      && Number.isInteger(pricePlan.level_count)
      && pricePlan.level_count > 0
      ? String(Number((nextNumeric / pricePlan.level_count).toFixed(8)))
      : nextAmount;
    setOrderSchedule((current) => ({
      ...current,
      amount_distribution: {
        ...current.amount_distribution,
        base_notional: nextBase,
      },
    }));
    setOrderScheduleReady(false);
  };
  useEffect(() => {
    const maximum = directFundingCapState.maximum;
    if (
      directFundingCapSeededRef.current
      || !directExecution
      || editing
      || copying
      || directFundingCapTouchedRef.current
      || maximum === null
      || !Number.isFinite(Number(maximum))
      || Number(maximum) <= 0
    ) return;
    directFundingCapSeededRef.current = true;
    updateDirectTradeAmount(maximum, { userInitiated: false });
  }, [
    copying,
    directExecution,
    directFundingCapState.maximum,
    editing,
  ]);
  const selectStrategy = (nextStrategyId: string) => {
    if (nextStrategyId !== selectedStrategyId) setParameters(DEFAULT_PARAMETERS);
    const strategy = strategies.data?.find((item) => item.strategy_id === nextStrategyId);
    if (strategy) {
      const automaticName = defaultStrategyPlanName(
        strategy,
        formatUserVisibleTime(new Date().toISOString()),
      );
      setPlanName((current) => (
        shouldReplaceAutomaticPlanName(current, automaticPlanNameRef.current)
          ? automaticName
          : current
      ));
      automaticPlanNameRef.current = automaticName;
      setDecisionContext(strategyDecisionContext(strategy));
    }
    setSelectedStrategyId(nextStrategyId);
    setCreationStep("configuration");
    window.requestAnimationFrame(() => window.scrollTo({ top: 0, left: 0 }));
  };
  const selectDirectExecution = () => {
    const automaticName = `BTCUSDT 直接执行 ${formatUserVisibleTime(new Date().toISOString())}`.slice(0, 80);
    setSelectedStrategyId(DIRECT_EXECUTION_REF);
    setParameters(DEFAULT_PARAMETERS);
    setOrderSchedule(createDefaultOrderScheduleSpec());
    setDecisionContext(DIRECT_DECISION_CONTEXT);
    setPlanName((current) => (
      shouldReplaceAutomaticPlanName(current, automaticPlanNameRef.current)
        ? automaticName
        : current
    ));
    automaticPlanNameRef.current = automaticName;
    directReferenceSeededRef.current = false;
    directReferenceSeedValueRef.current = null;
    directFundingCapSeededRef.current = false;
    directFundingCapTouchedRef.current = false;
    setOrderScheduleReady(false);
    setCreationStep("configuration");
    window.requestAnimationFrame(() => window.scrollTo({ top: 0, left: 0 }));
  };
  const confirmationBarsValid = integerInRange(parameters.confirmation_bars_1m, 1, 3);
  const entryValidityValid = integerInRange(parameters.entry_valid_minutes, 15, 10080);
  const maxHoldingBarsValid = integerInRange(parameters.max_hold_bars_15m, 4, 672);
  const planValidityValid = numberInRange(validMinutes, 15, 10080)
    && Number.isInteger(Number(validMinutes));
  const normalizedPlanName = planName.trim();
  const planNameValid = normalizedPlanName.length > 0 && normalizedPlanName.length <= 80;
  const tradeAmountInputValid = Number(tradeAmount) > 0 && Number.isFinite(Number(tradeAmount));
  const directFundingMaximum = directFundingCapState.maximum;
  const directFundingMaximumNumber = directFundingMaximum !== null
    && Number.isFinite(Number(directFundingMaximum))
    ? Number(directFundingMaximum)
    : null;
  const directFundingSliderValue = fundingPercentageForAmount(
    directFundingMaximum,
    tradeAmount,
  );
  const directFundingCapExceeded = directExecution
    && directFundingMaximumNumber !== null
    && directFundingMaximumNumber > 0
    && Number(tradeAmount) > directFundingMaximumNumber + 1e-8;
  const directFundingCapacityExhausted = directExecution
    && directFundingMaximumNumber !== null
    && directFundingMaximumNumber <= 0;
  const directFundingDisciplineBlocked = directExecution
    && directFundingDisciplineState?.new_risk_allowed === false;
  const directFundingDisciplineIssue = !directExecution
    ? null
    : directFundingDiscipline.isError
      ? overviewReadFailureMessage(directFundingDiscipline.error)
      : !directFundingDiscipline.data
        ? "正在读取账户纪律。"
        : directFundingDisciplineBlocked
          ? (
            accountObservationBlockerText(
              directFundingDiscipline.data,
              status.executor_status,
            ) ?? newRiskDisciplineBlockerText(
              directFundingDisciplineState?.blocker_codes ?? [],
            )
          ) || "当前交易纪律阻止新增风险。"
          : null;
  const directPlanLossLimit = directFundingDisciplineState?.max_plan_loss ?? null;
  const directLossLimitedEntry = lossLimitedDirectEntry({
    effectiveNotional: directEffectiveNotional,
    maximumProjectedLoss: directMaximumProjectedLoss,
    maxPlanLoss: directPlanLossLimit,
  });
  const directScheduleForSubmission = scaleScheduleToEntryNotional(
    orderSchedule,
    directRequestedNotional,
    directLossLimitedEntry.maximumEntryNotional,
  );
  const directExecutionNotional = directLossLimitedEntry.maximumEntryNotional
    ?? tradeAmount;
  const strategyFundingMaximum = strategyFundingCapState.maximum;
  const strategyFundingMaximumNumber = strategyFundingMaximum !== null
    && Number.isFinite(Number(strategyFundingMaximum))
    ? Number(strategyFundingMaximum)
    : null;
  const strategyFundingCapExceeded = !directExecution
    && strategyFundingMaximumNumber !== null
    && strategyFundingMaximumNumber > 0
    && Number(tradeAmount) > strategyFundingMaximumNumber + 1e-8;
  const directRewardRiskRatio = weightedTakeProfitRewardRisk(
    orderSchedule.protection_policy.take_profit_ladder?.levels ?? [],
  );
  const strategyRewardRiskRatio = strategyRewardRisk(parameters);
  const applicableMinimumRewardRisk = minimumRewardRisk(
    (directExecution ? directFundingDisciplineState : strategyFundingDisciplineState)
      ?.minimum_reward_risk_ratio,
  );
  const applicableRewardRiskRatio = directExecution
    ? directRewardRiskRatio
    : strategyRewardRiskRatio;
  const planRewardRiskBlocked = rewardRiskDisciplineBlocked(
    applicableRewardRiskRatio,
    applicableMinimumRewardRisk,
  );
  const tradeAmountValid = tradeAmountInputValid
    && !directFundingCapExceeded
    && !strategyFundingCapExceeded;
  const initialStopValid = numberInRange(parameters.initial_stop_atr_multiple, 1, 3);
  const maxExtensionValid = numberInRange(parameters.max_entry_extension_atr, .1, 1);
  const takeProfitFractionValid = numberInRange(parameters.take_profit_1_fraction, .25, .75);
  const takeProfit1Valid = numberInRange(parameters.take_profit_1_r, 1, 3);
  const takeProfit2Valid = numberInRange(parameters.take_profit_2_r, 2, 6);
  const takeProfitOrderValid = takeProfit1Valid
    && takeProfit2Valid
    && Number(parameters.take_profit_2_r) > Number(parameters.take_profit_1_r);
  const strategyParameterRangesValid = channelLookbackValid
    && confirmationBarsValid
    && entryValidityValid
    && maxHoldingBarsValid
    && initialStopValid
    && maxExtensionValid
    && takeProfitFractionValid
    && takeProfitOrderValid;
  const configurationValid = planValidityValid
    && tradeAmountValid
    && !planRewardRiskBlocked
    && (directExecution
      ? orderScheduleReady && isPositive(directMaximumProjectedLoss ?? "")
      : strategyParameterRangesValid);
  const marketSourceMismatch = Boolean(
    market.data
    && !isMarketSourceForEnvironment(
      market.data.source,
      status.environment_kind,
    ),
  );
  const currentMarket = market.data?.channel_lookback_15m === parameters.channel_lookback_15m
    && !marketSourceMismatch
    ? market.data
    : undefined;
  useEffect(() => {
    if (!currentMarket?.source_cutoff || decisionContext.evidence_cutoff) return;
    setDecisionContext((current) => (
      current.evidence_cutoff
        ? current
        : { ...current, evidence_cutoff: currentMarket.source_cutoff }
    ));
  }, [currentMarket?.source_cutoff, decisionContext.evidence_cutoff]);
  const selectedStopReferenceMarket = stopReferenceInterval === "15m"
    ? currentMarket
    : stopReferenceMarket.data?.channel_lookback_15m === parameters.channel_lookback_15m
      && stopReferenceMarket.data.stop_reference_interval === stopReferenceInterval
      && isMarketSourceForEnvironment(
        stopReferenceMarket.data.source,
        status.environment_kind,
      )
      ? stopReferenceMarket.data
      : undefined;
  const usableLiveQuote = marketStream.status === "LIVE"
    && isUsableExecutionQuote(
      marketStream.quote,
      expectedMarketSource,
      Date.now(),
    )
    ? marketStream.quote
    : null;
  const usableFunding = marketStream.status === "LIVE"
    && isUsableMarketStreamFunding(
      marketStream.funding,
      expectedMarketSource,
      Date.now(),
    )
    ? marketStream.funding
    : null;
  const usableFundingHistory = fundingHistory.data
    && isMarketSourceForEnvironment(fundingHistory.data.source, status.environment_kind)
    ? fundingHistory.data
    : null;
  const stableReferencePrice = currentMarket?.reference_price ?? null;
  const liveReferencePrice = usableLiveQuote?.reference_price ?? null;
  const visibleReferencePrice = liveReferencePrice ?? stableReferencePrice;
  const visibleBidPrice = usableLiveQuote?.bid_price
    ?? currentMarket?.bid_price
    ?? null;
  const visibleAskPrice = usableLiveQuote?.ask_price
    ?? currentMarket?.ask_price
    ?? null;
  const visibleSourceCutoff = usableLiveQuote?.source_cutoff
    ?? currentMarket?.source_cutoff
    ?? null;
  const optionalDecisionText = (value: string): string | null => {
    const normalized = value.trim();
    return normalized || null;
  };
  const normalizedDecisionContext: PlanDraftPayload["decision_context"] = {
    rationale: optionalDecisionText(decisionContext.rationale),
    evidence: optionalDecisionText(decisionContext.evidence),
    limitations: optionalDecisionText(decisionContext.limitations),
    intent: decisionContext.intent || null,
    setup_family: decisionContext.setup_family || null,
    playbook_ref: null,
    invalidation: optionalDecisionText(decisionContext.invalidation),
    evidence_cutoff: decisionContext.evidence_cutoff
      ?? currentMarket?.source_cutoff
      ?? null,
  };
  const decisionContextValid = (
    normalizedDecisionContext.rationale !== null
    && normalizedDecisionContext.rationale !== undefined
    && normalizedDecisionContext.rationale.length <= 2000
    && normalizedDecisionContext.intent !== null
    && normalizedDecisionContext.setup_family !== null
    && Boolean(normalizedDecisionContext.evidence_cutoff)
    && (!parameters.demo_immediate_entry || normalizedDecisionContext.intent === "VALIDATION")
    && (
      (normalizedDecisionContext.intent === "PROFIT_SEEKING" && profitSeekingIntentAllowed)
      || (normalizedDecisionContext.intent === "VALIDATION" && validationIntentAllowed)
    )
  );
  const decisionEvidencePayload: DecisionEvidencePreviewPayload = {
    instrument_ref: instrument,
    direction: parameters.direction,
    decision_basis: directExecution
      ? {
          kind: "DIRECT_EXECUTION",
          decision_basis_ref: DIRECT_EXECUTION_REF,
          parameters: {},
        }
      : {
          kind: "STRATEGY_SIGNAL",
          decision_basis_ref: strategyId,
          parameters,
        },
    intent: decisionContext.intent as PlanDecisionIntent,
    setup_family: decisionContext.setup_family as PlanSetupFamily,
    playbook_ref: null,
  };
  const decisionEvidence = useQuery({
    queryKey: [
      "decision-evidence-preview",
      environmentScope,
      instrument,
      parameters.direction,
      directExecution ? DIRECT_EXECUTION_REF : strategyId,
      directExecution ? "{}" : JSON.stringify(parameters),
      decisionContext.intent,
      decisionContext.setup_family,
      normalizedDecisionContext.playbook_ref,
    ],
    queryFn: () => previewDecisionEvidence(decisionEvidencePayload),
    enabled: !selectingStrategy
      && !directExecution
      && Boolean(strategyId)
      && Boolean(decisionContext.intent)
      && Boolean(decisionContext.setup_family)
      && strategyParameterRangesValid,
    retry: 1,
    staleTime: 30_000,
  });
  const qualificationExport = useMutation({
    mutationFn: (target: PlaybookQualificationTarget) => (
      exportPlaybookQualification({
        ...decisionEvidencePayload,
        target_venue_account_type: target,
      })
    ),
    onSuccess: (result, target) => {
      const content = serializePlaybookQualification(result);
      const filename = playbookQualificationFilename(
        result.artifact.cohort.playbook_ref,
        target,
        result.artifact_content_digest,
      );
      const url = window.URL.createObjectURL(
        new Blob([content], { type: "application/json;charset=utf-8" }),
      );
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      anchor.hidden = true;
      document.body.appendChild(anchor);
      try {
        anchor.click();
      } finally {
        anchor.remove();
        window.URL.revokeObjectURL(url);
      }
    },
  });
  useEffect(() => {
    qualificationExport.reset();
  }, [decisionEvidence.data?.sample_identity_digest]);
  const strategyPriceTickSize = instrument === "BTCUSDT-PERP" ? "0.1" : null;
  const strategyPrice = (value: string | number) => (
    tradingPrice(value, strategyPriceTickSize)
  );
  useEffect(() => {
    if (
      directReferenceSeededRef.current
      || !directExecution
      || editing
      || copying
      || !liveReferencePrice
    ) {
      return;
    }
    directReferenceSeededRef.current = true;
    if (
      orderSchedule.price_distribution.kind !== "SINGLE"
      || orderSchedule.venue_policy.order_type !== "LIMIT"
      || orderSchedule.venue_policy.price_match !== null
      || orderSchedule.price_distribution.limit_price?.trim()
    ) {
      return;
    }
    setOrderSchedule({
      ...orderSchedule,
      price_distribution: {
        ...orderSchedule.price_distribution,
        limit_price: liveReferencePrice,
      },
    });
    directReferenceSeedValueRef.current = liveReferencePrice;
    setOrderScheduleReady(false);
  }, [
    copying,
    directExecution,
    editing,
    liveReferencePrice,
    orderSchedule,
  ]);
  const marketContextRefreshing = channelLookbackValid && market.isFetching;
  const selectedBreakoutGap = currentMarket
    ? parameters.direction === "LONG"
      ? currentMarket.long_breakout_gap_pct
      : currentMarket.short_breakout_gap_pct
    : null;
  const selectedClosedBarBreakoutGap = currentMarket
    ? closedBarBreakoutGapPercent(
      parameters.direction,
      currentMarket.latest_close_1m,
      parameters.direction === "LONG" ? currentMarket.channel_upper : currentMarket.channel_lower,
    )
    : "";
  const currentSpread = currentMarket
    ? subtractDecimal(currentMarket.ask_price, currentMarket.bid_price) ?? ""
    : "";
  const visibleSpread = visibleBidPrice && visibleAskPrice
    ? subtractDecimal(visibleAskPrice, visibleBidPrice)
    : null;
  const currentSpreadBps = currentMarket
    ? Number(currentSpread) / Number(currentMarket.reference_price) * 10_000
    : Number.NaN;
  const selectedChannelBoundary = currentMarket
    ? parameters.direction === "LONG"
      ? currentMarket.channel_upper
      : currentMarket.channel_lower
    : "";
  const entryExtensionLimit = currentMarket
    ? entryExtensionBoundary(
      parameters.direction,
      selectedChannelBoundary,
      currentMarket.atr_14,
      parameters.max_entry_extension_atr,
    )
    : null;
  const latestClose1m = currentMarket ? Number(currentMarket.latest_close_1m) : Number.NaN;
  const latestClosedBarBeyondBoundary = currentMarket
    ? parameters.direction === "LONG"
      ? latestClose1m > Number(currentMarket.channel_upper)
      : latestClose1m < Number(currentMarket.channel_lower)
    : false;
  const latestClosedBarBeyondExtension = Number.isFinite(latestClose1m)
    && entryExtensionLimit !== null
    && (parameters.direction === "LONG"
      ? latestClose1m > entryExtensionLimit
      : latestClose1m < entryExtensionLimit);
  const strategyChartAnnotations: OrderChartPriceAnnotation[] = currentMarket
    ? ([
        ...(visibleReferencePrice ? [{
          id: "strategy-current-reference",
          role: "REFERENCE" as const,
          label: "当前参考价",
          detail: "当前环境实时盘口参考价；不是策略触发价。",
          price: Number(visibleReferencePrice),
          authority: "MARKET" as const,
          lineStyle: "dotted" as const,
          draggable: false,
        }] : []),
        {
          id: "strategy-channel-upper",
          role: "MARK_CONDITION",
          label: "做多突破线",
          detail: `${currentMarket.channel_lookback_15m} 根 15m 通道上沿；仍需闭合 1m 确认。`,
          price: Number(currentMarket.channel_upper),
          authority: "MARKET",
          lineStyle: "dashed",
          draggable: false,
        },
        {
          id: "strategy-channel-lower",
          role: "MARK_CONDITION",
          label: "做空突破线",
          detail: `${currentMarket.channel_lookback_15m} 根 15m 通道下沿；仍需闭合 1m 确认。`,
          price: Number(currentMarket.channel_lower),
          authority: "MARKET",
          lineStyle: "dashed",
          draggable: false,
        },
        ...(entryExtensionLimit !== null ? [{
          id: "strategy-entry-extension",
          role: "ENTRY_INVALIDATION" as const,
          label: parameters.direction === "LONG" ? "做多最大追价" : "做空最大追价",
          detail: `所选方向超过此价格后不追入；边界为通道触发价加减 ${parameters.max_entry_extension_atr} ATR。`,
          price: entryExtensionLimit,
          authority: "MARKET" as const,
          lineStyle: "dotted" as const,
          draggable: false,
        }] : []),
      ] satisfies OrderChartPriceAnnotation[])
        .filter((annotation) => Number.isFinite(annotation.price) && annotation.price > 0)
    : [];
  // Chart-bar readiness is presentation state. Direct-plan eligibility needs a
  // current server market context and a fresh same-environment execution quote;
  // the backend rebuilds the schedule again at review, fix, and activation.
  const directMarketDataReady = Boolean(
    currentMarket
    && !market.isError
    && !market.isFetching
    && expectedMarketSource
    && marketStream.status === "LIVE"
    && usableLiveQuote !== null,
  );
  const currentShockRule = orderSchedule.dynamic_rules.find(
    (rule) => rule.kind === "CANCEL_ON_SHOCK",
  );
  const entryBoundaryBreach = directExecution && directMarketDataReady
    ? currentEntryBoundaryBreach({
        direction: parameters.direction,
        referencePrice: liveReferencePrice,
        invalidationPrice: currentShockRule?.invalidation_price,
        opportunityMissedPrice: currentShockRule?.opportunity_missed_price,
      })
    : null;
  const entryBoundaryBreachMessage = entryBoundaryBreach
    ? entryBoundaryBreach.kind === "ENTRY_INVALIDATED"
      ? `当前标记价 ${tradingPrice(entryBoundaryBreach.currentPrice, strategyPriceTickSize)} USDT`
        + ` 已${parameters.direction === "LONG" ? "达到或跌破" : "达到或上破"}入场失效价`
        + ` ${tradingPrice(entryBoundaryBreach.boundaryPrice, strategyPriceTickSize)} USDT；`
        + "不能启动。请调整失效边界或重新评估计划。"
      : `当前标记价 ${tradingPrice(entryBoundaryBreach.currentPrice, strategyPriceTickSize)} USDT`
        + ` 已${parameters.direction === "LONG" ? "达到或上破" : "达到或跌破"}机会错过价`
        + ` ${tradingPrice(entryBoundaryBreach.boundaryPrice, strategyPriceTickSize)} USDT；`
        + "不能启动。请调整边界或重新评估计划。"
    : null;
  const planDraftPayload = (): PlanDraftPayload => {
    const commonPayload = {
      plan_name: normalizedPlanName,
      decision_context: normalizedDecisionContext,
      venue_ref: "BINANCE_USDM" as const,
      instrument_ref: instrument,
      direction: parameters.direction,
      target_exposure: directExecution ? directExecutionNotional : tradeAmount,
      // Keep the user-selected funding ceiling intact.  Protection may reduce
      // the submitted schedule's actual entry notional, but that reduction is
      // an execution result rather than a rewrite of the plan's upper limit.
      max_margin: tradeAmount,
      max_notional: tradeAmount,
      max_allowed_loss: directExecution
        ? directPlanLossLimit ?? directMaximumProjectedLoss ?? tradeAmount
        : tradeAmount,
      valid_minutes: Number(validMinutes),
    };
    return directExecution
      ? {
          ...commonPayload,
          decision_basis: {
            kind: "DIRECT_EXECUTION",
            decision_basis_ref: DIRECT_EXECUTION_REF,
            parameters: {},
          },
          order_schedule_spec: directScheduleForSubmission,
        }
      : {
          ...commonPayload,
          decision_basis: {
            kind: "STRATEGY_SIGNAL",
            decision_basis_ref: strategyId,
            parameters,
          },
        };
  };
  const activeDraft = newestDraft(
    reviewDraft,
    editing ? draft.data ?? null : null,
  );
  latestPersistedDraftRef.current = activeDraft;
  const activePlanId = activeDraft?.plan_id ?? null;
  const currentInputFingerprint = directExecution
    ? directReviewInputFingerprint({
        planName: normalizedPlanName,
        decisionContext: normalizedDecisionContext,
        instrument,
      direction: parameters.direction,
      requestedNotional: tradeAmount,
      validMinutes,
      orderSchedule: directScheduleForSubmission,
    })
    : reviewInputFingerprint(planDraftPayload());
  currentInputFingerprintRef.current = currentInputFingerprint;
  useEffect(() => {
    if (!activeDraft) return;
    const identity = `${activeDraft.plan_id}:${activeDraft.draft_version}`;
    if (initializedSavedDraftRef.current === identity) return;
    initializedSavedDraftRef.current = identity;
    const savedFingerprint = directExecution
      ? savedDirectReviewInputFingerprint(activeDraft)
      : reviewInputFingerprint(savedDraftInput(activeDraft));
    setLastSavedInputFingerprint(savedFingerprint);
    autoSaveAttemptFingerprintRef.current = savedFingerprint;
    setDraftSaveState({
      status: "SAVED",
      at: Date.parse(activeDraft.updated_at) || Date.now(),
    });
  }, [
    activeDraft?.draft_version,
    activeDraft?.plan_id,
    activeDraft?.updated_at,
    directExecution,
  ]);
  const canPersistDraft = !liveReadOnly
    && !aiReviewInProgress
    && planNameValid
    && planValidityValid
    && tradeAmountInputValid
    && (directExecution || Boolean(strategyId));
  const draftSave = useMutation({
    mutationFn: async ({
      payload,
      fingerprint,
    }: {
      payload: PlanDraftPayload;
      fingerprint: string;
    }) => {
      const existingDraft = latestPersistedDraftRef.current;
      if (existingDraft) {
        return {
          draft: await updatePlan(
            existingDraft.plan_id,
            existingDraft.draft_version,
            payload,
          ),
          fingerprint,
        };
      }
      const createPayload = {
        ...payload,
        creator_kind: creatorKind,
      } satisfies PlanCreatePayload;
      const createIdentity = persistentRequestIdentity(
        pendingCreateIdentityRef.current,
        createIdentityScope,
        stableInputFingerprint(createPayload),
      );
      pendingCreateIdentityRef.current = createIdentity;
      const saved = await createPlan(createPayload, createIdentity.idempotencyKey);
      pendingCreateIdentityRef.current = null;
      clearPersistentRequestIdentity(createIdentityScope);
      return { draft: saved, fingerprint };
    },
    onMutate: () => setDraftSaveState({ status: "SAVING" }),
    onSuccess: ({ draft: saved, fingerprint }) => {
      latestPersistedDraftRef.current = saved;
      setReviewDraft(saved);
      setLastSavedInputFingerprint(fingerprint);
      autoSaveAttemptFingerprintRef.current = fingerprint;
      setDraftSaveState({ status: "SAVED", at: Date.now() });
      if (!editing) {
        navigate(`/plans/${saved.plan_id}/edit`, { replace: true });
      }
    },
    onError: (error, attempt) => {
      if (!isUnknownMutationResult(error)) {
        pendingCreateIdentityRef.current = null;
        clearPersistentRequestIdentity(createIdentityScope);
      }
      const code = error instanceof ApiFailure ? error.code : "DRAFT_SAVE_FAILED";
      if (code === "PLAN_VERSION_CONFLICT" && editing) {
        // A completed autosave can reach the server before React renders its
        // returned version. Refresh the authoritative version, then allow the
        // normal fingerprint comparison to issue one safe follow-up save.
        autoSaveAttemptFingerprintRef.current = null;
        void draft.refetch();
      } else {
        autoSaveAttemptFingerprintRef.current = attempt.fingerprint;
      }
      setDraftSaveState({
        status: "FAILED",
        message: `草稿未保存（${code}）。`,
      });
    },
  });
  const saveCurrentDraft = useCallback(() => {
    if (!canPersistDraft || draftSave.isPending) return;
    draftSave.mutate({
      payload: planDraftPayload(),
      fingerprint: currentInputFingerprint,
    });
  }, [canPersistDraft, currentInputFingerprint, draftSave, planDraftPayload]);
  saveCurrentDraftRef.current = saveCurrentDraft;
  useEffect(() => {
    if (
      !canPersistDraft
      || draftSave.isPending
    ) return undefined;
    const timer = window.setInterval(() => {
      const fingerprint = currentInputFingerprintRef.current;
      if (
        lastSavedInputFingerprint === fingerprint
        || autoSaveAttemptFingerprintRef.current === fingerprint
      ) return;
      saveCurrentDraftRef.current();
    }, 1_500);
    return () => window.clearInterval(timer);
  }, [
    canPersistDraft,
    draftSave.isPending,
    lastSavedInputFingerprint,
  ]);
  const latestAiReview = useQuery({
    queryKey: ["plan-ai-review", activePlanId],
    queryFn: () => getLatestPlanAiReview(activePlanId ?? ""),
    enabled: Boolean(activePlanId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "QUEUED" || status === "RUNNING" ? 1_000 : false;
    },
  });
  useEffect(() => {
    const next = latestAiReview.data;
    if (!next) return;
    setAiReview((current) => (
      !current
      || current.review_id !== next.review_id
      || Date.parse(next.updated_at) >= Date.parse(current.updated_at)
        ? next
        : current
    ));
  }, [latestAiReview.data]);
  const handleAiReviewUpdate = useCallback((next: PlanAiReview) => {
    setAiReview((current) => (
      !current
      || current.review_id !== next.review_id
      || Date.parse(next.updated_at) >= Date.parse(current.updated_at)
        ? next
        : current
    ));
  }, []);
  const reviewBindsActiveDraft = Boolean(
    aiReview
    && activeDraft
    && aiReview.plan_id === activeDraft.plan_id
    && aiReview.draft_version === activeDraft.draft_version
    && aiReview.draft_content_digest === activeDraft.content_digest,
  );
  useEffect(() => {
    const deadline = Date.parse(aiReview?.approval_valid_until ?? "");
    if (!Number.isFinite(deadline)) return undefined;
    const remaining = deadline - Date.now();
    if (remaining <= 0) {
      setAiReviewClock(Date.now());
      return undefined;
    }
    const timer = window.setTimeout(
      () => setAiReviewClock(Date.now()),
      remaining + 20,
    );
    return () => window.clearTimeout(timer);
  }, [aiReview?.approval_valid_until]);
  const aiReviewApprovalExpired = Boolean(
    reviewBindsActiveDraft
    && aiReview?.status === "APPROVED"
    && aiReview?.decision === "APPROVE"
    && Number.isFinite(Date.parse(aiReview.approval_valid_until ?? ""))
    && Date.parse(aiReview.approval_valid_until ?? "") <= aiReviewClock,
  );
  const boundInputFingerprint = reviewedInputFingerprint
    ?? (reviewBindsActiveDraft && activeDraft
      ? (directExecution
        ? savedDirectReviewInputFingerprint(activeDraft)
        : reviewInputFingerprint(savedDraftInput(activeDraft)))
      : null);
  const aiReviewMatchesCurrentInput = reviewBindsActiveDraft
    && boundInputFingerprint === currentInputFingerprint
    && aiReview?.configuration.model === aiReviewConfiguration.model
    && aiReview?.configuration.reasoning_effort === aiReviewConfiguration.reasoning_effort
    && aiReview?.prompt_version === CURRENT_PLAN_AI_REVIEW_PROMPT_VERSION
    && !aiReviewApprovalExpired;
  const aiReviewStaleReason = !aiReview || aiReviewMatchesCurrentInput
    ? null
    : aiReviewApprovalExpired
      ? "MARKET_CONTEXT_EXPIRED" as const
    : aiReview.prompt_version !== CURRENT_PLAN_AI_REVIEW_PROMPT_VERSION
      ? "PROMPT_UPGRADED" as const
      : aiReview.configuration.model !== aiReviewConfiguration.model
        || aiReview.configuration.reasoning_effort !== aiReviewConfiguration.reasoning_effort
        ? "CONFIGURATION_CHANGED" as const
        : "PLAN_CHANGED" as const;
  const aiReviewApproved = aiReviewMatchesCurrentInput
    && aiReview?.status === "APPROVED"
    && aiReview?.decision === "APPROVE";
  const aiReviewIdentityScope = activePlanId
    ? `${environmentScope}:PLAN_AI_REVIEW:${activePlanId}`
    : null;
  useEffect(() => {
    if (
      !aiReviewIdentityScope
      || !aiReview
      || !["APPROVED", "REJECTED", "FAILED"].includes(aiReview.status)
    ) return;
    pendingAiReviewIdentityRef.current = null;
    clearPersistentRequestIdentity(aiReviewIdentityScope);
  }, [aiReview?.status, aiReviewIdentityScope]);
  const aiReviewRequest = useMutation({
    onMutate: () => setAiReviewStarting(true),
    mutationFn: async () => {
      const payload = planDraftPayload();
      const inputFingerprint = currentInputFingerprint;
      const existingDraft = latestPersistedDraftRef.current;
      let saved: PlanDraft;
      if (existingDraft && lastSavedInputFingerprint === inputFingerprint) {
        saved = existingDraft;
      } else if (existingDraft) {
        saved = await updatePlan(
          existingDraft.plan_id,
          existingDraft.draft_version,
          payload,
        );
      } else {
        const createPayload = {
          ...payload,
          creator_kind: creatorKind,
        } satisfies PlanCreatePayload;
        const createIdentity = persistentRequestIdentity(
          pendingCreateIdentityRef.current,
          createIdentityScope,
          stableInputFingerprint(createPayload),
        );
        pendingCreateIdentityRef.current = createIdentity;
        saved = await createPlan(
          createPayload,
          createIdentity.idempotencyKey,
        );
        pendingCreateIdentityRef.current = null;
        clearPersistentRequestIdentity(createIdentityScope);
      }
      latestPersistedDraftRef.current = saved;
      setReviewDraft(saved);
      setLastSavedInputFingerprint(inputFingerprint);
      autoSaveAttemptFingerprintRef.current = inputFingerprint;
      setAiReview(null);
      setReviewedInputFingerprint(inputFingerprint);
      const reviewScope = `${environmentScope}:PLAN_AI_REVIEW:${saved.plan_id}`;
      const reviewIdentity = persistentRequestIdentity(
        pendingAiReviewIdentityRef.current,
        reviewScope,
        stableInputFingerprint({
          plan_id: saved.plan_id,
          draft_version: saved.draft_version,
          draft_content_digest: saved.content_digest,
          configuration: aiReviewConfiguration,
        }),
      );
      pendingAiReviewIdentityRef.current = reviewIdentity;
      const review = await requestPlanAiReview(
        saved.plan_id,
        saved.draft_version,
        reviewIdentity.idempotencyKey,
        aiReviewConfiguration,
      );
      // The server refreshes the system-owned review evidence cutoff in the
      // same transaction that creates the review.  Reload its new exact draft
      // binding instead of leaving the editor on the pre-review version.
      const refreshed = await getPlan(saved.plan_id);
      return { review, saved: refreshed, inputFingerprint };
    },
    onSuccess: ({ review, saved, inputFingerprint }) => {
      setReviewDraft(saved);
      setReviewedInputFingerprint(inputFingerprint);
      setAiReview(review);
      void latestAiReview.refetch();
    },
    onSettled: () => setAiReviewStarting(false),
  });
  const submitAndStart = useMutation({
    onMutate: () => {
      setSubmitAndStartPhase("SUBMITTING");
    },
    mutationFn: async ({ planId, draftVersion }: SubmitAndStartAttempt) => {
      const submitScope = `${environmentScope}:PLAN_SUBMIT_AND_START:${planId}`;
      const submitFingerprint = JSON.stringify({
        planId,
        draftVersion,
      });
      const submitIdentity = persistentRequestIdentity(
        pendingSubmitAndStartIdentityRef.current,
        submitScope,
        submitFingerprint,
      );
      pendingSubmitAndStartIdentityRef.current = submitIdentity;
      const result = await submitAndStartPlan(
        planId,
        draftVersion,
        submitIdentity.idempotencyKey,
      );
      const activation = (
        typeof result.activation === "object"
        && result.activation !== null
        && !Array.isArray(result.activation)
      )
        ? result.activation as Record<string, unknown>
        : {};
      const activationId = String(activation.activation_id ?? "");
      if (!activationId) {
        throw new ApiFailure(500, "SUBMIT_AND_START_ACTIVATION_ID_INVALID");
      }
      return { activationId, submitScope };
    },
    onSuccess: ({ activationId, submitScope }) => {
      setSubmitAndStartPhase("IDLE");
      pendingSubmitAndStartIdentityRef.current = null;
      clearPersistentRequestIdentity(submitScope);
      navigate(`/activations/${activationId}`);
    },
    onError: (error) => {
      setSubmitAndStartPhase("IDLE");
      if (!isUnknownMutationResult(error) && activePlanId) {
        pendingSubmitAndStartIdentityRef.current = null;
        clearPersistentRequestIdentity(
          `${environmentScope}:PLAN_SUBMIT_AND_START:${activePlanId}`,
        );
      }
      if (
        error instanceof ApiFailure
        && newRiskDisciplineBlockerCodesFromFailureCode(error.code).length > 0
      ) {
        void accountOverview.refetch();
        void directFundingDiscipline.refetch();
      }
    },
  });

  const loading = (Boolean(loadedPlanId) && (draft.isPending || draft.isFetching))
    || (!directExecution && strategies.isPending);
  const loadFailed = (Boolean(loadedPlanId) && draft.isError)
    || (!directExecution && strategies.isError)
    || (!loading && !selectingStrategy && !directExecution && !selectedStrategy);
  const accountDiscipline = accountOverview.data?.new_risk_discipline;
  const accountDisciplineUnavailableMessage = liveReadOnly
    ? "账户级新增风险状态暂时不可读取。当前入口同时为只读公开行情模式，不能创建或保存计划；恢复可写后，提交并启动仍会按服务端当前事实复核。"
    : "账户级新增风险状态暂时不可读取。可以继续准备并保存草稿；提交并启动会按服务端当前事实复核。恢复完整账户快照后再核对。";
  const accountDisciplineBlockedConsequence = liveReadOnly
    ? "当前入口同时为只读公开行情模式，不能创建或保存计划；恢复可写后，提交并启动仍会以当前事实重新核对，不会为新计划预留或绕过容量。"
    : "可以继续准备并保存草稿；提交并启动仍会以当前事实重新核对，不会为新计划预留或绕过容量。";
  const accountDisciplineNotice = !editing && accountOverview.isError
    ? (
      <Alert severity="warning" variant="outlined" sx={{ mb: 2 }}>
        {accountDisciplineUnavailableMessage}
      </Alert>
    )
    : !editing && accountDiscipline && !accountDiscipline.new_risk_allowed
      ? (
        <Box sx={{ mb: 2 }}>
          <NewRiskDisciplineBlockNotice
            discipline={accountDiscipline}
            consequence={accountDisciplineBlockedConsequence}
          />
        </Box>
      )
      : null;
  const strategyTargetedDisciplineBlocked = !directExecution
    && strategyFundingDisciplineState?.new_risk_allowed === false
    && accountDiscipline?.new_risk_allowed !== false;
  const canSubmit = !loading
    && !liveReadOnly
    && !loadFailed
    && !draftSave.isPending
    && !submitAndStart.isPending
    && !marketContextRefreshing
    && (!directExecution || !market.isError)
    && !marketSourceMismatch
    && expectedMarketSource !== null
    && (!directExecution || directMarketDataReady)
    && configurationValid
    && planNameValid
    && decisionContextValid;
  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canSubmit || !aiReviewApproved || !activeDraft) return;
    submitAndStart.mutate({
      planId: activeDraft.plan_id,
      draftVersion: activeDraft.draft_version,
    });
  };
  const submitAndStartButtonLabel = submitAndStartPhase === "SUBMITTING"
    ? "正在复核并启动…"
    : "提交并启动";
  const submitAndStartErrorCode = submitAndStart.error instanceof ApiFailure
    ? submitAndStart.error.code
    : "结果未知";
  const submitAndStartDisciplineBlockers = submitAndStart.error instanceof ApiFailure
    ? newRiskDisciplineBlockerCodesFromFailureCode(submitAndStart.error.code)
    : [];
  const submitAndStartDisciplineRejected = submitAndStartDisciplineBlockers.length > 0;
  const submitAndStartGenericErrorMessage = isUnknownMutationResult(submitAndStart.error)
    ? "提交并启动结果尚未确认；请保留当前草稿并用同一提交操作核对，不要修改后另行提交。"
    : submitAndStartErrorCode === "PLAN_AI_REVIEW_EXPIRED"
      ? "AI 审核的市场依据已过期（非 AI 拒绝）；请按当前草稿重新提交 AI 审核。"
    : `未能提交并启动（${submitAndStartErrorCode}）；草稿未变更，可修正后重新提交。`;
  const orderSettings = (
    <Box
      component="section"
      aria-labelledby="order-settings-title"
      sx={directExecution ? { ...surfaceFrameSx, p: { xs: 1.75, sm: 2 } } : undefined}
    >
      <Typography
        id="order-settings-title"
        variant={directExecution ? "h3" : "h2"}
        sx={{ mb: directExecution ? 1.5 : 2 }}
      >
        下单设置
      </Typography>
      <Box sx={{
        display: "grid",
        gridTemplateColumns: { xs: "1fr", sm: "repeat(2,minmax(0,1fr))" },
        gap: 2,
      }}>
        <TextField
          select
          label="方向"
          value={parameters.direction}
          onChange={(event) => update("direction", event.target.value as Direction)}
          sx={{ "& .MuiSelect-select": { color: parameters.direction === "LONG" ? "var(--halpha-market-up)" : "var(--halpha-market-down)", fontWeight: 750 } }}
        >
          <MenuItem value="LONG" className={marketToneClassName("up")}>做多</MenuItem>
          <MenuItem value="SHORT" className={marketToneClassName("down")}>做空</MenuItem>
        </TextField>
        <TextField
          label="交易对象"
          value={instrument}
          required
          helperText={directExecution
            ? "交易对象由计划入口固定；如需更换，请返回持仓或新建流程。"
            : "当前唯一策略对象固定为 BTCUSDT-PERP"}
          slotProps={{ htmlInput: { readOnly: true } }}
        />
        <TextField
          label="交易金额（USDT）"
          value={tradeAmount}
          onChange={(event) => setTradeAmount(event.target.value)}
          error={!tradeAmountValid}
          required
          helperText={!tradeAmountInputValid
            ? "必须填写大于 0 的金额"
            : strategyFundingCapExceeded
              ? `超过当前交易纪律允许的 ${quoteCurrencyAmount(strategyFundingMaximum ?? "0")} USDT。`
              : !directExecution && strategyFundingMaximum !== null
                ? `当前交易纪律允许最多 ${quoteCurrencyAmount(strategyFundingMaximum)} USDT；策略计划以此金额形成最大预计损失边界。`
                : "该金额就是本计划的资金边界，启动时无需再次授权"}
        />
        <TextField label="计划有效分钟" type="number" value={validMinutes} onChange={(event) => setValidMinutes(event.target.value)} error={!planValidityValid} helperText="范围 15–10080 分钟" slotProps={{ htmlInput: { min: 15, max: 10080, step: 1 } }} required />
      </Box>
      {strategyTargetedDisciplineBlocked ? (
        <Box sx={{ mt: 2 }}>
          <NewRiskDisciplineBlockNotice
            discipline={strategyFundingDisciplineState}
            title="当前策略方向的新增风险纪律未通过"
            consequence="可以继续准备草稿；提交并启动和实际新增风险动作都会重新核对，页面不能覆盖该限制。"
          />
        </Box>
      ) : null}
    </Box>
  );
  const decisionContextFields = (
    <Box sx={{ pt: 1.25, borderTop: 1, borderColor: "divider" }}>
      <Typography component="h3" variant="subtitle2">交易判断</Typography>
      <Stack spacing={1.25} sx={{ mt: 1.25 }}>
        <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", sm: "repeat(2,minmax(0,1fr))" }, gap: 1.25 }}>
          <TextField
            select
            size="small"
            label="本次目的"
            value={decisionContext.intent}
            onChange={(event) => setDecisionContext((current) => ({
              ...current,
              intent: event.target.value as PlanDecisionIntent,
            }))}
            error={!decisionContext.intent
              || (decisionContext.intent === "PROFIT_SEEKING" && !profitSeekingIntentAllowed)
              || (decisionContext.intent === "VALIDATION" && !validationIntentAllowed)}
            helperText={directExecution
              ? undefined
              : decisionContext.intent === "PROFIT_SEEKING" && !profitSeekingIntentAllowed
              ? "当前策略未取得盈利导向资格；请选择机制验证。服务端也会拒绝以该目的提交并启动"
              : decisionContext.intent === "VALIDATION" && !validationIntentAllowed
                ? "当前策略未取得验证用途资格；不能保存或激活这个目的"
              : decisionContext.intent === "VALIDATION"
              ? "验证软件或执行闭环；实际盈亏保留，但不进入策略收益样本"
              : decisionContext.intent === "PROFIT_SEEKING"
                ? "按固定交易假设获取市场收益；盈利与亏损都进入后续评价"
              : "必须明确是盈利导向还是机制验证"}
            required
          >
            <MenuItem value="" disabled>请选择本次目的</MenuItem>
            <MenuItem value="PROFIT_SEEKING" disabled={!profitSeekingIntentAllowed}>
              盈利导向交易{profitSeekingIntentAllowed ? "" : "（当前策略未获资格）"}
            </MenuItem>
            <MenuItem value="VALIDATION" disabled={!validationIntentAllowed}>
              机制 / 软件验证{validationIntentAllowed ? "" : "（当前策略未获资格）"}
            </MenuItem>
          </TextField>
          <TextField
            select
            size="small"
            label="交易形态"
            value={decisionContext.setup_family}
            onChange={(event) => setDecisionContext((current) => ({
              ...current,
              setup_family: event.target.value as PlanSetupFamily,
            }))}
            error={!decisionContext.setup_family}
            required
          >
            <MenuItem value="" disabled>请选择交易形态</MenuItem>
            {(Object.entries(setupFamilyLabels) as Array<[PlanSetupFamily, string]>).map(([value, label]) => (
              <MenuItem key={value} value={value}>{label}</MenuItem>
            ))}
          </TextField>
        </Box>
        <TextField
          size="small"
          multiline
          minRows={directExecution ? 3 : 4}
          maxRows={8}
          label="交易理由"
          value={decisionContext.rationale}
          onChange={(event) => setDecisionContext((current) => ({
            ...current,
            rationale: event.target.value,
          }))}
          error={!decisionContext.rationale.trim() || decisionContext.rationale.trim().length > 2000}
          helperText={directExecution
            ? undefined
            : decisionContext.rationale.trim()
              ? `${decisionContext.rationale.length}/2000`
              : "必填"}
          slotProps={{ htmlInput: { maxLength: 2000 } }}
          required
        />
      </Stack>
    </Box>
  );
  const decisionEvidencePanel = !directExecution
    && decisionContext.intent
    && decisionContext.setup_family
    ? <DecisionEvidencePanel
        evidence={decisionEvidence.data}
        pending={decisionEvidence.isPending || decisionEvidence.isFetching}
        failed={decisionEvidence.isError}
        qualificationExportAvailable={status.environment_kind === "DEMO"}
        qualificationExportPending={qualificationExport.isPending}
        qualificationExportTarget={qualificationExport.variables ?? null}
        qualificationExportError={qualificationExport.isError
          ? qualificationExportError(qualificationExport.error)
          : null}
        qualificationExportSuccess={qualificationExport.isSuccess
          ? {
              target: qualificationExport.variables,
              digest: qualificationExport.data.artifact_content_digest,
            }
          : null}
        onExportQualification={(target) => qualificationExport.mutate(target)}
      />
    : null;

  if (selectingStrategy) {
    return <StrategySelection
      strategies={strategies.data ?? []}
      loading={strategies.isPending}
      failed={strategies.isError}
      readOnly={liveReadOnly}
      disciplineNotice={accountDisciplineNotice}
      onSelect={selectStrategy}
      onSelectDirect={selectDirectExecution}
      onCancel={() => navigate("/plans")}
    />;
  }

  if (directExecution) {
    const directBlockingReason = liveReadOnly
      ? "当前实盘入口为只读公开行情模式，不能保存或修改计划。"
      : loadFailed
      ? "草稿或直接执行依据当前不可用。"
      : !planNameValid
        ? "填写有效计划名称。"
        : !decisionContextValid
          ? "选择交易目的和形态，并填写一段完整的交易理由。"
        : planRewardRiskBlocked
          ? `价格退出的计划加权收益 / 风险必须达到 ${quoteAmount(applicableMinimumRewardRisk)}R；时间退出不能替代该新增风险纪律。`
          : !tradeAmountInputValid
            ? "计划资金上限必须大于 0。"
          : directFundingCapExceeded
            ? `资金上限超过当前纪律可用的 ${quoteCurrencyAmount(directFundingMaximum ?? "0")} USDT；已设上限不会被自动修改，请主动调整后再提交。`
          : !planValidityValid
            ? "填写 15–10080 分钟的有效期。"
            : market.isError
              ? "行情刷新失败；刷新成功并取得当前事实后才能保存。"
            : marketContextRefreshing
              ? "正在刷新当前行情并重新生成预览。"
              : !directMarketDataReady
                ? "等待当前环境的历史 K 线与实时来源完成核对。"
              : !orderScheduleReady
                  ? "修正输入并等待当前版本的服务端预览通过。"
                  : null;
    const directSubmitAndStartErrorMessage = submitAndStartDisciplineRejected
      ? "当前账户纪律拒绝提交并启动；草稿仍可修改。请在恢复条件满足后重新提交。"
      : submitAndStartErrorCode === "PLAN_AI_REVIEW_EXPIRED"
        ? "AI 审核的市场依据已过期（非 AI 拒绝）；请按当前草稿重新提交 AI 审核。"
      : submitAndStartErrorCode === "PLAN_REWARD_RISK_UNAVAILABLE"
        ? "当前计划没有可计算的价格退出收益 / 风险；请返回退出步骤配置价格止盈并重新审核。"
        : submitAndStartErrorCode === "PLAN_REWARD_RISK_BELOW_DISCIPLINE_MINIMUM"
          ? `当前计划收益 / 风险低于纪律最低 ${quoteAmount(applicableMinimumRewardRisk)}R；请调整价格退出后重新审核。`
      : submitAndStartErrorCode === "ATTRIBUTION_AMBIGUOUS"
        ? `${instrument} 已有运行中的计划；草稿仍可修改。请先处理现有计划后重新提交。`
        : submitAndStartGenericErrorMessage;
    const directAiReviewRequestError = aiReviewRequestErrorMessage(
      aiReviewRequest.error,
      aiReviewRequest.isError,
    );
    const directAiReviewBlockedReasons = Array.from(new Set([
      ...directScheduleProblems,
      directBlockingReason,
      !planNameValid ? "填写有效的计划名称。" : null,
      !decisionContextValid ? "选择交易目的、形态并填写交易理由。" : null,
      planRewardRiskBlocked
        ? `价格退出的计划加权收益 / 风险须达到 ${quoteAmount(applicableMinimumRewardRisk)}R。`
        : null,
      !tradeAmountInputValid ? "资金上限必须大于 0。" : null,
      directFundingCapExceeded
        ? `资金上限超过当前纪律可用的 ${quoteCurrencyAmount(directFundingMaximum ?? "0")} USDT。`
        : null,
      directFundingDisciplineIssue,
      directFundingCapacityExhausted ? "当前纪律名义敞口没有剩余容量。" : null,
      !planValidityValid ? "填写 15–10080 分钟的有效期。" : null,
      !directMarketDataReady ? "等待当前行情与交易所规则就绪。" : null,
      !orderScheduleReady ? "修正订单计划并等待服务端预览通过。" : null,
      draftSave.isPending ? "正在保存草稿。" : null,
    ].filter((reason): reason is string => Boolean(reason))));
    const directAiReviewPanel = (
      <PlanAiReviewPanel
        review={aiReview}
        reviewMatchesDraft={aiReviewMatchesCurrentInput}
        staleReason={aiReviewStaleReason}
        requesting={aiReviewRequest.isPending}
        requestError={directAiReviewRequestError}
        requestBlockedReasons={directAiReviewBlockedReasons}
        configuration={aiReviewConfiguration}
        onConfigurationChange={setAiReviewConfiguration}
        onRequest={() => aiReviewRequest.mutate()}
        onReviewUpdate={handleAiReviewUpdate}
      />
    );
    const directOpenRiskRemaining = directFundingDisciplineState?.open_risk_limit !== null
      && directFundingDisciplineState?.open_risk_limit !== undefined
      && directFundingDisciplineState?.open_risk_committed !== null
      && directFundingDisciplineState?.open_risk_committed !== undefined
      ? subtractDecimal(
        directFundingDisciplineState.open_risk_limit,
        directFundingDisciplineState.open_risk_committed,
      )
      : null;
    const directExposurePair = (used: string | null | undefined, limit: string | null | undefined) => (
      used !== null
      && used !== undefined
      && limit !== null
      && limit !== undefined
        ? `${quoteCurrencyAmount(used)} / ${quoteCurrencyAmount(limit)} USDT`
        : "待账户快照"
    );
    const directExposureRows = directFundingDisciplineState
      ? [
        ["总名义敞口", directExposurePair(
          directFundingDisciplineState.gross_exposure,
          directFundingDisciplineState.gross_exposure_limit,
        )],
        [`${instrument} 敞口`, directExposurePair(
          directFundingDisciplineState.instrument_exposure,
          directFundingDisciplineState.instrument_exposure_limit,
        )],
        [`${directFundingDisciplineState.correlation_cluster ?? "相关簇"}敞口`, directExposurePair(
          directFundingDisciplineState.correlated_exposure,
          directFundingDisciplineState.correlated_exposure_limit,
        )],
      ] as const
      : [["名义敞口", "待账户快照"]] as const;
    const directTakeProfitLevels = orderSchedule.protection_policy.take_profit_ladder?.levels ?? [];
    const directRiskRewardFact = directTakeProfitLevels.length > 0
      ? `价格止盈 ${directTakeProfitLevels.map((level, index) => (
        `TP${index + 1} ${quoteAmount(level.trigger_r)}R`
      )).join(" · ")}`
      : orderSchedule.protection_policy.time_exit_seconds !== null
        ? "未设价格止盈（已设时间退出）"
        : "未设自动退出";
    const directDisciplineReviewSummary = (
      <Box
        component="section"
        data-testid="direct-discipline-review-summary"
        sx={{
          borderTop: 1,
          borderColor: directFundingCapExceeded
            || directFundingCapacityExhausted
            || planRewardRiskBlocked
            ? "error.main"
            : "divider",
          pt: 1.25,
        }}
      >
        <Typography component="h3" variant="caption" color="text.secondary" sx={{ display: "block", mb: .75 }}>
          交易纪律
        </Typography>
        <Box
          component="dl"
          sx={{
            m: 0,
            display: "grid",
            borderTop: 1,
            borderColor: "divider",
          }}
        >
          {[
            [
              "计划资金",
              `${quoteCurrencyAmount(tradeAmount || "0")} / 可用 ${directFundingMaximum === null ? "待账户快照" : `${quoteCurrencyAmount(directFundingMaximum)} USDT`}`,
              directFundingCapExceeded,
            ],
            [
              "最大预计亏损",
              directMaximumProjectedLoss
                ? <FinancialToneText tone={financialToneForSignedValue(-1)}>{`-${quoteCurrencyAmount(directMaximumProjectedLoss)} USDT`}</FinancialToneText>
                : "等待预览",
              false,
            ],
            [
              "单计划限额",
              directPlanLossLimit
                ? <FinancialToneText tone={financialToneForSignedValue(-1)}>{`-${quoteCurrencyAmount(directPlanLossLimit)} USDT`}</FinancialToneText>
                : "待账户快照",
              false,
            ],
            [
              "收益 / 风险",
              `${directRiskRewardFact} · 计划加权 ${directRewardRiskRatio === null ? "不可计算" : `${quoteAmount(directRewardRiskRatio)}R`} / 最低 ${quoteAmount(applicableMinimumRewardRisk)}R`,
              planRewardRiskBlocked,
            ],
            [
              "组合剩余风险",
              directOpenRiskRemaining === null ? "待账户快照" : `${quoteCurrencyAmount(directOpenRiskRemaining)} USDT`,
              directFundingCapacityExhausted,
            ],
            ...directExposureRows.map(([label, display]) => [label, display, false] as const),
          ].map(([label, display, blocked]) => (
            <Box
              key={String(label)}
              sx={{
                display: "grid",
                gridTemplateColumns: "104px minmax(0, 1fr)",
                columnGap: 1,
                alignItems: "baseline",
                py: .8,
                borderBottom: 1,
                borderColor: "divider",
              }}
            >
              <Typography component="dt" variant="caption" color="text.secondary" sx={{ m: 0 }}>
                {label}
              </Typography>
              <Typography
                component="dd"
                variant="body2"
                color={blocked ? "error.main" : "text.primary"}
                sx={{ minWidth: 0, m: 0, fontWeight: 650, lineHeight: 1.45 }}
              >
                {display}
              </Typography>
            </Box>
          ))}
        </Box>
      </Box>
    );
    const directConfigGroupSx = {
      borderTop: 1,
      borderColor: "divider",
      pt: 1.1,
    } as const;
    const directConfigGroupTitleSx = {
      borderLeft: 3,
      borderColor: "primary.main",
      pl: 1,
      fontWeight: 850,
    } as const;
    const directPlanMetadata = (
      <Box
        component="section"
        aria-labelledby="direct-plan-metadata-title"
        sx={directConfigGroupSx}
      >
        <Box sx={{ pb: .85 }}>
          <Typography id="direct-plan-metadata-title" component="h2" variant="body2" sx={directConfigGroupTitleSx}>
            计划基础信息
          </Typography>
        </Box>
        <Stack spacing={1.1}>
          <TextField
            size="small"
            label="计划名称"
            value={planName}
            onChange={(event) => setPlanName(event.target.value)}
            error={planName.length > 0 && !planNameValid}
            helperText={planNameValid ? undefined : "必填，最多 80 个字符"}
            slotProps={{ htmlInput: { maxLength: 80 } }}
            required
          />
          <Box sx={{ display: "grid", gridTemplateColumns: "repeat(2,minmax(0,1fr))", gap: 1 }}>
            <TextField
              size="small"
              label="计划有效分钟"
              type="number"
              value={validMinutes}
              onChange={(event) => setValidMinutes(event.target.value)}
              error={!planValidityValid}
              helperText="范围 15–10080 分钟"
              slotProps={{ htmlInput: { min: 15, max: 10080, step: 1 } }}
              required
            />
            {!editing ? (
              <TextField
                select
                size="small"
                label="创建方式"
                value={creatorKind}
                onChange={(event) => setCreatorKind(event.target.value as PlanCreatorKind)}
              >
                <MenuItem value="HUMAN">人工创建</MenuItem>
                <MenuItem value="AI">AI 创建</MenuItem>
              </TextField>
            ) : (
              <TextField
                size="small"
                label="创建来源"
                value={draft.data?.content.creator_kind === "AI" ? "AI 创建" : draft.data?.content.creator_kind === "HUMAN" ? "人工创建" : draft.data?.content.creator_kind === "MONITOR" ? "Monitor 创建" : "未知"}
                slotProps={{ htmlInput: { readOnly: true } }}
              />
            )}
          </Box>
        </Stack>
      </Box>
    );
    const planOptions = (
      <Box
        component="section"
        aria-label="交易判断核对"
        sx={{ px: 1.5, py: 1.25 }}
      >
        {decisionContextFields}
      </Box>
    );
    const workspaceHeader = (
      <Stack direction="row" spacing={1} sx={{ alignItems: "center", width: "100%", minWidth: 0 }}>
        <Stack direction="row" spacing={.25} sx={{ alignItems: "center", flex: "0 0 auto" }}>
          <Typography component="h1" variant="subtitle1" sx={{ fontWeight: 800 }}>
            {addPositionRequested ? "独立追加开仓" : "直接执行"}
          </Typography>
          <Tooltip
            arrow
            title={addPositionRequested
              ? "追加开仓是独立的新风险计划，不修改外部持仓的来源和既有盈亏。"
              : "直接执行不选择策略；保存草稿不会启动，提交并启动会创建运行快照并交给执行器。"}
          >
            <IconButton size="small" aria-label={addPositionRequested ? "了解独立追加开仓" : "了解直接执行与保存草稿"}>
              <InfoOutlined sx={{ fontSize: 16 }} />
            </IconButton>
          </Tooltip>
        </Stack>
        <Chip size="small" variant="outlined" label={instrument} />
        <Chip
          size="small"
          variant="outlined"
          label={status.environment_kind}
          sx={{ display: { xs: "none", sm: "inline-flex" } }}
        />
        {liveReadOnly && <Chip size="small" color="warning" variant="outlined" label="只读" />}
        <Tooltip
          arrow
          title={visibleSourceCutoff
            ? `公开行情截止 ${formatUserVisibleTime(visibleSourceCutoff)}；实时展示不替代保存与启动时的服务端核验。`
            : "公开实时行情尚未形成；规划仍使用服务端快照。"}
        >
          <Chip
            size="small"
            color={marketStreamStatusColor(marketStream.status)}
            variant="outlined"
            label={marketStreamStatusText(marketStream.status)}
            sx={marketStream.status === "LIVE" ? {
              color: "#166534",
              borderColor: "#86B69F",
              bgcolor: "#F0FDF4",
            } : undefined}
          />
        </Tooltip>
        <Stack
          direction="row"
          spacing={{ sm: 1.5, md: 2.25 }}
          sx={{ ml: "auto", alignItems: "center", minWidth: 0, overflow: "hidden" }}
        >
          <Box sx={{ display: { xs: "none", sm: "block" }, minWidth: 0 }}>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>中间价</Typography>
            <Typography className="mono" variant="body2" noWrap sx={{ fontWeight: 750 }}>
              {visibleReferencePrice ? marketPrice(visibleReferencePrice) : "未知"}
            </Typography>
          </Box>
          <Box sx={{ display: { xs: "none", xl: "block" }, minWidth: 0 }}>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>买一 / 卖一</Typography>
            <Typography className="mono" variant="body2" noWrap sx={{ fontWeight: 700 }}>
              {visibleBidPrice && visibleAskPrice
                ? `${marketPrice(visibleBidPrice)} / ${marketPrice(visibleAskPrice)}`
                : "未知"}
            </Typography>
          </Box>
          <Box sx={{ display: { xs: "none", xl: "block" }, minWidth: 0 }}>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>价差</Typography>
            <Typography className="mono" variant="body2" noWrap sx={{ fontWeight: 700 }}>
              {visibleSpread ? `${marketPrice(visibleSpread)} USDT` : "未知"}
            </Typography>
          </Box>
          <Box sx={{ display: { xs: "none", xl: "block" }, minWidth: 0 }}>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>行情截止</Typography>
            <Typography variant="caption" noWrap>
              {visibleSourceCutoff ? formatUserVisibleTime(visibleSourceCutoff) : "未知"}
            </Typography>
          </Box>
        </Stack>
        <Tooltip title="刷新规划快照与订单预览" arrow>
          <span>
            <IconButton
              size="small"
              aria-label="刷新规划快照与订单预览"
              onClick={() => market.refetch()}
              disabled={!channelLookbackValid || market.isFetching}
            >
              <RefreshOutlined fontSize="small" />
            </IconButton>
          </span>
        </Tooltip>
      </Stack>
    );
    const quickControls = (
      <Box sx={{ px: 1.5, py: 1.25, borderBottom: 1, borderColor: "divider" }}>
        {addPositionRequested ? (
          <Alert severity="warning" variant="outlined" sx={{ mb: 1.25 }}>
            这是独立的新风险计划，不会把 {instrument} 的外部持仓基线改写成 Halpha 入场。
            {sourcePositionSnapshotCutoff
              ? ` 来源持仓快照截止 ${formatUserVisibleTime(sourcePositionSnapshotCutoff)}。`
              : ""}
            激活前仍须完成现有持仓、未结委托和账户模式核对。
          </Alert>
        ) : null}
        {loading ? <LinearProgress aria-label="正在读取直接执行计划" sx={{ mb: 1.25 }} /> : null}
        {loadFailed ? <Alert severity="error" sx={{ mb: 1.25 }}>草稿或直接执行依据当前不可用，不能编辑。</Alert> : null}
        {market.isError ? (
          <Alert severity={currentMarket ? "warning" : "error"} variant="outlined" sx={{ mb: 1.25 }}>
            {currentMarket
              ? `行情刷新失败；保留截至 ${formatUserVisibleTime(currentMarket.source_cutoff)} 的上次事实，需刷新成功后再保存。`
              : "当前行情不可用；参考价格和服务端预览不能据此视为安全。"}
          </Alert>
        ) : null}
        {marketSourceMismatch ? (
          <Alert severity="error" variant="outlined" sx={{ mb: 1.25 }}>
            行情来源与当前 {status.environment_kind} 环境不一致，已拒绝显示和预览；请核对运行配置后刷新。
          </Alert>
        ) : null}
        <Stack spacing={0}>
          {directPlanMetadata}
          <Box component="section" aria-labelledby="direct-direction-title" sx={directConfigGroupSx}>
            <Box sx={{ pb: .85 }}>
              <Typography id="direct-direction-title" component="h2" variant="body2" sx={directConfigGroupTitleSx}>
                方向
              </Typography>
            </Box>
            <Box>
          <ToggleButtonGroup
            exclusive
            fullWidth
            size="small"
            value={parameters.direction}
            aria-label="交易方向"
            onChange={(_event, next: Direction | null) => {
              if (!next || next === parameters.direction) return;
              setOrderScheduleReady(false);
              setOrderSchedule((current) => retargetGeneratedEventCondition(
                current,
                parameters.direction,
                next,
              ));
              update("direction", next);
            }}
            sx={{
              "& .MuiToggleButton-root": {
                minHeight: 40,
                py: .5,
                fontWeight: 800,
                borderColor: "divider",
                bgcolor: "background.paper",
              },
              "& .MuiToggleButton-root[value=\"LONG\"]": {
                color: "var(--halpha-market-up)",
              },
              "& .MuiToggleButton-root[value=\"SHORT\"]": {
                color: "var(--halpha-market-down)",
              },
              "& .MuiToggleButton-root.Mui-selected, & .MuiToggleButton-root.Mui-selected:hover": {
                bgcolor: "background.paper",
                fontWeight: 850,
                borderWidth: 2,
                boxShadow: "none",
              },
              "& .MuiToggleButton-root[value=\"LONG\"].Mui-selected": {
                color: "var(--halpha-market-up)",
                borderColor: "var(--halpha-market-up) !important",
              },
              "& .MuiToggleButton-root[value=\"SHORT\"].Mui-selected": {
                color: "var(--halpha-market-down)",
                borderColor: "var(--halpha-market-down) !important",
              },
            }}
          >
            <ToggleButton value="LONG" className={marketToneClassName(marketToneForDirection("LONG"))}>
              做多
            </ToggleButton>
            <ToggleButton value="SHORT" className={marketToneClassName(marketToneForDirection("SHORT"))}>
              做空
            </ToggleButton>
          </ToggleButtonGroup>
            </Box>
          </Box>
          <Box
            aria-label="资金上限配置"
            sx={{
              ...directConfigGroupSx,
              borderColor: directFundingCapExceeded || directFundingCapacityExhausted
                ? "warning.main"
                : "divider",
            }}
          >
            <Stack direction="row" spacing={1} sx={{ pb: .85, alignItems: "baseline", justifyContent: "space-between" }}>
              <Typography component="h2" variant="body2" sx={directConfigGroupTitleSx}>资金上限</Typography>
              <Typography
                variant="caption"
                className="mono"
                color={directFundingMaximumNumber !== null && directFundingMaximumNumber <= 0 ? "warning.main" : "text.secondary"}
                sx={{ flexShrink: 0, fontWeight: 750, whiteSpace: "nowrap" }}
              >
                {directFundingDiscipline.isError
                  ? "纪律上限不可读取"
                  : directFundingDisciplineBlocked
                    ? "纪律上限不可用"
                    : directFundingMaximum === null
                      ? "纪律上限待账户快照"
                      : `当前可用 ${quoteCurrencyAmount(directFundingMaximum)} USDT`}
              </Typography>
            </Stack>
            <Box sx={{ pt: .4 }}>
            <Slider
              aria-label="按当前纪律比例快速设定资金上限"
              value={directFundingSliderValue}
              min={0}
              max={100}
              step={1}
              marks={[
                { value: 25, label: "25%" },
                { value: 50, label: "50%" },
                { value: 75, label: "75%" },
                { value: 100, label: "100%" },
              ]}
              disabled={directFundingMaximumNumber === null || directFundingMaximumNumber <= 0}
              onChange={(_event, next) => {
                if (typeof next !== "number" || directFundingMaximum === null) return;
                updateDirectTradeAmount(
                  fundingAmountAtPercentage(directFundingMaximum, next),
                );
              }}
              sx={{ mt: .4, mb: 2.5 }}
            />
            <TextField
              fullWidth
              size="small"
              label="资金上限（USDT）"
              value={tradeAmount}
              onChange={(event) => updateDirectTradeAmount(event.target.value)}
              onBlur={() => {
                const amount = Number(tradeAmount);
                if (Number.isFinite(amount) && amount >= 0) {
                  updateDirectTradeAmount(amount.toFixed(2));
                }
              }}
              error={!tradeAmountValid}
              helperText={!tradeAmountInputValid
                ? "请输入大于 0 的金额。"
                : directFundingCapExceeded
                ? `纪律上限：${quoteCurrencyAmount(directFundingMaximum ?? "0")} USDT。`
                : directFundingCapacityExhausted
                  ? "当前名义敞口没有剩余容量。"
                  : undefined}
              slotProps={{ htmlInput: { inputMode: "decimal" } }}
              required
            />
            </Box>
          </Box>
        </Stack>
      </Box>
    );
    const directSubmissionProblems = Array.from(new Set([
      !activeDraft ? "等待当前草稿保存完成。" : null,
      !aiReviewApproved ? "完成当前草稿的 AI 审核并获得批准。" : null,
      directBlockingReason,
      directFundingDisciplineIssue,
      directFundingCapacityExhausted ? "当前纪律名义敞口没有剩余容量。" : null,
      !directMarketDataReady ? "等待当前行情与交易所规则就绪。" : null,
    ].filter((problem): problem is string => Boolean(problem))));
    const directSubmitDisabled = !canSubmit
      || !aiReviewApproved
      || directFundingDisciplineIssue !== null;
    const directSubmissionTooltip = directSubmissionProblems.length > 0 ? (
      <Box component="ul" sx={{ m: 0, pl: 2, py: .25, maxWidth: 360 }}>
        {directSubmissionProblems.map((problem) => <li key={problem}>{problem}</li>)}
      </Box>
    ) : "复核当前草稿并立即启动。";
    const draftSaveLabel = draftSaveState.status === "SAVING"
      ? "正在保存草稿"
      : draftSaveState.status === "SAVED"
        ? `草稿已保存 ${formatUserVisibleTime(new Date(draftSaveState.at).toISOString())}`
        : draftSaveState.status === "FAILED"
          ? draftSaveState.message
          : "草稿未保存";
    const footerControls = (
      <Stack spacing={.8}>
        <Stack direction="row" spacing={1}>
          <Tooltip
            title={canPersistDraft ? "保存当前草稿" : "填写计划名称、有效期和资金上限后可保存。"}
            arrow
          >
            <Box sx={{ flex: "0 0 auto" }}>
              <Button
                type="button"
                variant="outlined"
                disabled={!canPersistDraft || draftSave.isPending}
                onClick={saveCurrentDraft}
              >
                保存草稿
              </Button>
            </Box>
          </Tooltip>
          <Tooltip title={directSubmissionTooltip} arrow disableHoverListener={!directSubmitDisabled}>
            <Box sx={{ flex: 1 }}>
              <Button
                type="submit"
                variant="contained"
                color="warning"
                fullWidth
                disabled={directSubmitDisabled}
              >
                <Box component="span" sx={{ display: "inline-flex", alignItems: "center", gap: .5 }}>
                  {submitAndStartButtonLabel}
                  {directSubmitDisabled ? <ErrorOutlined color="error" fontSize="small" aria-label="提交并启动条件未满足" /> : null}
                </Box>
              </Button>
            </Box>
          </Tooltip>
        </Stack>
        <Typography
          variant="caption"
          color={draftSaveState.status === "FAILED" ? "error.main" : "text.secondary"}
          aria-live="polite"
        >
          {draftSaveLabel}
        </Typography>
        {submitAndStart.isError ? (
          <>
            <Alert
              severity="error"
              variant="outlined"
          >
              {directSubmitAndStartErrorMessage}
            </Alert>
          </>
        ) : null}
      </Stack>
    );

    return (
      <Box
        component="form"
        onSubmit={handleSubmit}
        data-testid="direct-execution-workspace"
        sx={{
          width: "100%",
          height: { xs: "auto", md: "calc(100dvh - 65px)" },
          minHeight: { xs: "calc(100dvh - 96px)", md: 620 },
          overflow: { xs: "visible", md: "hidden" },
        }}
      >
        <OrderScheduleEditor
          value={orderSchedule}
          onChange={(next) => {
            setOrderScheduleReady(false);
            setDirectMaximumProjectedLoss(null);
            setDirectEffectiveNotional(null);
            setDirectRequestedNotional(null);
            setOrderSchedule(next);
          }}
          environmentId={status.environment_id}
          environmentKind={status.environment_kind}
          instrumentRef={instrument}
          direction={parameters.direction}
          maxNotional={tradeAmount}
          maxPlanLoss={directPlanLossLimit}
          referencePrice={stableReferencePrice}
          liveReferencePrice={liveReferencePrice}
          bidPrice={visibleBidPrice}
          askPrice={visibleAskPrice}
          marketContext={(stopReferenceInterval === "15m"
            ? market.isError || market.isFetching
            : stopReferenceMarket.isError || stopReferenceMarket.isFetching)
            ? null
            : selectedStopReferenceMarket ?? null}
          stopReferenceInterval={stopReferenceInterval}
          onStopReferenceIntervalChange={setStopReferenceInterval}
          stopReferenceLoading={stopReferenceInterval === "15m"
            ? market.isFetching
            : stopReferenceMarket.isFetching}
          stopReferenceUnavailable={stopReferenceInterval === "15m"
            ? market.isError
            : stopReferenceMarket.isError}
          feeEvidence={executionFeeEvidence.data ?? null}
          feeEvidenceLoading={executionFeeEvidence.isPending}
          feeEvidenceUnavailable={executionFeeEvidence.isError}
          funding={usableFunding}
          fundingHistory={usableFundingHistory}
          chartInterval={chartInterval}
          onChartIntervalChange={handleChartIntervalChange}
          liveBar={marketStream.liveBar}
          streamStatus={marketStream.status}
          streamGeneration={marketStream.generation}
          marketProjectionReady={directMarketDataReady}
          marketColorScheme={marketColorScheme}
          scheduleRef={activeDraft?.plan_id ?? loadedPlanId ?? "new-direct-order-plan"}
          workspaceHeader={workspaceHeader}
          leadingControls={quickControls}
          planOptions={planOptions}
          reviewDisciplineSummary={directDisciplineReviewSummary}
          aiReviewPanel={directAiReviewPanel}
          footerControls={footerControls}
          decisionReviewReady={decisionContextValid && !planRewardRiskBlocked}
          aiReviewApproved={aiReviewApproved}
          milestoneProblems={[
            [
              entryBoundaryBreachMessage,
              !planNameValid ? "填写有效的计划名称。" : null,
              !tradeAmountInputValid ? "资金上限必须大于 0。" : null,
              directFundingCapExceeded ? `资金上限超过当前纪律可用 ${quoteCurrencyAmount(directFundingMaximum ?? "0")} USDT。` : null,
            ].filter((problem): problem is string => Boolean(problem)),
            [],
            [planRewardRiskBlocked ? `计划加权收益 / 风险必须达到 ${quoteAmount(applicableMinimumRewardRisk)}R。` : null].filter((problem): problem is string => Boolean(problem)),
            [!decisionContextValid ? "选择交易目的、形态并填写交易理由。" : null].filter((problem): problem is string => Boolean(problem)),
            [!aiReviewApproved ? "完成并获得当前计划的 AI 审核批准。" : null].filter((problem): problem is string => Boolean(problem)),
          ]}
          onValidationChange={handleOrderScheduleValidation}
          onSubmissionProblemsChange={setDirectScheduleProblems}
          onMaximumProjectedLossChange={handleMaximumProjectedLoss}
          onEffectiveNotionalChange={handleEffectiveNotional}
          onRequestedNotionalChange={handleRequestedNotional}
        />
      </Box>
    );
  }

  const strategySubmitDisabled = !canSubmit || !aiReviewApproved;
  const strategySubmissionProblems = Array.from(new Set([
    !activeDraft ? "等待当前草稿保存完成。" : null,
    !aiReviewApproved ? "完成当前草稿的 AI 审核并获得批准。" : null,
    !planNameValid ? "填写有效的计划名称。" : null,
    !planValidityValid ? "填写 15–10080 分钟的有效期。" : null,
    !decisionContextValid ? "选择交易目的、形态并填写交易理由。" : null,
    planRewardRiskBlocked
      ? `计划加权收益 / 风险须达到 ${quoteAmount(applicableMinimumRewardRisk)}R。`
      : null,
    strategyTargetedDisciplineBlocked ? "当前交易纪律阻止新增风险。" : null,
    !canSubmit ? "等待当前策略配置与行情核对完成。" : null,
  ].filter((problem): problem is string => Boolean(problem))));
  const strategySubmissionTooltip = strategySubmissionProblems.length > 0
    ? (
      <Box component="ul" sx={{ m: 0, pl: 2, py: .25, maxWidth: 360 }}>
        {strategySubmissionProblems.map((problem) => <li key={problem}>{problem}</li>)}
      </Box>
    )
    : "复核当前草稿并立即启动。";

  return (
    <Box
      component="form"
      onSubmit={handleSubmit}
      data-testid="strategy-configuration-workspace"
      sx={{
        width: "100%",
        height: { xs: "auto", md: "calc(100dvh - 65px)" },
        minHeight: { xs: "calc(100dvh - 96px)", md: 620 },
        overflow: { xs: "visible", md: "hidden" },
        display: "grid",
        gridTemplateColumns: { xs: "minmax(0, 1fr)", md: "minmax(0, 1fr) minmax(360px, 430px)" },
      }}
    >
      <Box sx={{ minWidth: 0, minHeight: 0, p: { xs: 1.25, sm: 2, md: 1.5 }, pr: { md: 0.75 } }}>
        <OrderScheduleChart
          workspaceMode
          chartPurpose="STRATEGY_INPUT"
          environmentId={status.environment_id}
          environmentKind={status.environment_kind}
          instrumentRef={instrument}
          direction={parameters.direction}
          marketColorScheme={marketColorScheme}
          interval={chartInterval}
          onIntervalChange={handleChartIntervalChange}
          liveBar={marketStream.liveBar}
          streamStatus={marketStream.status}
          streamGeneration={marketStream.generation}
          priceProjectionReady={false}
          priceTickSize={strategyPriceTickSize}
          referencePrice={null}
          spec={orderSchedule}
          previewLegs={[]}
          previewState="BLOCKED"
          additionalPriceAnnotations={strategyChartAnnotations}
          onRangeChange={ignoreStrategyChartRangeChange}
          onSingleLimitPriceChange={ignoreStrategyChartPriceChange}
        />
      </Box>
      <Box
        data-testid="strategy-configuration-scroll"
        sx={{
          minWidth: 0,
          minHeight: 0,
          overflowY: { xs: "visible", md: "auto" },
          overscrollBehavior: "contain",
          borderLeft: { md: 1 },
          borderColor: { md: "divider" },
          px: { xs: 2, sm: 3, md: 2 },
          py: { xs: 2.5, md: 2 },
        }}
      >
      <PageHeader
        eyebrow={copying
          ? "沿用计划参数 · 新草稿"
          : editing
            ? `可编辑草稿${draft.data ? ` · v${draft.data.draft_version}` : ""}`
            : "新建交易计划"}
        title={editing
          ? "编辑策略计划"
          : copying
            ? "沿用参数新建计划"
            : "配置策略计划"}
        description={copying
          ? "原计划的方向、交易金额和策略参数已带入；修改后需要重新审核并提交并启动。"
          : "配置方向、交易金额和策略参数。"}
      />
      {liveReadOnly && (
        <Alert severity="info" variant="outlined" sx={{ mb: 2 }}>
          当前实盘入口为只读公开行情模式；不能创建、修改或提交并启动计划。
        </Alert>
      )}
      {accountDisciplineNotice}
      {loading && <LinearProgress aria-label={editing ? "正在读取草稿" : "正在读取策略"} />}
      {loadFailed && <Alert severity="error">{editing ? "草稿或执行依据当前不可用，不能编辑。" : "执行依据当前不可用。"}</Alert>}
      <Box component="section" aria-labelledby="plan-identity-title" sx={{ ...surfaceFrameSx, mb: 3, p: 2 }}>
        <Typography id="plan-identity-title" variant="h2" sx={{ mb: 2 }}>计划信息</Typography>
        <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", sm: "repeat(2,minmax(0,1fr))" }, gap: 2 }}>
          <TextField
            label="计划名称"
            value={planName}
            onChange={(event) => setPlanName(event.target.value)}
            error={planName.length > 0 && !planNameValid}
            helperText={planNameValid ? "用于区分不同交易计划" : "必填，最多 80 个字符"}
            slotProps={{ htmlInput: { maxLength: 80 } }}
            required
          />
          {!editing ? (
            <TextField
              select
              label="创建方式"
              value={creatorKind}
              onChange={(event) => setCreatorKind(event.target.value as PlanCreatorKind)}
              helperText="AI 代为创建时必须主动选择“AI 创建”"
            >
              <MenuItem value="HUMAN">人工创建</MenuItem>
              <MenuItem value="AI">AI 创建</MenuItem>
            </TextField>
          ) : (
            <TextField
              label="创建来源"
              value={draft.data?.content.creator_kind === "AI" ? "AI 创建" : draft.data?.content.creator_kind === "HUMAN" ? "人工创建" : draft.data?.content.creator_kind === "MONITOR" ? "Monitor 创建" : "未知"}
              helperText={draft.data?.content.created_at ? `创建于 ${formatUserVisibleTime(draft.data.content.created_at)}` : "创建时间未知"}
              slotProps={{ htmlInput: { readOnly: true } }}
            />
          )}
        </Box>
        <Box sx={{ mt: 2 }}>{decisionContextFields}</Box>
        {decisionEvidencePanel}
      </Box>

      {selectedStrategy ? (
        <Box sx={{ ...surfaceFrameSx, mb: 3, p: 2 }}>
          <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} sx={{ justifyContent: "space-between", alignItems: { xs: "stretch", sm: "flex-start" } }}>
            <Box>
              <Typography variant="caption" color="text.secondary">已选策略</Typography>
              <Typography variant="h2" sx={{ mt: .25 }}>{selectedStrategy.display_name}</Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: .5, overflowWrap: "anywhere" }}>
                {selectedStrategy.strategy_id} · v{selectedStrategy.strategy_version}
              </Typography>
            </Box>
            {!editing && !copying && <Button variant="outlined" onClick={() => setCreationStep("strategy")}>重新选择策略</Button>}
          </Stack>
          {typeof selectedStrategy.economic_scope.evidence_limit === "string" && (
            <Alert severity="warning" variant="outlined" sx={{ mt: 1.5 }}>
              <strong>
                {selectedStrategy.economic_scope.profitability_evidence === "NO_POSITIVE_EXPECTANCY_EVIDENCE"
                  ? "收益证据未支持："
                  : "证据边界："}
              </strong>
              {selectedStrategy.economic_scope.evidence_limit}
            </Alert>
          )}
          <Box component="details" sx={{ mt: 1.5 }}>
            <Box component="summary" sx={{ cursor: "pointer", fontWeight: 700 }}>查看策略介绍</Box>
            <StrategyIntroduction strategy={selectedStrategy} embedded showEvidenceLimit={false} />
          </Box>
        </Box>
      ) : null}

      <Box
        component="section"
        aria-labelledby="market-context-title"
        sx={{
          ...surfaceFrameSx,
          mb: 4,
          p: { xs: 2, sm: 2.5 },
        }}
      >
        <Stack direction={{ xs: "column", sm: "row" }} spacing={1.25} sx={{ justifyContent: "space-between", alignItems: { xs: "stretch", sm: "center" }, mb: 2 }}>
          <Box>
            <Typography id="market-context-title" variant="h2">当前策略输入</Typography>
            <Typography color="text.secondary" variant="body2" sx={{ mt: .75 }}>
              Binance 当前环境公开行情；仅辅助选择方向，不承诺盈利。
            </Typography>
          </Box>
          <Button variant="outlined" onClick={() => market.refetch()} disabled={!channelLookbackValid || market.isFetching}>{market.isFetching ? "正在刷新…" : "刷新行情"}</Button>
        </Stack>
        {!channelLookbackValid && <Alert severity="warning" variant="outlined">通道回看必须是 4–96 根 15m K 线；修正参数后才读取对应行情。</Alert>}
        {channelLookbackValid && market.isPending && <LinearProgress aria-label="正在读取当前公开行情" />}
        {channelLookbackValid && market.isError && currentMarket && <Alert severity="warning" variant="outlined">
          行情刷新失败；以下保留上次成功行情（截止 {formatUserVisibleTime(currentMarket.source_cutoff)}），可能已经过期，仅用于定位。请刷新成功后再据此选择方向。
        </Alert>}
        {channelLookbackValid && market.isError && !currentMarket && <Alert severity="warning" variant="outlined">当前行情不可用，方向判断缺少产品内依据。可以稍后刷新，不应把空值视为安全或无波动。</Alert>}
        {channelLookbackValid && marketSourceMismatch && <Alert severity="error" variant="outlined">
          返回行情不属于当前 {status.environment_kind} 环境，已拒绝显示；不同环境数据不会用于方向判断、价格预览或下单。
        </Alert>}
        {currentMarket && <>
          <FactGrid columns={3} dense facts={[
            ["盘口中间价", `${strategyPrice(currentMarket.reference_price)} USDT`],
            ["买一 / 卖一", `${strategyPrice(currentMarket.bid_price)} / ${strategyPrice(currentMarket.ask_price)}`],
            ["买卖价差", `${strategyPrice(currentSpread)} USDT`],
            ["当前资金费率", usableFunding
              ? `${formatFundingRatePercent(usableFunding.funding_rate)}${formatFundingSettlementInterval(usableFundingHistory?.average_interval_seconds ?? null) ? ` / ${formatFundingSettlementInterval(usableFundingHistory?.average_interval_seconds ?? null)}` : ""}`
              : "实时数据不可用"],
            ["下次资金结算", usableFunding
              ? formatUserVisibleTime(usableFunding.next_funding_at)
              : "未知"],
            ["最近闭合 1m", `${strategyPrice(currentMarket.latest_close_1m)} USDT`],
            ["最近闭合 1m 成交量 / 笔数", `${marketVolume(currentMarket.latest_volume_1m)} BTC / ${currentMarket.latest_trade_count_1m} 笔`],
            ["最近闭合 15m", `${strategyPrice(currentMarket.latest_close_15m)} USDT`],
            ["通道回看", `${currentMarket.channel_lookback_15m} × 15m`],
            ["通道上沿", `${strategyPrice(currentMarket.channel_upper)} USDT`],
            ["通道下沿", `${strategyPrice(currentMarket.channel_lower)} USDT`],
            ...(entryExtensionLimit !== null ? [["最大追价边界", `${strategyPrice(entryExtensionLimit)} USDT`]] : []),
            ["1m 收盘距上沿 / 下沿", `${gapPercent(closedBarBreakoutGapPercent("LONG", currentMarket.latest_close_1m, currentMarket.channel_upper))} / ${gapPercent(closedBarBreakoutGapPercent("SHORT", currentMarket.latest_close_1m, currentMarket.channel_lower))}`],
            ["盘口中间价距上沿 / 下沿", `${gapPercent(currentMarket.long_breakout_gap_pct)} / ${gapPercent(currentMarket.short_breakout_gap_pct)}`],
            ["ATR(14)", `${strategyPrice(currentMarket.atr_14)} USDT`],
          ].map(([label = "", value = ""]) => ({ label: label as string, value }))} />
          <Alert severity="info" variant="outlined" sx={{ mt: 2 }}>
            当前选择 <MarketToneText tone={marketToneForDirection(parameters.direction)}>{parameters.direction === "LONG" ? "做多" : "做空"}</MarketToneText>：1m 收盘距离{parameters.direction === "LONG" ? "通道上沿" : "通道下沿"} {gapPercent(selectedClosedBarBreakoutGap)}（策略触发口径）；
            盘口中间价距离 {gapPercent(selectedBreakoutGap ?? "")}。正值表示尚未突破，负值表示已经越过；入场仍需连续 {parameters.confirmation_bars_1m} 根 1m 收盘确认，并通过标记价格与买卖一形成的执行前保守价格检查。行情截止 {formatUserVisibleTime(currentMarket.source_cutoff)}。
          </Alert>
          {latestClosedBarBeyondBoundary && latestClosedBarBeyondExtension && entryExtensionLimit !== null && (
            <Alert severity="warning" sx={{ mt: 2 }}>
              最近闭合 1m 已突破，但超过最大追价边界 {strategyPrice(entryExtensionLimit)} USDT；按当前参数不应追入。启动后策略只会等待价格回到允许范围并重新通过闭合 K 线与执行前检查。
            </Alert>
          )}
          {Number.isFinite(currentSpreadBps) && currentSpreadBps > 10 && (
            <Alert severity="warning" sx={{ mt: 2 }}>
              当前买卖价差约 {currentSpreadBps.toFixed(1)} bps，超过策略提交上限 10 bps。系统不会在该盘口创建入场动作，会等待后续有效闭合 1m 再判断。
            </Alert>
          )}
        </>}
      </Box>

      {orderSettings}

      <Alert
        severity={planRewardRiskBlocked ? "error" : "info"}
        variant="outlined"
        sx={{ mt: 2 }}
        data-testid="strategy-reward-risk-discipline"
      >
        策略计划加权收益 / 风险 {strategyRewardRiskRatio === null ? "不可计算" : `${quoteAmount(strategyRewardRiskRatio)}R`}
        {` / 当前纪律最低 ${quoteAmount(applicableMinimumRewardRisk)}R`}。
        {planRewardRiskBlocked ? " 当前参数不能提交 AI 审核或启动。" : " 提交并启动时服务端会按同一阈值复核。"}
      </Alert>

      {status.environment_kind === "DEMO" && <Box sx={{ ...surfaceFrameSx, mt: 3, p: 2, borderColor: parameters.demo_immediate_entry ? "warning.main" : "divider" }}>
        <FormControlLabel
          control={<Checkbox checked={parameters.demo_immediate_entry} onChange={(event) => {
            update("demo_immediate_entry", event.target.checked);
            if (event.target.checked) {
              setDecisionContext((current) => ({ ...current, intent: "VALIDATION" }));
            }
          }} />}
          label="下单流程验证"
        />
        <Typography color="text.secondary" variant="body2">
          开启后，同一策略在下一根有效闭合 1m 上执行一次入场，用于验证下单、成交、保护和退出链路；它不是突破信号。
        </Typography>
      </Box>}

      <Box component="details" sx={{ ...surfaceFrameSx, mt: 4, p: 2 }}>
        <Box component="summary" sx={{ cursor: "pointer", fontWeight: 750 }}>高级策略参数（可保持默认）</Box>
        <Typography color="text.secondary" sx={{ mt: 1, mb: 2 }}>只有需要调整入场、止损和止盈逻辑时再修改。</Typography>
        <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", sm: "repeat(2,minmax(0,1fr))" }, gap: 2 }}>
          <TextField label="15m 通道回看" type="number" value={parameters.channel_lookback_15m} onChange={(event) => update("channel_lookback_15m", Number(event.target.value))} error={!channelLookbackValid} helperText="范围 4–96 根；越短触发越频繁，噪声也越多" slotProps={{ htmlInput: { min: 4, max: 96, step: 1 } }} required />
          <TextField label="1m 确认根数" type="number" value={parameters.confirmation_bars_1m} onChange={(event) => update("confirmation_bars_1m", Number(event.target.value))} error={!confirmationBarsValid} helperText="范围 1–3 根" slotProps={{ htmlInput: { min: 1, max: 3, step: 1 } }} required />
          <TextField label="入场有效分钟" type="number" value={parameters.entry_valid_minutes} onChange={(event) => update("entry_valid_minutes", Number(event.target.value))} error={!entryValidityValid} helperText="范围 15–10080 分钟" slotProps={{ htmlInput: { min: 15, max: 10080, step: 1 } }} required />
          <TextField label="初始止损 ATR 倍数" type="number" value={parameters.initial_stop_atr_multiple} onChange={(event) => update("initial_stop_atr_multiple", event.target.value)} error={!initialStopValid} helperText="范围 1–3 ATR" slotProps={{ htmlInput: { min: 1, max: 3, step: "any" } }} required />
          <TextField label="最大追价 ATR" type="number" value={parameters.max_entry_extension_atr} onChange={(event) => update("max_entry_extension_atr", event.target.value)} error={!maxExtensionValid} helperText="范围 0.1–1 ATR" slotProps={{ htmlInput: { min: .1, max: 1, step: "any" } }} required />
          <TextField label="最大持仓 15m 根数" type="number" value={parameters.max_hold_bars_15m} onChange={(event) => update("max_hold_bars_15m", Number(event.target.value))} error={!maxHoldingBarsValid} helperText="范围 4–672 根" slotProps={{ htmlInput: { min: 4, max: 672, step: 1 } }} required />
          <TextField label="止盈一仓位比例" type="number" value={parameters.take_profit_1_fraction} onChange={(event) => update("take_profit_1_fraction", event.target.value)} error={!takeProfitFractionValid} helperText="范围 0.25–0.75" slotProps={{ htmlInput: { min: .25, max: .75, step: "any" } }} required />
          <TextField label="止盈一 R 倍数" type="number" value={parameters.take_profit_1_r} onChange={(event) => update("take_profit_1_r", event.target.value)} error={!takeProfit1Valid} helperText="范围 1–3R" slotProps={{ htmlInput: { min: 1, max: 3, step: "any" } }} required />
          <TextField label="止盈二 R 倍数" type="number" value={parameters.take_profit_2_r} onChange={(event) => update("take_profit_2_r", event.target.value)} error={!takeProfit2Valid || !takeProfitOrderValid} helperText="范围 2–6R，且必须大于止盈一" slotProps={{ htmlInput: { min: 2, max: 6, step: "any" } }} required />
        </Box>
      </Box>
      <Box sx={{ ...surfaceFrameSx, mt: 3 }}>
        <PlanAiReviewPanel
          review={aiReview}
          reviewMatchesDraft={aiReviewMatchesCurrentInput}
          staleReason={aiReviewStaleReason}
          requesting={aiReviewRequest.isPending}
          requestError={aiReviewRequestErrorMessage(
            aiReviewRequest.error,
            aiReviewRequest.isError,
          )}
          requestBlockedReasons={canSubmit
            ? []
            : ["请先完成计划信息、策略参数、交易理由与当前行情核对。"]}
          configuration={aiReviewConfiguration}
          onConfigurationChange={setAiReviewConfiguration}
          onRequest={() => aiReviewRequest.mutate()}
          onReviewUpdate={handleAiReviewUpdate}
        />
      </Box>
      <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} sx={{ mt: 3 }}>
        <Button
          type="button"
          variant="outlined"
          disabled={!canPersistDraft || draftSave.isPending}
          onClick={saveCurrentDraft}
        >
          {draftSave.isPending ? "正在保存草稿" : "保存草稿"}
        </Button>
        <Tooltip title={strategySubmissionTooltip} arrow disableHoverListener={!strategySubmitDisabled}>
          <Box sx={{ flex: 1 }}>
            <Button type="submit" variant="contained" fullWidth disabled={strategySubmitDisabled}>
              <Box component="span" sx={{ display: "inline-flex", alignItems: "center", gap: .5 }}>
                {submitAndStartButtonLabel}
                {strategySubmitDisabled ? <ErrorOutlined color="error" fontSize="small" aria-label="提交并启动条件未满足" /> : null}
              </Box>
            </Button>
          </Box>
        </Tooltip>
        <Button variant="outlined" onClick={() => navigate("/plans")}>取消</Button>
      </Stack>
      <Typography
        variant="caption"
        color={draftSaveState.status === "FAILED" ? "error.main" : "text.secondary"}
        aria-live="polite"
        sx={{ display: "block", mt: 1 }}
      >
        {draftSaveState.status === "SAVING"
          ? "正在保存草稿"
          : draftSaveState.status === "SAVED"
            ? `草稿已保存 ${formatUserVisibleTime(new Date(draftSaveState.at).toISOString())}`
            : draftSaveState.status === "FAILED"
              ? draftSaveState.message
              : "草稿未保存"}
      </Typography>
      {submitAndStart.isError ? (
        <Alert
          severity="error"
          variant="outlined"
          sx={{ mt: 1 }}
        >
          {submitAndStartGenericErrorMessage}
        </Alert>
      ) : null}
      </Box>
    </Box>
  );
}
