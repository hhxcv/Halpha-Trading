export type ReviewPerformanceTrade = {
  netPnl: number;
  commission: number;
  entryNotional?: number | null;
  classification?: string;
  profitSeekingEligible?: boolean;
};

export const LOSS_STREAK_ALERT_THRESHOLD = 3;

export type ReviewStreakKind = "WIN" | "LOSS" | "NONE";

const STRATEGY_PERFORMANCE_CLASSIFICATIONS = new Set([
  "USABLE_SAMPLE",
  "TRADE_DECISION_ISSUE",
  "AS_EXPECTED",
]);

export function isStrategyPerformanceClassification(
  classification: string | null | undefined,
): boolean {
  return STRATEGY_PERFORMANCE_CLASSIFICATIONS.has(classification ?? "");
}

export type ReviewPerformanceSummary = {
  tradeCount: number;
  netPnl: number;
  commissions: number;
  wins: number;
  grossProfit: number;
  grossLoss: number;
  averageNetPnl: number | null;
  totalEntryNotional: number | null;
  notionalReturnPercent: number | null;
  entryNotionalTradeCount: number;
  maximumDrawdown: number;
  currentStreakKind: ReviewStreakKind;
  currentStreakCount: number;
};

export type AccountAndProfitSeekingPerformanceSummary = {
  account: ReviewPerformanceSummary;
  profitSeeking: ReviewPerformanceSummary;
};

export function summarizeReviewPerformance(
  tradesInClosingOrder: ReviewPerformanceTrade[],
): ReviewPerformanceSummary {
  const trades = tradesInClosingOrder.filter((trade) => (
    Number.isFinite(trade.netPnl)
    && Number.isFinite(trade.commission)
    && trade.commission >= 0
  ));
  let cumulative = 0;
  let peak = 0;
  let maximumDrawdown = 0;
  let netPnl = 0;
  let commissions = 0;
  let wins = 0;
  let grossProfit = 0;
  let grossLoss = 0;
  let totalEntryNotional = 0;
  let entryNotionalTradeCount = 0;

  trades.forEach((trade) => {
    netPnl += trade.netPnl;
    commissions += trade.commission;
    wins += trade.netPnl > 0 ? 1 : 0;
    grossProfit += Math.max(0, trade.netPnl);
    grossLoss += Math.max(0, -trade.netPnl);
    if (
      Number.isFinite(trade.entryNotional)
      && (trade.entryNotional as number) > 0
    ) {
      totalEntryNotional += trade.entryNotional as number;
      entryNotionalTradeCount += 1;
    }
    cumulative += trade.netPnl;
    peak = Math.max(peak, cumulative);
    maximumDrawdown = Math.max(maximumDrawdown, peak - cumulative);
  });

  let currentStreakKind: ReviewStreakKind = "NONE";
  let currentStreakCount = 0;
  for (let index = trades.length - 1; index >= 0; index -= 1) {
    const result = trades[index]?.netPnl ?? 0;
    const resultKind: ReviewStreakKind = result > 0
      ? "WIN"
      : result < 0
        ? "LOSS"
        : "NONE";
    if (resultKind === "NONE") break;
    if (currentStreakKind === "NONE") currentStreakKind = resultKind;
    if (resultKind !== currentStreakKind) break;
    currentStreakCount += 1;
  }

  return {
    tradeCount: trades.length,
    netPnl,
    commissions,
    wins,
    grossProfit,
    grossLoss,
    averageNetPnl: trades.length > 0 ? netPnl / trades.length : null,
    totalEntryNotional: (
      trades.length > 0
      && entryNotionalTradeCount === trades.length
      && totalEntryNotional > 0
    ) ? totalEntryNotional : null,
    notionalReturnPercent: (
      trades.length > 0
      && entryNotionalTradeCount === trades.length
      && totalEntryNotional > 0
    ) ? netPnl / totalEntryNotional * 100 : null,
    entryNotionalTradeCount,
    maximumDrawdown,
    currentStreakKind,
    currentStreakCount,
  };
}

export function summarizeAccountAndProfitSeekingPerformance(
  tradesInClosingOrder: ReviewPerformanceTrade[],
): AccountAndProfitSeekingPerformanceSummary {
  return {
    account: summarizeReviewPerformance(tradesInClosingOrder),
    profitSeeking: summarizeReviewPerformance(
      tradesInClosingOrder.filter((trade) => (
        trade.profitSeekingEligible === true
        && isStrategyPerformanceClassification(trade.classification)
      )),
    ),
  };
}
