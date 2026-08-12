import { Alert, Box, Typography } from "@mui/material";

import { formatUserVisibleTime, shortDigest } from "../format";
import { surfaceFrameSx } from "../theme";
import FactGrid from "./FactGrid";


type Direction = "LONG" | "SHORT";

function recordOf(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function textOf(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function finite(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatted(
  value: number | null,
  options: Intl.NumberFormatOptions,
): string {
  return value === null
    ? "未知"
    : new Intl.NumberFormat("zh-CN", options).format(value);
}

export function entrySetupPrice(value: unknown): string {
  const parsed = finite(value);
  return parsed === null
    ? "未知"
    : formatted(parsed, { minimumFractionDigits: 2, maximumFractionDigits: 8 }) + " USDT";
}

export function entrySetupAtr(value: unknown): string {
  const parsed = finite(value);
  return parsed === null
    ? "未知"
    : formatted(parsed, { minimumFractionDigits: 2, maximumFractionDigits: 4, signDisplay: "exceptZero" }) + " ATR";
}

export function entrySetupBps(value: unknown): string {
  const parsed = finite(value);
  return parsed === null
    ? "未知"
    : formatted(parsed, { minimumFractionDigits: 2, maximumFractionDigits: 3, signDisplay: "exceptZero" }) + " bps";
}

export function directionalEntryDistance(
  direction: Direction,
  priceValue: unknown,
  boundaryValue: unknown,
  atrValue: unknown,
): { price: number; atr: number; bps: number } | null {
  const price = finite(priceValue);
  const boundary = finite(boundaryValue);
  const atr = finite(atrValue);
  if (price === null || boundary === null || atr === null || boundary <= 0 || atr <= 0) {
    return null;
  }
  const distance = direction === "LONG" ? price - boundary : boundary - price;
  return {
    price: distance,
    atr: distance / atr,
    bps: distance / boundary * 10_000,
  };
}

export function entrySetupAssessment(
  mode: string,
  lastExtensionAtrValue: unknown,
  maxExtensionAtrValue: unknown,
  referenceWithinMax: unknown,
): { severity: "info" | "warning"; message: string } {
  if (mode === "DEMO_ORDER_FLOW_CHECK") {
    return {
      severity: "info",
      message: "这是下单闭环验证，不是 Donchian 突破样本；不能用本次触发评价突破逻辑。",
    };
  }
  const lastExtension = finite(lastExtensionAtrValue);
  const maximum = finite(maxExtensionAtrValue);
  if (lastExtension === null || maximum === null || maximum <= 0) {
    return {
      severity: "warning",
      message: "触发快照不完整，不能判断入场是在突破边界附近还是已经追离。",
    };
  }
  if (referenceWithinMax !== true) {
    return {
      severity: "warning",
      message: "闭合 K 线满足突破，但触发时的保守参考价已经越过最大追价边界；该时刻不应形成可提交入场。",
    };
  }
  const used = lastExtension / maximum;
  if (used >= 0.8) {
    return {
      severity: "warning",
      message: "突破确认成立，但最后确认收盘已用掉至少 80% 的允许追价空间；这属于边界偏晚的入场，需重点核对后续回撤。",
    };
  }
  if (used <= 0.25) {
    return {
      severity: "info",
      message: "确认收盘仍靠近突破边界，触发本身没有明显追离；后续亏损不能仅凭结果反推为追高。",
    };
  }
  return {
    severity: "info",
    message: "突破确认成立且仍在最大追价范围内；入场位置居于边界与上限之间，仍需结合实际成交和入场后价路判断。",
  };
}

export default function ReviewEntrySetupEvidencePanel({
  entryRiskContext,
  direction,
  averageEntryPrice,
  timelineFailed,
}: {
  entryRiskContext: Record<string, unknown> | null;
  direction: string;
  averageEntryPrice: string | null;
  timelineFailed: boolean;
}) {
  const context = entryRiskContext ?? {};
  const evidence = recordOf(context.setup_evidence);
  const hasEvidence = Object.keys(evidence).length > 0;
  const normalizedDirection: Direction = direction === "SHORT" ? "SHORT" : "LONG";
  const mode = textOf(evidence.mode);
  const assessment = entrySetupAssessment(
    mode,
    evidence.last_confirmation_extension_atr,
    evidence.max_entry_extension_atr,
    evidence.reference_within_max_extension,
  );
  const fillDistance = directionalEntryDistance(
    normalizedDirection,
    averageEntryPrice,
    evidence.trigger_boundary,
    evidence.trigger_atr,
  );
  const referencePrice = finite(evidence.trigger_reference_price);
  const fillPrice = finite(averageEntryPrice);
  const fillVsReference = referencePrice !== null && fillPrice !== null
    ? (normalizedDirection === "LONG" ? fillPrice - referencePrice : referencePrice - fillPrice)
    : null;
  const fillVsReferenceBps = fillVsReference !== null && referencePrice !== null && referencePrice > 0
    ? fillVsReference / referencePrice * 10_000
    : null;
  const triggerAtr = finite(evidence.trigger_atr);
  const stopMultiple = finite(context.initial_stop_atr_multiple);
  const stopDistance = triggerAtr !== null && stopMultiple !== null
    ? triggerAtr * stopMultiple
    : null;
  const plannedStop = stopDistance !== null && fillPrice !== null
    ? normalizedDirection === "LONG" ? fillPrice - stopDistance : fillPrice + stopDistance
    : null;
  const closes = Array.isArray(evidence.confirmation_closes)
    ? evidence.confirmation_closes.map((item) => textOf(item)).filter(Boolean)
    : [];
  const closeTimes = Array.isArray(evidence.confirmation_close_times)
    ? evidence.confirmation_close_times.map((item) => textOf(item)).filter(Boolean)
    : [];

  return (
    <Box
      component="section"
      aria-labelledby="review-entry-setup-title"
      sx={{ ...surfaceFrameSx, p: { xs: 1.5, sm: 2 }, mt: 1.5 }}
    >
      <Typography id="review-entry-setup-title" variant="h3">
        突破入场还原
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mt: .5 }}>
        使用策略触发时冻结的通道、ATR、逐根闭合 1m 收盘和保守参考价；实际成交另取交易所事实，不用事后 K 线补造入场理由。
      </Typography>
      {timelineFailed && <Alert severity="warning" variant="outlined" sx={{ mt: 1.5 }}>
        激活时间线读取失败，不能核对当时的突破触发快照。
      </Alert>}
      {!timelineFailed && !hasEvidence && <Alert severity="warning" variant="outlined" sx={{ mt: 1.5 }}>
        这笔历史记录没有结构化入场快照；只能确认动作与成交，不能可靠判断当时是否贴近突破边界或已经追价。
      </Alert>}
      {hasEvidence && <>
        <Alert severity={assessment.severity} variant="outlined" sx={{ mt: 1.5 }}>
          {assessment.message}
        </Alert>
        <FactGrid
          columns={3}
          dense
          facts={[
            {
              label: normalizedDirection === "LONG" ? "做多突破上沿" : "做空突破下沿",
              value: entrySetupPrice(evidence.trigger_boundary),
              note: "通道 " + entrySetupPrice(evidence.channel_upper) + " / " + entrySetupPrice(evidence.channel_lower),
            },
            {
              label: "连续确认收盘",
              value: closes.length > 0 ? closes.map((item) => entrySetupPrice(item)).join(" → ") : "未知",
              note: closeTimes.length > 0
                ? closeTimes.map((item) => formatUserVisibleTime(item)).join(" → ")
                : undefined,
            },
            {
              label: "确认收盘离边界",
              value: entrySetupAtr(evidence.last_confirmation_extension_atr) + " · " + entrySetupBps(evidence.last_confirmation_extension_bps),
              note: "最多允许 " + entrySetupAtr(evidence.max_entry_extension_atr) + "，价格上限 " + entrySetupPrice(evidence.max_entry_extension_price),
            },
            {
              label: "触发时保守参考价",
              value: entrySetupPrice(evidence.trigger_reference_price),
              note: (textOf(evidence.trigger_reference_source) || "来源未知") + " · 离边界 " + entrySetupAtr(evidence.reference_extension_atr),
            },
            {
              label: "实际平均入场",
              value: entrySetupPrice(averageEntryPrice),
              note: fillDistance
                ? "离突破边界 " + entrySetupAtr(fillDistance.atr) + " · " + entrySetupBps(fillDistance.bps)
                : "成交或边界事实不足",
            },
            {
              label: "成交相对触发参考",
              value: fillVsReference === null
                ? "未知"
                : entrySetupPrice(fillVsReference) + " · " + entrySetupBps(fillVsReferenceBps),
              note: "正值表示沿交易方向成交得更差；市价单不承诺触发参考价",
            },
            {
              label: "触发 ATR",
              value: entrySetupPrice(evidence.trigger_atr),
              note: "15m 指标截止 " + formatUserVisibleTime(textOf(evidence.indicator_source_cutoff_at)),
            },
            {
              label: "冻结初始止损距离",
              value: stopDistance === null ? "未知" : entrySetupPrice(stopDistance),
              note: plannedStop === null
                ? undefined
                : String(stopMultiple) + " ATR · 按平均入场折算 " + entrySetupPrice(plannedStop),
            },
            {
              label: "触发证据截止",
              value: formatUserVisibleTime(textOf(evidence.source_cutoff)),
              note: "指标输入摘要 " + shortDigest(textOf(evidence.indicator_source_digest)),
            },
          ]}
        />
        {fillDistance && finite(evidence.max_entry_extension_atr) !== null
          && fillDistance.atr > Number(evidence.max_entry_extension_atr) && <Alert severity="warning" variant="outlined" sx={{ mt: 1.5 }}>
            实际平均成交已越过策略的最大追价边界。触发时仍可能通过检查，但市价成交存在延迟与滑点；这需要与提交前检查和交易所成交事实分开评价。
          </Alert>}
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1.25 }}>
          这份快照解释“机器为何在该时刻提出入场”，不证明突破具有正期望，也不授予继续交易、加本金或提高风险。
        </Typography>
      </>}
    </Box>
  );
}
