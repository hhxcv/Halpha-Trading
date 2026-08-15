import { describe, expect, it } from "vitest";

import type { OrderScheduleSpec } from "../api/client";
import {
  buildOrderScheduleChartAnnotations,
  chartPeriod,
  chartPriceInput,
  expandedVisiblePriceRange,
  groupNearbyPriceAnnotations,
  marketIntervalForPeriod,
  marketWindowBounds,
  orderedPriceRange,
  priceAnnotationTagMultiplicity,
  priceTagAxisLayout,
  projectOrderScheduleProtectionPrices,
  selectPriceAnnotationForTag,
  shouldBlockChartSurface,
  spreadChartLabelAnchors,
  summarizeRelativeRules,
  type OrderChartPriceAnnotation,
} from "./orderScheduleChartModel";

function defaultSpec(limitPrice = "65750"): OrderScheduleSpec {
  return {
    entry_program: {
      kind: "ONE_TIME",
      slice_count: 1,
      first_slice_delay_seconds: 0,
      slice_interval_seconds: 0,
    },
    price_distribution: { kind: "SINGLE", limit_price: limitPrice },
    amount_distribution: {
      mode: "FIXED",
      direction: "LOW_TO_HIGH",
      base_notional: "10",
      linear_step: "10",
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
    submission_order: "HIGH_TO_LOW",
    entry_conditions: {
      operator: "ALL",
      items: [{ kind: "DECISION_BASIS_READY" }],
    },
    protection_policy: {
      full_fill_loss_budget: null,
      initial_stop: {
        distance_bps: "100",
        trigger_source: "MARK_PRICE",
        coverage: "EACH_CONFIRMED_FILL",
      },
      take_profit_ladder: null,
      time_exit_seconds: null,
    },
    dynamic_rules: [],
  };
}

describe("order schedule chart model", () => {
  it("places custom price tags at the outer axis edge and connects from the plot", () => {
    expect(priceTagAxisLayout("right", 120)).toEqual({
      plotEdgeX: 0,
      elbowX: 8,
      labelLeadX: 106,
      labelX: 120,
      labelAlign: "right",
    });
    expect(priceTagAxisLayout("left", 120)).toEqual({
      plotEdgeX: 120,
      elbowX: 112,
      labelLeadX: 14,
      labelX: 0,
      labelAlign: "left",
    });
  });

  it("keeps price-tag connector geometry inside a narrow axis", () => {
    expect(priceTagAxisLayout("right", 6)).toEqual({
      plotEdgeX: 0,
      elbowX: 6,
      labelLeadX: 0,
      labelX: 6,
      labelAlign: "right",
    });
    expect(priceTagAxisLayout("left", 6)).toEqual({
      plotEdgeX: 6,
      elbowX: 0,
      labelLeadX: 6,
      labelX: 0,
      labelAlign: "left",
    });
  });

  it("separates nearby price labels while preserving their vertical order", () => {
    const spread = spreadChartLabelAnchors([
      { id: "reference", y: 100 },
      { id: "position", y: 105 },
      { id: "stop", y: 108 },
    ], 220);

    expect(spread.map(({ id }) => id)).toEqual([
      "reference",
      "position",
      "stop",
    ]);
    expect(spread[1]!.y - spread[0]!.y).toBeCloseTo(22);
    expect(spread[2]!.y - spread[1]!.y).toBeCloseTo(22);
    expect(
      spread.reduce((sum, anchor) => sum + anchor.y, 0) / spread.length,
    ).toBeCloseTo((100 + 105 + 108) / 3);
  });

  it("keeps price labels inside the pane and reduces the gap only when required", () => {
    expect(spreadChartLabelAnchors([
      { id: "a", y: -20 },
      { id: "b", y: 1 },
      { id: "c", y: 5 },
    ], 40)).toEqual([
      { id: "a", y: 11 },
      { id: "b", y: 20 },
      { id: "c", y: 29 },
    ]);
  });

  it("preserves price order when several annotations project below the pane", () => {
    const spread = spreadChartLabelAnchors([
      { id: "a-lower-price", y: 260 },
      { id: "z-higher-price", y: 240 },
    ], 220);

    expect(spread.map(({ id }) => id)).toEqual([
      "z-higher-price",
      "a-lower-price",
    ]);
    expect(spread[0]!.y).toBeLessThan(spread[1]!.y);
  });

  it("orders the same interval for upward and downward drags", () => {
    expect(orderedPriceRange(10, 30)).toEqual([10, 30]);
    expect(orderedPriceRange(30, 10)).toEqual([10, 30]);
  });

  it("requests only closed bars and preserves the inclusive count", () => {
    const range = marketWindowBounds("2026-07-23T08:14:59.999Z", "15m", 160);
    expect(range).toEqual({
      startAt: "2026-07-21T16:00:00.000Z",
      endAt: "2026-07-23T07:45:00.000Z",
    });
    const intervalMilliseconds = 15 * 60_000;
    expect(
      (Date.parse(range!.endAt) - Date.parse(range!.startAt))
        / intervalMilliseconds
        + 1,
    ).toBe(160);

    expect(marketWindowBounds("2026-07-23T08:00:00.000Z", "1h", 3)).toEqual({
      startAt: "2026-07-23T05:00:00.000Z",
      endAt: "2026-07-23T07:00:00.000Z",
    });
    expect(marketWindowBounds("2026-07-23T08:00:00.000Z", "1d", 3)).toEqual({
      startAt: "2026-07-20T00:00:00.000Z",
      endAt: "2026-07-22T00:00:00.000Z",
    });
  });

  it("rejects invalid market cutoffs and unusable window sizes", () => {
    expect(marketWindowBounds("not-a-time", "15m")).toBeNull();
    expect(marketWindowBounds("2026-07-23T08:00:00.000Z", "15m", 1)).toBeNull();
  });

  it("keeps loaded history visible while realtime bars are still synchronizing", () => {
    expect(shouldBlockChartSurface("RUNTIME", true, false)).toBe(false);
    expect(shouldBlockChartSurface("RUNTIME", false, false)).toBe(true);
    expect(shouldBlockChartSurface("DRAFT", true, false)).toBe(false);
    expect(shouldBlockChartSurface("DRAFT", true, true)).toBe(false);
  });

  it("maps every supported timeframe to and from KLineCharts periods", () => {
    expect(chartPeriod("1m")).toEqual({ type: "minute", span: 1 });
    expect(chartPeriod("5m")).toEqual({ type: "minute", span: 5 });
    expect(chartPeriod("15m")).toEqual({ type: "minute", span: 15 });
    expect(chartPeriod("1h")).toEqual({ type: "hour", span: 1 });
    expect(chartPeriod("4h")).toEqual({ type: "hour", span: 4 });
    expect(chartPeriod("1d")).toEqual({ type: "day", span: 1 });
    expect(marketIntervalForPeriod({ type: "hour", span: 4 })).toBe("4h");
    expect(marketIntervalForPeriod({ type: "week", span: 1 })).toBeNull();
  });

  it("writes stable decimal strings without exponential notation", () => {
    expect(chartPriceInput(10)).toBe("10");
    expect(chartPriceInput(10.125)).toBe("10.125");
    expect(chartPriceInput(0.00000001)).toBe("0.00000001");
    expect(chartPriceInput(64122.832369, "0.1")).toBe("64122.8");
  });

  it("keeps candle-only scale unless valid annotations are explicitly included", () => {
    expect(
      expandedVisiblePriceRange(
        65_560,
        66_300,
        [61_000, 70_000],
        0.4,
        false,
      ),
    ).toEqual([65_560, 66_300]);
    expect(
      expandedVisiblePriceRange(
        65_560,
        66_300,
        [65_000, 65_250, 66_000],
        0.4,
        true,
      ),
    ).toEqual([64_948, 66_352]);
    expect(
      expandedVisiblePriceRange(
        65_000,
        66_000,
        [65_250, 65_500, 65_750],
        0.4,
        true,
      ),
    ).toEqual([65_000, 66_000]);
    expect(
      expandedVisiblePriceRange(
        65_000,
        66_000,
        [Number.NaN, -1],
        0.4,
        true,
      ),
    ).toEqual([65_000, 66_000]);
  });

  it("groups nearby labels without transitively collapsing a price ladder", () => {
    const annotations = Array.from({ length: 10 }, (_, index) => ({
      id: `leg-${index}`,
      role: "NORMALIZED_LEG" as const,
      label: `leg ${index + 1}`,
      detail: "",
      price: 100 + index * 10,
      authority: "SERVER_PREVIEW" as const,
      lineStyle: "dashed" as const,
      draggable: false,
    }));

    const groups = groupNearbyPriceAnnotations(annotations, 25);

    expect(groups.map((group) => group.map((item) => item.price))).toEqual([
      [100, 110, 120],
      [130, 140, 150],
      [160, 170, 180],
      [190],
    ]);
    expect(groups.flat()).toHaveLength(annotations.length);
  });

  it("keeps nearby labels with different trading meanings separate", () => {
    const annotations: OrderChartPriceAnnotation[] = [
      {
        id: "reference",
        role: "REFERENCE",
        label: "reference",
        detail: "",
        price: 100,
        authority: "MARKET",
        lineStyle: "dotted",
        draggable: false,
      },
      {
        id: "trigger",
        role: "MARK_CONDITION",
        label: "trigger",
        detail: "",
        price: 110,
        authority: "SERVER_PREVIEW",
        lineStyle: "dashed",
        draggable: false,
      },
      {
        id: "invalidation",
        role: "ENTRY_INVALIDATION",
        label: "invalidation",
        detail: "",
        price: 111,
        authority: "SERVER_PREVIEW",
        lineStyle: "dashed",
        draggable: false,
      },
    ];

    expect(groupNearbyPriceAnnotations(annotations, 25)).toEqual([
      [annotations[0]],
      [annotations[1]],
      [annotations[2]],
    ]);
  });

  it("uses the runtime fact instead of counting duplicate projections as orders", () => {
    const group: OrderChartPriceAnnotation[] = [
      {
        id: "draft",
        role: "SINGLE_LIMIT",
        label: "输入限价",
        detail: "",
        price: 100,
        authority: "DRAFT_INPUT",
        lineStyle: "solid",
        draggable: false,
      },
      {
        id: "preview",
        role: "NORMALIZED_LEG",
        label: "标准化入场 1/1",
        detail: "",
        price: 100,
        authority: "SERVER_PREVIEW",
        lineStyle: "dashed",
        draggable: false,
      },
      {
        id: "runtime",
        role: "RUNTIME_ENTRY",
        label: "入场动作",
        detail: "",
        price: 100,
        authority: "SERVER_FACT",
        lineStyle: "solid",
        draggable: false,
      },
    ];

    const primary = selectPriceAnnotationForTag(group);

    expect(primary?.id).toBe("runtime");
    expect(primary && priceAnnotationTagMultiplicity(group, primary)).toBe(1);
  });

  it("keeps a count for multiple facts with the same trading meaning", () => {
    const group: OrderChartPriceAnnotation[] = [100, 100.01].map(
      (price, index) => ({
        id: `runtime-${index}`,
        role: "RUNTIME_ENTRY",
        label: "入场动作",
        detail: "",
        price,
        authority: "SERVER_FACT",
        lineStyle: "solid",
        draggable: false,
      }),
    );
    const primary = selectPriceAnnotationForTag(group);

    expect(primary && priceAnnotationTagMultiplicity(group, primary)).toBe(2);
  });

  it("keeps the default single limit, reference price and server leg as distinct annotations", () => {
    const spec = defaultSpec();
    const annotations = buildOrderScheduleChartAnnotations({
      direction: "LONG",
      referencePrice: "65755.5",
      spec,
      previewState: "READY",
      priceTickSize: "0.1",
      previewLegs: [{
        leg_index: 0,
        leg_count: 1,
        release_after_seconds: 0,
        raw_price: "65750.03236908222222222222222222222222222222",
        price: "65750.0",
        sizing_price: "65750.0",
        requested_notional: "10",
        quantity: "0.0001",
        effective_notional: "6.575",
      }],
    });

    expect(annotations.priceAnnotations.map((item) => item.role)).toEqual([
      "REFERENCE",
      "SINGLE_LIMIT",
      "NORMALIZED_LEG",
      "PROTECTION",
    ]);
    expect(annotations.priceAnnotations[1]).toMatchObject({
      label: "输入限价",
      authority: "DRAFT_INPUT",
      draggable: true,
      price: 65750,
    });
    expect(annotations.priceAnnotations[2]?.detail).toContain("有效 6.575 USDT");
    expect(annotations.priceAnnotations[2]?.detail).toContain("归一化前 65,750.0");
    expect(annotations.priceAnnotations[2]?.detail).not.toContain("032369082222");
    expect(annotations.priceAnnotations[3]).toMatchObject({
      label: "预计止损触发价",
      authority: "SERVER_PREVIEW",
      lineStyle: "dotted",
    });
  });

  it("projects one aggregate stop and take-profit set from the weighted entry price", () => {
    const spec = defaultSpec();
    spec.protection_policy.take_profit_ladder = {
      levels: [
        { trigger_r: "2", quantity_fraction: "0.6" },
        { trigger_r: "3", quantity_fraction: "0.4" },
      ],
    };
    const legs = [100, 110].map((price, index) => ({
      leg_index: index,
      leg_count: 2,
      release_after_seconds: 0,
      raw_price: String(price),
      price: String(price),
      sizing_price: String(price),
      requested_notional: "10",
      quantity: "0.1",
      effective_notional: "10",
    }));

    const longProjection = projectOrderScheduleProtectionPrices(
      "LONG",
      spec,
      legs,
    );
    expect(longProjection?.entry).toEqual({ lower: 105, upper: 105 });
    expect(longProjection?.stop).toEqual({ lower: 103.95, upper: 103.95 });
    expect(longProjection?.takeProfits[0]?.price).toEqual({
      lower: 107.1,
      upper: 107.1,
    });
    expect(longProjection?.takeProfits[1]?.price).toEqual({
      lower: 108.15,
      upper: 108.15,
    });

    const shortProjection = projectOrderScheduleProtectionPrices(
      "SHORT",
      spec,
      legs,
    );
    expect(shortProjection?.stop).toEqual({ lower: 106.05, upper: 106.05 });
    expect(shortProjection?.takeProfits[0]?.price).toEqual({
      lower: 102.9,
      upper: 102.9,
    });

    const compiledProjection = projectOrderScheduleProtectionPrices(
      "LONG",
      spec,
      legs,
      {
        average_entry_price: "104.9",
        entry_boundary_price: "100",
        stop_price: "103.8",
        quantity: "0.2",
        gross_price_loss: "0.22",
        estimated_entry_fee: "0.0042",
        estimated_exit_fee: "0.01038",
        maximum_projected_loss: "0.23458",
      },
    );
    expect(compiledProjection?.entry).toEqual({ lower: 104.9, upper: 104.9 });
    expect(compiledProjection?.stop).toEqual({ lower: 103.8, upper: 103.8 });

    spec.protection_policy.take_profit_ladder = {
      levels: [{ trigger_r: "1e308", quantity_fraction: "1" }],
    };
    expect(projectOrderScheduleProtectionPrices(
      "LONG",
      spec,
      [{ ...legs[0]!, price: "1e100", sizing_price: "1e100" }],
    )?.takeProfits).toEqual([]);
  });

  it("hides stale server legs while preserving current draft inputs", () => {
    const spec = defaultSpec();
    const annotations = buildOrderScheduleChartAnnotations({
      direction: "LONG",
      referencePrice: "65755.5",
      spec,
      previewState: "PENDING",
      previewLegs: [{
        leg_index: 0,
        leg_count: 1,
        release_after_seconds: 0,
        raw_price: "65750",
        price: "65750",
        sizing_price: "65750",
        requested_notional: "10",
        quantity: "0.0001",
        effective_notional: "6.575",
      }],
    });

    expect(annotations.priceAnnotations.map((item) => item.role)).toEqual([
      "REFERENCE",
      "SINGLE_LIMIT",
    ]);
  });

  it("draws only absolute prices and describes venue or relative thresholds without fake lines", () => {
    const spec = defaultSpec();
    spec.venue_policy.price_match = "OPPONENT_5";
    spec.price_distribution = { kind: "SINGLE", limit_price: null };
    spec.entry_conditions.items.push(
      { kind: "MARK_PRICE", comparator: "LTE", price: "65000" },
      { kind: "CLOSED_BAR_PRICE_15M", comparator: "LTE", price: "64950" },
      { kind: "SPREAD_BPS", maximum_bps: "10" },
      { kind: "PRICE_MOVE_BPS", comparator: "DROP_GTE", threshold_bps: "30", window_seconds: 20 },
    );
    spec.protection_policy.take_profit_ladder = {
      levels: [{ trigger_r: "2", quantity_fraction: "1" }],
    };
    spec.dynamic_rules.push({
      kind: "CANCEL_ON_SHOCK",
      window_seconds: 15,
      adverse_move_bps: "40",
      invalidation_price: "66000",
      opportunity_missed_price: "64500",
      max_triggers: 1,
    });

    const annotations = buildOrderScheduleChartAnnotations({
      direction: "SHORT",
      referencePrice: "65755.5",
      spec,
      previewState: "BLOCKED",
      previewLegs: [],
    });

    expect(annotations.priceAnnotations.map((item) => item.role)).toEqual([
      "REFERENCE",
      "MARK_CONDITION",
      "MARK_CONDITION",
      "ENTRY_INVALIDATION",
      "ENTRY_INVALIDATION",
    ]);
    expect(annotations.priceAnnotations.find(
      (item) => item.id === "halpha-closed-bar-15m-condition",
    )?.detail).toContain("正在形成的 K 线不参与");
    expect(annotations.priceAnnotations.find(
      (item) => item.id === "halpha-entry-opportunity-missed-price",
    )?.label).toBe("做空机会错过下界");
    expect(annotations.relativeRules.map((item) => item.id)).toEqual([
      "halpha-price-match",
      "halpha-spread-condition",
      "halpha-price-move-condition",
      "halpha-initial-stop",
      "halpha-take-profit-0",
      "halpha-shock-cancel",
    ]);
    expect(annotations.relativeRules.find(
      (item) => item.id === "halpha-price-move-condition",
    )?.label).toBe("20s 下跌 ≥ 30 bps");
    expect(annotations.relativeRules.at(-1)?.detail).toContain("空头只把向上变动视为不利");
  });

  it("summarizes every configured take-profit level instead of implying only the first one exists", () => {
    expect(summarizeRelativeRules([
      {
        id: "halpha-initial-stop",
        label: "每笔成交后止损 · 100 bps",
        detail: "",
        base: "CONFIRMED_FILL",
      },
      {
        id: "halpha-take-profit-0",
        label: "成交后止盈 1 · 1R",
        detail: "",
        base: "CONFIRMED_FILL",
      },
      {
        id: "halpha-take-profit-1",
        label: "成交后止盈 2 · 2R",
        detail: "",
        base: "CONFIRMED_FILL",
      },
    ])).toBe("每笔成交后止损 · 100 bps · 2 级止盈 · 1R/2R");
  });
});
