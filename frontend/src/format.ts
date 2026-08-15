export const USER_VISIBLE_TIME_ZONE = "Asia/Shanghai";
export const USER_VISIBLE_TIME_ZONE_LABEL = "";
export const USER_VISIBLE_TIME_LOCALE = "zh-CN";
export const MAX_TRADING_DECIMAL_FRACTION_DIGITS = 8;

export function positiveFiniteNumber(value: unknown): number | null {
  if (
    value === null
    || value === undefined
    || (typeof value === "string" && value.trim() === "")
  ) {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

type DecimalParts = {
  sign: "" | "-";
  integer: string;
  fraction: string;
};

type DecimalDisplayOptions = {
  maximumFractionDigits?: number;
  minimumFractionDigits?: number;
  useGrouping?: boolean;
  truncatedMarker?: string;
};

function decimalParts(value: string | number): DecimalParts | null {
  const raw = String(value).trim();
  const match = raw.match(/^([+-]?)(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/);
  if (!match) return null;

  const sign = match[1] === "-" ? "-" : "";
  const integer = match[2] ?? "0";
  const fraction = match[3] ?? "";
  const exponent = Number(match[4] ?? "0");
  if (!Number.isSafeInteger(exponent) || Math.abs(exponent) > 100) return null;

  const digits = `${integer}${fraction}`;
  const decimalIndex = integer.length + exponent;
  const expanded = decimalIndex <= 0
    ? `0.${"0".repeat(-decimalIndex)}${digits}`
    : decimalIndex >= digits.length
      ? `${digits}${"0".repeat(decimalIndex - digits.length)}`
      : `${digits.slice(0, decimalIndex)}.${digits.slice(decimalIndex)}`;
  const [expandedInteger = "0", expandedFraction = ""] = expanded.split(".");
  const normalizedInteger = expandedInteger.replace(/^0+(?=\d)/, "") || "0";
  const normalizedFraction = expandedFraction.replace(/0+$/, "");
  const normalizedSign = normalizedInteger === "0" && !/[1-9]/.test(normalizedFraction)
    ? ""
    : sign;
  return {
    sign: normalizedSign,
    integer: normalizedInteger,
    fraction: normalizedFraction,
  };
}

function boundedFractionDigits(value: number | undefined, fallback: number): number {
  if (!Number.isInteger(value)) return fallback;
  return Math.min(MAX_TRADING_DECIMAL_FRACTION_DIGITS, Math.max(0, value ?? fallback));
}

function groupedInteger(value: string): string {
  return value.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function scaledInteger(value: DecimalParts, scale: number): bigint {
  const digits = `${value.integer}${value.fraction.padEnd(scale, "0")}`;
  const amount = BigInt(digits || "0");
  return value.sign === "-" ? -amount : amount;
}

export function subtractDecimal(
  minuend: string,
  subtrahend: string,
): string | null {
  const left = decimalParts(minuend);
  const right = decimalParts(subtrahend);
  if (!left || !right) return null;
  const scale = Math.max(left.fraction.length, right.fraction.length);
  const difference = scaledInteger(left, scale) - scaledInteger(right, scale);
  const sign = difference < 0n ? "-" : "";
  const digits = (difference < 0n ? -difference : difference)
    .toString()
    .padStart(scale + 1, "0");
  if (scale === 0) return `${sign}${digits}`;
  const integer = digits.slice(0, -scale) || "0";
  const fraction = digits.slice(-scale).replace(/0+$/, "");
  return `${sign}${integer}${fraction ? `.${fraction}` : ""}`;
}

export function scaleDecimalByPowerOfTen(
  value: string | number,
  power: number,
): string | null {
  const parsed = decimalParts(value);
  if (!parsed || !Number.isSafeInteger(power) || Math.abs(power) > 100) {
    return null;
  }
  const digits = `${parsed.integer}${parsed.fraction}`;
  const decimalIndex = parsed.integer.length + power;
  const shifted = decimalIndex <= 0
    ? `0.${"0".repeat(-decimalIndex)}${digits}`
    : decimalIndex >= digits.length
      ? `${digits}${"0".repeat(decimalIndex - digits.length)}`
      : `${digits.slice(0, decimalIndex)}.${digits.slice(decimalIndex)}`;
  const normalized = decimalParts(`${parsed.sign}${shifted}`);
  if (!normalized) return null;
  return `${normalized.sign}${normalized.integer}${
    normalized.fraction ? `.${normalized.fraction}` : ""
  }`;
}

/**
 * Bounds user-visible decimals without changing the exact value used by forms,
 * API payloads, calculations, or persistence. An ellipsis makes any omitted
 * non-zero fraction explicit instead of presenting a rounded value as exact.
 */
export function compactDecimal(
  value: string | number,
  options: DecimalDisplayOptions = {},
): string {
  const parsed = decimalParts(value);
  if (!parsed) return String(value);
  const maximumFractionDigits = boundedFractionDigits(
    options.maximumFractionDigits,
    MAX_TRADING_DECIMAL_FRACTION_DIGITS,
  );
  const minimumFractionDigits = Math.min(
    maximumFractionDigits,
    boundedFractionDigits(options.minimumFractionDigits, 0),
  );
  const visibleFraction = parsed.fraction.slice(0, maximumFractionDigits);
  const paddedFraction = visibleFraction.padEnd(minimumFractionDigits, "0");
  const truncated = parsed.fraction.length > maximumFractionDigits;
  const integer = options.useGrouping === false
    ? parsed.integer
    : groupedInteger(parsed.integer);
  return `${parsed.sign}${integer}${paddedFraction ? `.${paddedFraction}` : ""}${
    truncated ? options.truncatedMarker ?? "…" : ""
  }`;
}

export function fractionDigitsFromIncrement(
  increment: string | null | undefined,
): number | null {
  if (!increment) return null;
  const parsed = decimalParts(increment);
  if (!parsed || parsed.sign === "-" || (parsed.integer === "0" && !parsed.fraction)) {
    return null;
  }
  return Math.min(
    MAX_TRADING_DECIMAL_FRACTION_DIGITS,
    parsed.fraction.length,
  );
}

export function tradingPrice(
  value: string | number,
  tickSize?: string | null,
): string {
  const precision = fractionDigitsFromIncrement(tickSize);
  const parsed = Number(value);
  if (precision !== null && Number.isFinite(parsed)) {
    return new Intl.NumberFormat("en-US", {
      minimumFractionDigits: precision,
      maximumFractionDigits: precision,
      useGrouping: true,
    }).format(parsed);
  }
  return compactDecimal(value, {
    maximumFractionDigits: precision ?? MAX_TRADING_DECIMAL_FRACTION_DIGITS,
    minimumFractionDigits: precision ?? 0,
    truncatedMarker: "",
  });
}

/**
 * Rounds a calculated reference price to the venue's visible price precision.
 * Use this only for derived estimates whose full floating-point tail is not an
 * exchange fact; exact order and persisted values continue to use tradingPrice.
 */
export function roundedTradingPriceEstimate(
  value: string | number,
  tickSize?: string | null,
): string {
  const precision = fractionDigitsFromIncrement(tickSize);
  const parsed = Number(value);
  if (precision === null || !Number.isFinite(parsed)) {
    return tradingPrice(value, tickSize);
  }
  return new Intl.NumberFormat("en-US", {
    minimumFractionDigits: precision,
    maximumFractionDigits: precision,
    useGrouping: true,
  }).format(parsed);
}

export function tradingQuantity(
  value: string | number,
  stepSize?: string | null,
): string {
  const precision = fractionDigitsFromIncrement(stepSize);
  return compactDecimal(value, {
    maximumFractionDigits: precision ?? MAX_TRADING_DECIMAL_FRACTION_DIGITS,
  });
}

export function quoteAmount(value: string | number): string {
  return compactDecimal(value, {
    maximumFractionDigits: MAX_TRADING_DECIMAL_FRACTION_DIGITS,
  });
}

export function quoteCurrencyAmount(value: string | number): string {
  return compactDecimal(value, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
    truncatedMarker: "",
  });
}

export function quoteCurrencyEstimate(value: string | number): string {
  return compactDecimal(value, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
    truncatedMarker: "",
  });
}

export function basisPoints(value: string | number): string {
  return compactDecimal(value, {
    maximumFractionDigits: 2,
    truncatedMarker: "",
  });
}

const userVisibleTimeFormatter = new Intl.DateTimeFormat(USER_VISIBLE_TIME_LOCALE, {
  timeZone: USER_VISIBLE_TIME_ZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

const compactUserVisibleTimeFormatter = new Intl.DateTimeFormat(USER_VISIBLE_TIME_LOCALE, {
  timeZone: USER_VISIBLE_TIME_ZONE,
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

export function formatUserVisibleTime(value: string | null | undefined): string {
  if (!value?.trim()) return "未知";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "未知";

  const parts = userVisibleTimeFormatter.formatToParts(parsed);
  const part = (type: Intl.DateTimeFormatPartTypes): string =>
    parts.find((item) => item.type === type)?.value ?? "";
  const timestamp = `${part("year")}-${part("month")}-${part("day")} ${part("hour")}:${part("minute")}:${part("second")}`;
  return USER_VISIBLE_TIME_ZONE_LABEL ? `${timestamp} ${USER_VISIBLE_TIME_ZONE_LABEL}` : timestamp;
}

export function formatCompactUserVisibleTime(value: string | null | undefined): string {
  if (!value?.trim()) return "未知";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "未知";

  const parts = compactUserVisibleTimeFormatter.formatToParts(parsed);
  const part = (type: Intl.DateTimeFormatPartTypes): string =>
    parts.find((item) => item.type === type)?.value ?? "";
  return `${part("month")}/${part("day")} ${part("hour")}:${part("minute")}`;
}

export function latestUtc(values: Array<string | null | undefined>): string | null {
  let latest: { value: string; time: number } | null = null;
  for (const value of values) {
    if (!value) continue;
    const time = Date.parse(value);
    if (!Number.isFinite(time)) continue;
    if (latest === null || time > latest.time) latest = { value, time };
  }
  return latest?.value ?? null;
}

export function shortDigest(value: string | null): string {
  if (!value) return "NOT BOUND";
  return value.length > 16 ? `${value.slice(0, 12)}…${value.slice(-4)}` : value;
}

export function marketPrice(value: string): string {
  return compactDecimal(value, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
    truncatedMarker: "",
  });
}

export function marketVolume(value: string): string {
  return tradingQuantity(value);
}

export function gapPercent(value: string): string {
  if (!value.trim()) return "UNKNOWN";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `${parsed.toFixed(3)}%` : "UNKNOWN";
}

export function pendingBreakoutNote(direction: "LONG" | "SHORT"): string {
  return direction === "LONG"
    ? "当前做多计划：本根尚未收于通道上沿之外"
    : "当前做空计划：本根尚未收于通道下沿之外";
}

export type VenueRejectionKind =
  | "POST_ONLY_RETRYABLE"
  | "PRICE_MATCH_RETRYABLE"
  | "FOK_NO_FILL"
  | "OTHER";

export function venueRejectionKind(value: string): VenueRejectionKind {
  const reason = value.trim();
  if (/-5022|could not be executed as maker|post[- ]only order will be rejected/i.test(reason)) {
    return "POST_ONLY_RETRYABLE";
  }
  if (/-5037|invalid price match/i.test(reason)) {
    return "PRICE_MATCH_RETRYABLE";
  }
  if (/-5021|could not be filled immediately.*\bFOK\b|\bFOK\b.*could not be filled immediately/i.test(reason)) {
    return "FOK_NO_FILL";
  }
  return "OTHER";
}

export function venueReasonText(value: string): string {
  const reason = value.trim();
  if (!reason) return "";
  if (venueRejectionKind(reason) === "FOK_NO_FILL") {
    return "FOK 订单未能立即全部成交，交易所已正常拒绝且未创建挂单";
  }
  if (venueRejectionKind(reason) === "POST_ONLY_RETRYABLE") {
    return "Maker only 订单会立即成交，交易所已正常拒绝且未创建挂单";
  }
  if (venueRejectionKind(reason) === "PRICE_MATCH_RETRYABLE") {
    return "盘口价指令当前被交易所拒绝，订单未创建且未成交";
  }
  const htmlStatus = reason.match(/<title>\s*(\d{3})\s*([^<]*)<\/title>/i);
  if (htmlStatus) {
    const code = htmlStatus[1] ?? "UNKNOWN";
    const label = (htmlStatus[2] ?? "").trim();
    const status = label ? ` ${label}` : "";
    return `Binance Demo 暂时不可用（HTTP ${code}${status}）；提交结果未确认，系统不会自动重复提交。`;
  }
  return reason.length > 240 ? `${reason.slice(0, 237)}…` : reason;
}

export function unknownExecutionReasonText(value: string): string {
  const reason = value.trim();
  if (reason.startsWith("VENUE_CALL_UNCERTAIN:")) {
    return "交易所异步提交结果尚未被权威事件确认";
  }
  if (reason === "VENUE_SUBMISSION_RESULT_UNKNOWN") {
    return "交易所未返回可判定的提交结果（常见于超时或连接中断）";
  }
  if (reason === "VENUE_CANCEL_RESULT_UNKNOWN") {
    return "交易所未返回可判定的撤单结果（常见于超时或连接中断）";
  }
  if (reason === "VENUE_RESULT_UNKNOWN") {
    return "交易所结果暂时无法判定";
  }
  return reason ? `交易所结果未决（${reason}）` : "交易所结果暂时无法判定";
}

const acceptedPlanEventLabels: Record<string, string> = {
  ENTRY_BREAKOUT: "入场意图已通过资金检查",
  DIRECT_ORDER_SCHEDULE_LEG: "直接执行入场档位已通过资金检查",
  PROTECTION_AFTER_FILL: "保护委托已通过资金检查",
  TAKE_PROFIT_1_AFTER_PROTECTION: "止盈一委托已通过资金检查",
  TAKE_PROFIT_2_AFTER_PROTECTION: "止盈二委托已通过资金检查",
  REDUCE_OR_CLOSE_POSITION: "减仓或平仓意图已通过资金检查",
  CANCEL_OPEN_RESPONSIBILITY: "撤单意图已通过资金检查",
};

const rejectedPlanEventLabels: Record<string, string> = {
  ENTRY_BREAKOUT: "入场意图未通过资金检查",
  DIRECT_ORDER_SCHEDULE_LEG: "直接执行入场档位未通过资金检查",
  PROTECTION_AFTER_FILL: "保护委托未通过资金检查",
  TAKE_PROFIT_1_AFTER_PROTECTION: "止盈一委托未通过资金检查",
  TAKE_PROFIT_2_AFTER_PROTECTION: "止盈二委托未通过资金检查",
  REDUCE_OR_CLOSE_POSITION: "减仓或平仓意图未通过资金检查",
  CANCEL_OPEN_RESPONSIBILITY: "撤单意图未通过资金检查",
};

export function planEventSummary(status: string, ruleId: string): string {
  if (status === "PROPOSED_ACTION_CAP_ACCEPTED") {
    return acceptedPlanEventLabels[ruleId] ?? "动作意图已通过资金检查";
  }
  if (status === "PROPOSED_ACTION_CAP_REJECTED") {
    return rejectedPlanEventLabels[ruleId] ?? "动作意图未通过资金检查";
  }
  if (status === "ENTRY_DEADLINE_EXPIRED") {
    return "入场截止时间已到，未创建交易动作";
  }
  if (status === "DEMO_ORDER_FLOW_CHECK") {
    return "Demo 下单流程验证";
  }
  return status;
}

export function closedBarBreakoutGapPercent(
  direction: "LONG" | "SHORT",
  latestClose: string,
  boundary: string,
): string {
  const close = Number(latestClose);
  const breakoutBoundary = Number(boundary);
  if (!Number.isFinite(close) || close <= 0 || !Number.isFinite(breakoutBoundary)) {
    return "";
  }
  const gap = direction === "LONG"
    ? (breakoutBoundary - close) / close * 100
    : (close - breakoutBoundary) / close * 100;
  return String(gap);
}

export function entryExtensionBoundary(
  direction: "LONG" | "SHORT",
  channelBoundary: string,
  atr: string,
  multiple: string,
): number | null {
  const boundary = Number(channelBoundary);
  const atrValue = Number(atr);
  const multipleValue = Number(multiple);
  if (
    !Number.isFinite(boundary)
    || !Number.isFinite(atrValue)
    || atrValue <= 0
    || !Number.isFinite(multipleValue)
    || multipleValue < 0
  ) {
    return null;
  }
  return direction === "LONG"
    ? boundary + atrValue * multipleValue
    : boundary - atrValue * multipleValue;
}

export type ImmediateExitEstimate = {
  exitPrice: number;
  exitCommission: number;
  netResult: number;
};

export function estimateMarkedNetResult(
  positionQuantity: number,
  fillCashFlow: number,
  attributedCommission: number,
  markPrice: number,
  attributedFunding: number,
): number | null {
  const values = [
    positionQuantity,
    fillCashFlow,
    attributedCommission,
    markPrice,
    attributedFunding,
  ];
  if (
    values.some((value) => !Number.isFinite(value))
    || positionQuantity === 0
    || attributedCommission < 0
    || markPrice <= 0
  ) {
    return null;
  }
  return fillCashFlow
    + positionQuantity * markPrice
    - attributedCommission
    + attributedFunding;
}

export function estimateImmediateExit(
  positionQuantity: number,
  fillCashFlow: number,
  attributedCommission: number,
  bidPrice: number,
  askPrice: number,
  takerFeeRate: number,
): ImmediateExitEstimate | null {
  const values = [
    positionQuantity,
    fillCashFlow,
    attributedCommission,
    bidPrice,
    askPrice,
    takerFeeRate,
  ];
  if (
    values.some((value) => !Number.isFinite(value))
    || positionQuantity === 0
    || attributedCommission < 0
    || bidPrice <= 0
    || askPrice <= 0
    || askPrice < bidPrice
    || takerFeeRate < 0
  ) {
    return null;
  }
  const exitPrice = positionQuantity > 0 ? bidPrice : askPrice;
  const exitCommission = Math.abs(positionQuantity) * exitPrice * takerFeeRate;
  return {
    exitPrice,
    exitCommission,
    netResult: fillCashFlow
      + positionQuantity * exitPrice
      - attributedCommission
      - exitCommission,
  };
}
