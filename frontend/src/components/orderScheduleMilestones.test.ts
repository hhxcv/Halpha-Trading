import { describe, expect, it } from "vitest";

import { ApiFailure } from "../api/client";
import { createDefaultOrderScheduleSpec } from "./orderScheduleEditorModel";
import {
  localOrderScheduleProblems,
  milestoneConfigurationReady,
  scheduleServerProblems,
  serverScheduleWasAssessed,
  stageHasServerProblem,
} from "./orderScheduleMilestones";

describe("order schedule milestone server validation", () => {
  it("keeps an event entry blocked locally until a market event exists", () => {
    const spec = createDefaultOrderScheduleSpec();
    spec.entry_program = {
      kind: "EVENT_TRIGGERED",
      slice_count: 1,
      first_slice_delay_seconds: 0,
      slice_interval_seconds: 0,
    };
    spec.entry_conditions.items = [{ kind: "DECISION_BASIS_READY" }];

    expect(localOrderScheduleProblems(spec, "BTCUSDT-PERP", "100", "schedule-1"))
      .toContain("事件触发入场必须至少配置一个价格、K 线收盘或短时变动事件。");

    spec.entry_conditions.items.push({
      kind: "PRICE_MOVE_BPS",
      comparator: "GTE",
      threshold_bps: "50",
      window_seconds: 60,
    });

    expect(localOrderScheduleProblems(spec, "BTCUSDT-PERP", "100", "schedule-1"))
      .not.toContain("事件触发入场必须至少配置一个价格、K 线收盘或短时变动事件。");
  });

  it("assigns structured request errors to the owning milestone", () => {
    const spec = createDefaultOrderScheduleSpec();
    const error = new ApiFailure(422, "INPUT_VALIDATION_FAILED", {
      detail: [
        {
          loc: ["body", "spec", "protection_policy", "initial_stop", "distance_bps"],
          type: "value_error",
        },
        {
          loc: ["body", "spec", "price_distribution", "limit_price"],
          type: "value_error",
        },
      ],
    });

    const problems = scheduleServerProblems(undefined, error, spec);

    expect(serverScheduleWasAssessed(undefined, error)).toBe(true);
    expect(stageHasServerProblem(problems, 0)).toBe(true);
    expect(stageHasServerProblem(problems, 1)).toBe(true);
    expect(stageHasServerProblem(problems, 2)).toBe(false);
  });

  it("rejects an empty closed 15m bar threshold locally", () => {
    const spec = createDefaultOrderScheduleSpec();
    spec.entry_conditions.items.push({
      kind: "CLOSED_BAR_PRICE_15M",
      comparator: "LTE",
      price: "",
    });

    expect(localOrderScheduleProblems(
      spec,
      "BTCUSDT-PERP",
      "100",
      "schedule-closed-bar",
    )).toContain("15m 已闭合 K 线收盘阈值必须大于 0。");
  });

  it("does not impose a static 5000 bps initial-stop ceiling", () => {
    const spec = createDefaultOrderScheduleSpec();
    spec.protection_policy.initial_stop.distance_bps = "7500";

    expect(localOrderScheduleProblems(spec, "BTCUSDT-PERP", "100", "schedule-stop"))
      .not.toContain("初始止损距离必须大于 0 且低于 10000 bps。");
  });

  it("assigns exit dynamic rules and semantic errors to Exit", () => {
    const spec = createDefaultOrderScheduleSpec();
    spec.dynamic_rules = [{
      kind: "PROFIT_LOCK",
      activation_r: "1",
      mode: "RATIO",
      lock_fraction: "0.5",
      giveback_r: null,
      minimum_step_r: "0.25",
      minimum_update_interval_seconds: 5,
      max_adjustments: 8,
    }];
    const validationError = new ApiFailure(422, "INPUT_VALIDATION_FAILED", {
      detail: [{ loc: ["body", "spec", "dynamic_rules", 0, "lock_fraction"], type: "value_error" }],
    });
    const semanticError = new ApiFailure(
      409,
      "DIRECT_EXECUTION_AUTOMATIC_EXIT_REQUIRED",
    );

    expect(stageHasServerProblem(
      scheduleServerProblems(undefined, validationError, spec),
      2,
    )).toBe(true);
    expect(stageHasServerProblem(
      scheduleServerProblems(undefined, semanticError, spec),
      2,
    )).toBe(true);
  });

  it("does not treat availability failures as a completed assessment", () => {
    expect(serverScheduleWasAssessed(
      undefined,
      new ApiFailure(503, "INSTRUMENT_RULES_UNAVAILABLE"),
    )).toBe(false);
    expect(milestoneConfigurationReady(true, false, false)).toBe(true);
    expect(milestoneConfigurationReady(true, true, true)).toBe(false);
  });
});
