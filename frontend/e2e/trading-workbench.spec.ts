import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type TestInfo } from "@playwright/test";

type Activation = {
  activation_id: string;
  instrument_ref: string;
  lifecycle: string;
  protection_state?: string;
};

type PlanSummary = {
  plan_version_id: string | null;
  decision_basis_kind?: string | null;
};

function availableMarketContext(
  requestUrl: string,
  source: "BINANCE_DEMO_PUBLIC" | "BINANCE_LIVE_PUBLIC",
) {
  const request = new URL(requestUrl);
  const stopReferenceInterval = request.searchParams.get("stop_reference_interval") ?? "15m";
  const stopReferenceCutoff = new Date(Date.now() - 15 * 60_000).toISOString();
  return {
    instrument_ref: request.searchParams.get("instrument_ref") ?? "BTCUSDT-PERP",
    source,
    source_cutoff: new Date().toISOString(),
    latest_closed_1m_at: new Date(Date.now() - 60_000).toISOString(),
    latest_closed_15m_at: stopReferenceCutoff,
    latest_closed_stop_reference_at: stopReferenceCutoff,
    channel_lookback_15m: Number(request.searchParams.get("channel_lookback_15m") ?? 20),
    stop_reference_interval: stopReferenceInterval,
    bid_price: "100",
    ask_price: "101",
    reference_price: "100.5",
    latest_close_1m: "100.5",
    latest_volume_1m: "1000",
    latest_trade_count_1m: 10,
    latest_close_15m: "100.5",
    channel_upper: "102",
    channel_lower: "98",
    atr_14: "1",
    stop_reference_atr_14: "1",
    long_breakout_gap_pct: "1.5",
    short_breakout_gap_pct: "-2.5",
    stop_references: [],
  };
}

test.beforeEach(async ({ request }) => {
  const response = await request.get("/api/v1/overview");
  if (!response.ok()) {
    test.skip(true, `Trading fixture overview unavailable: HTTP ${response.status()}`);
    return;
  }
  const overview = await response.json() as { environment_id?: string };
  test.skip(
    overview.environment_id !== "trading-workbench-fixture",
    "Run against tools.qualification.run_trading_workbench_fixture.",
  );
});

async function activations(page: Page): Promise<Activation[]> {
  return page.evaluate(async () => {
    const response = await fetch("/api/v1/activations", { credentials: "same-origin" });
    if (!response.ok) throw new Error(`ACTIVATIONS_HTTP_${response.status}`);
    return response.json();
  });
}

async function assertAccessible(page: Page, testInfo: TestInfo, name: string) {
  const result = await new AxeBuilder({ page }).analyze();
  const violations = result.violations.map(({ id, impact, nodes }) => ({
    id,
    impact,
    nodeCount: nodes.length,
    nodes: nodes.map((node) => ({
      target: node.target,
      html: node.html,
      failureSummary: node.failureSummary,
    })),
  }));
  await testInfo.attach(`${name}-axe.json`, {
    body: Buffer.from(JSON.stringify({ url: page.url(), violations }, null, 2)),
    contentType: "application/json",
  });
  expect(violations).toEqual([]);
}

async function assertNoDocumentOverflow(page: Page, testInfo: TestInfo, name: string) {
  const layout = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
    offenders: [...document.querySelectorAll<HTMLElement>("body *")]
      .filter((element) => !element.closest(".table-scroll"))
      .filter((element) => getComputedStyle(element).visibility !== "hidden")
      .map((element) => {
        const bounds = element.getBoundingClientRect();
        return {
          tag: element.tagName,
          className: element.className,
          left: bounds.left,
          right: bounds.right,
          text: element.textContent?.trim().slice(0, 120) ?? "",
        };
      })
      .filter(({ left, right }) => left < -0.5 || right > document.documentElement.clientWidth + 0.5),
  }));
  await testInfo.attach(`${name}-layout.json`, {
    body: Buffer.from(JSON.stringify(layout, null, 2)),
    contentType: "application/json",
  });
  expect(layout.offenders).toEqual([]);
  expect(layout.scrollWidth).toBe(layout.clientWidth);
}

test("the workbench preserves the available protection, exit, takeover, closure and review facts without collapsing responsibility", async ({ page }, testInfo) => {
  await page.goto("/overview");
  const items = await activations(page);
  const gap = items.find((item) => item.protection_state === "GAP");
  const exiting = items.find((item) => item.lifecycle === "EXITING");
  const takeover = items.find((item) => item.lifecycle === "USER_TAKEOVER");
  const missingStates = [
    !gap && "GAP",
    !exiting && "EXITING",
    !takeover && "USER_TAKEOVER",
  ].filter(Boolean);
  if (missingStates.length > 0) {
    testInfo.annotations.push({
      type: "coverage-gap",
      description: `当前运行库缺少 ${missingStates.join(" / ")} 激活；仅跳过对应状态片段。`,
    });
  }

  if (gap) {
    await page.goto(`/activations/${gap.activation_id}`);
    const gapPlanName = await page.evaluate(async (activationId) => {
      const response = await fetch(`/api/v1/activations/${activationId}`, { credentials: "same-origin" });
      if (!response.ok) throw new Error(`ACTIVATION_HTTP_${response.status}`);
      const detail = await response.json() as { plan?: { plan_name?: string | null } };
      return detail.plan?.plan_name?.trim() || "未命名计划";
    }, gap.activation_id);
    await expect(page.getByText(/计划详情与复盘/).first()).toBeVisible();
    await expect(page.getByRole("heading", { name: gapPlanName })).toBeVisible();
    await expect(page.getByRole("alert").filter({ hasText: "交易所原生保护尚未证明为工作中" })).toBeVisible();
    const unresolvedVenueAlert = page.getByRole("alert").filter({ hasText: "交易所结果未决" });
    await expect(unresolvedVenueAlert).toBeVisible();
    await expect(unresolvedVenueAlert).toContainText("系统只查询原订单 UUID，并暂停新的入场动作");
    await assertAccessible(page, testInfo, "trading-gap-unknown-max-loss");
    await testInfo.attach("trading-gap-unknown-max-loss.png", {
      body: await page.screenshot({ fullPage: true }),
      contentType: "image/png",
    });
  }

  if (exiting) {
    await page.goto(`/activations/${exiting.activation_id}`);
    await expect(page.getByText("正在退出", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("仅本计划归属仓位", { exact: false })).toBeVisible();
  }

  if (takeover) {
    await page.goto(`/activations/${takeover.activation_id}`);
    await expect(page.getByRole("alert").filter({ hasText: "用户接管已持久化" })).toBeVisible();
    await expect(page.getByRole("alert").filter({ hasText: "Halpha 不再提交新的待执行动作" })).toBeVisible();
    const unresolvedEntry = page.getByRole("alert").filter({ hasText: "入场动作结果未决" });
    if (await unresolvedEntry.count() > 0) {
      await expect(unresolvedEntry).toBeVisible();
      await expect(page.getByText("本次结果（未成交）", { exact: true })).toHaveCount(0);
      await expect(page.getByText("本次结果", { exact: true }).first()).toBeVisible();
      await expect(page.getByText("待核对", { exact: true }).first()).toBeVisible();
    }
    await assertAccessible(page, testInfo, "trading-user-takeover");
  }

  await page.goto("/reviews");
  await expect(page.getByRole("table", { name: "交易与复盘记录" })).toBeVisible();
  await page.getByRole("tab", { name: /全部记录/ }).click();
  await expect(page.getByText("账户累计手续费", { exact: true }).locator(".."))
    .toContainText("-0.06 USDT");
  await expect(page.getByText("已完成交易", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("待评价").first()).toBeVisible();
  await page.getByRole("table", { name: "交易与复盘记录" }).locator("tbody tr").first().click();
  await expect(page.getByRole("heading", { name: "复盘与结果" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "机器为何交易" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "复盘判断" })).toBeVisible();
  await expect(page.getByText("成交结果仅使用该复盘明确引用的权威事实", { exact: false })).toBeVisible();
  await assertAccessible(page, testInfo, "trading-review");

  await page.goto("/operations");
  await expect(page.getByRole("heading", { name: "故障接管" })).toBeVisible();
  await expect(page.getByRole("link", { name: "打开 Binance 官方入口" })).toBeVisible();
  if (gap && gap.lifecycle !== "COMPLETED") {
    await expect(page.getByText("存在保护缺口", { exact: true }).first()).toBeVisible();
  }
  if (exiting) await expect(page.getByText("正在退出", { exact: true }).first()).toBeVisible();
  if (takeover) await expect(page.getByText("已由用户接管", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("恢复激活", { exact: false })).toHaveCount(0);
  await expect(page.locator("table")).toHaveCount(0);
  await assertAccessible(page, testInfo, "trading-operations");

  await assertNoDocumentOverflow(page, testInfo, "trading-operations");
});

test("the workbench renders the synthetic LIVE strategy-start target state without an exchange-changing request", async ({ page }, testInfo) => {
  await page.goto("/overview");

  const fixtureFacts = await page.evaluate(async () => {
    const [statusResponse, plansResponse] = await Promise.all([
      fetch("/api/v1/settings/status", { credentials: "same-origin" }),
      fetch("/api/v1/plans", { credentials: "same-origin" }),
    ]);
    if (!statusResponse.ok) throw new Error(`SETTINGS_HTTP_${statusResponse.status}`);
    if (!plansResponse.ok) throw new Error(`PLANS_HTTP_${plansResponse.status}`);
    return {
      status: await statusResponse.json() as Record<string, unknown>,
      plans: await plansResponse.json() as PlanSummary[],
    };
  });
  const planVersionId = fixtureFacts.plans.find((plan) => (
    plan.plan_version_id && plan.decision_basis_kind === "STRATEGY_SIGNAL"
  ))?.plan_version_id;
  expect(planVersionId).toBeTruthy();

  let projectedStatus: Record<string, unknown> = {
    ...fixtureFacts.status,
    environment_kind: "LIVE",
    environment_id: "synthetic-live-target-environment",
    account_id: "synthetic-live-target-account",
    venue_account_type: "USDM_COPY_LEAD",
    profile: "BINANCE_LIVE_WRITE",
    authority_class: "LIVE_REAL_CAPITAL",
    product_build_id: "a".repeat(64),
    app_executor_product_build_consistent: true,
    executor_status: "READY",
    executor_status_checked_at: "2026-07-21T00:00:00Z",
    configured_runtime_real_write_gate: "CLOSED",
    runtime_real_write_gate: "CLOSED",
    live_write_gate_violations: [],
  };
  let projectedPreview: Record<string, unknown> | undefined;
  let activationSubmissions = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && /\/api\/v1\/activations$/.test(request.url())) {
      activationSubmissions += 1;
    }
  });
  await page.route("**/api/v1/settings/status", (route) => route.fulfill({ json: projectedStatus }));
  await page.route("**/api/v1/plan-versions/*/activation-preview", async (route) => {
    if (projectedPreview) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        json: projectedPreview,
      });
      return;
    }
    const response = await route.fetch();
    const actualPreview = await response.json() as Record<string, unknown>;
    projectedPreview ??= {
      ...actualPreview,
      environment_kind: "LIVE",
      authority_class: "LIVE_REAL_CAPITAL",
      account_ref: "synthetic-live-target-account",
      valid_until: new Date(Date.now() + 60 * 60 * 1_000).toISOString(),
      product_build_id: "a".repeat(64),
      product_build_consistent: true,
      executor_status: "READY",
      executor_status_checked_at: "2026-07-21T00:00:00Z",
      configured_runtime_real_write_gate: "CLOSED",
      runtime_real_write_gate: "CLOSED",
      live_activation_eligible: true,
    };
    await route.fulfill({ response, json: projectedPreview });
  });
  await page.route("**/api/v1/market-context?**", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      json: availableMarketContext(route.request().url(), "BINANCE_LIVE_PUBLIC"),
    });
  });

  await testInfo.attach("synthetic-live-target-state.json", {
    body: Buffer.from(JSON.stringify({
      fixture_kind: "SYNTHETIC_LIVE_TARGET_STATE",
      venue_writes: false,
      activation_submission_exercised: false,
      purpose: "STRATEGY_START_UI_AND_GATE_VALIDATION_ONLY",
    }, null, 2)),
    contentType: "application/json",
  });

  await page.goto(`/plans/${planVersionId}/activate`);
  await expect(page.getByRole("combobox", { name: "交易上下文" }))
    .toContainText("实盘 · 带单账户");
  await expect(page.getByText("LIVE", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("实盘写门 · 已关闭")).toBeVisible();
  await expect(page.getByText("确认启动计划", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: String(projectedPreview?.plan_name ?? "未命名计划") })).toBeVisible();
  await expect(page.getByText("计划中的交易金额就是本次边界", { exact: false })).toBeVisible();
  const submit = page.getByRole("button", { name: "在带单账户启动实盘计划" });
  await expect(submit).toBeEnabled();
  await expect(page.getByText("成交可能进入带单组合并被跟单者复制", { exact: false })).toBeVisible();
  await assertAccessible(page, testInfo, "trading-synthetic-live-closed");
  await assertNoDocumentOverflow(page, testInfo, "trading-synthetic-live-closed");
  await page.evaluate(() => window.scrollTo(0, 0));
  await testInfo.attach("trading-synthetic-live-closed.png", {
    body: await page.screenshot({ fullPage: true }),
    contentType: "image/png",
  });

  projectedStatus = {
    ...projectedStatus,
    executor_status: "STARTING",
    app_executor_product_build_consistent: null,
  };
  projectedPreview = {
    ...projectedPreview,
    executor_status: "STARTING",
  };
  await page.reload();
  await expect(page.getByText("成交可能进入带单组合并被跟单者复制", { exact: false })).toBeVisible();
  await expect(page.getByRole("button", { name: "在带单账户启动实盘计划" })).toBeEnabled();

  projectedStatus = {
    ...projectedStatus,
    executor_status: "READY",
    app_executor_product_build_consistent: true,
    configured_runtime_real_write_gate: "OPEN",
    runtime_real_write_gate: "OPEN",
  };
  projectedPreview = {
    ...projectedPreview,
    executor_status: "READY",
    configured_runtime_real_write_gate: "OPEN",
    runtime_real_write_gate: "OPEN",
    live_activation_eligible: false,
  };
  await page.reload();
  await expect(page.getByText("实盘写门 · 已开启")).toBeVisible();
  await expect(page.getByText("当前 App、Executor 或实盘变更门配置尚未一致；当前不能启动真实账户计划。")).toBeVisible();
  await expect(page.getByRole("button", { name: "在带单账户启动实盘计划" })).toBeDisabled();
  await assertAccessible(page, testInfo, "trading-synthetic-live-open");
  await assertNoDocumentOverflow(page, testInfo, "trading-synthetic-live-open");
  await page.evaluate(() => window.scrollTo(0, 0));
  await testInfo.attach("trading-synthetic-live-open.png", {
    body: await page.screenshot({ fullPage: true }),
    contentType: "image/png",
  });

  projectedStatus = {
    ...projectedStatus,
    environment_id: "synthetic-live-personal-environment",
    account_id: "synthetic-live-personal-account",
    venue_account_type: "USDM_PERSONAL",
    configured_runtime_real_write_gate: "CLOSED",
    runtime_real_write_gate: "CLOSED",
  };
  projectedPreview = {
    ...projectedPreview,
    account_ref: "synthetic-live-personal-account",
    configured_runtime_real_write_gate: "CLOSED",
    runtime_real_write_gate: "CLOSED",
    live_activation_eligible: true,
  };
  await page.reload();
  await expect(page.getByRole("combobox", { name: "交易上下文" }))
    .toContainText("实盘 · 个人账户");
  await expect(page.getByText("交易仅属于个人账户，不进入带单组合", { exact: false }))
    .toBeVisible();
  await expect(page.getByText("成交可能进入带单组合", { exact: false }))
    .toHaveCount(0);
  await expect(page.getByRole("button", { name: "在个人账户启动实盘计划" }))
    .toBeEnabled();
  expect(activationSubmissions).toBe(0);
});

test("unknown activation creation retries with the original request identity", async ({ page }, testInfo) => {
  test.setTimeout(45_000);
  await page.goto("/overview");
  const plans = await page.evaluate(async () => {
    const response = await fetch("/api/v1/plans", { credentials: "same-origin" });
    if (!response.ok) throw new Error(`PLANS_HTTP_${response.status}`);
    return response.json() as Promise<PlanSummary[]>;
  });
  const planVersionId = plans.find((plan) => (
    plan.plan_version_id && plan.decision_basis_kind === "STRATEGY_SIGNAL"
  ))?.plan_version_id;
  expect(planVersionId).toBeTruthy();
  if (!planVersionId) throw new Error("FIXED_PLAN_REQUIRED");

  await page.route("**/api/v1/plan-versions/*/activation-preview", async (route) => {
    const response = await route.fetch();
    const preview = await response.json() as Record<string, unknown>;
    await route.fulfill({
      response,
      contentType: "application/json",
      body: JSON.stringify({
        ...preview,
        valid_until: new Date(Date.now() + 60 * 60 * 1_000).toISOString(),
        product_build_consistent: true,
        executor_status: "READY",
        live_activation_eligible: true,
      }),
    });
  });
  await page.route("**/api/v1/market-context?**", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      json: availableMarketContext(route.request().url(), "BINANCE_DEMO_PUBLIC"),
    });
  });

  const idempotencyKeys: string[] = [];
  let activationAttempts = 0;
  await page.route(/\/api\/v1\/activations$/, async (route) => {
    const request = route.request();
    if (request.method() !== "POST") {
      await route.continue();
      return;
    }
    activationAttempts += 1;
    idempotencyKeys.push(await request.headerValue("idempotency-key") ?? "");
    if (activationAttempts === 1) {
      await route.abort("failed");
      return;
    }
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        activation: { activation_id: "synthetic-idempotent-activation" },
        venue_write_created: false,
      }),
    });
  });

  await page.goto(`/plans/${planVersionId}/activate`);
  const startButton = page.getByRole("button", { name: "启动策略" });
  await expect(startButton).toBeEnabled({ timeout: 20_000 });
  await startButton.click();
  await expect(page.getByRole("alert").filter({
    hasText: "激活结果未知；再次启动会沿用同一请求身份",
  })).toBeVisible();
  const retryButton = page.getByRole("button", {
    name: "沿用原请求身份重试",
  });
  await expect(retryButton).toBeEnabled();
  await retryButton.click();
  await expect(page).toHaveURL(/\/activations\/synthetic-idempotent-activation$/);
  expect(idempotencyKeys).toHaveLength(2);
  expect(idempotencyKeys[0]).toBeTruthy();
  expect(idempotencyKeys[1]).toBe(idempotencyKeys[0]);

  await assertAccessible(page, testInfo, "activation-idempotent-retry");
});

test("the workbench rejects a stale control submission instead of applying a newer activation version", async ({ page, context, browser }, testInfo) => {
  test.skip(testInfo.project.name !== "chromium-desktop", "One state-changing stale-version drill is sufficient.");
  await page.goto("/overview");
  const items = await activations(page);
  const staleControl = items.find((item) => item.instrument_ref === "XRPUSDT-PERP" && item.lifecycle === "RUNNING");
  test.skip(!staleControl, "This stale-version drill needs the seeded running XRP activation.");

  const activationPath = `/api/v1/activations/${staleControl!.activation_id}`;
  const initialResponse = await context.request.get(activationPath);
  expect(initialResponse.ok()).toBeTruthy();
  const frozenDetail = await initialResponse.json() as { activation: { state_version: number } };
  const initialVersion = frozenDetail.activation.state_version;

  // A separate browser context isolates the stale client from query-cache
  // broadcasts. Replay its actual initial read while both control requests
  // continue to use the fixture service and its current version checks.
  const staleContext = await browser.newContext({
    baseURL: new URL(page.url()).origin,
    storageState: await context.storageState(),
    viewport: page.viewportSize(),
  });
  try {
    await staleContext.route(new RegExp(`${activationPath}$`), (route) => (
      route.fulfill({ json: frozenDetail })
    ));
    const stalePage = await staleContext.newPage();
    await Promise.all([
      page.goto(`/activations/${staleControl!.activation_id}`),
      stalePage.goto(`/activations/${staleControl!.activation_id}`),
    ]);
    await expect(page.getByText("XRPUSDT-PERP / 做多", { exact: true })).toBeVisible();
    await expect(stalePage.getByText("XRPUSDT-PERP / 做多", { exact: true })).toBeVisible();
    const [previewResponse] = await Promise.all([
      stalePage.waitForResponse((response) => (
        new URL(response.url()).pathname === `${activationPath}/control-preview`
        && response.request().method() === "POST"
      )),
      stalePage.getByRole("button", { name: /^用户接管；/ }).click(),
    ]);
    expect(previewResponse.ok()).toBeTruthy();
    const preview = await previewResponse.json() as { activation: { state_version: number } };
    expect(preview.activation.state_version).toBe(initialVersion);
    await expect(stalePage.getByRole("button", { name: "确认用户接管" })).toBeVisible();

    await page.getByRole("button", { name: "退出策略" }).click();
    await page.getByRole("button", { name: "确认退出策略" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "命令已生效，当前执行责任已经核对" })).toBeVisible();
    const afterExit = await (await context.request.get(activationPath)).json() as {
      activation: { state_version: number; lifecycle: string };
    };
    expect(afterExit.activation.state_version).toBeGreaterThan(initialVersion);

    const [rejection] = await Promise.all([
      stalePage.waitForResponse((response) => (
        new URL(response.url()).pathname === `${activationPath}/takeover`
        && response.request().method() === "POST"
      )),
      stalePage.getByRole("button", { name: "确认用户接管" }).click(),
    ]);
    expect(rejection.request().postDataJSON().expected_version).toBe(initialVersion);
    expect(await rejection.json()).toMatchObject({
      state: "REJECTED", reason_code: "PLAN_VERSION_CONFLICT",
    });
    await expect(stalePage.getByRole("alert").filter({ hasText: "PLAN_VERSION_CONFLICT" })).toBeVisible();
    const afterRejection = await (await context.request.get(activationPath)).json() as {
      activation: { state_version: number; lifecycle: string };
    };
    expect(afterRejection.activation.state_version).toBe(afterExit.activation.state_version);
    expect(afterRejection.activation.lifecycle).toBe(afterExit.activation.lifecycle);
    expect(afterRejection.activation.lifecycle).not.toBe("USER_TAKEOVER");
    await assertAccessible(stalePage, testInfo, "trading-stale-control-rejected");
  } finally {
    await staleContext.close();
  }
});
