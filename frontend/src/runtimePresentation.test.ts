import { describe, expect, it } from "vitest";

import {
  activationSummaryCloseReason,
  boundedProgressPercent,
  compactRuntimeTimeline,
  currentRuntimeProtectionPrice,
  currentAccountSystemStop,
  elapsedDurationLabel,
  evaluateRuntimeEntryConditions,
  groupRuntimeTakeProfits,
  nextRuntimeProtectionStep,
  relativeAgeLabel,
  remainingTimeLabel,
  reviewExitReason,
  runtimeActionHasCurrentResponsibility,
  runtimeFilledEntryLegCount,
  runtimeConditionPendingPresentation,
  runtimeDynamicCancelPresentation,
  runtimeNotSubmittedEntryPresentation,
  runtimeEntryInterruptionPresentation,
  runtimeEntryOrderDeadline,
  runtimeNoActionPresentation,
  runtimePlanEventDynamicCancelPresentation,
  runtimeProtectionAttention,
  runtimeHasEntryFill,
  runtimeHasPendingVenueAction,
  runtimeEntryConditionClauses,
  runtimeExecutorConditionStatus,
  runtimeEventCategory,
  runtimeEntryPolicyRetryState,
  runtimeEntryOrderAttemptedBefore,
  runtimeSignedMovePresentation,
  terminalEntryResultRequiresReview,
  runtimeWorkingEntryOrders,
} from "./runtimePresentation";

describe("runtime presentation", () => {
  it("keeps only action states that still represent a current responsibility", () => {
    expect([
      "READY",
      "SUBMITTING",
      "UNKNOWN",
      "OPEN",
    ].every((state) => runtimeActionHasCurrentResponsibility({ state }))).toBe(true);
    expect([
      "NOT_SUBMITTED",
      "CLOSED",
      "HANDED_OVER",
      "",
    ].some((state) => runtimeActionHasCurrentResponsibility({ state }))).toBe(false);
  });

  it("counts filled entry legs once across retries and duplicate fill facts", () => {
    const entry = (
      actionId: string,
      legIndex: number,
      attemptIndex: number,
    ) => ({
      execution_action_id: actionId,
      action_kind: "ENTRY",
      action_terms: {
        execution_context: {
          order_schedule: {
            leg_index: legIndex,
            attempt_index: attemptIndex,
          },
        },
      },
    });
    const actions = [
      entry("leg-0-attempt-0", 0, 0),
      entry("leg-0-attempt-1", 0, 1),
      entry("leg-1-attempt-0", 1, 0),
      { execution_action_id: "stop", action_kind: "PROTECTION" },
    ];
    const facts = [
      { venue_fact_id: "fill-0", action_ref: "leg-0-attempt-0", kind: "FILL" },
      { venue_fact_id: "fill-0-copy", action_ref: "leg-0-attempt-0", kind: "FILL" },
      { venue_fact_id: "fill-0-retry", action_ref: "leg-0-attempt-1", kind: "FILL" },
      { venue_fact_id: "fill-1", action_ref: "leg-1-attempt-0", kind: "FILL" },
      { venue_fact_id: "stop-fill", action_ref: "stop", kind: "FILL" },
    ];

    expect(runtimeFilledEntryLegCount(actions, facts, 3)).toBe(2);
    expect(runtimeFilledEntryLegCount(actions, facts, 0)).toBe(0);
  });

  it("only treats unresolved action states as pending venue activity", () => {
    expect(runtimeHasPendingVenueAction([
      { action_kind: "ENTRY", state: "READY" },
      { action_kind: "ENTRY", state: "NOT_SUBMITTED" },
      { action_kind: "ENTRY", state: "CLOSED" },
      { action_kind: "ENTRY", state: "HANDED_OVER" },
    ])).toBe(false);
    expect(runtimeHasPendingVenueAction([
      { action_kind: "ENTRY", state: "SUBMITTING" },
    ])).toBe(true);
    expect(runtimeHasPendingVenueAction([
      { action_kind: "ENTRY", state: "UNKNOWN" },
    ])).toBe(true);
    expect(runtimeHasPendingVenueAction([
      { action_kind: "ENTRY", state: "OPEN" },
    ])).toBe(true);
  });

  it("does not keep an earlier normal rejection after a retry becomes current", () => {
    const original = {
      execution_action_id: "entry-0",
      action_kind: "ENTRY",
      created_at: "2026-07-30T00:00:00Z",
      action_terms: {
        execution_context: {
          order_schedule: { leg_index: 0, attempt_index: 0 },
        },
      },
    };
    const retry = {
      execution_action_id: "entry-1",
      action_kind: "ENTRY",
      created_at: "2026-07-30T00:00:03Z",
      action_terms: {
        execution_context: {
          order_schedule: {
            leg_index: 0,
            attempt_index: 1,
            retry_reason: "POST_ONLY_WOULD_TAKE_RACE",
          },
        },
      },
    };
    const state = runtimeEntryPolicyRetryState(
      [original, retry],
      [{
        venue_fact_id: "rejected-0",
        action_ref: "entry-0",
        kind: "ORDER_STATE",
        cutoff: "2026-07-30T00:00:01Z",
        payload: { status: "REJECTED", reason: "-5022" },
      }],
    );
    expect(state.latestEntryAction).toBe(retry);
    expect(state.latestRejectedFact).toBeNull();
    expect(state.retryCount).toBe(1);
  });

  it("shows the latest action rejection and counts retries across schedule legs", () => {
    const actions = [
      {
        execution_action_id: "leg-0-attempt-0",
        action_kind: "ENTRY",
        created_at: "2026-07-30T00:00:00Z",
        action_terms: {
          execution_context: {
            order_schedule: { leg_index: 0, attempt_index: 0 },
          },
        },
      },
      {
        execution_action_id: "leg-0-attempt-1",
        action_kind: "ENTRY",
        created_at: "2026-07-30T00:00:02Z",
        action_terms: {
          execution_context: {
            order_schedule: {
              leg_index: 0,
              attempt_index: 1,
              retry_reason: "POST_ONLY_WOULD_TAKE_RACE",
            },
          },
        },
      },
      {
        execution_action_id: "leg-1-attempt-1",
        action_kind: "ENTRY",
        created_at: "2026-07-30T00:00:04Z",
        action_terms: {
          execution_context: {
            order_schedule: {
              leg_index: 1,
              attempt_index: 1,
              retry_reason: "PRICE_MATCH_TEMPORARILY_UNAVAILABLE",
            },
          },
        },
      },
    ];
    const earlier = {
      venue_fact_id: "rejected-earlier",
      action_ref: "leg-0-attempt-1",
      kind: "ORDER_STATE",
      cutoff: "2026-07-30T00:00:03Z",
      payload: { status: "REJECTED", reason: "-5022" },
    };
    const latest = {
      venue_fact_id: "rejected-latest",
      action_ref: "leg-1-attempt-1",
      kind: "ORDER_STATE",
      cutoff: "2026-07-30T00:00:05Z",
      payload: { status: "REJECTED", reason: "-5037" },
    };
    const state = runtimeEntryPolicyRetryState(actions, [earlier, latest]);
    expect(state.latestEntryAction).toBe(actions[2]);
    expect(state.latestRejectedFact).toBe(latest);
    expect(state.retryCount).toBe(2);
  });

  it("keeps a called entry handed to the user out of the zero-fill result", () => {
    expect(terminalEntryResultRequiresReview([
      {
        action_kind: "ENTRY",
        state: "HANDED_OVER",
        call_started_at: "2026-07-29T03:00:00Z",
      },
    ])).toBe(true);
    expect(terminalEntryResultRequiresReview([
      {
        action_kind: "ENTRY",
        state: "HANDED_OVER",
        call_started_at: null,
      },
      {
        action_kind: "ENTRY",
        state: "NOT_SUBMITTED",
      },
      {
        action_kind: "ENTRY",
        state: "CLOSED",
        closure_evidence_digest: "closed",
      },
    ])).toBe(false);
    expect(terminalEntryResultRequiresReview([
      {
        action_kind: "PROTECTION",
        state: "HANDED_OVER",
        call_started_at: "2026-07-29T03:00:00Z",
      },
    ])).toBe(false);
  });

  it("distinguishes planned exit handoff from an unexpected protection gap", () => {
    const common = {
      hasEntryFill: true,
      tradeClosed: false,
      protectionState: "GAP",
      firstFillAt: "2026-07-29T00:00:00Z",
      timeExitSeconds: 60,
      nowMs: Date.parse("2026-07-29T00:00:30Z"),
    };
    expect(runtimeProtectionAttention({
      ...common,
      lifecycle: "RUNNING",
    })).toBe("UNEXPECTED_GAP");
    expect(runtimeProtectionAttention({
      ...common,
      lifecycle: "EXITING",
    })).toBe("EXIT_HANDOFF");
    expect(runtimeProtectionAttention({
      ...common,
      lifecycle: "RUNNING",
      exitActionStarted: true,
    })).toBe("EXIT_HANDOFF");
    expect(runtimeProtectionAttention({
      ...common,
      lifecycle: "RUNNING",
      nowMs: Date.parse("2026-07-29T00:01:00Z"),
    })).toBe("EXIT_HANDOFF");
    expect(runtimeProtectionAttention({
      ...common,
      lifecycle: "RUNNING",
      protectionState: "WORKING",
      nowMs: Date.parse("2026-07-29T00:01:00Z"),
    })).toBe("EXIT_HANDOFF");
    expect(runtimeProtectionAttention({
      ...common,
      lifecycle: "RUNNING",
      protectionState: "WORKING",
      nowMs: Date.parse("2026-07-29T00:00:59Z"),
    })).toBe("NONE");
    expect(runtimeProtectionAttention({
      ...common,
      lifecycle: "COMPLETED",
      tradeClosed: true,
    })).toBe("NONE");
  });

  it("does not call a confirmed fill unentered while the activation projection catches up", () => {
    expect(runtimeHasEntryFill({
      projectedHasEntryFill: false,
      fillCount: 1,
      attributedPositionQuantity: 0,
    })).toBe(true);
    expect(runtimeHasEntryFill({
      projectedHasEntryFill: false,
      fillCount: 0,
      attributedPositionQuantity: -0.0011,
    })).toBe(true);
    expect(runtimeHasEntryFill({
      projectedHasEntryFill: false,
      fillCount: 0,
      attributedPositionQuantity: 0,
    })).toBe(false);
  });

  it("shows the tightest working protection and the next unapplied step", () => {
    const initial = {
      action_kind: "PROTECTION",
      state: "OPEN",
      action_terms: { trigger_price: "104" },
    };
    const firstReplacement = {
      action_kind: "PROTECTION",
      state: "OPEN",
      action_terms: {
        trigger_price: "100",
        execution_context: {
          protection_replacement: { step_index: 0 },
        },
      },
    };
    const unrelated = {
      action_kind: "TAKE_PROFIT",
      state: "OPEN",
      action_terms: { trigger_price: "90" },
    };
    expect(currentRuntimeProtectionPrice(
      [initial, firstReplacement, unrelated],
      "SHORT",
    )).toBe(100);
    expect(currentRuntimeProtectionPrice(
      [
        { ...initial, action_terms: { trigger_price: "96" } },
        { ...firstReplacement, action_terms: { ...firstReplacement.action_terms, trigger_price: "100" } },
      ],
      "LONG",
    )).toBe(100);

    const schedule = {
      dynamic_rules: [{
        kind: "STEPPED_PROTECTION",
        steps: [
          { trigger_r: "1", stop_r: "0" },
          { trigger_r: "2", stop_r: "1" },
        ],
      }],
    };
    const state = {
      direct_protection: {
        anchor_price: "100",
        anchor_r: "4",
      },
    };
    expect(nextRuntimeProtectionStep(
      schedule,
      state,
      [initial, unrelated],
      [],
      "SHORT",
      97,
    )).toEqual({
      stepIndex: 0,
      stepCount: 2,
      triggerR: "1",
      stopR: "0",
      triggerPrice: 96,
      stopPrice: 100,
      crossed: false,
    });
    expect(nextRuntimeProtectionStep(
      schedule,
      state,
      [initial, firstReplacement, unrelated],
      [],
      "SHORT",
      91,
    )).toEqual({
      stepIndex: 1,
      stepCount: 2,
      triggerR: "2",
      stopR: "1",
      triggerPrice: 92,
      stopPrice: 96,
      crossed: true,
    });
    expect(nextRuntimeProtectionStep(
      schedule,
      state,
      [initial, {
        ...firstReplacement,
        execution_action_id: "rejected-replacement",
        state: "CLOSED",
      }],
      [{
        kind: "ORDER_STATE",
        action_ref: "rejected-replacement",
        payload: { status: "REJECTED" },
      }],
      "SHORT",
      97,
    )?.stepIndex).toBe(0);
  });

  it("explains a shock-triggered entry cancellation from the frozen schedule", () => {
    expect(runtimeDynamicCancelPresentation({
      action_kind: "CANCEL",
      action_terms: {
        causation_ref: "activation:DIRECT_DYNAMIC:DIRECT_ENTRY_SHOCK:entry:v4",
      },
    }, {
      schedule_spec: {
        dynamic_rules: [{
          kind: "CANCEL_ON_SHOCK",
          window_seconds: 30,
          adverse_move_bps: "12",
          max_triggers: 1,
        }],
      },
    })).toEqual({
      headline: "短时不利异动已触发撤单",
      detail: "30 秒内不利变动达到 12 bps；已终止未成交入场",
    });
  });

  it("explains a fixed-price invalidation cancellation", () => {
    expect(runtimeDynamicCancelPresentation({
      action_kind: "CANCEL",
      source_identity: "activation:DIRECT_DYNAMIC:DIRECT_ENTRY_INVALIDATION_PRICE:entry",
      action_terms: {},
    }, {
      schedule_spec: {
        dynamic_rules: [{
          kind: "CANCEL_ON_SHOCK",
          invalidation_price: "65000",
          max_triggers: 1,
        }],
      },
    })).toEqual({
      headline: "价格突破失效位，入场已取消",
      detail: "标记价到达固定失效位 65000 USDT；已终止未成交入场",
    });
  });

  it("explains a fail-closed cancellation when invalidation facts go stale", () => {
    expect(runtimeDynamicCancelPresentation({
      action_kind: "CANCEL",
      action_terms: {
        causation_ref:
          "activation:DIRECT_DYNAMIC:DIRECT_ENTRY_INVALIDATION_STATUS_UNKNOWN:entry:v4",
      },
    }, {
      schedule_spec: {
        dynamic_rules: [{
          kind: "CANCEL_ON_SHOCK",
          opportunity_missed_price: "64110",
          max_triggers: 1,
        }],
      },
    })).toEqual({
      headline: "行情数据中断，未成交挂单已撤销",
      detail: "入场失效条件暂时无法核对；为避免在失去监控时继续成交，系统撤销了当前挂单",
    });
  });

  it("explains a favorable move that made the planned entry stale", () => {
    expect(runtimeDynamicCancelPresentation({
      action_kind: "CANCEL",
      source_identity:
        "activation:DIRECT_DYNAMIC:DIRECT_ENTRY_OPPORTUNITY_MISSED_PRICE:entry",
      action_terms: {},
    }, {
      schedule_spec: {
        dynamic_rules: [{
          kind: "CANCEL_ON_SHOCK",
          opportunity_missed_price: "63450",
          max_triggers: 1,
        }],
      },
    })).toEqual({
      headline: "价格已走远，本次入场机会已取消",
      detail: "标记价到达机会错过边界 63450 USDT；已终止未成交入场",
    });
  });

  it("explains expiry cancellation and leaves unrelated cancels generic", () => {
    const schedule = {
      dynamic_rules: [{
        kind: "EXPIRE_REMAINING",
        after_seconds: 1200,
      }],
    };
    expect(runtimeDynamicCancelPresentation({
      action_kind: "CANCEL",
      source_identity:
        "activation:DIRECT_DYNAMIC:DIRECT_ENTRY_REMAINING_EXPIRED:entry:v2",
    }, schedule)).toEqual({
      headline: "未成交委托已到期撤销",
      detail: "首次提交后 1200 秒仍未成交；已撤销剩余入场委托",
    });
    expect(runtimeDynamicCancelPresentation({
      action_kind: "CANCEL",
      action_terms: {
        causation_ref:
          "activation:DIRECT_DYNAMIC:DIRECT_TIME_SLICE_EXPIRED:entry:1:v2",
      },
    }, schedule)).toEqual({
      headline: "本批未成交委托已到期撤销",
      detail: "本批提交后 1200 秒仍未成交；撤单闭合后继续按原计划处理剩余批次",
    });
    expect(runtimeDynamicCancelPresentation({
      action_kind: "CANCEL",
      state: "CLOSED",
      action_terms: {
        causation_ref:
          "activation:DIRECT_DYNAMIC:DIRECT_ENTRY_REPRICE:entry:v4",
      },
    }, {
      schedule_spec: {
        dynamic_rules: [{
          kind: "REPRICE_ENTRY",
          trigger_distance_bps: "1",
          maximum_total_move_bps: "20",
          max_adjustments: 5,
        }],
      },
    })).toEqual({
      headline: "移动挂单触发，旧委托撤销已核对闭合",
      detail: "盘口偏离达到 1 bps；撤销后重新核对入场条件与重挂上限，不保证一定产生新委托",
    });
    expect(runtimePlanEventDynamicCancelPresentation({
      rule_id: "CANCEL_OPEN_RESPONSIBILITY",
      source_identity:
        "activation:CANCEL:entry:activation:DIRECT_DYNAMIC:DIRECT_ENTRY_REPRICE:entry:v4",
    }, {
      schedule_spec: {
        dynamic_rules: [{
          kind: "REPRICE_ENTRY",
          trigger_distance_bps: "1",
        }],
      },
    })).toEqual({
      headline: "移动挂单触发，准备撤销旧委托",
      detail: "盘口偏离达到 1 bps；撤销后重新核对入场条件与重挂上限，不保证一定产生新委托",
    });
    expect(runtimeDynamicCancelPresentation({
      action_kind: "CANCEL",
      action_terms: { causation_ref: "activation:USER_CANCEL:entry" },
    }, schedule)).toBeNull();
    expect(runtimeDynamicCancelPresentation({
      action_kind: "ENTRY",
    }, schedule)).toBeNull();
  });

  it("explains an invalidated entry leg that was never submitted", () => {
    const schedule = {
      schedule_spec: {
        dynamic_rules: [{
          kind: "CANCEL_ON_SHOCK",
          window_seconds: 30,
          adverse_move_bps: "3",
          max_triggers: 1,
        }],
      },
    };
    expect(runtimeNotSubmittedEntryPresentation({
      action_kind: "ENTRY",
      state: "NOT_SUBMITTED",
      not_submitted_reason: "DIRECT_ENTRY_SHOCK",
    }, schedule)).toEqual({
      headline: "短时反向异动触发，剩余入场已取消",
      detail: "30 秒内不利变动达到 3 bps；未释放批次不再提交；已成交仓位继续按原保护与退出计划处理",
    });
    expect(runtimeNotSubmittedEntryPresentation({
      action_kind: "ENTRY",
      state: "READY",
      not_submitted_reason: "DIRECT_ENTRY_SHOCK",
    }, schedule)).toBeNull();
    expect(runtimeNotSubmittedEntryPresentation({
      action_kind: "ENTRY",
      state: "NOT_SUBMITTED",
      not_submitted_reason: "DIRECT_ENTRY_UNKNOWN",
    }, schedule)).toBeNull();
  });

  it("recovers the entry invalidation reason from the persisted cancel plan event", () => {
    const schedule = {
      schedule_spec: {
        dynamic_rules: [{
          kind: "CANCEL_ON_SHOCK",
          invalidation_price: "64040",
          max_triggers: 1,
        }],
      },
    };
    const detail = {
      rule_id: "CANCEL_OPEN_RESPONSIBILITY",
      source_identity:
        "activation:CANCEL:entry:activation:DIRECT_DYNAMIC:DIRECT_ENTRY_INVALIDATION_PRICE:entry:v2",
    };

    expect(runtimePlanEventDynamicCancelPresentation(detail, schedule)).toEqual({
      headline: "价格突破失效位，入场已取消",
      detail: "标记价到达固定失效位 64040 USDT；已终止未成交入场",
    });
    expect(runtimeEntryInterruptionPresentation([{
      source: "PLAN_EVENT",
      at: "2026-07-29T00:43:34Z",
      detail,
    }], schedule)).toEqual({
      headline: "价格突破失效位，入场已取消",
      detail: "标记价到达固定失效位 64040 USDT；已终止未成交入场",
      at: "2026-07-29T00:43:34Z",
    });
  });

  it("does not call one expired time slice a terminal entry interruption", () => {
    const schedule = {
      schedule_spec: {
        dynamic_rules: [{
          kind: "EXPIRE_REMAINING",
          after_seconds: 15,
        }],
      },
    };
    expect(runtimeEntryInterruptionPresentation([{
      source: "PLAN_EVENT",
      at: "2026-07-30T06:48:23+08:00",
      detail: {
        rule_id: "CANCEL_OPEN_RESPONSIBILITY",
        source_identity:
          "activation:CANCEL:entry:activation:DIRECT_DYNAMIC:DIRECT_TIME_SLICE_EXPIRED:entry:1:v2",
      },
    }], schedule)).toBeNull();
    expect(runtimeEntryInterruptionPresentation([{
      source: "PLAN_EVENT",
      at: "2026-07-30T06:49:23+08:00",
      detail: {
        rule_id: "CANCEL_OPEN_RESPONSIBILITY",
        source_identity:
          "activation:CANCEL:entry:activation:DIRECT_DYNAMIC:DIRECT_ENTRY_REPRICE:entry:1:v2",
      },
    }], schedule)).toBeNull();
  });

  it("counts only entry orders whose latest venue fact is still working", () => {
    const actions = [
      { execution_action_id: "entry-1", action_kind: "ENTRY" },
      { execution_action_id: "entry-2", action_kind: "ENTRY" },
      { execution_action_id: "stop", action_kind: "PROTECTION" },
    ];
    const facts = [
      {
        kind: "ORDER_STATE",
        action_ref: "entry-1",
        cutoff: "2026-07-27T00:00:01Z",
        payload: { status: "WORKING" },
      },
      {
        kind: "ORDER_STATE",
        action_ref: "entry-2",
        cutoff: "2026-07-27T00:00:02Z",
        payload: { status: "PARTIALLY_FILLED" },
      },
      {
        kind: "ORDER_STATE",
        action_ref: "entry-1",
        cutoff: "2026-07-27T00:00:03Z",
        payload: { status: "CANCELED" },
      },
      {
        kind: "ORDER_STATE",
        action_ref: "stop",
        cutoff: "2026-07-27T00:00:04Z",
        payload: { status: "WORKING" },
      },
    ];

    expect(runtimeWorkingEntryOrders(actions, facts)).toEqual({
      count: 1,
      complete: true,
    });
    expect(runtimeWorkingEntryOrders([
      ...actions,
      { execution_action_id: "entry-3", action_kind: "ENTRY" },
    ], facts)).toEqual({
      count: 1,
      complete: false,
    });
  });

  it("does not call a fully filled market entry a working order", () => {
    const actions = [
      { execution_action_id: "entry-market", action_kind: "ENTRY" },
    ];
    const facts = [
      {
        kind: "ORDER_STATE",
        action_ref: "entry-market",
        cutoff: "2026-07-27T00:00:01Z",
        payload: { status: "WORKING" },
      },
      {
        kind: "FILL",
        action_ref: "entry-market",
        cutoff: "2026-07-27T00:00:02Z",
        payload: { last_quantity: "0.0015", leaves_quantity: "0" },
      },
    ];

    expect(runtimeWorkingEntryOrders(actions, facts)).toEqual({
      count: 0,
      complete: true,
    });
  });

  it("turns connector exception codes into actionable user-facing no-action events", () => {
    expect(runtimeNoActionPresentation(
      "EXECUTOR_RUNTIME_REATTACHED",
    )).toEqual({
      headline: "执行器连接已恢复",
      detail: "实时价格变动窗口从此时重新累计；新增入场暂停不会因本事件自动解除",
    });
    expect(runtimeNoActionPresentation(
      "ENTRY_WINDOW_EXPIRED",
    )).toEqual({
      headline: "入场窗口已结束",
      detail: "现有事实未记录订单尝试或明确阻断，无法确定未提交原因",
    });
    expect(runtimeNoActionPresentation(
      "ENTRY_WINDOW_EXPIRED",
      {},
      null,
      { entryConditionsConfigured: true },
    )).toEqual({
      headline: "入场窗口已结束",
      detail: "条件未在有效期内同时满足，因此未提交订单",
    });
    expect(runtimeNoActionPresentation(
      "ENTRY_WINDOW_EXPIRED",
      {},
      null,
      { entryOrderAttempted: true },
    )).toEqual({
      headline: "入场窗口已结束",
      detail: "此前已有入场订单尝试；窗口结束后，未成交订单与剩余入场已按计划停止",
    });
    expect(runtimeNoActionPresentation(
      "ENTRY_WINDOW_EXPIRED",
      {},
      null,
      { priorBlockingReason: "ACCOUNT_MARGIN_MODE_NOT_ISOLATED" },
    )).toEqual({
      headline: "入场窗口已结束",
      detail: "窗口结束前最后一次记录的明确阻断为“当前交易对不是逐仓保证金模式”；未形成交易所订单",
    });
    expect(runtimeNoActionPresentation(
      "ENTRY_REMAINING_EXPIRED",
    )).toEqual({
      headline: "未成交委托等待期已结束",
      detail: "入场机会已关闭；开放委托将按计划撤销",
    });
    expect(runtimeNoActionPresentation(
      "ENTRY_MARKET_INVALIDATED",
      {
        capital_decision: {
          evidence: {
            checks: [
              {
                kind: "INVALIDATION_PRICE",
                configured_price: "64050",
                observed_mark_price: "64057.12500000",
                result: "TRUE",
              },
            ],
          },
        },
      },
    )).toEqual({
      headline: "价格已突破失效位，取消入场",
      detail: "标记价 64,057.125 USDT 已到达失效边界 64,050 USDT；未形成交易所风险",
    });
    expect(runtimeNoActionPresentation(
      "ENTRY_MARKET_INVALIDATED",
      {
        capital_decision: {
          evidence: {
            checks: [
              {
                kind: "OPPORTUNITY_MISSED_PRICE",
                configured_price: "64390",
                observed_mark_price: "64418.47762681",
                result: "TRUE",
              },
            ],
          },
        },
      },
      "0.1",
    )).toEqual({
      headline: "价格已错过机会边界，取消入场",
      detail: "标记价 64,418.5 USDT 已到达机会错过边界 64,390.0 USDT；未形成交易所风险",
    });
    expect(runtimeNoActionPresentation(
      "ENTRY_MARKET_INVALIDATED",
      {
        capital_decision: {
          evidence: {
            checks: [
              {
                kind: "ADVERSE_MOVE",
                window_seconds: 30,
                configured_adverse_move_bps: "20",
                observed_move_bps: "22.5",
                result: "TRUE",
              },
            ],
          },
        },
      },
    )).toEqual({
      headline: "短时反向异动触发，取消入场",
      detail: "30 秒价格变动 22.5 bps 已达到不利异动阈值 20 bps；未形成交易所风险",
    });
    expect(runtimeNoActionPresentation(
      "MARK_PRICE_QUERY_FAILED_BINANCECLIENTERROR",
    )).toEqual({
      headline: "标记价格查询暂时失败",
      detail: "未下单；系统会自动重试，不影响已有委托与保护",
    });
    expect(runtimeNoActionPresentation(
      "ACCOUNT_FACT_QUERY_FAILED_BINANCECLIENTERROR",
    ).headline).toBe("账户与持仓查询暂时失败");
    expect(runtimeNoActionPresentation(
      "UNMAPPED_INTERNAL_REASON",
    )).toEqual({
      headline: "本次执行条件暂不可确认",
      detail: "未下单；技术原因可在补充诊断中查看",
    });
  });

  it("detects an entry order acknowledged before the terminal window event", () => {
    const actions = [{
      execution_action_id: "entry-1",
      action_kind: "ENTRY",
    }];
    const facts = [{
      kind: "ORDER_STATE",
      action_ref: "entry-1",
      cutoff: "2026-07-27T00:00:02Z",
      payload: { status: "WORKING" },
    }];

    expect(runtimeEntryOrderAttemptedBefore(
      actions,
      facts,
      "2026-07-27T00:00:03Z",
    )).toBe(true);
    expect(runtimeEntryOrderAttemptedBefore(
      actions,
      facts,
      "2026-07-27T00:00:01Z",
    )).toBe(false);
    expect(runtimeEntryOrderAttemptedBefore(
      [{ execution_action_id: "stop-1", action_kind: "PROTECTION" }],
      facts,
      "2026-07-27T00:00:03Z",
    )).toBe(false);
  });

  it("collapses unchanged authoritative order queries without removing transitions", () => {
    const facts = [
      {
        venue_fact_id: "stream",
        kind: "ORDER_STATE",
        source_class: "VENUE_STREAM",
        action_ref: "protection",
        payload: { status: "WORKING", venue_order_quantity: "0.1" },
      },
      {
        venue_fact_id: "query-1",
        kind: "ORDER_STATE",
        source_class: "VENUE_QUERY",
        action_ref: "protection",
        payload: { status: "WORKING", venue_order_quantity: "0.1" },
      },
      {
        venue_fact_id: "query-2",
        kind: "ORDER_STATE",
        source_class: "VENUE_QUERY",
        action_ref: "protection",
        payload: { status: "WORKING", venue_order_quantity: "0.1" },
      },
      {
        venue_fact_id: "query-cancelled",
        kind: "ORDER_STATE",
        source_class: "VENUE_QUERY",
        action_ref: "protection",
        payload: { status: "CANCELLED", venue_order_quantity: "0.1" },
      },
    ];
    const timeline = facts.map((fact) => ({
      source: "VENUE_FACT",
      source_ref: fact.venue_fact_id,
    }));

    const compact = compactRuntimeTimeline(timeline, facts);

    expect(compact.map((entry) => entry.item.source_ref))
      .toEqual(["stream", "query-2", "query-cancelled"]);
    expect(compact.map((entry) => entry.repeatCount)).toEqual([1, 2, 1]);
  });

  it("keeps non-query facts even when their shape repeats", () => {
    const facts = [
      { venue_fact_id: "fill-1", kind: "FILL", source_class: "VENUE_STREAM" },
      { venue_fact_id: "fill-2", kind: "FILL", source_class: "VENUE_STREAM" },
    ];
    const timeline = facts.map((fact) => ({
      source: "VENUE_FACT",
      source_ref: fact.venue_fact_id,
    }));

    expect(compactRuntimeTimeline(timeline, facts)).toHaveLength(2);
  });

  it("collapses unchanged venue-stream order updates but preserves acceptance", () => {
    const facts = [
      {
        venue_fact_id: "accepted",
        kind: "ORDER_STATE",
        source_class: "VENUE_STREAM",
        action_ref: "entry",
        payload: {
          status: "WORKING",
          event_type: "OrderAccepted",
          venue_order_ref: "venue-1",
          venue_order_quantity: "0.001",
        },
      },
      ...["updated-1", "updated-2", "updated-3"].map((venueFactId) => ({
        venue_fact_id: venueFactId,
        kind: "ORDER_STATE",
        source_class: "VENUE_STREAM",
        action_ref: "entry",
        payload: {
          status: "WORKING",
          event_type: "OrderUpdated",
          venue_order_ref: "venue-1",
          venue_order_quantity: venueFactId === "updated-1" ? "0.001" : "0.0010",
        },
      })),
    ];
    const timeline = facts.map((fact) => ({
      source: "VENUE_FACT",
      source_ref: fact.venue_fact_id,
    }));

    const compact = compactRuntimeTimeline(timeline, facts);

    expect(compact.map((entry) => entry.item.source_ref))
      .toEqual(["accepted", "updated-3"]);
    expect(compact.map((entry) => entry.repeatCount)).toEqual([1, 3]);
  });

  it("shows the actual emergency exit cause instead of a generic plan exit", () => {
    const result = {
      fills: [
        { action_kind: "ENTRY" },
        { action_kind: "EXIT" },
      ],
    };
    const actions = [{
      action_kind: "EXIT",
      action_terms: {
        causation_ref: "activation-1:EXIT:PROTECTION_GAP",
      },
    }];

    expect(reviewExitReason(result, actions)).toBe("保护缺口紧急退出");
    expect(reviewExitReason(result)).toBe("计划退出");
  });

  it("describes a take-profit fill as an order outcome, not a profit claim", () => {
    expect(reviewExitReason({
      fills: [
        { action_kind: "ENTRY" },
        { action_kind: "TAKE_PROFIT" },
      ],
    })).toBe("止盈订单成交");
  });

  it("uses only the latest account stop version and never revives an older stop", () => {
    const stopEvidence = [
      {
        scope: "ACCOUNT",
        categories: ["NEW_RISK"],
        source: "SYSTEM_EXTERNAL_ACTIVITY",
        version: 7,
      },
      {
        scope: "ACTIVATION",
        categories: ["NEW_RISK"],
        source: "USER",
        version: 1,
      },
      {
        scope: "ACCOUNT",
        categories: [],
        source: "SYSTEM_FRAMEWORK_SYNTHETIC_CORRECTION",
        version: 8,
      },
    ];

    expect(currentAccountSystemStop(stopEvidence)).toBeUndefined();
    expect(currentAccountSystemStop([{
      scope: "ACCOUNT",
      categories: ["ALL_EXCHANGE_CHANGES"],
      version: 9,
    }])).toBeDefined();
  });

  it("categorizes event records by the user action they describe", () => {
    const protectionAction = { action_kind: "PROTECTION" };
    expect(runtimeEventCategory({
      item: { source: "EXECUTION_ACTION", detail: {} },
      repeatCount: 1,
    }, protectionAction)).toBe("PROTECTION");
    expect(runtimeEventCategory({
      item: { source: "VENUE_FACT", detail: {} },
      fact: { source_class: "VENUE_QUERY", kind: "ORDER_STATE" },
      repeatCount: 1,
    }, protectionAction)).toBe("RECONCILIATION");
    expect(runtimeEventCategory({
      item: {
        source: "PLAN_EVENT",
        detail: { rule_id: "PROTECTION_AFTER_FILL" },
      },
      repeatCount: 1,
    })).toBe("PROTECTION");
    expect(runtimeEventCategory({
      item: {
        source: "PLAN_EVENT",
        detail: { rule_id: "EXECUTOR_RUNTIME_CONTINUITY" },
      },
      repeatCount: 1,
    })).toBe("RECONCILIATION");
    expect(runtimeEventCategory({
      item: { source: "ACTIVATION", detail: {} },
      repeatCount: 1,
    })).toBe("PLAN");
  });

  it("groups partial-fill take-profit orders by configured profit target", () => {
    const actions = [
      {
        action_kind: "TAKE_PROFIT",
        state: "OPEN",
        action_terms: {
          trigger_price: "64837.6",
          action_profile: "TAKE_PROFIT_1",
          execution_context: {
            direct_take_profit: { level_index: 0, trigger_r: "1" },
          },
        },
      },
      {
        action_kind: "TAKE_PROFIT",
        state: "OPEN",
        action_terms: {
          trigger_price: "64854.3",
          action_profile: "TAKE_PROFIT_1",
          execution_context: {
            direct_take_profit: { level_index: 0, trigger_r: "1" },
          },
        },
      },
      {
        action_kind: "TAKE_PROFIT",
        state: "CLOSED",
        action_terms: {
          trigger_price: "64967",
          action_profile: "TAKE_PROFIT_2",
          execution_context: {
            direct_take_profit: { level_index: 1, trigger_r: "2" },
          },
        },
      },
      {
        action_kind: "TAKE_PROFIT",
        state: "OPEN",
        action_terms: {
          trigger_price: "64983.7",
          action_profile: "TAKE_PROFIT_2",
          execution_context: {
            direct_take_profit: { level_index: 1, trigger_r: "2" },
          },
        },
      },
    ];

    expect(groupRuntimeTakeProfits(actions, "LONG")).toEqual([
      {
        levelIndex: 0,
        triggerR: "1",
        prices: [64837.6, 64854.3],
        workingOrderCount: 2,
        orderCount: 2,
      },
      {
        levelIndex: 1,
        triggerR: "2",
        prices: [64967, 64983.7],
        workingOrderCount: 1,
        orderCount: 2,
      },
    ]);
  });

  it("uses the action profile fallback and keeps short target prices descending", () => {
    const actions = [
      {
        action_kind: "TAKE_PROFIT",
        state: "OPEN",
        action_terms: {
          trigger_price: "64000",
          action_profile: "TAKE_PROFIT_1",
        },
      },
      {
        action_kind: "TAKE_PROFIT",
        state: "OPEN",
        action_terms: {
          trigger_price: "63900",
          action_profile: "TAKE_PROFIT_1",
        },
      },
    ];

    expect(groupRuntimeTakeProfits(actions, "SHORT")).toEqual([{
      levelIndex: 0,
      triggerR: "",
      prices: [64000, 63900],
      workingOrderCount: 2,
      orderCount: 2,
    }]);
  });

  it("extracts only user-consumable market entry conditions", () => {
    expect(runtimeEntryConditionClauses({
      order_schedule_snapshot: {
        schedule_spec: {
          entry_conditions: {
            items: [
              { kind: "DECISION_BASIS_READY" },
              { kind: "MARK_PRICE", comparator: "LTE", price: "64635" },
              {
                kind: "CLOSED_BAR_PRICE_15M",
                comparator: "LTE",
                price: "64500",
              },
              { kind: "SPREAD_BPS", maximum_bps: "2" },
              {
                kind: "PRICE_MOVE_BPS",
                comparator: "DROP_GTE",
                threshold_bps: "3",
                window_seconds: 30,
              },
            ],
          },
        },
      },
    })).toEqual([
      {
        kind: "MARK_PRICE",
        comparator: "LTE",
        value: "64635",
        windowSeconds: null,
      },
      {
        kind: "CLOSED_BAR_PRICE_15M",
        comparator: "LTE",
        value: "64500",
        windowSeconds: null,
      },
      {
        kind: "SPREAD_BPS",
        comparator: "LTE",
        value: "2",
        windowSeconds: null,
      },
      {
        kind: "PRICE_MOVE_BPS",
        comparator: "DROP_GTE",
        value: "3",
        windowSeconds: 30,
      },
    ]);
  });

  it("shows each frozen direct condition with a fail-closed current estimate", () => {
    const activation = {
      order_schedule_snapshot: {
        schedule_spec: {
          entry_conditions: {
            operator: "ALL",
            items: [
              { kind: "DECISION_BASIS_READY" },
              { kind: "SPREAD_BPS", maximum_bps: "2" },
              {
                kind: "PRICE_MOVE_BPS",
                comparator: "GTE",
                threshold_bps: "1",
                window_seconds: 30,
              },
            ],
          },
        },
      },
    };

    expect(evaluateRuntimeEntryConditions(activation, {
      basisReady: true,
      referencePrice: "65200",
      closedBar15mClose: null,
      spreadBps: 0.4,
      priceMoveBpsByWindow: { "30": "0.8" },
    })).toEqual({
      operator: "ALL",
      result: "FALSE",
      items: [
        {
          kind: "DECISION_BASIS_READY",
          comparator: "IS_TRUE",
          threshold: "已确认",
          windowSeconds: null,
          currentValue: "已确认",
          result: "TRUE",
        },
        {
          kind: "SPREAD_BPS",
          comparator: "LTE",
          threshold: "2",
          windowSeconds: null,
          currentValue: "0.4",
          result: "TRUE",
        },
        {
          kind: "PRICE_MOVE_BPS",
          comparator: "GTE",
          threshold: "1",
          windowSeconds: 30,
          currentValue: "0.8",
          result: "FALSE",
        },
      ],
    });
  });

  it("evaluates a 15m close condition from the closed-bar fact only", () => {
    const activation = {
      order_schedule_snapshot: {
        schedule_spec: {
          entry_conditions: {
            operator: "ALL",
            items: [{
              kind: "CLOSED_BAR_PRICE_15M",
              comparator: "LTE",
              price: "62970.5",
            }],
          },
        },
      },
    };

    expect(evaluateRuntimeEntryConditions(activation, {
      basisReady: true,
      referencePrice: "62900",
      closedBar15mClose: "62980",
      spreadBps: null,
      priceMoveBpsByWindow: {},
    })).toEqual({
      operator: "ALL",
      result: "FALSE",
      items: [{
        kind: "CLOSED_BAR_PRICE_15M",
        comparator: "LTE",
        threshold: "62970.5",
        windowSeconds: null,
        currentValue: "62980",
        result: "FALSE",
      }],
    });
  });

  it("keeps a short-window condition unknown until continuous quotes exist", () => {
    const evaluation = evaluateRuntimeEntryConditions({
      order_schedule_snapshot: {
        schedule_spec: {
          entry_conditions: {
            operator: "ALL",
            items: [{
              kind: "PRICE_MOVE_BPS",
              comparator: "DROP_GTE",
              threshold_bps: "3",
              window_seconds: 60,
            }],
          },
        },
      },
    }, {
      basisReady: true,
      referencePrice: "65200",
      closedBar15mClose: null,
      spreadBps: null,
      priceMoveBpsByWindow: {},
    });

    expect(evaluation.result).toBe("UNKNOWN");
    expect(evaluation.items[0]).toMatchObject({
      currentValue: null,
      result: "UNKNOWN",
    });
  });

  it("uses the persisted Executor judgement and exposes a Maker wait reason", () => {
    const activation = {
      order_schedule_snapshot: {
        schedule_spec: {
          entry_conditions: {
            operator: "ALL",
            items: [
              { kind: "DECISION_BASIS_READY" },
              { kind: "MARK_PRICE", comparator: "GTE", price: "63460" },
              { kind: "SPREAD_BPS", maximum_bps: "3" },
              {
                kind: "PRICE_MOVE_BPS",
                comparator: "DROP_GTE",
                threshold_bps: "5",
                window_seconds: 60,
              },
            ],
          },
        },
      },
      rule_state: {
        condition_judgements: {
          DIRECT_ENTRY: {
            result: "TRUE",
            item_results: ["TRUE", "TRUE", "TRUE", "TRUE"],
            phase: "PRE_SUBMIT_RECHECK",
            source_cutoff: "2026-07-28T02:00:00Z",
            evaluated_at: "2026-07-28T02:00:01Z",
            facts: {
              basis_ready: true,
              mark_price: "63470",
              bid_price: "63490",
              ask_price: "63500",
              price_move_bps_by_window: { "60": "-5.2" },
            },
            submission_ready: false,
            blocking_reason: "DIRECT_POST_ONLY_WOULD_TAKE",
          },
        },
      },
    };

    expect(runtimeExecutorConditionStatus(activation)).toEqual({
      evaluation: {
        operator: "ALL",
        result: "TRUE",
        items: [
          {
            kind: "DECISION_BASIS_READY",
            comparator: "IS_TRUE",
            threshold: "已确认",
            windowSeconds: null,
            currentValue: "已确认",
            result: "TRUE",
          },
          {
            kind: "MARK_PRICE",
            comparator: "GTE",
            threshold: "63460",
            windowSeconds: null,
            currentValue: "63470",
            result: "TRUE",
          },
          {
            kind: "SPREAD_BPS",
            comparator: "LTE",
            threshold: "3",
            windowSeconds: null,
            currentValue: expect.any(String),
            result: "TRUE",
          },
          {
            kind: "PRICE_MOVE_BPS",
            comparator: "DROP_GTE",
            threshold: "5",
            windowSeconds: 60,
            currentValue: "-5.2",
            result: "TRUE",
          },
        ],
      },
      phase: "PRE_SUBMIT_RECHECK",
      sourceCutoff: "2026-07-28T02:00:00Z",
      evaluatedAt: "2026-07-28T02:00:01Z",
      submissionReady: false,
      blockingReason: "DIRECT_POST_ONLY_WOULD_TAKE",
    });
  });

  it("rejects malformed Executor condition state instead of inventing authority", () => {
    expect(runtimeExecutorConditionStatus({
      order_schedule_snapshot: {
        schedule_spec: {
          entry_conditions: {
            operator: "ALL",
            items: [{ kind: "DECISION_BASIS_READY" }],
          },
        },
      },
      rule_state: {
        condition_judgements: {
          DIRECT_ENTRY: {
            result: "TRUE",
            item_results: [],
            phase: "INITIAL",
            source_cutoff: "2026-07-28T02:00:00Z",
            evaluated_at: "2026-07-28T02:00:01Z",
            facts: {},
          },
        },
      },
    })).toBeNull();
  });

  it("explains which missing fact is still being collected", () => {
    expect(runtimeConditionPendingPresentation({
      kind: "PRICE_MOVE_BPS",
      windowSeconds: 300,
    })).toBe("连续行情正在积累（需 300 秒）");
    expect(runtimeConditionPendingPresentation({
      kind: "PRICE_MOVE_BPS",
      windowSeconds: null,
    })).toBe("连续行情正在积累");
    expect(runtimeConditionPendingPresentation({
      kind: "MARK_PRICE",
      windowSeconds: null,
    })).toBe("等待实时行情");
    expect(runtimeConditionPendingPresentation({
      kind: "SPREAD_BPS",
      windowSeconds: null,
    })).toBe("等待实时行情");
    expect(runtimeConditionPendingPresentation({
      kind: "DECISION_BASIS_READY",
      windowSeconds: null,
    })).toBe("等待执行依据状态");
  });

  it("presents signed short-window moves with an explicit direction", () => {
    expect(runtimeSignedMovePresentation("-4.0088")).toEqual({
      direction: "下跌",
      magnitude: "4.0088",
    });
    expect(runtimeSignedMovePresentation("5.5182")).toEqual({
      direction: "上涨",
      magnitude: "5.5182",
    });
    expect(runtimeSignedMovePresentation("0")).toEqual({
      direction: "持平",
      magnitude: "0",
    });
    expect(runtimeSignedMovePresentation("0.0099")).toEqual({
      direction: "持平",
      magnitude: "0",
    });
    expect(runtimeSignedMovePresentation("-0.0099")).toEqual({
      direction: "持平",
      magnitude: "0",
    });
  });

  it("shows relative freshness at decision-friendly precision", () => {
    const now = Date.parse("2026-07-26T05:00:00Z");
    expect(relativeAgeLabel("2026-07-26T04:59:58Z", now)).toBe("刚刚");
    expect(relativeAgeLabel("2026-07-26T04:59:40Z", now)).toBe("20 秒前");
    expect(relativeAgeLabel("2026-07-26T04:30:00Z", now)).toBe("30 分钟前");
    expect(relativeAgeLabel("UNKNOWN", now)).toBe("未知");
  });

  it("shows elapsed review age without noisy seconds", () => {
    const now = Date.parse("2026-07-27T05:30:00Z");
    expect(elapsedDurationLabel("2026-07-27T05:29:40Z", now)).toBe("不足 1 分钟");
    expect(elapsedDurationLabel("2026-07-27T05:15:00Z", now)).toBe("15 分钟");
    expect(elapsedDurationLabel("2026-07-27T03:15:00Z", now)).toBe("2 小时 15 分钟");
    expect(elapsedDurationLabel("2026-07-25T03:30:00Z", now)).toBe("2 天 2 小时");
    expect(elapsedDurationLabel("UNKNOWN", now)).toBe("未知");
  });

  it("bounds window progress and formats the remaining duration", () => {
    const start = "2026-07-26T04:00:00Z";
    const end = "2026-07-26T06:00:00Z";
    expect(boundedProgressPercent(start, end, Date.parse("2026-07-26T05:00:00Z")))
      .toBe(50);
    expect(boundedProgressPercent(start, end, Date.parse("2026-07-26T07:00:00Z")))
      .toBe(100);
    expect(remainingTimeLabel(end, Date.parse("2026-07-26T05:00:00Z")))
      .toBe("剩余 1 小时");
    expect(remainingTimeLabel(end, Date.parse("2026-07-26T07:00:00Z")))
      .toBe("已到期");
  });

  it("uses the first real submission and frozen expiry rule for the remaining entry deadline", () => {
    const deadline = runtimeEntryOrderDeadline(
      [
        {
          action_kind: "ENTRY",
          state: "NOT_SUBMITTED",
          created_at: "2026-07-28T07:40:00Z",
        },
        {
          action_kind: "ENTRY",
          state: "OPEN",
          client_order_id: "entry-1",
          created_at: "2026-07-28T07:45:14Z",
          call_started_at: "2026-07-28T07:45:15Z",
          action_terms: {
            execution_context: {
              dynamic_rules: [{
                kind: "EXPIRE_REMAINING",
                after_seconds: 2700,
              }],
            },
          },
        },
      ],
      {
        dynamic_rules: [{
          kind: "EXPIRE_REMAINING",
          after_seconds: 300,
        }],
      },
      "2026-07-28T08:45:12Z",
    );

    expect(deadline).toEqual({
      submittedAt: "2026-07-28T07:45:15Z",
      ruleExpiresAt: "2026-07-28T08:30:15.000Z",
      effectiveDeadlineAt: "2026-07-28T08:30:15.000Z",
      expireAfterSeconds: 2700,
      limitedByPlanValidity: false,
    });
  });

  it("shows the plan validity as the effective limit when it ends before the expiry rule", () => {
    const deadline = runtimeEntryOrderDeadline(
      [{
        action_kind: "ENTRY",
        state: "OPEN",
        call_started_at: "2026-07-28T07:45:15Z",
        action_terms: {
          execution_context: {
            dynamic_rules: [{
              kind: "EXPIRE_REMAINING",
              after_seconds: 7200,
            }],
          },
        },
      }],
      {},
      "2026-07-28T08:45:12Z",
    );

    expect(deadline?.ruleExpiresAt).toBe("2026-07-28T09:45:15.000Z");
    expect(deadline?.effectiveDeadlineAt).toBe("2026-07-28T08:45:12.000Z");
    expect(deadline?.limitedByPlanValidity).toBe(true);
  });

  it("derives plan-list close reasons from attributed completion facts", () => {
    expect(activationSummaryCloseReason({
      lifecycle: "COMPLETED",
      primary_result: "NO_ACTION",
      closure_reason_code: "ENTRY_MARKET_INVALIDATED",
    })).toBe("行情失效，取消入场");
    expect(activationSummaryCloseReason({
      lifecycle: "COMPLETED",
      primary_result: "COMPLETED",
      trade_result: {
        fills: [
          { action_kind: "ENTRY" },
          { action_kind: "PROTECTION" },
        ],
      },
    })).toBe("保护止损");
    expect(activationSummaryCloseReason({
      lifecycle: "RUNNING",
    })).toBe("仍在运行");
    expect(activationSummaryCloseReason({
      lifecycle: "COMPLETED",
      primary_result: "NO_ACTION",
      closure_reason_code: "EXIT_STRATEGY",
    })).toBe("计划退出");
  });
});
