import { expect, test as base, type Page, type WebSocketRoute } from "@playwright/test";
import { AxeBuilder } from "@axe-core/playwright";
import type { components } from "../src/api/schema.js";

// All account facts, responses and public market events in this suite are synthetic.
// The local server provides static assets only; unmatched API calls fail closed.
const environmentId = "scalp-e2e-demo";
const accountId = "scalp-e2e-account";
const templateKey = `halpha.scalp-template.v1:${environmentId}:${accountId}`;
const template = {
  schema_version: "HALPHA_SCALP_TEMPLATE_V1", notional: "100", entry_mode: "MARKET",
  initial_stop_bps: "35", take_profit_r: "1.5", max_holding_seconds: 300,
  max_spread_bps: "5", entry_fee_bps: "6", exit_fee_bps: "6",
};

function openCycle(direction = "LONG") {
  return {
    cycle_id: "synthetic-cycle", activation_id: "synthetic-activation", activation_state_version: 3,
    instrument_ref: "BTCUSDT-PERP", direction, triggered_at: new Date().toISOString(),
    lifecycle: "ACTIVE", result_status: "OPEN", gross_pnl: null, commission: null,
    funding: null, net_pnl: null, currency: "USDT", entry_notional: null, holding_duration_seconds: null,
  };
}

function receipt(state: string, reasonCode: string | null = null) {
  const now = new Date().toISOString();
  return {
    receipt_id: "synthetic-receipt", command_id: "synthetic-command", content_digest: "a".repeat(64),
    state, reason_code: reasonCode, state_version: 1, result: null,
    processing_owner: "local-owner", pending_responsibility_refs: [], created_at: now, updated_at: now,
  };
}

type Submitted = { key: string; body: Record<string, unknown> };
type Harness = {
  executor: string;
  account: string;
  profile: string;
  flowEnabled: boolean;
  statusReads: number;
  historyReads: number;
  connections: number;
  closes: number;
  triggerRequests: Submitted[];
  triggerLookups: string[];
  exitRequests: Submitted[];
  exitLookups: string[];
  triggerResponse: "ABORT" | "CREATED";
  triggerLookupFound: boolean;
  exitResponse: ReturnType<typeof receipt> | "ABORT";
  exitLookupResponse: ReturnType<typeof receipt> | null;
  latest: ReturnType<typeof openCycle> | null;
};

const test = base.extend<{ scalp: Harness }>({
  scalp: async ({ page }, use) => {
    const harness: Harness = {
      executor: "READY", account: accountId, profile: "BINANCE_DEMO", flowEnabled: true,
      statusReads: 0, historyReads: 0, connections: 0, closes: 0,
      triggerRequests: [], triggerLookups: [], exitRequests: [], exitLookups: [],
      triggerResponse: "CREATED", triggerLookupFound: false, exitResponse: receipt("APPLIED"),
      exitLookupResponse: null, latest: null,
    };
    const unexpected: string[] = [];
    const timers = new Set<ReturnType<typeof setInterval>>();
    const sockets = new Set<WebSocketRoute>();
    let active = true;
    await page.addInitScript(() => {
      const port = location.port || (location.protocol === "https:" ? "443" : "80");
      document.cookie = `halpha_csrf_${port}=synthetic-scalp-csrf; path=/; SameSite=Strict`;
    });
    const created = (request: Submitted) => ({
      cycle: {
        cycle_id: "synthetic-cycle", activation_id: "synthetic-activation", plan_id: "synthetic-plan",
        plan_version_id: "synthetic-plan-version", environment_id: environmentId, account_ref: harness.account,
        instrument_ref: request.body.instrument_ref, direction: request.body.direction, template: request.body.template,
        idempotency_key: request.key, request_digest: "a".repeat(64), template_digest: "b".repeat(64),
        triggered_at: new Date().toISOString(),
      },
      activation: { activation_id: "synthetic-activation", state_version: 3, lifecycle: "ACTIVE" },
      venue_write_created: false, runtime_real_write_gate: "CLOSED",
    });

    await page.route("**/api/**", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const path = url.pathname;
      const now = new Date().toISOString();
      const source = harness.profile === "BINANCE_DEMO" ? "BINANCE_DEMO_PUBLIC" : "BINANCE_LIVE_PUBLIC";
      const venueAccountType = harness.profile === "BINANCE_DEMO" ? "USDM_DEMO" : "USDM_PERSONAL";
      const respond = (json: unknown) => route.fulfill({ contentType: "application/json", json });
      if (path === "/api/v1/settings/status" && request.method() === "GET") {
        harness.statusReads += 1;
        const status = {
          environment_kind: harness.profile === "BINANCE_DEMO" ? "DEMO" : "LIVE", environment_id: environmentId, account_id: harness.account,
          venue_account_type: venueAccountType, profile: harness.profile,
          authority_class: harness.profile === "BINANCE_DEMO" ? "DEMO_VALIDATION" : harness.profile === "BINANCE_LIVE_WRITE" ? "LIVE_REAL_CAPITAL" : "NO_TRADING_AUTHORITY",
          bind: "127.0.0.1", port: Number(url.port),
          trading_contexts: [{ venue_account_type: venueAccountType, environment_id: environmentId,
            account_id: harness.account, url: new URL("/scalping", url).href }], database_name: "synthetic-scalp",
          database_available: true, database_reason_code: null, server_fact_cutoff: now,
          product_build_id: "a".repeat(64), executor_status: harness.executor,
          app_executor_product_build_consistent: true, executor_status_checked_at: now,
          configured_runtime_real_write_gate: "CLOSED", runtime_real_write_gate: harness.profile === "BINANCE_LIVE_WRITE" ? "OPEN" : "CLOSED",
          live_write_gate_violations: [], authorized_activation_ids: [], email_delivery_enabled: false,
          email_configuration_status: "DISABLED", view_retrieved_at: now,
        } satisfies components["schemas"]["SettingsStatusResponse"];
        return respond(status);
      }
      if (path === "/api/v1/scalping/contracts") return respond({
        source, source_cutoff: now,
        contracts: [{ symbol: "BTCUSDT", instrument_ref: "BTCUSDT-PERP", base_asset: "BTC", quote_asset: "USDT", contract_type: "PERPETUAL" }],
      });
      if (path === "/api/v1/scalping/results") return respond({
        environment_id: environmentId, account_ref: harness.account, scope: url.searchParams.get("scope"),
        range_start: url.searchParams.get("anchor_at"), range_end: now, fact_cutoff: now, latest_cycle: harness.latest,
        aggregate: {
          reliable_trade_count: 0, win_count: 0, loss_count: 0, flat_count: 0, win_rate: null,
          open_cycle_count: harness.latest ? 1 : 0, unknown_result_count: 0, no_trade_cycle_count: 0,
          gross_pnl: null, commission: null, funding: null, net_pnl: null,
        },
      });
      if (path === "/api/v1/order-schedules/preview" && request.method() === "POST") {
        const body = request.postDataJSON();
        const entryPrice = body.spec.price_distribution.limit_price ?? "100";
        const quantity = String(Number(body.max_notional) / Number(entryPrice));
        const leg = { leg_index: 0, leg_count: 1, release_after_seconds: 0, raw_price: entryPrice,
          price: entryPrice, sizing_price: entryPrice, requested_notional: body.max_notional,
          quantity, effective_notional: body.max_notional };
        return respond({
          valid: true, compiler_version: "synthetic-preview", schedule_ref: body.schedule_ref,
          schedule_digest: "a".repeat(64), schedule_spec: body.spec, preprotected_parallel_supported: true,
          venue_ref: "BINANCE_USDM", instrument_ref: body.instrument_ref, direction: body.direction,
          max_notional: body.max_notional, reference_price: entryPrice,
          instrument_rules: {
            source: "SYNTHETIC_RULES", source_cutoff: now, min_price: "0.01", max_price: "1000000",
            price_tick_size: "0.01", limit_quantity_step: "0.001", min_limit_quantity: "0.001",
            max_limit_quantity: "1000", market_quantity_step: "0.001", min_market_quantity: "0.001",
            max_market_quantity: "1000", min_notional: "5",
          },
          instrument_rules_digest: "b".repeat(64), source_cutoff: now,
          requested_total_notional: body.max_notional, effective_total_notional: body.max_notional,
          full_fill_protection_estimate: { average_entry_price: entryPrice, entry_boundary_price: entryPrice,
            stop_price: body.direction === "LONG" ? "99.65" : "100.35", quantity,
            gross_price_loss: "0.35", estimated_entry_fee: "0.06", estimated_exit_fee: "0.06", maximum_projected_loss: "0.47" },
          normalized_legs: [leg], legs: [leg], issues: [],
        });
      }
      if (path === "/api/v1/market-window") {
        harness.historyReads += 1;
        const interval = url.searchParams.get("interval") ?? "1m";
        const end = Math.floor(Date.now() / 60_000) * 60_000;
        return respond({ instrument_ref: "BTCUSDT-PERP", interval, source, source_cutoff: now,
          bars: Array.from({ length: 32 }, (_, index) => ({
            open_at: new Date(end - (32 - index) * 60_000).toISOString(),
            close_at: new Date(end - (31 - index) * 60_000).toISOString(),
            open: "100", high: "100.2", low: "99.8", close: "100.01", volume: "12",
          })),
        });
      }
      if (path === "/api/v1/scalping/cycles" && request.method() === "POST") {
        const submitted = { key: request.headers()["idempotency-key"] ?? "", body: request.postDataJSON() };
        harness.triggerRequests.push(submitted);
        if (harness.triggerResponse === "ABORT") return route.abort("failed");
        harness.latest = openCycle(String(submitted.body.direction));
        return respond(created(submitted));
      }
      if (path === "/api/v1/scalping/cycles/by-idempotency" && request.method() === "GET") {
        harness.triggerLookups.push(url.searchParams.get("idempotency_key") ?? "");
        const original = harness.triggerRequests[0];
        if (!harness.triggerLookupFound || !original) return respond(null);
        harness.latest = openCycle(String(original.body.direction));
        return respond(created(original));
      }
      if (path === "/api/v1/activations/synthetic-activation") return respond({
        activation: { activation_id: "synthetic-activation", state_version: 3, lifecycle: "ACTIVE" },
        execution_actions: [], venue_facts: [], receipts: [],
      });
      if (path === "/api/v1/activations/synthetic-activation/control-preview") return respond({
        activation: { activation_id: "synthetic-activation", state_version: 3 },
        consequence: "停止新增风险并退出合成周期的挂单和持仓。", intent: "EXIT_STRATEGY",
        venue_write_created_by_preview: false,
      });
      if (path === "/api/v1/activations/synthetic-activation/exit" && request.method() === "POST") {
        harness.exitRequests.push({ key: request.headers()["idempotency-key"] ?? "", body: request.postDataJSON() });
        return harness.exitResponse === "ABORT" ? route.abort("failed") : respond(harness.exitResponse);
      }
      if (path === "/api/v1/activations/synthetic-activation/exit-by-idempotency" && request.method() === "GET") {
        harness.exitLookups.push(url.searchParams.get("idempotency_key") ?? "");
        return respond(harness.exitLookupResponse);
      }
      unexpected.push(`${request.method()} ${path}`);
      return route.fulfill({ status: 503, json: { detail: { code: "UNEXPECTED_SYNTHETIC_REQUEST" } } });
    });

    await page.routeWebSocket(/\/api\/v1\/market-stream/, (socket) => {
      if (!active) {
        void socket.close();
        return;
      }
      harness.connections += 1;
      sockets.add(socket);
      let sequence = 0;
      const send = () => {
        const now = new Date().toISOString();
        const source = harness.profile === "BINANCE_DEMO" ? "BINANCE_DEMO_PUBLIC" : "BINANCE_LIVE_PUBLIC";
        const identity = { instrument_ref: "BTCUSDT-PERP", source, source_cutoff: now, received_at: now };
        socket.send(JSON.stringify({ type: "quote", ...identity, bid_price: "100", ask_price: "100.01", reference_price: "100.005" }));
        const barOpen = Math.floor(Date.now() / 60_000) * 60_000;
        socket.send(JSON.stringify({ type: "bar", ...identity, interval: "1m", closed: false,
          bar: { open_at: new Date(barOpen).toISOString(), close_at: new Date(barOpen + 60_000).toISOString(),
            open: "100", high: "100.02", low: "99.98", close: "100.01", volume: "12" } }));
        if (harness.flowEnabled) {
          socket.send(JSON.stringify({ type: "depth", ...identity, update_id: ++sequence,
            bids: [{ price: "100", quantity: "3" }], asks: [{ price: "100.01", quantity: "4" }] }));
          socket.send(JSON.stringify({ type: "trade", ...identity, trade_id: `synthetic-${sequence}`,
            price: "100.01", quantity: "0.123", aggressor_side: "BUYER" }));
        }
      };
      send();
      const timer = setInterval(send, 250);
      timers.add(timer);
      socket.onClose(() => {
        harness.closes += 1;
        clearInterval(timer);
        timers.delete(timer);
        sockets.delete(socket);
      });
    });
    try {
      await use(harness);
      expect(unexpected).toEqual([]);
    } finally {
      active = false;
      for (const timer of timers) clearInterval(timer);
      await Promise.allSettled([...sockets].map((socket) => socket.close()));
    }
  },
});

const longButton = (page: Page) => page.getByRole("button", { name: "做多一键启动周期", exact: true });
const shortButton = (page: Page) => page.getByRole("button", { name: "做空一键启动周期", exact: true });

async function saveAndReady(page: Page) {
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(longButton(page)).toBeEnabled();
  await expect(shortButton(page)).toBeEnabled();
}

async function visibility(page: Page, value: "hidden" | "visible") {
  await page.evaluate((next) => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => next });
    document.dispatchEvent(new Event("visibilitychange"));
  }, value);
}

for (const stored of [null, "{broken"] as const) {
  test(`requires an explicit save before trading with ${stored === null ? "absent" : "corrupt"} preferences`, async ({ page, scalp }, testInfo) => {
    if (stored) await page.addInitScript(({ key, value }) => localStorage.setItem(key, value), { key: templateKey, value: stored });
    await page.goto("/scalping");
    await expect(page.getByText("模板未保存", { exact: true })).toBeVisible();
    await expect(longButton(page)).toBeDisabled();
    await expect(shortButton(page)).toBeDisabled();
    await saveAndReady(page);
    await expect(page.getByText("模板已保存", { exact: true })).toBeVisible();
    expect(scalp.triggerRequests).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    if (stored === null) {
      const contrast = await new AxeBuilder({ page })
        .include('button[aria-label="做多一键启动周期"], button[aria-label="做空一键启动周期"]')
        .withRules(["color-contrast"])
        .analyze();
      await testInfo.attach("scalp-ready-contrast.json", { body: JSON.stringify(contrast), contentType: "application/json" });
      expect(contrast.violations).toEqual([]);
      expect(contrast.incomplete).toEqual([]);
      expect(contrast.passes.find((rule) => rule.id === "color-contrast")?.nodes).toHaveLength(2);
      await page.screenshot({ path: testInfo.outputPath("scalp-ready.png"), fullPage: true });
    }
  });
}

test("retains an unknown trigger identity across edits and reload and resolves it only by lookup", async ({ page, scalp }) => {
  scalp.triggerResponse = "ABORT";
  await page.goto("/scalping");
  await saveAndReady(page);
  await longButton(page).click();
  await expect(page.getByRole("button", { name: "核对启动结果" })).toBeVisible();
  await expect.poll(() => scalp.triggerLookups.length).toBeGreaterThan(0);
  expect(scalp.triggerRequests).toHaveLength(1);
  const original = scalp.triggerRequests[0]!;
  expect(original.body.direction).toBe("LONG");
  expect(original.key).not.toBe("");
  await page.getByLabel("名义金额（USDT）", { exact: true }).fill("200");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(shortButton(page)).toBeDisabled();
  await page.reload();
  await expect(page.getByRole("button", { name: "核对启动结果" })).toBeVisible();
  await expect(longButton(page)).toBeDisabled();
  await expect(shortButton(page)).toBeDisabled();
  await page.getByRole("button", { name: "核对启动结果" }).click();
  expect(scalp.triggerLookups.every((key) => key === original.key)).toBe(true);
  expect(scalp.triggerRequests).toHaveLength(1);
  scalp.triggerLookupFound = true;
  await page.getByRole("button", { name: "核对启动结果" }).click();
  await expect(page.getByText("BTCUSDT-PERP 做多周期已创建，等待交易所回报。", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "核对启动结果" })).toHaveCount(0);
  await expect(shortButton(page)).toBeDisabled();
  expect(scalp.triggerRequests).toHaveLength(1);
});

test("uses the current direction click once and keeps open responsibility blocking a second cycle", async ({ page, scalp }) => {
  await page.goto("/scalping");
  await saveAndReady(page);
  await shortButton(page).click();
  await expect(page.getByText("BTCUSDT-PERP 做空周期已创建，等待交易所回报。", { exact: true })).toBeVisible();
  expect(scalp.triggerRequests).toHaveLength(1);
  expect(scalp.triggerRequests[0]!.body).toMatchObject({ direction: "SHORT", template });
  await expect(longButton(page)).toBeDisabled();
});

test("shows a rejected exit receipt without claiming acceptance", async ({ page, scalp }) => {
  scalp.latest = openCycle();
  scalp.exitResponse = receipt("REJECTED", "PLAN_VERSION_CONFLICT");
  await page.goto("/scalping");
  await page.getByRole("button", { name: "退出周期", exact: true }).click();
  await page.getByRole("button", { name: "确认退出", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "一键交易", exact: true }).getByText("周期状态已变化，退出指令未被接受；请重新核对退出。", { exact: true })).toBeVisible();
  await expect(page.getByText("退出指令已接受，等待撤单和平仓结果。", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "退出周期", exact: true })).toBeEnabled();
  expect(scalp.exitRequests).toHaveLength(1);
});

test("blocks an open exit confirmation when refreshed permissions become read only", async ({ page, scalp }) => {
  scalp.profile = "BINANCE_LIVE_WRITE";
  scalp.latest = openCycle();
  await page.goto("/scalping");
  await page.getByRole("button", { name: "退出周期", exact: true }).click();
  await expect(page.getByRole("button", { name: "确认退出", exact: true })).toBeEnabled();
  // Wait for the app's five-second cache freshness period, then simulate reconnect.
  await page.waitForTimeout(5_100);
  scalp.profile = "BINANCE_LIVE_READ_ONLY";
  await page.evaluate(() => {
    window.dispatchEvent(new Event("offline"));
    window.dispatchEvent(new Event("online"));
  });
  await expect(page.getByRole("dialog").getByText("当前真实账户为只读模式，无法提交退出。", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "确认退出", exact: true })).toBeDisabled();
  expect(scalp.exitRequests).toHaveLength(0);
});

for (const unknown of ["RECEIPT", "NETWORK"] as const) {
  test(`preserves an unknown ${unknown.toLowerCase()} exit for query recovery after reload`, async ({ page, scalp }) => {
    scalp.latest = openCycle();
    scalp.exitResponse = unknown === "RECEIPT" ? receipt("UNKNOWN") : "ABORT";
    await page.goto("/scalping");
    await page.getByRole("button", { name: "退出周期", exact: true }).click();
    await page.getByRole("button", { name: "确认退出", exact: true }).click();
    await expect(page.getByRole("button", { name: "确认退出", exact: true })).toBeDisabled();
    await expect.poll(() => scalp.exitLookups.length).toBeGreaterThan(0);
    await expect(page.getByText("退出指令已接受，等待撤单和平仓结果。", { exact: true })).toHaveCount(0);
    const key = scalp.exitRequests[0]!.key;
    await page.reload();
    await expect(page.getByRole("button", { name: "核对退出结果", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "退出周期", exact: true })).toBeDisabled();
    scalp.exitLookupResponse = receipt("APPLIED");
    await page.getByRole("button", { name: "核对退出结果", exact: true }).click();
    await expect(page.getByText("退出指令已接受，等待撤单和平仓结果。", { exact: true })).toBeVisible();
    expect(scalp.exitRequests).toHaveLength(1);
    expect(scalp.exitLookups.every((lookup) => lookup === key)).toBe(true);
    await expect(longButton(page)).toBeDisabled();
  });
}

test("expires old flow while quotes continue and fills chart history after visibility recovery", async ({ page, scalp }) => {
  await page.goto("/scalping");
  await saveAndReady(page);
  const flow = page.getByRole("region", { name: "订单簿 / 成交带" });
  await expect(flow.getByText("100.00 / 100.01", { exact: true })).toBeVisible();
  scalp.flowEnabled = false;
  await expect(flow.getByText("订单簿等待当前数据", { exact: true })).toBeVisible({ timeout: 8_000 });
  await expect(flow.getByText("成交明细等待当前数据", { exact: true })).toBeVisible();
  await expect(longButton(page)).toBeEnabled();
  await expect.poll(() => scalp.historyReads).toBeGreaterThan(0);
  const historyBefore = scalp.historyReads;
  const connectionsBefore = scalp.connections;
  const closesBefore = scalp.closes;
  await visibility(page, "hidden");
  await expect.poll(() => scalp.closes).toBeGreaterThan(closesBefore);
  await visibility(page, "visible");
  await expect.poll(() => scalp.connections).toBeGreaterThan(connectionsBefore);
  await expect.poll(() => scalp.historyReads).toBeGreaterThan(historyBefore);
  await expect(longButton(page)).toBeEnabled();
});

test("refreshes executor permission and isolates templates after an account change", async ({ page, scalp }, testInfo) => {
  test.skip(testInfo.project.name === "chromium-narrow", "The shared permission polling runs once; all interaction cases cover both viewports.");
  test.setTimeout(80_000);
  await page.goto("/scalping");
  await saveAndReady(page);
  scalp.executor = "NOT_READY";
  await expect(longButton(page)).toBeDisabled({ timeout: 35_000 });
  await expect(page.getByRole("region", { name: "一键交易", exact: true }).getByText("执行器未就绪", { exact: true })).toBeVisible();
  scalp.account = "scalp-e2e-other";
  scalp.executor = "READY";
  await expect(page.getByText("模板未保存", { exact: true })).toBeVisible({ timeout: 35_000 });
  await expect(longButton(page)).toBeDisabled();
  await expect(shortButton(page)).toBeDisabled();
  expect(scalp.triggerRequests).toHaveLength(0);
});
