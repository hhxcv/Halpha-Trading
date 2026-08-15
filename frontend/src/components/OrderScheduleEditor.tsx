import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  FormControlLabel,
  IconButton,
  LinearProgress,
  MenuItem,
  Stack,
  Switch,
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
import ErrorOutlined from "@mui/icons-material/ErrorOutlined";
import InfoOutlined from "@mui/icons-material/InfoOutlined";
import { useQuery } from "@tanstack/react-query";

import {
  previewOrderSchedule,
  type ExecutionFeeEvidence,
  type MarketFundingRateHistory,
  type MarketContext,
  type MarketInterval,
  type OrderScheduleCondition,
  type OrderScheduleDirection,
  type OrderScheduleEntryProgram,
  type OrderSchedulePreview,
  type OrderSchedulePreviewIssue,
  type OrderSchedulePriceMatch,
  type OrderScheduleSpec,
} from "../api/client";
import {
  compactDecimal,
  formatUserVisibleTime,
  quoteCurrencyAmount,
  quoteCurrencyEstimate,
  roundedTradingPriceEstimate,
  scaleDecimalByPowerOfTen,
  shortDigest,
  tradingPrice,
  tradingQuantity,
} from "../format";
import {
  financialToneClassName,
  financialToneForSignedValue,
  type MarketColorScheme,
} from "../marketColors";
import type {
  MarketStreamBar,
  MarketStreamClientStatus,
  MarketStreamFunding,
} from "../marketStream";
import { surfaceFrameSx } from "../theme";
import OrderScheduleChart from "./OrderScheduleChart";
import { buildInitialStopRecommendations } from "./orderScheduleStopRecommendations";
import {
  entrySignalQualityWarning,
  takeProfitAfterCostEstimate,
  takeProfitSpreadCoverageWarning,
} from "./orderScheduleDecisionAid";
import {
  localOrderScheduleProblems,
  milestoneConfigurationReady,
  scheduleServerProblems,
  serverScheduleWasAssessed,
  stageHasServerProblem,
} from "./orderScheduleMilestones";
import {
  approximatelyEqual,
  conditionByKind,
  dynamicRuleByKind,
  evenlyDividedNotional,
  finiteNumber,
  generatedOffsetPrice,
  isoFromLocalDateTime,
  isPositive,
  localDateTimeValue,
  normalizedDirectConditionItems,
  previewFailureText,
  replaceAt,
  resized,
  resolvedEntryProgram,
  withCondition,
  withDynamicRule,
  withoutCondition,
  withoutDynamicRule,
  withoutGeneratedEventCondition,
} from "./orderScheduleEditorModel";
import { lossLimitedDirectEntry } from "../directLossLimit";
import {
  estimateFundingCost,
  formatFundingRatePercent,
  formatFundingSettlementInterval,
} from "../fundingEstimate";

export { createDefaultOrderScheduleSpec } from "./orderScheduleEditorModel";

export type OrderScheduleEditorProps = {
  value: OrderScheduleSpec;
  onChange: (value: OrderScheduleSpec) => void;
  environmentId: string;
  environmentKind: string;
  instrumentRef: string;
  direction: OrderScheduleDirection;
  maxNotional: string;
  maxPlanLoss?: string | null;
  referencePrice: string | null;
  liveReferencePrice?: string | null;
  bidPrice?: string | null;
  askPrice?: string | null;
  marketContext?: MarketContext | null;
  stopReferenceInterval: MarketInterval;
  onStopReferenceIntervalChange: (interval: MarketInterval) => void;
  stopReferenceLoading?: boolean;
  stopReferenceUnavailable?: boolean;
  feeEvidence?: ExecutionFeeEvidence | null;
  feeEvidenceLoading?: boolean;
  feeEvidenceUnavailable?: boolean;
  funding?: MarketStreamFunding | null;
  fundingHistory?: MarketFundingRateHistory | null;
  chartInterval: MarketInterval;
  onChartIntervalChange: (interval: MarketInterval) => void;
  liveBar: MarketStreamBar | null;
  streamStatus: MarketStreamClientStatus;
  streamGeneration: number;
  marketProjectionReady: boolean;
  marketColorScheme: MarketColorScheme;
  scheduleRef: string;
  workspaceHeader?: ReactNode;
  leadingControls?: ReactNode;
  planOptions?: ReactNode;
  reviewDisciplineSummary?: ReactNode;
  aiReviewPanel?: ReactNode;
  footerControls?: ReactNode;
  decisionReviewReady?: boolean;
  aiReviewApproved?: boolean;
  milestoneProblems?: ReadonlyArray<ReadonlyArray<string>>;
  onValidationChange?: (ready: boolean) => void;
  onSubmissionProblemsChange?: (problems: string[]) => void;
  onMaximumProjectedLossChange?: (value: string | null) => void;
  onEffectiveNotionalChange?: (value: string | null) => void;
  onRequestedNotionalChange?: (value: string | null) => void;
  onMarketReadinessChange?: (ready: boolean) => void;
};

type EditorMilestone = 0 | 1 | 2 | 3 | 4;

const MAX_SAFE_STOP_DISTANCE_BPS = 10_000;

const fieldGridSx = {
  display: "grid",
  gridTemplateColumns: { xs: "1fr", sm: "repeat(2, minmax(0, 1fr))" },
  gap: 1.5,
  minWidth: 0,
  "& > *": { minWidth: 0 },
} as const;

const compactFieldGridSx = {
  display: "grid",
  gridTemplateColumns: { xs: "1fr", sm: "repeat(2, minmax(0, 1fr))" },
  gap: 1.25,
  minWidth: 0,
  "& > *": { minWidth: 0 },
} as const;

const selectedChoiceSx = {
  color: "var(--halpha-selection-text)",
  fontWeight: 850,
  borderColor: "transparent !important",
  backgroundColor: "var(--halpha-selection) !important",
  boxShadow: "none",
} as const;

const outlinedChoiceSx = {
  "& .MuiToggleButton-root": {
    minHeight: 36,
    px: 1.25,
    py: .5,
    color: "text.primary",
    textTransform: "none",
    fontWeight: 800,
    borderColor: "divider",
    bgcolor: "background.paper",
  },
  "& .MuiToggleButton-root.Mui-selected": selectedChoiceSx,
  "& .MuiToggleButton-root.Mui-selected:hover": {
    backgroundColor: "var(--halpha-selection) !important",
  },
} as const;

const configuredRuleSx = {
  mt: 1.25,
  pt: 1.25,
  borderTop: 1,
  borderColor: "divider",
} as const;

const POST_ONLY_RETRY_MAX_ATTEMPTS = 5;

const priceMatchOptions: ReadonlyArray<{
  value: OrderSchedulePriceMatch;
  label: string;
}> = [
  { value: "OPPONENT", label: "对手价" },
  { value: "OPPONENT_5", label: "对手价 5 档" },
  { value: "OPPONENT_10", label: "对手价 10 档" },
  { value: "OPPONENT_20", label: "对手价 20 档" },
  { value: "QUEUE", label: "同向队列价" },
  { value: "QUEUE_5", label: "同向队列 5 档" },
  { value: "QUEUE_10", label: "同向队列 10 档" },
  { value: "QUEUE_20", label: "同向队列 20 档" },
];

const issueLabels: Record<string, string> = {
  DIRECT_EXECUTION_AUTOMATIC_EXIT_REQUIRED: "必须配置至少一种自动退出方式。",
  DIRECT_EXECUTION_ENTRY_PROGRAM_REQUIRED: "请选择入场方式。",
  DIRECT_EXECUTION_PROTECTION_REQUIRED: "必须配置初始保护。",
  DIRECT_EXECUTION_TAKE_PROFIT_FRACTION_TOTAL_INVALID: "分段止盈比例合计必须为 100%。",
  DIRECT_EXECUTION_TAKE_PROFIT_LEVEL_COUNT_INVALID: "分段止盈最多支持 4 档。",
  DIRECT_EXECUTION_TIME_SLICED_EXPIRY_UNSUPPORTED: "时间分批入场不能同时使用订单等待到期。",
  DIRECT_EXECUTION_TIME_SLICED_TIF_UNSUPPORTED: "时间分批限价单必须使用 IOC 或 FOK。",
  GTD_EXPIRY_TOO_SOON: "GTD 到期时间距离本次校验时刻不足 10 分钟。",
  ORDER_SCHEDULE_PRICE_COLLISION: "按价格步进标准化后出现重合档位。",
  ORDER_SCHEDULE_REFERENCE_PRICE_REQUIRED: "市价单或 priceMatch 需要当前参考价格。",
  ORDER_SCHEDULE_TOTAL_EXCEEDS_PLAN_LIMIT: "请求总额超过计划交易金额。",
  ORDER_SCHEDULE_PRICE_OUTSIDE_VENUE_LIMIT: "档位价格超出交易所允许范围。",
  PROTECTION_INSIDE_ENTRY_RANGE: "止损必须位于全部计划入场档位之外。",
  ORDER_SCHEDULE_QUANTITY_BELOW_MINIMUM: "标准化数量低于交易所最小数量。",
  ORDER_SCHEDULE_QUANTITY_ABOVE_MAXIMUM: "标准化数量超过交易所最大数量。",
  ORDER_SCHEDULE_NOTIONAL_BELOW_MINIMUM: "标准化后名义金额低于交易所最小金额。",
};

function previewIssueText(
  issue: OrderSchedulePreviewIssue,
  preview: OrderSchedulePreview,
): string {
  const prefix = issue.leg_index === null ? "" : `档位 ${issue.leg_index + 1}：`;
  const leg = issue.leg_index === null
    ? null
    : preview.normalized_legs.find((candidate) => candidate.leg_index === issue.leg_index) ?? null;
  if (issue.code === "ORDER_SCHEDULE_NOTIONAL_BELOW_MINIMUM" && leg) {
    return `${prefix}标准化后有效金额 ${quoteCurrencyAmount(leg.effective_notional)} USDT，低于交易所最低 ${quoteCurrencyAmount(preview.instrument_rules.min_notional)} USDT；请提高该档金额。`;
  }
  return `${prefix}${issueLabels[issue.code] ?? issue.code}`;
}

function EditorSection({
  id,
  title,
  description,
  collapsible = false,
  summary,
  children,
}: {
  id: string;
  title: string;
  description?: string;
  collapsible?: boolean;
  summary?: ReactNode;
  children: ReactNode;
}) {
  if (collapsible) {
    return (
      <Box
        component="details"
        sx={{
          borderBottom: 1,
          borderColor: "divider",
          "&[open] > summary": { bgcolor: "action.hover" },
        }}
      >
        <Box
          component="summary"
          sx={{
            minHeight: 44,
            px: 1.5,
            py: 1.1,
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 1,
            listStyle: "none",
            "&::-webkit-details-marker": { display: "none" },
            "&::after": {
              content: '"⌄"',
              color: "text.secondary",
              fontSize: 18,
              lineHeight: 1,
              transition: "transform 120ms ease",
            },
            "details[open] > &::after": { transform: "rotate(180deg)" },
          }}
        >
          <Box sx={{ minWidth: 0, borderLeft: 3, borderColor: "primary.main", pl: 1 }}>
            <Typography id={id} component="h2" variant="body2" sx={{ fontWeight: 850 }}>{title}</Typography>
            {summary ? (
              <Typography
                component="div"
                variant="caption"
                color="text.secondary"
                noWrap
                sx={{ mt: .15 }}
              >
                {summary}
              </Typography>
            ) : null}
          </Box>
        </Box>
        <Box component="section" aria-labelledby={id} sx={{ px: 1.5, pt: 1.25, pb: 1.5 }}>
          {description ? (
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.25 }}>
              {description}
            </Typography>
          ) : null}
          {children}
        </Box>
      </Box>
    );
  }
  return (
    <Box
      component="section"
      aria-labelledby={id}
      sx={{ px: 1.5, py: 1.35, borderBottom: 1, borderColor: "divider" }}
    >
      <Box sx={{ borderLeft: 3, borderColor: "primary.main", pl: 1 }}>
        <Typography id={id} component="h2" variant="body2" sx={{ fontWeight: 850 }}>{title}</Typography>
      </Box>
      {description ? (
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: .35, mb: 1.25 }}>
          {description}
        </Typography>
      ) : null}
      <Box sx={{ mt: description ? 0 : 1.25 }}>
        {children}
      </Box>
    </Box>
  );
}

function ReviewFactRows({
  rows,
}: {
  rows: ReadonlyArray<readonly [string, ReactNode]>;
}) {
  return (
    <Box
      component="dl"
      sx={{
        m: 0,
        display: "grid",
        borderTop: 1,
        borderColor: "divider",
      }}
    >
      {rows.map(([label, display]) => (
        <Box
          key={label}
          sx={{
            display: "grid",
            gridTemplateColumns: "84px minmax(0, 1fr)",
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
          <Typography component="dd" variant="body2" sx={{ minWidth: 0, m: 0, fontWeight: 650, lineHeight: 1.45 }}>
            {display}
          </Typography>
        </Box>
      ))}
    </Box>
  );
}

export default function OrderScheduleEditor({
  value,
  onChange,
  environmentId,
  environmentKind,
  instrumentRef,
  direction,
  maxNotional,
  maxPlanLoss = null,
  referencePrice,
  liveReferencePrice,
  chartInterval,
  onChartIntervalChange,
  liveBar,
  streamStatus,
  streamGeneration,
  marketProjectionReady,
  marketColorScheme,
  scheduleRef,
  bidPrice,
  askPrice,
  marketContext = null,
  stopReferenceInterval,
  onStopReferenceIntervalChange,
  stopReferenceLoading = false,
  stopReferenceUnavailable = false,
  feeEvidence,
  feeEvidenceLoading = false,
  feeEvidenceUnavailable = false,
  funding = null,
  fundingHistory = null,
  workspaceHeader,
  leadingControls,
  planOptions,
  reviewDisciplineSummary,
  aiReviewPanel,
  footerControls,
  decisionReviewReady = true,
  aiReviewApproved = false,
  milestoneProblems = [],
  onValidationChange,
  onSubmissionProblemsChange,
  onMaximumProjectedLossChange,
  onEffectiveNotionalChange,
  onRequestedNotionalChange,
  onMarketReadinessChange,
}: OrderScheduleEditorProps) {
  const environmentScope = `${environmentKind}:${environmentId}`;
  const [activeMilestone, setActiveMilestone] = useState<EditorMilestone>(0);
  const emittedSubmissionProblemsRef = useRef<{
    callback: typeof onSubmissionProblemsChange;
    values: readonly string[];
  } | null>(null);
  const [entryCatalogOpen, setEntryCatalogOpen] = useState(false);
  const [protectionCatalogOpen, setProtectionCatalogOpen] = useState(false);
  const [exitCatalogOpen, setExitCatalogOpen] = useState(false);
  const [fundingEstimateHoldingHours, setFundingEstimateHoldingHours] = useState("24");
  const price = value.price_distribution;
  const amount = value.amount_distribution;
  const venue = value.venue_policy;
  const entryProgram = resolvedEntryProgram(value);
  const markCondition = conditionByKind(value.entry_conditions.items, "MARK_PRICE");
  const closedBarCondition = conditionByKind(
    value.entry_conditions.items,
    "CLOSED_BAR_PRICE_15M",
  );
  const spreadCondition = conditionByKind(value.entry_conditions.items, "SPREAD_BPS");
  const moveCondition = conditionByKind(value.entry_conditions.items, "PRICE_MOVE_BPS");
  const expireRule = dynamicRuleByKind(value.dynamic_rules, "EXPIRE_REMAINING");
  const shockRule = dynamicRuleByKind(value.dynamic_rules, "CANCEL_ON_SHOCK");
  const repriceRule = dynamicRuleByKind(value.dynamic_rules, "REPRICE_ENTRY");
  const steppedRule = dynamicRuleByKind(value.dynamic_rules, "STEPPED_PROTECTION");
  const profitLockRule = dynamicRuleByKind(value.dynamic_rules, "PROFIT_LOCK");
  const addProfitLock = (mode: "RATIO" | "FIXED_GIVEBACK") => {
    onChange({
      ...value,
      dynamic_rules: withDynamicRule(
        withoutDynamicRule(value.dynamic_rules, "STEPPED_PROTECTION"),
        {
          kind: "PROFIT_LOCK",
          mode,
          activation_r: "1",
          lock_fraction: mode === "RATIO" ? "0.5" : null,
          giveback_r: mode === "FIXED_GIVEBACK" ? "0.5" : null,
          minimum_step_r: "0.25",
          minimum_update_interval_seconds: 5,
          max_adjustments: 8,
        },
      ),
    });
    setProtectionCatalogOpen(false);
    setExitCatalogOpen(false);
  };
  const takeProfitLevels = value.protection_policy.take_profit_ladder?.levels ?? [];
  const takeProfitTotalFraction = takeProfitLevels.reduce(
    (total, level) => total + (finiteNumber(level.quantity_fraction) ?? 0),
    0,
  );
  const takeProfitTotalPercent = compactDecimal(
    String(Number((takeProfitTotalFraction * 100).toFixed(8))),
  );
  const automaticProfitExitMissing = (
    takeProfitLevels.length === 0
    && value.protection_policy.time_exit_seconds === null
    && !steppedRule
    && !profitLockRule
  );
  const displayReferencePrice = liveReferencePrice ?? referencePrice;
  const entrySignalWarning = entrySignalQualityWarning(
    direction,
    value.entry_conditions.operator,
    value.entry_conditions.items,
  );
  const makerEntry = venue.order_type === "LIMIT" && venue.post_only;
  const selectedEntryFeeBps = makerEntry
    ? feeEvidence?.maker?.conservative_rate_bps
    : feeEvidence?.taker?.conservative_rate_bps;
  const selectedExitFeeBps = feeEvidence?.taker?.conservative_rate_bps;
  const requiredFeeEvidenceMissing = !selectedEntryFeeBps || !selectedExitFeeBps;
  useEffect(() => {
    if (!selectedEntryFeeBps || !selectedExitFeeBps) return;
    const current = value.protection_policy.full_fill_loss_budget;
    if (
      current?.entry_fee_bps === selectedEntryFeeBps
      && current.exit_fee_bps === selectedExitFeeBps
    ) return;
    onChange({
      ...value,
      protection_policy: {
        ...value.protection_policy,
        full_fill_loss_budget: {
          entry_fee_bps: selectedEntryFeeBps,
          exit_fee_bps: selectedExitFeeBps,
        },
      },
    });
  }, [onChange, selectedEntryFeeBps, selectedExitFeeBps, value]);
  const takeProfitCostWarning = takeProfitSpreadCoverageWarning({
    initialStopDistanceBps:
      value.protection_policy.initial_stop.distance_bps,
    levels: takeProfitLevels,
    bidPrice,
    askPrice,
    orderType: venue.order_type,
    postOnly: venue.post_only,
    makerFeeRateBps: feeEvidence?.maker?.conservative_rate_bps,
    takerFeeRateBps: feeEvidence?.taker?.conservative_rate_bps,
  });
  const localValidation = localOrderScheduleProblems(
    value,
    instrumentRef,
    maxNotional,
    scheduleRef,
  );
  const entryValidation = localOrderScheduleProblems(
    {
      ...value,
      protection_policy: {
        initial_stop: {
          distance_bps: "100",
          trigger_source: "MARK_PRICE",
          coverage: "EACH_CONFIRMED_FILL",
        },
        full_fill_loss_budget: {
          entry_fee_bps: "0",
          exit_fee_bps: "0",
        },
        take_profit_ladder: {
          levels: [{ trigger_r: "2", quantity_fraction: "1" }],
        },
        time_exit_seconds: null,
      },
      dynamic_rules: value.dynamic_rules.filter(
        (rule) => rule.kind === "EXPIRE_REMAINING"
          || rule.kind === "CANCEL_ON_SHOCK"
          || rule.kind === "REPRICE_ENTRY",
      ),
    },
    instrumentRef,
    maxNotional,
    scheduleRef,
  );
  const stopDistance = finiteNumber(
    value.protection_policy.initial_stop.distance_bps,
  );
  const protectionLocalReady = stopDistance !== null
    && stopDistance > 0
    && stopDistance < MAX_SAFE_STOP_DISTANCE_BPS
    && !feeEvidenceLoading
    && !feeEvidenceUnavailable
    && !requiredFeeEvidenceMissing;
  const exitValidation = localOrderScheduleProblems(
    {
      entry_program: {
        kind: "ONE_TIME",
        slice_count: 1,
        first_slice_delay_seconds: 0,
        slice_interval_seconds: 0,
      },
      price_distribution: { kind: "SINGLE", limit_price: "1" },
      amount_distribution: {
        mode: "FIXED",
        direction: "LOW_TO_HIGH",
        base_notional: "1",
        linear_step: "0",
        exponential_ratio: "2",
        custom_notionals: [],
      },
      venue_policy: {
        order_type: "LIMIT",
        time_in_force: "GTC",
        post_only: false,
        price_match: null,
        expire_at: null,
      },
      submission_mode: "SERIAL_PROTECTED",
      submission_order: "LOW_TO_HIGH",
      entry_conditions: {
        operator: "ALL",
        items: [{ kind: "DECISION_BASIS_READY" }],
      },
      protection_policy: {
        ...value.protection_policy,
        initial_stop: {
          distance_bps: "100",
          trigger_source: "MARK_PRICE",
          coverage: "EACH_CONFIRMED_FILL",
        },
      },
      dynamic_rules: value.dynamic_rules.filter(
        (rule) => rule.kind === "STEPPED_PROTECTION" || rule.kind === "PROFIT_LOCK",
      ),
    },
    "VALIDATION-PROBE",
    "1",
    "VALIDATION-PROBE",
  );
  const [deferredValue, setDeferredValue] = useState(value);
  useEffect(() => {
    if (deferredValue === value) return undefined;
    const timeout = window.setTimeout(() => setDeferredValue(value), 180);
    return () => window.clearTimeout(timeout);
  }, [deferredValue, value]);
  const previewStale = deferredValue !== value;
  const previewRequestReady = marketProjectionReady
    && instrumentRef.trim().length > 0
    && scheduleRef.trim().length > 0
    && isPositive(maxNotional)
    && !previewStale
    && localValidation.length === 0;
  const preview = useQuery({
    queryKey: [
      "order-schedule-preview",
      environmentScope,
      scheduleRef,
      instrumentRef,
      direction,
      maxNotional,
      referencePrice,
      deferredValue,
    ],
    queryFn: ({ signal }) => previewOrderSchedule({
      decision_basis_kind: "DIRECT_EXECUTION",
      schedule_ref: scheduleRef,
      venue_ref: "BINANCE_USDM",
      instrument_ref: instrumentRef,
      direction,
      max_notional: maxNotional,
      reference_price: referencePrice,
      spec: deferredValue,
    }, signal),
    enabled: previewRequestReady,
    retry: false,
  });
  const serverProblems = scheduleServerProblems(
    preview.data,
    preview.error,
    deferredValue,
  );
  const serverAssessmentReady = previewRequestReady
    && !previewStale
    && !preview.isFetching
    && serverScheduleWasAssessed(preview.data, preview.error);
  const previewReady = marketProjectionReady
    && localValidation.length === 0
    && !previewStale
    && !preview.isFetching
    && preview.isSuccess
    && preview.data.valid;
  const entryStepReady = milestoneConfigurationReady(
    entryValidation.length === 0,
    serverAssessmentReady,
    stageHasServerProblem(serverProblems, 0),
  );
  const protectionStepReady = milestoneConfigurationReady(
    protectionLocalReady,
    serverAssessmentReady,
    stageHasServerProblem(serverProblems, 1),
  );
  // This is workflow completeness, not a duplicate of the server's trading
  // semantics: review must never mark Exit complete while no automatic exit
  // component exists. The server preview remains the final validity gate.
  const exitStepReady = milestoneConfigurationReady(
    exitValidation.length === 0 && !automaticProfitExitMissing,
    serverAssessmentReady,
    stageHasServerProblem(serverProblems, 2),
  );
  const milestoneReady = [
    entryStepReady,
    protectionStepReady,
    exitStepReady,
    previewReady && decisionReviewReady,
    aiReviewApproved,
  ] as const;
  const visitMilestone = (next: EditorMilestone) => {
    setActiveMilestone(next);
  };
  useEffect(() => {
    const nextProblems = Array.from(new Set([
      ...localValidation,
      ...serverProblems.map((problem) => issueLabels[problem.code] ?? "订单计划未通过服务端校验。"),
    ]));
    const previous = emittedSubmissionProblemsRef.current;
    const hasSameProblems = previous?.values.length === nextProblems.length
      && previous.values.every((problem, index) => problem === nextProblems[index]);
    if (previous?.callback === onSubmissionProblemsChange && hasSameProblems) return;
    emittedSubmissionProblemsRef.current = {
      callback: onSubmissionProblemsChange,
      values: nextProblems,
    };
    onSubmissionProblemsChange?.(nextProblems);
  }, [localValidation, onSubmissionProblemsChange, serverProblems]);
  const instrumentRules = preview.data?.instrument_rules;
  const priceTickSize = instrumentRules?.price_tick_size ?? null;
  const quantityStep = venue.order_type === "MARKET"
    ? instrumentRules?.market_quantity_step ?? null
    : instrumentRules?.limit_quantity_step ?? null;
  const previewNormalizedLegs = preview.data?.normalized_legs;
  const fullFillEntryProjection = useMemo(() => (
    previewNormalizedLegs ?? []
  ).reduce(
    (projection, leg) => {
      const price = Number(leg.price ?? leg.sizing_price);
      const quantity = Number(leg.quantity);
      if (!Number.isFinite(price) || !Number.isFinite(quantity)) return projection;
      return {
        quantity: projection.quantity + quantity,
        notional: projection.notional + price * quantity,
      };
    },
    { quantity: 0, notional: 0 },
  ), [previewNormalizedLegs]);
  // Stop candidates depend on normalized entry legs and the selected market
  // reference only. A final schedule verdict can fail for an unrelated
  // condition (for example, a total-notional constraint), but the trader must
  // still be able to use the candidate prices to repair the plan.
  const stopRecommendationsReady = marketProjectionReady
    && entryValidation.length === 0
    && (previewNormalizedLegs?.length ?? 0) > 0;
  const stopRecommendations = useMemo(
    () => stopRecommendationsReady
      ? buildInitialStopRecommendations({
        direction,
        market: marketContext,
        previewLegs: previewNormalizedLegs ?? [],
      })
      : [],
    [
      direction,
      marketContext,
      previewNormalizedLegs,
      stopRecommendationsReady,
    ],
  );
  const selectedStopRecommendation = useMemo(
    () => stopRecommendations.find((recommendation) => approximatelyEqual(
      value.protection_policy.initial_stop.distance_bps,
      recommendation.distanceBpsInput,
    )) ?? null,
    [stopRecommendations, value.protection_policy.initial_stop.distance_bps],
  );
  const stopRecommendationAnnotations = useMemo(
    () => selectedStopRecommendation ? [{
      id: `halpha-stop-reference-${selectedStopRecommendation.kind.toLowerCase()}`,
      role: "STOP_REFERENCE" as const,
      label: selectedStopRecommendation.label,
      detail: `${selectedStopRecommendation.evidence}；按全档成交后的预计均价折算 ${selectedStopRecommendation.distanceBpsInput} bps；${stopReferenceInterval} K 线截止 ${formatUserVisibleTime(selectedStopRecommendation.evidenceCutoff)}`,
      price: selectedStopRecommendation.price,
      authority: "MARKET" as const,
      lineStyle: "dotted" as const,
      draggable: false,
    }] : [],
    [selectedStopRecommendation, stopReferenceInterval],
  );
  const fullFillProtectionEstimate = previewReady
    ? preview.data?.full_fill_protection_estimate ?? null
    : null;
  useEffect(() => {
    onEffectiveNotionalChange?.(
      previewReady ? preview.data?.effective_total_notional ?? null : null,
    );
  }, [onEffectiveNotionalChange, preview.data?.effective_total_notional, previewReady]);
  useEffect(() => {
    onRequestedNotionalChange?.(
      previewReady ? preview.data?.requested_total_notional ?? null : null,
    );
  }, [onRequestedNotionalChange, preview.data?.requested_total_notional, previewReady]);
  const lossLimitedEntry = lossLimitedDirectEntry({
    effectiveNotional: preview.data?.effective_total_notional,
    maximumProjectedLoss: fullFillProtectionEstimate?.maximum_projected_loss,
    maxPlanLoss,
  });
  const maximumDisciplineStopPrice = useMemo(() => {
    if (!fullFillProtectionEstimate || maxPlanLoss === null) return null;
    const averageEntry = Number(fullFillProtectionEstimate.average_entry_price);
    const quantity = Number(fullFillProtectionEstimate.quantity);
    const entryFee = Number(fullFillProtectionEstimate.estimated_entry_fee);
    const stop = Number(fullFillProtectionEstimate.stop_price);
    const exitFee = Number(fullFillProtectionEstimate.estimated_exit_fee);
    const lossLimit = Number(maxPlanLoss);
    if (![averageEntry, quantity, entryFee, stop, exitFee, lossLimit].every(Number.isFinite)
      || averageEntry <= 0 || quantity <= 0 || stop <= 0 || lossLimit <= 0) return null;
    const exitFeeRate = exitFee / (quantity * stop);
    const price = direction === "LONG"
      ? (quantity * averageEntry + entryFee - lossLimit) / (quantity * (1 - exitFeeRate))
      : (lossLimit + quantity * averageEntry - entryFee) / (quantity * (1 + exitFeeRate));
    return Number.isFinite(price) && price > 0 ? price : null;
  }, [direction, fullFillProtectionEstimate, maxPlanLoss]);
  const maximumDisciplineStopDistanceBps = useMemo(() => {
    if (!fullFillProtectionEstimate || maximumDisciplineStopPrice === null) return null;
    const averageEntry = Number(fullFillProtectionEstimate.average_entry_price);
    if (!Number.isFinite(averageEntry) || averageEntry <= 0) return null;
    const distance = Math.abs(averageEntry - maximumDisciplineStopPrice)
      / averageEntry * 10_000;
    return Number.isFinite(distance) && distance > 0 ? distance : null;
  }, [fullFillProtectionEstimate, maximumDisciplineStopPrice]);
  const stopExceedsDisciplineLoss = maximumDisciplineStopPrice !== null
    && fullFillProtectionEstimate !== null
    && (direction === "LONG"
      ? Number(fullFillProtectionEstimate.stop_price) < maximumDisciplineStopPrice
      : Number(fullFillProtectionEstimate.stop_price) > maximumDisciplineStopPrice);
  const lossLimitAnnotation = useMemo(() => maximumDisciplineStopPrice === null ? [] : [{
    id: "halpha-discipline-maximum-stop",
    role: "STOP_REFERENCE" as const,
    label: "纪律最大止损",
    detail: `按单计划最大损失 ${quoteCurrencyEstimate(maxPlanLoss ?? "0")} USDT 与当前预计仓位计算。`,
    price: maximumDisciplineStopPrice,
    authority: "SERVER_PREVIEW" as const,
    lineStyle: "dotted" as const,
    draggable: false,
  }], [maxPlanLoss, maximumDisciplineStopPrice]);
  const visibleStopRecommendationAnnotations = useMemo(
    () => activeMilestone === 1
      ? [...stopRecommendationAnnotations, ...lossLimitAnnotation]
      : [],
    [activeMilestone, lossLimitAnnotation, stopRecommendationAnnotations],
  );
  const protectionLocationRisks = useMemo(() => {
    if (!marketContext || !fullFillProtectionEstimate) return [];
    const averageEntry = Number(fullFillProtectionEstimate.average_entry_price);
    const stopPrice = Number(fullFillProtectionEstimate.stop_price);
    const atr = Number(marketContext.stop_reference_atr_14);
    if (![averageEntry, stopPrice, atr].every(Number.isFinite) || atr <= 0) return [];
    const riskDistance = Math.abs(averageEntry - stopPrice);
    const items: string[] = [];
    const riskAtr = riskDistance / atr;
    if (riskAtr < 0.75) {
      items.push(`止损距预计均价 ${riskAtr.toFixed(2)} ATR，接近近期常规波动范围。`);
    }
    const structurePrice = direction === "LONG"
      ? Number(marketContext.channel_lower)
      : Number(marketContext.channel_upper);
    if (Number.isFinite(structurePrice) && Math.abs(stopPrice - structurePrice) / atr < 0.25) {
      items.push("止损靠近近期通道边界，可能与结构性流动性聚集位置重合。");
    }
    const sameSideReferences = marketContext.stop_references.filter((reference) => (
      direction === "LONG" ? reference.side === "LOWER" : reference.side === "UPPER"
    ));
    if (sameSideReferences.some((reference) => Math.abs(stopPrice - Number(reference.price)) / atr < 0.25)) {
      items.push("止损靠近当前结构止损参考位，留意扫损后再反转的风险。");
    }
    const opposingChannel = direction === "LONG"
      ? Number(marketContext.channel_upper)
      : Number(marketContext.channel_lower);
    const nearestTarget = takeProfitLevels[0]
      ? averageEntry + (direction === "LONG" ? 1 : -1) * riskDistance * Number(takeProfitLevels[0].trigger_r)
      : null;
    if (nearestTarget !== null && Number.isFinite(opposingChannel)
      && Math.abs(nearestTarget - opposingChannel) / atr < 0.25) {
      items.push("首个止盈靠近近期通道边界，可能遇到短期阻力或支撑。");
    }
    return items;
  }, [direction, fullFillProtectionEstimate, marketContext, takeProfitLevels]);
  const takeProfitAfterCost = takeProfitAfterCostEstimate({
    initialStopDistanceBps:
      value.protection_policy.initial_stop.distance_bps,
    levels: takeProfitLevels,
    bidPrice,
    askPrice,
    orderType: venue.order_type,
    postOnly: venue.post_only,
    effectiveNotional: preview.data?.valid
      ? preview.data.effective_total_notional
      : null,
    makerFeeRateBps: feeEvidence?.maker?.conservative_rate_bps,
    takerFeeRateBps: feeEvidence?.taker?.conservative_rate_bps,
  });
  const takeProfitAfterCostPanel = takeProfitLevels.length === 0
    ? null
    : feeEvidenceLoading
      ? (
        <Typography variant="caption" color="text.secondary" aria-live="polite">
          正在读取近期实付费用参考…
        </Typography>
      )
      : feeEvidenceUnavailable || requiredFeeEvidenceMissing
        ? (
          <Alert severity="warning" variant="outlined" data-testid="after-cost-estimate-unavailable">
            近期实付费用证据不足；费用后盈亏、盈亏平衡与净盈亏比保持未知。
          </Alert>
        )
        : takeProfitAfterCost
          ? (
            <Box
              data-testid="after-cost-estimate"
              sx={{ pt: .9, borderTop: 1, borderColor: "divider" }}
            >
              <Typography variant="caption" sx={{ display: "block", mb: .8, fontWeight: 800 }}>
                费用后风险收益
              </Typography>
              <Box
                component="dl"
                sx={{
                  m: 0,
                  display: "grid",
                  gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
                  gap: .8,
                }}
              >
                {[
                { label: "初始止损风险", value: -takeProfitAfterCost.grossRisk, exact: -takeProfitAfterCost.grossRisk },
                { label: "预计手续费", value: -takeProfitAfterCost.estimatedFee, exact: -takeProfitAfterCost.estimatedFee },
                { label: "费用后目标净收益", value: takeProfitAfterCost.netReward, exact: takeProfitAfterCost.netReward },
                { label: "费用后净盈亏比", value: null, exact: takeProfitAfterCost.netRiskReward },
              ].map((item) => (
                <Box key={item.label} sx={{ minWidth: 0 }}>
                  <Typography component="dt" variant="caption" color="text.secondary">
                    {item.label}
                  </Typography>
                  <Typography
                    component="dd"
                    variant="body2"
                    title={item.value === null || item.value === item.exact ? undefined : String(item.exact)}
                    className={financialToneClassName(item.value === null ? undefined : financialToneForSignedValue(item.value))}
                    sx={{ m: 0, fontWeight: 750, overflowWrap: "anywhere" }}
                  >
                    {item.value === null
                      ? `${compactDecimal(Number(item.exact), { maximumFractionDigits: 2, truncatedMarker: "" })} : 1`
                      : `${item.value > 0 ? "+" : ""}${quoteCurrencyEstimate(item.value)} USDT`}
                  </Typography>
                </Box>
              ))}
              </Box>
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
                近期实付参考：入场 {takeProfitAfterCost.entryLiquidity === "MAKER" ? "Maker" : "Taker"}
                {` ${compactDecimal(takeProfitAfterCost.entryFeeRateBps)} bps，退出 Taker ${compactDecimal(takeProfitAfterCost.exitFeeRateBps)} bps；`}
                样本截至 {formatUserVisibleTime(feeEvidence?.source_cutoff)}。
              </Typography>
            </Box>
          )
          : (
            <Typography variant="caption" color="text.secondary">
              完成有效预览并取得当前盘口后显示费用后风险收益。
            </Typography>
          );
  const fundingSettlementInterval = formatFundingSettlementInterval(
    fundingHistory?.average_interval_seconds ?? null,
  );
  const fundingEstimate = estimateFundingCost({
    averageFundingRate: fundingHistory?.average_funding_rate ?? null,
    averageIntervalSeconds: fundingHistory?.average_interval_seconds ?? null,
    nextFundingAt: funding?.next_funding_at ?? null,
    sourceCutoff: funding?.source_cutoff ?? null,
    holdingHours: fundingEstimateHoldingHours,
    notional: maxNotional,
    direction,
  });
  const fundingPanel = (
    <Box data-testid="current-funding" sx={{ pt: .85, borderTop: 1, borderColor: "divider" }}>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
        当前资金费率
      </Typography>
      {funding ? (
        <>
          <Typography
            className="mono"
            variant="body2"
            sx={{ mt: .25, fontWeight: 800 }}
          >
            {formatFundingRatePercent(funding.funding_rate)}
            {fundingSettlementInterval ? ` / ${fundingSettlementInterval}` : ""}
          </Typography>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: .25 }}>
            下次结算 {formatUserVisibleTime(funding.next_funding_at)}
          </Typography>
          <Box sx={{ mt: 1, pt: 1, borderTop: 1, borderColor: "divider" }}>
            <Stack direction="row" spacing={1} sx={{ alignItems: "center" }}>
              <TextField
                size="small"
                type="number"
                label="预计持仓（小时）"
                value={fundingEstimateHoldingHours}
                onChange={(event) => setFundingEstimateHoldingHours(event.target.value)}
                slotProps={{ htmlInput: { min: 0, step: "any" } }}
                sx={{ width: 150, flexShrink: 0 }}
              />
              <Box sx={{ minWidth: 0 }}>
                <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                  预计资金费用
                </Typography>
                {fundingEstimate ? (
                  <Typography
                    className={`mono ${financialToneClassName(financialToneForSignedValue(fundingEstimate.signedAmount)) ?? ""}`}
                    variant="body2"
                    sx={{ fontWeight: 800, whiteSpace: "nowrap" }}
                  >
                    {Number(fundingEstimate.signedAmount) > 0 ? "+" : Number(fundingEstimate.signedAmount) < 0 ? "-" : ""}
                    {quoteCurrencyAmount(String(Math.abs(Number(fundingEstimate.signedAmount))))} USDT
                  </Typography>
                ) : (
                  <Typography variant="body2" color="text.secondary">历史费率不可用</Typography>
                )}
              </Box>
            </Stack>
            {fundingEstimate && fundingHistory ? (
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: .55 }}>
                近 {fundingHistory.samples.length} 次均值 {formatFundingRatePercent(fundingHistory.average_funding_rate)}{fundingSettlementInterval ? ` / ${fundingSettlementInterval}` : ""} · 按 {quoteCurrencyAmount(maxNotional)} USDT · 预计 {fundingEstimate.settlements} 次结算
              </Typography>
            ) : null}
          </Box>
        </>
      ) : (
        <Typography variant="body2" sx={{ mt: .25, fontWeight: 750 }}>
          实时数据不可用
        </Typography>
      )}
    </Box>
  );

  useEffect(() => {
    onValidationChange?.(previewReady);
  }, [onValidationChange, previewReady]);
  useEffect(() => {
    onMaximumProjectedLossChange?.(
      fullFillProtectionEstimate?.maximum_projected_loss ?? null,
    );
  }, [
    fullFillProtectionEstimate,
    onMaximumProjectedLossChange,
  ]);

  const changeOrderType = (orderType: "MARKET" | "LIMIT") => {
    if (orderType === "MARKET") {
      onChange({
        ...value,
        price_distribution: { kind: "SINGLE", limit_price: null },
        amount_distribution: amount.mode === "FIXED" ? {
          ...amount,
          base_notional: approximatelyEqual(amount.base_notional, maxNotional)
            ? maxNotional
            : amount.base_notional,
        } : {
          ...amount,
          mode: "FIXED",
          base_notional: maxNotional,
          custom_notionals: [],
        },
        venue_policy: {
          order_type: "MARKET",
          time_in_force: null,
          post_only: false,
          price_match: null,
          expire_at: null,
        },
        dynamic_rules: value.dynamic_rules.filter(
          (rule) => rule.kind !== "EXPIRE_REMAINING"
            && rule.kind !== "REPRICE_ENTRY",
        ),
      });
      return;
    }
    onChange({
      ...value,
      price_distribution: price.kind === "SINGLE"
        ? { ...price, limit_price: price.limit_price ?? displayReferencePrice ?? "" }
        : price,
      venue_policy: {
        ...venue,
        order_type: "LIMIT",
        time_in_force: entryProgram.kind === "TIME_SLICED" ? "IOC" : "GTC",
        post_only: false,
        expire_at: null,
      },
    });
  };

  const changeEntryProgram = (kind: OrderScheduleEntryProgram["kind"]) => {
    const currentPrice = price.kind === "SINGLE"
      ? price.limit_price
      : price.lower_price;
    const entryConditions = entryProgram.kind === "EVENT_TRIGGERED"
      && kind !== "EVENT_TRIGGERED"
      ? {
        ...value.entry_conditions,
        items: withoutGeneratedEventCondition(
          value.entry_conditions.operator,
          value.entry_conditions.items,
          direction,
        ),
      }
      : value.entry_conditions;
    const singlePrice = {
      kind: "SINGLE" as const,
      limit_price: currentPrice || displayReferencePrice || "",
    };
    if (kind === "PRICE_LADDER") {
      const lower = generatedOffsetPrice(
        displayReferencePrice,
        0.995,
        priceTickSize,
      ) ?? currentPrice ?? "";
      const upper = generatedOffsetPrice(
        displayReferencePrice,
        1.005,
        priceTickSize,
      ) ?? "";
      onChange({
        ...value,
        entry_program: {
          kind,
          slice_count: 1,
          first_slice_delay_seconds: 0,
          slice_interval_seconds: 0,
        },
        price_distribution: {
          kind: "LADDER",
          lower_price: lower,
          upper_price: upper,
          level_count: 5,
          spacing_mode: "EQUAL",
          spacing_direction: "LOW_TO_HIGH",
          linear_start_weight: "1",
          linear_step: "1",
          geometric_ratio: "2",
          custom_gap_weights: [],
        },
        amount_distribution: {
          ...amount,
          mode: "FIXED",
          base_notional: evenlyDividedNotional(maxNotional, 5)
            ?? amount.base_notional,
          custom_notionals: [],
        },
        venue_policy: {
          order_type: "LIMIT",
          time_in_force: "GTC",
          post_only: false,
          price_match: null,
          expire_at: null,
        },
        submission_order: "HIGH_TO_LOW",
        entry_conditions: entryConditions,
        dynamic_rules: value.dynamic_rules.filter(
          (rule) => rule.kind !== "REPRICE_ENTRY",
        ),
      });
      return;
    }
    if (kind === "TIME_SLICED") {
      onChange({
        ...value,
        entry_program: {
          kind,
          slice_count: 4,
          first_slice_delay_seconds: 0,
          slice_interval_seconds: 300,
        },
        price_distribution: { kind: "SINGLE", limit_price: null },
        amount_distribution: {
          ...amount,
          mode: "FIXED",
          base_notional: evenlyDividedNotional(maxNotional, 4)
            ?? amount.base_notional,
          custom_notionals: [],
        },
        venue_policy: {
          order_type: "MARKET",
          time_in_force: null,
          post_only: false,
          price_match: null,
          expire_at: null,
        },
        submission_order: "LOW_TO_HIGH",
        entry_conditions: entryConditions,
        dynamic_rules: value.dynamic_rules.filter(
          (rule) => rule.kind !== "EXPIRE_REMAINING"
            && rule.kind !== "REPRICE_ENTRY",
        ),
      });
      return;
    }
    if (kind === "EVENT_TRIGGERED") {
      const existingEvent = value.entry_conditions.items.some(
        (condition) => condition.kind === "MARK_PRICE"
          || condition.kind === "CLOSED_BAR_PRICE_15M"
          || condition.kind === "PRICE_MOVE_BPS",
      );
      const preservesCurrentOrder = price.kind === "SINGLE";
      onChange({
        ...value,
        entry_program: {
          kind,
          slice_count: 1,
          first_slice_delay_seconds: 0,
          slice_interval_seconds: 0,
        },
        price_distribution: preservesCurrentOrder
          ? price
          : { kind: "SINGLE", limit_price: null },
        amount_distribution: {
          ...amount,
          mode: "FIXED",
          base_notional: maxNotional || amount.base_notional,
          custom_notionals: [],
        },
        venue_policy: preservesCurrentOrder
          ? venue
          : {
            order_type: "MARKET",
            time_in_force: null,
            post_only: false,
            price_match: null,
            expire_at: null,
          },
        submission_order: "LOW_TO_HIGH",
        entry_conditions: existingEvent
          ? value.entry_conditions
          : {
            operator: "ALL",
            items: [
              { kind: "DECISION_BASIS_READY" },
              {
                kind: "PRICE_MOVE_BPS",
                comparator: direction === "LONG" ? "GTE" : "DROP_GTE",
                threshold_bps: "30",
                window_seconds: 30,
              },
            ],
          },
        dynamic_rules: preservesCurrentOrder
          ? value.dynamic_rules
          : value.dynamic_rules.filter(
            (rule) => rule.kind !== "EXPIRE_REMAINING"
              && rule.kind !== "REPRICE_ENTRY",
          ),
      });
      return;
    }
    onChange({
      ...value,
      entry_program: {
        kind,
        slice_count: 1,
        first_slice_delay_seconds: 0,
        slice_interval_seconds: 0,
      },
      price_distribution: venue.order_type === "MARKET"
        ? { kind: "SINGLE", limit_price: null }
        : singlePrice,
      amount_distribution: {
        ...amount,
        mode: "FIXED",
        base_notional: maxNotional || amount.base_notional,
        custom_notionals: [],
      },
      venue_policy: venue.order_type === "MARKET"
        ? venue
        : { ...venue, time_in_force: venue.time_in_force ?? "GTC" },
      submission_order: "LOW_TO_HIGH",
      entry_conditions: entryConditions,
    });
  };

  const setConditionEnabled = (
    kind: "MARK_PRICE" | "CLOSED_BAR_PRICE_15M" | "SPREAD_BPS" | "PRICE_MOVE_BPS",
    enabled: boolean,
  ) => {
    let condition: OrderScheduleCondition;
    if (kind === "MARK_PRICE") {
      condition = { kind, comparator: "GTE", price: referencePrice ?? "" };
    } else if (kind === "CLOSED_BAR_PRICE_15M") {
      condition = {
        kind,
        comparator: direction === "LONG" ? "GTE" : "LTE",
        price: referencePrice ?? "",
      };
    } else if (kind === "SPREAD_BPS") {
      condition = { kind, maximum_bps: "10" };
    } else {
      condition = { kind, comparator: "ABS_GTE", threshold_bps: "30", window_seconds: 30 };
    }
    const items = enabled
      ? withCondition(value.entry_conditions.items, condition)
      : withoutCondition(value.entry_conditions.items, kind);
    onChange({
      ...value,
      entry_conditions: {
        ...value.entry_conditions,
        items: normalizedDirectConditionItems(
          value.entry_conditions.operator,
          items,
        ),
      },
    });
  };

  const marketConditionCount = value.entry_conditions.items.filter(
    (item) => item.kind !== "DECISION_BASIS_READY",
  ).length;
  const entryDynamicRules = value.dynamic_rules.filter(
    (rule) => rule.kind === "EXPIRE_REMAINING"
      || rule.kind === "CANCEL_ON_SHOCK"
      || rule.kind === "REPRICE_ENTRY",
  );
  const repriceCompatible = (
    (entryProgram.kind === "ONE_TIME"
      || entryProgram.kind === "EVENT_TRIGGERED")
    && price.kind === "SINGLE"
    && price.limit_price !== null
    && venue.order_type === "LIMIT"
    && venue.time_in_force === "GTC"
    && venue.price_match === null
  );
  const entryProgramSummary = (() => {
    if (entryProgram.kind === "PRICE_LADDER" && price.kind === "LADDER") {
      return `价格区间分批 · ${price.level_count} 档`;
    }
    if (entryProgram.kind === "TIME_SLICED") {
      const start = entryProgram.first_slice_delay_seconds === 0
        ? "立即开始"
        : `${entryProgram.first_slice_delay_seconds}s 后开始`;
      return `时间分批 · ${entryProgram.slice_count} 笔 · ${start} · 最早间隔 ${entryProgram.slice_interval_seconds}s；前一笔成交并建立保护，或未成交撤单闭合后才释放下一笔`;
    }
    if (entryProgram.kind === "EVENT_TRIGGERED") return "事件触发入场";
    return "一次性入场";
  })();
  const orderInstructionSummary = venue.order_type === "MARKET"
    ? "市价 · 场所决定成交价"
    : [
      venue.post_only
        ? `Maker only 限价 · 正常拒绝最多重挂 ${POST_ONLY_RETRY_MAX_ATTEMPTS} 次`
        : "限价",
      venue.time_in_force,
      venue.time_in_force === "GTD" && venue.expire_at
        ? `到期 ${formatUserVisibleTime(venue.expire_at)}`
        : "",
      venue.price_match,
      entryProgram.kind === "PRICE_LADDER"
        ? value.submission_order === "HIGH_TO_LOW" ? "高→低" : "低→高"
        : "",
    ].filter(Boolean).join(" · ");
  const amountModeLabel = {
    FIXED: "固定金额",
    LINEAR: "线性增长",
    EXPONENTIAL: "指数增长",
    CUSTOM: "逐档自定义",
  }[amount.mode];
  const amountSummary = amount.mode === "CUSTOM"
    ? `${amountModeLabel} · ${amount.custom_notionals.length} 笔`
    : `${amountModeLabel} · 起始 ${quoteCurrencyAmount(amount.base_notional)} USDT`;
  const conditionReviewDetails = value.entry_conditions.items.flatMap((item) => {
    if (item.kind === "DECISION_BASIS_READY") return [];
    if (item.kind === "MARK_PRICE") {
      return [
        `标记价 ${item.comparator === "GTE" ? "≥" : "≤"} ${quoteCurrencyAmount(item.price)} USDT`,
      ];
    }
    if (item.kind === "CLOSED_BAR_PRICE_15M") {
      return [
        `15m 已闭合 K 线收盘 ${item.comparator === "GTE" ? "≥" : "≤"} ${quoteCurrencyAmount(item.price)} USDT`,
      ];
    }
    if (item.kind === "SPREAD_BPS") {
      return [`买卖价差 ≤ ${compactDecimal(item.maximum_bps)} bps`];
    }
    if (item.kind === "PROFIT_R") {
      return [
        `收益 R ${item.comparator === "GTE" ? "≥" : "≤"} ${compactDecimal(item.threshold_r)}`,
      ];
    }
    if (item.kind === "ELAPSED_SECONDS") {
      return [`经过 ${item.minimum_seconds} 秒`];
    }
    const comparator = {
      GTE: "上涨",
      DROP_GTE: "下跌",
      ABS_GTE: "绝对变动",
      LTE: "绝对变动不超过",
    }[item.comparator];
    return [
      `${item.window_seconds} 秒${comparator} ${item.comparator === "LTE" ? "≤" : "≥"} ${compactDecimal(item.threshold_bps)} bps`,
    ];
  });
  const conditionReviewSummary = conditionReviewDetails.length === 0
    ? "立即尝试入场"
    : [
      value.entry_conditions.operator === "ANY" ? "任一满足" : "全部满足",
      ...conditionReviewDetails,
    ].join("；");
  const invalidationReviewSummary = [
    expireRule
      ? entryProgram.kind === "TIME_SLICED"
        ? `每批提交后 ${expireRule.after_seconds} 秒未成交即撤销，再释放下一批`
        : `首次提交后 ${expireRule.after_seconds} 秒未成交即撤销`
      : "",
    shockRule?.invalidation_price
      ? `标记价 ${direction === "SHORT" ? "≥" : "≤"} ${quoteCurrencyAmount(shockRule.invalidation_price)} USDT 时取消`
      : "",
    shockRule?.opportunity_missed_price
      ? `标记价 ${direction === "SHORT" ? "≤" : "≥"} ${quoteCurrencyAmount(shockRule.opportunity_missed_price)} USDT 时视为错过`
      : "",
    shockRule?.window_seconds && shockRule.adverse_move_bps
      ? `${shockRule.window_seconds} 秒反向${direction === "SHORT" ? "上涨" : "下跌"} ≥ ${compactDecimal(shockRule.adverse_move_bps)} bps 时取消`
      : "",
    repriceRule
      ? `价格偏离 ${compactDecimal(repriceRule.trigger_distance_bps)} bps 时按同侧盘口重挂，最多 ${repriceRule.max_adjustments} 次、总移动不超过 ${compactDecimal(repriceRule.maximum_total_move_bps)} bps`
      : "",
  ].filter(Boolean).join(" · ") || "未配置";
  const takeProfitReviewDetails = takeProfitLevels.map((level, index) => (
    `TP${index + 1} ${compactDecimal(level.trigger_r)}R / ${compactDecimal(scaleDecimalByPowerOfTen(level.quantity_fraction, 2) ?? "0")}%`
  ));
  const exitReviewSummary = [
    ...takeProfitReviewDetails,
    value.protection_policy.time_exit_seconds !== null
      ? `首笔成交后 ${value.protection_policy.time_exit_seconds} 秒发起整组退出`
      : "",
    steppedRule
      ? `阶梯保盈：${steppedRule.steps.map((step) => (
        `${compactDecimal(step.trigger_r)}R → 止损 ${compactDecimal(step.stop_r)}R`
      )).join("、")}（间隔 ${steppedRule.minimum_update_interval_seconds} 秒，最多 ${steppedRule.max_adjustments} 次）`
      : "",
    profitLockRule
      ? profitLockRule.mode === "RATIO"
        ? `达到 ${compactDecimal(profitLockRule.activation_r)}R 后锁定峰值盈利 ${compactDecimal(scaleDecimalByPowerOfTen(profitLockRule.lock_fraction ?? "0", 2) ?? "0")}%（最小收紧 ${compactDecimal(profitLockRule.minimum_step_r)}R，间隔 ${profitLockRule.minimum_update_interval_seconds} 秒，最多 ${profitLockRule.max_adjustments} 次）`
        : `达到 ${compactDecimal(profitLockRule.activation_r)}R 后最多回吐 ${compactDecimal(profitLockRule.giveback_r ?? "0")}R（最小收紧 ${compactDecimal(profitLockRule.minimum_step_r)}R，间隔 ${profitLockRule.minimum_update_interval_seconds} 秒，最多 ${profitLockRule.max_adjustments} 次）`
      : "",
  ].filter(Boolean).join(" · ");

  return (
    <Box
      sx={{
        display: "grid",
        gridTemplateColumns: {
          xs: "minmax(0, 1fr)",
          md: "minmax(0, 1fr) minmax(390px, 36%)",
        },
        gridTemplateRows: { xs: "auto auto", md: "minmax(0, 1fr)" },
        height: { xs: "auto", md: "100%" },
        minHeight: 0,
        overflow: { xs: "visible", md: "hidden" },
        bgcolor: "background.paper",
      }}
    >
      <Box
        data-testid="direct-execution-market-context"
        sx={{
          gridColumn: "1",
          gridRow: "1",
          minWidth: 0,
          minHeight: { xs: 0, md: 0 },
          display: "flex",
          flexDirection: "column",
          overflow: { xs: "visible", md: "hidden" },
        }}
      >
        <Box
          data-testid="direct-execution-context-strip"
          sx={{
            flex: "0 0 auto",
            minHeight: 54,
            px: { xs: 1.25, sm: 1.75 },
            py: 1,
            display: "flex",
            gap: 1,
            alignItems: "center",
            borderBottom: 1,
            borderColor: "divider",
            overflow: "hidden",
          }}
        >
          {workspaceHeader ?? (
            <>
              <Typography component="h1" variant="subtitle1" sx={{ fontWeight: 750, mr: .5 }}>直接执行</Typography>
              <Chip size="small" variant="outlined" label={instrumentRef || "交易对象未知"} />
              <Chip size="small" variant="outlined" label={direction === "LONG" ? "做多" : "做空"} />
              <Chip size="small" variant="outlined" label={`资金上限 ${maxNotional ? quoteCurrencyAmount(maxNotional) : "未知"} USDT`} />
            </>
          )}
        </Box>
        <Box
          sx={{
            minWidth: 0,
            minHeight: { xs: 0, md: 0 },
            flex: { xs: "0 0 auto", md: "1 1 auto" },
            height: { xs: "auto", md: "100%" },
            p: { xs: 1, md: 1.25 },
            overflow: { xs: "visible", md: "hidden" },
          }}
        >
          <OrderScheduleChart
            key={environmentScope}
            workspaceMode
            environmentId={environmentId}
            environmentKind={environmentKind}
            instrumentRef={instrumentRef}
            direction={direction}
            marketColorScheme={marketColorScheme}
            interval={chartInterval}
            onIntervalChange={onChartIntervalChange}
            liveBar={liveBar}
            streamStatus={streamStatus}
            streamGeneration={streamGeneration}
            priceProjectionReady={marketProjectionReady}
            priceTickSize={preview.data?.instrument_rules.price_tick_size ?? null}
            referencePrice={displayReferencePrice}
            spec={value}
            previewLegs={previewReady ? (preview.data?.normalized_legs ?? []) : []}
            fullFillProtectionEstimate={fullFillProtectionEstimate}
            previewState={previewReady
              ? "READY"
              : localValidation.length === 0
                && marketProjectionReady
                && (previewStale || preview.isPending || preview.isFetching)
                ? "PENDING"
                : "BLOCKED"}
            additionalPriceAnnotations={visibleStopRecommendationAnnotations}
            onRangeChange={(lowerPrice, upperPrice) => {
              if (value.price_distribution.kind !== "LADDER") return;
              onChange({
                ...value,
                price_distribution: {
                  ...value.price_distribution,
                  lower_price: lowerPrice,
                  upper_price: upperPrice,
                },
              });
            }}
            onSingleLimitPriceChange={(limitPrice) => {
              if (value.price_distribution.kind !== "SINGLE") return;
              onChange({
                ...value,
                price_distribution: {
                  ...value.price_distribution,
                  limit_price: limitPrice,
                },
                venue_policy: {
                  ...venue,
                  order_type: "LIMIT",
                  price_match: null,
                },
              });
            }}
            onMarketReadinessChange={onMarketReadinessChange}
          />
        </Box>
      </Box>

      <Box
        component="aside"
        aria-label="直接执行快速配置"
        sx={{
          gridColumn: { xs: "1", md: "2" },
          gridRow: { xs: "2", md: "1" },
          minWidth: 0,
          minHeight: 0,
          display: "flex",
          flexDirection: "column",
          borderLeft: { xs: 0, md: 1 },
          borderTop: { xs: 1, md: 0 },
          borderColor: "divider",
        bgcolor: "background.paper",
      }}
      >
        <Box
          data-testid="direct-order-config-scroll"
          sx={{
            minHeight: 0,
            flex: "1 1 auto",
            overflowY: { xs: "visible", md: "auto" },
            overscrollBehavior: "contain",
          }}
        >
          <Box
            component="nav"
            data-testid="direct-order-milestones"
            aria-label="计划创建步骤"
            sx={{
              position: "sticky",
              top: { xs: "96px", md: 0 },
              zIndex: 4,
              display: "grid",
              gridTemplateColumns: "repeat(5,minmax(0,1fr))",
              bgcolor: "background.default",
              borderBottom: 1,
              borderColor: "divider",
            }}
          >
            {(["入场", "保护", "退出", "核对", "AI审核"] as const).map((label, index) => (
              <Button
                key={label}
                type="button"
                aria-current={activeMilestone === index ? "step" : undefined}
                onClick={() => visitMilestone(index as EditorMilestone)}
                sx={{
                  minWidth: 0,
                  minHeight: 46,
                  px: .5,
                  borderRadius: 0,
                  color: activeMilestone === index ? "text.primary" : "text.secondary",
                  borderBottom: 2,
                  borderBottomColor: activeMilestone === index
                    ? "primary.main"
                    : "transparent",
                  fontWeight: activeMilestone === index ? 800 : 650,
                  fontSize: 12,
                }}
              >
                <Box
                  component="span"
                  sx={{
                    display: "inline-flex",
                    alignItems: "center",
                    minWidth: 0,
                  }}
                >
                  <Box
                    component="span"
                    sx={{
                      display: "inline-grid",
                      placeItems: "center",
                      width: 20,
                      height: 20,
                      mr: .5,
                      borderRadius: "50%",
                      bgcolor: activeMilestone === index ? "primary.main" : "action.hover",
                      color: activeMilestone === index ? "primary.contrastText" : "text.secondary",
                      fontSize: 11,
                    }}
                  >
                    {index !== activeMilestone && milestoneReady[index]
                      ? "✓"
                      : index + 1}
                  </Box>
                  <Box component="span" sx={{ overflow: "hidden", textOverflow: "ellipsis" }}>{label}</Box>
                  {(() => {
                    const problems = milestoneProblems[index] ?? [];
                    const hasProblem = problems.length > 0 || !milestoneReady[index];
                    const title = problems.length > 0
                      ? <Box component="ul" sx={{ m: 0, pl: 2, py: .25 }}>{problems.map((problem) => <li key={problem}>{problem}</li>)}</Box>
                      : "该页仍有待处理配置。";
                    return hasProblem ? (
                      <Tooltip title={title} arrow>
                        <ErrorOutlined color="error" sx={{ fontSize: 15, ml: .35, flex: "0 0 auto" }} />
                      </Tooltip>
                    ) : null;
                  })()}
                </Box>
              </Button>
            ))}
          </Box>
          {leadingControls && activeMilestone === 0 ? (
            <Box sx={{ minWidth: 0 }}>
              {leadingControls}
            </Box>
          ) : null}
          {activeMilestone === 0 ? (
            <>
          <EditorSection
            id="order-schedule-entry-program-title"
            title="入场方案"
          >
            <Box
              role="radiogroup"
              aria-label="入场方案"
              sx={{
                display: "grid",
                gridTemplateColumns: "repeat(2,minmax(0,1fr))",
                overflow: "hidden",
                border: 1,
                borderColor: "divider",
                borderRadius: 1.25,
                "& > :nth-of-type(odd)": { borderRight: 1, borderRightColor: "divider" },
                "& > :nth-of-type(-n+2)": { borderBottom: 1, borderBottomColor: "divider" },
              }}
            >
              {([
                ["ONE_TIME", "一次性入场"],
                ["PRICE_LADDER", "价格阶梯入场"],
                ["TIME_SLICED", "时间分批"],
                ["EVENT_TRIGGERED", "事件触发入场"],
              ] as const).map(([kind, label]) => (
                <Button
                  key={kind}
                  type="button"
                  role="radio"
                  aria-checked={entryProgram.kind === kind}
                  variant="outlined"
                  color="inherit"
                  onClick={() => changeEntryProgram(kind)}
                  sx={{
                    minHeight: 42,
                    p: 0,
                    border: 0,
                    borderRadius: 0,
                    alignItems: "center",
                    justifyContent: "center",
                    textAlign: "center",
                    textTransform: "none",
                    ...(entryProgram.kind === kind
                      ? selectedChoiceSx
                      : {
                        color: "text.primary",
                        bgcolor: "background.paper",
                        boxShadow: "none",
                      }),
                    "&:hover": {
                      borderColor: "transparent",
                      backgroundColor: entryProgram.kind === kind ? "var(--halpha-selection) !important" : undefined,
                      bgcolor: entryProgram.kind === kind ? undefined : "action.hover",
                    },
                  }}
                >
                  <Typography component="span" variant="body2" sx={{ fontWeight: entryProgram.kind === kind ? 850 : 800, lineHeight: 1 }}>
                    {label}
                  </Typography>
                </Button>
              ))}
            </Box>
            {entryProgram.kind === "TIME_SLICED" ? (
              <>
                <Box sx={{ ...compactFieldGridSx, mt: 1.25 }}>
                  <TextField
                    size="small"
                    type="number"
                    label="分批数量"
                    value={entryProgram.slice_count}
                    onChange={(event) => onChange({
                      ...value,
                      entry_program: {
                        ...entryProgram,
                        slice_count: Number(event.target.value),
                      },
                    })}
                    slotProps={{ htmlInput: { min: 2, max: 50, step: 1 } }}
                  />
                  <TextField
                    size="small"
                    type="number"
                    label="首笔等待（秒）"
                    value={entryProgram.first_slice_delay_seconds}
                    onChange={(event) => onChange({
                      ...value,
                      entry_program: {
                        ...entryProgram,
                        first_slice_delay_seconds: Number(event.target.value),
                      },
                    })}
                    slotProps={{ htmlInput: { min: 0, max: 604_800, step: 1 } }}
                  />
                  <TextField
                    size="small"
                    type="number"
                    label="每笔最早间隔（秒）"
                    value={entryProgram.slice_interval_seconds}
                    onChange={(event) => onChange({
                      ...value,
                      entry_program: {
                        ...entryProgram,
                        slice_interval_seconds: Number(event.target.value),
                      },
                    })}
                    slotProps={{ htmlInput: { min: 1, max: 604_800, step: 1 } }}
                  />
                </Box>
                <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.75 }}>
                  每笔还需等待前一笔成交并建立保护，或未成交撤单闭合；自动退出到达时，尚未释放的批次不再开仓。
                </Typography>
              </>
            ) : null}
          </EditorSection>
          <EditorSection
        id="order-schedule-price-title"
        title="价格与档位"
      >
        <ToggleButtonGroup
          exclusive
          fullWidth
          size="small"
          value={venue.order_type}
          aria-label="下单方式"
          onChange={(_event, next: "MARKET" | "LIMIT" | null) => {
            if (!next || next === venue.order_type) return;
            changeOrderType(next);
          }}
          sx={{
            ...outlinedChoiceSx,
            "& .MuiToggleButton-root": {
              ...outlinedChoiceSx["& .MuiToggleButton-root"],
              minHeight: 38,
            },
          }}
        >
          <ToggleButton
            value="MARKET"
            disabled={entryProgram.kind === "PRICE_LADDER"}
          >
            市价
          </ToggleButton>
          <ToggleButton value="LIMIT">
            {entryProgram.kind === "PRICE_LADDER" ? "分档限价" : "限价"}
          </ToggleButton>
        </ToggleButtonGroup>

        {price.kind === "SINGLE" ? (
          <Box sx={{ mt: 1.25 }}>
            {venue.order_type === "MARKET" ? (
              <Typography variant="caption" color="text.secondary">
                按场所当时可成交价格执行；当前 {displayReferencePrice ? `参考 ${tradingPrice(displayReferencePrice, priceTickSize)} USDT` : "参考价不可用"}，成交价仍未知。
              </Typography>
            ) : (
              <>
                <TextField
                  select
                  fullWidth
                  size="small"
                  label="限价来源"
                  value={venue.price_match ?? "MANUAL"}
                  onChange={(event) => {
                    const next = event.target.value as OrderSchedulePriceMatch | "MANUAL";
                    const useBookPrice = next !== "MANUAL";
                    onChange({
                      ...value,
                      price_distribution: {
                        ...price,
                        limit_price: useBookPrice
                          ? null
                          : (price.limit_price ?? referencePrice ?? ""),
                      },
                      venue_policy: {
                        ...venue,
                        post_only: false,
                        price_match: useBookPrice ? next : null,
                      },
                      dynamic_rules: useBookPrice
                        ? withoutDynamicRule(value.dynamic_rules, "REPRICE_ENTRY")
                        : value.dynamic_rules,
                    });
                  }}
                >
                  <MenuItem value="MANUAL">手动指定价格</MenuItem>
                  {priceMatchOptions.map((option) => (
                    <MenuItem key={option.value} value={option.value}>
                      盘口定价 · {option.label}
                    </MenuItem>
                  ))}
                </TextField>
                {venue.price_match === null ? (
                  <>
                    <TextField
                      fullWidth
                      size="small"
                      type="number"
                      label="限价（USDT）"
                      value={price.limit_price ?? ""}
                      onChange={(event) => onChange({
                        ...value,
                        price_distribution: { ...price, limit_price: event.target.value },
                        venue_policy: { ...venue, price_match: null },
                      })}
                      slotProps={{ htmlInput: { min: 0, step: "any" } }}
                      sx={{ mt: 1 }}
                    />
                <Stack direction="row" spacing={.75} sx={{ mt: .75 }}>
                  {[
                    { label: "买一", candidate: bidPrice },
                    { label: "中间价", candidate: displayReferencePrice },
                    { label: "卖一", candidate: askPrice },
                  ].map(({ label, candidate }) => (
                    <Button
                      key={label}
                      size="small"
                      variant="text"
                      disabled={!candidate}
                      onClick={() => onChange({
                        ...value,
                        price_distribution: { ...price, limit_price: candidate ?? "" },
                        venue_policy: { ...venue, price_match: null },
                      })}
                      sx={{ minWidth: 0, px: .75 }}
                    >
                      {label}
                    </Button>
                  ))}
                </Stack>
                  </>
                ) : null}
              </>
            )}
          </Box>
        ) : (
          <Stack spacing={1.5} sx={{ mt: 1.5 }}>
            <Box sx={compactFieldGridSx}>
              <TextField
                size="small"
                type="number"
                label="下限（USDT）"
                value={price.lower_price}
                onChange={(event) => onChange({ ...value, price_distribution: { ...price, lower_price: event.target.value } })}
                slotProps={{ htmlInput: { min: 0, step: "any" } }}
              />
              <TextField
                size="small"
                type="number"
                label="上限（USDT）"
                value={price.upper_price}
                onChange={(event) => onChange({ ...value, price_distribution: { ...price, upper_price: event.target.value } })}
                slotProps={{ htmlInput: { min: 0, step: "any" } }}
              />
              <TextField
                size="small"
                type="number"
                label="价格档位数"
                value={price.level_count}
                onChange={(event) => {
                  const count = Number(event.target.value);
                  const safeCount = Number.isInteger(count) ? count : 0;
                  const boundedCount = safeCount >= 2 && safeCount <= 50 ? safeCount : 0;
                  onChange({
                    ...value,
                    price_distribution: {
                      ...price,
                      level_count: safeCount,
                      custom_gap_weights: price.spacing_mode === "CUSTOM_WEIGHTS"
                        ? resized(price.custom_gap_weights, Math.max(0, boundedCount - 1), "1")
                        : [],
                    },
                    amount_distribution: amount.mode === "CUSTOM"
                      ? { ...amount, custom_notionals: resized(amount.custom_notionals, boundedCount, amount.base_notional || "10") }
                      : amount,
                  });
                }}
                slotProps={{ htmlInput: { min: 2, max: 50, step: 1 } }}
              />
            </Box>
            <Box sx={fieldGridSx}>
              <TextField
                select
                size="small"
                label="提交顺序"
                value={value.submission_order}
                onChange={(event) => onChange({
                  ...value,
                  submission_order: event.target.value as typeof value.submission_order,
                })}
              >
                <MenuItem value="LOW_TO_HIGH">低价 → 高价</MenuItem>
                <MenuItem value="HIGH_TO_LOW">高价 → 低价</MenuItem>
              </TextField>
            </Box>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: .65 }}>
              依次提交；前档闭合并确认保护后才提交下一档。
            </Typography>
            <Box
              component="details"
              sx={{
                borderTop: 1,
                borderColor: "divider",
                pt: .75,
                "& > summary": { cursor: "pointer", fontSize: 12, fontWeight: 700 },
              }}
            >
              <Box component="summary">
                价格间距 · {price.spacing_mode === "EQUAL"
                  ? "等距"
                  : price.spacing_mode === "LINEAR"
                    ? "线性比例"
                    : price.spacing_mode === "GEOMETRIC" ? "指数比例" : "自定义"}
              </Box>
              <Stack spacing={1.25} sx={{ mt: 1 }}>
              <Box sx={fieldGridSx}>
                <TextField
                select
                size="small"
                label="价格切分间距"
                value={price.spacing_mode}
                onChange={(event) => {
                  const mode = event.target.value as typeof price.spacing_mode;
                  onChange({
                    ...value,
                    price_distribution: {
                      ...price,
                      spacing_mode: mode,
                      custom_gap_weights: mode === "CUSTOM_WEIGHTS"
                        ? resized(
                          price.custom_gap_weights,
                          Number.isInteger(price.level_count)
                            && price.level_count >= 2
                            && price.level_count <= 50
                            ? price.level_count - 1
                            : 0,
                          "1",
                        )
                        : [],
                    },
                  });
                }}
              >
                <MenuItem value="EQUAL">等距</MenuItem>
                <MenuItem value="LINEAR">线性比例</MenuItem>
                <MenuItem value="GEOMETRIC">指数比例</MenuItem>
                <MenuItem value="CUSTOM_WEIGHTS">自定义比例</MenuItem>
              </TextField>
              {price.spacing_mode !== "EQUAL" ? (
                <TextField
                  select
                  size="small"
                  label="间距比例应用方向"
                  value={price.spacing_direction}
                  onChange={(event) => onChange({
                    ...value,
                    price_distribution: {
                      ...price,
                      spacing_direction: event.target.value as typeof price.spacing_direction,
                    },
                  })}
                >
                  <MenuItem value="LOW_TO_HIGH">从低价到高价</MenuItem>
                  <MenuItem value="HIGH_TO_LOW">从高价到低价</MenuItem>
                </TextField>
              ) : <Box />}
            </Box>
            {price.spacing_mode === "LINEAR" ? (
              <Box sx={fieldGridSx}>
                <TextField
                  size="small"
                  type="number"
                  label="首个间距权重"
                  value={price.linear_start_weight}
                  onChange={(event) => onChange({ ...value, price_distribution: { ...price, linear_start_weight: event.target.value } })}
                  slotProps={{ htmlInput: { step: "any" } }}
                />
                <TextField
                  size="small"
                  type="number"
                  label="每档权重增量"
                  value={price.linear_step}
                  onChange={(event) => onChange({ ...value, price_distribution: { ...price, linear_step: event.target.value } })}
                  helperText="可为负，但每个生成权重都必须大于 0"
                  slotProps={{ htmlInput: { step: "any" } }}
                />
              </Box>
            ) : null}
            {price.spacing_mode === "GEOMETRIC" ? (
              <TextField
                size="small"
                type="number"
                label="间距指数比例"
                value={price.geometric_ratio}
                onChange={(event) => onChange({ ...value, price_distribution: { ...price, geometric_ratio: event.target.value } })}
                helperText="大于 1，最大 100"
                slotProps={{ htmlInput: { min: 1, max: 100, step: "any" } }}
                sx={{ maxWidth: 360 }}
              />
            ) : null}
              {price.spacing_mode === "CUSTOM_WEIGHTS" ? (
              <Box>
                <Typography variant="caption" color="text.secondary">
                  依次填写相邻价格之间的比例，例如 5:4:3:2。
                </Typography>
                <Box sx={{ ...compactFieldGridSx, mt: 1 }}>
                  {price.custom_gap_weights.map((weight, index) => (
                    <TextField
                      key={`gap-${index}`}
                      size="small"
                      type="number"
                      label={`间距 ${index + 1} 权重`}
                      value={weight}
                      onChange={(event) => onChange({
                        ...value,
                        price_distribution: {
                          ...price,
                          custom_gap_weights: replaceAt(price.custom_gap_weights, index, event.target.value),
                        },
                      })}
                      slotProps={{ htmlInput: { min: 0, step: "any" } }}
                    />
                  ))}
                </Box>
              </Box>
              ) : null}
              </Stack>
            </Box>
          </Stack>
        )}
      </EditorSection>

      <EditorSection
        id="order-schedule-amount-title"
        title="下单金额"
      >
        <Box sx={price.kind === "SINGLE" && entryProgram.kind !== "TIME_SLICED" ? undefined : fieldGridSx}>
          {price.kind !== "SINGLE" || entryProgram.kind === "TIME_SLICED" ? (
          <TextField
            select
            size="small"
            label="下单额模式"
            value={amount.mode}
            onChange={(event) => {
              const mode = event.target.value as typeof amount.mode;
              onChange({
                ...value,
                amount_distribution: {
                  ...amount,
                  mode,
                  custom_notionals: mode === "CUSTOM"
                    ? resized(
                      amount.custom_notionals,
                      entryProgram.kind === "TIME_SLICED"
                        ? entryProgram.slice_count
                        : price.kind === "SINGLE"
                        ? 1
                        : Number.isInteger(price.level_count)
                          && price.level_count >= 2
                          && price.level_count <= 50
                          ? price.level_count
                          : 0,
                      amount.base_notional || "10",
                    )
                    : [],
                },
              });
            }}
          >
            <MenuItem value="FIXED">固定金额</MenuItem>
            <MenuItem value="LINEAR">线性增长</MenuItem>
            <MenuItem value="EXPONENTIAL">指数增长</MenuItem>
            <MenuItem value="CUSTOM">逐档自定义</MenuItem>
          </TextField>
          ) : null}
          <TextField
            size="small"
            type="number"
            label={price.kind === "SINGLE"
              ? entryProgram.kind === "TIME_SLICED"
                ? amount.mode === "FIXED" ? "每笔金额（USDT）" : "起始金额（USDT）"
                : "下单金额（USDT）"
              : amount.mode === "FIXED" ? "每档金额（USDT）" : "起始金额（USDT）"}
            value={amount.base_notional}
            disabled={amount.mode === "CUSTOM"}
            onChange={(event) => onChange({ ...value, amount_distribution: { ...amount, base_notional: event.target.value } })}
            slotProps={{ htmlInput: { min: 0, step: "any" } }}
          />
        </Box>
        {amount.mode !== "FIXED" && amount.mode !== "CUSTOM" ? (
          <Box sx={{ ...fieldGridSx, mt: 1.5 }}>
            {amount.mode === "LINEAR" ? (
              <TextField
                size="small"
                type="number"
                label={entryProgram.kind === "TIME_SLICED"
                  ? "每笔金额增量（USDT）"
                  : "每档金额增量（USDT）"}
                value={amount.linear_step}
                onChange={(event) => onChange({ ...value, amount_distribution: { ...amount, linear_step: event.target.value } })}
                slotProps={{ htmlInput: { min: 0, step: "any" } }}
              />
            ) : (
              <TextField
                size="small"
                type="number"
                label="金额指数比例"
                value={amount.exponential_ratio}
                onChange={(event) => onChange({ ...value, amount_distribution: { ...amount, exponential_ratio: event.target.value } })}
                helperText="大于 1，最大 100"
                slotProps={{ htmlInput: { min: 1, max: 100, step: "any" } }}
              />
            )}
            <TextField
              select
              size="small"
              label="金额增长方向"
              value={amount.direction}
              onChange={(event) => onChange({
                ...value,
                amount_distribution: {
                  ...amount,
                  direction: event.target.value as typeof amount.direction,
                },
              })}
            >
              <MenuItem value="LOW_TO_HIGH">
                {entryProgram.kind === "TIME_SLICED" ? "从首笔到末笔" : "从低价到高价"}
              </MenuItem>
              <MenuItem value="HIGH_TO_LOW">
                {entryProgram.kind === "TIME_SLICED" ? "从末笔到首笔" : "从高价到低价"}
              </MenuItem>
            </TextField>
          </Box>
        ) : null}
        {amount.mode === "CUSTOM" ? (
          <Box sx={{ mt: 1.5 }}>
            <Typography variant="caption" color="text.secondary">
              {entryProgram.kind === "TIME_SLICED"
                ? "按首笔到末笔的时间顺序填写。"
                : "按低价到高价顺序逐档填写。"}
            </Typography>
            <Box sx={{ ...compactFieldGridSx, mt: 1 }}>
              {amount.custom_notionals.map((notional, index) => (
                <TextField
                  key={`notional-${index}`}
                  size="small"
                  type="number"
                  label={entryProgram.kind === "TIME_SLICED"
                    ? `第 ${index + 1} 笔（USDT）`
                    : `档位 ${index + 1}（USDT）`}
                  value={notional}
                  onChange={(event) => onChange({
                    ...value,
                    amount_distribution: {
                      ...amount,
                      custom_notionals: replaceAt(amount.custom_notionals, index, event.target.value),
                    },
                  })}
                  slotProps={{ htmlInput: { min: 0, step: "any" } }}
                />
              ))}
            </Box>
          </Box>
        ) : null}
      </EditorSection>

      {venue.order_type === "LIMIT" ? (
      <EditorSection
        id="order-schedule-venue-title"
        title="交易所模式"
      >
        {venue.time_in_force === "GTC"
          && entryProgram.kind !== "TIME_SLICED"
          && venue.price_match === null ? (
        <Stack direction="row" spacing={1} sx={{ alignItems: "center", justifyContent: "space-between" }}>
          <FormControlLabel
            control={(
              <Switch
                size="small"
                checked={venue.post_only}
                onChange={(event) => onChange({
                  ...value,
                  price_distribution: event.target.checked
                    && price.kind === "SINGLE"
                    && price.limit_price === null
                    ? { ...price, limit_price: referencePrice ?? "" }
                    : price,
                  venue_policy: {
                    ...venue,
                    post_only: event.target.checked,
                    price_match: event.target.checked ? null : venue.price_match,
                  },
                })}
              />
            )}
            label="Maker only"
            sx={{ m: 0 }}
          />
          <Typography variant="caption" color="text.secondary" sx={{ alignSelf: "center" }}>
            {venue.post_only
              ? `只允许 Maker 成交；正常拒绝最多自动重挂 ${POST_ONLY_RETRY_MAX_ATTEMPTS} 次`
              : "允许按当前订单类型执行"}
          </Typography>
        </Stack>
        ) : null}
        <Stack spacing={1.25} sx={{ mt: 1 }}>
            <Box sx={fieldGridSx}>
              <TextField
                select
                size="small"
                label="有效方式"
                value={venue.time_in_force ?? ""}
                onChange={(event) => {
                  const next = event.target.value as "GTC" | "GTD" | "IOC" | "FOK";
                  onChange({
                    ...value,
                    venue_policy: {
                      ...venue,
                      time_in_force: next,
                      post_only: next === "GTC" ? venue.post_only : false,
                      expire_at: next === "GTD" ? venue.expire_at : null,
                    },
                    dynamic_rules: next === "GTC"
                      ? value.dynamic_rules
                      : withoutDynamicRule(
                        value.dynamic_rules,
                        "REPRICE_ENTRY",
                      ),
                  });
                }}
              >
                {entryProgram.kind !== "TIME_SLICED" ? (
                  [
                    <MenuItem key="GTC" value="GTC">GTC · 持续有效</MenuItem>,
                    <MenuItem key="GTD" value="GTD">GTD · 指定到期</MenuItem>,
                  ]
                ) : null}
                <MenuItem value="IOC">IOC · 立即成交余量撤销</MenuItem>
                <MenuItem value="FOK">FOK · 全成或全撤</MenuItem>
              </TextField>
            </Box>
            {venue.time_in_force === "GTD" ? (
              <TextField
                size="small"
                type="datetime-local"
                label="GTD 到期时间"
                value={localDateTimeValue(venue.expire_at)}
                onChange={(event) => onChange({
                  ...value,
                  venue_policy: { ...venue, expire_at: isoFromLocalDateTime(event.target.value) },
                })}
                helperText="到期时间须晚于本次预览和启动时刻至少 10 分钟"
                slotProps={{ inputLabel: { shrink: true } }}
              />
            ) : null}
        </Stack>
      </EditorSection>
      ) : null}

      <Box sx={{ px: 1.5, py: 1.25, borderBottom: 1, borderColor: "divider" }}>
        <Button
          type="button"
          variant="outlined"
          fullWidth
          aria-expanded={entryCatalogOpen}
          onClick={() => setEntryCatalogOpen((open) => !open)}
          sx={{ justifyContent: "flex-start", textTransform: "none", fontWeight: 800 }}
        >
          ＋ 添加入场条件或管理规则
        </Button>
        {entryCatalogOpen ? (
          <Stack
            spacing={1.25}
            sx={{ ...surfaceFrameSx, mt: 1, p: 1.25 }}
            aria-label="入场扩展目录"
          >
            <Box>
              <Typography variant="caption" color="text.secondary">
                入场前置条件 · 可叠加并选择全部或任一满足
              </Typography>
              <Stack spacing={.25}>
                <Button
                  type="button"
                  size="small"
                  fullWidth
                  disabled={Boolean(markCondition)}
                  onClick={() => {
                    setConditionEnabled("MARK_PRICE", true);
                    setEntryCatalogOpen(false);
                  }}
                  sx={{ justifyContent: "flex-start", textTransform: "none" }}
                >
                  到价触发 · 标记价达到指定价格
                </Button>
                <Button
                  type="button"
                  size="small"
                  fullWidth
                  disabled={Boolean(spreadCondition)}
                  onClick={() => {
                    setConditionEnabled("SPREAD_BPS", true);
                    setEntryCatalogOpen(false);
                  }}
                  sx={{ justifyContent: "flex-start", textTransform: "none" }}
                >
                  价差限制 · 买卖价差不超过上限
                </Button>
                <Button
                  type="button"
                  size="small"
                  fullWidth
                  disabled={Boolean(moveCondition)}
                  onClick={() => {
                    setConditionEnabled("PRICE_MOVE_BPS", true);
                    setEntryCatalogOpen(false);
                  }}
                  sx={{ justifyContent: "flex-start", textTransform: "none" }}
                >
                  短时异动 · 指定窗口内达到涨跌幅
                </Button>
                <Button
                  type="button"
                  size="small"
                  fullWidth
                  disabled={Boolean(closedBarCondition)}
                  onClick={() => {
                    setConditionEnabled("CLOSED_BAR_PRICE_15M", true);
                    setEntryCatalogOpen(false);
                  }}
                  sx={{ justifyContent: "flex-start", textTransform: "none" }}
                >
                  15m 收盘确认 · 已闭合 K 线收盘达到指定价格
                </Button>
              </Stack>
            </Box>
            <Box>
              <Typography variant="caption" color="text.secondary">
                撤单与失效 · 可组合
              </Typography>
              <Stack spacing={.25}>
                <Button
                  type="button"
                  size="small"
                  fullWidth
                  disabled={Boolean(shockRule)}
                  onClick={() => {
                    onChange({
                      ...value,
                      dynamic_rules: withDynamicRule(value.dynamic_rules, {
                        kind: "CANCEL_ON_SHOCK",
                        window_seconds: 30,
                        adverse_move_bps: "50",
                        invalidation_price: generatedOffsetPrice(
                          displayReferencePrice,
                          direction === "SHORT" ? 1.01 : 0.99,
                          priceTickSize,
                        ),
                        opportunity_missed_price: null,
                        max_triggers: 1,
                      }),
                    });
                    setEntryCatalogOpen(false);
                  }}
                  sx={{ justifyContent: "flex-start", textTransform: "none" }}
                >
                  行情失效 · 失效价、错过价或短时急反时取消
                </Button>
                <Button
                  type="button"
                  size="small"
                  fullWidth
                  disabled={
                    venue.order_type !== "LIMIT"
                    || entryProgram.kind === "TIME_SLICED"
                    || Boolean(expireRule)
                  }
                  onClick={() => {
                    onChange({
                      ...value,
                      dynamic_rules: withDynamicRule(value.dynamic_rules, {
                        kind: "EXPIRE_REMAINING",
                        after_seconds: 300,
                      }),
                    });
                    setEntryCatalogOpen(false);
                  }}
                  sx={{ justifyContent: "flex-start", textTransform: "none" }}
                >
                  未成交到期 · 首次提交后撤销剩余挂单
                </Button>
              </Stack>
              {venue.order_type !== "LIMIT" ? (
                <Typography variant="caption" color="text.secondary">
                  未成交到期仅适用于限价入场。
                </Typography>
              ) : null}
            </Box>
            <Box>
              <Typography variant="caption" color="text.secondary">
                挂单管理 · 有界撤单重挂
              </Typography>
              <Button
                type="button"
                size="small"
                fullWidth
                disabled={!repriceCompatible}
                onClick={() => {
                  onChange({
                    ...value,
                    dynamic_rules: withDynamicRule(value.dynamic_rules, {
                      kind: "REPRICE_ENTRY",
                      trigger_distance_bps: "5",
                      book_offset_bps: "1",
                      maximum_total_move_bps: "30",
                      minimum_update_interval_seconds: 5,
                      max_adjustments: 3,
                    }),
                  });
                  setEntryCatalogOpen(false);
                }}
                sx={{ justifyContent: "flex-start", textTransform: "none" }}
              >
                跟随同侧盘口 · 撤单确认后按最新买一/卖一重挂
              </Button>
              <Typography variant="caption" color="text.secondary">
                {repriceCompatible
                  ? " 适用于一次性或事件触发的显式 GTC 限价单；每次先确认原订单终态，再按预设的次数、间隔与总移动上限重挂。"
                  : " 当前入场方式也不兼容；市价、区间、时间分批、IOC/FOK/GTD 与 priceMatch 不可组合。"}
              </Typography>
            </Box>
          </Stack>
        ) : null}
      </Box>

      {marketConditionCount > 0 ? (
      <EditorSection
        id="order-schedule-condition-title"
        title="入场前置条件"
        description="事实缺失、冲突或过期时结果为未知，不形成新增风险动作。"
      >
        <TextField
          select
          size="small"
          label="条件组合"
          value={value.entry_conditions.operator}
          onChange={(event) => {
            const operator = event.target.value as "ALL" | "ANY";
            onChange({
              ...value,
              entry_conditions: {
                ...value.entry_conditions,
                operator,
                items: normalizedDirectConditionItems(
                  operator,
                  value.entry_conditions.items,
                ),
              },
            });
          }}
          sx={{ width: { xs: "100%", sm: 260 } }}
        >
          <MenuItem value="ALL">全部满足（ALL）</MenuItem>
          <MenuItem value="ANY">任一满足（ANY）</MenuItem>
        </TextField>
        {value.entry_conditions.operator === "ANY" ? (
          <Alert severity="info" variant="outlined" sx={{ mt: 1.25 }}>
            ANY 只在已选择的市场条件之间取“任一满足”；没有选择市场条件时才表示立即执行。
          </Alert>
        ) : null}
        {entrySignalWarning ? (
          <Alert severity="warning" variant="outlined" sx={{ mt: 1.25 }}>
            {entrySignalWarning}
          </Alert>
        ) : null}
        <Stack spacing={1.25} sx={{ mt: 1.25 }}>
          {markCondition ? (
          <Box sx={{ ...surfaceFrameSx, p: 1.25 }}>
            <Stack direction="row" spacing={1} sx={{ alignItems: "center", justifyContent: "space-between" }}>
              <Typography variant="body2" sx={{ fontWeight: 750 }}>到价触发</Typography>
              <Button
                type="button"
                size="small"
                color="inherit"
                aria-label="移除到价触发"
                onClick={() => setConditionEnabled("MARK_PRICE", false)}
              >
                移除
              </Button>
            </Stack>
              <Box sx={{ ...fieldGridSx, mt: .75 }}>
                <TextField
                  select
                  size="small"
                  label="比较方式"
                  value={markCondition.comparator}
                  onChange={(event) => onChange({
                    ...value,
                    entry_conditions: {
                      ...value.entry_conditions,
                      items: withCondition(value.entry_conditions.items, {
                        ...markCondition,
                        comparator: event.target.value as "GTE" | "LTE",
                      }),
                    },
                  })}
                >
                  <MenuItem value="GTE">大于等于</MenuItem>
                  <MenuItem value="LTE">小于等于</MenuItem>
                </TextField>
                <TextField
                  size="small"
                  type="number"
                  label="标记价格（USDT）"
                  value={markCondition.price}
                  onChange={(event) => onChange({
                    ...value,
                    entry_conditions: {
                      ...value.entry_conditions,
                      items: withCondition(value.entry_conditions.items, { ...markCondition, price: event.target.value }),
                    },
                  })}
                  slotProps={{ htmlInput: { min: 0, step: "any" } }}
                />
              </Box>
          </Box>
          ) : null}

          {closedBarCondition ? (
          <Box sx={{ ...surfaceFrameSx, p: 1.25 }}>
            <Stack direction="row" spacing={1} sx={{ alignItems: "center", justifyContent: "space-between" }}>
              <Box sx={{ minWidth: 0 }}>
                <Typography variant="body2" sx={{ fontWeight: 750 }}>15m 收盘确认</Typography>
                <Typography variant="caption" color="text.secondary">
                  只判断最近一根完整闭合的 15m K 线；正在形成的 K 线不参与。
                </Typography>
              </Box>
              <Button
                type="button"
                size="small"
                color="inherit"
                aria-label="移除 15m 收盘确认"
                onClick={() => setConditionEnabled("CLOSED_BAR_PRICE_15M", false)}
              >
                移除
              </Button>
            </Stack>
              <Box sx={{ ...fieldGridSx, mt: .75 }}>
                <TextField
                  select
                  size="small"
                  label="比较方式"
                  value={closedBarCondition.comparator}
                  onChange={(event) => onChange({
                    ...value,
                    entry_conditions: {
                      ...value.entry_conditions,
                      items: withCondition(value.entry_conditions.items, {
                        ...closedBarCondition,
                        comparator: event.target.value as "GTE" | "LTE",
                      }),
                    },
                  })}
                >
                  <MenuItem value="GTE">收盘大于等于</MenuItem>
                  <MenuItem value="LTE">收盘小于等于</MenuItem>
                </TextField>
                <TextField
                  size="small"
                  type="number"
                  label="收盘阈值（USDT）"
                  value={closedBarCondition.price}
                  onChange={(event) => onChange({
                    ...value,
                    entry_conditions: {
                      ...value.entry_conditions,
                      items: withCondition(value.entry_conditions.items, {
                        ...closedBarCondition,
                        price: event.target.value,
                      }),
                    },
                  })}
                  slotProps={{ htmlInput: { min: 0, step: "any" } }}
                />
              </Box>
          </Box>
          ) : null}

          {spreadCondition ? (
          <Box sx={{ ...surfaceFrameSx, p: 1.25 }}>
            <Stack direction="row" spacing={1} sx={{ alignItems: "center", justifyContent: "space-between" }}>
              <Typography variant="body2" sx={{ fontWeight: 750 }}>价差限制</Typography>
              <Button
                type="button"
                size="small"
                color="inherit"
                aria-label="移除价差限制"
                onClick={() => setConditionEnabled("SPREAD_BPS", false)}
              >
                移除
              </Button>
            </Stack>
              <TextField
                size="small"
                type="number"
                label="最大价差（bps）"
                value={spreadCondition.maximum_bps}
                onChange={(event) => onChange({
                  ...value,
                  entry_conditions: {
                    ...value.entry_conditions,
                    items: withCondition(value.entry_conditions.items, { ...spreadCondition, maximum_bps: event.target.value }),
                  },
                })}
                slotProps={{ htmlInput: { min: 0, step: "any" } }}
                sx={{ mt: .75, width: { xs: "100%", sm: 320 } }}
              />
          </Box>
          ) : null}

          {moveCondition ? (
          <Box sx={{ ...surfaceFrameSx, p: 1.25 }}>
            <Stack direction="row" spacing={1} sx={{ alignItems: "center", justifyContent: "space-between" }}>
              <Typography variant="body2" sx={{ fontWeight: 750 }}>短时异动</Typography>
              <Button
                type="button"
                size="small"
                color="inherit"
                aria-label="移除短时异动"
                onClick={() => setConditionEnabled("PRICE_MOVE_BPS", false)}
              >
                移除
              </Button>
            </Stack>
              <Box sx={{ ...compactFieldGridSx, mt: .75 }}>
                <TextField
                  select
                  size="small"
                  label="比较方式"
                  value={moveCondition.comparator}
                  onChange={(event) => onChange({
                    ...value,
                    entry_conditions: {
                      ...value.entry_conditions,
                      items: withCondition(value.entry_conditions.items, {
                        ...moveCondition,
                        comparator: event.target.value as "GTE" | "LTE" | "DROP_GTE" | "ABS_GTE",
                      }),
                    },
                  })}
                >
                  <MenuItem value="GTE">上涨至少</MenuItem>
                  <MenuItem value="DROP_GTE">下跌至少</MenuItem>
                  <MenuItem value="LTE">有符号变动不高于</MenuItem>
                  <MenuItem value="ABS_GTE">绝对变动至少</MenuItem>
                </TextField>
                <TextField
                  size="small"
                  type="number"
                  label="观察窗口（秒）"
                  value={moveCondition.window_seconds}
                  onChange={(event) => onChange({
                    ...value,
                    entry_conditions: {
                      ...value.entry_conditions,
                      items: withCondition(value.entry_conditions.items, { ...moveCondition, window_seconds: Number(event.target.value) }),
                    },
                  })}
                  slotProps={{ htmlInput: { min: 1, max: 300, step: 1 } }}
                />
                <TextField
                  size="small"
                  type="number"
                  label="变动阈值（bps）"
                  value={moveCondition.threshold_bps}
                  onChange={(event) => onChange({
                    ...value,
                    entry_conditions: {
                      ...value.entry_conditions,
                      items: withCondition(value.entry_conditions.items, { ...moveCondition, threshold_bps: event.target.value }),
                    },
                  })}
                  slotProps={{ htmlInput: { min: 0, step: "any" } }}
                />
              </Box>
          </Box>
          ) : null}
        </Stack>
      </EditorSection>
      ) : null}
            </>
          ) : null}

      {activeMilestone === 1 ? (
        <EditorSection
          id="order-schedule-protection-title"
          title="成交后立即保护"
        >
        <Typography component="h3" variant="body2" sx={{ display: "block", mb: 1, fontWeight: 800 }}>
          初始止损
        </Typography>
        <Box
          data-testid="initial-stop-recommendations"
          sx={{ mb: 1.25, pb: 1.25, borderBottom: 1, borderColor: "divider" }}
        >
          <Stack
            direction={{ xs: "column", sm: "row" }}
            spacing={1}
            sx={{
              alignItems: { xs: "stretch", sm: "center" },
              justifyContent: "space-between",
              mb: .6,
            }}
          >
            <Typography component="h4" variant="body2" sx={{ fontWeight: 800, whiteSpace: "nowrap" }}>
              止损候选
            </Typography>
            <ToggleButtonGroup
              exclusive
              size="small"
              value={stopReferenceInterval}
              aria-label="止损参考K线周期"
              onChange={(_event, interval: MarketInterval | null) => {
                if (interval !== null) onStopReferenceIntervalChange(interval);
              }}
              sx={{
                ...outlinedChoiceSx,
                flexWrap: "nowrap",
                width: { xs: "100%", sm: "auto" },
                "& .MuiToggleButton-root": {
                  ...outlinedChoiceSx["& .MuiToggleButton-root"],
                  flex: { xs: 1, sm: "0 0 auto" },
                  minWidth: 0,
                  px: { xs: .5, sm: 1.25 },
                },
              }}
            >
              {(["1m", "5m", "15m", "1h", "4h", "1d"] as const).map((interval) => (
                <ToggleButton key={interval} value={interval} aria-label={`${interval}止损参考`}>
                  {interval}
                </ToggleButton>
              ))}
            </ToggleButtonGroup>
          </Stack>
          {stopRecommendations[0] ? (
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: .5 }}>
              {stopReferenceInterval} K 线 · 截止 {formatUserVisibleTime(stopRecommendations[0].evidenceCutoff)}
            </Typography>
          ) : null}
          {stopRecommendations.length > 0 ? (
            <Stack spacing={0}>
              {stopRecommendations.map((recommendation) => {
                const selected = approximatelyEqual(
                  value.protection_policy.initial_stop.distance_bps,
                  recommendation.distanceBpsInput,
                );
                const entryFeeBps = Number(selectedEntryFeeBps);
                const exitFeeBps = Number(selectedExitFeeBps);
                const exitNotional = (
                  fullFillEntryProjection.quantity * recommendation.price
                );
                const estimatedLoss = fullFillEntryProjection.quantity > 0
                  && fullFillEntryProjection.notional > 0
                  && Number.isFinite(entryFeeBps)
                  && Number.isFinite(exitFeeBps)
                  ? Math.abs(fullFillEntryProjection.notional - exitNotional)
                    + fullFillEntryProjection.notional * entryFeeBps / 10_000
                    + exitNotional * exitFeeBps / 10_000
                  : null;
                const displayedPrice = roundedTradingPriceEstimate(
                  recommendation.price,
                  priceTickSize,
                );
                const distanceBase = venue.order_type === "MARKET"
                  ? Number(displayReferencePrice)
                  : Number(fullFillProtectionEstimate?.average_entry_price);
                const priceDistance = Number.isFinite(distanceBase) && distanceBase > 0
                  ? Math.abs(recommendation.price - distanceBase)
                  : null;
                return (
                  <Box
                    key={recommendation.id}
                    data-testid={`initial-stop-recommendation-${recommendation.kind.toLowerCase()}`}
                    sx={{
                      display: "grid",
                      gridTemplateColumns: "minmax(0, 1fr) auto",
                      columnGap: 1.25,
                      rowGap: .45,
                      alignItems: "start",
                      py: 1,
                      borderTop: 1,
                      borderColor: selected ? "primary.main" : "divider",
                      bgcolor: "transparent",
                    }}
                  >
                    <Box sx={{ minWidth: 0, display: "grid", gridTemplateColumns: "minmax(0, 1fr) auto", columnGap: 1, rowGap: .35 }}>
                      <Stack
                        direction="row"
                        spacing={.45}
                        sx={{ alignItems: "center", minWidth: 0 }}
                      >
                        <Typography variant="body2" sx={{ fontWeight: 800 }}>
                          {recommendation.label}
                        </Typography>
                        <Tooltip
                          arrow
                          placement="top"
                          title={(
                            <Box sx={{ maxWidth: 320 }}>
                              <Typography variant="caption" sx={{ display: "block", fontWeight: 800 }}>
                                止损逻辑
                              </Typography>
                              <Typography variant="caption" sx={{ display: "block" }}>
                                {recommendation.evidence}
                              </Typography>
                              <Typography variant="caption" sx={{ display: "block", mt: .5 }}>
                                按全档成交后的预计均价换算。实际分批成交后按本计划当前均价与已入场数量重算，并受创建时最大预计亏损约束。
                              </Typography>
                            </Box>
                          )}
                        >
                          <IconButton
                            size="small"
                            aria-label={`${recommendation.label}止损逻辑`}
                            sx={{
                              width: 24,
                              height: 24,
                              p: 0,
                              border: 0,
                              borderRadius: "50%",
                              color: "text.secondary",
                              flexShrink: 0,
                            }}
                          >
                            <InfoOutlined sx={{ fontSize: 16 }} />
                          </IconButton>
                        </Tooltip>
                      </Stack>
                      <Typography
                        className="mono"
                        variant="body2"
                        sx={{ fontWeight: 800, fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap", textAlign: "right" }}
                      >
                        {displayedPrice} USDT
                      </Typography>
                      <Typography variant="caption" color="text.secondary" sx={{ gridColumn: "1 / -1", minWidth: 0 }}>
                        {priceDistance === null
                          ? `${recommendation.distanceBpsInput} bps`
                          : `距${venue.order_type === "MARKET" ? "现价" : "预计均价"} ${roundedTradingPriceEstimate(priceDistance, priceTickSize)} USDT · ${recommendation.distanceBpsInput} bps`}
                        {estimatedLoss === null
                          ? ""
                          : <> · <Box component="span" className="market-tone-down" sx={{ fontWeight: 700 }}>{`约 -${quoteCurrencyEstimate(estimatedLoss)} USDT`}</Box></>}
                      </Typography>
                    </Box>
                    <Button
                      type="button"
                      size="small"
                      variant={selected ? "contained" : "outlined"}
                      disabled={selected || previewStale || preview.isFetching || preview.isError}
                      aria-label={`采用${recommendation.label} ${displayedPrice} USDT`}
                      onClick={() => onChange({
                        ...value,
                        protection_policy: {
                          ...value.protection_policy,
                          initial_stop: {
                            ...value.protection_policy.initial_stop,
                            distance_bps: recommendation.distanceBpsInput,
                          },
                        },
                      })}
                      sx={{
                        width: 64,
                        minWidth: 64,
                        flexShrink: 0,
                        ...(selected ? {
                          "&.Mui-disabled": {
                            bgcolor: "primary.main",
                            color: "primary.contrastText",
                            opacity: 1,
                          },
                        } : {}),
                      }}
                    >
                      {selected ? "已采用" : "采用"}
                    </Button>
                  </Box>
                );
              })}
            </Stack>
          ) : (
            <Typography variant="caption" color="text.secondary" aria-live="polite">
              {stopReferenceLoading
                ? `正在计算 ${stopReferenceInterval} 止损参考…`
                : stopReferenceUnavailable
                  ? `${stopReferenceInterval} 止损参考暂不可用；当前固定止损不受影响。`
                  : marketContext
                    ? "当前周期未形成位于入场价外侧的候选止损。"
                    : "等待归一化入场档位与当前周期行情后显示候选位置。"}
            </Typography>
          )}
        </Box>
        {protectionLocationRisks.length > 0 ? (
          <Box data-testid="protection-location-risk" sx={{ mb: 1.25, pl: 1, borderLeft: 3, borderColor: "warning.main" }}>
            <Typography variant="caption" sx={{ fontWeight: 800 }}>保护位置风险</Typography>
            <Box component="ul" sx={{ m: .4, pl: 2, "& li": { fontSize: 12 } }}>
              {protectionLocationRisks.map((risk) => <li key={risk}>{risk}</li>)}
            </Box>
          </Box>
        ) : null}
        <Box>
          <TextField
            fullWidth
            size="small"
            type="number"
            label="初始止损距离（bps）"
            value={value.protection_policy.initial_stop.distance_bps}
            error={!protectionLocalReady || stopExceedsDisciplineLoss}
            onChange={(event) => onChange({
              ...value,
              protection_policy: {
                ...value.protection_policy,
                initial_stop: {
                  ...value.protection_policy.initial_stop,
                  distance_bps: event.target.value,
                },
              },
            })}
            helperText={stopExceedsDisciplineLoss && maximumDisciplineStopDistanceBps !== null
              ? lossLimitedEntry.maximumEntryNotional
                ? `超过纪律最大 ${compactDecimal(maximumDisciplineStopDistanceBps)} bps；实际最大开仓额度将按 ${quoteCurrencyEstimate(lossLimitedEntry.maximumEntryNotional)} USDT 核算。`
                : `超过纪律最大 ${compactDecimal(maximumDisciplineStopDistanceBps)} bps。`
              : maximumDisciplineStopDistanceBps !== null
              ? `纪律最大 ${compactDecimal(maximumDisciplineStopDistanceBps)} bps`
              : protectionLocalReady
              ? undefined
              : requiredFeeEvidenceMissing || feeEvidenceLoading || feeEvidenceUnavailable
                ? "等待同环境手续费参考，以计算费用后最大预计亏损"
                : "必须大于 0 且低于 10000 bps"}
            slotProps={{ htmlInput: { min: 0, step: "any" } }}
          />
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: .45 }}>
            标记价格触发
          </Typography>
        </Box>
        <Box
          data-testid="initial-stop-projection"
          sx={configuredRuleSx}
        >
          <Typography variant="body2" sx={{ fontWeight: 750, mb: .8 }}>
            止损价格与风险预览
          </Typography>
          {fullFillProtectionEstimate ? (
            <Box
              component="dl"
              sx={{
                m: 0,
                display: "grid",
                gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
                gap: .9,
              }}
            >
              {[
                [
                  "全档成交预计均价",
                  `${roundedTradingPriceEstimate(fullFillProtectionEstimate.average_entry_price, priceTickSize)} USDT`,
                  undefined,
                ],
                [
                  "全档成交预计止损",
                  `${roundedTradingPriceEstimate(fullFillProtectionEstimate.stop_price, priceTickSize)} USDT`,
                  undefined,
                ],
                [
                  venue.order_type === "MARKET" ? "距实时现价" : "距预计均价",
                  (() => {
                    const base = venue.order_type === "MARKET"
                      ? Number(displayReferencePrice)
                      : Number(fullFillProtectionEstimate.average_entry_price);
                    const stop = Number(fullFillProtectionEstimate.stop_price);
                    return Number.isFinite(base) && Number.isFinite(stop)
                      ? `${roundedTradingPriceEstimate(Math.abs(base - stop), priceTickSize)} USDT`
                      : "待行情";
                  })(),
                  undefined,
                ],
                ...(maximumDisciplineStopPrice === null ? [] : [[
                  "纪律最大止损",
                  `${roundedTradingPriceEstimate(maximumDisciplineStopPrice, priceTickSize)} USDT`,
                  undefined,
                ]]),
                ...(lossLimitedEntry.constrained && lossLimitedEntry.maximumEntryNotional ? [[
                  "核算后最大开仓额度",
                  `${quoteCurrencyEstimate(lossLimitedEntry.maximumEntryNotional)} USDT`,
                  undefined,
                ]] : []),
                [
                  "预计进出手续费",
                  `-${quoteCurrencyEstimate(
                    Number(fullFillProtectionEstimate.estimated_entry_fee)
                    + Number(fullFillProtectionEstimate.estimated_exit_fee),
                  )} USDT`,
                  "market-tone-down",
                ],
                [
                  "最大预计亏损",
                  `-${quoteCurrencyEstimate(fullFillProtectionEstimate.maximum_projected_loss)} USDT`,
                  "market-tone-down",
                ],
              ].map(([label, display, toneClassName]) => (
                <Box key={label} sx={{ minWidth: 0 }}>
                  <Typography component="dt" variant="caption" color="text.secondary">
                    {label}
                  </Typography>
                  <Typography
                    component="dd"
                    variant="body2"
                    className={toneClassName}
                    sx={{ m: 0, fontWeight: 750, overflowWrap: "anywhere" }}
                  >
                    {display}
                  </Typography>
                </Box>
              ))}
            </Box>
          ) : (
            <Typography variant="caption" color="text.secondary" aria-live="polite">
              完成有效档位预览并取得同环境手续费参考后显示费用后最大预计亏损。
            </Typography>
          )}
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: .8 }}>
            最大预计亏损按全部标准化档位、全档成交均价、止损价及预计进出手续费计算；不含触发后滑点、跳空和未来资金费，因此不是成交结果保证。
          </Typography>
        </Box>
        <Box sx={{ mt: 1.25 }}>
          <Button
            type="button"
            variant="outlined"
            fullWidth
            aria-expanded={protectionCatalogOpen}
            onClick={() => setProtectionCatalogOpen((open) => !open)}
            sx={{ justifyContent: "flex-start", textTransform: "none", fontWeight: 800 }}
          >
            ＋ 添加成交后动态止损
          </Button>
          {protectionCatalogOpen ? (
            <Stack
              spacing={1.25}
              sx={{ ...surfaceFrameSx, mt: 1, p: 1.25 }}
              aria-label="动态止损目录"
            >
              <Box>
                <Typography variant="caption" color="text.secondary">
                  离散触发 · 以下方式互斥
                </Typography>
                <Stack spacing={.25}>
                  {[
                    {
                      label: "盈亏平衡止损 · 盈利 1R 后移到入场价",
                      steps: [{ trigger_r: "1", stop_r: "0" }],
                    },
                    {
                      label: "保底盈利止损 · 盈利 1R 后至少保住 0.5R",
                      steps: [{ trigger_r: "1", stop_r: "0.5" }],
                    },
                    {
                      label: "阶梯保盈 · 1R 保本，2R 后保住 1R",
                      steps: [
                        { trigger_r: "1", stop_r: "0" },
                        { trigger_r: "2", stop_r: "1" },
                      ],
                    },
                  ].map((preset) => (
                    <Button
                      key={preset.label}
                      type="button"
                      size="small"
                      fullWidth
                      onClick={() => {
                        onChange({
                          ...value,
                          dynamic_rules: withDynamicRule(
                            withoutDynamicRule(value.dynamic_rules, "PROFIT_LOCK"),
                            {
                              kind: "STEPPED_PROTECTION",
                              steps: preset.steps,
                              minimum_update_interval_seconds: 5,
                              max_adjustments: 8,
                            },
                          ),
                        });
                        setProtectionCatalogOpen(false);
                      }}
                      sx={{ justifyContent: "flex-start", textTransform: "none" }}
                    >
                      {preset.label}
                    </Button>
                  ))}
                </Stack>
              </Box>
              <Box>
                <Typography variant="caption" color="text.secondary">
                  连续收紧 · 以下方式与离散触发互斥
                </Typography>
                <Stack spacing={.25}>
                  <Button
                    type="button"
                    size="small"
                    fullWidth
                    onClick={() => addProfitLock("RATIO")}
                    sx={{ justifyContent: "flex-start", textTransform: "none" }}
                  >
                    峰值比例锁盈 · 达到 1R 后锁定峰值盈利的 50%
                  </Button>
                  <Button
                    type="button"
                    size="small"
                    fullWidth
                    onClick={() => addProfitLock("FIXED_GIVEBACK")}
                    sx={{ justifyContent: "flex-start", textTransform: "none" }}
                  >
                    固定回撤锁盈 · 达到 1R 后最多回吐 0.5R
                  </Button>
                </Stack>
              </Box>
              <Typography variant="caption" color="text.secondary">
                动态止损按每笔实际成交价与初始风险 R 计算，不使用 K 线周期；详细阈值可在下一步调整。
              </Typography>
            </Stack>
          ) : null}
          {steppedRule || profitLockRule ? (
            <Box sx={{ ...configuredRuleSx }}>
              <Stack direction="row" spacing={1} sx={{ justifyContent: "space-between", alignItems: "center" }}>
                <Box sx={{ minWidth: 0 }}>
                  <Typography variant="body2" sx={{ fontWeight: 800 }}>
                    {steppedRule ? "离散动态止损" : "连续动态止损"}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    {steppedRule
                      ? steppedRule.steps.map((step) => `${step.trigger_r}R→${step.stop_r}R`).join(" · ")
                      : profitLockRule?.mode === "RATIO"
                        ? `达到 ${profitLockRule.activation_r}R 后锁定峰值盈利的 ${compactDecimal(scaleDecimalByPowerOfTen(profitLockRule.lock_fraction ?? "0", 2) ?? "0")}%`
                        : `达到 ${profitLockRule?.activation_r}R 后最多回吐 ${profitLockRule?.giveback_r}R`}
                  </Typography>
                </Box>
                <Button
                  type="button"
                  size="small"
                  color="inherit"
                  onClick={() => onChange({
                    ...value,
                    dynamic_rules: value.dynamic_rules.filter((rule) => (
                      rule.kind !== "STEPPED_PROTECTION" && rule.kind !== "PROFIT_LOCK"
                    )),
                  })}
                >
                  移除
                </Button>
              </Stack>
            </Box>
          ) : null}
        </Box>
      </EditorSection>
      ) : null}

      {activeMilestone === 2 ? (
        <EditorSection
          id="order-schedule-exit-title"
          title="自动退出"
        >
        <Box sx={{ mb: 1.25 }}>{fundingPanel}</Box>
        <Box
          sx={{
            mt: 0,
          }}
        >
          <Button
            type="button"
            variant="outlined"
            fullWidth
            aria-expanded={exitCatalogOpen}
            onClick={() => setExitCatalogOpen((open) => !open)}
            sx={{ justifyContent: "flex-start", textTransform: "none", fontWeight: 800 }}
          >
            ＋ 添加退出方式
          </Button>
          {exitCatalogOpen ? (
            <Stack
              spacing={1.25}
              sx={{ mt: 1, pt: 1, borderTop: 1, borderColor: "divider" }}
              aria-label="退出方式目录"
            >
              <Box>
                <Typography variant="caption" color="text.secondary">价格目标</Typography>
                <Button
                  type="button"
                  size="small"
                  fullWidth
                  disabled={value.protection_policy.take_profit_ladder !== null}
                  onClick={() => {
                    onChange({
                      ...value,
                      protection_policy: {
                        ...value.protection_policy,
                        take_profit_ladder: {
                          levels: [
                            { trigger_r: "1", quantity_fraction: "0.5" },
                            { trigger_r: "2", quantity_fraction: "0.5" },
                          ],
                        },
                      },
                    });
                    setExitCatalogOpen(false);
                  }}
                  sx={{ justifyContent: "flex-start", textTransform: "none" }}
                >
                  固定 / 分级止盈 · 1–4 个价格目标
                </Button>
              </Box>
              <Box>
                <Typography variant="caption" color="text.secondary">收益锁定</Typography>
                <Stack spacing={.25}>
                  <Button
                    type="button"
                    size="small"
                    fullWidth
                    onClick={() => addProfitLock("RATIO")}
                    sx={{ justifyContent: "flex-start", textTransform: "none" }}
                  >
                    峰值比例锁盈 · 达到 1R 后锁定峰值盈利的 50%
                  </Button>
                  <Button
                    type="button"
                    size="small"
                    fullWidth
                    onClick={() => addProfitLock("FIXED_GIVEBACK")}
                    sx={{ justifyContent: "flex-start", textTransform: "none" }}
                  >
                    固定回撤锁盈 · 达到 1R 后最多回吐 0.5R
                  </Button>
                </Stack>
              </Box>
              <Box>
                <Typography variant="caption" color="text.secondary">时间约束</Typography>
                <Button
                  type="button"
                  size="small"
                  fullWidth
                  disabled={value.protection_policy.time_exit_seconds !== null}
                  onClick={() => {
                    onChange({
                      ...value,
                      protection_policy: {
                        ...value.protection_policy,
                        time_exit_seconds: 86_400,
                      },
                    });
                    setExitCatalogOpen(false);
                  }}
                  sx={{ justifyContent: "flex-start", textTransform: "none" }}
                >
                  时间退出 · 首笔成交后整组计时
                </Button>
              </Box>
            </Stack>
          ) : null}
          {value.protection_policy.take_profit_ladder !== null ? (
          <Box sx={configuredRuleSx}>
          <Stack direction="row" spacing={1} sx={{ alignItems: "center", justifyContent: "space-between" }}>
            <Box sx={{ minWidth: 0 }}>
              <Typography variant="body2" sx={{ fontWeight: 750 }}>固定 / 分级止盈</Typography>
              <Typography variant="caption" color="text.secondary">
                {takeProfitLevels.length} 档止盈
              </Typography>
            </Box>
            <Button
              type="button"
              size="small"
              color="inherit"
              aria-label="移除分级止盈"
              onClick={() => onChange({
                ...value,
                protection_policy: {
                  ...value.protection_policy,
                  take_profit_ladder: null,
                },
              })}
            >
              移除
            </Button>
          </Stack>
        {value.protection_policy.take_profit_ladder !== null ? (
          <Box sx={{ mt: .75 }}>
            <Box sx={{ mb: 1 }}>
              {takeProfitAfterCostPanel}
            </Box>
            {takeProfitCostWarning ? (
              <Typography
                role="alert"
                variant="caption"
                color="warning.dark"
                sx={{ display: "block", mb: 1, fontWeight: 750 }}
              >
                {takeProfitCostWarning}
              </Typography>
            ) : null}
            <Stack spacing={.75}>
              {takeProfitLevels.map((level, index) => (
                <Box
                  key={`take-profit-${index}`}
                  sx={{
                    display: "grid",
                    gridTemplateColumns: "auto minmax(0, 1fr) minmax(0, 1fr) auto",
                    gap: .75,
                    alignItems: "center",
                  }}
                >
                  <Typography variant="caption" sx={{ width: 28, fontWeight: 750 }}>
                    TP{index + 1}
                  </Typography>
                  <TextField
                    size="small"
                    type="number"
                    label="目标（R）"
                    value={level.trigger_r}
                    onChange={(event) => onChange({
                      ...value,
                      protection_policy: {
                        ...value.protection_policy,
                        take_profit_ladder: {
                          levels: takeProfitLevels.map((item, itemIndex) => (
                            itemIndex === index
                              ? { ...item, trigger_r: event.target.value }
                              : item
                          )),
                        },
                      },
                    })}
                    slotProps={{ htmlInput: { min: 0, step: "any" } }}
                  />
                  <TextField
                    size="small"
                    type="number"
                    label="成交量（%）"
                    value={scaleDecimalByPowerOfTen(level.quantity_fraction, 2) ?? ""}
                    onChange={(event) => onChange({
                      ...value,
                      protection_policy: {
                        ...value.protection_policy,
                        take_profit_ladder: {
                          levels: takeProfitLevels.map((item, itemIndex) => (
                            itemIndex === index
                              ? {
                                ...item,
                                quantity_fraction:
                                  scaleDecimalByPowerOfTen(event.target.value, -2)
                                  ?? event.target.value,
                              }
                              : item
                          )),
                        },
                      },
                    })}
                    slotProps={{ htmlInput: { min: 0, max: 100, step: "any" } }}
                  />
                  <Button
                    size="small"
                    color="inherit"
                    disabled={takeProfitLevels.length === 1}
                    onClick={() => onChange({
                      ...value,
                      protection_policy: {
                        ...value.protection_policy,
                        take_profit_ladder: {
                          levels: takeProfitLevels.filter(
                            (_item, itemIndex) => itemIndex !== index,
                          ),
                        },
                      },
                    })}
                  >
                    删除
                  </Button>
                </Box>
              ))}
            </Stack>
            <Stack direction="row" spacing={1} sx={{ mt: 1, alignItems: "center", justifyContent: "space-between" }}>
              <Button
                size="small"
                variant="outlined"
                disabled={takeProfitLevels.length >= 4}
                onClick={() => onChange({
                  ...value,
                  protection_policy: {
                    ...value.protection_policy,
                    take_profit_ladder: {
                      levels: [
                        ...takeProfitLevels,
                        {
                          trigger_r: String(
                            (finiteNumber(takeProfitLevels.at(-1)?.trigger_r ?? "") ?? 0) + 1,
                          ),
                          quantity_fraction: "0.1",
                        },
                      ],
                    },
                  },
                })}
              >
                添加止盈档
              </Button>
              <Typography
                role="status"
                variant="caption"
                color={approximatelyEqual(
                  String(takeProfitTotalFraction),
                  "1",
                ) ? "text.secondary" : "error.main"}
              >
                合计 {takeProfitTotalPercent}%
              </Typography>
            </Stack>
          </Box>
        ) : null}
          </Box>
          ) : null}

          {steppedRule ? (
          <Box sx={configuredRuleSx}>
            <Stack direction="row" spacing={1} sx={{ alignItems: "center", justifyContent: "space-between" }}>
              <Box sx={{ minWidth: 0 }}>
                <Typography variant="body2" sx={{ fontWeight: 750 }}>阶梯保盈</Typography>
                <Typography variant="caption" color="text.secondary">
                  {steppedRule.steps.length} 档 · 逐级收紧止损
                </Typography>
              </Box>
              <Button
                type="button"
                size="small"
                color="inherit"
                aria-label="移除阶梯保盈"
                onClick={() => onChange({
                  ...value,
                  dynamic_rules: withoutDynamicRule(value.dynamic_rules, "STEPPED_PROTECTION"),
                })}
              >
                移除
              </Button>
            </Stack>
            {steppedRule ? (
              <Box sx={{ mt: .75 }}>
                <Stack spacing={.75}>
                  {steppedRule.steps.map((step, index) => (
                    <Box
                      key={`protection-step-${index}`}
                      sx={{
                        display: "grid",
                        gridTemplateColumns: "auto minmax(0, 1fr) minmax(0, 1fr) auto",
                        gap: .75,
                        alignItems: "center",
                      }}
                    >
                      <Typography variant="caption" sx={{ width: 28, fontWeight: 750 }}>
                        M{index + 1}
                      </Typography>
                      <TextField
                        size="small"
                        type="number"
                        label="盈利触发（R）"
                        value={step.trigger_r}
                        onChange={(event) => onChange({
                          ...value,
                          dynamic_rules: withDynamicRule(value.dynamic_rules, {
                            ...steppedRule,
                            steps: steppedRule.steps.map((item, itemIndex) => (
                              itemIndex === index
                                ? { ...item, trigger_r: event.target.value }
                                : item
                            )),
                          }),
                        })}
                        slotProps={{ htmlInput: { min: 0, step: "any" } }}
                      />
                      <TextField
                        size="small"
                        type="number"
                        label="止损移至（R）"
                        value={step.stop_r}
                        onChange={(event) => onChange({
                          ...value,
                          dynamic_rules: withDynamicRule(value.dynamic_rules, {
                            ...steppedRule,
                            steps: steppedRule.steps.map((item, itemIndex) => (
                              itemIndex === index
                                ? { ...item, stop_r: event.target.value }
                                : item
                            )),
                          }),
                        })}
                        slotProps={{ htmlInput: { step: "any" } }}
                      />
                      <Button
                        size="small"
                        color="inherit"
                        disabled={steppedRule.steps.length === 1}
                        onClick={() => onChange({
                          ...value,
                          dynamic_rules: withDynamicRule(value.dynamic_rules, {
                            ...steppedRule,
                            steps: steppedRule.steps.filter(
                              (_item, itemIndex) => itemIndex !== index,
                            ),
                          }),
                        })}
                      >
                        删除
                      </Button>
                    </Box>
                  ))}
                </Stack>
                <Box sx={{ ...compactFieldGridSx, mt: 1 }}>
                  <Button
                    size="small"
                    variant="outlined"
                    disabled={steppedRule.steps.length >= 8}
                    onClick={() => {
                      const previous = steppedRule.steps.at(-1);
                      const previousTrigger = finiteNumber(previous?.trigger_r ?? "") ?? 0;
                      const previousStop = finiteNumber(previous?.stop_r ?? "") ?? -1;
                      onChange({
                        ...value,
                        dynamic_rules: withDynamicRule(value.dynamic_rules, {
                          ...steppedRule,
                          steps: [
                            ...steppedRule.steps,
                            {
                              trigger_r: String(previousTrigger + 1),
                              stop_r: String(previousStop + 1),
                            },
                          ],
                        }),
                      });
                    }}
                  >
                    添加移动档
                  </Button>
                  <TextField
                    size="small"
                    type="number"
                    label="最短更新间隔（秒）"
                    value={steppedRule.minimum_update_interval_seconds}
                    onChange={(event) => onChange({
                      ...value,
                      dynamic_rules: withDynamicRule(value.dynamic_rules, {
                        ...steppedRule,
                        minimum_update_interval_seconds: Number(event.target.value),
                      }),
                    })}
                    slotProps={{ htmlInput: { min: 1, max: 3_600, step: 1 } }}
                  />
                  <TextField
                    size="small"
                    type="number"
                    label="最多收紧次数"
                    value={steppedRule.max_adjustments}
                    onChange={(event) => onChange({
                      ...value,
                      dynamic_rules: withDynamicRule(value.dynamic_rules, {
                        ...steppedRule,
                        max_adjustments: Number(event.target.value),
                      }),
                    })}
                    slotProps={{
                      htmlInput: {
                        min: steppedRule.steps.length,
                        max: 8,
                        step: 1,
                      },
                    }}
                    helperText={`不能少于当前 ${steppedRule.steps.length} 个移动档`}
                  />
                </Box>
              </Box>
            ) : null}
          </Box>
          ) : null}

          {profitLockRule ? (
          <Box sx={configuredRuleSx}>
            <Stack direction="row" spacing={1} sx={{ alignItems: "center", justifyContent: "space-between" }}>
              <Box sx={{ minWidth: 0 }}>
                <Typography variant="body2" sx={{ fontWeight: 750 }}>连续收益锁定</Typography>
                <Typography variant="caption" color="text.secondary">
                  {profitLockRule.mode === "RATIO"
                    ? `锁定峰值盈利的 ${compactDecimal(scaleDecimalByPowerOfTen(profitLockRule.lock_fraction ?? "0", 2) ?? "0")}%`
                    : `允许从峰值回撤 ${compactDecimal(profitLockRule.giveback_r ?? "0")}R`}
                </Typography>
              </Box>
              <Button
                type="button"
                size="small"
                color="inherit"
                aria-label="移除连续收益锁定"
                onClick={() => onChange({
                  ...value,
                  dynamic_rules: withoutDynamicRule(value.dynamic_rules, "PROFIT_LOCK"),
                })}
              >
                移除
              </Button>
            </Stack>
            {profitLockRule ? (
              <Box sx={{ ...compactFieldGridSx, mt: .75 }}>
                <TextField
                  select
                  size="small"
                  label="锁定方式"
                  value={profitLockRule.mode}
                  onChange={(event) => {
                    const mode = event.target.value as "RATIO" | "FIXED_GIVEBACK";
                    onChange({
                      ...value,
                      dynamic_rules: withDynamicRule(value.dynamic_rules, {
                        ...profitLockRule,
                        mode,
                        lock_fraction: mode === "RATIO" ? "0.5" : null,
                        giveback_r: mode === "FIXED_GIVEBACK" ? "0.5" : null,
                      }),
                    });
                  }}
                >
                  <MenuItem value="RATIO">锁定峰值盈利比例</MenuItem>
                  <MenuItem value="FIXED_GIVEBACK">允许固定 R 回撤</MenuItem>
                </TextField>
                <TextField
                  size="small"
                  type="number"
                  label="开始锁定（R）"
                  value={profitLockRule.activation_r}
                  onChange={(event) => onChange({
                    ...value,
                    dynamic_rules: withDynamicRule(value.dynamic_rules, {
                      ...profitLockRule,
                      activation_r: event.target.value,
                    }),
                  })}
                  slotProps={{ htmlInput: { min: 0, step: "any" } }}
                />
                {profitLockRule.mode === "RATIO" ? (
                  <TextField
                    size="small"
                    type="number"
                    label="锁定比例（%）"
                    value={scaleDecimalByPowerOfTen(profitLockRule.lock_fraction ?? "", 2) ?? ""}
                    onChange={(event) => onChange({
                      ...value,
                      dynamic_rules: withDynamicRule(value.dynamic_rules, {
                        ...profitLockRule,
                        lock_fraction:
                          scaleDecimalByPowerOfTen(event.target.value, -2)
                          ?? event.target.value,
                      }),
                    })}
                    slotProps={{ htmlInput: { min: 0, max: 100, step: "any" } }}
                  />
                ) : (
                  <TextField
                    size="small"
                    type="number"
                    label="允许回撤（R）"
                    value={profitLockRule.giveback_r ?? ""}
                    onChange={(event) => onChange({
                      ...value,
                      dynamic_rules: withDynamicRule(value.dynamic_rules, {
                        ...profitLockRule,
                        giveback_r: event.target.value,
                      }),
                    })}
                    slotProps={{ htmlInput: { min: 0, step: "any" } }}
                  />
                )}
                <TextField
                  size="small"
                  type="number"
                  label="最小收紧步长（R）"
                  value={profitLockRule.minimum_step_r}
                  onChange={(event) => onChange({
                    ...value,
                    dynamic_rules: withDynamicRule(value.dynamic_rules, {
                      ...profitLockRule,
                      minimum_step_r: event.target.value,
                    }),
                  })}
                  slotProps={{ htmlInput: { min: 0, step: "any" } }}
                />
                <TextField
                  size="small"
                  type="number"
                  label="最短更新间隔（秒）"
                  value={profitLockRule.minimum_update_interval_seconds}
                  onChange={(event) => onChange({
                    ...value,
                    dynamic_rules: withDynamicRule(value.dynamic_rules, {
                      ...profitLockRule,
                      minimum_update_interval_seconds: Number(event.target.value),
                    }),
                  })}
                  slotProps={{ htmlInput: { min: 1, max: 3_600, step: 1 } }}
                />
                <TextField
                  size="small"
                  type="number"
                  label="最多收紧次数"
                  value={profitLockRule.max_adjustments}
                  onChange={(event) => onChange({
                    ...value,
                    dynamic_rules: withDynamicRule(value.dynamic_rules, {
                      ...profitLockRule,
                      max_adjustments: Number(event.target.value),
                    }),
                  })}
                  slotProps={{ htmlInput: { min: 1, max: 8, step: 1 } }}
                />
              </Box>
            ) : null}
          </Box>
          ) : null}

          {value.protection_policy.time_exit_seconds !== null ? (
          <Box sx={configuredRuleSx}>
          <Stack direction="row" spacing={1} sx={{ alignItems: "center", justifyContent: "space-between" }}>
            <Typography variant="body2" sx={{ fontWeight: 750 }}>时间退出</Typography>
            <Button
              type="button"
              size="small"
              color="inherit"
              aria-label="移除时间退出"
              onClick={() => onChange({
                ...value,
                protection_policy: {
                  ...value.protection_policy,
                  time_exit_seconds: null,
                },
              })}
            >
              移除
            </Button>
          </Stack>
          {value.protection_policy.time_exit_seconds !== null ? (
            <TextField
              size="small"
              type="number"
              label="首笔成交后整组退出（秒）"
              value={value.protection_policy.time_exit_seconds}
              onChange={(event) => onChange({
                ...value,
                protection_policy: {
                  ...value.protection_policy,
                  time_exit_seconds: Number(event.target.value),
                },
              })}
              slotProps={{ htmlInput: { min: 1, max: 2_592_000, step: 1 } }}
              helperText="以本次激活的首笔成交为全局时钟；到期先停止并撤销剩余入场，再按当前持仓退出"
              sx={{ display: "block", mt: 1, maxWidth: 360 }}
            />
          ) : null}
          </Box>
          ) : null}
        </Box>
      </EditorSection>
      ) : null}

      {activeMilestone === 0 && entryDynamicRules.length > 0 ? (
      <EditorSection
        id="order-schedule-dynamic-title"
        title="入场管理"
      >
        <Stack spacing={1.25}>
          {expireRule ? (
          <Box sx={{ ...surfaceFrameSx, p: 1.25 }}>
            <FormControlLabel
              control={(
                <Checkbox
                  checked={Boolean(expireRule)}
                  disabled={venue.order_type !== "LIMIT"}
                  onChange={(event) => onChange({
                    ...value,
                    dynamic_rules: event.target.checked
                      ? withDynamicRule(value.dynamic_rules, { kind: "EXPIRE_REMAINING", after_seconds: 300 })
                      : withoutDynamicRule(value.dynamic_rules, "EXPIRE_REMAINING"),
                  })}
                />
              )}
              label={entryProgram.kind === "TIME_SLICED"
                ? "每批未成交超时"
                : "到期撤销未成交余量"}
            />
            {venue.order_type !== "LIMIT" ? (
              <Typography variant="caption" color="text.secondary">仅限价单可用。</Typography>
            ) : null}
            {expireRule ? (
              <TextField
                size="small"
                type="number"
                label={entryProgram.kind === "TIME_SLICED"
                  ? "每批最多挂单（秒）"
                  : "首档提交后等待（秒）"}
                value={expireRule.after_seconds}
                onChange={(event) => onChange({
                  ...value,
                  dynamic_rules: withDynamicRule(value.dynamic_rules, {
                    ...expireRule,
                    after_seconds: Number(event.target.value),
                  }),
                })}
                slotProps={{ htmlInput: { min: 1, max: 604_800, step: 1 } }}
                helperText={entryProgram.kind === "TIME_SLICED"
                  ? "每批从真正提交开始独立计时；到期撤单并核对闭合后，才释放下一批"
                  : "从首个档位真正进入提交开始计时，条件等待不会提前消耗该时长"}
                sx={{ mt: 1, display: "block", maxWidth: 320 }}
              />
            ) : null}
          </Box>
          ) : null}

          {repriceRule ? (
          <Box sx={{ ...surfaceFrameSx, p: 1.25 }}>
            <Stack direction="row" spacing={1} sx={{ alignItems: "center", justifyContent: "space-between" }}>
              <Typography variant="body2" sx={{ fontWeight: 750 }}>
                跟随同侧盘口
              </Typography>
              <Button
                type="button"
                size="small"
                color="inherit"
                aria-label="移除移动挂单"
                onClick={() => onChange({
                  ...value,
                  dynamic_rules: withoutDynamicRule(
                    value.dynamic_rules,
                    "REPRICE_ENTRY",
                  ),
                })}
              >
                移除
              </Button>
            </Stack>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: .5 }}>
              仅移动尚未成交的单笔挂单。先撤销旧单，确认终态且累计成交为零后，
              再按最新同侧盘口重挂；部分成交不会移动。
            </Typography>
            <Box sx={{ ...compactFieldGridSx, mt: 1 }}>
              <TextField
                size="small"
                type="number"
                label="触发偏离（bps）"
                value={repriceRule.trigger_distance_bps}
                onChange={(event) => onChange({
                  ...value,
                  dynamic_rules: withDynamicRule(value.dynamic_rules, {
                    ...repriceRule,
                    trigger_distance_bps: event.target.value,
                  }),
                })}
                slotProps={{ htmlInput: { min: 0, max: 2_000, step: "any" } }}
              />
              <TextField
                size="small"
                type="number"
                label="盘口被动偏移（bps）"
                value={repriceRule.book_offset_bps}
                onChange={(event) => onChange({
                  ...value,
                  dynamic_rules: withDynamicRule(value.dynamic_rules, {
                    ...repriceRule,
                    book_offset_bps: event.target.value,
                  }),
                })}
                slotProps={{ htmlInput: { min: 0, max: 500, step: "any" } }}
                helperText={direction === "LONG"
                  ? "重挂价 = 买一价 − 偏移"
                  : "重挂价 = 卖一价 + 偏移"}
              />
              <TextField
                size="small"
                type="number"
                label="相对初始价最大总移动（bps）"
                value={repriceRule.maximum_total_move_bps}
                onChange={(event) => onChange({
                  ...value,
                  dynamic_rules: withDynamicRule(value.dynamic_rules, {
                    ...repriceRule,
                    maximum_total_move_bps: event.target.value,
                  }),
                })}
                slotProps={{ htmlInput: { min: 0, max: 2_000, step: "any" } }}
              />
              <TextField
                size="small"
                type="number"
                label="最短更新间隔（秒）"
                value={repriceRule.minimum_update_interval_seconds}
                onChange={(event) => onChange({
                  ...value,
                  dynamic_rules: withDynamicRule(value.dynamic_rules, {
                    ...repriceRule,
                    minimum_update_interval_seconds: Number(event.target.value),
                  }),
                })}
                slotProps={{ htmlInput: { min: 1, max: 3_600, step: 1 } }}
              />
              <TextField
                size="small"
                type="number"
                label="最多移动次数"
                value={repriceRule.max_adjustments}
                onChange={(event) => onChange({
                  ...value,
                  dynamic_rules: withDynamicRule(value.dynamic_rules, {
                    ...repriceRule,
                    max_adjustments: Number(event.target.value),
                  }),
                })}
                slotProps={{ htmlInput: { min: 1, max: 8, step: 1 } }}
              />
            </Box>
          </Box>
          ) : null}

          {shockRule ? (
          <Box sx={{ ...surfaceFrameSx, p: 1.25 }}>
            <FormControlLabel
              control={(
                <Checkbox
                  checked={Boolean(shockRule)}
                  onChange={(event) => onChange({
                    ...value,
                    dynamic_rules: event.target.checked
                      ? withDynamicRule(value.dynamic_rules, {
                        kind: "CANCEL_ON_SHOCK",
                        window_seconds: 30,
                        adverse_move_bps: "50",
                        invalidation_price: (() => {
                          const current = finiteNumber(displayReferencePrice ?? "");
                          if (current === null || current <= 0) return null;
                          return generatedOffsetPrice(
                            displayReferencePrice,
                            direction === "SHORT" ? 1.01 : 0.99,
                            priceTickSize,
                          );
                        })(),
                        opportunity_missed_price: null,
                        max_triggers: 1,
                      })
                      : withoutDynamicRule(value.dynamic_rules, "CANCEL_ON_SHOCK"),
                  })}
                />
              )}
              label="行情失效时自动取消入场"
            />
            {shockRule ? (
              <Box sx={{ ...compactFieldGridSx, mt: 1 }}>
                <Typography variant="caption" color="text.secondary" sx={{ gridColumn: "1 / -1" }}>
                  不利方向突破失效价，或价格已向预期方向走远而错过入场，都会永久终止本次未成交机会。任一路径只触发一次。
                </Typography>
                <FormControlLabel
                  sx={{ gridColumn: "1 / -1", m: 0 }}
                  control={(
                    <Checkbox
                      size="small"
                      checked={Boolean(shockRule.invalidation_price)}
                      onChange={(event) => {
                        if (
                          !event.target.checked
                          && !shockRule.window_seconds
                          && !shockRule.opportunity_missed_price
                        ) {
                          onChange({
                            ...value,
                            dynamic_rules: withoutDynamicRule(value.dynamic_rules, "CANCEL_ON_SHOCK"),
                          });
                          return;
                        }
                        onChange({
                          ...value,
                          dynamic_rules: withDynamicRule(value.dynamic_rules, {
                            ...shockRule,
                            invalidation_price: event.target.checked
                              ? generatedOffsetPrice(
                                displayReferencePrice,
                                direction === "SHORT" ? 1.01 : 0.99,
                                priceTickSize,
                              )
                              : null,
                          }),
                        });
                      }}
                    />
                  )}
                  label={`固定失效价（${direction === "SHORT" ? "上破" : "跌破"}即取消）`}
                />
                {shockRule.invalidation_price ? (
                  <TextField
                    size="small"
                    type="number"
                    label="失效价（USDT）"
                    value={shockRule.invalidation_price}
                    onChange={(event) => onChange({
                      ...value,
                      dynamic_rules: withDynamicRule(value.dynamic_rules, {
                        ...shockRule,
                        invalidation_price: event.target.value,
                      }),
                    })}
                    slotProps={{ htmlInput: { min: 0, step: "any" } }}
                    helperText={direction === "SHORT" ? "标记价 ≥ 此价时取消" : "标记价 ≤ 此价时取消"}
                  />
                ) : null}
                <FormControlLabel
                  sx={{ gridColumn: "1 / -1", m: 0 }}
                  control={(
                    <Checkbox
                      size="small"
                      checked={Boolean(shockRule.opportunity_missed_price)}
                      onChange={(event) => {
                        if (
                          !event.target.checked
                          && !shockRule.window_seconds
                          && !shockRule.invalidation_price
                        ) {
                          onChange({
                            ...value,
                            dynamic_rules: withoutDynamicRule(value.dynamic_rules, "CANCEL_ON_SHOCK"),
                          });
                          return;
                        }
                        onChange({
                          ...value,
                          dynamic_rules: withDynamicRule(value.dynamic_rules, {
                            ...shockRule,
                            opportunity_missed_price: event.target.checked
                              ? generatedOffsetPrice(
                                displayReferencePrice,
                                direction === "SHORT" ? 0.99 : 1.01,
                                priceTickSize,
                              )
                              : null,
                          }),
                        });
                      }}
                    />
                  )}
                  label={`机会错过价（${direction === "SHORT" ? "跌破" : "上破"}即取消）`}
                />
                {shockRule.opportunity_missed_price ? (
                  <TextField
                    size="small"
                    type="number"
                    label="机会错过价（USDT）"
                    value={shockRule.opportunity_missed_price}
                    onChange={(event) => onChange({
                      ...value,
                      dynamic_rules: withDynamicRule(value.dynamic_rules, {
                        ...shockRule,
                        opportunity_missed_price: event.target.value,
                      }),
                    })}
                    slotProps={{ htmlInput: { min: 0, step: "any" } }}
                    helperText={direction === "SHORT"
                      ? "标记价 ≤ 此价时取消，避免低位再追空"
                      : "标记价 ≥ 此价时取消，避免高位再追多"}
                  />
                ) : null}
                <FormControlLabel
                  sx={{ gridColumn: "1 / -1", m: 0 }}
                  control={(
                    <Checkbox
                      size="small"
                      checked={Boolean(shockRule.window_seconds)}
                      onChange={(event) => {
                        if (
                          !event.target.checked
                          && !shockRule.invalidation_price
                          && !shockRule.opportunity_missed_price
                        ) {
                          onChange({
                            ...value,
                            dynamic_rules: withoutDynamicRule(value.dynamic_rules, "CANCEL_ON_SHOCK"),
                          });
                          return;
                        }
                        onChange({
                          ...value,
                          dynamic_rules: withDynamicRule(value.dynamic_rules, {
                            ...shockRule,
                            window_seconds: event.target.checked ? 30 : null,
                            adverse_move_bps: event.target.checked ? "50" : null,
                          }),
                        });
                      }}
                    />
                  )}
                  label="同时监测短时急剧反向"
                />
                {shockRule.window_seconds ? (
                  <>
                    <TextField
                      size="small"
                      type="number"
                      label="观察窗口（秒）"
                      value={shockRule.window_seconds}
                      onChange={(event) => onChange({
                        ...value,
                        dynamic_rules: withDynamicRule(value.dynamic_rules, {
                          ...shockRule,
                          window_seconds: Number(event.target.value),
                        }),
                      })}
                      slotProps={{ htmlInput: { min: 1, max: 300, step: 1 } }}
                    />
                    <TextField
                      size="small"
                      type="number"
                      label="反向变动（bps）"
                      value={shockRule.adverse_move_bps ?? ""}
                      onChange={(event) => onChange({
                        ...value,
                        dynamic_rules: withDynamicRule(value.dynamic_rules, {
                          ...shockRule,
                          adverse_move_bps: event.target.value,
                        }),
                      })}
                      slotProps={{ htmlInput: { min: 0, step: "any" } }}
                      helperText={direction === "SHORT" ? "窗口内上涨达到阈值" : "窗口内下跌达到阈值"}
                    />
                  </>
                ) : null}
                <TextField
                  size="small"
                  label="最多触发次数"
                  value="1（当前已验证上限）"
                  slotProps={{ htmlInput: { readOnly: true } }}
                />
              </Box>
            ) : null}
          </Box>
          ) : null}
        </Stack>
      </EditorSection>
      ) : null}

      {activeMilestone === 3 ? (
        <>
        {planOptions ? (
          <Box sx={{ borderBottom: 1, borderColor: "divider" }}>
            {planOptions}
          </Box>
        ) : null}
        {takeProfitAfterCostPanel ? (
          <Box
            data-testid="review-after-cost-summary"
            sx={{ px: 1.5, py: 1.35, borderBottom: 1, borderColor: "divider" }}
          >
            {takeProfitAfterCostPanel}
          </Box>
        ) : null}
        <Box sx={{ px: 1.5, py: 1.35, borderBottom: 1, borderColor: "divider" }}>
          {fundingPanel}
        </Box>
        <EditorSection
          id="order-schedule-review-title"
          title="计划概要"
        >
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: .75 }}>
            当前配置
          </Typography>
          <ReviewFactRows
            rows={[
              ["入场", entryProgramSummary],
              ["订单", orderInstructionSummary],
              ["下单金额", amountSummary],
              ["条件", conditionReviewSummary],
              ["入场管理", invalidationReviewSummary],
              ["成交保护", `标记价止损 · ${compactDecimal(value.protection_policy.initial_stop.distance_bps)} bps`],
              ["自动退出", exitReviewSummary || "缺失"],
            ]}
          />
          {reviewDisciplineSummary ? (
            <Box sx={{ mt: 1.75 }}>
              {reviewDisciplineSummary}
            </Box>
          ) : null}
        </EditorSection>
        <EditorSection
        id="order-schedule-preview-title"
        title="服务端预览"
      >
        {localValidation.length > 0 ? (
          <Alert severity="warning" variant="outlined">
            <Typography variant="body2" sx={{ fontWeight: 700 }}>修正以下输入后再生成预览：</Typography>
            <Box component="ul" sx={{ my: .75, pl: 2.5 }}>
              {localValidation.map((problem) => <li key={problem}>{problem}</li>)}
            </Box>
          </Alert>
        ) : null}
        {localValidation.length === 0 && !marketProjectionReady ? (
          <Alert severity="warning" variant="outlined">
            当前环境的历史 K 线或实时来源尚未就绪；旧价格投影已清除，价格预览与保存保持阻断。
          </Alert>
        ) : null}
        {localValidation.length === 0
          && marketProjectionReady
          && (preview.isPending || previewStale) ? (
          <Box aria-live="polite">
            <LinearProgress aria-label="正在按当前输入生成订单计划预览" />
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
              {previewStale ? "输入已变化，等待当前版本的服务端预览。" : "正在读取交易所规则并标准化全部档位。"}
            </Typography>
          </Box>
        ) : null}
        {localValidation.length === 0
          && marketProjectionReady
          && preview.isError
          && !previewStale ? (
          <Alert
            severity="error"
            action={<Button color="inherit" size="small" onClick={() => preview.refetch()}>重试预览</Button>}
          >
            {previewFailureText(preview.error)}
          </Alert>
        ) : null}
        {localValidation.length === 0
          && marketProjectionReady
          && preview.data
          && !previewStale ? (
          <Stack spacing={1.5}>
            {preview.isFetching ? (
              <Alert severity="info" variant="outlined">
                正在刷新交易所规则；下表仍是截止 {formatUserVisibleTime(preview.data.source_cutoff)} 的上次预览。
              </Alert>
            ) : null}
            <Alert severity={preview.data.valid ? "success" : "error"}>
              {preview.data.valid
                ? `技术预览可保存 · ${preview.data.legs.length} 档 · 标准化总额 ${quoteCurrencyAmount(preview.data.effective_total_notional)} USDT`
                : "预览被服务端阻断；标准化结果仅用于定位，不能形成执行档位。"}
            </Alert>
            {preview.data.issues.length > 0 ? (
              <Alert severity="error" variant="outlined">
                <Box component="ul" sx={{ my: 0, pl: 2.5 }}>
                  {preview.data.issues.map((issue, index) => (
                    <li key={`${issue.code}:${issue.leg_index ?? "all"}:${index}`}>
                      {previewIssueText(issue, preview.data)}
                      <Typography component="span" variant="caption" color="text.secondary"> · {issue.field}</Typography>
                    </li>
                  ))}
                </Box>
              </Alert>
            ) : null}
            <Box
              component="details"
              sx={{
                borderTop: 1,
                borderColor: "divider",
                pt: .75,
                "& > summary": { cursor: "pointer", fontSize: 12, fontWeight: 700 },
              }}
            >
              <Box component="summary">
                完整标准化明细 · {shortDigest(preview.data.schedule_digest)}
              </Box>
              <Stack spacing={1.25} sx={{ mt: 1 }}>
            <Box
              component="dl"
              sx={{
                m: 0,
                display: "grid",
                gridTemplateColumns: { xs: "1fr", sm: "repeat(2, minmax(0, 1fr))" },
                gap: 1,
              }}
            >
              {[
                ["请求总额", `${quoteCurrencyAmount(preview.data.requested_total_notional)} USDT`],
                ["标准化总额", `${quoteCurrencyAmount(preview.data.effective_total_notional)} USDT`],
                ["计量参考价", preview.data.reference_price === null ? "未使用" : `${tradingPrice(preview.data.reference_price, priceTickSize)} USDT`],
                ["交易所规则", preview.data.instrument_rules.source],
                ["规则截止", formatUserVisibleTime(preview.data.source_cutoff)],
                ["价格步进", compactDecimal(preview.data.instrument_rules.price_tick_size)],
                ["最小名义金额", `${quoteCurrencyAmount(preview.data.instrument_rules.min_notional)} USDT`],
              ].map(([label, display]) => (
                <Box key={label} sx={{ px: 1.25, py: 1, bgcolor: "action.hover", borderRadius: 1 }}>
                  <Typography component="dt" variant="caption" color="text.secondary">{label}</Typography>
                  <Typography component="dd" className="mono" variant="body2" sx={{ m: 0, mt: .25 }}>{display}</Typography>
                </Box>
              ))}
            </Box>
            <TableContainer
              className="table-scroll"
              role="region"
              aria-label="标准化订单档位"
              tabIndex={0}
              sx={{ ...surfaceFrameSx, overflowX: "auto" }}
            >
              <Table size="small" aria-label="标准化订单档位表" sx={{ minWidth: 860 }}>
                <TableHead>
                  <TableRow>
                    <TableCell>档位</TableCell>
                    <TableCell>释放时间</TableCell>
                    <TableCell align="right">原始价格</TableCell>
                    <TableCell align="right">标准化价格</TableCell>
                    <TableCell align="right">计量价格</TableCell>
                    <TableCell align="right">请求金额</TableCell>
                    <TableCell align="right">数量</TableCell>
                    <TableCell align="right">有效金额</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {preview.data.normalized_legs.map((leg) => (
                    <TableRow key={leg.leg_index} hover>
                      <TableCell>{leg.leg_index + 1} / {leg.leg_count}</TableCell>
                      <TableCell className="mono">
                        {leg.release_after_seconds === 0
                          ? "立即"
                          : `+${leg.release_after_seconds}s`}
                      </TableCell>
                      <TableCell
                        className="mono"
                        align="right"
                      >
                        {leg.raw_price === null ? "场所决定" : tradingPrice(leg.raw_price, priceTickSize)}
                      </TableCell>
                      <TableCell className="mono" align="right">{leg.price === null ? "场所决定" : tradingPrice(leg.price, priceTickSize)}</TableCell>
                      <TableCell className="mono" align="right">{tradingPrice(leg.sizing_price, priceTickSize)}</TableCell>
                      <TableCell className="mono" align="right">{quoteCurrencyAmount(leg.requested_notional)} USDT</TableCell>
                      <TableCell className="mono" align="right">{tradingQuantity(leg.quantity, quantityStep)}</TableCell>
                      <TableCell className="mono" align="right">{quoteCurrencyAmount(leg.effective_notional)} USDT</TableCell>
                    </TableRow>
                  ))}
                  {preview.data.normalized_legs.length === 0 ? (
                    <TableRow>
                      <TableCell colSpan={8} align="center">没有可展示的标准化档位。</TableCell>
                    </TableRow>
                  ) : null}
                </TableBody>
              </Table>
            </TableContainer>
            <Button
              variant="outlined"
              size="small"
              onClick={() => preview.refetch()}
              disabled={preview.isFetching}
              sx={{ alignSelf: "flex-start" }}
            >
              {preview.isFetching ? "正在刷新…" : "按当前输入重新预览"}
            </Button>
              </Stack>
            </Box>
          </Stack>
        ) : null}
      </EditorSection>
        </>
      ) : null}
      {activeMilestone === 4 ? (
        <Box sx={{ minWidth: 0 }}>
          {aiReviewPanel}
        </Box>
      ) : null}
        </Box>
        {footerControls ? (
          <Box
            sx={{
              flex: "0 0 auto",
              borderTop: 1,
              borderColor: "divider",
              bgcolor: "background.paper",
              px: 1.5,
              py: 1.25,
            }}
          >
            {footerControls}
          </Box>
        ) : null}
      </Box>
    </Box>
  );
}
