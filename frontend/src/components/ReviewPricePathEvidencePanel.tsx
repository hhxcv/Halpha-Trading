import {
  Alert,
  Box,
  LinearProgress,
  Typography,
} from "@mui/material";

import type { ReviewPricePathEvidence } from "../api/client";
import { formatUserVisibleTime } from "../format";
import { surfaceFrameSx } from "../theme";
import FactGrid from "./FactGrid";


const reasonLabels: Record<string, string> = {
  EXTERNAL_POSITION_DISPOSITION: "外部持仓处置不具备本计划入场路径",
  NO_ATTRIBUTED_ENTRY: "没有可归属入场成交",
  TRADE_NOT_CLOSED: "交易尚未闭合",
  STRATEGY_ATTRIBUTION_INCOMPLETE: "策略归因不完整",
  FILL_TIMES_INCOMPLETE: "成交时间不完整",
  DIRECTION_UNKNOWN: "方向未知",
  AVERAGE_ENTRY_PRICE_UNKNOWN: "平均入场价未知",
  FIRST_REDUCTION_UNKNOWN: "首次减仓时间未知",
  FILL_SEQUENCE_INVALID: "成交先后无法形成可靠持仓窗口",
  NO_COMPLETE_HOLDING_BAR: "持仓时间内没有完整K线",
  INTERVAL_TOO_FINE: "当前周期无法在有界窗口内覆盖完整持仓",
  INITIAL_RISK_UNKNOWN: "冻结初始止损距离未知",
  MARKET_WINDOW_COVERAGE_MISMATCH: "行情窗口未精确覆盖持仓证据范围",
  MARKET_WINDOW_NOT_CLOSED: "行情窗口尚未完整闭合",
};

const sequenceLabels: Record<ReviewPricePathEvidence["threshold_sequence"], string> = {
  ONE_R_BEFORE_STOP: "先触及 1R，后触及止损",
  STOP_BEFORE_ONE_R: "先触及止损，后触及 1R",
  SAME_BAR_AMBIGUOUS: "同一根K线同时触及，先后未知",
  ONE_R_ONLY: "仅触及 1R",
  STOP_ONLY: "仅触及止损",
  NEITHER: "1R 与止损均未触及",
  UNKNOWN: "未知",
};

function parsedNumber(value: string | null | undefined): number | null {
  if (value === null || value === undefined || value.trim().length === 0) {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function reviewPricePathPercent(
  value: string | null | undefined,
): string {
  const parsed = parsedNumber(value);
  if (parsed === null) return "未知";
  return `${new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
    signDisplay: "exceptZero",
  }).format(parsed)}%`;
}

export function reviewPricePathR(value: string | null | undefined): string {
  const parsed = parsedNumber(value);
  if (parsed === null) return "未知";
  return `${new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 3,
  }).format(parsed)}R`;
}

export function reviewPricePathPrice(
  value: string | null | undefined,
): string {
  const parsed = parsedNumber(value);
  if (parsed === null) return "未知";
  return `${new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 8,
  }).format(parsed)} USDT`;
}

function touchLabel(value: boolean | null): string {
  if (value === null) return "未知";
  return value ? "触及" : "未触及";
}

export default function ReviewPricePathEvidencePanel({
  evidence,
  pending,
  failed,
}: {
  evidence: ReviewPricePathEvidence | undefined;
  pending: boolean;
  failed: boolean;
}) {
  const unavailableReasons = evidence?.reason_codes
    .map((reason) => reasonLabels[reason] ?? reason)
    .join("；");
  const adverseSequence = evidence
    ? ["STOP_BEFORE_ONE_R", "STOP_ONLY"].includes(evidence.threshold_sequence)
    : false;

  return (
    <Box
      component="section"
      aria-labelledby="review-price-path-title"
      sx={{ ...surfaceFrameSx, p: { xs: 1.5, sm: 2 }, mt: 1.5 }}
    >
      <Typography id="review-price-path-title" component="h2" variant="h3">
        入场后价路证据
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mt: .5 }}>
        只分析末次入场完成后、首次减仓前的完整 {evidence?.interval ?? "所选周期"} K线；部分K线和事后价格不混入持仓极值。
      </Typography>
      {pending && <LinearProgress aria-label="正在计算入场后价路证据" sx={{ mt: 1.5 }} />}
      {failed && <Alert severity="warning" variant="outlined" sx={{ mt: 1.5 }}>
        价路证据当前不可读。不会把行情读取失败解释为“没有浮亏”“没有触发止损”或“入场正确”。
      </Alert>}
      {evidence?.evidence_status === "NOT_APPLICABLE" && <Alert severity="info" variant="outlined" sx={{ mt: 1.5 }}>
        本次不适用价路分析：{unavailableReasons || "没有可分析的本计划入场持仓"}。
      </Alert>}
      {evidence?.evidence_status === "UNKNOWN" && <Alert severity="warning" variant="outlined" sx={{ mt: 1.5 }}>
        当前不能形成可靠价路结论：{unavailableReasons || "关键输入未知"}。缺失值不会按零处理。
      </Alert>}
      {evidence?.evidence_status === "AVAILABLE" && <>
        <Alert
          severity={evidence.ever_favorable_on_complete_bars === false || adverseSequence ? "warning" : "info"}
          variant="outlined"
          sx={{ mt: 1.5 }}
        >
          {evidence.ever_favorable_on_complete_bars === false
            ? `纳入的 ${evidence.complete_bar_count} 根完整 ${evidence.interval} K线中，价格从未越过平均入场价的有利一侧。这支持“完整K线证据未见浮盈”，但不覆盖入场与退出所在的部分K线。`
            : `价格曾进入有利一侧；最佳方向移动为 ${reviewPricePathPercent(evidence.best_directional_move_percent)}，最大不利波动为 ${reviewPricePathPercent(evidence.maximum_adverse_excursion_percent)}。这描述路径，不自动判定入场正确。`}
        </Alert>
        <FactGrid
          columns={3}
          dense
          facts={[
            {
              label: "完整K线",
              value: `${evidence.complete_bar_count} 根 · ${evidence.interval}`,
              note: evidence.analysis_start_at && evidence.analysis_end_at
                ? `${formatUserVisibleTime(evidence.analysis_start_at)} 至 ${formatUserVisibleTime(evidence.analysis_end_at)}`
                : undefined,
            },
            { label: "实际平均入场", value: reviewPricePathPrice(evidence.average_entry_price) },
            {
              label: "冻结初始止损",
              value: evidence.initial_stop_distance_bps === null
                ? "未知"
                : `${evidence.initial_stop_distance_bps} bps`,
              note: evidence.planned_stop_price
                ? `按平均入场折算 ${reviewPricePathPrice(evidence.planned_stop_price)}`
                : undefined,
            },
            {
              label: "最佳方向移动",
              value: reviewPricePathPercent(evidence.best_directional_move_percent),
              note: `${reviewPricePathPrice(evidence.best_favorable_price)} · 所在K线 ${formatUserVisibleTime(evidence.best_favorable_bar_open_at)}`,
            },
            {
              label: "最大不利波动",
              value: reviewPricePathPercent(evidence.maximum_adverse_excursion_percent),
              note: `${reviewPricePathPrice(evidence.worst_adverse_price)} · 所在K线 ${formatUserVisibleTime(evidence.worst_adverse_bar_open_at)}`,
            },
            {
              label: "MFE / MAE",
              value: `${reviewPricePathR(evidence.maximum_favorable_excursion_r)} / ${reviewPricePathR(evidence.maximum_adverse_excursion_r)}`,
              note: "按冻结初始止损距离折算",
            },
            {
              label: "1R / 2R / 止损",
              value: `${touchLabel(evidence.one_r_touched)} / ${touchLabel(evidence.two_r_touched)} / ${touchLabel(evidence.planned_stop_touched)}`,
            },
            {
              label: "阈值先后",
              value: sequenceLabels[evidence.threshold_sequence],
              note: evidence.threshold_sequence === "SAME_BAR_AMBIGUOUS"
                ? "OHLC 无法还原同一根K线内先后"
                : undefined,
            },
            {
              label: "首根完整K线收盘",
              value: reviewPricePathPercent(evidence.first_complete_bar_directional_return_percent),
              note: `${reviewPricePathPrice(evidence.first_complete_bar_close_price)} · ${formatUserVisibleTime(evidence.first_complete_bar_close_at)}`,
            },
          ]}
        />
        {evidence.reason_codes.includes("INITIAL_RISK_UNKNOWN") && <Alert severity="info" variant="outlined" sx={{ mt: 1.5 }}>
          冻结初始止损距离未知，因此仍可显示原始价格路径，但 R 倍数和阈值先后保持未知。
        </Alert>}
        <Box component="details" sx={{ mt: 1.25 }}>
          <Box component="summary" sx={{ cursor: "pointer", fontSize: 12, fontWeight: 700 }}>
            口径与限制
          </Box>
          <Box component="ul" sx={{ mt: 1, mb: 0, pl: 2.5, color: "text.secondary" }}>
            {evidence.limitations.map((item) => <Typography component="li" variant="caption" key={item}>{item}</Typography>)}
          </Box>
        </Box>
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1.25 }}>
          行情来源 {evidence.market_source ?? "未知"}，截止 {formatUserVisibleTime(evidence.market_source_cutoff)}；该投影没有加本金或放大风险的授权效力。
        </Typography>
      </>}
    </Box>
  );
}
