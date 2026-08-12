import {
  Alert,
  Box,
  Button,
  LinearProgress,
  MenuItem,
  Stack,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
} from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import {
  getReviewSequenceEvidence,
  type ReviewPricePathInterval,
  type ReviewSequenceEvidence,
  type ReviewSequenceScope,
} from "../api/client";
import { formatUserVisibleTime } from "../format";
import { surfaceFrameSx } from "../theme";
import FactGrid from "./FactGrid";


type RangePreset = "ALL" | "30D" | "90D";
type SequenceSegment = ReviewSequenceEvidence["segments"][number];

const exclusionLabels: Record<string, string> = {
  NO_ATTRIBUTED_TRADE: "未形成可归属交易",
  EXTERNAL_POSITION_DISPOSITION: "外部持仓处置",
  UNRELIABLE_RESULT: "结果或费用不可靠",
  FILL_TIME_UNKNOWN: "平仓时间未知",
  OUTSIDE_RANGE: "不在所选范围",
  VALIDATION_INTENT: "交易前目的为验证",
  MISSING_DECISION_INTENT: "历史缺少交易前目的",
  PENDING_REVIEW: "复盘仍待评价",
  TOOLING_ISSUE: "工具问题影响",
  VALIDATION_TRADE: "验证性交易",
  INSUFFICIENT_EVIDENCE: "证据不足",
  MISSING_CLASSIFICATION: "缺少复盘分类",
  EXECUTION_COST_INCOMPLETE: "执行费用不完整",
  STRATEGY_ATTRIBUTION_INCOMPLETE: "策略归因不完整",
  ENTRY_NOTIONAL_UNKNOWN: "入场名义金额未知",
  REVIEW_IDENTITY_UNKNOWN: "复盘身份未知",
};

const resultKindLabels: Record<SequenceSegment["result_kind"], string> = {
  WIN: "连盈",
  LOSS: "连亏",
  FLAT: "持平",
};

function parsedNumber(value: string | null | undefined): number | null {
  if (value === null || value === undefined || value.trim().length === 0) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function sequenceEvidenceUsdt(value: string | null | undefined): string {
  const parsed = parsedNumber(value);
  if (parsed === null) return "未知";
  const normalized = Math.abs(parsed) < .0000005 ? 0 : parsed;
  return `${new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 6,
    signDisplay: "exceptZero",
  }).format(normalized)} USDT`;
}

export function sequenceEvidencePercent(value: string | null | undefined): string {
  const parsed = parsedNumber(value);
  if (parsed === null) return "未知";
  const normalized = Math.abs(parsed) < .00005 ? 0 : parsed;
  return `${new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
    signDisplay: "exceptZero",
  }).format(normalized)}%`;
}

export function sequenceEvidenceR(value: string | null | undefined): string {
  const parsed = parsedNumber(value);
  if (parsed === null) return "未知";
  return `${new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 3,
  }).format(parsed)}R`;
}

function sequenceEvidenceMagnitudeUsdt(value: string | null | undefined): string {
  const parsed = parsedNumber(value);
  if (parsed === null) return "未知";
  return `${new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 6,
  }).format(Math.abs(parsed))} USDT`;
}

function sequenceEvidenceMagnitudePercent(value: string | null | undefined): string {
  const parsed = parsedNumber(value);
  if (parsed === null) return "未知";
  return `${new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  }).format(Math.abs(parsed))}%`;
}

function rangeStartForPreset(
  preset: RangePreset,
  latestClosedAt: string | null,
): string | undefined {
  if (preset === "ALL") return undefined;
  const latest = latestClosedAt ? Date.parse(latestClosedAt) : Number.NaN;
  if (!Number.isFinite(latest)) return undefined;
  const days = preset === "30D" ? 30 : 90;
  return new Date(latest - days * 24 * 60 * 60_000).toISOString();
}

function segmentByIndex(
  evidence: ReviewSequenceEvidence,
  index: number | null,
): SequenceSegment | undefined {
  if (index === null) return undefined;
  return evidence.segments.find((segment) => segment.segment_index === index);
}

function segmentValue(segment: SequenceSegment | undefined): string {
  if (!segment) return "无";
  return `${segment.trade_count} 笔 · ${sequenceEvidenceUsdt(segment.net_pnl)}`;
}

function segmentNote(segment: SequenceSegment | undefined): string | undefined {
  if (!segment) return undefined;
  return `${formatUserVisibleTime(segment.start_at)} 至 ${formatUserVisibleTime(segment.end_at)}`;
}

function exclusionSummary(evidence: ReviewSequenceEvidence): string {
  const rows = Object.entries(evidence.exclusions)
    .filter(([, count]) => count > 0)
    .sort((left, right) => right[1] - left[1])
    .slice(0, 5)
    .map(([reason, count]) => `${exclusionLabels[reason] ?? reason} ${count} 条`);
  return rows.length > 0 ? rows.join("；") : "没有排除项";
}

export default function ReviewSequenceEvidencePanel({
  environmentId,
  latestClosedAt,
  onOpenReview,
}: {
  environmentId: string;
  latestClosedAt: string | null;
  onOpenReview: (reviewId: string) => void;
}) {
  const [scope, setScope] = useState<ReviewSequenceScope>("ACCOUNT_RESULTS");
  const [rangePreset, setRangePreset] = useState<RangePreset>("ALL");
  const [pathInterval, setPathInterval] = useState<ReviewPricePathInterval>("1m");
  const [pathRequestSignature, setPathRequestSignature] = useState<string | null>(null);
  const rangeStart = useMemo(
    () => rangeStartForPreset(rangePreset, latestClosedAt),
    [latestClosedAt, rangePreset],
  );
  const currentPathSignature = `${environmentId}:${scope}:${rangePreset}:${pathInterval}`;
  const pathRequested = pathRequestSignature === currentPathSignature;

  const queryKey = [
    "review-sequence-evidence",
    environmentId,
    scope,
    rangePreset,
    rangeStart ?? "ALL",
  ] as const;
  const query = useQuery({
    queryKey,
    queryFn: () => getReviewSequenceEvidence({ scope, rangeStart }),
    staleTime: 60_000,
  });
  const pathQuery = useQuery({
    queryKey: [...queryKey, "price-path", pathInterval],
    queryFn: () => getReviewSequenceEvidence({
      scope,
      rangeStart,
      includePricePath: true,
      interval: pathInterval,
    }),
    enabled: pathRequested,
    staleTime: 60 * 60_000,
  });
  const evidence = pathQuery.data ?? query.data;
  const metrics = evidence?.metrics;
  const longestWin = evidence && metrics
    ? segmentByIndex(evidence, metrics.longest_win_segment_index)
    : undefined;
  const longestLoss = evidence && metrics
    ? segmentByIndex(evidence, metrics.longest_loss_segment_index)
    : undefined;
  const bestWin = evidence && metrics
    ? segmentByIndex(evidence, metrics.best_win_segment_index)
    : undefined;
  const worstLoss = evidence && metrics
    ? segmentByIndex(evidence, metrics.worst_loss_segment_index)
    : undefined;
  const currentSegment = evidence && metrics
    ? segmentByIndex(evidence, metrics.current_segment_index)
    : undefined;
  const recentSegments = evidence?.segments.slice(-12).reverse() ?? [];
  const path = pathQuery.data?.price_path;
  const winPath = path?.by_result.WIN;
  const lossPath = path?.by_result.LOSS;

  return (
    <Box
      component="section"
      aria-labelledby="review-sequence-evidence-title"
      sx={{ ...surfaceFrameSx, p: { xs: 1.5, sm: 2 }, mb: 2 }}
    >
      <Stack
        direction={{ xs: "column", md: "row" }}
        spacing={1.25}
        sx={{ justifyContent: "space-between", alignItems: { md: "flex-start" } }}
      >
        <Box>
          <Typography id="review-sequence-evidence-title" variant="h2">
            连续交易证据
          </Typography>
          <Typography variant="caption" color="text.secondary">
            按确切平仓时间还原连续结果、回撤和尾部敏感性；账户结果与盈利导向样本分开，不把缺失目的补猜成策略证据。
          </Typography>
        </Box>
        <Stack direction="row" spacing={1} sx={{ flexWrap: "wrap", justifyContent: { md: "flex-end" } }}>
          <ToggleButtonGroup
            size="small"
            exclusive
            value={scope}
            onChange={(_event, value: ReviewSequenceScope | null) => value && setScope(value)}
            aria-label="连续交易证据范围"
          >
            <ToggleButton value="ACCOUNT_RESULTS">账户结果</ToggleButton>
            <ToggleButton value="PROFIT_SEEKING">盈利导向</ToggleButton>
          </ToggleButtonGroup>
          <ToggleButtonGroup
            size="small"
            exclusive
            value={rangePreset}
            onChange={(_event, value: RangePreset | null) => value && setRangePreset(value)}
            aria-label="连续交易时间范围"
          >
            <ToggleButton value="ALL">全部</ToggleButton>
            <ToggleButton value="30D">30 日</ToggleButton>
            <ToggleButton value="90D">90 日</ToggleButton>
          </ToggleButtonGroup>
        </Stack>
      </Stack>

      {query.isPending && <LinearProgress aria-label="正在计算连续交易证据" sx={{ mt: 1.5 }} />}
      {query.isError && <Alert severity="error" variant="outlined" sx={{ mt: 1.5 }}>
        连续交易证据当前不可读；不会把读取失败显示为空样本、零回撤或稳定盈利。
      </Alert>}
      {evidence && metrics && <>
        {evidence.eligible_trade_count === 0
          ? <Alert severity="warning" variant="outlined" sx={{ mt: 1.5 }}>
              当前范围没有符合“{scope === "ACCOUNT_RESULTS" ? "可排序可靠账户结果" : "交易前已记录为盈利导向且完成评价"}”口径的交易。排除情况：{exclusionSummary(evidence)}。
            </Alert>
          : <>
              <FactGrid
                columns={3}
                dense
                facts={[
                  { label: "可排序闭合结果", value: `${metrics.trade_count} 笔`, note: `${metrics.wins} 盈 / ${metrics.losses} 亏 / ${metrics.flat} 平` },
                  { label: "累计净盈亏", value: sequenceEvidenceUsdt(metrics.net_pnl), note: `手续费 ${sequenceEvidenceMagnitudeUsdt(metrics.commission)}` },
                  { label: "样本路径最大回撤", value: sequenceEvidenceUsdt(`-${metrics.maximum_drawdown}`), note: metrics.maximum_drawdown_trough_at ? `谷底 ${formatUserVisibleTime(metrics.maximum_drawdown_trough_at)}` : undefined },
                  { label: "最大亏损占全部亏损", value: sequenceEvidenceMagnitudePercent(metrics.largest_loss_share_percent), note: metrics.worst_trade_net_pnl ? `最差一笔 ${sequenceEvidenceUsdt(metrics.worst_trade_net_pnl)}` : "当前没有亏损结果" },
                  { label: "最长连盈", value: segmentValue(longestWin), note: segmentNote(longestWin) },
                  { label: "最长连亏", value: segmentValue(longestLoss), note: segmentNote(longestLoss) },
                  { label: "最佳连盈段", value: segmentValue(bestWin), note: segmentNote(bestWin) },
                  { label: "最差连亏段", value: segmentValue(worstLoss), note: segmentNote(worstLoss) },
                ]}
              />
              <Alert severity="info" variant="outlined" sx={{ mt: 1.5 }}>
                真实累计结果为 {sequenceEvidenceUsdt(metrics.net_pnl)}。
                {metrics.net_pnl_without_worst_trade !== null
                  ? ` 事后剔除最差一笔会变成 ${sequenceEvidenceUsdt(metrics.net_pnl_without_worst_trade)}；剔除最差 ${metrics.top_loss_count} 笔会变成 ${sequenceEvidenceUsdt(metrics.net_pnl_without_top_losses)}，这些都不具有真实业绩效力。`
                  : " 当前没有可计算的亏损剔除敏感性。"}
                {currentSegment ? ` 当前连续段：${resultKindLabels[currentSegment.result_kind]} ${currentSegment.trade_count} 笔，合计 ${sequenceEvidenceUsdt(currentSegment.net_pnl)}。` : ""}
              </Alert>
            </>}

        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1.25 }}>
          来源：当前环境最新复盘 · 截止 {formatUserVisibleTime(evidence.source_cutoff)} · 排除 {evidence.excluded_review_count} 条（{exclusionSummary(evidence)}）。账户权益收益率和复利收益率保持未知；该投影没有加本金或放大风险的授权效力。
        </Typography>

        {evidence.eligible_trade_count > 0 && <Box sx={{ mt: 1.5, pt: 1.5, borderTop: "1px solid", borderColor: "divider" }}>
          <Stack
            direction={{ xs: "column", sm: "row" }}
            spacing={1}
            sx={{ alignItems: { sm: "center" }, justifyContent: "space-between" }}
          >
            <Box>
              <Typography variant="h3">入场后价路覆盖</Typography>
              <Typography variant="caption" color="text.secondary">
                显式读取每笔精确持仓窗口；不扫描其他品种，也不把无完整 K 线当成无浮盈。
              </Typography>
            </Box>
            <Stack direction="row" spacing={1}>
              <TextField
                select
                size="small"
                label="K 线周期"
                value={pathInterval}
                onChange={(event) => setPathInterval(event.target.value as ReviewPricePathInterval)}
                sx={{ minWidth: 110 }}
              >
                {(["1m", "5m", "15m", "1h", "4h", "1d"] as ReviewPricePathInterval[]).map((interval) => (
                  <MenuItem key={interval} value={interval}>{interval}</MenuItem>
                ))}
              </TextField>
              <Button
                variant="outlined"
                onClick={() => {
                  if (pathRequested || pathQuery.data) {
                    void pathQuery.refetch();
                    return;
                  }
                  setPathRequestSignature(currentPathSignature);
                }}
                disabled={pathQuery.isFetching}
              >
                {pathQuery.isFetching ? "正在还原…" : pathQuery.data ? "重新读取价路" : "计算价路"}
              </Button>
            </Stack>
          </Stack>
          {pathQuery.isFetching && <LinearProgress aria-label="正在逐笔还原价路" sx={{ mt: 1.25 }} />}
          {pathQuery.isError && <Alert severity="warning" variant="outlined" sx={{ mt: 1.25 }}>
            批量价路当前不可读；账户结果和连续段仍可用，但不会生成无浮盈比例或 MFE/MAE 零值。
          </Alert>}
          {path && <>
            <FactGrid
              columns={3}
              dense
              facts={[
                { label: "价路可用覆盖", value: `${path.available_count} / ${path.trade_count} 笔`, note: `未知 ${path.unknown_count} · 不适用 ${path.not_applicable_count}` },
                { label: "完整 K 线从未有利", value: `${path.never_favorable_count} 笔`, note: `曾有利 ${path.ever_favorable_count} 笔` },
                { label: "中位 MFE / MAE", value: `${sequenceEvidenceMagnitudePercent(path.median_mfe_percent)} / ${sequenceEvidenceMagnitudePercent(path.median_mae_percent)}`, note: "只按价路可用交易" },
                { label: "中位 MFE / MAE（R）", value: `${sequenceEvidenceR(path.median_mfe_r)} / ${sequenceEvidenceR(path.median_mae_r)}`, note: "缺少冻结止损的交易不进入 R 中位数" },
                { label: "盈利交易无有利价路", value: winPath ? `${winPath.never_favorable_count} / ${winPath.available_count} 笔` : "未知" },
                { label: "亏损交易无有利价路", value: lossPath ? `${lossPath.never_favorable_count} / ${lossPath.available_count} 笔` : "未知" },
                { label: "盈利交易中位 MFE", value: sequenceEvidenceR(winPath?.median_mfe_r), note: sequenceEvidenceMagnitudePercent(winPath?.median_mfe_percent) },
                { label: "亏损交易中位 MAE", value: sequenceEvidenceR(lossPath?.median_mae_r), note: sequenceEvidenceMagnitudePercent(lossPath?.median_mae_percent) },
              ]}
            />
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
              价路来源截止 {formatUserVisibleTime(path.market_source_cutoff)}；覆盖不足、行情失败和同 K 线先后未知均保留在排除原因中。
            </Typography>
          </>}
        </Box>}

        {recentSegments.length > 0 && <Box component="details" sx={{ mt: 1.5 }}>
          <Box component="summary" sx={{ cursor: "pointer", fontSize: 13, fontWeight: 700 }}>
            查看最近连续段（最多 12 段）
          </Box>
          <Stack spacing={.5} sx={{ mt: 1 }}>
            {recentSegments.map((segment) => (
              <Stack
                key={segment.segment_index}
                direction={{ xs: "column", sm: "row" }}
                spacing={1}
                sx={{ px: 1, py: .75, borderRadius: 1, bgcolor: "action.hover", justifyContent: "space-between", alignItems: { sm: "center" } }}
              >
                <Box>
                  <Typography variant="body2" sx={{ fontWeight: 700 }}>
                    {resultKindLabels[segment.result_kind]} · {segment.trade_count} 笔 · {sequenceEvidenceUsdt(segment.net_pnl)}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    {formatUserVisibleTime(segment.start_at)} 至 {formatUserVisibleTime(segment.end_at)}
                  </Typography>
                </Box>
                <Button
                  size="small"
                  variant="text"
                  onClick={() => onOpenReview(segment.review_refs[0]?.review_id ?? "")}
                  disabled={!segment.review_refs[0]?.review_id}
                >
                  查看首笔
                </Button>
              </Stack>
            ))}
          </Stack>
        </Box>}
      </>}
    </Box>
  );
}
