import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type TestInfo } from "@playwright/test";
import type { ReviewSequenceEvidence } from "../src/api/client";

type JsonRecord = Record<string, unknown>;

function tradeResult({
  entry,
  exit,
  netPnl,
  grossPnl,
  commission,
  firstFillTime,
  lastFillTime,
}: {
  entry: string;
  exit: string;
  netPnl: string;
  grossPnl: string;
  commission: string;
  firstFillTime: string;
  lastFillTime: string;
}) {
  return {
    fill_count: 2,
    calculation_complete: true,
    commission_complete: true,
    execution_cost_complete: true,
    funding_complete: true,
    funding_included: true,
    funding: "0",
    strategy_attribution_complete: true,
    closed: true,
    gross_pnl: grossPnl,
    commission,
    net_pnl: netPnl,
    average_entry_price: entry,
    average_exit_price: exit,
    entry_notional: "100.00",
    holding_duration_seconds: (Date.parse(lastFillTime) - Date.parse(firstFillTime)) / 1000,
    fill_times_complete: true,
    first_fill_time: firstFillTime,
    last_fill_time: lastFillTime,
    fills: [
      {
        trade_id: `entry-${firstFillTime}`,
        action_kind: "ENTRY",
        quantity: "1",
        price: entry,
        notional: "100.00",
        liquidity_side: "2",
        fee: "0.06",
        fee_currency: "USDT",
        fill_time: firstFillTime,
      },
      {
        trade_id: `exit-${lastFillTime}`,
        action_kind: "TAKE_PROFIT",
        quantity: "1",
        price: exit,
        notional: exit,
        liquidity_side: "1",
        fee: "0.04",
        fee_currency: "USDT",
        fill_time: lastFillTime,
      },
    ],
  };
}

const professionalThreeTradeResult = tradeResult({
  entry: "100",
  exit: "102.5",
  netPnl: "2.4",
  grossPnl: "2.5",
  commission: "0.1",
  firstFillTime: "2026-07-21T00:20:00Z",
  lastFillTime: "2026-07-21T00:25:30Z",
});
const externalClosureTradeResult = {
  ...tradeResult({
    entry: "101",
    exit: "100",
    netPnl: "-0.9",
    grossPnl: "-0.8",
    commission: "0.1",
    firstFillTime: "2026-07-21T00:10:00Z",
    lastFillTime: "2026-07-21T00:15:30Z",
  }),
  result_scope: "ACCOUNT_FACTS_WITH_EXTERNAL_CLOSURE",
};
externalClosureTradeResult.strategy_attribution_complete = false;
externalClosureTradeResult.fills[1].action_kind = "EXTERNAL_ACCOUNT_CLOSURE";
const legacyProfessionalThreeTradeResult = {
  calculation_complete: professionalThreeTradeResult.calculation_complete,
  closed: professionalThreeTradeResult.closed,
  gross_pnl: professionalThreeTradeResult.gross_pnl,
  commission: professionalThreeTradeResult.commission,
  net_pnl: professionalThreeTradeResult.net_pnl,
  average_entry_price: professionalThreeTradeResult.average_entry_price,
};

const reviews: JsonRecord[] = [
  {
    review_id: "review-professional-3",
    review_version: 2,
    previous_version: 1,
    revision_reason: "OWNER_EVALUATION_CHANGED",
    activation_id: "activation-professional-3",
    status: "COMPLETE",
    primary_result: "COMPLETED",
    fact_cutoff: "2026-07-21T00:28:00Z",
    content_digest: "3".repeat(64),
    evidence_purpose: "SYSTEM_MECHANISM_EVIDENCE",
    input_refs: { plan_events: [{}], execution_actions: [{}, {}, {}], venue_facts: [{}, {}, {}, {}], commands_and_receipts: [] },
    open_responsibilities: { execution_action_refs: [], unknown_action_refs: [] },
    evaluations: { owner_conclusion: { result: "AS_EXPECTED", reason: "按计划止盈", evidence_refs: [] } },
    trade_context: { plan_name: "AI BTC 突破复核", instrument_ref: "BTCUSDT-PERP", direction: "LONG", strategy_id: "ONE_SHOT_DONCHIAN_ATR_BREAKOUT", trade_amount: "100" },
    account_result: { trade_result: legacyProfessionalThreeTradeResult },
    resolved_trade_result: professionalThreeTradeResult,
  },
  {
    review_id: "review-professional-2",
    review_version: 1,
    activation_id: "activation-professional-2",
    status: "COMPLETE",
    primary_result: "COMPLETED",
    fact_cutoff: "2026-07-21T00:18:00Z",
    content_digest: "2".repeat(64),
    input_refs: {},
    open_responsibilities: {},
    evaluations: { owner_conclusion: { result: "ISSUE_FOUND", reason: "突破后回落", evidence_refs: [] } },
    trade_context: { instrument_ref: "ETHUSDT-PERP", direction: "LONG", strategy_id: "MEAN_REVERSION_TEST", trade_amount: "100" },
    account_result: { trade_result: tradeResult({ entry: "101", exit: "100", netPnl: "-0.9", grossPnl: "-0.8", commission: "0.1", firstFillTime: "2026-07-21T00:10:00Z", lastFillTime: "2026-07-21T00:15:30Z" }) },
    resolved_trade_result: externalClosureTradeResult,
  },
  {
    review_id: "review-professional-1",
    review_version: 1,
    activation_id: "activation-professional-1",
    status: "COMPLETE",
    primary_result: "COMPLETED",
    fact_cutoff: "2026-07-21T00:08:00Z",
    content_digest: "1".repeat(64),
    input_refs: {},
    open_responsibilities: {},
    evaluations: { owner_conclusion: { result: "AS_EXPECTED", reason: "保护退出正常", evidence_refs: [] } },
    trade_context: { instrument_ref: "BTCUSDT-PERP", direction: "SHORT", strategy_id: "ONE_SHOT_DONCHIAN_ATR_BREAKOUT", trade_amount: "100" },
    account_result: { trade_result: tradeResult({ entry: "102", exit: "100", netPnl: "1.8", grossPnl: "1.9", commission: "0.1", firstFillTime: "2026-07-21T00:00:00Z", lastFillTime: "2026-07-21T00:05:30Z" }) },
    resolved_trade_result: tradeResult({ entry: "102", exit: "100", netPnl: "1.8", grossPnl: "1.9", commission: "0.1", firstFillTime: "2026-07-21T00:00:00Z", lastFillTime: "2026-07-21T00:05:30Z" }),
  },
];

function professionalSequenceEvidence(scope: ReviewSequenceEvidence["scope"]): ReviewSequenceEvidence {
  // These legacy reviews have no recorded pre-trade intent. Their reliable
  // account results remain visible without inventing profit-seeking eligibility.
  const accountScope = scope === "ACCOUNT_RESULTS";
  const cumulativePnl = ["1.8", "0.9", "3.3"];
  const trades: ReviewSequenceEvidence["trades"] = accountScope
    ? [...reviews].reverse().map((review, index) => {
      const result = review.resolved_trade_result as JsonRecord;
      const context = review.trade_context as JsonRecord;
      const owner = (review.evaluations as { owner_conclusion: JsonRecord }).owner_conclusion;
      return {
        review_id: String(review.review_id), review_version: Number(review.review_version),
        closed_at: String(result.last_fill_time), instrument_ref: String(context.instrument_ref),
        direction: context.direction as "LONG" | "SHORT", classification: String(owner.result),
        intent: null, result_kind: index === 1 ? "LOSS" : "WIN",
        net_pnl: String(result.net_pnl), commission: String(result.commission),
        entry_notional: String(Number(result.entry_notional)), notional_return_percent: String(result.net_pnl),
        cumulative_net_pnl: cumulativePnl[index], drawdown_from_peak: index === 1 ? "0.9" : "0",
        segment_index: index, price_path: null,
      };
    })
    : [];
  return {
    scope, range_start: null, range_end: null, source: "CURRENT_LATEST_REVIEWS",
    source_cutoff: "2026-07-21T00:28:00Z", source_review_count: 3,
    eligible_trade_count: trades.length, excluded_review_count: accountScope ? 0 : 3,
    exclusions: accountScope ? {} : { MISSING_DECISION_INTENT: 3 },
    metrics: {
      trade_count: trades.length, wins: accountScope ? 2 : 0, losses: accountScope ? 1 : 0, flat: 0,
      net_pnl: accountScope ? "3.3" : "0", commission: accountScope ? "0.3" : "0",
      gross_profit: accountScope ? "4.2" : "0", gross_loss: accountScope ? "0.9" : "0",
      profit_factor: accountScope ? "4.666666666666666666666666667" : null,
      total_entry_notional: accountScope ? "300" : null,
      notional_return_percent: accountScope ? "1.1" : null,
      maximum_drawdown: accountScope ? "0.9" : "0",
      maximum_drawdown_peak_review_id: accountScope ? "review-professional-1" : null,
      maximum_drawdown_peak_at: accountScope ? "2026-07-21T00:05:30Z" : null,
      maximum_drawdown_trough_review_id: accountScope ? "review-professional-2" : null,
      maximum_drawdown_trough_at: accountScope ? "2026-07-21T00:15:30Z" : null,
      current_segment_index: accountScope ? 2 : null,
      longest_win_segment_index: accountScope ? 2 : null,
      longest_loss_segment_index: accountScope ? 1 : null,
      best_win_segment_index: accountScope ? 2 : null,
      worst_loss_segment_index: accountScope ? 1 : null,
      worst_trade_review_id: accountScope ? "review-professional-2" : null,
      worst_trade_net_pnl: accountScope ? "-0.9" : null,
      net_pnl_without_worst_trade: accountScope ? "4.2" : null,
      largest_loss_share_percent: accountScope ? "100" : null,
      top_loss_count: accountScope ? 1 : 0, top_loss_total: accountScope ? "0.9" : null,
      net_pnl_without_top_losses: accountScope ? "4.2" : null,
      top_loss_share_percent: accountScope ? "100" : null,
    },
    segments: trades.map((trade) => ({
      segment_index: trade.segment_index, result_kind: trade.result_kind,
      start_at: trade.closed_at, end_at: trade.closed_at, trade_count: 1,
      net_pnl: trade.net_pnl, commission: trade.commission, total_entry_notional: trade.entry_notional,
      notional_return_percent: trade.notional_return_percent,
      review_refs: [{ review_id: trade.review_id, review_version: trade.review_version }], price_path: null,
    })),
    trades, price_path: null, capital_scaling_authority: false,
    limitations: ["确定性复盘界面样本；不形成实盘或资金授权。"],
  };
}

async function mockProfessionalReviews(page: Page) {
  await page.route("**/api/v1/review-sequence-evidence?**", async (route) => {
    const scope = new URL(route.request().url()).searchParams.get("scope");
    await route.fulfill({ json: professionalSequenceEvidence(
      scope === "PROFIT_SEEKING" ? "PROFIT_SEEKING" : "ACCOUNT_RESULTS",
    ) });
  });
  await page.route("**/api/v1/market-window**", async (route) => {
    const requestUrl = new URL(route.request().url());
    const interval = requestUrl.searchParams.get("interval") === "15m" ? "15m" : "1m";
    const intervalMs = interval === "15m" ? 15 * 60_000 : 60_000;
    const requestedStart = Date.parse(requestUrl.searchParams.get("start_at") ?? "");
    const startAt = Number.isFinite(requestedStart) ? requestedStart : Date.parse("2026-07-20T23:56:00Z");
    const bars = Array.from({ length: 48 }, (_value, index) => {
      const open = 99.4 + index * 0.07;
      const close = open + (index % 3 === 0 ? -0.12 : 0.16);
      return {
        open_at: new Date(startAt + index * intervalMs).toISOString(),
        close_at: new Date(startAt + (index + 1) * intervalMs).toISOString(),
        open: open.toFixed(2),
        high: (Math.max(open, close) + 0.18).toFixed(2),
        low: (Math.min(open, close) - 0.16).toFixed(2),
        close: close.toFixed(2),
        volume: String(100 + index),
      };
    });
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({
      instrument_ref: "BTCUSDT-PERP",
      interval,
      source: "BINANCE_DEMO_PUBLIC",
      source_cutoff: bars.at(-1)?.close_at,
      bars,
    }) });
  });
  await page.route("**/api/v1/reviews**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/reviews") {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(reviews) });
      return;
    }
    if (path === "/api/v1/reviews/review-professional-3") {
      const historical = {
        ...reviews[0],
        review_version: 1,
        previous_version: null,
        revision_reason: "INITIAL_DERIVATION",
        content_digest: "4".repeat(64),
        evaluations: { owner_conclusion: { result: "UNKNOWN", reason: "最初结论", evidence_refs: [] } },
      };
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ review: reviews[0], versions: [reviews[0], historical] }),
      });
      return;
    }
    await route.fallback();
  });
  await page.route("**/api/v1/activations/activation-professional-3**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/timeline")) {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify([{
        source: "PLAN_EVENT",
        source_ref: "plan-event-entry-3",
        status: "PROPOSED_ACTION_CAP_ACCEPTED",
        at: "2026-07-21T00:19:58Z",
        detail: { rule_id: "ENTRY_BREAKOUT", source_identity: "BTCUSDT 15m 闭合 K 线", source_cutoff: "2026-07-21T00:19:00Z" },
      }]) });
      return;
    }
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({
      activation: {
        activation_id: "activation-professional-3",
        lifecycle: "COMPLETED",
        result_ref: "review-professional-3",
        instrument_ref: "BTCUSDT-PERP",
        direction: "LONG",
        created_at: "2026-07-21T00:00:00Z",
        updated_at: "2026-07-21T00:28:00Z",
      },
      plan: { plan_name: "AI BTC 突破复核", creator_kind: "AI", created_at: "2026-07-21T00:00:00Z" },
      decision_basis: {
        kind: "STRATEGY_SIGNAL",
        decision_basis_ref: "ONE_SHOT_DONCHIAN_ATR_BREAKOUT@1",
      },
      strategy: { strategy_ref: "ONE_SHOT_DONCHIAN_ATR_BREAKOUT@1" },
      trade_result: professionalThreeTradeResult,
      execution_actions: [
        { action_kind: "PROTECTION", state: "CLOSED", action_terms: { trigger_price: "98.5" } },
        { action_kind: "TAKE_PROFIT", state: "CLOSED", action_terms: { trigger_price: "102.5" } },
        { action_kind: "TAKE_PROFIT", state: "NOT_SUBMITTED", action_terms: { trigger_price: "104" } },
      ],
    }) });
  });
}

async function mockStageReviews(page: Page) {
  const stageReviews: JsonRecord[] = [];
  await page.route("**/api/v1/stage-reviews", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(stageReviews),
      });
      return;
    }
    const payload = route.request().postDataJSON() as JsonRecord;
    const created = {
      stage_review_id: "stage-review-1",
      environment_id: "binance-demo-primary",
      title: payload.title,
      range_start: payload.range_start,
      range_end: payload.range_end,
      source_review_refs: reviews.map((review) => ({
        review_id: review.review_id,
        review_version: review.review_version,
      })),
      metrics_snapshot: {
        review_count: 3,
        reliable_trade_count: 3,
        pending_evaluation_count: 0,
        net_pnl: "3.3",
        commission: "0.3",
        total_entry_notional: "300",
        notional_return_percent: "1.1",
      },
      problem_analysis: payload.problem_analysis,
      improvement_plan: payload.improvement_plan,
      creator_kind: payload.creator_kind,
      created_at: "2026-07-21T00:30:00Z",
    };
    stageReviews.unshift(created);
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify(created),
    });
  });
}

async function mockDirectExecutionReviews(page: Page) {
  const directTradeResult = tradeResult({
    entry: "100",
    exit: "101",
    netPnl: "0.8",
    grossPnl: "1",
    commission: "0.2",
    firstFillTime: "2026-07-23T03:19:21Z",
    lastFillTime: "2026-07-23T03:20:14Z",
  });
  directTradeResult.fills[1].action_kind = "EXIT";
  const incompleteTradeResult = {
    fill_count: 0,
    fills: [],
    position_quantity: "0",
    average_entry_price: null,
    average_exit_price: null,
    entry_notional: "0",
    commission: "0",
    commission_complete: false,
    calculation_complete: false,
    closed: false,
    gross_pnl: null,
    net_pnl: null,
    fill_times_complete: false,
    first_fill_time: null,
    last_fill_time: null,
    holding_duration_seconds: null,
    unresolved_refs: [],
  };
  const directReviews: JsonRecord[] = [
    {
      review_id: "review-direct-complete",
      review_version: 1,
      activation_id: "activation-direct-complete",
      status: "DRAFT",
      primary_result: "COMPLETED",
      fact_cutoff: "2026-07-23T03:20:15Z",
      content_digest: "d".repeat(64),
      input_refs: { plan_events: [{}, {}], execution_actions: [{}, {}], venue_facts: [{}, {}, {}, {}], commands_and_receipts: [] },
      open_responsibilities: { execution_action_refs: [], unknown_action_refs: [] },
      evaluations: { owner_conclusion: { result: "UNKNOWN", reason: "", evidence_refs: [] } },
      trade_context: {
        plan_name: "直接执行完整成交",
        instrument_ref: "BTCUSDT-PERP",
        direction: "LONG",
        decision_basis_ref: "DIRECT_EXECUTION@1",
        strategy_id: null,
        trade_amount: "100",
      },
      account_result: { trade_result: directTradeResult },
      resolved_trade_result: directTradeResult,
    },
    {
      review_id: "review-direct-partial",
      review_version: 1,
      activation_id: "activation-direct-partial",
      status: "DRAFT",
      primary_result: "PARTIAL",
      fact_cutoff: "2026-07-23T03:18:40Z",
      content_digest: "p".repeat(64),
      input_refs: { plan_events: [{}], execution_actions: [{}], venue_facts: [], commands_and_receipts: [] },
      open_responsibilities: { execution_action_refs: ["entry-open"], unknown_action_refs: [] },
      evaluations: { owner_conclusion: { result: "UNKNOWN", reason: "", evidence_refs: [] } },
      trade_context: {
        plan_name: "直接执行责任待闭合",
        instrument_ref: "BTCUSDT-PERP",
        direction: "LONG",
        decision_basis_ref: "DIRECT_EXECUTION@1",
        strategy_id: null,
        trade_amount: "100",
      },
      account_result: { trade_result: incompleteTradeResult },
      resolved_trade_result: incompleteTradeResult,
    },
    {
      review_id: "review-direct-no-action",
      review_version: 1,
      activation_id: "activation-direct-no-action",
      status: "COMPLETE",
      primary_result: "NO_ACTION",
      fact_cutoff: "2026-07-23T03:40:00Z",
      content_digest: "n".repeat(64),
      input_refs: { plan_events: [], execution_actions: [], venue_facts: [], commands_and_receipts: [] },
      open_responsibilities: { execution_action_refs: [], unknown_action_refs: [] },
      evaluations: { owner_conclusion: { result: "NOT_APPLICABLE", reason: "", evidence_refs: [] } },
      trade_context: {
        plan_name: "直接执行未发生交易",
        instrument_ref: "BTCUSDT-PERP",
        direction: "LONG",
        decision_basis_ref: "DIRECT_EXECUTION@1",
        activation_updated_at: "2026-07-23T03:16:00Z",
        strategy_id: null,
        trade_amount: "100",
      },
      account_result: { trade_result: incompleteTradeResult },
      resolved_trade_result: incompleteTradeResult,
    },
  ];
  await page.route("**/api/v1/market-window**", async (route) => {
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({
      instrument_ref: "BTCUSDT-PERP",
      interval: "1m",
      source: "BINANCE_DEMO_PUBLIC",
      source_cutoff: "2026-07-23T03:30:00Z",
      bars: [],
    }) });
  });
  await page.route("**/api/v1/reviews**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/reviews") {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(directReviews) });
      return;
    }
    if (path === "/api/v1/reviews/review-direct-complete") {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ review: directReviews[0] }) });
      return;
    }
    await route.fallback();
  });
  await page.route("**/api/v1/activations/activation-direct-complete**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/timeline")) {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify([0, 1].map((index) => ({
        source: "PLAN_EVENT",
        source_ref: `direct-leg-event-${index}`,
        status: "PROPOSED_ACTION_CAP_ACCEPTED",
        at: "2026-07-23T03:19:20Z",
        detail: {
          rule_id: "DIRECT_ORDER_SCHEDULE_LEG",
          source_identity: `activation-direct-complete:ORDER_SCHEDULE:digest:LEG:${index}`,
          source_cutoff: "2026-07-23T03:19:20Z",
        },
      }))) });
      return;
    }
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({
      activation: {
        activation_id: "activation-direct-complete",
        lifecycle: "COMPLETED",
        result_ref: "review-direct-complete",
        instrument_ref: "BTCUSDT-PERP",
        direction: "LONG",
        created_at: "2026-07-23T03:18:00Z",
        decision_basis_ref: "DIRECT_EXECUTION@1",
        updated_at: "2026-07-23T03:20:15Z",
      },
      plan: { plan_name: "直接执行完整成交", creator_kind: "AI", created_at: "2026-07-23T03:18:00Z" },
      decision_basis: {
        kind: "DIRECT_EXECUTION",
        decision_basis_ref: "DIRECT_EXECUTION@1",
      },
      strategy: null,
      trade_result: directTradeResult,
      execution_actions: [],
    }) });
  });
}

async function assertAccessible(page: Page, testInfo: TestInfo, name: string) {
  const violations = (await new AxeBuilder({ page }).analyze()).violations;
  await testInfo.attach(`${name}-axe.json`, { body: Buffer.from(JSON.stringify(violations, null, 2)), contentType: "application/json" });
  expect(violations).toEqual([]);
}

async function chooseFilter(page: Page, label: string, option: string) {
  await page.getByRole("combobox", { name: label, exact: true }).click();
  await page.getByRole("option", { name: option, exact: true }).click();
  await page.getByRole("listbox").waitFor({ state: "detached" });
  await expect(page.locator('[role="option"]')).toHaveCount(0);
}

test("review records support conjunctive strategy, instrument, direction, pnl, result and conclusion filters", async ({ page }, testInfo) => {
  await mockProfessionalReviews(page);
  await page.goto("/reviews");

  await chooseFilter(page, "决策依据 / 策略", "MEAN_REVERSION_TEST");
  await chooseFilter(page, "交易对象", "ETHUSDT-PERP");
  await chooseFilter(page, "方向", "做多");
  await chooseFilter(page, "盈亏", "亏损");
  await chooseFilter(page, "交易结果", "已完成交易");
  await chooseFilter(page, "复盘分类", "旧分类：问题未分类");

  const table = page.getByRole("table", { name: "交易与复盘记录" });
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await expect(table).toContainText("ETHUSDT-PERP");
  await expect(table).toContainText("-0.90 USDT");
  await expect(page.getByText("条件同时满足 · 匹配 1 / 3 条 · 已选 6 项", { exact: true })).toBeVisible();
  const layout = await page.evaluate(() => ({ clientWidth: document.documentElement.clientWidth, scrollWidth: document.documentElement.scrollWidth }));
  expect(layout.scrollWidth).toBe(layout.clientWidth);
  await assertAccessible(page, testInfo, `review-filters-${testInfo.project.name}`);
  const filterScreenshot = testInfo.outputPath(`review-filters-${testInfo.project.name}.png`);
  await page.screenshot({ path: filterScreenshot, fullPage: true });
  await testInfo.attach(`review-filters-${testInfo.project.name}.png`, { path: filterScreenshot, contentType: "image/png" });

  await chooseFilter(page, "方向", "做空");
  await expect(page.getByText("当前筛选下没有复盘记录。")).toBeVisible();
  await expect(table.locator("tbody tr")).toHaveCount(0);

  await page.getByRole("button", { name: "重置筛选" }).click();
  await expect(table.locator("tbody tr")).toHaveCount(3);
  await expect(page.getByText("条件同时满足 · 匹配 3 / 3 条", { exact: true })).toBeVisible();
  const keyboardReviewRow = table.locator("tbody tr").first();
  await keyboardReviewRow.focus();
  await expect(keyboardReviewRow).toBeFocused();
  await page.keyboard.press(" ");
  await expect(page.getByRole("heading", { name: "复盘与结果" })).toBeVisible();
});

test("direct-execution reviews use their authoritative basis and keep incomplete facts distinct from zero", async ({ page }, testInfo) => {
  await mockDirectExecutionReviews(page);
  await page.goto("/reviews");

  const table = page.getByRole("table", { name: "交易与复盘记录" });
  const completeRow = table.locator("tbody tr").filter({ hasText: "2026-07-23 11:20:14" });
  await expect(completeRow.locator("td").nth(2)).toContainText("直接执行订单计划");
  const partialRow = table.locator("tbody tr").filter({ hasText: "2026-07-23 11:18:40" });
  await expect(partialRow.locator("td").nth(2)).toContainText("直接执行订单计划");
  await expect(partialRow.locator("td").nth(3)).toHaveText("未知 / 未知");
  await expect(partialRow.locator("td").nth(7)).toContainText("未知");
  await expect(partialRow.locator("td").nth(7)).toContainText("1 个责任尚未闭合");
  await expect(partialRow.locator("td").nth(7)).not.toContainText("0 秒");

  await page.getByRole("tab", { name: "全部记录（3）" }).click();
  await expect(table.locator("tbody tr").first()).toHaveAttribute(
    "aria-label",
    /查看 BTCUSDT-PERP 做多 2026-07-23 11:20:14/,
  );
  const noActionRow = table.locator("tbody tr").filter({ hasText: "2026-07-23 11:16:00" });
  await expect(noActionRow.locator("td").nth(3)).toHaveText("不适用");
  await expect(noActionRow.locator("td").nth(4)).toHaveText("不适用");
  await expect(noActionRow.locator("td").nth(5)).toHaveText("不适用");
  await expect(noActionRow.locator("td").nth(6)).toHaveText("不适用");

  await completeRow.click();
  await expect(page.getByRole("heading", { name: "直接执行完整成交" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "复盘与结果" })).toBeVisible();
  await expect(page.getByText("操作理由", { exact: true }).locator("..")).toContainText("直接执行订单计划的 2 个入场档位已通过资金检查");
  await expect(page.getByText("触发来源", { exact: true }).locator("..")).toContainText("直接执行订单计划");
  await expect(page.getByText("触发来源", { exact: true }).locator("..")).toContainText("DIRECT_EXECUTION@1");
  await assertAccessible(page, testInfo, `review-direct-${testInfo.project.name}`);
});

test("review workbench exposes full-history performance and one visual trade narrative", async ({ page }, testInfo) => {
  await mockProfessionalReviews(page);
  await page.goto("/reviews");

  await expect(page.getByRole("group", { name: "全部已闭合交易累计净盈亏趋势" })).toBeVisible();
  await expect(page.getByText("账户累计净盈亏", { exact: true }).locator("..")).toContainText("+3.30 USDT");
  await expect(page.getByRole("table", { name: "交易与复盘记录" })).toContainText("-0.90 USDT");
  await expect(page.getByRole("table", { name: "交易与复盘记录" })).toContainText("按计划止盈");
  await expect(page.getByRole("table", { name: "交易与复盘记录" })).toContainText("AI BTC 突破复核");
  await expect(page.getByRole("table", { name: "交易与复盘记录" })).toContainText("账户结果");
  await expect(page.getByRole("table", { name: "交易与复盘记录" })).toContainText("外部应急平仓");
  const firstTradeRow = page.getByRole("table", { name: "交易与复盘记录" }).locator("tbody tr").first();
  await expect(firstTradeRow.locator("td").nth(3)).toHaveText("100.00 / 102.50");
  await expect(firstTradeRow.locator("td").nth(4)).toHaveText("100.00 USDT");
  const listScreenshot = testInfo.outputPath(`review-list-${testInfo.project.name}.png`);
  await page.screenshot({ path: listScreenshot, fullPage: true });
  await testInfo.attach(`review-list-${testInfo.project.name}.png`, { path: listScreenshot, contentType: "image/png" });

  await page.getByRole("table", { name: "交易与复盘记录" }).locator("tbody tr").first().click();
  await expect(page.getByRole("heading", { name: "AI BTC 突破复核" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "复盘与结果" })).toBeVisible();
  await expect(page.getByRole("group", { name: /15m K 线运行主图/ })).toBeVisible();
  const compactIntervalPicker = page.getByRole("combobox", { name: "K 线周期" });
  if (await compactIntervalPicker.count()) {
    await chooseFilter(page, "K 线周期", "1m");
  } else {
    await page.getByRole("button", { name: "1m", exact: true }).click();
  }
  await expect(page.getByRole("group", { name: /1m K 线运行主图/ })).toBeVisible();
  if (await compactIntervalPicker.count()) {
    await chooseFilter(page, "K 线周期", "15m");
  } else {
    await page.getByRole("button", { name: "15m", exact: true }).click();
  }
  await expect(page.getByRole("group", { name: /15m K 线运行主图/ })).toBeVisible();
  await expect(page.getByText("平均入场价", { exact: true }).locator("..")).toContainText("100.00 USDT");
  await expect(page.getByText("平均出场价", { exact: true }).locator("..")).toContainText("102.50 USDT");
  await expect(page.getByText("持仓周期", { exact: true }).locator("..")).toContainText("5 分钟 30 秒");
  await expect(page.getByRole("table", { name: "本次复盘成交明细" })).toContainText("吃单");
  await expect(page.getByRole("table", { name: "本次复盘成交明细" })).toContainText("挂单");
  await expect(page.getByRole("heading", { name: "机器为何交易" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "复盘判断" })).toBeVisible();
  const layout = await page.evaluate(() => ({ clientWidth: document.documentElement.clientWidth, scrollWidth: document.documentElement.scrollWidth }));
  expect(layout.scrollWidth).toBe(layout.clientWidth);
  await page.getByTestId("order-schedule-chart-market-source").hover();
  await expect(page.getByRole("main").getByRole("tooltip", { name: /图表历史、实时 K 线和当前价/ })).toBeVisible();
  await assertAccessible(page, testInfo, `review-detail-${testInfo.project.name}`);
  const detailScreenshot = testInfo.outputPath(`review-detail-${testInfo.project.name}.png`);
  await page.screenshot({ path: detailScreenshot, fullPage: true });
  await testInfo.attach(`review-detail-${testInfo.project.name}.png`, { path: detailScreenshot, contentType: "image/png" });
});

test("stage reviews freeze a selected historical range and remain separate from live metrics", async ({ page }, testInfo) => {
  await mockProfessionalReviews(page);
  await mockStageReviews(page);
  await page.goto("/reviews");
  await page.evaluate(() => {
    document.cookie = `halpha_csrf_${window.location.port}=e2e-stage-review; path=/; SameSite=Strict`;
  });

  await expect(page.getByText("账户累计净回报", { exact: true })).toBeVisible();
  await expect(page.getByText("+1.10%", { exact: true })).toBeVisible();
  await expect(page.getByText("账户累计手续费", { exact: true })).toBeVisible();
  await expect(page.getByText("当前闭合样本归因", { exact: true })).toHaveCount(0);

  await page.getByRole("tab", { name: "阶段性复盘" }).click();
  await expect(page.getByRole("heading", { name: "阶段性复盘" })).toBeVisible();
  await expect(page.getByText(/创建后不会随新交易自动变化/)).toBeVisible();
  await expect(page.getByRole("table", { name: "交易与复盘记录" })).toBeHidden();
  await page.getByRole("button", { name: "新建阶段性复盘" }).click();

  await page.getByLabel("标题").fill("近期执行质量复盘");
  await page.getByLabel("创建者").click();
  await page.getByRole("option", { name: "AI 创建" }).click();
  await page.getByLabel("问题判断").fill("费用占比偏高，且一次外部闭合不进入策略归因。");
  await page.getByLabel("改进方案").fill("下一阶段固定 Maker 入场，并单独验证退出滑点。");
  await page.getByRole("button", { name: "保存阶段性复盘" }).click();

  const stageCard = page.getByRole("article").filter({ hasText: "近期执行质量复盘" });
  await expect(stageCard).toBeVisible();
  await expect(stageCard).toContainText("3 个计划");
  await expect(stageCard).toContainText("AI 创建");
  await expect(stageCard).toContainText("+3.30 USDT");
  await expect(stageCard).toContainText("+1.10%");
  await expect(stageCard).toContainText("累计手续费");
  await stageCard.getByText("查看问题判断与改进方案").click();
  await expect(stageCard).toContainText("费用占比偏高");
  await expect(stageCard).toContainText("固定 Maker 入场");

  const layout = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(layout.scrollWidth).toBe(layout.clientWidth);
  await assertAccessible(page, testInfo, `stage-review-${testInfo.project.name}`);
  const screenshot = testInfo.outputPath(`stage-review-${testInfo.project.name}.png`);
  await page.screenshot({ path: screenshot, fullPage: true });
  await testInfo.attach(`stage-review-${testInfo.project.name}.png`, {
    path: screenshot,
    contentType: "image/png",
  });
});
