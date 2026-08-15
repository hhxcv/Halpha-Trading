import { describe, expect, it } from "vitest";

import {
  basisPoints,
  closedBarBreakoutGapPercent,
  compactDecimal,
  entryExtensionBoundary,
  estimateImmediateExit,
  estimateMarkedNetResult,
  formatCompactUserVisibleTime,
  formatUserVisibleTime,
  fractionDigitsFromIncrement,
  gapPercent,
  latestUtc,
  marketPrice,
  marketVolume,
  pendingBreakoutNote,
  planEventSummary,
  positiveFiniteNumber,
  quoteAmount,
  quoteCurrencyAmount,
  quoteCurrencyEstimate,
  roundedTradingPriceEstimate,
  shortDigest,
  subtractDecimal,
  scaleDecimalByPowerOfTen,
  tradingPrice,
  tradingQuantity,
  USER_VISIBLE_TIME_LOCALE,
  USER_VISIBLE_TIME_ZONE,
  USER_VISIBLE_TIME_ZONE_LABEL,
  unknownExecutionReasonText,
  venueRejectionKind,
  venueReasonText,
} from "./format";

describe("deterministic workbench formatting", () => {
  it("uses the global Chinese default display time zone without a redundant suffix", () => {
    expect(USER_VISIBLE_TIME_LOCALE).toBe("zh-CN");
    expect(USER_VISIBLE_TIME_ZONE).toBe("Asia/Shanghai");
    expect(USER_VISIBLE_TIME_ZONE_LABEL).toBe("");
  });

  it("keeps basis-point values decision-readable", () => {
    expect(basisPoints("0.0314589021")).toBe("0.03");
    expect(basisPoints("20.00000000")).toBe("20");
    expect(basisPoints("-12.3456")).toBe("-12.34");
  });

  it("converts Z timestamps to the user-visible default time zone", () => {
    expect(formatUserVisibleTime("2026-07-17T00:00:00Z"))
      .toBe("2026-07-17 08:00:00");
  });

  it("normalizes timestamp offsets before displaying the default time zone", () => {
    expect(formatUserVisibleTime("2026-07-17T12:34:56+09:00"))
      .toBe("2026-07-17 11:34:56");
    expect(formatUserVisibleTime("2026-07-16T19:30:00-05:00"))
      .toBe("2026-07-17 08:30:00");
  });

  it("formats compact plan-card times without repeating the global zone label", () => {
    expect(formatCompactUserVisibleTime("2026-07-17T00:30:00Z"))
      .toBe("07/17 08:30");
    expect(formatCompactUserVisibleTime("not-a-date")).toBe("未知");
  });

  it("keeps missing, unknown, and invalid timestamps explicit", () => {
    expect(formatUserVisibleTime(null)).toBe("未知");
    expect(formatUserVisibleTime(undefined)).toBe("未知");
    expect(formatUserVisibleTime("")).toBe("未知");
    expect(formatUserVisibleTime("UNKNOWN")).toBe("未知");
    expect(formatUserVisibleTime("not-a-date")).toBe("未知");
  });

  it("shortens digests without implying equality", () => {
    expect(shortDigest("0123456789abcdef0123456789abcdef")).toBe("0123456789ab…cdef");
    expect(shortDigest(null)).toBe("NOT BOUND");
  });

  it("uses the latest valid venue fact cutoff", () => {
    expect(latestUtc([
      null,
      "UNKNOWN",
      "2026-07-20T11:05:01Z",
      "2026-07-20T11:16:28Z",
    ])).toBe("2026-07-20T11:16:28Z");
    expect(latestUtc([null, "UNKNOWN"])).toBeNull();
  });

  it("把 HTML 场所错误压缩为可操作提示", () => {
    expect(venueReasonText("<html><head><title>502 Bad Gateway</title></head></html>"))
      .toBe("Binance Demo 暂时不可用（HTTP 502 Bad Gateway）；提交结果未确认，系统不会自动重复提交。");
    expect(venueReasonText("MIN_NOTIONAL")).toBe("MIN_NOTIONAL");
  });

  it("区分可重试的 Maker、盘口价与 FOK 零成交拒绝", () => {
    const fokReason = "{'code': -5021, 'msg': 'Due to the order could not be filled immediately, the FOK order has been rejected.'}";
    const postOnlyReason = "{'code': -5022, 'msg': 'Due to the order could not be executed as maker.'}";
    const priceMatchReason = "{'code': -5037, 'msg': 'Invalid price match'}";
    expect(venueRejectionKind(fokReason)).toBe("FOK_NO_FILL");
    expect(venueReasonText(fokReason)).toContain("正常拒绝");
    expect(venueRejectionKind(postOnlyReason)).toBe("POST_ONLY_RETRYABLE");
    expect(venueReasonText(postOnlyReason)).toContain("正常拒绝");
    expect(venueRejectionKind(priceMatchReason)).toBe("PRICE_MATCH_RETRYABLE");
    expect(venueReasonText(priceMatchReason)).toContain("未创建且未成交");
    expect(venueRejectionKind("MIN_NOTIONAL")).toBe("OTHER");
  });

  it("把未决动作原因翻译为不误判成交结果的提示", () => {
    expect(unknownExecutionReasonText("VENUE_CALL_UNCERTAIN:VenueSubmissionUncertain"))
      .toContain("异步提交结果尚未被权威事件确认");
    expect(unknownExecutionReasonText("VENUE_SUBMISSION_RESULT_UNKNOWN"))
      .toContain("未返回可判定的提交结果");
    expect(unknownExecutionReasonText("VENUE_CANCEL_RESULT_UNKNOWN"))
      .toContain("未返回可判定的撤单结果");
  });

  it("按计划规则显示实际动作含义，而不是把所有事件都写成入场", () => {
    expect(planEventSummary("PROPOSED_ACTION_CAP_ACCEPTED", "ENTRY_BREAKOUT"))
      .toBe("入场意图已通过资金检查");
    expect(planEventSummary("PROPOSED_ACTION_CAP_ACCEPTED", "DIRECT_ORDER_SCHEDULE_LEG"))
      .toBe("直接执行入场档位已通过资金检查");
    expect(planEventSummary("PROPOSED_ACTION_CAP_ACCEPTED", "PROTECTION_AFTER_FILL"))
      .toBe("保护委托已通过资金检查");
    expect(planEventSummary("PROPOSED_ACTION_CAP_ACCEPTED", "TAKE_PROFIT_1_AFTER_PROTECTION"))
      .toBe("止盈一委托已通过资金检查");
    expect(planEventSummary("PROPOSED_ACTION_CAP_ACCEPTED", "REDUCE_OR_CLOSE_POSITION"))
      .toBe("减仓或平仓意图已通过资金检查");
    expect(planEventSummary("PROPOSED_ACTION_CAP_ACCEPTED", "CANCEL_OPEN_RESPONSIBILITY"))
      .toBe("撤单意图已通过资金检查");
  });

  it("keeps missing percentages unknown", () => {
    expect(gapPercent("")).toBe("UNKNOWN");
    expect(gapPercent("not-a-number")).toBe("UNKNOWN");
  });

  it("keeps small base-asset volume visible", () => {
    expect(marketVolume("0.00012345")).toBe("0.00012345");
  });

  it("bounds decimals without rounding or changing the underlying value", () => {
    const exact = "64122.83236908222222222222222222222222222222";
    expect(compactDecimal(exact)).toBe("64,122.83236908…");
    expect(compactDecimal("001234.50000000")).toBe("1,234.5");
    expect(compactDecimal("-0.000000001")).toBe("-0.00000000…");
    expect(compactDecimal("1e-8")).toBe("0.00000001");
    expect(exact).toBe("64122.83236908222222222222222222222222222222");
  });

  it("derives display precision from venue tick and step increments", () => {
    expect(fractionDigitsFromIncrement("0.1")).toBe(1);
    expect(fractionDigitsFromIncrement("0.0100")).toBe(2);
    expect(fractionDigitsFromIncrement("1")).toBe(0);
    expect(fractionDigitsFromIncrement("0.00000001")).toBe(8);
    expect(fractionDigitsFromIncrement("0")).toBeNull();
  });

  it("uses venue precision for prices and quantities with bounded fallbacks", () => {
    expect(tradingPrice("64122.832369082222", "0.1")).toBe("64,122.8");
    expect(tradingPrice("64122.8", "0.1")).toBe("64,122.8");
    expect(tradingPrice("12", "0.01")).toBe("12.00");
    expect(tradingQuantity("0.123456789", "0.001")).toBe("0.123…");
    expect(quoteAmount("96.1842000000000001")).toBe("96.18420000…");
  });

  it("rounds calculated reference prices to venue precision without an ellipsis", () => {
    expect(roundedTradingPriceEstimate("63251.807431", "0.1")).toBe("63,251.8");
    expect(roundedTradingPriceEstimate("63251.85", "0.1")).toBe("63,251.9");
    expect(roundedTradingPriceEstimate("12.345", "0.01")).toBe("12.35");
  });

  it("keeps user-facing quote-currency budgets at monetary precision", () => {
    expect(quoteCurrencyAmount("4993.95234")).toBe("4,993.95");
    expect(quoteCurrencyAmount("5000")).toBe("5,000.00");
  });

  it("keeps quote-currency estimates at two decimal places", () => {
    expect(quoteCurrencyEstimate("0.00119995")).toBe("0.00");
    expect(quoteCurrencyEstimate("0.945597")).toBe("0.94");
    expect(quoteCurrencyEstimate("1.891194444")).toBe("1.89");
  });

  it("keeps market fallback displays bounded while preserving useful precision", () => {
    expect(marketPrice("64178.5")).toBe("64,178.50");
    expect(marketPrice("0.123456789")).toBe("0.12");
  });

  it("subtracts decimal market facts without binary floating-point artifacts", () => {
    expect(subtractDecimal("64150.10", "64150.00")).toBe("0.1");
    expect(subtractDecimal("0.00000002", "0.00000001")).toBe("0.00000001");
    expect(subtractDecimal("-1.5", "0.25")).toBe("-1.75");
    expect(subtractDecimal("UNKNOWN", "1")).toBeNull();
  });

  it("moves the decimal point exactly for percentage editing", () => {
    expect(scaleDecimalByPowerOfTen("33.3", -2)).toBe("0.333");
    expect(scaleDecimalByPowerOfTen("0.125", 2)).toBe("12.5");
    expect(scaleDecimalByPowerOfTen("", -2)).toBeNull();
  });

  it("measures the strategy trigger from the latest closed bar", () => {
    expect(Number(closedBarBreakoutGapPercent("LONG", "100", "101"))).toBeCloseTo(1);
    expect(Number(closedBarBreakoutGapPercent("LONG", "102", "101"))).toBeLessThan(0);
    expect(Number(closedBarBreakoutGapPercent("SHORT", "100", "99"))).toBeCloseTo(1);
    expect(Number(closedBarBreakoutGapPercent("SHORT", "98", "99"))).toBeLessThan(0);
    expect(closedBarBreakoutGapPercent("LONG", "", "101")).toBe("");
  });

  it("明确等待突破提示只针对当前计划方向", () => {
    expect(pendingBreakoutNote("LONG")).toContain("通道上沿");
    expect(pendingBreakoutNote("SHORT")).toContain("通道下沿");
  });

  it("calculates the farthest strategy entry price without adding runtime state", () => {
    expect(entryExtensionBoundary("LONG", "100", "2", "0.5")).toBe(101);
    expect(entryExtensionBoundary("SHORT", "100", "2", "0.5")).toBe(99);
    expect(entryExtensionBoundary("LONG", "100", "0", "0.5")).toBeNull();
    expect(entryExtensionBoundary("SHORT", "100", "2", "invalid")).toBeNull();
  });

  it("estimates an immediate long exit from bid after the projected taker fee", () => {
    const estimate = estimateImmediateExit(0.01, -1000, 0.4, 100_100, 100_110, 0.0004);
    expect(estimate?.exitPrice).toBe(100_100);
    expect(estimate?.exitCommission).toBeCloseTo(0.4004);
    expect(estimate?.netResult).toBeCloseTo(0.1996);
  });

  it("estimates an immediate short exit from ask after the projected taker fee", () => {
    const estimate = estimateImmediateExit(-0.01, 1000, 0.4, 99_890, 99_900, 0.0004);
    expect(estimate?.exitPrice).toBe(99_900);
    expect(estimate?.exitCommission).toBeCloseTo(0.3996);
    expect(estimate?.netResult).toBeCloseTo(0.2004);
  });

  it("marks a partially exited long plan including realized and unrealized result", () => {
    expect(estimateMarkedNetResult(1, -90, 3, 105, -1)).toBe(11);
  });

  it("marks a partially exited short plan including realized and unrealized result", () => {
    expect(estimateMarkedNetResult(-1, 110, 3, 95, -1)).toBe(11);
  });

  it("does not turn a missing market price into zero", () => {
    expect(positiveFiniteNumber("")).toBeNull();
    expect(positiveFiniteNumber("   ")).toBeNull();
    expect(positiveFiniteNumber(null)).toBeNull();
    expect(positiveFiniteNumber("0")).toBeNull();
    expect(positiveFiniteNumber("-1")).toBeNull();
    expect(positiveFiniteNumber("64270.9")).toBe(64270.9);
  });

  it("does not invent a marked plan result from incomplete inputs", () => {
    expect(estimateMarkedNetResult(0, 0, 0, 100, 0)).toBeNull();
    expect(estimateMarkedNetResult(1, -100, -1, 101, 0)).toBeNull();
    expect(estimateMarkedNetResult(1, -100, 0, Number.NaN, 0)).toBeNull();
  });

  it("does not invent an immediate-exit estimate from incomplete inputs", () => {
    expect(estimateImmediateExit(0, 0, 0, 100, 101, 0.0004)).toBeNull();
    expect(estimateImmediateExit(1, -100, 0, 101, 100, 0.0004)).toBeNull();
    expect(estimateImmediateExit(1, -100, 0, 100, 101, Number.NaN)).toBeNull();
  });

});
