import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Alert,
  Autocomplete,
  Box,
  Button,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  LinearProgress,
  Stack,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  Tooltip,
  Typography,
} from "@mui/material";
import AutoAwesomeOutlined from "@mui/icons-material/AutoAwesomeOutlined";
import BoltOutlined from "@mui/icons-material/BoltOutlined";
import CloseOutlined from "@mui/icons-material/CloseOutlined";
import SaveOutlined from "@mui/icons-material/SaveOutlined";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useOutletContext } from "react-router";

import {
  ApiFailure,
  createScalpCycle,
  getActivation,
  getScalpCycleByIdempotency,
  getScalpExitByIdempotency,
  getScalpRecommendation,
  getScalpResults,
  getScalpingContracts,
  isUnknownMutationResult,
  previewControl,
  previewOrderSchedule,
  submitActivationControl,
  type BinanceContractCatalog,
  type MarketInterval,
  type OrderScheduleDirection,
  type OrderScheduleSpec,
  type ScalpResultScope,
  type ScalpResults,
  type ScalpTemplate,
  type ScalpCycleCreateResult,
  type SettingsStatus,
} from "../api/client";
import {
  readChartIntervalPreference,
  writeChartIntervalPreference,
} from "../chartIntervalPreference";
import { formatUserVisibleTime, quoteCurrencyAmount, tradingPrice } from "../format";
import {
  FinancialToneText,
  MarketToneText,
  financialToneForSignedValue,
  marketToneForDirection,
  type MarketColorScheme,
} from "../marketColors";
import {
  expectedMarketSourceForEnvironment,
  marketEnvironmentScopeKey,
  usePublicMarketStream,
} from "../marketStream";
import {
  clearPendingScalpRequest,
  readPendingScalpRequest,
  savePendingScalpRequest,
  scalpExitOutcome,
  type ScalpExitRequest,
  type ScalpTriggerRequest,
} from "../scalpRequests";
import {
  buildScalpScheduleSpec,
  DEFAULT_SCALP_TEMPLATE,
  readScalpAnchor,
  readScalpInstrument,
  readScalpTemplate,
  saveScalpAnchor,
  saveScalpInstrument,
  saveScalpTemplate,
  validateScalpTemplate,
  type ScalpTemplateField,
} from "../scalpingWorkbench";
import { scalpVenueProjection } from "../scalpVenueProjection";
import { surfaceFrameSx } from "../theme";
import MarketFlowTape from "../components/MarketFlowTape";

const OrderScheduleChart = lazy(() => import("../components/OrderScheduleChart"));

const visuallyHiddenSx = {
  position: "absolute",
  width: "1px",
  height: "1px",
  padding: 0,
  margin: -1,
  overflow: "hidden",
  clip: "rect(0 0 0 0)",
  whiteSpace: "nowrap",
  border: 0,
} as const;

type FrameContext = {
  status: SettingsStatus;
  marketColorScheme: MarketColorScheme;
};

type Feedback = Readonly<{
  severity: "success" | "info" | "warning" | "error";
  text: string;
}>;

type MetricProps = Readonly<{
  label: string;
  value: string;
  signedValue?: string | null;
  prominent?: boolean;
}>;

const RESULT_SCOPE_LABELS: Readonly<Record<ScalpResultScope, string>> = {
  ALL: "全部",
  TODAY: "今日",
  ANCHOR: "从此刻",
};

const RESULT_STATUS_LABELS: Readonly<Record<string, string>> = {
  OPEN: "运行中",
  RELIABLE: "已结算",
  NO_TRADE: "未成交",
  UNKNOWN: "待核对",
};

const ERROR_LABELS: Readonly<Record<string, string>> = {
  EXECUTOR_NOT_READY: "执行器未就绪",
  SCALP_CYCLE_ALREADY_OPEN: "已有运行中周期",
  ORDER_SCHEDULE_INVALID: "金额或保护价无法按当前合约规则提交",
  SCALP_PREVIEW_MISMATCH: "行情或合约规则已变化，请重试",
  LIVE_READ_ONLY_PRODUCT_MUTATION_FORBIDDEN: "当前真实账户为只读模式",
  LIVE_WRITE_GATE_MUST_BE_OPEN_FOR_SCALP: "真实账户交易开关未打开",
  LIVE_WRITE_PRODUCT_BUILD_MISMATCH: "App、执行器与交易开关版本不一致",
  LIVE_WRITE_GATE_BINDING_INVALID_FOR_ACTIVATION: "真实账户交易开关绑定无效",
  NEW_RISK_DISCIPLINE_BLOCKED: "当前账户风险纪律不接受新增风险",
  PLAN_MAXIMUM_ALLOWED_LOSS_EXCEEDED: "本轮最大预计损失超过账户纪律",
  ACCOUNT_NEW_RISK_SERIALIZATION_CONFLICT: "账户风险状态刚发生变化，请重试",
};

function Metric({ label, value, signedValue, prominent = false }: MetricProps) {
  const content = signedValue === undefined ? value : (
    <FinancialToneText tone={financialToneForSignedValue(signedValue)}>
      {value}
    </FinancialToneText>
  );
  return (
    <Box sx={{ minWidth: 0 }}>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
        {label}
      </Typography>
      <Typography
        className="mono"
        sx={{
          mt: .35,
          fontSize: prominent ? 20 : 13,
          lineHeight: 1.25,
          fontWeight: prominent ? 800 : 700,
          overflowWrap: "anywhere",
        }}
      >
        {content}
      </Typography>
    </Box>
  );
}

function MetricGrid({ children }: { children: React.ReactNode }) {
  return (
    <Box
      sx={{
        display: "grid",
        gridTemplateColumns: { xs: "repeat(2, minmax(0, 1fr))", sm: "repeat(4, minmax(0, 1fr))" },
        gap: { xs: 1.5, md: 2 },
      }}
    >
      {children}
    </Box>
  );
}

function signedMoney(value: string | null, currency = "USDT"): string {
  if (value === null) return "—";
  const numeric = Number(value);
  const sign = Number.isFinite(numeric) && numeric > 0 ? "+" : "";
  return `${sign}${quoteCurrencyAmount(value)} ${currency}`;
}

function feeMoney(value: string | null, currency = "USDT"): string {
  if (value === null) return "—";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";
  return `${numeric === 0 ? "" : "-"}${quoteCurrencyAmount(Math.abs(numeric))} ${currency}`;
}

function holdingLabel(value: string | null): string {
  if (value === null) return "—";
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.round(seconds % 60);
  return remainder > 0 ? `${minutes} 分 ${remainder} 秒` : `${minutes} 分`;
}

function venueStatusColor(
  status: ReturnType<typeof scalpVenueProjection>["status"],
): "default" | "info" | "warning" | "success" {
  if (status === "FILLED") return "success";
  if (status === "UNKNOWN") return "warning";
  if (status === "WORKING" || status === "PARTIALLY_FILLED") return "info";
  return "default";
}

function failureText(error: unknown, fallback: string): string {
  if (!(error instanceof ApiFailure)) return fallback;
  return ERROR_LABELS[error.code] ?? `${fallback}（${error.code}）`;
}

function currentSpreadBps(bid: string | null, ask: string | null): number | null {
  const bidValue = Number(bid);
  const askValue = Number(ask);
  if (
    !Number.isFinite(bidValue)
    || !Number.isFinite(askValue)
    || bidValue <= 0
    || askValue < bidValue
  ) return null;
  return (askValue - bidValue) / ((askValue + bidValue) / 2) * 10_000;
}

function accountConsequence(status: SettingsStatus): string | null {
  if (status.venue_account_type === "USDM_COPY_LEAD") {
    return "带单账户：后续成交可能进入带单组合并被跟随复制。";
  }
  if (status.venue_account_type === "USDM_PERSONAL") {
    return "个人账户：本轮只影响当前个人合约账户。";
  }
  return null;
}

export default function ScalpingPage() {
  const { status, marketColorScheme } = useOutletContext<FrameContext>();
  const queryClient = useQueryClient();
  const initialTemplate = useMemo(
    () => readScalpTemplate(status.environment_id, status.account_id),
    [status.account_id, status.environment_id],
  );
  const [savedTemplate, setSavedTemplate] = useState<ScalpTemplate | null>(initialTemplate);
  const [draftTemplate, setDraftTemplate] = useState<ScalpTemplate>(initialTemplate ?? { ...DEFAULT_SCALP_TEMPLATE });
  const [instrumentRef, setInstrumentRef] = useState(
    () => readScalpInstrument(status.environment_id, status.account_id) ?? "BTCUSDT-PERP",
  );
  const [chartDirection, setChartDirection] = useState<OrderScheduleDirection>("LONG");
  const [chartInterval, setChartInterval] = useState<MarketInterval>(
    () => readChartIntervalPreference(status.environment_id, instrumentRef, "1m"),
  );
  const [resultScope, setResultScope] = useState<ScalpResultScope>("TODAY");
  const [anchorAt, setAnchorAt] = useState<string | null>(
    () => readScalpAnchor(status.environment_id, status.account_id),
  );
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [recommendationNote, setRecommendationNote] = useState<string | null>(null);
  const [exitPreview, setExitPreview] = useState<Awaited<ReturnType<typeof previewControl>> | null>(null);
  const [pendingTrigger, setPendingTrigger] = useState(() => (
    readPendingScalpRequest("TRIGGER", status.environment_id, status.account_id)
  ));
  const [pendingExit, setPendingExit] = useState(() => (
    readPendingScalpRequest("EXIT", status.environment_id, status.account_id)
  ));
  const pendingTriggerRef = useRef(pendingTrigger);
  const pendingExitRef = useRef(pendingExit);
  const exitButtonRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    const readPending = () => {
      pendingTriggerRef.current = readPendingScalpRequest("TRIGGER", status.environment_id, status.account_id);
      pendingExitRef.current = readPendingScalpRequest("EXIT", status.environment_id, status.account_id);
      setPendingTrigger(pendingTriggerRef.current);
      setPendingExit(pendingExitRef.current);
    };
    window.addEventListener("storage", readPending);
    return () => window.removeEventListener("storage", readPending);
  }, [status.account_id, status.environment_id]);

  const catalogQuery = useQuery({
    queryKey: ["scalp-contracts", status.environment_id],
    queryFn: getScalpingContracts,
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
  });
  const contracts = catalogQuery.data?.contracts ?? [];
  const selectedContract = useMemo(
    () => contracts.find((item) => item.instrument_ref === instrumentRef) ?? null,
    [contracts, instrumentRef],
  );

  useEffect(() => {
    if (contracts.length === 0 || selectedContract !== null) return;
    const fallback = contracts.find((item) => item.symbol === "BTCUSDT") ?? contracts[0];
    if (!fallback) return;
    setInstrumentRef(fallback.instrument_ref);
    setChartInterval(
      readChartIntervalPreference(status.environment_id, fallback.instrument_ref, "1m"),
    );
    saveScalpInstrument(
      status.environment_id,
      status.account_id,
      fallback.instrument_ref,
    );
  }, [contracts, selectedContract, status.account_id, status.environment_id]);

  const environmentScope = marketEnvironmentScopeKey(
    status.environment_kind,
    status.environment_id,
  );
  const expectedMarketSource = expectedMarketSourceForEnvironment(status.environment_kind);
  const marketStream = usePublicMarketStream(
    instrumentRef.length > 0,
    instrumentRef,
    chartInterval,
    environmentScope,
    expectedMarketSource,
    [10, 30, 60],
  );
  const bidPrice = marketStream.quote?.bid_price ?? null;
  const askPrice = marketStream.quote?.ask_price ?? null;
  const referencePrice = marketStream.quote?.reference_price ?? null;
  const spreadBps = currentSpreadBps(bidPrice, askPrice);

  const templateErrors = useMemo(
    () => validateScalpTemplate(draftTemplate),
    [draftTemplate],
  );
  const templateValid = Object.keys(templateErrors).length === 0;
  const templateDirty = JSON.stringify(draftTemplate) !== JSON.stringify(savedTemplate);
  const makerLimitPrice = draftTemplate.entry_mode === "MAKER_ONLY_SAME_SIDE"
    ? (chartDirection === "LONG" ? bidPrice : askPrice)
    : null;
  const draftScheduleSpec = useMemo(
    () => buildScalpScheduleSpec(draftTemplate, makerLimitPrice),
    [draftTemplate, makerLimitPrice],
  );
  const previewQuery = useQuery({
    queryKey: [
      "scalp-schedule-preview",
      status.environment_id,
      instrumentRef,
      chartDirection,
      JSON.stringify(draftTemplate),
    ],
    queryFn: ({ signal }) => previewOrderSchedule(
      {
        decision_basis_kind: "DIRECT_EXECUTION",
        venue_ref: "BINANCE_USDM",
        instrument_ref: instrumentRef,
        direction: chartDirection,
        max_notional: draftTemplate.notional,
        schedule_ref: `scalp-template-${chartDirection.toLowerCase()}`,
        spec: draftScheduleSpec,
      },
      signal,
    ),
    enabled: instrumentRef.length > 0
      && templateValid
      && marketStream.status === "LIVE"
      && marketStream.quote !== null
      && (draftTemplate.entry_mode === "MARKET" || makerLimitPrice !== null),
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
  const preview = previewQuery.data;
  const previewReady = preview?.valid === true;

  const resultsQuery = useQuery({
    queryKey: [
      "scalp-results",
      status.environment_id,
      status.account_id,
      resultScope,
      resultScope === "ANCHOR" ? anchorAt : null,
    ],
    queryFn: () => getScalpResults(
      resultScope,
      resultScope === "ANCHOR" ? anchorAt : null,
    ),
    enabled: resultScope !== "ANCHOR" || anchorAt !== null,
    refetchInterval: (query) => (
      query.state.data?.latest_cycle?.result_status === "OPEN"
        || query.state.data?.aggregate.unknown_result_count ? 2_000 : false
    ),
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
  const results: ScalpResults | null = resultsQuery.data ?? null;
  const latestCycle = results?.latest_cycle ?? null;
  const hasOpenCycle = latestCycle?.result_status === "OPEN";
  const venueDetailQuery = useQuery({
    queryKey: ["scalp-venue-detail", status.environment_id, latestCycle?.activation_id],
    queryFn: () => getActivation(latestCycle?.activation_id ?? ""),
    enabled: latestCycle !== null,
    refetchInterval: hasOpenCycle ? 2_000 : false,
  });
  const venueProjection = useMemo(
    () => scalpVenueProjection(venueDetailQuery.data),
    [venueDetailQuery.data],
  );

  const recommendationMutation = useMutation({
    mutationFn: () => getScalpRecommendation(instrumentRef, draftTemplate),
    onSuccess: (recommendation) => {
      setDraftTemplate(recommendation.recommended_template);
      setRecommendationNote(
        `${recommendation.basis.join(" · ")} · ${formatUserVisibleTime(recommendation.source_cutoff)}`,
      );
      setFeedback({ severity: "info", text: "建议已应用；保存模板后可交易。" });
    },
    onError: (error) => {
      setFeedback({
        severity: "error",
        text: failureText(error, "暂时无法生成参数建议"),
      });
    },
  });

  const acceptCreatedCycle = useCallback((created: ScalpCycleCreateResult, request: ScalpTriggerRequest) => {
    if (pendingTriggerRef.current?.idempotencyKey !== request.idempotencyKey
      || created.cycle.idempotency_key !== request.idempotencyKey) return;
    clearPendingScalpRequest(request, status.environment_id, status.account_id);
    pendingTriggerRef.current = null;
    setPendingTrigger(null);
    setChartDirection(created.cycle.direction);
    setFeedback({
      severity: "success",
      text: `${created.cycle.instrument_ref} ${created.cycle.direction === "LONG" ? "做多" : "做空"}周期已创建，等待交易所回报。`,
    });
    void queryClient.invalidateQueries({
      queryKey: ["scalp-results", status.environment_id, status.account_id],
    });
  }, [queryClient, status.account_id, status.environment_id]);

  const tradeMutation = useMutation({
    mutationFn: ({
      direction,
      idempotencyKey,
      requestInstrumentRef,
      requestTemplate,
    }: {
      direction: OrderScheduleDirection;
      idempotencyKey: string;
      requestInstrumentRef: string;
      requestTemplate: ScalpTemplate;
    }) => createScalpCycle(
      {
        instrument_ref: requestInstrumentRef,
        direction,
        template: requestTemplate,
      },
      idempotencyKey,
    ),
    retry: false,
    onSuccess: (created) => {
      if (pendingTriggerRef.current) acceptCreatedCycle(created, pendingTriggerRef.current);
    },
    onError: (error) => {
      if (!isUnknownMutationResult(error) && pendingTriggerRef.current) {
        clearPendingScalpRequest(pendingTriggerRef.current, status.environment_id, status.account_id);
        pendingTriggerRef.current = null;
        setPendingTrigger(null);
      }
      setFeedback({
        severity: isUnknownMutationResult(error) ? "warning" : "error",
        text: isUnknownMutationResult(error)
          ? "启动结果待确认，正在查询原请求。"
          : failureText(error, "当前条件不允许启动周期"),
      });
      void resultsQuery.refetch();
    },
  });

  const pendingTriggerQuery = useQuery({
    queryKey: ["scalp-trigger-request", status.environment_id, status.account_id, pendingTrigger?.idempotencyKey],
    queryFn: () => getScalpCycleByIdempotency(pendingTrigger!.idempotencyKey),
    enabled: pendingTrigger !== null && !tradeMutation.isPending,
    retry: false,
    refetchInterval: 2_000,
    refetchIntervalInBackground: false,
  });
  useEffect(() => {
    if (pendingTrigger && pendingTriggerQuery.data) {
      acceptCreatedCycle(pendingTriggerQuery.data, pendingTrigger);
    }
  }, [acceptCreatedCycle, pendingTrigger, pendingTriggerQuery.data]);

  const exitPreviewMutation = useMutation({
    mutationFn: (activationId: string) => previewControl(activationId, "EXIT_STRATEGY"),
    onSuccess: setExitPreview,
    onError: (error) => setFeedback({
      severity: "error",
      text: failureText(error, "暂时无法核对退出责任"),
    }),
  });
  const applyExitReceipt = useCallback((receipt: Record<string, unknown>, request: ScalpExitRequest) => {
    if (pendingExitRef.current?.idempotencyKey !== request.idempotencyKey) return;
    const outcome = scalpExitOutcome(receipt);
    if (outcome !== "UNKNOWN") {
      clearPendingScalpRequest(request, status.environment_id, status.account_id);
      pendingExitRef.current = null;
      setPendingExit(null);
      setExitPreview(null);
    }
    setFeedback({
      severity: outcome === "APPLIED" ? "success" : outcome === "REJECTED" ? "error" : "warning",
      text: outcome === "APPLIED"
        ? "退出指令已接受，等待撤单和平仓结果。"
        : outcome === "REJECTED"
          ? receipt.reason_code === "PLAN_VERSION_CONFLICT"
            ? "周期状态已变化，退出指令未被接受；请重新核对退出。"
            : `退出指令被拒绝${typeof receipt.reason_code === "string" ? `（${receipt.reason_code}）` : ""}。`
          : "退出结果待确认，正在查询原请求。",
    });
    void queryClient.invalidateQueries({ queryKey: ["scalp-results", status.environment_id, status.account_id] });
  }, [queryClient, status.account_id, status.environment_id]);

  const exitMutation = useMutation({
    mutationFn: (request: ScalpExitRequest) => submitActivationControl(
      request.activationId, "EXIT_STRATEGY", { expected_version: request.expectedVersion }, request.idempotencyKey,
    ),
    retry: false,
    onSuccess: applyExitReceipt,
    onError: (error, request) => {
      if (!isUnknownMutationResult(error)) {
        clearPendingScalpRequest(request, status.environment_id, status.account_id);
        pendingExitRef.current = null;
        setPendingExit(null);
      }
      setFeedback({
        severity: isUnknownMutationResult(error) ? "warning" : "error",
        text: isUnknownMutationResult(error)
          ? "退出结果待确认，正在查询原请求。"
          : failureText(error, "退出指令未被接受"),
      });
    },
  });

  const pendingExitQuery = useQuery({
    queryKey: ["scalp-exit-request", status.environment_id, status.account_id, pendingExit?.idempotencyKey],
    queryFn: () => getScalpExitByIdempotency(pendingExit!.activationId, pendingExit!.idempotencyKey),
    enabled: pendingExit !== null && !exitMutation.isPending,
    retry: false,
    refetchInterval: 2_000,
    refetchIntervalInBackground: false,
  });
  useEffect(() => {
    if (pendingExit && pendingExitQuery.data) applyExitReceipt(pendingExitQuery.data, pendingExit);
  }, [applyExitReceipt, pendingExit, pendingExitQuery.data]);

  const requestExit = () => {
    if (!exitPreview || pendingExitRef.current || status.profile === "BINANCE_LIVE_READ_ONLY") return;
    const request: ScalpExitRequest = {
      kind: "EXIT", idempotencyKey: crypto.randomUUID(),
      activationId: exitPreview.activation.activation_id,
      expectedVersion: exitPreview.activation.state_version,
    };
    if (!savePendingScalpRequest(request, status.environment_id, status.account_id)) {
      setFeedback({ severity: "error", text: "退出请求身份未能保存，请先核对已有请求。" });
      return;
    }
    pendingExitRef.current = request;
    setPendingExit(request);
    exitMutation.mutate(request);
  };

  const editTemplate = useCallback((field: ScalpTemplateField, value: string | number) => {
    setDraftTemplate((current) => ({ ...current, [field]: value }));
    setRecommendationNote(null);
    setFeedback(null);
  }, []);

  const saveTemplate = useCallback(() => {
    if (!templateValid) return;
    const stored = saveScalpTemplate(
      status.environment_id,
      status.account_id,
      draftTemplate,
    );
    if (!stored) {
      setFeedback({ severity: "error", text: "浏览器未能保存模板。" });
      return;
    }
    setSavedTemplate(draftTemplate);
    setFeedback({ severity: "success", text: "模板已保存。" });
  }, [draftTemplate, status.account_id, status.environment_id, templateValid]);

  const tradeBlockers = useMemo(() => {
    const blockers: string[] = [];
    if (pendingTrigger) blockers.push("上次启动结果待确认");
    if (pendingExit) blockers.push("退出结果待确认");
    if (status.profile === "BINANCE_LIVE_READ_ONLY") {
      blockers.push("当前真实账户为只读模式");
    }
    if (resultsQuery.isPending || resultsQuery.isError || resultsQuery.isFetching) {
      blockers.push("正在核对当前周期");
    }
    if (!status.database_available) blockers.push("账户事实不可用");
    if (status.executor_status !== "READY") blockers.push("执行器未就绪");
    if (status.app_executor_product_build_consistent === false) {
      blockers.push("App 与执行器版本不一致");
    }
    if (
      status.profile === "BINANCE_LIVE_WRITE"
      && status.runtime_real_write_gate !== "OPEN"
    ) {
      blockers.push("真实账户交易开关未打开");
    }
    if (
      status.profile === "BINANCE_LIVE_WRITE"
      && status.live_write_gate_violations.length > 0
    ) blockers.push("真实账户交易开关绑定无效");
    if (selectedContract === null) blockers.push("请选择合约");
    if (!templateValid) blockers.push("模板参数无效");
    if (templateDirty) blockers.push("先保存模板");
    if (!previewReady) blockers.push("订单预览未就绪");
    if (marketStream.status !== "LIVE" || marketStream.quote === null) {
      blockers.push("实时买卖价未就绪");
    }
    if (
      spreadBps !== null
      && spreadBps > Number(savedTemplate?.max_spread_bps)
    ) {
      blockers.push("当前价差超过模板上限");
    }
    if (hasOpenCycle) blockers.push("已有运行中周期");
    return blockers;
  }, [
    marketStream.quote,
    marketStream.status,
    previewReady,
    savedTemplate?.max_spread_bps,
    pendingTrigger,
    pendingExit,
    resultsQuery.isPending,
    resultsQuery.isError,
    resultsQuery.isFetching,
    selectedContract,
    hasOpenCycle,
    spreadBps,
    status.app_executor_product_build_consistent,
    status.database_available,
    status.executor_status,
    status.live_write_gate_violations,
    status.profile,
    status.runtime_real_write_gate,
    templateDirty,
    templateValid,
  ]);
  const tradeReady = tradeBlockers.length === 0 && !tradeMutation.isPending;

  const triggerTrade = useCallback((direction: OrderScheduleDirection) => {
    if (!tradeReady || !savedTemplate || pendingTriggerRef.current || pendingExitRef.current) return;
    const request: ScalpTriggerRequest = {
      kind: "TRIGGER", idempotencyKey: crypto.randomUUID(), instrumentRef, direction,
    };
    if (!savePendingScalpRequest(request, status.environment_id, status.account_id)) {
      setFeedback({ severity: "error", text: "启动请求身份未能保存，请先核对已有请求。" });
      return;
    }
    pendingTriggerRef.current = request;
    setPendingTrigger(request);
    setFeedback(null);
    tradeMutation.mutate({
      direction,
      idempotencyKey: request.idempotencyKey,
      requestInstrumentRef: instrumentRef,
      requestTemplate: savedTemplate,
    });
  }, [
    instrumentRef,
    savedTemplate,
    status.account_id,
    status.environment_id,
    tradeMutation,
    tradeReady,
  ]);

  const setFromNow = useCallback(() => {
    const nextAnchor = new Date().toISOString();
    setAnchorAt(nextAnchor);
    setResultScope("ANCHOR");
    saveScalpAnchor(status.environment_id, status.account_id, nextAnchor);
  }, [status.account_id, status.environment_id]);

  const chooseInstrument = useCallback((next: BinanceContractCatalog["contracts"][number] | null) => {
    if (!next) return;
    setInstrumentRef(next.instrument_ref);
    setChartInterval(
      readChartIntervalPreference(status.environment_id, next.instrument_ref, "1m"),
    );
    saveScalpInstrument(
      status.environment_id,
      status.account_id,
      next.instrument_ref,
    );
    setFeedback(null);
  }, [status.account_id, status.environment_id]);

  const consequence = accountConsequence(status);
  const maximumProjectedLoss = preview?.full_fill_protection_estimate?.maximum_projected_loss ?? null;
  const previewIssue = preview?.issues[0]?.code ?? null;
  const tradeDisabledReason = tradeBlockers[0] ?? null;
  const aggregate = results?.aggregate ?? null;

  return (
    <Box
      sx={{
        width: "min(1780px, 100%)",
        mx: "auto",
        px: { xs: 1.25, sm: 2, xl: 2.5 },
        py: { xs: 1.5, md: 2 },
      }}
    >
      <Typography component="h1" sx={visuallyHiddenSx}>剥头皮</Typography>

      <Box
        sx={{
          ...surfaceFrameSx,
          p: { xs: 1.25, sm: 1.5 },
          mb: 1.5,
          display: "grid",
          gridTemplateColumns: { xs: "1fr", md: "minmax(280px, 420px) 1fr" },
          gap: { xs: 1.25, md: 2 },
          alignItems: "center",
        }}
      >
        <Autocomplete
          size="small"
          options={contracts}
          value={selectedContract}
          loading={catalogQuery.isPending}
          disabled={tradeMutation.isPending}
          onChange={(_event, next) => chooseInstrument(next)}
          isOptionEqualToValue={(option, value) => option.instrument_ref === value.instrument_ref}
          getOptionLabel={(option) => `${option.symbol} · ${option.base_asset}`}
          filterOptions={(options, state) => {
            const search = state.inputValue.trim().toUpperCase();
            if (!search) return options.slice(0, 120);
            return options.filter((option) => (
              option.symbol.includes(search)
              || option.base_asset.includes(search)
              || option.instrument_ref.includes(search)
            )).slice(0, 120);
          }}
          renderInput={(params) => (
            <TextField
              {...params}
              label="Binance U 本位永续"
              placeholder="搜索 BTC、ETH 或 symbol"
              error={catalogQuery.isError}
              helperText={catalogQuery.isError ? "合约目录暂不可用" : `${contracts.length} 个可交易合约`}
            />
          )}
          noOptionsText="没有匹配合约"
        />
        <Box
          aria-label="当前买卖价"
          sx={{
            display: "grid",
            gridTemplateColumns: { xs: "repeat(3, minmax(0, 1fr))", sm: "repeat(4, minmax(0, 1fr))" },
            gap: 1.5,
          }}
        >
          <Metric label="买一" value={bidPrice ? `${tradingPrice(bidPrice)} USDT` : "—"} />
          <Metric label="卖一" value={askPrice ? `${tradingPrice(askPrice)} USDT` : "—"} />
          <Metric label="价差" value={spreadBps === null ? "—" : `${spreadBps.toFixed(2)} bps`} />
          <Box sx={{ display: { xs: "none", sm: "block" } }}>
            <Metric
              label="行情截止"
              value={formatUserVisibleTime(
                marketStream.quote?.source_cutoff ?? null,
              )}
            />
          </Box>
        </Box>
      </Box>

      <Box
        sx={{
          display: "grid",
          gridTemplateAreas: {
            xs: '"command" "latest" "aggregate" "chart"',
            md: '"chart command" "latest aggregate"',
          },
          gridTemplateColumns: { xs: "minmax(0, 1fr)", md: "minmax(0, 1fr) 370px" },
          gridTemplateRows: { md: "clamp(560px, calc(100vh - 330px), 680px) auto" },
          gap: 1.5,
          alignItems: "stretch",
        }}
      >
        <Box
          sx={{
            gridArea: "chart",
            minWidth: 0,
            minHeight: { xs: 620, md: 0 },
            display: "grid",
            gridTemplateColumns: { xs: "minmax(0, 1fr)", md: "minmax(0, 1fr) minmax(0, 1fr)" },
            gridTemplateRows: { xs: "minmax(0, 1fr) minmax(0, 1fr)", md: "minmax(0, 1fr)" },
            gap: 1.5,
          }}
        >
          <Box sx={{ minWidth: 0, minHeight: 0 }}>
            <Suspense fallback={<LinearProgress aria-label="正在加载行情图" />}>
              <OrderScheduleChart
                workspaceMode
                readOnlyDraft
                draftProjectionLabel="模板投影"
                environmentId={status.environment_id}
                environmentKind={status.environment_kind}
                instrumentRef={instrumentRef}
                direction={hasOpenCycle && latestCycle ? latestCycle.direction : chartDirection}
                marketColorScheme={marketColorScheme}
                interval={chartInterval}
                onIntervalChange={(interval) => {
                  setChartInterval(interval);
                  writeChartIntervalPreference(status.environment_id, instrumentRef, interval);
                }}
                liveBar={marketStream.liveBar}
                streamStatus={marketStream.status}
                streamGeneration={marketStream.generation}
                priceProjectionReady={previewReady}
                priceTickSize={preview?.instrument_rules.price_tick_size ?? null}
                referencePrice={preview?.reference_price ?? referencePrice}
                spec={(preview?.schedule_spec as OrderScheduleSpec | undefined) ?? draftScheduleSpec}
                previewLegs={preview?.normalized_legs ?? []}
                fullFillProtectionEstimate={preview?.full_fill_protection_estimate ?? null}
                additionalPriceAnnotations={
                  latestCycle?.instrument_ref === instrumentRef
                    ? venueProjection.priceAnnotations
                    : []
                }
                previewState={previewQuery.isPending ? "PENDING" : previewReady ? "READY" : "BLOCKED"}
                onRangeChange={() => undefined}
                onSingleLimitPriceChange={() => undefined}
              />
            </Suspense>
          </Box>
          <Box sx={{ minWidth: 0, minHeight: 0 }}>
            <MarketFlowTape
              depth={marketStream.depth}
              trades={marketStream.trades}
              streamStatus={marketStream.status}
              priceTickSize={preview?.instrument_rules.price_tick_size ?? null}
            />
          </Box>
        </Box>

        <Box
          component="section"
          aria-labelledby="scalp-command-title"
          sx={{
            ...surfaceFrameSx,
            gridArea: "command",
            p: { xs: 1.5, sm: 1.75 },
            minHeight: 0,
            overflowY: { md: "auto" },
          }}
        >
          <Stack direction="row" spacing={1} sx={{ alignItems: "center", justifyContent: "space-between" }}>
            <Typography id="scalp-command-title" component="h2" variant="h2">
              一键交易
            </Typography>
            <Chip
              size="small"
              color={templateDirty ? "warning" : "success"}
              variant="outlined"
              label={savedTemplate === null ? "模板未保存" : templateDirty ? "模板有改动" : "模板已保存"}
            />
          </Stack>
          <Stack direction="row" spacing={1} sx={{ mt: 1.5, alignItems: "baseline" }}>
            <Typography className="mono" sx={{ fontSize: 28, lineHeight: 1, fontWeight: 820 }}>
              {draftTemplate.notional || "—"}
            </Typography>
            <Typography variant="body2" color="text.secondary">USDT / 次</Typography>
          </Stack>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: .75 }}>
            {draftTemplate.entry_mode === "MAKER_ONLY_SAME_SIDE"
              ? `同向价1 · Maker Only${makerLimitPrice ? ` · ${chartDirection === "LONG" ? "买一" : "卖一"} ${makerLimitPrice}` : ""}`
              : "市价入场"}
            {" · "}止损 {draftTemplate.initial_stop_bps || "—"} bps · 止盈 {draftTemplate.take_profit_r || "—"} R
          </Typography>
          {consequence && status.environment_kind === "LIVE" ? (
            <Alert severity="warning" sx={{ mt: 1.25, py: 0 }}>{consequence}</Alert>
          ) : null}
          <Box
            sx={{ mb: 1.25 }}
          >
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: .5 }}>
              入场方式
            </Typography>
            <ToggleButtonGroup
              exclusive
              size="small"
              value={draftTemplate.entry_mode}
              onChange={(_event, value: ScalpTemplate["entry_mode"] | null) => {
                if (value) editTemplate("entry_mode", value);
              }}
              aria-label="入场方式"
            >
              <ToggleButton value="MARKET">市价</ToggleButton>
              <ToggleButton value="MAKER_ONLY_SAME_SIDE">同向价1 · Maker</ToggleButton>
            </ToggleButtonGroup>
          </Box>
          <Box
            sx={{
              display: "grid",
              gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
              gap: 1,
              mt: 1.5,
            }}
          >
            {(["LONG", "SHORT"] as const).map((direction) => (
              <Tooltip key={direction} title={!tradeReady && tradeDisabledReason ? tradeDisabledReason : ""} arrow>
                <span>
                  <Button
                    fullWidth
                    variant="contained"
                    color="inherit"
                    disabled={!tradeReady}
                    aria-label={`${direction === "LONG" ? "做多" : "做空"}一键启动周期`}
                    onClick={() => triggerTrade(direction)}
                    startIcon={<BoltOutlined />}
                    sx={{
                      minHeight: 68,
                      "&:not(.Mui-disabled)": {
                        color: "#fff",
                        bgcolor: direction === "LONG" ? "var(--halpha-market-up)" : "var(--halpha-market-down)",
                      },
                      "&:not(.Mui-disabled):hover": {
                        color: "#fff",
                        bgcolor: direction === "LONG" ? "var(--halpha-market-up)" : "var(--halpha-market-down)",
                        filter: "brightness(.92)",
                      },
                    }}
                  >
                    {tradeMutation.isPending ? "启动中" : direction === "LONG" ? "做多" : "做空"}
                  </Button>
                </span>
              </Tooltip>
            ))}
          </Box>
          <Stack
            direction="row"
            spacing={1}
            useFlexGap
            sx={{ mt: .75, minHeight: 24, alignItems: "center", justifyContent: "space-between" }}
          >
            <Typography
              aria-live="polite"
              variant="caption"
              color={tradeReady ? "success.dark" : "text.secondary"}
              sx={{ minHeight: 18, minWidth: 0 }}
            >
              {tradeReady ? "已就绪" : tradeBlockers.slice(0, 2).join(" · ")}
            </Typography>
            <Stack direction="row" spacing={.5} sx={{ alignItems: "center", flexShrink: 0 }}>
              <Typography variant="caption" color="text.secondary" noWrap>图表投影</Typography>
              <ToggleButtonGroup
                exclusive
                size="small"
                value={chartDirection}
                onChange={(_event, value: OrderScheduleDirection | null) => {
                  if (value) setChartDirection(value);
                }}
                aria-label="图表投影方向"
              >
                <ToggleButton value="LONG" disabled={hasOpenCycle}>做多</ToggleButton>
                <ToggleButton value="SHORT" disabled={hasOpenCycle}>做空</ToggleButton>
              </ToggleButtonGroup>
            </Stack>
          </Stack>

          <Divider sx={{ my: 1.5 }} />
          <Stack direction="row" spacing={1} sx={{ alignItems: "center", justifyContent: "space-between", mb: 1.25 }}>
            <Typography component="h3" variant="h3">模板</Typography>
            <Stack direction="row" spacing={.5}>
              <Button
                size="small"
                variant="text"
                startIcon={<AutoAwesomeOutlined />}
                disabled={recommendationMutation.isPending || !instrumentRef}
                onClick={() => recommendationMutation.mutate()}
              >
                {recommendationMutation.isPending ? "计算中" : "应用建议"}
              </Button>
              <Button
                size="small"
                variant="contained"
                startIcon={<SaveOutlined />}
                disabled={!templateDirty || !templateValid}
                onClick={saveTemplate}
              >
                保存
              </Button>
            </Stack>
          </Stack>
          <Box
            sx={{
              display: "grid",
              gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
              gap: 1,
            }}
          >
            <TextField
              size="small"
              label="名义金额（USDT）"
              value={draftTemplate.notional}
              onChange={(event) => editTemplate("notional", event.target.value)}
              error={Boolean(templateErrors.notional)}
              helperText={templateErrors.notional}
              slotProps={{ htmlInput: { inputMode: "decimal" } }}
            />
            <TextField
              size="small"
              label="止损（bps）"
              value={draftTemplate.initial_stop_bps}
              onChange={(event) => editTemplate("initial_stop_bps", event.target.value)}
              error={Boolean(templateErrors.initial_stop_bps)}
              helperText={templateErrors.initial_stop_bps}
              slotProps={{ htmlInput: { inputMode: "decimal" } }}
            />
            <TextField
              size="small"
              label="止盈（R）"
              value={draftTemplate.take_profit_r}
              onChange={(event) => editTemplate("take_profit_r", event.target.value)}
              error={Boolean(templateErrors.take_profit_r)}
              helperText={templateErrors.take_profit_r}
              slotProps={{ htmlInput: { inputMode: "decimal" } }}
            />
            <TextField
              size="small"
              type="number"
              label="最长持有（秒）"
              value={draftTemplate.max_holding_seconds}
              onChange={(event) => editTemplate("max_holding_seconds", Number(event.target.value))}
              error={Boolean(templateErrors.max_holding_seconds)}
              helperText={templateErrors.max_holding_seconds}
              slotProps={{ htmlInput: { min: 30, max: 86400, step: 1 } }}
            />
            <TextField
              size="small"
              label="最大价差（bps）"
              value={draftTemplate.max_spread_bps}
              onChange={(event) => editTemplate("max_spread_bps", event.target.value)}
              error={Boolean(templateErrors.max_spread_bps)}
              helperText={templateErrors.max_spread_bps}
              slotProps={{ htmlInput: { inputMode: "decimal" } }}
            />
            <TextField
              size="small"
              label="最大预计损失"
              value={maximumProjectedLoss ? `${quoteCurrencyAmount(maximumProjectedLoss)} USDT` : "待预览"}
              slotProps={{ htmlInput: { readOnly: true } }}
            />
          </Box>
          <Box
            component="details"
            sx={{
              mt: 1.25,
              "& > summary": { cursor: "pointer", color: "text.secondary", fontSize: 12, fontWeight: 700 },
            }}
          >
            <Box component="summary">费用假设</Box>
            <Box sx={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 1, mt: 1 }}>
              <TextField
                size="small"
                label="入场费（bps）"
                value={draftTemplate.entry_fee_bps}
                onChange={(event) => editTemplate("entry_fee_bps", event.target.value)}
                error={Boolean(templateErrors.entry_fee_bps)}
                helperText={templateErrors.entry_fee_bps}
                slotProps={{ htmlInput: { inputMode: "decimal" } }}
              />
              <TextField
                size="small"
                label="退出费（bps）"
                value={draftTemplate.exit_fee_bps}
                onChange={(event) => editTemplate("exit_fee_bps", event.target.value)}
                error={Boolean(templateErrors.exit_fee_bps)}
                helperText={templateErrors.exit_fee_bps}
                slotProps={{ htmlInput: { inputMode: "decimal" } }}
              />
            </Box>
          </Box>
          {recommendationNote ? (
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
              {recommendationNote}
            </Typography>
          ) : null}
          {previewIssue ? (
            <Typography variant="caption" color="error.dark" sx={{ display: "block", mt: 1 }}>
              当前预览不可提交（{previewIssue}）
            </Typography>
          ) : null}
          {feedback ? (
            <Alert severity={feedback.severity} sx={{ mt: 1.25, py: 0 }}>
              {feedback.text}
            </Alert>
          ) : null}
          {pendingTrigger && !tradeMutation.isPending ? (
            <Alert severity="warning" sx={{ mt: 1 }}>
              {pendingTrigger.instrumentRef} {pendingTrigger.direction === "LONG" ? "做多" : "做空"}启动结果待确认。
              {pendingTriggerQuery.isError ? "原请求暂不可读。" : "正在查询原请求。"}
              <Button size="small" onClick={() => void pendingTriggerQuery.refetch()}>核对启动结果</Button>
            </Alert>
          ) : null}
          {pendingExit && !exitMutation.isPending ? (
            <Alert severity="warning" sx={{ mt: 1 }}>
              退出结果待确认。{pendingExitQuery.isError ? "原请求暂不可读。" : "正在查询原请求。"}
              <Button size="small" onClick={() => void pendingExitQuery.refetch()}>核对退出结果</Button>
            </Alert>
          ) : null}
        </Box>

        <Box
          component="section"
          aria-labelledby="latest-scalp-title"
          sx={{ ...surfaceFrameSx, gridArea: "latest", p: { xs: 1.5, sm: 1.75 }, minWidth: 0 }}
        >
          <Stack direction="row" spacing={1} sx={{ alignItems: "center", justifyContent: "space-between", mb: 1.5 }}>
            <Box>
              <Typography id="latest-scalp-title" component="h2" variant="h2">最近一笔</Typography>
              {latestCycle ? (
                <Typography variant="caption" color="text.secondary">
                  {formatUserVisibleTime(latestCycle.triggered_at)}
                </Typography>
              ) : null}
            </Box>
            {latestCycle?.result_status === "OPEN" ? (
              <Button
                ref={exitButtonRef}
                size="small"
                color="error"
                variant="outlined"
                startIcon={<CloseOutlined />}
                disabled={exitPreviewMutation.isPending || pendingExit !== null || status.profile === "BINANCE_LIVE_READ_ONLY"}
                onClick={() => exitPreviewMutation.mutate(latestCycle.activation_id)}
              >
                退出周期
              </Button>
            ) : null}
          </Stack>
          {resultsQuery.isPending ? <LinearProgress aria-label="正在读取最近周期" /> : null}
          {resultsQuery.isError ? <Alert severity="error">最近周期暂不可读。</Alert> : null}
          {!resultsQuery.isPending && !resultsQuery.isError && latestCycle === null ? (
            <Typography variant="body2" color="text.secondary">还没有周期。</Typography>
          ) : null}
          {latestCycle ? (
            <>
              <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: "wrap", mb: 1.5, alignItems: "center" }}>
                <Typography className="mono" sx={{ fontWeight: 800 }}>{latestCycle.instrument_ref}</Typography>
                <Chip
                  size="small"
                  variant="outlined"
                  label={(
                    <MarketToneText tone={marketToneForDirection(latestCycle.direction)}>
                      {latestCycle.direction === "LONG" ? "做多" : "做空"}
                    </MarketToneText>
                  )}
                />
                <Chip
                  size="small"
                  color={latestCycle.result_status === "OPEN" ? "warning" : latestCycle.result_status === "RELIABLE" ? "success" : "default"}
                  label={RESULT_STATUS_LABELS[latestCycle.result_status] ?? latestCycle.result_status}
                />
                {venueDetailQuery.isError ? (
                  <Chip size="small" color="warning" variant="outlined" label="交易所状态暂不可读" />
                ) : venueDetailQuery.isPending ? (
                  <Chip size="small" variant="outlined" label="正在读取交易所状态" />
                ) : (
                  <Chip
                    size="small"
                    color={venueStatusColor(venueProjection.status)}
                    variant="outlined"
                    label={venueProjection.label}
                  />
                )}
              </Stack>
              {!venueDetailQuery.isError && !venueDetailQuery.isPending ? (
                <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
                  {venueProjection.detail}
                  {venueProjection.factCutoff ? ` · 交易所事实 ${formatUserVisibleTime(venueProjection.factCutoff)}` : ""}
                </Typography>
              ) : null}
              <MetricGrid>
                <Metric label="毛盈亏" value={signedMoney(latestCycle.gross_pnl, latestCycle.currency ?? "USDT")} signedValue={latestCycle.gross_pnl} />
                <Metric label="手续费" value={feeMoney(latestCycle.commission, latestCycle.currency ?? "USDT")} signedValue={latestCycle.commission === null ? null : `-${latestCycle.commission}`} />
                <Metric label="资金费" value={signedMoney(latestCycle.funding, latestCycle.currency ?? "USDT")} signedValue={latestCycle.funding} />
                <Metric label="净盈亏" value={signedMoney(latestCycle.net_pnl, latestCycle.currency ?? "USDT")} signedValue={latestCycle.net_pnl} prominent />
                <Metric label="入场名义金额" value={latestCycle.entry_notional === null ? "—" : `${quoteCurrencyAmount(latestCycle.entry_notional)} USDT`} />
                <Metric label="持仓时长" value={holdingLabel(latestCycle.holding_duration_seconds)} />
              </MetricGrid>
            </>
          ) : null}
        </Box>

        <Box
          component="section"
          aria-labelledby="scalp-aggregate-title"
          sx={{ ...surfaceFrameSx, gridArea: "aggregate", p: { xs: 1.5, sm: 1.75 }, minWidth: 0 }}
        >
          <Stack
            direction={{ xs: "column", sm: "row" }}
            spacing={1}
            sx={{ alignItems: { sm: "center" }, justifyContent: "space-between", mb: 1.5 }}
          >
            <Box>
              <Typography id="scalp-aggregate-title" component="h2" variant="h2">累计</Typography>
              <Typography variant="caption" color="text.secondary">
                {resultScope === "ANCHOR" && anchorAt
                  ? `起点 ${formatUserVisibleTime(anchorAt)}`
                  : results ? `事实截止 ${formatUserVisibleTime(results.fact_cutoff)}` : "仅累计可靠结算"}
              </Typography>
            </Box>
            <Stack direction="row" spacing={.75} sx={{ alignItems: "center", flexWrap: "wrap" }}>
              <ToggleButtonGroup
                exclusive
                size="small"
                value={resultScope}
                onChange={(_event, next: ScalpResultScope | null) => {
                  if (!next) return;
                  if (next === "ANCHOR") setFromNow();
                  else setResultScope(next);
                }}
                aria-label="累计范围"
              >
                {(Object.keys(RESULT_SCOPE_LABELS) as ScalpResultScope[]).map((scope) => (
                  <ToggleButton key={scope} value={scope}>{RESULT_SCOPE_LABELS[scope]}</ToggleButton>
                ))}
              </ToggleButtonGroup>
              {resultScope === "ANCHOR" ? (
                <Button size="small" variant="text" onClick={setFromNow}>更新起点</Button>
              ) : null}
            </Stack>
          </Stack>
          {resultsQuery.isFetching && !results ? <LinearProgress aria-label="正在读取累计结果" /> : null}
          {resultsQuery.isError ? <Alert severity="error">累计结果暂不可读。</Alert> : null}
          {aggregate ? (
            <MetricGrid>
              <Metric label="已结算" value={`${aggregate.reliable_trade_count} 笔`} />
              <Metric label="胜 / 负 / 平" value={`${aggregate.win_count} / ${aggregate.loss_count} / ${aggregate.flat_count}`} />
              <Metric label="胜率" value={aggregate.win_rate === null ? "—" : `${Number(aggregate.win_rate).toFixed(1)}%`} />
              <Metric label="运行中 / 待核对 / 未成交" value={`${aggregate.open_cycle_count} / ${aggregate.unknown_result_count} / ${aggregate.no_trade_cycle_count}`} />
              <Metric label="毛盈亏" value={signedMoney(aggregate.gross_pnl)} signedValue={aggregate.gross_pnl} />
              <Metric label="手续费" value={feeMoney(aggregate.commission)} signedValue={aggregate.commission === null ? null : `-${aggregate.commission}`} />
              <Metric label="资金费" value={signedMoney(aggregate.funding)} signedValue={aggregate.funding} />
              <Metric label="净盈亏" value={signedMoney(aggregate.net_pnl)} signedValue={aggregate.net_pnl} prominent />
            </MetricGrid>
          ) : null}
        </Box>
      </Box>

      <Dialog
        open={exitPreview !== null}
        onClose={() => { if (!exitMutation.isPending) setExitPreview(null); }}
        maxWidth="sm"
        fullWidth
        slotProps={{
          transition: {
            onExited: () => exitButtonRef.current?.focus(),
          },
        }}
      >
        <DialogTitle>退出当前周期</DialogTitle>
        <DialogContent>
          {status.profile === "BINANCE_LIVE_READ_ONLY" ? <Alert severity="warning" sx={{ mb: 1 }}>当前真实账户为只读模式，无法提交退出。</Alert> : null}
          {pendingExit && !exitMutation.isPending ? <Alert severity="warning" sx={{ mb: 1 }}>退出结果待确认，正在查询原请求。</Alert> : null}
          {feedback?.severity === "error" ? <Alert severity="error" sx={{ mb: 1 }}>{feedback.text}</Alert> : null}
          <Typography variant="body2">
            {exitPreview?.consequence ?? "停止新增风险并退出本周期持仓与挂单。"}
          </Typography>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
            退出按当前交易所事实执行；预览本身不会产生交易所请求。
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button disabled={exitMutation.isPending} onClick={() => setExitPreview(null)}>取消</Button>
          <Button
            color="error"
            variant="contained"
            disabled={!exitPreview || exitMutation.isPending || pendingExit !== null || status.profile === "BINANCE_LIVE_READ_ONLY"}
            onClick={requestExit}
          >
            {exitMutation.isPending ? "退出中" : "确认退出"}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
