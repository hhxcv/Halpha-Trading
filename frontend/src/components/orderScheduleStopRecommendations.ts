import type {
  MarketContext,
  OrderScheduleDirection,
  OrderSchedulePreviewLeg,
} from "../api/client";

const MAX_INITIAL_STOP_BPS = 10_000;
const ENTRY_ATR_MULTIPLE = 1.5;
const MAX_RECOMMENDATIONS = 3;

export type InitialStopRecommendationKind =
  | MarketContext["stop_references"][number]["kind"]
  | "ENTRY_ATR";

export type InitialStopRecommendation = {
  id: string;
  kind: InitialStopRecommendationKind;
  label: string;
  price: number;
  distanceBps: number;
  distanceBpsInput: string;
  entryBasis: number;
  evidence: string;
  evidenceCutoff: string;
};

function finitePositive(value: string | number | null | undefined): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function compactNumber(value: number, digits = 4): string {
  return value.toFixed(digits).replace(/\.?0+$/, "");
}

export function projectedEntryBasis(
  previewLegs: ReadonlyArray<OrderSchedulePreviewLeg>,
): number | null {
  let quantityTotal = 0;
  let notionalTotal = 0;
  const fallbackPrices: number[] = [];
  previewLegs.forEach((leg) => {
    const price = finitePositive(leg.price) ?? finitePositive(leg.sizing_price);
    if (price === null) return;
    fallbackPrices.push(price);
    const quantity = finitePositive(leg.quantity);
    if (quantity === null) return;
    quantityTotal += quantity;
    notionalTotal += price * quantity;
  });
  if (quantityTotal > 0 && Number.isFinite(notionalTotal)) {
    return notionalTotal / quantityTotal;
  }
  return fallbackPrices.length > 0
    ? fallbackPrices.reduce((total, price) => total + price, 0) / fallbackPrices.length
    : null;
}

function volumeBiasText(value: "POSITIVE" | "NEGATIVE" | "NEUTRAL" | null | undefined) {
  if (value === "POSITIVE") return "OBV 偏正";
  if (value === "NEGATIVE") return "OBV 偏负";
  return "OBV 中性";
}

function referencePresentation(
  reference: MarketContext["stop_references"][number],
): { label: string; evidence: string; priority: number } {
  const atrBuffer = compactNumber(Number(reference.atr_buffer_multiple), 2);
  const interval = reference.interval;
  if (reference.kind === "SWING_OBV") {
    return {
      label: "量价摆动位",
      evidence: `${reference.lookback_bars} 根 ${interval} 摆动结构，外扩 ${atrBuffer} ATR；${volumeBiasText(reference.volume_bias)}`,
      priority: 0,
    };
  }
  if (reference.kind === "STRUCTURE_ATR") {
    return {
      label: "近期结构位",
      evidence: `${reference.lookback_bars} 根 ${interval} Donchian 边界，外扩 ${atrBuffer} ATR`,
      priority: 1,
    };
  }
  const slope = Number(reference.trend_slope);
  const rSquared = Number(reference.trend_r_squared);
  const trendDetail = Number.isFinite(slope) && Number.isFinite(rSquared)
    ? `；斜率 ${slope >= 0 ? "+" : ""}${compactNumber(slope)}，R² ${compactNumber(rSquared, 2)}`
    : "";
  return {
    label: "趋势波动带",
    evidence: `${reference.lookback_bars} 根 ${interval} 线性回归，外扩 ${atrBuffer} ATR${trendDetail}`,
    priority: 2,
  };
}

export function buildInitialStopRecommendations(input: {
  direction: OrderScheduleDirection;
  market: MarketContext | null | undefined;
  previewLegs: ReadonlyArray<OrderSchedulePreviewLeg>;
}): InitialStopRecommendation[] {
  const entryBasis = projectedEntryBasis(input.previewLegs);
  const market = input.market;
  if (entryBasis === null || !market) return [];
  const entryPrices = input.previewLegs
    .map((leg) => finitePositive(leg.price) ?? finitePositive(leg.sizing_price))
    .filter((price): price is number => price !== null);
  if (entryPrices.length === 0) return [];
  const entryBoundary = input.direction === "LONG"
    ? Math.min(...entryPrices)
    : Math.max(...entryPrices);
  const outsideAllEntries = (price: number) => input.direction === "LONG"
    ? price < entryBoundary
    : price > entryBoundary;
  const expectedSide = input.direction === "LONG" ? "LOWER" : "UPPER";
  const candidates: Array<InitialStopRecommendation & { priority: number }> =
    market.stop_references.flatMap((reference) => {
    if (reference.side !== expectedSide) return [];
    const price = finitePositive(reference.price);
    if (price === null) return [];
    if (!outsideAllEntries(price)) return [];
    const adverse = input.direction === "LONG"
      ? price < entryBasis
      : price > entryBasis;
    if (!adverse) return [];
    const distanceBps = Math.abs(price - entryBasis) / entryBasis * 10_000;
    if (
      !Number.isFinite(distanceBps)
      || distanceBps <= 0
      || distanceBps >= MAX_INITIAL_STOP_BPS
    ) return [];
    const presentation = referencePresentation(reference);
    return [{
      id: `market-${reference.kind.toLowerCase()}`,
      kind: reference.kind,
      label: presentation.label,
      price,
      distanceBps,
      distanceBpsInput: compactNumber(distanceBps),
      entryBasis,
      evidence: presentation.evidence,
      evidenceCutoff: market.latest_closed_stop_reference_at,
      priority: presentation.priority,
    }];
  });

  const atr = finitePositive(market.stop_reference_atr_14);
  if (atr !== null) {
    const price = input.direction === "LONG"
      ? entryBasis - atr * ENTRY_ATR_MULTIPLE
      : entryBasis + atr * ENTRY_ATR_MULTIPLE;
    const distanceBps = Math.abs(price - entryBasis) / entryBasis * 10_000;
    if (
      price > 0
      && distanceBps < MAX_INITIAL_STOP_BPS
      && outsideAllEntries(price)
    ) {
      candidates.push({
        id: "market-entry-atr",
        kind: "ENTRY_ATR",
        label: "ATR 波动缓冲",
        price,
        distanceBps,
        distanceBpsInput: compactNumber(distanceBps),
        entryBasis,
        evidence: `预计加权入场价外侧 ${ENTRY_ATR_MULTIPLE} ATR（${market.stop_reference_interval} ATR ${compactNumber(atr)} USDT）`,
        evidenceCutoff: market.latest_closed_stop_reference_at,
        priority: 3,
      });
    }
  }

  const distinct: Array<InitialStopRecommendation & { priority: number }> = [];
  candidates
    .sort((left, right) => left.priority - right.priority)
    .forEach((candidate) => {
      const duplicatesExisting = distinct.some((existing) => (
        Math.abs(existing.distanceBps - candidate.distanceBps) < 1
      ));
      if (!duplicatesExisting) distinct.push(candidate);
    });
  return distinct
    .slice(0, MAX_RECOMMENDATIONS)
    .sort((left, right) => (
      left.distanceBps - right.distanceBps
      || left.priority - right.priority
    ))
    .map(({ priority: _priority, ...item }) => item);
}
