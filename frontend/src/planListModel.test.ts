import { describe, expect, it } from "vitest";

import type {
  ActivationSummary,
  OrderSchedulePreview,
  PlanSummary,
} from "./api/client";
import {
  orderScheduleConditionIntent,
  orderScheduleIntent,
  planWorkbenchSections,
} from "./planListModel";

const NOW = Date.parse("2026-07-26T03:00:00Z");

function plan(
  planId: string,
  planVersionId: string | null,
  fixedValidUntil: string | null,
  productBuildConsistent: boolean | null,
  status: PlanSummary["status"] = planVersionId ? "RUNNING" : "DRAFT",
): PlanSummary {
  return {
    plan_id: planId,
    draft_version: 1,
    draft_content_digest: "a".repeat(64),
    updated_at: "2026-07-26T00:00:00Z",
    plan_name: planId,
    created_at: "2026-07-26T00:00:00Z",
    creator_kind: "AI",
    decision_basis: {
      kind: "DIRECT_EXECUTION",
      decision_basis_ref: "DIRECT_EXECUTION@1",
      parameters: {},
    },
    decision_basis_kind: "DIRECT_EXECUTION",
    decision_basis_ref: "DIRECT_EXECUTION@1",
    strategy_id: null,
    instrument_ref: "BTCUSDT-PERP",
    direction: "LONG",
    parameters: {},
    order_schedule_spec: null,
    position_alignment: null,
    max_notional: "100",
    valid_from: "2026-07-26T00:00:00Z",
    valid_until: "2026-07-26T01:00:00Z",
    status,
    activation_id: status === "DRAFT" ? null : `${planId}-activation`,
    activation_lifecycle: status === "ENDED" ? "COMPLETED" : status === "RUNNING" ? "RUNNING" : null,
    activation_created_at: status === "DRAFT" ? null : "2026-07-26T01:30:00Z",
    activation_updated_at: status === "DRAFT" ? null : "2026-07-26T02:00:00Z",
    plan_version_id: planVersionId,
    fixed_at: planVersionId ? "2026-07-26T00:00:00Z" : null,
    fixed_content_digest: planVersionId ? "b".repeat(64) : null,
    fixed_product_build_id: planVersionId ? "old-build" : null,
    fixed_valid_until: fixedValidUntil,
    product_build_consistent: productBuildConsistent,
    runtime_compatible: planVersionId ? true : null,
    runtime_incompatibility_reason: null,
  };
}

function activation(
  activationId: string,
  planVersionRef: string,
  lifecycle: ActivationSummary["lifecycle"],
  runState: ActivationSummary["run_state"],
): ActivationSummary {
  return {
    account_ref: "demo-account",
    activation_id: activationId,
    authority_class: "DEMO_VALIDATION",
    closure_digest: null,
    plan_version_ref: planVersionRef,
    plan_name: activationId,
    plan_created_at: "2026-07-26T00:00:00Z",
    plan_creator_kind: "AI",
    decision_basis_ref: "DIRECT_EXECUTION@1",
    environment_id: "demo",
    environment_kind: "DEMO",
    framework_strategy_id: "DIRECT_EXECUTION",
    instrument_ref: "BTCUSDT-PERP",
    direction: "LONG",
    lifecycle,
    run_state: runState,
    responsibility_owner: "HALPHA",
    pause_reason: runState === "PAUSED" ? "WRITER_CONTINUITY_LOST" : null,
    paused_at: null,
    current_resume_command_ref: null,
    protection_state: "WORKING",
    state_version: 2,
    entry_opportunity_consumed: false,
    has_entry_fill: false,
    latest_venue_cutoff: null,
    order_schedule_snapshot: null,
    position_alignment: null,
    pending_action_digest: null,
    reconciliation_digest: null,
    rule_state: {},
    takeover_scope: null,
    target_exposure: "0",
    created_at: "2026-07-26T01:30:00Z",
    updated_at: "2026-07-26T02:00:00Z",
    result_ref: lifecycle === "COMPLETED" ? "review-1" : null,
    closure_reason_code: lifecycle === "COMPLETED" ? "PLAN_EXIT" : null,
    primary_result: lifecycle === "COMPLETED" ? "COMPLETED" : null,
    trade_result: null,
  };
}

describe("plan workbench sections", () => {
  it("summarizes the configured order role and price without relying on the plan name", () => {
    const makerPlan = plan(
      "maker-plan",
      "maker-version",
      "2026-07-26T04:00:00Z",
      true,
    );
    makerPlan.order_schedule_spec = {
      entry_program: null,
      price_distribution: { kind: "SINGLE", limit_price: "63350" },
      amount_distribution: {
        mode: "FIXED",
        direction: "LOW_TO_HIGH",
        base_notional: "100",
        linear_step: "0",
        exponential_ratio: "1",
        custom_notionals: [],
      },
      venue_policy: {
        order_type: "LIMIT",
        time_in_force: "GTC",
        post_only: true,
        price_match: null,
        expire_at: null,
      },
      submission_mode: "SERIAL_PROTECTED",
      submission_order: "LOW_TO_HIGH",
      entry_conditions: { operator: "ALL", items: [] },
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

    expect(orderScheduleIntent(makerPlan.order_schedule_spec))
      .toBe("Maker only · 限价 63,350.00 USDT");
  });

  it("summarizes market and ladder intentions distinctly", () => {
    const marketPlan = plan("market", null, null, null);
    marketPlan.order_schedule_spec = {
      ...plan("base", null, null, null).order_schedule_spec,
      entry_program: null,
      price_distribution: { kind: "SINGLE", limit_price: null },
      amount_distribution: {
        mode: "FIXED",
        direction: "LOW_TO_HIGH",
        base_notional: "100",
        linear_step: "0",
        exponential_ratio: "1",
        custom_notionals: [],
      },
      venue_policy: {
        order_type: "MARKET",
        time_in_force: null,
        post_only: false,
        price_match: null,
        expire_at: null,
      },
      submission_mode: "SERIAL_PROTECTED",
      submission_order: "LOW_TO_HIGH",
      entry_conditions: { operator: "ALL", items: [] },
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
    const ladder = structuredClone(marketPlan.order_schedule_spec);
    if (!ladder) throw new Error("ORDER_SCHEDULE_REQUIRED");
    ladder.venue_policy = {
      order_type: "LIMIT",
      time_in_force: "GTC",
      post_only: false,
      price_match: null,
      expire_at: null,
    };
    ladder.price_distribution = {
      kind: "LADDER",
      lower_price: "63000",
      upper_price: "64000",
      level_count: 5,
      spacing_mode: "EQUAL",
      spacing_direction: "LOW_TO_HIGH",
      linear_start_weight: "1",
      linear_step: "1",
      geometric_ratio: "1",
      custom_gap_weights: [],
    };

    expect(orderScheduleIntent(marketPlan.order_schedule_spec)).toBe("市价");
    expect(orderScheduleIntent(ladder))
      .toBe("区间限价 63,000.00–64,000.00 USDT · 5 档");
  });

  it("uses normalized legs and venue tick precision for running plans", () => {
    const ladder: NonNullable<PlanSummary["order_schedule_spec"]> = {
      entry_program: null,
      price_distribution: {
        kind: "LADDER",
        lower_price: "63886.16961898",
        upper_price: "64112.24705542",
        level_count: 10,
        spacing_mode: "EQUAL",
        spacing_direction: "LOW_TO_HIGH",
        linear_start_weight: "1",
        linear_step: "1",
        geometric_ratio: "1",
        custom_gap_weights: [],
      },
      amount_distribution: {
        mode: "FIXED",
        direction: "LOW_TO_HIGH",
        base_notional: "100",
        linear_step: "0",
        exponential_ratio: "1",
        custom_notionals: [],
      },
      venue_policy: {
        order_type: "LIMIT",
        time_in_force: "GTC",
        post_only: true,
        price_match: null,
        expire_at: null,
      },
      submission_mode: "SERIAL_PROTECTED",
      submission_order: "LOW_TO_HIGH",
      entry_conditions: { operator: "ALL", items: [] },
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
    const snapshot = {
      schedule_spec: ladder,
      instrument_rules: { price_tick_size: "0.1" },
      normalized_legs: [
        { leg_index: 9, price: "64112.2" },
        { leg_index: 0, price: "63886.2" },
      ],
    } as unknown as OrderSchedulePreview;

    expect(orderScheduleIntent(ladder, snapshot)).toBe(
      "Maker only · 区间限价 63,886.2–64,112.2 USDT · 10 档",
    );
  });

  it("shows time-sliced intent and excludes the execution basis from the visible condition count", () => {
    const timeSliced = plan("time-sliced", null, null, null);
    timeSliced.order_schedule_spec = {
      entry_program: {
        kind: "TIME_SLICED",
        slice_count: 2,
        first_slice_delay_seconds: 0,
        slice_interval_seconds: 30,
      },
      price_distribution: { kind: "SINGLE", limit_price: null },
      amount_distribution: {
        mode: "FIXED",
        direction: "LOW_TO_HIGH",
        base_notional: "100",
        linear_step: "0",
        exponential_ratio: "1",
        custom_notionals: [],
      },
      venue_policy: {
        order_type: "MARKET",
        time_in_force: null,
        post_only: false,
        price_match: null,
        expire_at: null,
      },
      submission_mode: "SERIAL_PROTECTED",
      submission_order: "LOW_TO_HIGH",
      entry_conditions: {
        operator: "ALL",
        items: [
          { kind: "DECISION_BASIS_READY" },
          { kind: "MARK_PRICE", comparator: "GTE", price: "63965" },
          { kind: "SPREAD_BPS", maximum_bps: "2" },
          {
            kind: "PRICE_MOVE_BPS",
            comparator: "GTE",
            threshold_bps: "3",
            window_seconds: 30,
          },
        ],
      },
      protection_policy: {
        full_fill_loss_budget: null,
        initial_stop: {
          distance_bps: "15",
          trigger_source: "MARK_PRICE",
          coverage: "EACH_CONFIRMED_FILL",
        },
        take_profit_ladder: null,
        time_exit_seconds: 600,
      },
      dynamic_rules: [],
    };

    expect(orderScheduleIntent(timeSliced.order_schedule_spec))
      .toBe("时间分批 · 2 笔 · 市价");
    expect(orderScheduleConditionIntent(timeSliced.order_schedule_spec))
      .toBe("全部成立 · 3 个条件");
  });

  it("keeps paused open activations current and moves the latest completed cycle to history", () => {
    const activePlan = plan(
      "active-plan",
      "active-version",
      "2026-07-26T01:00:00Z",
      false,
    );
    const sections = planWorkbenchSections(
      [
        activePlan,
        plan("historical-plan", "historical-version", "2026-07-26T01:00:00Z", false, "ENDED"),
        plan("draft-plan", null, null, null),
      ],
      [
        activation("paused-activation", "active-version", "RUNNING", "PAUSED"),
        activation("completed-activation", "historical-version", "COMPLETED", "ACTIVE"),
      ],
      NOW,
    );

    expect(sections.currentActivations.map((item) => item.activation_id))
      .toEqual(["paused-activation"]);
    expect(sections.currentPlans.map((item) => item.plan_id)).toEqual(["draft-plan"]);
    expect(sections.historicalPlans.map((item) => item.plan_id))
      .toEqual(["historical-plan"]);
  });

  it("keeps an editable draft in the current list regardless of runtime snapshot metadata", () => {
    const compatible = plan(
      "compatible-plan",
      null,
      null,
      false,
    );

    const sections = planWorkbenchSections([compatible], [], NOW);

    expect(sections.currentPlans.map((item) => item.plan_id))
      .toEqual(["compatible-plan"]);
    expect(sections.historicalPlans).toEqual([]);
  });

  it("does not create a fourth plan section for a running plan", () => {
    const sections = planWorkbenchSections(
      [plan("shared-plan", "shared-version", "2026-07-26T04:00:00Z", true)],
      [
        activation("activation-a", "shared-version", "RUNNING", "ACTIVE"),
      ],
      NOW,
    );

    expect(sections.currentActivations.map((item) => item.activation_id))
      .toEqual(["activation-a"]);
    expect(sections.currentPlans).toEqual([]);
    expect(sections.historicalPlans).toEqual([]);
  });
});
