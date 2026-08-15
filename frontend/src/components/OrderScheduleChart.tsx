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
  TextField,
  Tooltip,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
  useMediaQuery,
  useTheme,
} from "@mui/material";
import { Close, InfoOutlined } from "@mui/icons-material";
import {
  dispose,
  getOverlayClass,
  init,
  registerOverlay,
  type Chart,
  type Crosshair,
  type KLineData,
  type OverlayEvent,
  type OverlayTemplate,
  type Point,
} from "klinecharts";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent,
} from "react";

import {
  ApiFailure,
  getMarketWindow,
  type MarketInterval,
  type OrderScheduleDirection,
  type OrderScheduleFullFillProtectionEstimate,
  type OrderSchedulePreviewLeg,
  type OrderScheduleSpec,
} from "../api/client";
import { quoteAmount, tradingPrice } from "../format";

import type { MarketColorScheme } from "../marketColors";
import type {
  MarketStreamBar,
  MarketStreamClientStatus,
} from "../marketStream";
import {
  expectedMarketSourceForEnvironment,
  isUsableMarketStreamBar,
  MARKET_STREAM_STALE_AFTER_MS,
  shouldUseMarketStreamBar,
} from "../marketStream";
import {
  buildOrderScheduleChartAnnotations,
  chartPeriod,
  chartPriceInput,
  expandedVisiblePriceRange,
  groupNearbyPriceAnnotations,
  marketIntervalForPeriod,
  marketWindowBounds,
  ORDER_CHART_INTERVALS,
  ORDER_CHART_WINDOW_BAR_COUNT,
  orderedPriceRange,
  priceAnnotationTagMultiplicity,
  priceTagAxisLayout,
  selectPriceAnnotationForTag,
  shouldBlockChartSurface,
  spreadChartLabelAnchors,
  summarizeRelativeRules,
  type OrderChartPriceAnnotation,
} from "./orderScheduleChartModel";
import type {
  RuntimeChartOperationCategory,
  RuntimeChartOperationMarker,
} from "../runtimeChartMarkers";
import { compactRuntimeMarkerGroupLabel } from "../runtimeChartMarkers";

const SCHEDULE_GROUP = "halpha-order-schedule";
const ANALYSIS_GROUP = "halpha-analysis-drawings";
const EXECUTION_WINDOW_OVERLAY = "halphaExecutionWindow";
const PRICE_TAG_OVERLAY = "halphaCollisionAwarePriceTag";
const EXECUTION_WINDOW_FILL = "rgba(37, 99, 235, 0.075)";
const EXECUTION_WINDOW_BORDER = "rgba(37, 99, 235, 0.42)";
const PRICE_TAG_MIN_GAP = 30;
const PRICE_TAG_EDGE_PADDING = 11;
const PRICE_AXIS_WIDTH_NARROW = 128;
const PRICE_AXIS_WIDTH_DESKTOP = 136;
const PRICE_AXIS_POSITION = "right" as const;
const ANALYSIS_TIME_FORMATTER = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
const EVENT_TIME_FORMATTER = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

type ExecutionWindowOverlayData = {
  ongoing: boolean;
};

type PriceTagOverlayAnchor = {
  id: string;
  price: number;
};

type PriceTagOverlayData = {
  id: string;
  label: string;
  anchors: PriceTagOverlayAnchor[];
};

function ensureExecutionWindowOverlay(): void {
  if (getOverlayClass(EXECUTION_WINDOW_OVERLAY)) return;
  const template: OverlayTemplate<ExecutionWindowOverlayData> = {
    name: EXECUTION_WINDOW_OVERLAY,
    totalStep: 3,
    needDefaultPointFigure: false,
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: false,
    createPointFigures: ({ chart, overlay, coordinates, bounding }) => {
      const startX = coordinates[0]?.x;
      if (!Number.isFinite(startX)) return [];
      const halfBar = chart.getBarSpace().halfGapBar;
      let endX = coordinates[1]?.x;
      if (overlay.extendData.ongoing) {
        const lastBar = chart.getDataList().at(-1);
        const converted = lastBar
          ? chart.convertToPixel(
            { timestamp: lastBar.timestamp },
            { paneId: "candle_pane" },
          )
          : null;
        const lastCoordinate = Array.isArray(converted) ? converted[0] : converted;
        endX = lastCoordinate?.x;
      }
      if (!Number.isFinite(endX)) return [];
      const rawLeft = (startX as number) - halfBar;
      const rawRight = (endX as number) + halfBar;
      if (rawRight <= 0 || rawLeft >= bounding.width) return [];
      const left = Math.max(0, rawLeft);
      const right = Math.min(bounding.width, rawRight);
      if (right <= left) return [];
      return {
        type: "rect",
        attrs: {
          x: left,
          y: 0,
          width: right - left,
          height: bounding.height,
        },
        styles: {
          style: "stroke_fill",
          color: EXECUTION_WINDOW_FILL,
          borderColor: EXECUTION_WINDOW_BORDER,
          borderSize: 1,
          borderStyle: "solid",
          borderDashedValue: [],
          borderRadius: 0,
        },
        ignoreEvent: true,
      };
    },
  };
  registerOverlay(template);
}

ensureExecutionWindowOverlay();

function ensureCollisionAwarePriceTagOverlay(): void {
  if (getOverlayClass(PRICE_TAG_OVERLAY)) return;
  const template: OverlayTemplate<PriceTagOverlayData> = {
    name: PRICE_TAG_OVERLAY,
    totalStep: 2,
    needDefaultPointFigure: false,
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: false,
    createPointFigures: () => [],
    createYAxisFigures: ({
      overlay,
      coordinates,
      bounding,
      yAxis,
    }) => {
      const actualY = coordinates[0]?.y;
      if (!Number.isFinite(actualY) || !yAxis) return [];
      const spread = spreadChartLabelAnchors(
        overlay.extendData.anchors.map((anchor) => ({
          id: anchor.id,
          y: yAxis.convertToPixel(anchor.price),
        })),
        bounding.height,
        PRICE_TAG_MIN_GAP,
        PRICE_TAG_EDGE_PADDING,
      );
      const labelY = spread.find(
        (anchor) => anchor.id === overlay.extendData.id,
      )?.y ?? actualY as number;
      const axisLayout = priceTagAxisLayout(
        PRICE_AXIS_POSITION,
        bounding.width,
      );

      return [
        {
          type: "line",
          attrs: {
            coordinates: [
              { x: axisLayout.plotEdgeX, y: actualY as number },
              { x: axisLayout.elbowX, y: actualY as number },
              { x: axisLayout.labelLeadX, y: labelY },
              { x: axisLayout.labelX, y: labelY },
            ],
          },
          ignoreEvent: true,
        },
        {
          type: "text",
          attrs: {
            x: axisLayout.labelX,
            y: labelY,
            text: overlay.extendData.label,
            align: axisLayout.labelAlign,
            baseline: "middle",
          },
          ignoreEvent: true,
        },
      ];
    },
  };
  registerOverlay(template);
}

ensureCollisionAwarePriceTagOverlay();

type AnalysisDrawing = {
  id: string;
  name: "horizontalStraightLine" | "straightLine";
  points: Array<Partial<Point>>;
};

type DrawRange = {
  startY: number;
  currentY: number;
  startPrice: number;
  currentPrice: number;
};

type ResolvedOperationMarker = {
  marker: RuntimeChartOperationMarker;
  barTimestamp: number;
  displayPrice: number;
  contextualPrice: boolean;
};

function operationMarkerPriceLabel(item: ResolvedOperationMarker): string {
  if (item.contextualPrice) return "K 线价";
  return item.marker.priceKind === "EVENT" ? "触发价" : "成交价";
}

function operationMarkerPriceNote(item: ResolvedOperationMarker): string {
  if (item.contextualPrice) return "（对应 K 线收盘价，不是成交价）";
  return item.marker.priceKind === "EVENT" ? "（事件观测价格）" : "（实际成交价）";
}

export type OrderScheduleChartProps = {
  workspaceMode?: boolean;
  displayMode?: "DRAFT" | "RUNTIME";
  chartPurpose?: "ORDER_PLAN" | "STRATEGY_INPUT";
  runtimePhase?: "RUNNING" | "REVIEW";
  showPlanEntryAnnotations?: boolean;
  additionalPriceAnnotations?: OrderChartPriceAnnotation[];
  visibleAdditionalPriceAnnotationIds?: ReadonlySet<string>;
  onAdditionalPriceAnnotationVisibilityChange?: (
    annotationId: string,
    visible: boolean,
  ) => void;
  operationMarkers?: RuntimeChartOperationMarker[];
  timeWindowMode?: "LIVE" | "EXECUTION";
  timeWindowEndAt?: string | null;
  executionWindowStartAt?: string | null;
  executionWindowEndAt?: string | null;
  executionWindowFullyVisible?: boolean;
  onToggleTimeWindow?: () => void;
  environmentId: string;
  environmentKind: string;
  instrumentRef: string;
  direction: OrderScheduleDirection;
  marketColorScheme: MarketColorScheme;
  interval: MarketInterval;
  onIntervalChange: (interval: MarketInterval) => void;
  liveBar: MarketStreamBar | null;
  streamStatus: MarketStreamClientStatus;
  streamGeneration: number;
  priceProjectionReady: boolean;
  priceTickSize: string | null;
  referencePrice: string | null;
  spec: OrderScheduleSpec;
  previewLegs: OrderSchedulePreviewLeg[];
  fullFillProtectionEstimate?: OrderScheduleFullFillProtectionEstimate | null;
  previewState: "PENDING" | "READY" | "BLOCKED";
  onRangeChange: (lowerPrice: string, upperPrice: string) => void;
  onSingleLimitPriceChange: (price: string) => void;
  onMarketReadinessChange?: (ready: boolean) => void;
};

function finitePrice(value: string): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function pricePrecision(values: string[]): number {
  return Math.min(
    8,
    Math.max(
      1,
      ...values.map((value) => value.split(".")[1]?.replace(/0+$/, "").length ?? 0),
    ),
  );
}

function marketFailureText(
  error: unknown,
  displayMode: "DRAFT" | "RUNTIME",
  strategyInput: boolean,
): string {
  if (
    error instanceof Error
    && error.message === "MARKET_WINDOW_SOURCE_MISMATCH"
  ) {
    return "K 线来源与当前环境不一致，已拒绝显示；请核对运行环境后重试。";
  }
  const fallback = strategyInput
    ? "策略数值仍以右侧标明来源和截止时间的行情事实为准。"
    : displayMode === "RUNTIME"
      ? "运行事实仍以服务端记录为准；重试后可恢复图表。"
      : "仍可编辑数字草稿，但价格预览与保存已阻断。";
  if (!(error instanceof ApiFailure)) {
    return `K 线窗口读取失败；${fallback}`;
  }
  if (error.code === "MARKET_WINDOW_RANGE_INVALID") {
    return "K 线窗口范围不可用；请刷新当前行情后重试。";
  }
  return `K 线窗口读取失败（${error.code}）；${fallback}`;
}

function toKLineData(bar: MarketStreamBar["bar"]): KLineData | null {
  const timestamp = Date.parse(bar.open_at);
  const open = Number(bar.open);
  const high = Number(bar.high);
  const low = Number(bar.low);
  const close = Number(bar.close);
  const volume = Number(bar.volume);
  if (
    !Number.isFinite(timestamp)
    || ![open, high, low, close].every(Number.isFinite)
  ) {
    return null;
  }
  return {
    timestamp,
    open,
    high,
    low,
    close,
    ...(Number.isFinite(volume) ? { volume } : {}),
  };
}

function streamStatusLabel(
  status: MarketStreamClientStatus,
  hasAppliedLiveBar: boolean,
): string {
  if (status === "LIVE") return "K线实时";
  if (status === "STALE") return hasAppliedLiveBar ? "K线已过期" : "K线待同步";
  if (status === "RECONNECTING") return "K线重连中";
  if (status === "CONNECTING") return "K线连接中";
  if (status === "FAILED") return "K线实时流不可用";
  return "K线实时流未启用";
}

function streamStatusColor(
  status: MarketStreamClientStatus,
): "success" | "warning" | "error" | "default" {
  if (status === "LIVE") return "success";
  if (status === "STALE" || status === "RECONNECTING") return "warning";
  if (status === "FAILED") return "error";
  return "default";
}

function chartMarketSourceLabel(source: string): string {
  if (source === "BINANCE_LIVE_PUBLIC") return "Live · Binance K线";
  if (source === "BINANCE_DEMO_PUBLIC") return "Demo · Binance K线";
  return "来源不匹配";
}

function chartMarketSourceDescription(source: string): string {
  const isolation = `图表历史、实时 K 线和当前价均使用 ${source}；不同环境的数据不会在本图中拼接。`;
  if (source === "BINANCE_DEMO_PUBLIC") {
    return `${isolation} Demo 场所可能成交稀疏，重复价位、长影线或跳价会按原始 K 线保留，不代表实盘流动性。`;
  }
  return isolation;
}

function compactMarketCutoff(sourceCutoff: string | null): string | null {
  if (!sourceCutoff || !Number.isFinite(Date.parse(sourceCutoff))) return null;
  return ANALYSIS_TIME_FORMATTER.format(new Date(sourceCutoff));
}

const ANNOTATION_TAG_ABBREVIATIONS: Record<
  OrderChartPriceAnnotation["role"],
  string
> = {
  REFERENCE: "参",
  SINGLE_LIMIT: "限",
  RANGE_LOWER: "下",
  RANGE_UPPER: "上",
  NORMALIZED_LEG: "档",
  MARK_CONDITION: "触",
  ENTRY_INVALIDATION: "失",
  RUNTIME_ENTRY: "单",
  POSITION: "仓",
  STOP_REFERENCE: "荐",
  PROTECTION: "损",
  TAKE_PROFIT: "盈",
};

function annotationTagAbbreviation(annotation: OrderChartPriceAnnotation): string {
  if (annotation.id === "halpha-entry-opportunity-missed-price") return "错";
  if (annotation.authority === "SERVER_PREVIEW" && annotation.role === "PROTECTION") {
    return "预损";
  }
  if (annotation.authority === "SERVER_PREVIEW" && annotation.role === "TAKE_PROFIT") {
    return "预盈";
  }
  return ANNOTATION_TAG_ABBREVIATIONS[annotation.role];
}

function annotationTagName(annotation: OrderChartPriceAnnotation): string {
  if (annotation.id === "halpha-entry-opportunity-missed-price") return "错失";
  if (
    annotation.authority === "SERVER_PREVIEW"
    && annotation.role === "PROTECTION"
  ) return "预计止损";
  if (
    annotation.authority === "SERVER_PREVIEW"
    && annotation.role === "TAKE_PROFIT"
  ) return "预计止盈";
  if (annotation.role === "REFERENCE") return "参考";
  if (annotation.role === "SINGLE_LIMIT") return "限价";
  if (annotation.role === "RANGE_LOWER") return "下限";
  if (annotation.role === "RANGE_UPPER") return "上限";
  if (annotation.role === "MARK_CONDITION") return "触发";
  if (annotation.role === "ENTRY_INVALIDATION") return "失效";
  if (annotation.role === "RUNTIME_ENTRY") return "入场动作";
  if (annotation.role === "POSITION") return "持仓均价";
  if (annotation.role === "STOP_REFERENCE") return "止损参考";
  if (annotation.role === "PROTECTION") return "止损";
  if (annotation.role === "TAKE_PROFIT") return "止盈";
  return annotation.label.replace("标准化入场 ", "档 ");
}

function annotationGroupTag(
  group: OrderChartPriceAnnotation[],
  compact: boolean,
  precision: number,
  priceTickSize: string | null,
): string {
  const primary = selectPriceAnnotationForTag(group);
  if (!primary) return "";
  const name = compact
    ? annotationTagAbbreviation(primary)
    : annotationTagName(primary);
  const displayPrice = compact
    ? chartPriceInput(Number(primary.price.toFixed(Math.min(precision, 4))))
    : chartPriceInput(primary.price, priceTickSize);
  const multiplicity = priceAnnotationTagMultiplicity(group, primary);
  if (multiplicity > 1) {
    return compact
      ? `${name} ${displayPrice} ×${multiplicity}`
      : `${name} ${displayPrice} · ${multiplicity} 项`;
  }
  const effectiveAmounts = group.flatMap((annotation) => (
    annotation.effectiveNotional ? [quoteAmount(annotation.effectiveNotional)] : []
  ));
  const amount = !compact && effectiveAmounts.length === 1
    ? ` · ${effectiveAmounts[0]}U`
    : "";
  return `${name} ${displayPrice}${amount}`;
}

function clonePoints(points: Array<Partial<Point>>): Array<Partial<Point>> {
  return points.map((point) => ({ ...point }));
}

function analysisPriceText(
  point: Partial<Point> | undefined,
  priceTickSize: string | null,
): string {
  if (!point || !Number.isFinite(point.value)) return "价格未定";
  return `${chartPriceInput(point.value as number, priceTickSize)} USDT`;
}

function analysisPointText(
  point: Partial<Point> | undefined,
  priceTickSize: string | null,
): string {
  const price = analysisPriceText(point, priceTickSize);
  if (!point || price === "价格未定") return price;
  if (!Number.isFinite(point.timestamp)) return price;
  return `${ANALYSIS_TIME_FORMATTER.format(new Date(point.timestamp as number))} @ ${price}`;
}

function analysisDrawingText(
  drawing: AnalysisDrawing,
  index: number,
  priceTickSize: string | null,
): string {
  if (drawing.name === "horizontalStraightLine") {
    return `分析 ${index + 1} · 支撑 / 阻力 · ${analysisPriceText(drawing.points[0], priceTickSize)}`;
  }
  return `分析 ${index + 1} · 趋势线 · ${analysisPointText(drawing.points[0], priceTickSize)} → ${analysisPointText(drawing.points[1], priceTickSize)}`;
}

function eventDrawing(event: OverlayEvent<unknown>): AnalysisDrawing | null {
  const name = event.overlay.name;
  if (name !== "horizontalStraightLine" && name !== "straightLine") return null;
  return {
    id: event.overlay.id,
    name,
    points: clonePoints(event.overlay.points),
  };
}

function chartPointAt(chart: Chart, x: number, y: number): Partial<Point> | null {
  const converted = chart.convertFromPixel(
    [{ x, y }],
    { paneId: "candle_pane" },
  );
  const point = Array.isArray(converted) ? converted[0] : converted;
  return point && Number.isFinite(point.value) ? point : null;
}

function lineStyle(
  color: string,
  style: OrderChartPriceAnnotation["lineStyle"] = "solid",
) {
  const dashed = style !== "solid";
  return {
    line: {
      color,
      size: dashed ? 1 : 2,
      style: dashed ? "dashed" as const : "solid" as const,
      dashedValue: style === "dotted" ? [2, 4] : dashed ? [6, 4] : [4, 2],
    },
    point: {
      color,
      borderColor: "#FFFFFF",
      activeColor: color,
      activeBorderColor: "#FFFFFF",
    },
    text: {
      color: "#FFFFFF",
      backgroundColor: color,
    },
  };
}

function operationMarkerColor(category: RuntimeChartOperationCategory): string {
  if (category === "ENTRY") return "#0369A1";
  if (category === "PROTECTION") return "#DC2626";
  if (category === "TAKE_PROFIT") return "#16A34A";
  if (category === "CANCEL") return "#B45309";
  if (category === "PLAN") return "#EA580C";
  if (category === "CONTROL") return "#475569";
  return "#7C3AED";
}

function intervalMilliseconds(interval: MarketInterval): number {
  return {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
  }[interval];
}

function containingBarTimestamp(
  bars: KLineData[],
  timestamp: number,
  intervalMs: number,
): number | null {
  if (bars.length === 0 || !Number.isFinite(timestamp)) return null;
  const firstTimestamp = bars[0]?.timestamp;
  const lastTimestamp = bars.at(-1)?.timestamp;
  if (!Number.isFinite(firstTimestamp) || !Number.isFinite(lastTimestamp)) {
    return null;
  }
  if (timestamp <= (firstTimestamp as number)) return firstTimestamp as number;
  if (timestamp >= (lastTimestamp as number) + intervalMs) {
    return lastTimestamp as number;
  }
  for (let index = bars.length - 1; index >= 0; index -= 1) {
    const barTimestamp = bars[index]?.timestamp;
    if (Number.isFinite(barTimestamp) && (barTimestamp as number) <= timestamp) {
      return barTimestamp as number;
    }
  }
  return firstTimestamp as number;
}

function fitLoadedBarsToChart(chart: Chart, barCount: number): void {
  if (barCount <= 0) return;
  const paneWidth = chart.getSize("candle_pane", "main")?.width
    ?? chart.getDom("candle_pane", "main")?.clientWidth
    ?? 0;
  if (paneWidth <= 0) return;
  const usableWidth = Math.max(
    120,
    paneWidth - chart.getOffsetRightDistance(),
  );
  chart.setBarSpace(Math.max(2, Math.min(50, usableWidth / barCount)));
  chart.scrollToRealTime();
}

function annotationColor(
  annotation: OrderChartPriceAnnotation,
  direction: OrderScheduleDirection,
): string {
  if (annotation.role === "REFERENCE") return "#64748B";
  if (annotation.role === "SINGLE_LIMIT") return "#2563EB";
  if (annotation.role === "RANGE_LOWER") return "#2563EB";
  if (annotation.role === "RANGE_UPPER") return "#7C3AED";
  if (annotation.role === "MARK_CONDITION") return "#B45309";
  if (annotation.role === "ENTRY_INVALIDATION") return "#C2410C";
  if (annotation.role === "RUNTIME_ENTRY") return "#0369A1";
  if (annotation.role === "POSITION") return "#475569";
  if (annotation.role === "STOP_REFERENCE") return "#9A6700";
  if (annotation.role === "PROTECTION") return "#DC2626";
  // White 11 px abbreviations require the darker success tone to preserve
  // WCAG AA contrast; the brighter market green remains available to lines.
  if (annotation.role === "TAKE_PROFIT") return "#15803D";
  return direction === "LONG" ? "#0F766E" : "#B45309";
}

function annotationAuthorityLabel(
  authority: OrderChartPriceAnnotation["authority"],
): string {
  if (authority === "MARKET") return "行情";
  if (authority === "SERVER_FACT") return "已生效";
  if (authority === "SERVER_PREVIEW") return "预览";
  return "草稿";
}

function compactRuntimeRelativeRules(
  rules: ReturnType<typeof buildOrderScheduleChartAnnotations>["relativeRules"],
): string {
  const stop = rules.find((rule) => rule.id === "halpha-initial-stop");
  const takeProfitCount = rules.filter(
    (rule) => rule.id.startsWith("halpha-take-profit-"),
  ).length;
  const otherCount = rules.length - (stop ? 1 : 0) - takeProfitCount;
  return [
    stop ? stop.label.replace("每笔成交后止损 · ", "止损 ") : null,
    takeProfitCount > 0 ? `${takeProfitCount} 档止盈` : null,
    otherCount > 0 ? `${otherCount} 项动态规则` : null,
  ].filter(Boolean).join(" · ") || "无固定价格规则";
}

export default function OrderScheduleChart({
  workspaceMode = false,
  displayMode = "DRAFT",
  chartPurpose = "ORDER_PLAN",
  runtimePhase = "RUNNING",
  showPlanEntryAnnotations = true,
  additionalPriceAnnotations = [],
  visibleAdditionalPriceAnnotationIds,
  onAdditionalPriceAnnotationVisibilityChange,
  operationMarkers = [],
  timeWindowMode = "LIVE",
  timeWindowEndAt = null,
  executionWindowStartAt = null,
  executionWindowEndAt = null,
  executionWindowFullyVisible = true,
  onToggleTimeWindow,
  environmentId,
  environmentKind,
  instrumentRef,
  direction,
  marketColorScheme,
  interval,
  onIntervalChange,
  liveBar,
  streamStatus,
  streamGeneration,
  priceProjectionReady,
  priceTickSize,
  referencePrice,
  spec,
  previewLegs,
  fullFillProtectionEstimate = null,
  previewState,
  onRangeChange,
  onSingleLimitPriceChange,
  onMarketReadinessChange,
}: OrderScheduleChartProps) {
  const theme = useTheme();
  const narrow = useMediaQuery(theme.breakpoints.down("sm"));
  const strategyInput = chartPurpose === "STRATEGY_INPUT";
  const compactTagLabels = narrow || displayMode === "RUNTIME";
  const environmentScope = `${environmentKind}:${environmentId}`;
  const expectedMarketSource = expectedMarketSourceForEnvironment(environmentKind);
  const pricePlan = spec.price_distribution;
  const lowerPrice = pricePlan.kind === "LADDER" ? pricePlan.lower_price : "";
  const upperPrice = pricePlan.kind === "LADDER" ? pricePlan.upper_price : "";
  const chartContainerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<Chart | null>(null);
  const instrumentRefRef = useRef(instrumentRef);
  const intervalRef = useRef(interval);
  const expectedMarketSourceRef = useRef(expectedMarketSource);
  const timeWindowEndAtRef = useRef(timeWindowEndAt);
  const barsRef = useRef<KLineData[]>([]);
  const liveBarSpaceRef = useRef<number | null>(null);
  const appliedIntervalRef = useRef<MarketInterval | null>(null);
  const loaderRequestRef = useRef(0);
  const liveSubscriptionRef = useRef<{
    interval: MarketInterval;
    callback: (data: KLineData) => void;
  } | null>(null);
  const streamGenerationRef = useRef(streamGeneration);
  const lastStreamGenerationRef = useRef(streamGeneration);
  const latestRangeRef = useRef({ lowerPrice, upperPrice });
  const onRangeChangeRef = useRef(onRangeChange);
  const onSingleLimitPriceChangeRef = useRef(onSingleLimitPriceChange);
  const precisionRef = useRef(4);
  const narrowRef = useRef(narrow);
  const analysisDrawingsRef = useRef<AnalysisDrawing[]>([]);
  const activeAnalysisOverlayRef = useRef<string | null>(null);
  const undoRangeRef = useRef<{ lowerPrice: string; upperPrice: string } | null>(null);
  const undoSingleLimitRef = useRef<string | null>(null);
  const analysisHandlersRef = useRef<{
    persist: (event: OverlayEvent<unknown>) => void;
    remove: (event: OverlayEvent<unknown>) => void;
  }>({ persist: () => undefined, remove: () => undefined });
  const [chartGeneration, setChartGeneration] = useState(0);
  const [includeAnnotationsInScale, setIncludeAnnotationsInScale] = useState(false);
  const [rangeMode, setRangeMode] = useState(false);
  const [drawRange, setDrawRange] = useState<DrawRange | null>(null);
  const [activeTool, setActiveTool] = useState<"SUPPORT" | "TREND" | null>(null);
  const [analysisDrawings, setAnalysisDrawings] = useState<AnalysisDrawing[]>([]);
  const [hiddenLocalPriceAnnotationIds, setHiddenLocalPriceAnnotationIds] = useState(
    () => new Set<string>(),
  );
  const [bars, setBars] = useState<KLineData[]>([]);
  const [marketWindowLoading, setMarketWindowLoading] = useState(false);
  const [marketWindowError, setMarketWindowError] = useState<unknown>(null);
  const [marketWindowSource, setMarketWindowSource] = useState<string | null>(null);
  const [marketWindowSourceCutoff, setMarketWindowSourceCutoff] = useState<string | null>(null);
  const [freshnessRevision, setFreshnessRevision] = useState(0);
  const [hoveredOperationBarTimestamp, setHoveredOperationBarTimestamp] = useState<number | null>(null);
  const eventPanelPageSize = narrow ? 4 : 6;
  const [visibleOperationEventCount, setVisibleOperationEventCount] = useState(
    eventPanelPageSize,
  );
  const [statusMessage, setStatusMessage] = useState(
    strategyInput
      ? "策略关键价格只读投影；修改右侧参数后，通道与追价边界会随当前策略输入更新。"
      : "",
  );
  const liveBarOpenTimestamp = Date.parse(liveBar?.bar.open_at ?? "");
  const focusedWindowEndTimestamp = Date.parse(timeWindowEndAt ?? "");
  const liveBarWithinRequestedWindow = timeWindowMode !== "EXECUTION"
    || (
      Number.isFinite(liveBarOpenTimestamp)
      && Number.isFinite(focusedWindowEndTimestamp)
      && liveBarOpenTimestamp <= focusedWindowEndTimestamp
    );
  const matchingLiveBar = liveBar
    && liveBarWithinRequestedWindow
    && marketWindowSource
    && shouldUseMarketStreamBar(
      liveBar,
      instrumentRef,
      interval,
      marketWindowSource,
    )
    && isUsableMarketStreamBar(
      liveBar,
      expectedMarketSource,
      Date.now(),
    )
    ? liveBar
    : null;

  instrumentRefRef.current = instrumentRef;
  intervalRef.current = interval;
  expectedMarketSourceRef.current = expectedMarketSource;
  timeWindowEndAtRef.current = timeWindowEndAt;
  barsRef.current = bars;
  streamGenerationRef.current = streamGeneration;
  latestRangeRef.current = { lowerPrice, upperPrice };
  onRangeChangeRef.current = onRangeChange;
  onSingleLimitPriceChangeRef.current = onSingleLimitPriceChange;
  narrowRef.current = narrow;
  analysisDrawingsRef.current = analysisDrawings;

  const annotations = useMemo(() => {
    const scheduleAnnotations = priceProjectionReady
      ? buildOrderScheduleChartAnnotations({
        direction,
        referencePrice,
        spec,
        previewLegs,
        previewState,
        priceTickSize,
        fullFillProtectionEstimate,
      })
      : { priceAnnotations: [], relativeRules: [] };
    const schedulePriceAnnotations = scheduleAnnotations.priceAnnotations
      .filter((annotation) => {
        if (displayMode !== "RUNTIME") return true;
        if (["PROTECTION", "TAKE_PROFIT"].includes(annotation.role)) {
          return false;
        }
        if (annotation.role === "REFERENCE") return true;
        return showPlanEntryAnnotations;
      })
      .map((annotation) => displayMode === "RUNTIME"
        ? { ...annotation, draggable: false }
        : annotation);
    const additionalIds = new Set(
      additionalPriceAnnotations.map((annotation) => annotation.id),
    );
    const allPriceAnnotations = schedulePriceAnnotations.concat(additionalPriceAnnotations);
    const priceAnnotations = allPriceAnnotations.filter((annotation) => (
      additionalIds.has(annotation.id) && visibleAdditionalPriceAnnotationIds
        ? visibleAdditionalPriceAnnotationIds.has(annotation.id)
        : !hiddenLocalPriceAnnotationIds.has(annotation.id)
    ));
    return {
      ...scheduleAnnotations,
      allPriceAnnotations,
      priceAnnotations,
      visiblePriceAnnotationIds: new Set(priceAnnotations.map((annotation) => annotation.id)),
      additionalIds,
    };
  }, [
      additionalPriceAnnotations,
      displayMode,
      direction,
      previewLegs,
      fullFillProtectionEstimate,
      previewState,
      priceProjectionReady,
      referencePrice,
      showPlanEntryAnnotations,
      spec,
      priceTickSize,
      hiddenLocalPriceAnnotationIds,
      visibleAdditionalPriceAnnotationIds,
    ]);

  const operationMarkerBars = useMemo(() => {
    const currentLiveBar = matchingLiveBar
      ? toKLineData(matchingLiveBar.bar)
      : null;
    if (!currentLiveBar) return bars;
    const existingIndex = bars.findIndex(
      (bar) => bar.timestamp === currentLiveBar.timestamp,
    );
    if (existingIndex >= 0) {
      const merged = [...bars];
      merged[existingIndex] = currentLiveBar;
      return merged;
    }
    const lastTimestamp = bars.at(-1)?.timestamp;
    return lastTimestamp === undefined || lastTimestamp < currentLiveBar.timestamp
      ? [...bars, currentLiveBar]
      : bars;
  }, [bars, matchingLiveBar]);
  const resolvedOperationMarkers = useMemo<ResolvedOperationMarker[]>(() => {
    const firstBar = operationMarkerBars[0];
    const lastBar = operationMarkerBars.at(-1);
    if (!firstBar || !lastBar) return [];
    const firstTimestamp = firstBar.timestamp;
    const lastTimestamp = lastBar.timestamp;
    const intervalMs = intervalMilliseconds(interval);
    return operationMarkers.flatMap((marker) => {
      const markerTimestamp = Date.parse(marker.at);
      if (
        !Number.isFinite(markerTimestamp)
        || markerTimestamp < firstTimestamp
        || markerTimestamp >= lastTimestamp + intervalMs
      ) {
        return [];
      }
      let bar = firstBar;
      for (let index = operationMarkerBars.length - 1; index >= 0; index -= 1) {
        const candidate = operationMarkerBars[index];
        if (candidate && candidate.timestamp <= markerTimestamp) {
          bar = candidate;
          break;
        }
      }
      const displayPrice = Number.isFinite(marker.price)
        ? marker.price as number
        : bar.close;
      return [{
        marker,
        barTimestamp: bar.timestamp,
        displayPrice,
        contextualPrice: marker.price === null,
      }];
    });
  }, [interval, operationMarkerBars, operationMarkers]);
  const executionWindowStartTimestamp = Date.parse(
    executionWindowStartAt ?? "",
  );
  const executionWindowEndTimestamp = Date.parse(
    executionWindowEndAt ?? "",
  );
  const hasExecutionWindow = displayMode === "RUNTIME"
    && Number.isFinite(executionWindowStartTimestamp);
  const loadedFirstTimestamp = bars[0]?.timestamp;
  const loadedEndTimestamp = Number.isFinite(bars.at(-1)?.timestamp)
    ? (bars.at(-1)?.timestamp as number) + intervalMilliseconds(interval)
    : Number.NaN;
  const effectiveExecutionEndTimestamp = Number.isFinite(
    executionWindowEndTimestamp,
  )
    ? executionWindowEndTimestamp
    : loadedEndTimestamp;
  const executionWindowIntersectsBars = hasExecutionWindow
    && Number.isFinite(loadedFirstTimestamp)
    && Number.isFinite(loadedEndTimestamp)
    && executionWindowStartTimestamp < loadedEndTimestamp
    && effectiveExecutionEndTimestamp >= (loadedFirstTimestamp as number);
  const operationMarkerGroups = useMemo(() => {
    const groups = new Map<number, ResolvedOperationMarker[]>();
    resolvedOperationMarkers.forEach((marker) => {
      const group = groups.get(marker.barTimestamp) ?? [];
      group.push(marker);
      groups.set(marker.barTimestamp, group);
    });
    return groups;
  }, [resolvedOperationMarkers]);
  const operationMarkerGroupsRef = useRef(operationMarkerGroups);
  operationMarkerGroupsRef.current = operationMarkerGroups;
  const hoveredOperationGroup = hoveredOperationBarTimestamp === null
    ? null
    : operationMarkerGroups.get(hoveredOperationBarTimestamp) ?? null;
  const visibleHoveredOperationEvents = hoveredOperationGroup?.slice(
    0,
    visibleOperationEventCount,
  ) ?? [];
  const hiddenHoveredOperationEventCount = Math.max(
    0,
    (hoveredOperationGroup?.length ?? 0) - visibleOperationEventCount,
  );
  useEffect(() => {
    setHoveredOperationBarTimestamp(null);
  }, [interval, timeWindowEndAt, timeWindowMode]);
  useEffect(() => {
    setVisibleOperationEventCount(eventPanelPageSize);
  }, [eventPanelPageSize, hoveredOperationBarTimestamp]);
  const precision = useMemo(() => {
    if (priceTickSize) return pricePrecision([priceTickSize]);
    // Instrument rules arrive through the schedule preview. While that preview
    // is refreshing, keep the last venue precision instead of deriving a
    // temporary one from whichever annotations happen to be visible. Changing
    // symbol precision resets KLineChart's data loader, so a temporary fallback
    // here would create a market-window/preview request loop.
    return precisionRef.current;
  }, [priceTickSize]);
  precisionRef.current = precision;

  const replaceDrawing = useCallback((drawing: AnalysisDrawing) => {
    setAnalysisDrawings((current) => {
      const existing = current.some((item) => item.id === drawing.id);
      return existing
        ? current.map((item) => item.id === drawing.id ? drawing : item)
        : [...current, drawing];
    });
  }, []);

  analysisHandlersRef.current = {
    persist: (event) => {
      const drawing = eventDrawing(event);
      if (!drawing) return;
      replaceDrawing(drawing);
      activeAnalysisOverlayRef.current = null;
      setActiveTool(null);
      setStatusMessage(
        drawing.name === "straightLine"
          ? "趋势线已保留为本页分析绘图；它不会自动移动任何真实订单。"
          : "支撑/阻力线已保留为本页分析绘图；它不会自动形成订单或保护。",
      );
    },
    remove: (event) => {
      setAnalysisDrawings((current) => current.filter((item) => item.id !== event.overlay.id));
      if (activeAnalysisOverlayRef.current === event.overlay.id) {
        activeAnalysisOverlayRef.current = null;
        setActiveTool(null);
      }
    },
  };

  const analysisOverlay = useCallback((
    name: AnalysisDrawing["name"],
    options?: { id?: string; points?: Array<Partial<Point>>; locked?: boolean },
  ) => ({
    name,
    groupId: ANALYSIS_GROUP,
    ...(options?.id ? { id: options.id } : {}),
    ...(options?.points ? { points: options.points } : {}),
    mode: "weak_magnet" as const,
    lock: options?.locked ?? false,
    modeSensitivity: 8,
    needDefaultPointFigure: true,
    needDefaultXAxisFigure: true,
    needDefaultYAxisFigure: false,
    styles: lineStyle("#64748B"),
    onDrawEnd: (event: OverlayEvent<unknown>) => analysisHandlersRef.current.persist(event),
    onPressedMoveEnd: (event: OverlayEvent<unknown>) => analysisHandlersRef.current.persist(event),
    onRemoved: (event: OverlayEvent<unknown>) => analysisHandlersRef.current.remove(event),
  }), []);

  useEffect(() => {
    const container = chartContainerRef.current;
    if (!container) return undefined;
    const colors = marketColorScheme === "RED_UP_GREEN_DOWN"
      ? { up: "#D14343", down: "#138A5B" }
      : { up: "#138A5B", down: "#D14343" };
    const chart = init(container, {
      locale: "zh-CN",
      timezone: "Asia/Shanghai",
      layout: {
        yAxis: {
          position: PRICE_AXIS_POSITION,
          inside: false,
        },
      },
      styles: {
        grid: {
          horizontal: { color: "#EEF1F5" },
          vertical: { color: "#EEF1F5" },
        },
        yAxis: {
          size: narrowRef.current
            ? PRICE_AXIS_WIDTH_NARROW
            : PRICE_AXIS_WIDTH_DESKTOP,
        },
        candle: {
          type: "candle_solid",
          tooltip: {
            showRule: narrowRef.current ? "none" : "follow_cross",
          },
          bar: {
            upColor: colors.up,
            downColor: colors.down,
            noChangeColor: "#64748B",
            upBorderColor: colors.up,
            downBorderColor: colors.down,
            noChangeBorderColor: "#64748B",
            upWickColor: colors.up,
            downWickColor: colors.down,
            noChangeWickColor: "#64748B",
          },
        },
      },
    });
    if (!chart) return undefined;
    chartRef.current = chart;
    chart.setDataLoader({
      getBars: async ({ type, period, callback }) => {
        const requestedInterval = marketIntervalForPeriod(period);
        if (type === "backward" || type === "forward" || requestedInterval === null) {
          callback([], { backward: false, forward: false });
          return;
        }
        const bounds = marketWindowBounds(
          timeWindowEndAtRef.current ?? new Date().toISOString(),
          requestedInterval,
        );
        if (!bounds) {
          setBars([]);
          setMarketWindowSource(null);
          setMarketWindowSourceCutoff(null);
          setMarketWindowLoading(false);
          callback([], { backward: false, forward: false });
          return;
        }
        const requestId = ++loaderRequestRef.current;
        setMarketWindowLoading(true);
        setMarketWindowError(null);
        setBars([]);
        setMarketWindowSource(null);
        setMarketWindowSourceCutoff(null);
        try {
          const window = await getMarketWindow(
            instrumentRefRef.current,
            bounds.startAt,
            bounds.endAt,
            requestedInterval,
            "EXECUTION_REVIEW",
          );
          if (
            requestId !== loaderRequestRef.current
            || requestedInterval !== intervalRef.current
          ) {
            return;
          }
          if (
            expectedMarketSourceRef.current === null
            || window.source !== expectedMarketSourceRef.current
          ) {
            throw new Error("MARKET_WINDOW_SOURCE_MISMATCH");
          }
          const nextBars = window.bars.flatMap((marketBar) => {
            const normalized = toKLineData(marketBar);
            return normalized ? [normalized] : [];
          });
          setMarketWindowSource(window.source);
          setMarketWindowSourceCutoff(window.source_cutoff);
          setBars(nextBars);
          setMarketWindowLoading(false);
          callback(nextBars, { backward: false, forward: false });
        } catch (error) {
          if (
            requestId !== loaderRequestRef.current
            || requestedInterval !== intervalRef.current
          ) {
            return;
          }
          setBars([]);
          setMarketWindowSource(null);
          setMarketWindowSourceCutoff(null);
          setMarketWindowLoading(false);
          setMarketWindowError(error);
          callback([], { backward: false, forward: false });
        }
      },
      subscribeBar: ({ period, callback }) => {
        const requestedInterval = marketIntervalForPeriod(period);
        liveSubscriptionRef.current = requestedInterval
          ? { interval: requestedInterval, callback }
          : null;
      },
      unsubscribeBar: ({ period }) => {
        const requestedInterval = marketIntervalForPeriod(period);
        if (liveSubscriptionRef.current?.interval === requestedInterval) {
          liveSubscriptionRef.current = null;
        }
      },
    });
    chart.setSymbol({
      ticker: instrumentRefRef.current,
      pricePrecision: precisionRef.current,
      volumePrecision: 4,
    });
    appliedIntervalRef.current = intervalRef.current;
    chart.setPeriod(chartPeriod(intervalRef.current));
    chart.setOffsetRightDistance(72);
    const revealOperationGroup = (data?: unknown) => {
      const crosshair = data as Crosshair | undefined;
      const candleClick = data as {
        data?: { current?: { timestamp?: number } };
      } | undefined;
      const converted = Number.isFinite(crosshair?.x)
        ? chart.convertFromPixel([{ x: crosshair?.x }])
        : [];
      const coordinateTimestamp = Array.isArray(converted)
        ? converted[0]?.timestamp
        : converted.timestamp;
      const timestamp = Number(
        crosshair?.kLineData?.timestamp
        ?? candleClick?.data?.current?.timestamp
        ?? crosshair?.timestamp
        ?? coordinateTimestamp,
      );
      if (
        Number.isFinite(timestamp)
        && operationMarkerGroupsRef.current.has(timestamp)
      ) {
        setHoveredOperationBarTimestamp(timestamp);
      }
    };
    chart.subscribeAction("onCrosshairChange", revealOperationGroup);
    chart.subscribeAction("onCandleBarClick", revealOperationGroup);
    container.querySelector<HTMLElement>('[tabindex="1"]')?.setAttribute("tabindex", "-1");
    analysisDrawingsRef.current.forEach((drawing) => {
      chart.createOverlay(analysisOverlay(drawing.name, {
        id: drawing.id,
        points: drawing.points,
        locked: narrowRef.current,
      }));
    });
    let resizeFrame: number | null = null;
    let settledResizeFrame: number | null = null;
    const resizeForCurrentLayout = () => {
      chart.resize();
      if (timeWindowEndAtRef.current !== null) {
        fitLoadedBarsToChart(
          chart,
          barsRef.current.length || chart.getDataList().length,
        );
      }
    };
    const observer = new ResizeObserver(() => {
      if (resizeFrame !== null) window.cancelAnimationFrame(resizeFrame);
      if (settledResizeFrame !== null) {
        window.cancelAnimationFrame(settledResizeFrame);
      }
      resizeFrame = window.requestAnimationFrame(() => {
        resizeForCurrentLayout();
        settledResizeFrame = window.requestAnimationFrame(
          resizeForCurrentLayout,
        );
      });
    });
    observer.observe(container);
    lastStreamGenerationRef.current = streamGenerationRef.current;
    setChartGeneration((current) => current + 1);
    return () => {
      observer.disconnect();
      if (resizeFrame !== null) window.cancelAnimationFrame(resizeFrame);
      if (settledResizeFrame !== null) {
        window.cancelAnimationFrame(settledResizeFrame);
      }
      chart.unsubscribeAction("onCrosshairChange", revealOperationGroup);
      chart.unsubscribeAction("onCandleBarClick", revealOperationGroup);
      loaderRequestRef.current += 1;
      liveSubscriptionRef.current = null;
      if (chartRef.current === chart) chartRef.current = null;
      dispose(chart);
    };
  }, [analysisOverlay, marketColorScheme]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || chartGeneration === 0) return;
    const currentSymbol = chart.getSymbol();
    if (
      currentSymbol?.ticker === instrumentRef
      && currentSymbol.pricePrecision === precision
    ) {
      return;
    }
    chart.setSymbol({
      ticker: instrumentRef,
      pricePrecision: precision,
      volumePrecision: 4,
    });
  }, [chartGeneration, instrumentRef, precision]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || chartGeneration === 0) return;
    if (narrow) {
      setRangeMode(false);
      setDrawRange(null);
      if (activeAnalysisOverlayRef.current) {
        chart.removeOverlay({ id: activeAnalysisOverlayRef.current });
        activeAnalysisOverlayRef.current = null;
        setActiveTool(null);
      }
    }
    chart.overrideOverlay({ groupId: ANALYSIS_GROUP, lock: narrow });
    chart.setStyles({
      yAxis: {
        size: narrow
          ? PRICE_AXIS_WIDTH_NARROW
          : PRICE_AXIS_WIDTH_DESKTOP,
      },
      candle: {
        priceMark: { last: { text: { show: false } } },
        tooltip: { showRule: narrow ? "none" : "follow_cross" },
      },
    });
    let settledFrame: number | null = null;
    const resizeFrame = window.requestAnimationFrame(() => {
      chart.resize();
      settledFrame = window.requestAnimationFrame(() => {
        chart.resize();
        if (timeWindowEndAtRef.current !== null) {
          fitLoadedBarsToChart(
            chart,
            barsRef.current.length || chart.getDataList().length,
          );
        }
      });
    });
    return () => {
      window.cancelAnimationFrame(resizeFrame);
      if (settledFrame !== null) window.cancelAnimationFrame(settledFrame);
    };
  }, [chartGeneration, narrow]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || chartGeneration === 0 || appliedIntervalRef.current === interval) return;
    appliedIntervalRef.current = interval;
    setBars([]);
    setMarketWindowError(null);
    setMarketWindowSource(null);
    setMarketWindowSourceCutoff(null);
    chart.setPeriod(chartPeriod(interval));
  }, [chartGeneration, interval]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || chartGeneration === 0) return;
    setBars([]);
    setMarketWindowError(null);
    setMarketWindowSource(null);
    setMarketWindowSourceCutoff(null);
    chart.resetData();
  }, [chartGeneration, timeWindowEndAt]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || chartGeneration === 0) return;
    if (timeWindowMode === "EXECUTION") {
      liveBarSpaceRef.current ??= chart.getBarSpace().bar;
      fitLoadedBarsToChart(chart, bars.length);
      return;
    }
    if (liveBarSpaceRef.current !== null) {
      chart.setBarSpace(liveBarSpaceRef.current);
      liveBarSpaceRef.current = null;
      chart.scrollToRealTime();
    }
  }, [bars.length, chartGeneration, narrow, timeWindowMode]);

  useEffect(() => {
    if (
      streamGeneration <= 0
      || streamGeneration === lastStreamGenerationRef.current
    ) {
      return;
    }
    lastStreamGenerationRef.current = streamGeneration;
    const chart = chartRef.current;
    if (!chart || chartGeneration === 0) return;
    chart.resetData();
  }, [chartGeneration, streamGeneration]);

  useEffect(() => {
    if (!matchingLiveBar) return;
    const subscription = liveSubscriptionRef.current;
    if (!subscription || subscription.interval !== matchingLiveBar.interval) return;
    const nextBar = toKLineData(matchingLiveBar.bar);
    if (!nextBar) return;
    subscription.callback(nextBar);
    if (timeWindowMode === "EXECUTION") {
      setBars((current) => {
        const lastTimestamp = current.at(-1)?.timestamp;
        if (lastTimestamp === nextBar.timestamp) return current;
        if (lastTimestamp !== undefined && lastTimestamp > nextBar.timestamp) {
          return current;
        }
        return [...current, nextBar];
      });
    }
  }, [
    matchingLiveBar,
    timeWindowMode,
  ]);

  useEffect(() => {
    if (!matchingLiveBar) return undefined;
    const staleAt = Math.min(
      Date.parse(matchingLiveBar.received_at),
      Date.parse(matchingLiveBar.source_cutoff),
    ) + MARKET_STREAM_STALE_AFTER_MS;
    const remaining = staleAt - Date.now();
    if (remaining <= 0) return undefined;
    const timer = window.setTimeout(
      () => setFreshnessRevision((current) => current + 1),
      remaining + 25,
    );
    return () => window.clearTimeout(timer);
  }, [matchingLiveBar]);

  useEffect(() => {
    if (pricePlan.kind === "LADDER") return;
    setRangeMode(false);
    setDrawRange(null);
  }, [pricePlan.kind]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || chartGeneration === 0) return;
    chart.removeOverlay({ groupId: SCHEDULE_GROUP });
    const lastTimestamp = bars.at(-1)?.timestamp;
    if (!lastTimestamp) return;
    const visibleSchedulePrices = annotations.priceAnnotations.map(
      (annotation) => annotation.price,
    );
    const analysisAnchorPrices = analysisDrawings.flatMap((drawing) =>
      drawing.points.flatMap((point) =>
        Number.isFinite(point.value) ? [point.value as number] : [],
      ),
    );
    chart.overrideYAxis({
      paneId: "candle_pane",
      createRange: ({ defaultRange }) => {
        const [from, to] = expandedVisiblePriceRange(
          defaultRange.from,
          defaultRange.to,
          [...visibleSchedulePrices, ...analysisAnchorPrices],
          Math.pow(10, -precision) * 4,
          includeAnnotationsInScale,
        );
        if (from === defaultRange.from && to === defaultRange.to) {
          return defaultRange;
        }
        const range = to - from;
        return {
          from,
          to,
          range,
          realFrom: from,
          realTo: to,
          realRange: range,
          displayFrom: from,
          displayTo: to,
          displayRange: range,
        };
      },
    });

    if (executionWindowIntersectsBars) {
      const ongoing = !Number.isFinite(executionWindowEndTimestamp);
      const intervalMs = intervalMilliseconds(interval);
      const dataEndTimestamp = lastTimestamp + intervalMs;
      const effectiveEndTimestamp = ongoing
        ? dataEndTimestamp
        : executionWindowEndTimestamp;
      const startBarTimestamp = containingBarTimestamp(
        bars,
        executionWindowStartTimestamp,
        intervalMs,
      );
      const endBarTimestamp = containingBarTimestamp(
        bars,
        effectiveEndTimestamp,
        intervalMs,
      );
      if (startBarTimestamp !== null && endBarTimestamp !== null) {
        chart.createOverlay({
          name: EXECUTION_WINDOW_OVERLAY,
          id: "halpha-execution-window",
          groupId: SCHEDULE_GROUP,
          lock: true,
          zLevel: 0,
          points: [
            { timestamp: startBarTimestamp, value: bars.at(-1)?.close },
            { timestamp: endBarTimestamp, value: bars.at(-1)?.close },
          ],
          extendData: { ongoing },
          needDefaultPointFigure: false,
          needDefaultXAxisFigure: false,
          needDefaultYAxisFigure: false,
        });
      }
    }

    const commitBoundary = (kind: "LOWER" | "UPPER", event: OverlayEvent<unknown>) => {
      const moved = event.overlay.points[0]?.value;
      const other = finitePrice(
        kind === "LOWER"
          ? latestRangeRef.current.upperPrice
          : latestRangeRef.current.lowerPrice,
      );
      if (!Number.isFinite(moved) || other === null) return;
      const [nextLower, nextUpper] = orderedPriceRange(moved as number, other);
      onRangeChangeRef.current(chartPriceInput(nextLower), chartPriceInput(nextUpper));
      setStatusMessage("区间手柄已更新草稿；等待服务端重新标准化档位。");
    };
    const editableAnnotation = (annotation: OrderChartPriceAnnotation) => ({
      name: "horizontalStraightLine",
      id: `${annotation.id}-handle`,
      groupId: SCHEDULE_GROUP,
      points: [{ timestamp: lastTimestamp, value: annotation.price }],
      mode: "normal" as const,
      lock: narrow,
      needDefaultPointFigure: true,
      needDefaultXAxisFigure: false,
      needDefaultYAxisFigure: false,
      styles: lineStyle(annotationColor(annotation, direction), annotation.lineStyle),
      onPressedMoveStart: () => {
        if (annotation.role === "SINGLE_LIMIT") {
          undoSingleLimitRef.current = chartPriceInput(annotation.price);
          undoRangeRef.current = null;
        } else {
          undoRangeRef.current = { ...latestRangeRef.current };
          undoSingleLimitRef.current = null;
        }
        setStatusMessage(`${annotation.label}正在移动；松开后才写入草稿。`);
      },
      onPressedMoveEnd: (event: OverlayEvent<unknown>) => {
        if (annotation.role === "SINGLE_LIMIT") {
          const moved = event.overlay.points[0]?.value;
          if (!Number.isFinite(moved)) return;
          onSingleLimitPriceChangeRef.current(chartPriceInput(moved as number));
          setStatusMessage("单笔限价已更新草稿；等待服务端重新标准化。");
          return;
        }
        commitBoundary(
          annotation.role === "RANGE_LOWER" ? "LOWER" : "UPPER",
          event,
        );
      },
      onRightClick: (event: OverlayEvent<unknown>) => event.preventDefault?.(),
    });

    annotations.priceAnnotations.forEach((annotation) => {
      if (annotation.draggable) {
        chart.createOverlay(editableAnnotation(annotation));
      } else {
        chart.createOverlay({
          name: "horizontalStraightLine",
          id: `${annotation.id}-line`,
          groupId: SCHEDULE_GROUP,
          lock: true,
          zLevel: 1,
          points: [{ timestamp: lastTimestamp, value: annotation.price }],
          needDefaultPointFigure: false,
          needDefaultXAxisFigure: false,
          needDefaultYAxisFigure: false,
          styles: lineStyle(
            annotationColor(annotation, direction),
            annotation.lineStyle,
          ),
        });
      }
    });

    const visibleBarPrices = bars.flatMap((bar) => [bar.high, bar.low]);
    const visiblePriceSpan = visibleBarPrices.length > 0
      ? Math.max(...visibleBarPrices, ...visibleSchedulePrices)
        - Math.min(...visibleBarPrices, ...visibleSchedulePrices)
      : 0;
    const tagTolerance = Math.max(
      Math.pow(10, -precision),
      visiblePriceSpan / Math.max(chartContainerRef.current?.clientHeight ?? 320, 1) * 18,
    );
    const priceTags = groupNearbyPriceAnnotations(
      annotations.priceAnnotations,
      tagTolerance,
    ).flatMap((group, index) => {
      const primary = selectPriceAnnotationForTag(group);
      return primary ? [{
        id: `halpha-price-label-${index}`,
        group,
        primary,
      }] : [];
    });
    const priceTagAnchors = priceTags.map(({ id, primary }) => ({
      id,
      price: primary.price,
    }));
    priceTags.forEach(({ id, group, primary }) => {
      chart.createOverlay({
        name: PRICE_TAG_OVERLAY,
        id,
        groupId: SCHEDULE_GROUP,
        lock: true,
        zLevel: primary.draggable ? 3 : 2,
        points: [{ timestamp: lastTimestamp, value: primary.price }],
        extendData: {
          id,
          label: annotationGroupTag(
            group,
            compactTagLabels,
            precision,
            priceTickSize,
          ),
          anchors: priceTagAnchors,
        },
        needDefaultPointFigure: false,
        needDefaultXAxisFigure: false,
        needDefaultYAxisFigure: false,
        styles: lineStyle(
          annotationColor(primary, direction),
          primary.lineStyle,
        ),
      });
    });

    [...operationMarkerGroups.entries()].forEach(([barTimestamp, group], index) => {
      const label = compactRuntimeMarkerGroupLabel(
        group.map((item) => item.marker),
      );
      const primary = group.find((item) => item.marker.price !== null) ?? group[0];
      if (!primary) return;
      chart.createOverlay({
        name: "simpleAnnotation",
        id: `halpha-operation-marker-${index}`,
        groupId: SCHEDULE_GROUP,
        lock: true,
        zLevel: 5,
        points: [{
          timestamp: barTimestamp,
          value: primary.displayPrice,
        }],
        extendData: label,
        needDefaultPointFigure: true,
        needDefaultXAxisFigure: false,
        needDefaultYAxisFigure: false,
        styles: lineStyle(
          operationMarkerColor(primary.marker.category),
          "dotted",
        ),
      });
    });
  }, [
    analysisDrawings,
    annotations.priceAnnotations,
    bars,
    chartGeneration,
    compactTagLabels,
    direction,
    includeAnnotationsInScale,
    executionWindowEndTimestamp,
    executionWindowIntersectsBars,
    executionWindowStartTimestamp,
    interval,
    narrow,
    precision,
    priceTickSize,
    operationMarkerGroups,
  ]);

  const rememberCurrentRange = () => {
    const lower = finitePrice(latestRangeRef.current.lowerPrice);
    const upper = finitePrice(latestRangeRef.current.upperPrice);
    if (lower !== null && upper !== null) {
      undoRangeRef.current = { ...latestRangeRef.current };
    }
  };

  const startRangeMode = () => {
    if (narrow || pricePlan.kind !== "LADDER" || !chartRef.current) return;
    if (activeAnalysisOverlayRef.current) {
      chartRef.current.removeOverlay({ id: activeAnalysisOverlayRef.current });
      activeAnalysisOverlayRef.current = null;
    }
    rememberCurrentRange();
    setActiveTool(null);
    setRangeMode(true);
    setStatusMessage("在 K 线主图内按下并纵向拖动；松开后只更新区间草稿。按 Esc 取消。");
  };

  const startAnalysis = (tool: "SUPPORT" | "TREND") => {
    const chart = chartRef.current;
    if (narrow || !chart) return;
    if (activeAnalysisOverlayRef.current) {
      chart.removeOverlay({ id: activeAnalysisOverlayRef.current });
    }
    setRangeMode(false);
    setDrawRange(null);
    setActiveTool(tool);
    const result = chart.createOverlay(analysisOverlay(
      tool === "SUPPORT" ? "horizontalStraightLine" : "straightLine",
    ));
    activeAnalysisOverlayRef.current = typeof result === "string" ? result : null;
    setStatusMessage(
      tool === "SUPPORT"
        ? "在图上点击一次放置支撑/阻力线；之后可以拖动，右键可删除。"
        : "在图上依次点击两个锚点，再点击一次确认趋势线；之后可以拖动，右键可删除。",
    );
  };

  const cancelActiveInteraction = () => {
    const chart = chartRef.current;
    if (rangeMode || drawRange) {
      setRangeMode(false);
      setDrawRange(null);
      setStatusMessage("已取消本次区间绘制，计划草稿没有变化。");
      return true;
    }
    if (chart && activeAnalysisOverlayRef.current) {
      chart.removeOverlay({ id: activeAnalysisOverlayRef.current });
      activeAnalysisOverlayRef.current = null;
      setActiveTool(null);
      setStatusMessage("已取消未完成的分析绘图。");
      return true;
    }
    if (undoSingleLimitRef.current !== null) {
      const previous = undoSingleLimitRef.current;
      undoSingleLimitRef.current = null;
      onSingleLimitPriceChangeRef.current(previous);
      setStatusMessage("已撤销最近一次图上单笔限价修改。");
      return true;
    }
    if (undoRangeRef.current) {
      const previous = undoRangeRef.current;
      undoRangeRef.current = null;
      onRangeChangeRef.current(previous.lowerPrice, previous.upperPrice);
      setStatusMessage("已撤销最近一次图上区间修改。");
      return true;
    }
    return false;
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape" && cancelActiveInteraction()) {
      event.preventDefault();
      event.stopPropagation();
    }
  };

  const pointerPrice = (event: PointerEvent<HTMLDivElement>): number | null => {
    const chart = chartRef.current;
    const container = chartContainerRef.current;
    if (!chart || !container) return null;
    const bounds = container.getBoundingClientRect();
    const point = chartPointAt(
      chart,
      event.clientX - bounds.left,
      event.clientY - bounds.top,
    );
    return point?.value ?? null;
  };

  const handlePointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (!rangeMode || event.button !== 0) return;
    const price = pointerPrice(event);
    if (price === null) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    setDrawRange({
      startY: event.nativeEvent.offsetY,
      currentY: event.nativeEvent.offsetY,
      startPrice: price,
      currentPrice: price,
    });
  };

  const handlePointerMove = (event: PointerEvent<HTMLDivElement>) => {
    if (!drawRange || !event.currentTarget.hasPointerCapture(event.pointerId)) return;
    const price = pointerPrice(event);
    if (price === null) return;
    setDrawRange((current) => current ? {
      ...current,
      currentY: event.nativeEvent.offsetY,
      currentPrice: price,
    } : null);
  };

  const handlePointerUp = (event: PointerEvent<HTMLDivElement>) => {
    if (!drawRange) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    const distance = Math.abs(drawRange.currentY - drawRange.startY);
    if (distance < 8) {
      setDrawRange(null);
      setStatusMessage("拖动距离过小，区间草稿没有变化。请按住并纵向拖动。");
      return;
    }
    const [nextLower, nextUpper] = orderedPriceRange(
      drawRange.startPrice,
      drawRange.currentPrice,
    );
    onRangeChangeRef.current(chartPriceInput(nextLower), chartPriceInput(nextUpper));
    setDrawRange(null);
    setRangeMode(false);
    setStatusMessage("图上区间已写入草稿；档位线将在服务端标准化成功后显示。");
  };

  const clearAnalysis = () => {
    chartRef.current?.removeOverlay({ groupId: ANALYSIS_GROUP });
    activeAnalysisOverlayRef.current = null;
    setActiveTool(null);
    setAnalysisDrawings([]);
    setStatusMessage("已清除本页分析绘图；订单计划草稿没有变化。");
  };

  const reloadMarketWindow = () => {
    const chart = chartRef.current;
    if (!chart) return;
    setMarketWindowError(null);
    chart.resetData();
  };

  const hasFixedEntryPrices = pricePlan.kind === "LADDER"
    || (
      spec.venue_policy.order_type === "LIMIT"
      && spec.venue_policy.price_match === null
      && pricePlan.limit_price !== null
    );
  const previewStatus = previewState === "READY"
    ? displayMode === "RUNTIME"
      ? `${previewLegs.length} 条已归一化计划档位`
      : hasFixedEntryPrices
        ? `${previewLegs.length} 条服务端归一化草稿线`
        : `${previewLegs.length} 个服务端归一化草稿档位`
    : previewState === "PENDING"
      ? displayMode === "RUNTIME"
        ? "计划档位读取中"
        : "档位预览更新中，旧草稿线已隐藏"
      : displayMode === "RUNTIME"
        ? "计划档位不可用"
        : "档位预览不可用，执行草稿线未显示";
  const displayDraftPrice = (value: string) => tradingPrice(value, priceTickSize);
  const orderInstructionStatus = pricePlan.kind === "LADDER"
    ? `区间 ${pricePlan.lower_price ? displayDraftPrice(pricePlan.lower_price) : "未填写"} – ${pricePlan.upper_price ? displayDraftPrice(pricePlan.upper_price) : "未填写"}`
    : spec.venue_policy.order_type === "MARKET"
      ? "市价 · 场所决定"
      : spec.venue_policy.price_match !== null
        ? `priceMatch · ${spec.venue_policy.price_match}`
        : `限价 ${pricePlan.limit_price ? displayDraftPrice(pricePlan.limit_price) : "未填写"}`;
  const entryProgramStatus = spec.entry_program?.kind === "TIME_SLICED"
    ? `时间分批 · ${spec.entry_program.slice_count} 笔`
    : spec.entry_program?.kind === "EVENT_TRIGGERED"
      ? "事件触发入场"
      : spec.entry_program?.kind === "PRICE_LADDER"
        ? "价格区间分批"
        : pricePlan.kind === "SINGLE" ? "单笔" : "";
  const pricePlanStatus = [
    entryProgramStatus,
    orderInstructionStatus,
  ].filter(Boolean).join(" · ");
  const runtimeEntryStatus = [
    entryProgramStatus || "入场",
    previewState === "READY" ? `${previewLegs.length} 档` : previewStatus,
    spec.venue_policy.order_type === "MARKET"
      ? "市价"
      : spec.venue_policy.price_match !== null
        ? "PriceMatch"
        : spec.venue_policy.post_only
          ? "Maker only 限价"
          : "限价",
  ].filter(Boolean).join(" · ");
  const runtimeProtectionStatus = compactRuntimeRelativeRules(
    annotations.relativeRules,
  );
  const setPriceAnnotationVisibility = useCallback((
    annotation: OrderChartPriceAnnotation,
    visible: boolean,
  ) => {
    if (
      annotations.additionalIds.has(annotation.id)
      && onAdditionalPriceAnnotationVisibilityChange
    ) {
      onAdditionalPriceAnnotationVisibilityChange(annotation.id, visible);
      return;
    }
    setHiddenLocalPriceAnnotationIds((current) => {
      const next = new Set(current);
      if (visible) next.delete(annotation.id);
      else next.add(annotation.id);
      return next;
    });
  }, [annotations.additionalIds, onAdditionalPriceAnnotationVisibilityChange]);
  const chartSourceCutoff = matchingLiveBar?.source_cutoff
    ?? marketWindowSourceCutoff;
  const compactChartSourceCutoff = compactMarketCutoff(chartSourceCutoff);
  const appliedLiveBarFresh = isUsableMarketStreamBar(
    matchingLiveBar,
    expectedMarketSource,
    Date.now(),
  );
  // The revision is advanced by a one-shot expiry timer so the status becomes
  // stale even when the execution quote stream also stops rendering updates.
  void freshnessRevision;
  const chartStreamStatus: MarketStreamClientStatus = appliedLiveBarFresh
    ? "LIVE"
    : streamStatus === "CONNECTING"
      || streamStatus === "RECONNECTING"
      || streamStatus === "FAILED"
      || streamStatus === "DISABLED"
      ? streamStatus
      : "STALE";
  const marketHistoryReady = Boolean(
    expectedMarketSource
    && marketWindowSource === expectedMarketSource
    && bars.length > 0
    && !marketWindowLoading
    && marketWindowError === null,
  );
  const marketDataReady = Boolean(
    marketHistoryReady
    && streamStatus === "LIVE"
    && appliedLiveBarFresh,
  );
  const chartSurfaceBlocked = strategyInput
    ? !marketHistoryReady
    : shouldBlockChartSurface(
      displayMode,
      marketHistoryReady,
      marketDataReady,
    );
  const draftBlockSuffix = !strategyInput && displayMode === "DRAFT"
    ? "；价格预览与保存已阻断。"
    : "。";
  const chartEmptyMessage = marketWindowLoading
    ? `${timeWindowMode === "EXECUTION"
      ? executionWindowFullyVisible
        ? `正在读取完整执行区间的 ${interval} K 线`
        : `正在读取执行区间末段的 ${interval} K 线`
      : `正在读取最近 ${ORDER_CHART_WINDOW_BAR_COUNT} 根 ${interval} K 线`}${draftBlockSuffix}`
    : marketWindowError
      ? marketFailureText(marketWindowError, displayMode, strategyInput)
      : bars.length === 0
        ? strategyInput
          ? `当前时间窗没有可展示的 ${interval} K 线；策略数值仍以右侧标明来源和截止时间的行情事实为准。`
          : displayMode === "RUNTIME"
          ? `当前时间窗没有可展示的 ${interval} K 线；运行事实仍以服务端记录为准。`
          : `当前时间窗没有可展示的 ${interval} K 线；价格预览与保存已阻断。`
        : strategyInput
          ? `${streamStatusLabel(chartStreamStatus, matchingLiveBar !== null)}；历史 K 线暂不可用，右侧策略数值不会被当作实时图表。`
          : displayMode === "RUNTIME"
          ? `${streamStatusLabel(chartStreamStatus, matchingLiveBar !== null)}；K 线暂不可用，运行事实仍以服务端记录为准。`
          : `${streamStatusLabel(chartStreamStatus, matchingLiveBar !== null)}；价格预览与保存已阻断，恢复后将重新核对历史与实时来源。`;

  useEffect(() => {
    onMarketReadinessChange?.(marketDataReady);
  }, [marketDataReady, onMarketReadinessChange]);

  useEffect(
    () => () => onMarketReadinessChange?.(false),
    [onMarketReadinessChange],
  );

  return (
    <Box
      component="section"
      aria-labelledby="order-schedule-chart-title"
      onKeyDown={handleKeyDown}
      sx={{
        border: 1,
        borderColor: "divider",
        borderRadius: 1,
        overflow: { xs: "visible", md: "hidden" },
        bgcolor: "background.paper",
        height: workspaceMode ? "100%" : "auto",
        minHeight: 0,
        display: "flex",
        flexDirection: "column",
      }}
    >
      <Box sx={{ px: { xs: 1.25, sm: 1.5 }, py: 1, borderBottom: 1, borderColor: "divider", flex: "0 0 auto" }}>
        <Stack
          direction={{ xs: "column", lg: "row" }}
          spacing={1}
          sx={{ alignItems: { xs: "stretch", lg: "center" } }}
        >
          <Box sx={{ minWidth: 0, flex: "1 1 auto" }}>
            <Stack
              direction="row"
              spacing={.5}
              useFlexGap
              sx={{ alignItems: "center", flexWrap: "wrap" }}
            >
              <Typography id="order-schedule-chart-title" component="h2" variant="subtitle2">
                {interval} K 线 · {strategyInput
                  ? "策略输入"
                  : displayMode === "RUNTIME"
                  ? timeWindowMode === "EXECUTION"
                    ? executionWindowFullyVisible
                      ? "完整执行区间"
                      : "执行区间片段"
                    : runtimePhase === "REVIEW"
                      ? "结束后行情"
                      : "计划运行"
                  : "草稿投影"}
              </Typography>
              <Tooltip
                arrow
                title={strategyInput
                  ? "图中只读显示当前行情、策略通道和追价边界；策略是否触发仍以闭合 K 线、确认根数与执行前检查为准。"
                  : displayMode === "RUNTIME"
                  ? "计划线、动作线与交易所事实分层展示；图中线条不会修改运行中计划，当前行情也不替代服务端执行事实。"
                  : "图中价格线与右侧字段使用同一份计划输入；分析线不会写入执行条件。"}
              >
                <IconButton size="small" aria-label={strategyInput ? "了解策略输入图表" : displayMode === "RUNTIME" ? "了解运行图表的事实边界" : "了解图表草稿和订单事实的区别"}>
                  <InfoOutlined sx={{ fontSize: 16 }} />
                </IconButton>
              </Tooltip>
              <Chip
                size="small"
                color={timeWindowMode === "EXECUTION"
                  ? "info"
                  : streamStatusColor(chartStreamStatus)}
                variant="outlined"
                label={timeWindowMode === "EXECUTION"
                  ? executionWindowFullyVisible
                    ? "执行区间"
                    : "区间片段"
                  : streamStatusLabel(
                    chartStreamStatus,
                    matchingLiveBar !== null,
                  )}
                sx={{
                  ml: .5,
                  height: 22,
                  ...(timeWindowMode !== "EXECUTION" && chartStreamStatus === "LIVE" ? {
                    color: "#166534",
                    borderColor: "#86B69F",
                    bgcolor: "#F0FDF4",
                  } : {}),
                }}
              />
              {marketWindowSource ? (
                <Tooltip
                  arrow
                  title={`${chartMarketSourceDescription(marketWindowSource)} 来源截止 ${chartSourceCutoff ?? "未知"}。`}
                >
                  <Chip
                    size="small"
                    variant="outlined"
                    data-testid="order-schedule-chart-market-source"
                    label={chartMarketSourceLabel(marketWindowSource)}
                    sx={{ height: 22, bgcolor: "#EFF6FF", borderColor: "#93C5FD", color: "#1D4ED8" }}
                  />
                </Tooltip>
              ) : null}
            </Stack>
            <Typography
              variant="caption"
              color="text.secondary"
              data-testid="order-schedule-chart-subtitle"
              sx={{ display: "block", overflowWrap: "anywhere" }}
            >
              {instrumentRef}
              {compactChartSourceCutoff ? ` · K线截止 ${compactChartSourceCutoff}` : ""}
              {displayMode === "RUNTIME"
                ? timeWindowMode === "EXECUTION"
                  ? executionWindowFullyVisible
                    ? " · 已将计划起点至终点放入一屏"
                    : " · 当前周期仅显示执行末段；切换到更大周期可查看完整区间"
                  : runtimePhase === "REVIEW"
                    ? " · 继续显示计划结束后的实时走势"
                    : " · 只读展示计划、持仓与动作价格"
                : strategyInput
                  ? " · 只读展示当前策略输入与关键价格"
                  : " · 输入线可拖动 · Esc 撤销最近一次图上修改"}
            </Typography>
            <Stack
              direction="row"
              spacing={1}
              useFlexGap
              sx={{ alignItems: "center", flexWrap: "wrap", mt: .75 }}
            >
              {narrow ? (
                <TextField
                  select
                  size="small"
                  label="K 线周期"
                  value={interval}
                  onChange={(event) => onIntervalChange(event.target.value as MarketInterval)}
                  sx={{ width: 112 }}
                >
                  {ORDER_CHART_INTERVALS.map((option) => (
                    <MenuItem key={option.value} value={option.value}>{option.label}</MenuItem>
                  ))}
                </TextField>
              ) : (
                <ToggleButtonGroup
                  exclusive
                  size="small"
                  value={interval}
                  aria-label="K 线周期"
                  onChange={(_event, next: MarketInterval | null) => {
                    if (next) onIntervalChange(next);
                  }}
                  sx={{
                    "& .MuiToggleButton-root": {
                      minWidth: 42,
                      minHeight: 28,
                      px: .8,
                      py: .25,
                      textTransform: "none",
                      fontWeight: 750,
                    },
                  }}
                >
                  {ORDER_CHART_INTERVALS.map((option) => (
                    <ToggleButton key={option.value} value={option.value}>
                      {option.label}
                    </ToggleButton>
                  ))}
                </ToggleButtonGroup>
              )}
              <Tooltip
                arrow
                title="默认只按当前 K 线窗口的最高价与最低价缩放，远端价格线可能暂时在图外。勾选后，全部固定价格标注与分析线锚点都会纳入纵轴范围。"
              >
                <FormControlLabel
                  sx={{ m: 0 }}
                  control={(
                    <Checkbox
                      size="small"
                      checked={includeAnnotationsInScale}
                      onChange={(event) => {
                        const checked = event.target.checked;
                        setIncludeAnnotationsInScale(checked);
                        setStatusMessage(
                          checked
                            ? "已将全部价格标注纳入缩放；远端价格线可能压缩 K 线波动。"
                            : "已恢复仅按当前 K 线窗口缩放；图外价格线仍保留在计划中。",
                        );
                      }}
                      slotProps={{
                        input: { "aria-label": "全部价格标注纳入缩放" },
                      }}
                    />
                  )}
                  label={(
                    <Typography variant="caption" sx={{ fontWeight: 700 }}>
                      全部标注纳入缩放
                    </Typography>
                  )}
                />
              </Tooltip>
              {displayMode === "RUNTIME" && onToggleTimeWindow ? (
                <Tooltip
                  arrow
                  title={timeWindowMode === "EXECUTION"
                    ? "返回当前实时行情；K 线会继续更新，历史操作点仍保留在事件记录中。"
                    : "自动选择能容纳计划起点至终点的最小 K 线周期，并把整个执行过程放入一屏。"}
                >
                  <Button
                    size="small"
                    variant={timeWindowMode === "EXECUTION" ? "contained" : "outlined"}
                    data-testid="order-schedule-execution-window-toggle"
                    onClick={onToggleTimeWindow}
                  >
                    {timeWindowMode === "EXECUTION" ? "回到实时" : "查看执行区间"}
                  </Button>
                </Tooltip>
              ) : null}
            </Stack>
          </Box>
          {displayMode === "DRAFT" && !strategyInput && <Stack
            data-testid="order-schedule-chart-tools"
            direction="row"
            spacing={0.75}
            useFlexGap
            sx={{ flexWrap: "wrap", ml: { lg: "auto" }, justifyContent: { lg: "flex-end" } }}
          >
            <Button
              size="small"
              variant={rangeMode ? "contained" : "outlined"}
              aria-pressed={rangeMode}
              disabled={narrow || !marketDataReady || pricePlan.kind !== "LADDER"}
              onClick={() => rangeMode ? cancelActiveInteraction() : startRangeMode()}
            >
              拖动选择区间
            </Button>
            <Button
              size="small"
              variant={activeTool === "SUPPORT" ? "contained" : "outlined"}
              aria-pressed={activeTool === "SUPPORT"}
              disabled={narrow || !marketDataReady}
              onClick={() => startAnalysis("SUPPORT")}
            >
              支撑 / 阻力
            </Button>
            <Button
              size="small"
              variant={activeTool === "TREND" ? "contained" : "outlined"}
              aria-pressed={activeTool === "TREND"}
              disabled={narrow || !marketDataReady}
              onClick={() => startAnalysis("TREND")}
            >
              趋势线
            </Button>
            <Button
              size="small"
              color="inherit"
              disabled={analysisDrawings.length === 0 && !activeAnalysisOverlayRef.current}
              onClick={clearAnalysis}
            >
              清除分析线
            </Button>
          </Stack>}
        </Stack>
        {displayMode === "DRAFT" && !strategyInput && narrow ? (
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
            窄屏保留查看与精确数值输入；绘图和多档拖动仅在桌面开放。
          </Typography>
        ) : null}
      </Box>

      <Box
        tabIndex={0}
        role="group"
        onPointerLeave={() => setHoveredOperationBarTimestamp(null)}
        aria-label={strategyInput
          ? `策略输入 ${interval} K 线主图；只读展示当前价、通道与追价边界`
          : displayMode === "RUNTIME"
          ? `订单计划 ${interval} K 线运行主图；只读展示计划、持仓、保护与执行动作价格`
          : `订单计划 ${interval} K 线主图；可在桌面编辑单笔限价、区间、支撑阻力和趋势线`}
        sx={{
          position: "relative",
          height: workspaceMode ? { xs: 340, md: "auto" } : { xs: 330, sm: 430 },
          flex: workspaceMode ? { xs: "0 0 auto", md: "1 1 0" } : "0 0 auto",
          minHeight: workspaceMode ? { xs: 340, md: 320 } : undefined,
          outline: "none",
          bgcolor: "action.hover",
          "&:focus-visible": {
            boxShadow: `inset 0 0 0 3px ${theme.palette.primary.main}`,
          },
        }}
      >
        <Box
          ref={chartContainerRef}
          data-testid="order-schedule-kline-chart"
          data-market-environment={environmentScope}
          data-market-history-source={marketWindowSource ?? undefined}
          data-market-live-source={marketDataReady ? matchingLiveBar?.source : undefined}
          data-annotations-in-scale={includeAnnotationsInScale ? "true" : "false"}
          data-execution-window-view={!hasExecutionWindow
            ? "NONE"
            : timeWindowMode === "EXECUTION"
              ? executionWindowFullyVisible
                ? "FULL"
                : "PARTIAL"
              : "LIVE"}
          data-execution-window-visible={executionWindowIntersectsBars
            ? "true"
            : "false"}
          sx={{ position: "absolute", inset: 0 }}
        />
        {hoveredOperationGroup ? (
          <Box
            role="region"
            aria-label="本根 K 线事件详情"
            tabIndex={0}
            data-testid="order-schedule-event-tooltip"
            data-event-bar={hoveredOperationGroup[0]?.barTimestamp}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                setHoveredOperationBarTimestamp(null);
              }
            }}
            sx={{
              position: "absolute",
              zIndex: 6,
              top: 8,
              right: narrow ? 8 : 144,
              width: narrow
                ? "calc(100% - 16px)"
                : "min(360px, calc(100% - 160px))",
              maxHeight: "calc(100% - 16px)",
              overflow: "hidden",
              display: "flex",
              flexDirection: "column",
              pointerEvents: "auto",
              touchAction: "pan-y",
              border: 1,
              borderColor: "divider",
              borderRadius: 1.5,
              bgcolor: "background.paper",
              boxShadow: 4,
            }}
          >
            <Stack
              direction="row"
              spacing={1}
              sx={{
                alignItems: "center",
                justifyContent: "space-between",
                px: 1.25,
                py: 1,
                flex: "0 0 auto",
                borderBottom: 1,
                borderColor: "divider",
              }}
            >
              <Stack direction="row" spacing={.75} sx={{ alignItems: "center", minWidth: 0 }}>
                <Typography variant="caption" sx={{ fontWeight: 800 }}>
                  本根 K 线事件
                </Typography>
                <Chip size="small" label={`${hoveredOperationGroup.length} 项`} sx={{ height: 20 }} />
              </Stack>
              <IconButton
                size="small"
                aria-label="关闭本根 K 线事件"
                onClick={() => setHoveredOperationBarTimestamp(null)}
                sx={{ flex: "0 0 auto", p: .25 }}
              >
                <Close fontSize="small" />
              </IconButton>
            </Stack>
            <Stack
              role="list"
              spacing={.75}
              data-testid="order-schedule-event-list"
              sx={{
                minHeight: 0,
                overflowY: "auto",
                overscrollBehavior: "contain",
                px: 1.25,
                pb: 1.25,
              }}
            >
              {visibleHoveredOperationEvents.map((item) => (
                <Box
                  role="listitem"
                  key={item.marker.id}
                  sx={{
                    pt: .75,
                    borderTop: 1,
                    borderColor: "divider",
                    minWidth: 0,
                  }}
                >
                  <Typography variant="caption" sx={{ display: "block", fontWeight: 750 }}>
                    {item.marker.label}
                  </Typography>
                  <Typography variant="caption" color="text.secondary" className="mono" sx={{ display: "block" }}>
                    {EVENT_TIME_FORMATTER.format(new Date(item.marker.at))}
                    {" · "}
                    {operationMarkerPriceLabel(item)}{" "}
                    {chartPriceInput(item.displayPrice, priceTickSize)} USDT
                  </Typography>
                  {item.marker.detail ? (
                    <Typography
                      variant="caption"
                      color="text.secondary"
                      sx={{ display: "block", overflowWrap: "anywhere" }}
                    >
                      {item.marker.detail}
                    </Typography>
                  ) : null}
                </Box>
              ))}
              {hiddenHoveredOperationEventCount > 0 ? (
                <Button
                  size="small"
                  variant="text"
                  data-testid="order-schedule-event-load-more"
                  onClick={() => setVisibleOperationEventCount((current) =>
                    Math.min(
                      hoveredOperationGroup.length,
                      current + eventPanelPageSize,
                    ))}
                  sx={{ alignSelf: "stretch", flex: "0 0 auto" }}
                >
                  加载更多（剩余 {hiddenHoveredOperationEventCount} 项）
                </Button>
              ) : null}
            </Stack>
          </Box>
        ) : null}
        {!chartSurfaceBlocked
          && !strategyInput
          && displayMode === "DRAFT"
          && !marketDataReady ? (
          <Box
            role="status"
            sx={{
              position: "absolute",
              top: 8,
              left: 8,
              zIndex: 3,
              px: 1,
              py: .5,
              border: 1,
              borderColor: "warning.main",
              borderRadius: 1,
              bgcolor: "background.paper",
              boxShadow: 1,
              pointerEvents: "none",
            }}
          >
            <Typography variant="caption" color="warning.dark" sx={{ fontWeight: 700 }}>
              实时行情未就绪；价格预览与保存已阻断。
            </Typography>
          </Box>
        ) : null}
        {chartSurfaceBlocked ? (
          <Stack
            role="status"
            spacing={1.25}
            sx={{
              position: "absolute",
              inset: 0,
              zIndex: 2,
              alignItems: "center",
              justifyContent: "center",
              px: 3,
              textAlign: "center",
              bgcolor: "background.paper",
            }}
          >
            {marketWindowLoading ? (
              <Box sx={{ width: "min(360px, 100%)" }}>
                <LinearProgress aria-label="正在读取订单计划 K 线窗口" />
              </Box>
            ) : null}
            <Typography variant="body2" color="text.secondary">
              {chartEmptyMessage}
            </Typography>
            {marketWindowError ? (
              <Button size="small" variant="outlined" onClick={reloadMarketWindow}>
                重试 K 线
              </Button>
            ) : null}
          </Stack>
        ) : null}
        {displayMode === "DRAFT"
          && !strategyInput
          && marketDataReady
          && rangeMode ? (
          <Box
            data-testid="order-schedule-range-drag-layer"
            aria-hidden="true"
            onPointerDown={handlePointerDown}
            onPointerMove={handlePointerMove}
            onPointerUp={handlePointerUp}
            onPointerCancel={() => setDrawRange(null)}
            sx={{
              position: "absolute",
              inset: 0,
              zIndex: 4,
              cursor: "ns-resize",
              touchAction: "none",
              bgcolor: "rgba(37, 99, 235, 0.025)",
            }}
          >
            {drawRange ? (
              <Box
                sx={{
                  position: "absolute",
                  left: 0,
                  right: 0,
                  top: Math.min(drawRange.startY, drawRange.currentY),
                  height: Math.max(2, Math.abs(drawRange.currentY - drawRange.startY)),
                  bgcolor: "rgba(37, 99, 235, 0.12)",
                  borderTop: "2px solid #2563EB",
                  borderBottom: "2px solid #7C3AED",
                  pointerEvents: "none",
                }}
              />
            ) : null}
          </Box>
        ) : null}
      </Box>

      <Box
        data-testid="order-schedule-chart-detail-scroll"
        sx={{
          px: { xs: 1.25, sm: 1.5 },
          py: 1,
          borderTop: 1,
          borderColor: "divider",
          flex: { xs: "0 0 auto", md: "0 1 auto" },
          minHeight: 0,
          maxHeight: workspaceMode ? { md: "42%" } : undefined,
          overflowY: workspaceMode ? { xs: "visible", md: "auto" } : "visible",
          overscrollBehavior: "contain",
        }}
      >
        {displayMode === "RUNTIME" && !strategyInput ? (
          <Box
            aria-label="计划图摘要"
            sx={{
              display: "grid",
              gridTemplateColumns: { xs: "1fr", sm: "repeat(3, minmax(0, auto))" },
              columnGap: 1.5,
              rowGap: .4,
              alignItems: "center",
              mb: .5,
            }}
          >
            {executionWindowIntersectsBars ? (
              <Stack direction="row" spacing={.6} sx={{ alignItems: "center", minWidth: 0 }}>
                <Box
                  aria-hidden="true"
                  sx={{
                    flex: "0 0 auto",
                    width: 12,
                    height: 12,
                    border: `1px solid ${EXECUTION_WINDOW_BORDER}`,
                    bgcolor: EXECUTION_WINDOW_FILL,
                  }}
                />
                <Typography variant="caption" sx={{ fontWeight: 700 }}>
                  {runtimePhase === "RUNNING" ? "执行区间进行中" : "计划执行区间"}
                </Typography>
              </Stack>
            ) : <Box />}
            <Typography variant="caption" sx={{ minWidth: 0 }}>
              <Box component="span" color="text.secondary">入场　</Box>
              <Box component="span" sx={{ fontWeight: 700 }}>{runtimeEntryStatus}</Box>
            </Typography>
            <Typography variant="caption" sx={{ minWidth: 0 }}>
              <Box component="span" color="text.secondary">保护/退出　</Box>
              <Box component="span" sx={{ fontWeight: 700 }}>{runtimeProtectionStatus}</Box>
            </Typography>
          </Box>
        ) : (
          <Stack direction="row" spacing={0.75} useFlexGap sx={{ flexWrap: "wrap", mb: .5 }}>
            {strategyInput ? (
              <Chip size="small" variant="outlined" label="当前策略关键价格" />
            ) : (
              <>
                <Chip size="small" variant="outlined" label={pricePlanStatus} />
                <Chip size="small" variant="outlined" label={previewStatus} />
                <Chip
                  size="small"
                  variant="outlined"
                  label={summarizeRelativeRules(annotations.relativeRules)}
                />
              </>
            )}
          </Stack>
        )}
        {strategyInput || drawRange || statusMessage ? (
          <Typography aria-live="polite" variant="caption" color="text.secondary">
            {strategyInput
              ? statusMessage
              : drawRange
              ? `选择中：${chartPriceInput(Math.min(drawRange.startPrice, drawRange.currentPrice), priceTickSize)} – ${chartPriceInput(Math.max(drawRange.startPrice, drawRange.currentPrice), priceTickSize)} USDT`
              : statusMessage}
          </Typography>
        ) : null}

        <Box
          component="details"
          sx={{
            mt: .65,
            "& > summary": {
              cursor: "pointer",
              color: "text.secondary",
              fontSize: 12,
              fontWeight: 700,
            },
          }}
        >
          <Box component="summary">
            {strategyInput ? "策略关键价格" : "图线与操作点"} · 价格线 {annotations.priceAnnotations.length}/{annotations.allPriceAnnotations.length}
          </Box>
        <Box sx={{ mt: 1 }}>
          <Typography variant="caption" sx={{ fontWeight: 750 }}>
            价格线显示
          </Typography>
          {annotations.allPriceAnnotations.length > 0 ? (
            <Box
              component="div"
              aria-label="图中价格标注及等价数值"
              tabIndex={0}
              sx={{
                mt: 0.75,
                mb: 0,
                display: "grid",
                gridTemplateColumns: { xs: "1fr", sm: "repeat(2, minmax(0, 1fr))" },
                gap: 0.75,
                maxHeight: { lg: 180 },
                overflowY: { lg: "auto" },
              }}
            >
              {annotations.allPriceAnnotations.map((annotation) => {
                const visible = annotations.visiblePriceAnnotationIds.has(annotation.id);
                const abbreviation = annotationTagAbbreviation(annotation);
                const detail = displayMode === "RUNTIME" && annotation.role === "REFERENCE"
                  ? ""
                  : annotation.detail;
                return (
                  <Box
                    key={annotation.id}
                    sx={{
                      minWidth: 0,
                      display: "grid",
                      gridTemplateColumns: "auto auto minmax(0, 1fr)",
                      columnGap: .5,
                      alignItems: "start",
                      opacity: visible ? 1 : .6,
                    }}
                  >
                    <Checkbox
                      size="small"
                      checked={visible}
                      onChange={(event) => setPriceAnnotationVisibility(
                        annotation,
                        event.target.checked,
                      )}
                      slotProps={{
                        input: {
                          "aria-label": `显示图线 ${abbreviation} ${annotation.label}`,
                        },
                      }}
                      sx={{ p: .25, mt: -.25 }}
                    />
                    <Box
                      aria-hidden="true"
                      sx={{
                        minWidth: 24,
                        height: 20,
                        px: .45,
                        display: "inline-flex",
                        alignItems: "center",
                        justifyContent: "center",
                        borderRadius: .5,
                        color: "common.white",
                        bgcolor: annotationColor(annotation, direction),
                        fontSize: 11,
                        fontWeight: 800,
                        lineHeight: 1,
                      }}
                    >
                      {abbreviation}
                    </Box>
                    <Box sx={{ minWidth: 0, overflowWrap: "anywhere" }}>
                      <Stack
                        direction="row"
                        spacing={.75}
                        useFlexGap
                        sx={{ alignItems: "baseline", justifyContent: "space-between", flexWrap: "wrap" }}
                      >
                        <Box component="span" sx={{ fontSize: 12, fontWeight: 700 }}>
                          {annotation.label}
                        </Box>
                        <Box component="span" className="mono" sx={{ fontSize: 12, fontWeight: 700 }}>
                          {chartPriceInput(annotation.price, priceTickSize)} USDT
                        </Box>
                      </Stack>
                      <Typography component="span" variant="caption" color="text.secondary" sx={{ display: "block" }}>
                        {annotationAuthorityLabel(annotation.authority)}
                        {detail ? ` · ${detail}` : ""}
                      </Typography>
                    </Box>
                  </Box>
                );
              })}
            </Box>
          ) : (
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: .75 }}>
              当前没有可画成固定水平线的有效绝对价格。
            </Typography>
          )}
        </Box>

        {operationMarkers.length > 0 ? (
          <Box sx={{ mt: 1.25 }}>
            <Typography variant="caption" sx={{ fontWeight: 750 }}>
              当前 K 线窗口内的操作点 · {resolvedOperationMarkers.length} / {operationMarkers.length}
            </Typography>
            {resolvedOperationMarkers.length > 0 ? (
              <Box
                component="ol"
                aria-label="图中操作点及对应价格"
                sx={{ mt: .75, mb: 0, pl: 2.5, display: "grid", gap: .6 }}
              >
                {resolvedOperationMarkers.map((item) => (
                  <Typography
                    component="li"
                    variant="caption"
                    key={item.marker.id}
                    sx={{ minWidth: 0, overflowWrap: "anywhere" }}
                  >
                    <Box component="span" sx={{ fontWeight: 700 }}>{item.marker.label}</Box>
                    {" · "}
                    {ANALYSIS_TIME_FORMATTER.format(new Date(item.marker.at))}
                    {" · "}
                    <Box component="span" className="mono">
                      {chartPriceInput(item.displayPrice, priceTickSize)} USDT
                    </Box>
                    {operationMarkerPriceNote(item)}
                    {item.marker.detail ? (
                      <Box component="span" color="text.secondary"> · {item.marker.detail}</Box>
                    ) : null}
                  </Typography>
                ))}
              </Box>
            ) : (
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: .75 }}>
                {timeWindowMode === "EXECUTION" && !executionWindowFullyVisible
                  ? "当前周期只能显示执行末段；切换到更大周期可查看完整区间与全部操作点。"
                  : "操作点不在当前 K 线窗口；点击“查看执行区间”可把计划起点至终点与全部操作点放入一屏。"}
              </Typography>
            )}
          </Box>
        ) : null}

        {annotations.relativeRules.length > 0 ? (
          <Box sx={{ mt: 1.25 }}>
            <Typography variant="caption" sx={{ fontWeight: 750 }}>
              图内动态价格规则（无固定水平线）
            </Typography>
            <Box
              component="ul"
              aria-label="图中相对和动态价格规则"
              sx={{ mt: .75, mb: 0, pl: 2.5, display: "grid", gap: .6 }}
            >
              {annotations.relativeRules.map((rule) => (
                <Typography
                  component="li"
                  variant="caption"
                  key={rule.id}
                  sx={{ minWidth: 0, overflowWrap: "anywhere" }}
                >
                  <Box component="span" sx={{ fontWeight: 700 }}>{rule.label}</Box>
                  <Box component="span" color="text.secondary"> · {rule.detail}</Box>
                </Typography>
              ))}
            </Box>
          </Box>
        ) : null}

        {analysisDrawings.length > 0 ? (
          <Box sx={{ mt: 1.25 }}>
            <Typography variant="caption" sx={{ fontWeight: 750 }}>
              分析绘图的等价数值列表
            </Typography>
            <Box
              component="ol"
              aria-label="图中分析绘图及锚点"
              sx={{ mt: 0.75, mb: 0, pl: 2.5, display: "grid", gap: 0.5 }}
            >
              {analysisDrawings.map((drawing, index) => (
                <Typography component="li" variant="caption" key={drawing.id}>
                  {analysisDrawingText(drawing, index, priceTickSize)}
                </Typography>
              ))}
            </Box>
          </Box>
        ) : null}

        {analysisDrawings.length > 0 ? (
          <Alert severity="info" variant="outlined" sx={{ mt: 1.25 }}>
            分析线不会改变运行中计划；沿趋势线自动移动入场、止盈或止损尚未开放。
          </Alert>
        ) : null}
        </Box>
      </Box>
    </Box>
  );
}
