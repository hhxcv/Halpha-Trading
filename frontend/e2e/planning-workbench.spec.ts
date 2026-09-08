import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Locator, type Page, type TestInfo } from "@playwright/test";

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

type TestViewport = {
  name: string;
  width: number;
  height: number;
};

type LayoutRect = {
  x: number;
  y: number;
  width: number;
  height: number;
};

const directExecutionViewports = {
  desktop: [
    { name: "desktop-1440x1000", width: 1440, height: 1000 },
    { name: "desktop-1123x920", width: 1123, height: 920 },
    { name: "desktop-1024x768", width: 1024, height: 768 },
    { name: "desktop-768x900", width: 768, height: 900 },
  ],
  narrow: [
    { name: "narrow-390x844", width: 390, height: 844 },
  ],
} satisfies Record<string, TestViewport[]>;

function syntheticDirectDraft(planId: string, draftVersion: number, planName: string) {
  return {
    plan_id: planId,
    environment_id: "demo",
    draft_version: draftVersion,
    content: {
      plan_name: planName,
      account_ref: "demo-owner",
      allowed_actions: ["ENTER", "PROTECT", "REDUCE", "EXIT", "CANCEL"],
      authority_class: "TRADING",
      created_at: "2026-07-23T10:00:00.000Z",
      creator_kind: "HUMAN",
      environment_id: "demo",
      environment_kind: "DEMO",
      decision_context: {
        intent: "PROFIT_SEEKING",
        setup_family: "BREAKOUT_CONTINUATION",
        rationale: "闭合价格确认突破，保护位有效且计划退出空间覆盖风险后执行。",
        evidence: "SYSTEM_MANAGED_MARKET_CONTEXT",
        evidence_cutoff: "2026-07-23T10:00:00.000Z",
        invalidation: "SYSTEM_MANAGED_TYPED_PLAN_BOUNDARIES",
        limitations: "SYSTEM_MANAGED_EXECUTION_AND_MARKET_LIMITATIONS",
        playbook_ref: null,
      },
      decision_basis: {
        kind: "DIRECT_EXECUTION",
        decision_basis_ref: "DIRECT_EXECUTION@1",
        parameters: {},
      },
      order_schedule_spec: {
        entry_program: {
          kind: "ONE_TIME",
          slice_count: 1,
          first_slice_delay_seconds: 0,
          slice_interval_seconds: 0,
        },
        price_distribution: {
          kind: "SINGLE",
          limit_price: "65000",
        },
        amount_distribution: {
          mode: "FIXED",
          direction: "LOW_TO_HIGH",
          base_notional: "500",
          linear_step: "0",
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
          initial_stop: {
            distance_bps: "100",
            trigger_source: "MARK_PRICE",
            coverage: "EACH_CONFIRMED_FILL",
          },
          take_profit_ladder: {
            levels: [{ trigger_r: "2", quantity_fraction: "1" }],
          },
          time_exit_seconds: null,
        },
        dynamic_rules: [],
      },
      venue_ref: "BINANCE_USDM",
      instrument_ref: "BTCUSDT-PERP",
      direction: "LONG",
      position_alignment: null,
      target_exposure: "500",
      requested_limits: {
        max_margin: "500",
        max_notional: "500",
        max_allowed_loss: "500",
      },
      valid_from: "2026-07-23T10:00:00.000Z",
      valid_until: "2026-07-23T11:00:00.000Z",
      terms: {},
    },
    content_digest: String(draftVersion).repeat(64),
    updated_at: "2026-07-23T10:00:00.000Z",
  };
}

function syntheticPlanAiReview(
  status: "QUEUED" | "RUNNING" | "APPROVED" | "REJECTED" | "FAILED",
  draftVersion: number,
  contentDigest: string,
  reviewId = `review-${draftVersion}`,
  failureCode: string | null = null,
  planId = "ai-review-e2e",
) {
  const now = new Date().toISOString();
  return {
    review_id: reviewId,
    environment_id: "demo",
    plan_id: planId,
    draft_version: draftVersion,
    draft_content_digest: contentDigest,
    prompt_version: "HALPHA_PLAN_AI_REVIEW_V4",
    configuration: {
      model: "gpt-5.6-terra",
      reasoning_effort: "medium",
    },
    status,
    market_context_digest: "a".repeat(64),
    market_source_cutoff: now,
    decision: status === "APPROVED" ? "APPROVE" : status === "REJECTED" ? "REJECT" : null,
    reason: status === "APPROVED"
      ? "入场、保护与自动退出边界一致，最大预计亏损处于当前纪律额度内。"
      : status === "REJECTED"
        ? "当前交易理由不足以支持修改后的追价边界，费用后收益风险关系不成立。"
        : null,
    suggestions: status === "REJECTED"
      ? ["降低入场价格或重新设置保护位", "确保费用后目标至少覆盖计划风险"]
      : [],
    progress_message: status === "QUEUED"
      ? "等待 Codex 审核资源"
      : status === "RUNNING"
        ? "正在核对保护、退出与当前纪律"
        : "审核已完成",
    public_output: status === "RUNNING"
      ? "已读取计划与行情上下文，正在核对最大亏损和费用后收益风险关系…"
      : "",
    failure_code: failureCode,
    created_at: now,
    started_at: status === "QUEUED" ? null : now,
    completed_at: ["APPROVED", "REJECTED", "FAILED"].includes(status) ? now : null,
    updated_at: now,
  };
}

async function routeCurrentDemoMarketStream(
  page: Page,
  referencePrice: () => string = () => "65001",
) {
  // Creation flows require one coherent snapshot: history for chart/preview,
  // context for stops and the live stream for freshness.  Keep all three
  // deterministic whenever a test opts into the current Demo stream; tests
  // may still register a more specific window/context route afterwards.
  await routeCurrentDemoMarketWindow(page);
  await routeCurrentDemoMarketContext(page);
  await routeValidOrderSchedulePreview(page);
  await page.routeWebSocket(/\/api\/v1\/market-stream/, (socket) => {
    let closed = false;
    const sendCurrentFrames = () => {
      if (closed || page.isClosed()) return;
      const price = referencePrice();
      const timestamp = new Date(Date.now() + 4_000).toISOString();
      socket.send(JSON.stringify({
        type: "status",
        state: "LIVE",
        source: "BINANCE_DEMO_PUBLIC",
        observed_at: timestamp,
        reason: null,
      }));
      socket.send(JSON.stringify({
        type: "quote",
        instrument_ref: "BTCUSDT-PERP",
        source: "BINANCE_DEMO_PUBLIC",
        source_cutoff: timestamp,
        received_at: timestamp,
        bid_price: String(Number(price) - 1),
        ask_price: String(Number(price) + 1),
        reference_price: price,
      }));
      socket.send(JSON.stringify({
        type: "bar",
        instrument_ref: "BTCUSDT-PERP",
        interval: "15m",
        source: "BINANCE_DEMO_PUBLIC",
        source_cutoff: timestamp,
        received_at: timestamp,
        closed: false,
        bar: {
          open_at: "2026-07-23T11:30:00.000Z",
          close_at: "2026-07-23T11:45:00.000Z",
          open: "65000",
          high: "65005",
          low: "64995",
          close: "65001",
          volume: "10",
        },
      }));
    };
    sendCurrentFrames();
    const timer = setInterval(sendCurrentFrames, 1_000);
    const stop = () => {
      closed = true;
      clearInterval(timer);
    };
    socket.onClose(stop);
    page.on("close", stop);
  });
}

async function routeReadyDemoExecutor(
  page: Page,
  {
    executorStatus = "READY",
    productBuildConsistent = true,
    statusOverrides = {},
  }: {
    executorStatus?: string;
    productBuildConsistent?: boolean;
    statusOverrides?: Record<string, unknown>;
  } = {},
) {
  await addBrowserScopedCsrfCookie(page);
  await page.route("**/api/v1/execution-fee-evidence?**", async (route) => {
    const sourceCutoff = new Date().toISOString();
    await route.fulfill({
      contentType: "application/json",
      json: {
        instrument_ref: "BTCUSDT-PERP",
        source: "RECENT_ATTRIBUTED_COMPLETED_FILLS",
        calculation: "MAX_RATE_OF_LATEST_FILLS",
        sample_limit: 20,
        maker: {
          conservative_rate_bps: "2",
          sample_count: 8,
          latest_fill_time: sourceCutoff,
        },
        taker: {
          conservative_rate_bps: "4",
          sample_count: 12,
          latest_fill_time: sourceCutoff,
        },
        source_cutoff: sourceCutoff,
      },
    });
  });
  await page.route("**/api/v1/market-funding-history?**", async (route) => {
    const sourceCutoff = new Date().toISOString();
    await route.fulfill({
      contentType: "application/json",
      json: {
        instrument_ref: "BTCUSDT-PERP",
        source: "BINANCE_DEMO_PUBLIC",
        source_cutoff: sourceCutoff,
        samples: [
          { settled_at: "2026-07-20T00:00:00Z", funding_rate: "0.0001" },
          { settled_at: "2026-07-20T08:00:00Z", funding_rate: "0.0001" },
          { settled_at: "2026-07-20T16:00:00Z", funding_rate: "0.0001" },
        ],
        average_funding_rate: "0.0001",
        average_interval_seconds: 28_800,
      },
    });
  });
  await page.route("**/api/v1/strategies", async (route) => {
    await route.fulfill({ contentType: "application/json", json: [] });
  });
  await page.route("**/api/v1/settings/status", async (route) => {
    const now = new Date().toISOString();
    await route.fulfill({
      contentType: "application/json",
      json: {
        environment_kind: "DEMO",
        environment_id: "binance-demo-primary",
        account_id: "binance-usdm-demo-owner-primary",
        venue_account_type: "USDM_DEMO",
        profile: "BINANCE_DEMO",
        authority_class: "DEMO_SIMULATION",
        bind: "127.0.0.1",
        port: 8765,
        trading_contexts: [
          {
            venue_account_type: "USDM_DEMO",
            environment_id: "binance-demo-primary",
            account_id: "binance-usdm-demo-owner-primary",
            url: "http://127.0.0.1:8765/overview",
          },
          {
            venue_account_type: "USDM_COPY_LEAD",
            environment_id: "binance-live-copy-primary",
            account_id: "binance-usdm-copy-lead-primary",
            url: "http://127.0.0.1:8766/overview",
          },
          {
            venue_account_type: "USDM_PERSONAL",
            environment_id: "binance-live-personal-primary",
            account_id: "binance-usdm-personal-primary",
            url: "http://127.0.0.1:8767/overview",
          },
        ],
        database_name: "halpha_demo",
        database_available: true,
        database_reason_code: null,
        server_fact_cutoff: now,
        product_build_id: "a".repeat(64),
        executor_status: executorStatus,
        app_executor_product_build_consistent: productBuildConsistent,
        executor_status_checked_at: now,
        configured_runtime_real_write_gate: "CLOSED",
        runtime_real_write_gate: "CLOSED",
        live_write_gate_violations: [],
        authorized_activation_ids: [],
        email_delivery_enabled: false,
        email_configuration_status: "DISABLED",
        view_retrieved_at: now,
        ...statusOverrides,
      },
    });
  });
}

async function routeValidOrderSchedulePreview(
  page: Page,
  responseDelayMs: () => number = () => 0,
) {
  await page.route("**/api/v1/order-schedules/preview", async (route) => {
    const request = route.request();
    if (request.method() !== "POST") {
      await route.continue();
      return;
    }
    const payload = request.postDataJSON() as {
      direction: "LONG" | "SHORT";
      instrument_ref: string;
      max_notional: string;
      reference_price?: string | null;
      schedule_ref: string;
      spec: {
        amount_distribution: { base_notional: string };
        protection_policy: {
          initial_stop: { distance_bps: string };
          full_fill_loss_budget?: {
            entry_fee_bps: string;
            exit_fee_bps: string;
          } | null;
        };
        price_distribution:
          | { kind: "SINGLE"; limit_price?: string | null }
          | {
            kind: "LADDER";
            lower_price: string;
            upper_price: string;
            level_count: number;
          };
      };
      venue_ref: "BINANCE_USDM";
    };
    const referencePrice = Number(payload.reference_price ?? "65001");
    const distribution = payload.spec.price_distribution;
    const prices = distribution.kind === "SINGLE"
      ? [Number(distribution.limit_price ?? referencePrice)]
      : Array.from({ length: distribution.level_count }, (_, index) => {
        const lower = Number(distribution.lower_price);
        const upper = Number(distribution.upper_price);
        return lower + (upper - lower) * index / (distribution.level_count - 1);
      });
    const requestedNotional = Number(payload.spec.amount_distribution.base_notional);
    const normalizedLegs = prices.map((price, index) => ({
      leg_index: index,
      leg_count: prices.length,
      release_after_seconds: 0,
      raw_price: price.toFixed(1),
      price: price.toFixed(1),
      sizing_price: price.toFixed(1),
      requested_notional: requestedNotional.toFixed(1),
      quantity: (requestedNotional / price).toFixed(4),
      effective_notional: requestedNotional.toFixed(1),
    }));
    const totalNotional = (requestedNotional * prices.length).toFixed(1);
    const totalQuantity = normalizedLegs.reduce(
      (total, leg) => total + Number(leg.quantity),
      0,
    );
    const effectiveNotional = normalizedLegs.reduce(
      (total, leg) => total + Number(leg.price) * Number(leg.quantity),
      0,
    );
    const averageEntry = effectiveNotional / totalQuantity;
    const stopDistanceBps = Number(
      payload.spec.protection_policy.initial_stop.distance_bps,
    );
    const rawStop = payload.direction === "LONG"
      ? averageEntry * (1 - stopDistanceBps / 10_000)
      : averageEntry * (1 + stopDistanceBps / 10_000);
    const roundToTick = payload.direction === "LONG" ? Math.ceil : Math.floor;
    const stopPrice = roundToTick(rawStop * 10) / 10;
    const entryFeeBps = Number(
      payload.spec.protection_policy.full_fill_loss_budget?.entry_fee_bps ?? "2",
    );
    const exitFeeBps = Number(
      payload.spec.protection_policy.full_fill_loss_budget?.exit_fee_bps ?? "5",
    );
    const grossLoss = totalQuantity * Math.abs(averageEntry - stopPrice);
    const entryFee = effectiveNotional * entryFeeBps / 10_000;
    const exitFee = totalQuantity * stopPrice * exitFeeBps / 10_000;
    const sourceCutoff = new Date().toISOString();
    const delay = responseDelayMs();
    if (delay > 0) {
      await new Promise((resolve) => setTimeout(resolve, delay));
    }
    await route.fulfill({
      contentType: "application/json",
      json: {
        valid: true,
        compiler_version: "e2e-preview-v1",
        schedule_ref: payload.schedule_ref,
        schedule_digest: "1".repeat(64),
        schedule_spec: payload.spec,
        preprotected_parallel_supported: true,
        venue_ref: payload.venue_ref,
        instrument_ref: payload.instrument_ref,
        direction: payload.direction,
        max_notional: payload.max_notional,
        reference_price: String(referencePrice),
        instrument_rules: {
          source: "E2E_DETERMINISTIC_RULES",
          min_price: "0.1",
          max_price: "1000000",
          price_tick_size: "0.1",
          limit_quantity_step: "0.0001",
          min_limit_quantity: "0.0001",
          max_limit_quantity: "1000",
          market_quantity_step: "0.0001",
          min_market_quantity: "0.0001",
          max_market_quantity: "1000",
          min_notional: "5",
          source_cutoff: sourceCutoff,
        },
        instrument_rules_digest: "2".repeat(64),
        source_cutoff: sourceCutoff,
        requested_total_notional: totalNotional,
        effective_total_notional: totalNotional,
        full_fill_protection_estimate: {
          average_entry_price: String(averageEntry),
          entry_boundary_price: String(
            payload.direction === "LONG" ? Math.min(...prices) : Math.max(...prices),
          ),
          stop_price: stopPrice.toFixed(1),
          quantity: String(totalQuantity),
          gross_price_loss: String(grossLoss),
          estimated_entry_fee: String(entryFee),
          estimated_exit_fee: String(exitFee),
          maximum_projected_loss: String(grossLoss + entryFee + exitFee),
        },
        normalized_legs: normalizedLegs,
        legs: normalizedLegs,
        issues: [],
      },
    });
  });
}

async function routeCurrentDemoMarketWindow(page: Page) {
  await page.route("**/api/v1/market-window?**", async (route) => {
    const url = new URL(route.request().url());
    const interval = url.searchParams.get("interval") ?? "15m";
    const intervalMs = interval === "1m"
      ? 60_000
      : interval === "5m"
        ? 5 * 60_000
        : interval === "15m"
          ? 15 * 60_000
          : interval === "1h"
            ? 60 * 60_000
            : interval === "4h"
              ? 4 * 60 * 60_000
              : 24 * 60 * 60_000;
    const endAt = Date.now();
    const bars = Array.from({ length: 48 }, (_value, index) => {
      const openAt = endAt - (48 - index) * intervalMs;
      const open = 64_950 + index;
      return {
        open_at: new Date(openAt).toISOString(),
        close_at: new Date(openAt + intervalMs).toISOString(),
        open: String(open),
        high: String(open + 8),
        low: String(open - 6),
        close: String(open + 2),
        volume: String(10 + index),
      };
    });
    await route.fulfill({
      contentType: "application/json",
      json: {
        instrument_ref: "BTCUSDT-PERP",
        interval,
        source: "BINANCE_DEMO_PUBLIC",
        source_cutoff: new Date(endAt).toISOString(),
        bars,
      },
    });
  });
}

async function routeCurrentDemoMarketContext(page: Page) {
  await page.route("**/api/v1/market-context?**", async (route) => {
    const sourceCutoff = new Date().toISOString();
    const stopReferenceInterval = new URL(route.request().url()).searchParams
      .get("stop_reference_interval") ?? "15m";
    await route.fulfill({
      contentType: "application/json",
      json: {
        instrument_ref: "BTCUSDT-PERP",
        source: "BINANCE_DEMO_PUBLIC",
        source_cutoff: sourceCutoff,
        bid_price: "65000",
        ask_price: "65002",
        reference_price: "65001",
        latest_close_1m: "65001",
        latest_closed_1m_at: sourceCutoff,
        latest_volume_1m: "100",
        latest_trade_count_1m: 50,
        latest_close_15m: "65000",
        latest_closed_15m_at: sourceCutoff,
        latest_closed_stop_reference_at: sourceCutoff,
        channel_lookback_15m: 20,
        stop_reference_interval: stopReferenceInterval,
        channel_upper: "65100",
        channel_lower: "64900",
        atr_14: "100",
        stop_reference_atr_14: "100",
        long_breakout_gap_pct: "0.1538",
        short_breakout_gap_pct: "0.1554",
        stop_references: [
          {
            kind: "SWING_OBV",
            side: "LOWER",
            price: "64900",
            interval: stopReferenceInterval,
            lookback_bars: 20,
            atr_buffer_multiple: "0.2",
            volume_bias: "POSITIVE",
            trend_slope: null,
            trend_r_squared: null,
            method_version: "STOP_REFERENCE_MULTI_INTERVAL_V1",
          },
          {
            kind: "STRUCTURE_ATR",
            side: "LOWER",
            price: "64800",
            interval: stopReferenceInterval,
            lookback_bars: 20,
            atr_buffer_multiple: "0.2",
            volume_bias: null,
            trend_slope: null,
            trend_r_squared: null,
            method_version: "STOP_REFERENCE_MULTI_INTERVAL_V1",
          },
          {
            kind: "TREND_ATR",
            side: "LOWER",
            price: "64700",
            interval: stopReferenceInterval,
            lookback_bars: 20,
            atr_buffer_multiple: "0.8",
            volume_bias: null,
            trend_slope: "12.5",
            trend_r_squared: "0.82",
            method_version: "STOP_REFERENCE_MULTI_INTERVAL_V1",
          },
          {
            kind: "STRUCTURE_ATR",
            side: "UPPER",
            price: "65200",
            interval: stopReferenceInterval,
            lookback_bars: 20,
            atr_buffer_multiple: "0.2",
            volume_bias: null,
            trend_slope: null,
            trend_r_squared: null,
            method_version: "STOP_REFERENCE_MULTI_INTERVAL_V1",
          },
        ],
      },
    });
  });
}

async function addBrowserScopedCsrfCookie(page: Page) {
  const browserBaseUrl = new URL(
    process.env.HALPHA_BROWSER_BASE_URL ?? "http://127.0.0.1:8765",
  );
  const browserPort = browserBaseUrl.port
    || (browserBaseUrl.protocol === "https:" ? "443" : "80");
  await page.context().addCookies([{
    name: `halpha_csrf_${browserPort}`,
    value: "e2e-planning-token",
    url: `${browserBaseUrl.origin}/`,
  }]);
}

async function routeDirectDraftAutosave(
  page: Page,
  unexpectedTradingWrites: string[],
  beforeCreateResponse?: () => Promise<void>,
) {
  const planId = "workspace-autosave-fixture";
  let saved: ReturnType<typeof syntheticDirectDraft> | null = null;
  await page.route(/\/api\/v1\/plans(?:\/.*)?(?:\?.*)?$/, async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const isCreate = path === "/api/v1/plans" && request.method() === "POST";
    const isUpdate = path === `/api/v1/plans/${planId}` && request.method() === "PUT";
    if (isCreate || isUpdate) {
      const payload = request.postDataJSON() as Record<string, unknown>;
      const { max_margin, max_notional, max_allowed_loss, valid_minutes, ...content } = payload;
      const now = new Date().toISOString();
      const base = syntheticDirectDraft(planId, (saved?.draft_version ?? 0) + 1, String(payload.plan_name));
      saved = {
        ...base,
        environment_id: "binance-demo-primary",
        content: {
          ...base.content,
          environment_id: "binance-demo-primary",
          account_ref: "binance-usdm-demo-owner-primary",
          authority_class: "DEMO_SIMULATION",
          creator_kind: saved?.content.creator_kind ?? base.content.creator_kind,
          ...content,
          created_at: saved?.content.created_at ?? now,
          requested_limits: {
            max_margin: String(max_margin),
            max_notional: String(max_notional),
            max_allowed_loss: String(max_allowed_loss),
          },
          valid_from: now,
          valid_until: new Date(Date.parse(now) + Number(valid_minutes) * 60_000).toISOString(),
        },
        updated_at: now,
      };
      if (isCreate) await beforeCreateResponse?.();
      await route.fulfill({ status: isCreate ? 201 : 200, json: saved });
      return;
    }
    if (request.method() === "GET" && path === `/api/v1/plans/${planId}`) {
      await route.fulfill({ json: saved });
      return;
    }
    if (request.method() === "GET" && path === `/api/v1/plans/${planId}/ai-review/latest`) {
      await route.fulfill({ json: null });
      return;
    }
    if (request.method() !== "GET" && request.method() !== "HEAD") {
      unexpectedTradingWrites.push(`${request.method()} ${path}`);
      await route.abort();
      return;
    }
    await route.fallback();
  });
  await page.route(/\/api\/v1\/activations(?:\/.*)?(?:\?.*)?$/, async (route) => {
    const request = route.request();
    if (request.method() !== "GET" && request.method() !== "HEAD") {
      unexpectedTradingWrites.push(`${request.method()} ${new URL(request.url()).pathname}`);
      await route.abort();
      return;
    }
    await route.fallback();
  });
  return () => saved;
}

async function expectDirectSubmitProblem(page: Page, message: string | RegExp) {
  const submit = page.getByRole("button", { name: "提交并启动", exact: true });
  await expect(submit).toBeDisabled();
  await submit.locator("..").hover();
  await expect(page.getByRole("tooltip")).toContainText(message);
  await page.mouse.move(8, 8);
  await expect(page.getByRole("tooltip")).toBeHidden();
}

async function openDirectMilestone(
  page: Page,
  milestone: "1 入场" | "2 保护" | "3 退出" | "4 核对" | "5 AI审核",
) {
  const navigation = page.getByRole("navigation", { name: "计划创建步骤" });
  await expect(navigation).toBeVisible({ timeout: 15_000 });
  const milestoneLabel = milestone.replace(/^\d+\s+/, "");
  const button = navigation.getByRole("button", {
    name: new RegExp(`${milestoneLabel}$`),
  });
  await expect(button).toBeEnabled();
  await button.click();
  await expect(button).toHaveAttribute("aria-current", "step");
}

async function completeDirectDecisionRecord(page: Page) {
  const intent = page.getByRole("combobox", { name: "本次目的" });
  await intent.click();
  await page.getByRole("option", { name: "盈利导向交易" }).click();

  const setupFamily = page.getByRole("combobox", { name: "交易形态" });
  await setupFamily.click();
  await page.getByRole("option", { name: "突破延续" }).click();

  await page.getByRole("textbox", { name: "交易理由" })
    .fill("闭合价格确认突破，保护位有效且计划退出空间覆盖风险后执行；若入场边界失效则放弃本次交易。");
}

async function openDirectReview(page: Page) {
  await openDirectMilestone(page, "4 核对");
  await expect(page.getByRole("heading", { name: "计划概要" })).toBeVisible();
  await completeDirectDecisionRecord(page);
}

async function openDirectAiReview(page: Page) {
  await openDirectReview(page);
  await openDirectMilestone(page, "5 AI审核");
  await expect(page.getByRole("heading", { name: "AI 交易审核" })).toBeVisible();
}

function rectsIntersect(left: LayoutRect, right: LayoutRect, tolerance = 0.5) {
  return left.x < right.x + right.width - tolerance
    && left.x + left.width > right.x + tolerance
    && left.y < right.y + right.height - tolerance
    && left.y + left.height > right.y + tolerance;
}

async function expectNoOverlap(
  left: Locator,
  right: Locator,
  message: string,
) {
  await expect(left).toBeVisible();
  await expect(right).toBeVisible();
  const [leftBox, rightBox] = await Promise.all([left.boundingBox(), right.boundingBox()]);
  expect(leftBox, `${message}：左侧元素缺少布局框`).not.toBeNull();
  expect(rightBox, `${message}：右侧元素缺少布局框`).not.toBeNull();
  expect(
    rectsIntersect(leftBox!, rightBox!),
    `${message}：${JSON.stringify({ left: leftBox, right: rightBox })}`,
  ).toBe(false);
}

async function assertEditorSectionHeadingClear(
  page: Page,
  headingName: string,
  firstFieldLabel: string,
) {
  const heading = page.getByRole("heading", { name: headingName, exact: true });
  const section = heading.locator("xpath=ancestor::section[1]");
  const field = section.getByLabel(firstFieldLabel, { exact: true });
  const formControl = field.locator(
    "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' MuiFormControl-root ')][1]",
  );
  const fieldLabel = formControl.locator(".MuiInputLabel-root").first();
  const fieldOutline = formControl.locator("fieldset").first();
  await heading.scrollIntoViewIfNeeded();
  await expectNoOverlap(heading, fieldLabel, `${headingName}标题不得覆盖首个字段标签`);
  await expectNoOverlap(heading, fieldOutline, `${headingName}标题不得覆盖首个字段边框`);
}

async function assertChartHeaderClear(chartRegion: Locator) {
  const subtitle = chartRegion.getByText(/输入线可拖动/).first();
  const toolButtons = chartRegion.getByRole("button", {
    name: /^(拖动选择区间|支撑 \/ 阻力|趋势线|清除分析线)$/,
  });
  await subtitle.scrollIntoViewIfNeeded();
  await expect(toolButtons).toHaveCount(4);
  for (let index = 0; index < await toolButtons.count(); index += 1) {
    const toolButton = toolButtons.nth(index);
    await expectNoOverlap(
      subtitle,
      toolButton,
      `图表副标题不得覆盖工具栏第 ${index + 1} 个按钮`,
    );
  }
}

async function assertLastChartDetailReachable(
  chartRegion: Locator,
  testInfo: TestInfo,
  viewportName: string,
) {
  const detailSection = chartRegion.locator("details").filter({
    hasText: /图线与操作点/,
  }).first();
  const detailItems = chartRegion.locator([
    '[aria-label="图中价格标注及等价数值"] > li',
    '[aria-label="图中相对和动态价格规则"] > li',
    '[aria-label="图中分析绘图及锚点"] > li',
  ].join(", "));
  await expect(detailSection).toBeVisible();
  if (!await detailSection.evaluate((element) => (
    (element as HTMLDetailsElement).open
  ))) {
    await detailSection.locator("summary").click();
  }
  await expect.poll(async () => detailSection.evaluate((element) => (
    (element as HTMLDetailsElement).open
  )), {
    timeout: 10_000,
  }).toBe(true);
  await expect.poll(async () => detailItems.count(), {
    timeout: 10_000,
  }).toBeGreaterThan(0);
  const lastDetail = detailItems.last();
  const scrollTarget = await lastDetail.evaluate((element) => {
    const scrollingElement = document.scrollingElement as HTMLElement | null;
    let current = element.parentElement;
    while (current && current !== document.body && current !== document.documentElement) {
      const style = window.getComputedStyle(current);
      if (
        /^(auto|scroll|overlay)$/.test(style.overflowY)
        && current.scrollHeight > current.clientHeight + 1
      ) {
        return {
          kind: "element",
          tag: current.tagName,
          testId: current.dataset.testid ?? null,
          overflowY: style.overflowY,
        };
      }
      current = current.parentElement;
    }
    return {
      kind: "document",
      tag: scrollingElement?.tagName ?? null,
      testId: null,
      overflowY: scrollingElement ? window.getComputedStyle(scrollingElement).overflowY : null,
    };
  });

  await lastDetail.scrollIntoViewIfNeeded();
  const visibility = await lastDetail.evaluate((element) => {
    const elementRect = element.getBoundingClientRect();
    const clippingAncestors: Array<{
      tag: string;
      testId: string | null;
      overflowY: string;
      top: number;
      bottom: number;
    }> = [];
    let visibleTop = 0;
    let visibleBottom = window.innerHeight;
    let current = element.parentElement;
    while (current && current !== document.documentElement) {
      const style = window.getComputedStyle(current);
      if (/^(auto|scroll|overlay|hidden|clip)$/.test(style.overflowY)) {
        const bounds = current.getBoundingClientRect();
        visibleTop = Math.max(visibleTop, bounds.top);
        visibleBottom = Math.min(visibleBottom, bounds.bottom);
        clippingAncestors.push({
          tag: current.tagName,
          testId: current.dataset.testid ?? null,
          overflowY: style.overflowY,
          top: bounds.top,
          bottom: bounds.bottom,
        });
      }
      current = current.parentElement;
    }
    return {
      element: {
        top: elementRect.top,
        bottom: elementRect.bottom,
        height: elementRect.height,
      },
      visibleTop,
      visibleBottom,
      clippingAncestors,
      fullyVisible: elementRect.height > 0
        && elementRect.top >= visibleTop - 1
        && elementRect.bottom <= visibleBottom + 1,
    };
  });
  await testInfo.attach(`${viewportName}-chart-detail-scroll.json`, {
    body: Buffer.from(JSON.stringify({ scrollTarget, visibility }, null, 2)),
    contentType: "application/json",
  });
  expect(
    scrollTarget.kind === "document"
      || /^(auto|scroll|overlay)$/.test(scrollTarget.overflowY ?? ""),
    `图表详情必须由文档或显式纵向滚动容器承载：${JSON.stringify(scrollTarget)}`,
  ).toBe(true);
  expect(
    visibility.fullyVisible,
    `展开详情的最后一条等价值/分析项被 overflow 永久裁剪：${JSON.stringify(visibility)}`,
  ).toBe(true);
}

async function assertNoDocumentHorizontalOverflow(
  page: Page,
  testInfo: TestInfo,
  viewportName: string,
) {
  const layout = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
    offenders: [...document.querySelectorAll<HTMLElement>("body *")]
      .filter((element) => !element.classList.contains("MuiSwitch-input"))
      .filter((element) => {
        const drawer = element.closest<HTMLElement>(".MuiDrawer-paper");
        if (!drawer) return true;
        const bounds = drawer.getBoundingClientRect();
        return bounds.right > 0 && bounds.left < document.documentElement.clientWidth;
      })
      .map((element) => {
        const bounds = element.getBoundingClientRect();
        return {
          tag: element.tagName,
          testId: element.dataset.testid ?? null,
          left: bounds.left,
          right: bounds.right,
          text: element.textContent?.trim().slice(0, 100) ?? "",
        };
      })
      .filter(({ left, right }) => left < -0.5 || right > document.documentElement.clientWidth + 0.5),
  }));
  await testInfo.attach(`${viewportName}-horizontal-overflow.json`, {
    body: Buffer.from(JSON.stringify(layout, null, 2)),
    contentType: "application/json",
  });
  expect(
    layout.scrollWidth,
    `文档出现横向溢出：${JSON.stringify(layout.offenders.slice(0, 10))}`,
  ).toBe(layout.clientWidth);
}

test("direct execution layout stays usable without overlap or clipped chart details", async ({ page }, testInfo) => {
  const attemptedTradingWrites: string[] = [];
  await routeCurrentDemoMarketStream(page);
  await routeCurrentDemoMarketWindow(page);
  await routeCurrentDemoMarketContext(page);
  await routeReadyDemoExecutor(page);
  await routeValidOrderSchedulePreview(page);
  await routeDirectDraftAutosave(page, attemptedTradingWrites);

  const viewports = testInfo.project.name === "chromium-narrow"
    ? directExecutionViewports.narrow
    : directExecutionViewports.desktop;
  await page.setViewportSize(viewports[0]!);
  await page.goto("/plans/new?mode=direct");
  const chartRegion = page.locator('section[aria-labelledby="order-schedule-chart-title"]');
  await expect(chartRegion).toBeVisible({ timeout: 15_000 });
  await chartRegion.getByText(/图线与操作点/).click();

  for (const viewport of viewports) {
    await page.setViewportSize(viewport);
    await page.evaluate(() => new Promise<void>((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
    }));

    await openDirectMilestone(page, "1 入场");
    await assertChartHeaderClear(chartRegion);
    await assertEditorSectionHeadingClear(page, "下单金额", "下单金额（USDT）");
    await openDirectMilestone(page, "2 保护");
    await assertEditorSectionHeadingClear(page, "成交后立即保护", "初始止损距离（bps）");
    await openDirectMilestone(page, "3 退出");
    await expect(page.getByRole("heading", { name: "自动退出", exact: true })).toBeVisible();
    await openDirectReview(page);
    await expect(page.getByText(/^技术预览可保存 · 1 档 · 标准化总额/)).toBeVisible({ timeout: 15_000 });
    await assertLastChartDetailReachable(chartRegion, testInfo, viewport.name);
    await assertNoDocumentHorizontalOverflow(page, testInfo, viewport.name);
  }

  expect(attemptedTradingWrites, "布局回归允许内存草稿自动保存，不得确认或启动计划").toEqual([]);
  await expect(page).toHaveURL(/\/plans\/workspace-autosave-fixture\/edit$/);
  await testInfo.attach(`direct-layout-${testInfo.project.name}.png`, {
    body: await page.screenshot({ fullPage: true }),
    contentType: "image/png",
  });
});

test("direct shortcut reaches a launch-ready workspace once its decision record is complete", async ({ page }) => {
  const attemptedTradingWrites: string[] = [];
  await routeCurrentDemoMarketStream(page);
  await routeCurrentDemoMarketWindow(page);
  await routeCurrentDemoMarketContext(page);
  await routeReadyDemoExecutor(page);
  await routeValidOrderSchedulePreview(page);
  await routeDirectDraftAutosave(page, attemptedTradingWrites);

  await page.goto("/plans");
  await page.getByRole("button", { name: "直接执行", exact: true }).click();

  await expect(page).toHaveURL(/\/plans\/new\?mode=direct$/);
  await expect(page.getByRole("heading", { name: "选择执行依据" })).toHaveCount(0);
  await expect(page.getByLabel("计划名称")).toHaveValue(/^BTCUSDT 直接执行 .+/);
  await expect(page.getByLabel("计划有效分钟")).toHaveValue("60");
  await openDirectReview(page);
  await expect(page.getByRole("button", {
    name: "提交并启动",
    exact: true,
  })).toBeDisabled({ timeout: 20_000 });
  await expect(page.getByRole("region", { name: "计划概要", exact: true }))
    .toContainText("TP1 2R / 100%");
  expect(attemptedTradingWrites).toEqual([]);
});

test("direct AI review shows live progress, binds approval, invalidates edits, and blocks rejection", async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name === "chromium-narrow",
    "The stateful review flow is covered on desktop; narrow layout remains covered by the workspace layout test.",
  );
  const firstDigest = "d".repeat(64);
  const secondDigest = "e".repeat(64);
  let draftVersion = 1;
  let reviewDraftVersion = 1;
  let reviewDigest = firstDigest;
  let reviewSubmissionCount = 0;
  let currentReview: ReturnType<typeof syntheticPlanAiReview> | null = null;
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  const evidenceDirectory = process.env.HALPHA_BROWSER_EVIDENCE_DIR;
  const captureEvidence = async (name: string) => {
    const body = await page.screenshot({
      fullPage: true,
      ...(evidenceDirectory ? { path: `${evidenceDirectory}/${name}` } : {}),
    });
    await testInfo.attach(name, { body, contentType: "image/png" });
  };
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await page.setViewportSize({ width: 1672, height: 918 });

  await routeCurrentDemoMarketStream(page);
  await routeCurrentDemoMarketWindow(page);
  await routeCurrentDemoMarketContext(page);
  await routeReadyDemoExecutor(page);
  await page.route(/\/api\/v1\/overview(?:\?.*)?$/, async (route) => {
    await route.fulfill({
      contentType: "application/json",
      json: {
        new_risk_discipline: {
          status: "ALLOWED",
          new_risk_allowed: true,
          blocker_codes: [],
          available_notional_capacity: "1000",
          max_plan_loss: "100",
          minimum_reward_risk_ratio: "1",
          open_risk_limit: "200",
          open_risk_committed: "0",
        },
      },
    });
  });
  await page.route(/\/api\/v1\/plans(?:\?.*)?$/, async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    const requestPayload = await route.request().postDataJSON() as { plan_name?: string };
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      json: {
        ...syntheticDirectDraft(
          "ai-review-e2e",
          1,
          requestPayload.plan_name ?? "AI 审核交互验证",
        ),
        content_digest: firstDigest,
      },
    });
  });
  await page.route(/\/api\/v1\/plans\/ai-review-e2e(?:\?.*)?$/, async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({
        contentType: "application/json",
        json: {
          ...syntheticDirectDraft("ai-review-e2e", draftVersion, "AI 审核交互验证"),
          content_digest: draftVersion === 1 ? firstDigest : secondDigest,
        },
      });
      return;
    }
    if (route.request().method() !== "PUT") {
      await route.continue();
      return;
    }
    draftVersion = 2;
    await route.fulfill({
      contentType: "application/json",
      json: {
        ...syntheticDirectDraft("ai-review-e2e", 2, "AI 审核交互验证"),
        content_digest: secondDigest,
      },
    });
  });
  await page.route("**/api/v1/plans/ai-review-e2e/ai-review/latest", async (route) => {
    await route.fulfill({ contentType: "application/json", json: currentReview });
  });
  await page.route("**/api/v1/plans/ai-review-e2e/ai-review", async (route) => {
    reviewSubmissionCount += 1;
    const payload = route.request().postDataJSON() as { draft_version?: number };
    reviewDraftVersion = payload.draft_version ?? draftVersion;
    reviewDigest = reviewDraftVersion === 1 ? firstDigest : secondDigest;
    currentReview = reviewSubmissionCount === 1
      ? syntheticPlanAiReview("QUEUED", reviewDraftVersion, reviewDigest, "review-1")
      : reviewSubmissionCount === 2
        ? syntheticPlanAiReview("REJECTED", reviewDraftVersion, reviewDigest, "review-2")
        : syntheticPlanAiReview(
          "FAILED",
          reviewDraftVersion,
          reviewDigest,
          "review-3",
          "PLAN_AI_REVIEW_CONTEXT_TEMPORARILY_UNAVAILABLE",
        );
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      json: currentReview,
    });
  });
  await page.routeWebSocket("**/api/v1/plan-ai-reviews/review-1/stream", (socket) => {
    const running = syntheticPlanAiReview("RUNNING", reviewDraftVersion, reviewDigest, "review-1");
    setTimeout(() => {
      currentReview = running;
      socket.send(JSON.stringify(running));
    }, 100);
    setTimeout(() => {
      const approved = syntheticPlanAiReview("APPROVED", reviewDraftVersion, reviewDigest, "review-1");
      currentReview = approved;
      socket.send(JSON.stringify(approved));
    }, 2_500);
  });

  await page.goto("/plans/new?mode=direct");
  await openDirectReview(page);
  await page.keyboard.press("Escape");
  await page.getByRole("heading", { name: "交易判断" }).click();
  await captureEvidence("plan-review-simplified.png");
  await openDirectMilestone(page, "5 AI审核");
  await expect(page.getByRole("heading", { name: "AI 交易审核" })).toBeVisible();
  await expect(page.getByRole("button", { name: "提交 AI 审核", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "提交 AI 审核", exact: true }).click();

  await expect(page.getByTestId("plan-ai-review-live-output")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText("正在核对保护、退出与当前纪律", { exact: true })).toBeVisible();
  await captureEvidence("ai-review-running.png");

  const approved = page.getByTestId("plan-ai-review-approved");
  await expect(approved).toBeVisible({ timeout: 10_000 });
  await expect(approved).toContainText("AI 审核结论：批准");
  await expect(approved).toContainText("AI 审核理由");
  // The final command is now a single submit action.  A live quote/preview
  // refresh must not turn this approved draft into a stale review.
  await expect(page.getByRole("button", { name: "提交并启动", exact: true })).toBeEnabled();
  await page.waitForTimeout(1_200);
  await expect(page.getByTestId("plan-ai-review-approved")).toBeVisible();
  await expect(page.getByRole("button", { name: "提交并启动", exact: true })).toBeEnabled();
  await captureEvidence("ai-review-approved.png");

  await openDirectMilestone(page, "4 核对");
  await page.getByRole("textbox", { name: "交易理由" })
    .fill("修改后的交易理由改变了入场依据，需要重新审核当前草稿。");
  await openDirectMilestone(page, "5 AI审核");
  await expect(page.getByTestId("plan-ai-review-stale")).toBeVisible();
  await expect(page.getByRole("button", { name: "提交并启动", exact: true })).toBeDisabled();
  await captureEvidence("ai-review-stale.png");

  await page.getByRole("button", { name: "按当前计划重新提交 AI 审核", exact: true }).click();
  const rejected = page.getByTestId("plan-ai-review-rejected");
  await expect(rejected).toBeVisible({ timeout: 10_000 });
  await expect(rejected).toContainText("AI 审核结论：拒绝");
  await expect(rejected).toContainText("AI 审核理由");
  await expect(rejected).toContainText("费用后收益风险关系不成立");
  await expect(rejected).toContainText("降低入场价格或重新设置保护位");
  await expect(page.getByRole("button", { name: "提交并启动", exact: true })).toBeDisabled();
  await captureEvidence("ai-review-rejected.png");
  await page.getByRole("button", { name: "按当前计划重新提交 AI 审核", exact: true }).click();
  const failed = page.getByTestId("plan-ai-review-failed");
  await expect(failed).toBeVisible({ timeout: 10_000 });
  await expect(failed).toContainText("非 AI 审核结论");
  await expect(failed).toContainText("审核未完成");
  await expect(failed).toContainText("本次审核未产生 AI 的“批准”或“拒绝”结论");
  await expect(failed).toContainText("行情数据连接暂时超时");
  await expect(failed).not.toContainText("AI 审核结论：拒绝");
  await captureEvidence("ai-review-failed.png");
  expect(draftVersion).toBe(2);
  expect(reviewSubmissionCount).toBe(3);
  expect(consoleErrors).toEqual([]);
  expect(pageErrors).toEqual([]);
});

test("direct configuration allocates and autosaves an editable draft", async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name === "chromium-narrow",
    "The persistence lifecycle is covered on desktop; narrow layout shares the same save control.",
  );
  const planId = "autosave-direct-e2e";
  let draftVersion = 1;
  let createCount = 0;
  let updateCount = 0;
  let savedName = "BTCUSDT 自动保存草稿";

  await routeCurrentDemoMarketStream(page);
  await routeCurrentDemoMarketWindow(page);
  await routeCurrentDemoMarketContext(page);
  await routeReadyDemoExecutor(page);
  await routeValidOrderSchedulePreview(page);
  await page.route(/\/api\/v1\/plans(?:\?.*)?$/, async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    createCount += 1;
    const payload = route.request().postDataJSON() as { plan_name?: string };
    savedName = payload.plan_name ?? savedName;
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      json: syntheticDirectDraft(planId, draftVersion, savedName),
    });
  });
  await page.route(`**/api/v1/plans/${planId}`, async (route) => {
    if (route.request().method() === "PUT") {
      updateCount += 1;
      draftVersion += 1;
      const payload = route.request().postDataJSON() as { plan_name?: string };
      savedName = payload.plan_name ?? savedName;
    }
    await route.fulfill({
      contentType: "application/json",
      json: syntheticDirectDraft(planId, draftVersion, savedName),
    });
  });

  await page.goto("/plans/new?mode=direct");
  await expect(page.getByRole("button", { name: "保存草稿", exact: true })).toBeEnabled({ timeout: 15_000 });
  await expect(page).toHaveURL(new RegExp(`/plans/${planId}/edit`), { timeout: 15_000 });
  await expect.poll(() => createCount).toBe(1);
  await expect(page.getByText("草稿已保存", { exact: false })).toBeVisible();

  const name = page.getByRole("textbox", { name: "计划名称" });
  await name.fill("自动保存后的草稿名称");
  await expect.poll(() => updateCount, { timeout: 10_000 }).toBe(1);
  await expect(page.getByText("草稿已保存", { exact: false })).toBeVisible();
  await expect(page.getByRole("button", { name: "保存草稿", exact: true })).toBeEnabled();
  await testInfo.attach("direct-draft-autosave.png", {
    body: await page.screenshot({ fullPage: true }),
    contentType: "image/png",
  });
});

test("the first autosave reply preserves a protection edit made while creation was pending", async ({ page }) => {
  const attemptedTradingWrites: string[] = [];
  let firstCreateStarted = false;
  let releaseCreate = () => {};
  const createResponseGate = new Promise<void>((resolve) => { releaseCreate = resolve; });
  await routeCurrentDemoMarketStream(page);
  await routeReadyDemoExecutor(page);
  const savedDraft = await routeDirectDraftAutosave(page, attemptedTradingWrites, async () => {
    firstCreateStarted = true;
    await createResponseGate;
  });

  try {
    await page.goto("/plans/new?mode=direct&creator_kind=AI");
    await expect.poll(() => firstCreateStarted, { timeout: 15_000 }).toBe(true);
    expect(savedDraft()!.content.order_schedule_spec.protection_policy.initial_stop.distance_bps).toBe("100");
    await openDirectMilestone(page, "2 保护");
    const stopDistance = page.getByRole("spinbutton", { name: "初始止损距离（bps）" });
    await stopDistance.fill("0");
    await expect(stopDistance).toHaveValue("0");

    const initialRead = page.waitForResponse((response) => (
      response.request().method() === "GET"
      && new URL(response.url()).pathname === "/api/v1/plans/workspace-autosave-fixture"
    ));
    releaseCreate();
    await initialRead;
    await expect(page).toHaveURL(/\/plans\/workspace-autosave-fixture\/edit$/);
    await page.evaluate(() => new Promise<void>((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
    }));
    await expect(stopDistance).toHaveValue("0");
    await expectDirectSubmitProblem(page, "初始止损距离必须大于 0 且低于 10000 bps。");
    expect(attemptedTradingWrites).toEqual([]);

    const serverDraft = savedDraft()!;
    const protection = serverDraft.content.order_schedule_spec.protection_policy;
    const reloadedDraft = {
      ...serverDraft,
      draft_version: serverDraft.draft_version + 1,
      content_digest: "f".repeat(64),
      content: {
        ...serverDraft.content,
        order_schedule_spec: {
          ...serverDraft.content.order_schedule_spec,
          protection_policy: {
            ...protection,
            initial_stop: { ...protection.initial_stop, distance_bps: "75" },
          },
        },
      },
    };
    await page.route("**/api/v1/plans/workspace-autosave-fixture", async (route) => {
      if (route.request().method() === "GET") await route.fulfill({ json: reloadedDraft });
      else await route.fallback();
    });
    await page.reload();
    await openDirectMilestone(page, "2 保护");
    await expect(stopDistance).toHaveValue("75");
    expect(attemptedTradingWrites).toEqual([]);
  } finally {
    releaseCreate();
  }
});

test("plan creation entry explains a blocked new-risk discipline without disabling draft preparation", async ({ page }, testInfo) => {
  await routeReadyDemoExecutor(page);
  await page.route(/\/api\/v1\/overview(?:\?.*)?$/, async (route) => {
    await route.fulfill({
      contentType: "application/json",
      json: {
        new_risk_discipline: {
          status: "BLOCKED",
          new_risk_allowed: false,
          blocker_codes: ["NEW_RISK_DAILY_LOSS_STOP_REACHED"],
          day_window_started_at: "2026-08-12T16:00:00Z",
          week_window_started_at: "2026-08-10T16:00:00Z",
          rolling_drawdown_lookback_days: 30,
        },
      },
    });
  });
  await page.route(/\/api\/v1\/plans(?:\?.*)?$/, async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ contentType: "application/json", json: [] });
      return;
    }
    await route.continue();
  });
  await page.route(/\/api\/v1\/activations(?:\?.*)?$/, async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ contentType: "application/json", json: [] });
      return;
    }
    await route.continue();
  });
  await page.route("**/api/v1/strategies", async (route) => {
    await route.fulfill({ contentType: "application/json", json: [] });
  });

  await page.goto("/plans");

  const notice = page.getByRole("alert").filter({
    hasText: "新增风险纪律未通过",
  });
  await expect(notice).toContainText("当日已实现亏损已触发新增风险停止");
  await expect(notice).toContainText("2026-08-14 00:00:00");
  await expect(notice).not.toContainText("UTC+8");
  await expect(notice).toContainText("不是恢复承诺");
  await expect(notice).toContainText("创建入口仍可用于准备或保存草稿");
  await expect(page.getByRole("button", { name: "直接执行", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: "选择策略", exact: true })).toBeEnabled();
  const evidenceDirectory = process.env.HALPHA_BROWSER_EVIDENCE_DIR;
  const body = await page.screenshot({
    fullPage: true,
    ...(evidenceDirectory
      ? { path: `${evidenceDirectory}/discipline-entry.png` }
      : {}),
  });
  await testInfo.attach("discipline-entry.png", {
    body,
    contentType: "image/png",
  });
});

test("current plan card keeps its detail entry and visualizes a paused plan consistently", async ({ page }, testInfo) => {
  const activationId = "paused-entry-continuity";
  const startedAt = new Date(Date.now() - 4 * 60 * 60_000).toISOString();
  const filledAt = new Date(Date.now() - 3 * 60 * 60_000).toISOString();
  const pausedAt = new Date(Date.now() - 30 * 60_000).toISOString();
  const activation = {
    activation_id: activationId,
    plan_version_ref: "paused-plan-version",
    plan_name: "BTCUSDT 分批入场",
    instrument_ref: "BTCUSDT-PERP",
    direction: "LONG",
    lifecycle: "RUNNING",
    run_state: "PAUSED",
    pause_reason: "WRITER_CONTINUITY_LOST",
    paused_at: pausedAt,
    protection_state: "WORKING",
    state_version: 2,
    has_entry_fill: true,
    entry_opportunity_consumed: false,
    rule_state: {
      deadlines: {
        entry_valid_until: new Date(Date.now() + 60 * 60_000).toISOString(),
      },
    },
    created_at: startedAt,
    updated_at: pausedAt,
    result_ref: null,
    closure_reason_code: null,
    primary_result: null,
    trade_result: null,
  };
  const detail = {
    activation,
    plan: {
      plan_id: "paused-plan",
      plan_name: activation.plan_name,
      created_at: activation.created_at,
      max_notional: "5000",
    },
    capital: {
      max_notional: "5000",
      new_risk_stopped: false,
    },
    trade_result: {
      fill_count: 1,
      position_quantity: "0.0031",
      commission: "0.1",
      commission_complete: true,
      funding: "-0.01",
      funding_complete: true,
      funding_included: true,
      fills: [{
        action_kind: "ENTRY",
        fill_time: filledAt,
        order_side: "BUY",
        price: "64975",
        quantity: "0.0031",
        fee: "0.1",
        fee_currency: "USDT",
      }],
    },
    position_attribution: {
      activation_signed_position: "0.0031",
      exchange_net_position: "0.0031",
      reconciliation_status: "MATCH",
    },
    decision_basis: {
      kind: "DIRECT_EXECUTION",
      decision_basis_ref: "DIRECT_EXECUTION@1",
    },
    order_schedule: null,
    strategy: null,
    execution_actions: [],
    venue_facts: [{
      kind: "FUNDING",
      source_time: new Date(Date.now() - 60 * 60_000).toISOString(),
      payload: { income: "-0.01" },
    }],
    receipts: [],
  };

  await addBrowserScopedCsrfCookie(page);
  await routeReadyDemoExecutor(page);
  await routeCurrentDemoMarketStream(page);
  await routeCurrentDemoMarketWindow(page);
  await routeCurrentDemoMarketContext(page);
  await page.route(/\/api\/v1\/plans(?:\?.*)?$/, async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify([{
        plan_id: "paused-plan",
        plan_version_id: activation.plan_version_ref,
        max_notional: "5000",
        position_alignment: null,
        order_schedule_spec: {
          entry_program: { kind: "PRICE_LADDER", slice_count: 1 },
          price_distribution: {
            kind: "LADDER",
            lower_price: "63439",
            upper_price: "63739.4",
            level_count: 10,
          },
          venue_policy: {
            order_type: "LIMIT",
            post_only: false,
            price_match: null,
          },
        },
      }]),
    });
  });
  await page.route(/\/api\/v1\/strategies(?:\?.*)?$/, async (route) => {
    await route.fulfill({ contentType: "application/json", body: "[]" });
  });
  await page.route(/\/api\/v1\/reviews(?:\?.*)?$/, async (route) => {
    await route.fulfill({ contentType: "application/json", body: "[]" });
  });
  await page.route(/\/api\/v1\/activations(?:\/.*)?(?:\?.*)?$/, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/v1/activations") {
      await route.fulfill({
        contentType: "application/json",
        headers: { "cache-control": "no-store" },
        body: JSON.stringify([activation]),
      });
      return;
    }
    if (url.pathname === `/api/v1/activations/${activationId}/timeline`) {
      await route.fulfill({ contentType: "application/json", body: "[]" });
      return;
    }
    if (url.pathname === `/api/v1/activations/${activationId}`) {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(detail),
      });
      return;
    }
    await route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
  });

  await page.goto("/plans");

  const card = page.getByRole("link", { name: `查看计划详情 ${activation.plan_name}` });
  await expect(card.getByText("计划运行中", { exact: true })).toBeVisible();
  await expect(card.getByText("新增入场暂停", { exact: true })).toBeVisible();
  await expect(card.getByText("已暂停", { exact: true })).toHaveCount(0);
  await expect(card.getByText("保护有效", { exact: true })).toBeVisible();
  await expect(card).toContainText("执行器连接中断后，新的入场已暂停");
  await expect(card).toContainText("现有止损、止盈和退出不受影响");
  await expect(card).not.toContainText("原因：");
  await expect(card).not.toContainText("恢复：");

  await expect(card.getByText("计划运行中", { exact: true }).locator(".."))
    .toHaveClass(/MuiChip-colorSuccess/);
  await expect(card.getByText("计划运行中", { exact: true }).locator(".."))
    .toHaveClass(/MuiChip-outlined/);
  await expect(card.getByText("新增入场暂停", { exact: true }).locator(".."))
    .toHaveClass(/MuiChip-colorWarning/);
  await expect(card.getByText("保护有效", { exact: true }).locator(".."))
    .toHaveClass(/MuiChip-colorSuccess/);

  const instrument = card.getByText("BTCUSDT-PERP", { exact: true });
  const direction = card.getByText("做多", { exact: true });
  await expect(instrument).toBeVisible();
  await expect(direction).toBeVisible();
  await expect(instrument).toHaveClass(/market-tone-up/);
  await expect(direction).toHaveClass(/market-tone-up/);
  expect(await instrument.evaluate((element) => Number.parseFloat(getComputedStyle(element).fontSize)))
    .toBeGreaterThanOrEqual(19);
  await expect(card.getByRole("heading", { name: activation.plan_name, exact: true })).toBeVisible();
  const updatedAt = card.getByText(/更新 \d{2}\/\d{2} \d{2}:\d{2}/);
  await expect(updatedAt).toBeVisible();
  await expect(updatedAt).not.toContainText("UTC+8");
  await expect(card.getByText("计划结构", { exact: true })).toBeVisible();
  await expect(card).toContainText("价格区间分批");
  await expect(card.getByText("计划金额", { exact: true })).toBeVisible();
  await expect(card).toContainText("5,000.00 USDT");
  const pnlHeading = card.getByText("费用后盈亏估算", { exact: true });
  await expect(pnlHeading).toBeVisible();
  await pnlHeading.hover();
  await expect(page.getByRole("tooltip")).toContainText("全部已确认手续费和已发生资金费");
  await expect(page.getByRole("tooltip")).toContainText("不含未来退出滑点");
  await page.mouse.move(8, 8);
  await expect(page.getByRole("tooltip")).toBeHidden();
  await expect(card.getByRole("group", {
    name: "费用后盈亏曲线；盈利和亏损按当前市场配色区分",
  })).toBeVisible();
  const pnlRange = card.getByText(/\d{2}\/\d{2} \d{2}:\d{2} → \d{2}\/\d{2} \d{2}:\d{2}/);
  await expect(pnlRange).toBeVisible();
  await expect(pnlRange).not.toContainText("UTC+8");
  await expect(card).not.toContainText("从计划开始");
  await expect(card.getByRole("button", { name: /查看详情/ })).toHaveCount(0);
  await expect(card).toHaveAttribute("href", `/activations/${activationId}`);
  await assertNoDocumentHorizontalOverflow(page, testInfo, `paused-plan-card-${testInfo.project.name}`);
  await assertAccessible(page, testInfo, `paused-plan-card-${testInfo.project.name}`);

  await card.click({ position: { x: 24, y: 90 } });
  await expect(page).toHaveURL(new RegExp(`/activations/${activationId}$`));
  await expect(page.getByRole("heading", { name: activation.plan_name, exact: true })).toBeVisible();

  activation.entry_opportunity_consumed = true;
  activation.rule_state.deadlines.entry_valid_until = new Date(Date.now() - 60_000).toISOString();
  await page.goto("/plans");
  await page.getByRole("button", { name: "刷新" }).click();
  const completedEntryCard = page.getByRole("link", {
    name: `查看计划详情 ${activation.plan_name}`,
  });
  await expect(completedEntryCard.getByText("入场已结束", { exact: true })).toBeVisible();
  await expect(completedEntryCard.getByText("新增入场暂停", { exact: true })).toHaveCount(0);
  await expect(completedEntryCard).not.toContainText("执行器连接中断");
  await completedEntryCard.click({ position: { x: 24, y: 90 } });
  await expect(page.getByRole("button", { name: /恢复新增入场/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /重新开放入场/ })).toBeVisible();
  await testInfo.attach(`paused-plan-${testInfo.project.name}.png`, {
    body: await page.screenshot({ fullPage: true }),
    contentType: "image/png",
  });
});

test("protection milestone offers explainable stop references without silently changing the plan", async ({ page }, testInfo) => {
  const attemptedTradingWrites: string[] = [];
  let previewDelayMs = 0;
  await addBrowserScopedCsrfCookie(page);
  await routeCurrentDemoMarketStream(page);
  await routeCurrentDemoMarketWindow(page);
  await routeCurrentDemoMarketContext(page);
  await routeReadyDemoExecutor(page);
  await routeValidOrderSchedulePreview(page, () => previewDelayMs);
  const savedDraft = await routeDirectDraftAutosave(page, attemptedTradingWrites);

  await page.goto("/plans/new?mode=direct");
  const chartRegion = page.locator('section[aria-labelledby="order-schedule-chart-title"]');
  const limitPrice = page.getByRole("spinbutton", { name: "限价（USDT）" });
  await expect(limitPrice).toHaveValue("65001");
  const initialLimitPrice = await limitPrice.inputValue();
  const expectChartIntervalUnchanged = async () => {
    if (testInfo.project.name === "chromium-narrow") {
      await expect(page.getByRole("combobox", { name: "K 线周期" })).toHaveText("15m");
      return;
    }
    await expect(chartRegion.getByRole("button", { name: "15m" }))
      .toHaveAttribute("aria-pressed", "true");
  };
  await expectChartIntervalUnchanged();
  await openDirectMilestone(page, "2 保护");

  const recommendations = page.getByTestId("initial-stop-recommendations");
  await expect(recommendations.getByText("止损候选", { exact: true })).toBeVisible();
  await expect(page.getByTestId("initial-stop-recommendation-swing_obv"))
    .toContainText("量价摆动位");
  await expect(page.getByTestId("initial-stop-recommendation-structure_atr"))
    .toContainText("近期结构位");
  await expect(page.getByTestId("initial-stop-recommendation-trend_atr"))
    .toContainText("趋势波动带");
  await page.getByRole("button", { name: "1h止损参考" }).click();
  await expect(recommendations).toContainText("1h K 线 · 截止");
  const swingLogic = page.getByRole("button", { name: "量价摆动位止损逻辑" });
  await swingLogic.hover();
  await expect(page.getByRole("tooltip")).toContainText("1h 摆动结构");
  await expect(page.getByRole("tooltip")).toContainText("OBV 偏正");
  await page.mouse.move(8, 8);
  await expect(page.getByRole("tooltip")).toBeHidden();
  await expect(page.getByTestId("initial-stop-recommendation-swing_obv"))
    .not.toContainText("…");
  await expectChartIntervalUnchanged();

  const stopDistance = page.getByRole("spinbutton", { name: "初始止损距离（bps）" });
  const before = await stopDistance.inputValue();
  const adoptSwing = page.getByRole("button", {
    name: /采用量价摆动位 64,900\.0 USDT/,
  });
  previewDelayMs = 600;
  await adoptSwing.click();
  await page.waitForTimeout(250);
  await expect(recommendations).toBeVisible();
  await expect(page.getByTestId("initial-stop-recommendation-swing_obv"))
    .toBeVisible();
  await expect(stopDistance).not.toHaveValue(before);
  await expect(adoptSwing).toBeDisabled();
  await expect(adoptSwing).toHaveCSS("background-color", "rgb(255, 212, 59)");
  const alternativeAdopt = page.getByTestId("initial-stop-recommendation-structure_atr")
    .locator("button").last();
  await expect(alternativeAdopt).toBeVisible();
  expect((await adoptSwing.boundingBox())?.width).toBe(
    (await alternativeAdopt.boundingBox())?.width,
  );
  const adoptedStopDistance = await stopDistance.inputValue();
  previewDelayMs = 0;
  await expect(page.getByTestId("initial-stop-projection"))
    .toContainText("全档成交预计均价");
  await expect(page.getByTestId("initial-stop-projection"))
    .toContainText("最大预计亏损");

  await chartRegion.getByText(/图线与操作点/).click();
  const chartPrices = chartRegion.getByLabel("图中价格标注及等价数值");
  await expect(chartPrices).toContainText("量价摆动位");
  await expect(chartPrices).not.toContainText("近期结构位");
  await expect(chartPrices).not.toContainText("趋势波动带");
  await expect(chartPrices).toContainText("预计止损触发价");
  await expect(chartPrices).toContainText("行情 · 公开行情输入");

  await page.getByRole("button", { name: "＋ 添加成交后动态止损" }).click();
  await page.getByRole("button", {
    name: "盈亏平衡止损 · 盈利 1R 后移到入场价",
  }).click();
  await expect(page.getByText("1R→0R", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "＋ 添加成交后动态止损" }).click();
  await page.getByRole("button", {
    name: "保底盈利止损 · 盈利 1R 后至少保住 0.5R",
  }).click();
  await expect(page.getByText("1R→0.5R", { exact: true })).toBeVisible();
  await expect(page.getByTestId("initial-stop-recommendation-swing_obv"))
    .toBeVisible({ timeout: 15_000 });
  await expect(adoptSwing).toBeDisabled();

  await expect.poll(async () => recommendations.evaluate(
    (element) => element.scrollWidth <= element.clientWidth,
  )).toBe(true);
  await assertNoDocumentHorizontalOverflow(page, testInfo, `stop-recommendations-${testInfo.project.name}`);
  await testInfo.attach(`stop-recommendations-${testInfo.project.name}.png`, {
    body: await recommendations.screenshot(),
    contentType: "image/png",
  });
  await testInfo.attach(`stop-protection-page-${testInfo.project.name}.png`, {
    body: await page.screenshot({ fullPage: true }),
    contentType: "image/png",
  });

  await openDirectMilestone(page, "1 入场");
  await expect(limitPrice).toHaveValue(initialLimitPrice);
  await expect(chartPrices).not.toContainText("量价摆动位");
  await expect.poll(() => savedDraft()?.content.order_schedule_spec.protection_policy.initial_stop.distance_bps)
    .toBe(adoptedStopDistance);
  expect(attemptedTradingWrites, "采用候选后只允许内存草稿自动保存，不得确认或启动计划").toEqual([]);
});

test("direct review blocks launch when the live price has already crossed a fixed entry boundary", async ({ page }) => {
  await routeCurrentDemoMarketStream(page);
  await routeReadyDemoExecutor(page);
  await page.goto("/plans/new?mode=direct");

  await page.getByRole("button", { name: "＋ 添加入场条件或管理规则" }).click();
  await page.getByRole("button", { name: /行情失效/ }).click();
  await page.getByRole("spinbutton", { name: "失效价（USDT）" }).fill("65100");
  await openDirectReview(page);

  await expectDirectSubmitProblem(page,
    "当前标记价 65,001.0 USDT 已达到或跌破入场失效价 65,100.0 USDT",
  );
  await expect(page.getByRole("button", {
    name: "提交并启动",
    exact: true,
  })).toBeDisabled();
  await expect(page.getByRole("button", {
    name: "保存草稿",
    exact: true,
  })).toBeEnabled();

  await openDirectMilestone(page, "1 入场");
  await page.getByRole("spinbutton", { name: "失效价（USDT）" }).fill("64900");
  await openDirectReview(page);

  const submit = page.getByRole("button", { name: "提交并启动", exact: true });
  await submit.locator("..").hover();
  await expect(page.getByRole("tooltip")).not.toContainText("已达到或跌破入场失效价");
  await page.mouse.move(8, 8);
  await expect(page.getByRole("button", {
    name: "提交并启动",
    exact: true,
  })).toBeDisabled();
});

test("an approved direct draft stops submission when its live entry boundary or executor becomes invalid", async ({ page }) => {
  test.setTimeout(90_000);
  let quote = "65001";
  const statusOverrides: Record<string, unknown> = {};
  const attemptedTradingWrites: string[] = [];
  await routeCurrentDemoMarketStream(page, () => quote);
  await routeReadyDemoExecutor(page, { statusOverrides });
  await page.route(/\/api\/v1\/overview(?:\?.*)?$/, async (route) => {
    await route.fulfill({ json: {
      new_risk_discipline: {
        status: "ALLOWED",
        new_risk_allowed: true,
        blocker_codes: [],
        available_notional_capacity: "1000",
        max_plan_loss: "100",
        minimum_reward_risk_ratio: "1",
        open_risk_limit: "200",
        open_risk_committed: "0",
      },
    } });
  });
  const savedDraft = await routeDirectDraftAutosave(page, attemptedTradingWrites);
  let review: ReturnType<typeof syntheticPlanAiReview> | null = null;
  let reviewCount = 0;
  await page.route("**/api/v1/plans/workspace-autosave-fixture/ai-review/latest", async (route) => {
    await route.fulfill({ json: review });
  });
  await page.route("**/api/v1/plans/workspace-autosave-fixture/ai-review", async (route) => {
    const saved = savedDraft();
    expect(saved).not.toBeNull();
    reviewCount += 1;
    review = syntheticPlanAiReview(
      "APPROVED", saved!.draft_version, saved!.content_digest, "guard-review", null, saved!.plan_id,
    );
    await route.fulfill({ status: 202, json: review });
  });

  await page.goto("/plans/new?mode=direct&creator_kind=AI");
  await page.getByRole("button", { name: "＋ 添加入场条件或管理规则" }).click();
  await page.getByRole("button", { name: /行情失效/ }).click();
  await page.getByRole("spinbutton", { name: "失效价（USDT）" }).fill("64900");
  await openDirectAiReview(page);
  const requestReview = page.getByRole("button", { name: "提交 AI 审核", exact: true });
  await expect(requestReview).toBeEnabled({ timeout: 20_000 });
  await requestReview.click();
  const approved = page.getByTestId("plan-ai-review-approved");
  const submit = page.getByRole("button", { name: "提交并启动", exact: true });
  await expect(approved).toBeVisible();
  await expect(submit).toBeEnabled({ timeout: 15_000 });
  const approvedVersion = savedDraft()!.draft_version;
  const approvedDigest = savedDraft()!.content_digest;

  quote = "64890";
  await expectDirectSubmitProblem(page, "当前标记价 64,890.0 USDT 已达到或跌破入场失效价 64,900.0 USDT");
  await expect(approved).toBeVisible();
  expect(savedDraft()!.draft_version).toBe(approvedVersion);
  expect(savedDraft()!.content_digest).toBe(approvedDigest);

  quote = "65001";
  await expect(submit).toBeEnabled({ timeout: 15_000 });
  // Reload reads the changed runtime snapshot while retaining the same saved
  // draft and approval.  Neither preparation nor an old approval grants runtime authority.
  statusOverrides.executor_status = "READY";
  statusOverrides.app_executor_product_build_consistent = false;
  await page.reload();
  await openDirectMilestone(page, "5 AI审核");
  await expect(approved).toBeVisible();
  await expectDirectSubmitProblem(page, "应用与执行器版本不一致，不能提交并启动。");
  await expect(page.getByRole("button", { name: "保存草稿", exact: true })).toBeEnabled();

  statusOverrides.executor_status = "STARTING";
  statusOverrides.app_executor_product_build_consistent = true;
  await page.reload();
  await openDirectMilestone(page, "5 AI审核");
  await expect(approved).toBeVisible();
  await expectDirectSubmitProblem(page, "执行器未就绪，不能提交并启动。");
  expect(reviewCount).toBe(1);
  expect(attemptedTradingWrites).toEqual([]);
});

test("direct review keeps the Demo launch action visible when the executor is unavailable", async ({ page }) => {
  await routeCurrentDemoMarketStream(page);
  await routeReadyDemoExecutor(page, {
    executorStatus: "BUILD_MISMATCH",
    productBuildConsistent: false,
  });

  await page.goto("/plans/new?mode=direct");
  await openDirectReview(page);

  await expect(page.getByRole("button", {
    name: "提交并启动",
    exact: true,
  })).toBeVisible();
  await expect(page.getByRole("button", {
    name: "提交并启动",
    exact: true,
  })).toBeDisabled();
  await expectDirectSubmitProblem(page, "应用与执行器版本不一致，不能提交并启动。");
  await expect(page.getByRole("button", {
    name: "保存草稿",
    exact: true,
  })).toBeVisible();
});

test("direct plan creation does not consume or repeat review performance", async ({ page }) => {
  await routeCurrentDemoMarketStream(page);
  await routeReadyDemoExecutor(page);
  let reviewRequestCount = 0;
  await page.route("**/api/v1/reviews**", async (route) => {
    reviewRequestCount += 1;
    await route.fulfill({
      contentType: "application/json",
      body: "[]",
    });
  });

  await page.goto("/plans/new?mode=direct");
  await expect(page.getByRole("heading", { name: "计划基础信息", exact: true })).toBeVisible();
  await expect(page.getByLabel("计划名称")).toBeVisible();
  await expect(page.getByRole("combobox", { name: "创建方式" })).toBeVisible();
  await expect(page.getByLabel("计划有效分钟")).toBeVisible();
  await expect(page.getByRole("combobox", { name: "创建方式" })).toContainText("人工创建");
  await openDirectReview(page);
  await expect(page.getByText("近期同工具实际结果不利", { exact: false })).toHaveCount(0);
  await expect(page.getByText("当前样本未证明正期望", { exact: false })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "费用后收益门槛" })).toHaveCount(0);
  await expect(page.getByText("图表只编辑计划草稿", { exact: false })).toHaveCount(0);
  await expect(page.getByText("快速启动会连续保存草稿", { exact: false })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "提交并启动" })).toBeDisabled();
  expect(reviewRequestCount).toBe(0);
});

test("direct entry schemes clear incompatible fields and preserve compatible order components", async ({ page }) => {
  await routeCurrentDemoMarketStream(page);
  await routeReadyDemoExecutor(page);
  await page.goto("/plans/new?mode=direct");

  const protectionTab = page.getByRole("navigation", { name: "计划创建步骤" }).getByRole("button", { name: /保护$/ });
  const marketButton = page.getByRole("button", { name: "市价", exact: true });
  const limitButton = page.getByRole("button", { name: "限价", exact: true });
  const makerOnly = page.getByRole("switch", { name: "Maker only" });

  await page.getByRole("radio", { name: /时间分批/ }).click();
  await expect(marketButton).toHaveAttribute("aria-pressed", "true");
  await expect(makerOnly).toHaveCount(0);
  await limitButton.click();
  const timeInForce = page.getByRole("combobox", { name: "有效方式" });
  await expect(timeInForce).toContainText("IOC");
  await timeInForce.click();
  await expect(page.getByRole("option", { name: /GTC/ })).toHaveCount(0);
  await expect(page.getByRole("option", { name: /GTD/ })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await marketButton.click();

  await page.getByRole("radio", { name: /一次性入场/ }).click();
  await expect(marketButton).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByText("市价单不能设置限价。")).toHaveCount(0);
  await expect(protectionTab).toBeEnabled();

  await page.getByRole("radio", { name: /价格阶梯入场/ }).click();
  await expect(marketButton).toBeDisabled();
  await expect(page.getByRole("button", { name: "分档限价", exact: true }))
    .toHaveAttribute("aria-pressed", "true");
  await expect(makerOnly).toBeEnabled();

  await page.getByRole("radio", { name: /事件触发入场/ }).click();
  await expect(page.getByRole("heading", { name: "入场前置条件" })).toBeVisible();
  await expect(protectionTab).toBeEnabled();
  await page.getByRole("button", { name: "移除短时异动" }).click();
  await expect(protectionTab).toBeEnabled();
  await expectDirectSubmitProblem(page, "事件触发入场必须至少配置一个价格、K 线收盘或短时变动事件。");

  await page.getByRole("radio", { name: /一次性入场/ }).click();
  await limitButton.click();
  const limitPrice = await page.getByRole("spinbutton", {
    name: "限价（USDT）",
  }).inputValue();
  await page.getByRole("button", {
    name: "＋ 添加入场条件或管理规则",
  }).click();
  await page.getByRole("button", {
    name: /跟随同侧盘口/,
  }).click();
  await expect(page.getByRole("button", { name: "移除移动挂单" }))
    .toBeVisible();

  await page.getByRole("radio", { name: /事件触发入场/ }).click();
  await expect(limitButton).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("spinbutton", { name: "限价（USDT）" }))
    .toHaveValue(limitPrice);
  await expect(page.getByRole("button", { name: "移除移动挂单" }))
    .toBeVisible();
  await expect(page.getByRole("spinbutton", { name: "触发偏离（bps）" }))
    .toHaveValue("5");
});

test("time-sliced amount growth uses time order instead of price order", async ({ page }) => {
  await routeCurrentDemoMarketStream(page);
  await routeReadyDemoExecutor(page);
  await page.goto("/plans/new?mode=direct");

  await page.getByRole("radio", { name: /时间分批/ }).click();
  await expect(page.getByRole("radio", { name: /时间分批/ })).toBeChecked();
  await expect(page.getByRole("spinbutton", { name: "每笔最早间隔（秒）" }))
    .toBeVisible();
  await page.getByRole("combobox", { name: "下单额模式" }).click();
  await page.getByRole("option", { name: "线性增长" }).click();

  await expect(page.getByRole("spinbutton", { name: "每笔金额增量（USDT）" }))
    .toBeVisible();
  await expect(page.getByRole("combobox", { name: "金额增长方向" }))
    .toHaveText("从首笔到末笔");
  await page.getByRole("combobox", { name: "金额增长方向" }).click();
  await expect(page.getByRole("option", { name: "从末笔到首笔" })).toBeVisible();
  await expect(page.getByRole("option", { name: "从低价到高价" })).toHaveCount(0);
});

test("direct milestones remain switchable while submission requires protection and a price exit", async ({ page }) => {
  await routeCurrentDemoMarketStream(page);
  await routeReadyDemoExecutor(page);
  await page.goto("/plans/new?mode=direct");
  await openDirectReview(page);
  await openDirectMilestone(page, "3 退出");

  const navigation = page.getByRole("navigation", { name: "计划创建步骤" });
  const reviewMilestone = navigation.getByRole("button", { name: /核对$/ });
  await page.getByRole("button", { name: "移除分级止盈" }).click();
  await expect(reviewMilestone).toBeEnabled();
  await expect(navigation.getByRole("button", { name: /保护$/ })).toBeEnabled();
  await expectDirectSubmitProblem(page, "价格退出的计划加权收益 / 风险必须达到");

  await page.getByRole("button", { name: "＋ 添加退出方式" }).click();
  await page.getByRole("button", { name: /比例锁盈/ }).click();
  await expectDirectSubmitProblem(page, "价格退出的计划加权收益 / 风险必须达到");
  await page.getByRole("button", { name: "＋ 添加退出方式" }).click();
  await page.getByRole("button", { name: "固定 / 分级止盈 · 1–4 个价格目标", exact: true }).click();
  await openDirectMilestone(page, "4 核对");
  const summary = page.getByRole("region", { name: "计划概要", exact: true });
  await expect(summary).toContainText("达到 1R 后锁定峰值盈利 50%");
  await expect(summary).toContainText("TP1 1R / 50% · TP2 2R / 50%");
  await expectDirectSubmitProblem(page, "完成当前草稿的 AI 审核并获得批准。");
});

test("direct exit uses attributed fees and current spread without inventing a live fee quote", async ({ page }) => {
  await routeCurrentDemoMarketStream(page);
  await routeReadyDemoExecutor(page);
  await page.goto("/plans/new?mode=direct");
  await openDirectMilestone(page, "3 退出");

  const summary = page.getByTestId("after-cost-estimate");
  await expect(summary).toContainText("费用后风险收益");
  await expect(summary).toContainText("预计手续费");
  await expect(summary).toContainText("0.40 USDT");
  await expect(summary).toContainText("费用后目标净收益");
  await expect(summary).toContainText("+9.58 USDT");
  await expect(summary).toContainText("费用后净盈亏比");
  await expect(summary).toContainText("1.76 : 1");
  await expect(summary).toContainText("入场 Taker 4 bps，退出 Taker 4 bps");
  await expect(summary).toContainText("近期实付参考");
  await expect(summary).toContainText("样本截至");

  await openDirectMilestone(page, "1 入场");
  await page.getByRole("switch", { name: "Maker only" }).click();
  await openDirectMilestone(page, "3 退出");
  await expect(summary).toContainText("入场 Maker 2 bps，退出 Taker 4 bps");
  await expect(summary).toContainText("费用后净盈亏比");
  await expect(summary).toContainText("1.82 : 1");
});

test("direct review exposes exact entry, invalidation, protection, and exit intent", async ({ page }) => {
  await routeCurrentDemoMarketStream(page);
  await routeReadyDemoExecutor(page);
  await page.goto("/plans/new?mode=direct");

  await page.getByRole("button", { name: "做空", exact: true }).click();
  await page.getByRole("radio", { name: /事件触发入场/ }).click();
  await page.getByRole("button", { name: "市价", exact: true }).click();
  await page.getByRole("spinbutton", { name: "变动阈值（bps）" }).fill("10");
  await page.getByRole("button", { name: "＋ 添加入场条件或管理规则" }).click();
  await page.getByRole("button", { name: /到价触发/ }).click();
  await page.getByRole("spinbutton", { name: "标记价格（USDT）" }).fill("63600");
  await page.getByRole("button", { name: "＋ 添加入场条件或管理规则" }).click();
  await page.getByRole("button", { name: /行情失效/ }).click();
  await page.getByRole("spinbutton", { name: "失效价（USDT）" }).fill("63850");
  await page.getByRole("checkbox", { name: /机会错过价/ }).check();
  const opportunityMissedPrice = page.getByRole("spinbutton", {
    name: "机会错过价（USDT）",
  });
  await expect(opportunityMissedPrice).toHaveValue(/^\d+(?:\.\d{1,8})?$/);
  await opportunityMissedPrice.fill("63300");

  await openDirectMilestone(page, "2 保护");
  await page.getByRole("spinbutton", { name: "初始止损距离（bps）" }).fill("30");
  await openDirectMilestone(page, "3 退出");
  await page.getByRole("button", { name: "＋ 添加退出方式" }).click();
  await page.getByRole("button", { name: /比例锁盈/ }).click();
  await page.getByRole("spinbutton", { name: "开始锁定（R）" }).fill("0.75");
  await page.getByRole("spinbutton", { name: "锁定比例（%）" }).fill("75");
  await page.getByRole("spinbutton", { name: "最小收紧步长（R）" }).fill("0.1");
  await page.getByRole("button", { name: "＋ 添加退出方式" }).click();
  await page.getByRole("button", { name: /时间退出/ }).click();
  await page.getByRole("spinbutton", {
    name: "首笔成交后整组退出（秒）",
  }).fill("900");

  await openDirectReview(page);
  const summary = page.getByRole("region", { name: "计划概要", exact: true });
  await expect(summary).toContainText("全部满足");
  await expect(summary).toContainText("标记价 ≥ 63,600.00 USDT");
  await expect(summary).toContainText("30 秒下跌 ≥ 10 bps");
  await expect(summary).toContainText("标记价 ≥ 63,850.00 USDT 时取消");
  await expect(summary).toContainText("标记价 ≤ 63,300.00 USDT 时视为错过");
  await expect(summary).toContainText("30 秒反向上涨 ≥ 50 bps 时取消");
  await expect(summary).toContainText(
    "标记价止损 · 30 bps",
  );
  await expect(summary).toContainText("TP1 2R / 100%");
  await expect(summary).toContainText("首笔成交后 900 秒发起整组退出");
  await expect(summary).toContainText(
    "达到 0.75R 后锁定峰值盈利 75%（最小收紧 0.1R，间隔 5 秒，最多 8 次）",
  );
});

test("an empty overview opens the recent loss evidence instead of an empty position panel", async ({ page }) => {
  await routeReadyDemoExecutor(page);
  await page.route(/\/api\/v1\/overview(?:\?.*)?$/, async (route) => {
    await route.fulfill({
      contentType: "application/json",
      json: {
        environment_kind: "DEMO",
        environment_id: "binance-demo-primary",
        account_id: "binance-usdm-demo-owner-primary",
        profile: "BINANCE_DEMO",
        authority_class: "DEMO_VALIDATION",
        runtime_real_write_gate: "CLOSED",
        server_fact_cutoff: new Date().toISOString(),
        view_retrieved_at: new Date().toISOString(),
        open_activation_count: 0,
        database_name: "halpha_demo",
        account_snapshot_status: "CURRENT",
        account_snapshot_ref: "empty-account-snapshot",
        account_snapshot_cutoff: new Date().toISOString(),
        account_snapshot_age_seconds: 0,
        account_ordinary_open_order_count: 0,
        account_algo_open_order_count: 0,
        account_positions: [],
        account_orders: [],
      },
    });
  });
  const adverseReviews = Array.from({ length: 3 }, (_item, index) => {
    const closedAt = `2026-07-27T00:0${index}:00Z`;
    return {
      review_id: `overview-review-${index}`,
      activation_id: `overview-loss-${index}`,
      primary_result: "COMPLETED",
      fact_cutoff: closedAt,
      resolved_trade_result: {
        closed: true,
        calculation_complete: true,
        commission_complete: true,
        strategy_attribution_complete: true,
        result_scope: "HALPHA_ATTRIBUTED_ACTIONS",
        net_pnl: "-2",
        commission: "0.5",
        entry_notional: "100",
      },
      trade_context: {
        instrument_ref: "BTCUSDT-PERP",
        direction: "LONG",
        decision_basis_ref: "DIRECT_EXECUTION@1",
        strategy_id: null,
        trade_amount: "120",
        plan_name: `最近亏损计划 ${index + 1}`,
      },
    };
  });
  await page.route(/\/api\/v1\/activations(?:\?.*)?$/, async (route) => {
    await route.fulfill({ contentType: "application/json", body: "[]" });
  });
  await page.route(/\/api\/v1\/activations\/overview-loss-\d+$/, async (route) => {
    await route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ detail: { code: "ACTIVATION_DETAIL_NOT_REQUIRED" } }),
    });
  });
  await page.route("**/api/v1/reviews**", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(adverseReviews),
    });
  });

  await page.goto("/overview");

  await expect(page.getByRole("tab", { name: "最近交易结果" }))
    .toHaveAttribute("aria-selected", "true");
  const recentResults = page.getByRole("region", { name: "最近交易结果" });
  await expect(recentResults).toContainText(
    "连续亏损 3 笔，已触发连续亏损提醒；新的完整闭合交易净结果大于或等于零时自动解除。",
  );
  await expect(recentResults).toContainText("合计净结果");
  await expect(recentResults).toContainText("-6.00 USDT");
  await expect(recentResults).toContainText("平均净结果");
  await expect(recentResults).toContainText("-2.00 USDT");
  await expect(recentResults).toContainText("最近亏损计划 3");
  await expect(recentResults).toContainText("BTCUSDT-PERP · 做多 · -2.00 USDT");
  await expect(recentResults).toContainText(
    "直接执行订单计划 · 入场成交额 100.00 USDT · 计划上限 120.00 USDT",
  );

  await page.getByRole("tab", { name: "当前仓位（0）" }).click();
  await expect(page.getByText("当前无持仓。"))
    .toBeVisible();
});

test("overview previews plan-bound external position operations without creating a venue action", async ({ page }, testInfo) => {
  await routeReadyDemoExecutor(page, {
    statusOverrides: {
      environment_kind: "LIVE",
      environment_id: "binance-live-copy-primary",
      account_id: "binance-usdm-copy-lead-primary",
      venue_account_type: "USDM_COPY_LEAD",
      profile: "BINANCE_LIVE_READ_ONLY",
      authority_class: "NO_TRADING_AUTHORITY",
      database_name: "halpha_live_copy",
      port: 8766,
    },
  });
  const browserBaseUrl = new URL(
    process.env.HALPHA_BROWSER_BASE_URL ?? "http://127.0.0.1:8765",
  );
  const browserPort = browserBaseUrl.port || (browserBaseUrl.protocol === "https:" ? "443" : "80");
  await page.context().addCookies([{
    name: `halpha_csrf_${browserPort}`,
    value: "e2e-position-operation-token",
    url: `${browserBaseUrl.origin}/`,
  }]);
  const cutoff = new Date().toISOString();
  const snapshotRef = "synthetic-account-snapshot-1";
  const previewedOperations: string[] = [];
  const accountOrders = Array.from({ length: 4 }, (_item, index) => ({
    kind: index === 3 ? "ALGO" : "ORDINARY",
    instrument_ref: "TESTUSDT-PERP",
    symbol: "TESTUSDT",
    order_id: String(7000 + index),
    client_order_id: `external-order-${index}`,
    side: "BUY",
    position_side: "SHORT",
    order_type: index === 3 ? "STOP_MARKET" : "LIMIT",
    status: "NEW",
    time_in_force: "GTC",
    price: index === 3 ? "0" : String(98 + index),
    trigger_price: index === 3 ? "110" : "0",
    quantity: "0.5",
    executed_quantity: index === 3 ? null : "0",
    reduce_only: true,
    close_position: false,
    source_create_time_ms: Date.parse(cutoff) - index * 1000,
    source_update_time_ms: Date.parse(cutoff) - index * 1000,
    fact_cutoff: cutoff,
    snapshot_ref: snapshotRef,
  }));
  await page.route(/\/api\/v1\/overview(?:\?.*)?$/, async (route) => {
    await route.fulfill({
      contentType: "application/json",
      json: {
        environment_kind: "LIVE",
        environment_id: "binance-live-copy-primary",
        account_id: "binance-usdm-copy-lead-primary",
        profile: "BINANCE_LIVE_READ_ONLY",
        authority_class: "NO_TRADING_AUTHORITY",
        runtime_real_write_gate: "CLOSED",
        server_fact_cutoff: cutoff,
        view_retrieved_at: cutoff,
        open_activation_count: 0,
        database_name: "halpha_demo",
        account_snapshot_status: "CURRENT",
        account_snapshot_ref: snapshotRef,
        account_snapshot_cutoff: cutoff,
        account_snapshot_age_seconds: 1,
        account_ordinary_open_order_count: 3,
        account_algo_open_order_count: 1,
        account_summary: {
          can_trade: true,
          wallet_balance: "1000",
          unrealized_pnl: "-12.5",
          margin_balance: "987.5",
          available_balance: "800",
          initial_margin: "187.5",
          maintenance_margin: "25",
          position_initial_margin: "150",
          open_order_initial_margin: "37.5",
          cross_wallet_balance: "1000",
          cross_unrealized_pnl: "-12.5",
          source_update_time_ms: Date.parse(cutoff),
          fact_cutoff: cutoff,
          snapshot_ref: snapshotRef,
        },
        new_risk_discipline: {
          status: "ALLOWED",
          new_risk_allowed: true,
          blocker_codes: [],
          evaluated_at: cutoff,
          account_snapshot_ref: snapshotRef,
          account_snapshot_cutoff: cutoff,
          risk_equity: "987.5",
          max_plan_loss: "4.9375",
          open_risk_limit: "19.75",
          open_risk_committed: "0",
          open_risk_after_proposal: "4.9375",
          gross_exposure_limit: "2962.5",
          gross_exposure: "0",
          gross_exposure_after_proposal: "250",
          instrument_ref: "BTCUSDT-PERP",
          instrument_exposure_limit: "1481.25",
          instrument_exposure: "0",
          instrument_exposure_after_proposal: "250",
          correlation_cluster: "CRYPTO_MAJOR_BETA",
          correlated_exposure_limit: "1975",
          correlated_exposure: "0",
          correlated_exposure_after_proposal: "250",
          daily_loss_limit: "14.8125",
          daily_loss_measure: "0",
          weekly_loss_limit: "39.5",
          weekly_loss_measure: "0",
          rolling_peak_equity: "987.5",
          rolling_drawdown: "0",
          rolling_drawdown_fraction: "0",
          rolling_drawdown_limit_fraction: "0.06",
          rolling_drawdown_lookback_days: 30,
          open_new_risk_activation_count: 0,
          day_window_started_at: cutoff,
          week_window_started_at: cutoff,
        },
        account_positions: [{
          snapshot_ref: snapshotRef,
          instrument_ref: "TESTUSDT-PERP",
          symbol: "TESTUSDT",
          direction: "SHORT",
          position_side: "SHORT",
          quantity: "-2.5",
          absolute_quantity: "2.5",
          entry_price: "100",
          break_even_price: "100.1",
          mark_price: "105",
          unrealized_pnl: "-12.5",
          liquidation_price: "200",
          leverage: 2,
          margin_mode: "CROSS",
          notional: "-262.5",
          isolated_margin: "0",
          fact_cutoff: cutoff,
          origin: "EXTERNAL_UNMANAGED",
          management_status: "OBSERVED_ONLY",
          takeover_allowed: false,
          takeover_blockers: [
            "READ_ONLY_CREDENTIAL",
            "OPEN_ORDERS_REQUIRE_RECONCILIATION",
          ],
        }],
        account_orders: accountOrders,
      },
    });
  });
  await page.route("**/api/v1/account-position-operations/preview", async (route) => {
    const payload = route.request().postDataJSON() as {
      operation: "REDUCE" | "CLOSE" | "ADD";
      requested_quantity: string | null;
      requested_notional: string | null;
    };
    previewedOperations.push(payload.operation);
    const reduction = payload.operation === "CLOSE"
      ? "2.5"
      : payload.operation === "REDUCE" ? payload.requested_quantity ?? "1.25" : "0";
    const target = payload.operation === "CLOSE"
      ? "0"
      : payload.operation === "REDUCE" ? String(2.5 - Number(reduction)) : null;
    const newExposure = payload.operation === "ADD";
    await route.fulfill({
      contentType: "application/json",
      json: {
        operation: payload.operation,
        snapshot_ref: snapshotRef,
        fact_cutoff: cutoff,
        instrument_ref: "TESTUSDT-PERP",
        position_side: "SHORT",
        direction: "SHORT",
        preparation_allowed: true,
        activation_allowed: false,
        venue_action_created: false,
        blockers: [
          "READ_ONLY_CREDENTIAL",
          "OPEN_ORDERS_REQUIRE_RECONCILIATION",
        ],
        plan_prefill: {
          kind: newExposure ? "NEW_EXPOSURE" : "POSITION_DISPOSITION",
          plan_name: newExposure ? "TESTUSDT-PERP 独立追加开仓" : `TESTUSDT-PERP ${payload.operation === "CLOSE" ? "平仓" : "减仓"}处置`,
          instrument_ref: "TESTUSDT-PERP",
          direction: "SHORT",
          trade_amount: newExposure ? payload.requested_notional ?? "100" : String(Number(reduction) * 105),
          valid_minutes: 60,
          baseline_quantity: "2.5",
          target_quantity_after: target,
          position_alignment: newExposure ? null : {
            schema_version: "HALPHA_POSITION_ALIGNMENT_V1",
            operation: payload.operation,
            snapshot_ref: snapshotRef,
            fact_cutoff: cutoff,
            account_ref: "binance-usdm-copy-lead-primary",
            venue_ref: "BINANCE_USDM",
            instrument_ref: "TESTUSDT-PERP",
            direction: "SHORT",
            position_side: "SHORT",
            baseline_quantity: "2.5",
            requested_reduction_quantity: reduction,
            target_quantity_after: target,
            baseline_entry_price: "100",
            baseline_mark_price: "105",
          },
        },
      },
    });
  });
  await page.route(/\/api\/v1\/activations(?:\?.*)?$/, async (route) => {
    await route.fulfill({ contentType: "application/json", body: "[]" });
  });
  await page.route("**/api/v1/reviews**", async (route) => {
    await route.fulfill({ contentType: "application/json", body: "[]" });
  });
  await page.route("**/api/v1/strategies", async (route) => {
    await route.fulfill({ contentType: "application/json", body: "[]" });
  });

  await page.goto("/overview");

  await expect(page.getByRole("tab", { name: "当前仓位（1）" })).toBeVisible();
  const accountPositions = page.getByRole("region", { name: "交易所账户当前仓位" });
  await expect(accountPositions).toContainText("TESTUSDT-PERP");
  await expect(accountPositions).toContainText("外部");
  await expect(accountPositions).toContainText("未实现盈亏");
  await expect(accountPositions).toContainText("-12.50 USDT");
  await expect(accountPositions).toContainText("SHORT");
  await expect(accountPositions).toContainText("2× · 全仓");
  const ordersTab = page.getByRole("tab", { name: "当前委托（4）" });
  await ordersTab.click();
  const accountOrderTable = page.getByRole("region", { name: "交易所账户当前委托" });
  await expect(accountOrderTable).toContainText("普通");
  await expect(accountOrderTable).toContainText("条件");
  await expect(accountOrderTable).toContainText("110");
  await page.getByRole("tab", { name: "当前仓位（1）" }).click();
  const operationButton = accountPositions.getByRole("button", { name: "策略调整" });
  await expect(operationButton).toBeEnabled();
  await operationButton.click();
  const dialog = page.getByRole("dialog", { name: "策略计划对齐 · TESTUSDT-PERP" });
  await expect(dialog).toContainText("做空 · SHORT");
  await expect(dialog).toContainText("既有入场仍为外部事实，不计入 Halpha ENTRY 或策略盈亏");
  await dialog.getByRole("button", { name: "核对计划对齐" }).click();
  await expect(dialog).toContainText("当前 API Key 只读，不能向交易所提交订单");
  await expect(dialog).toContainText("账户仍有未结普通或条件委托，必须先逐笔核对");
  await expect(dialog).toContainText("本次仅完成预检；尚未创建执行动作，也未向 Binance 发出请求");
  await expect(dialog.getByRole("button", { name: "创建处置计划草稿" })).toBeDisabled();
  expect(previewedOperations).toEqual(["REDUCE"]);
  await dialog.getByRole("button", { name: "平仓", exact: true }).click();
  await expect(dialog).toContainText("目标数量为 0");
  await dialog.getByRole("button", { name: "核对计划对齐" }).click();
  await expect.poll(() => previewedOperations.join(",")).toBe("REDUCE,CLOSE");
  await assertAccessible(page, testInfo, "external-position-read-only-takeover");
  await testInfo.attach(`external-position-${testInfo.project.name}.png`, {
    body: await page.screenshot({ fullPage: true }),
    contentType: "image/png",
  });
  await routeCurrentDemoMarketContext(page);
  await routeCurrentDemoMarketWindow(page);
  await routeValidOrderSchedulePreview(page);
  await dialog.getByRole("button", { name: "追加开仓", exact: true }).click();
  await dialog.getByRole("button", { name: "核对计划对齐" }).click();
  await expect.poll(() => previewedOperations.join(",")).toBe("REDUCE,CLOSE,ADD");
  await expect(dialog).toContainText("已形成独立新增风险计划预填，但当前不能激活或提交交易所");
  await dialog.getByRole("button", { name: "查看独立开仓计划" }).click();
  await expect(page).toHaveURL(/positionOperation=ADD/);
  await expect(page.getByRole("heading", { name: "独立追加开仓" })).toBeVisible();
  await expect(page.getByText("TESTUSDT-PERP", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: "做空", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("textbox", { name: "资金上限（USDT）" })).toHaveValue("65.63");
  await expect(page.getByText("这是独立的新风险计划")).toBeVisible();
});

test("overview exposes a venue-confirmed working entry order before the plan has a position", async ({ page }) => {
  const activationId = "overview-working-entry";
  let detailRequestCount = 0;
  await page.route(/\/api\/v1\/activations(?:\?.*)?$/, async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify([{
        activation_id: activationId,
        plan_version_ref: "overview-plan-version",
        plan_name: "Overview working Maker",
        instrument_ref: "BTCUSDT-PERP",
        direction: "SHORT",
        lifecycle: "RUNNING",
        run_state: "ACTIVE",
        pause_reason: null,
        protection_state: "NONE",
        state_version: 1,
        has_entry_fill: false,
        rule_state: {
          deadlines: {
            entry_valid_until: new Date(Date.now() + 30 * 60_000).toISOString(),
          },
        },
        created_at: "2026-07-28T07:45:00Z",
        updated_at: "2026-07-28T07:45:00Z",
        result_ref: null,
        closure_reason_code: null,
        primary_result: null,
        trade_result: null,
      }]),
    });
  });
  await page.route(new RegExp(`/api/v1/activations/${activationId}(?:\\?.*)?$`), async (route) => {
    detailRequestCount += 1;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        activation: {
          activation_id: activationId,
          has_entry_fill: false,
        },
        trade_result: {
          position_quantity: "0",
        },
        execution_actions: [{
          execution_action_id: "working-entry-action",
          action_kind: "ENTRY",
          state: "OPEN",
          action_terms: {
            price: "63570",
            quantity: "0.0078",
            execution_context: {
              venue_policy: {
                post_only: true,
              },
            },
          },
        }],
        venue_facts: [{
          kind: "ORDER_STATE",
          action_ref: "working-entry-action",
          payload: {
            status: "WORKING",
          },
        }],
      }),
    });
  });
  await page.route("**/api/v1/reviews**", async (route) => {
    await route.fulfill({ contentType: "application/json", body: "[]" });
  });

  await page.goto("/overview");

  await expect.poll(() => detailRequestCount).toBeGreaterThan(0);
  const runningPlans = page.getByRole("heading", { name: "运行中的计划" }).locator("..");
  await expect(runningPlans).toContainText("Overview working Maker");
  await expect(runningPlans).toContainText("交易所工作中");
  await expect(runningPlans).toContainText("Maker");
  await expect(runningPlans).toContainText("63,570.00 USDT");
  await expect(runningPlans).toContainText("0.0078 BTC");
  await expect(runningPlans).toContainText("495.846 USDT");
});

test("running plan distinguishes the submitted order deadline from the later plan validity", async ({ page }) => {
  const activationId = "runtime-entry-deadline";
  const submittedAt = new Date(Date.now()).toISOString();
  const planValidUntil = new Date(Date.now() + 60 * 60_000).toISOString();
  await routeCurrentDemoMarketStream(page);
  await page.route(
    new RegExp(`/api/v1/activations/${activationId}/timeline(?:\\?.*)?$`),
    async (route) => {
      await route.fulfill({ contentType: "application/json", body: "[]" });
    },
  );
  await page.route(
    new RegExp(`/api/v1/activations/${activationId}(?:\\?.*)?$`),
    async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          activation: {
            activation_id: activationId,
            instrument_ref: "BTCUSDT-PERP",
            direction: "SHORT",
            lifecycle: "RUNNING",
            run_state: "ACTIVE",
            protection_state: "NONE",
            has_entry_fill: false,
            state_version: 1,
            created_at: submittedAt,
            updated_at: submittedAt,
            rule_state: {
              deadlines: {
                entry_valid_until: planValidUntil,
              },
            },
          },
          plan: {
            plan_name: "委托期限展示回归",
            created_at: submittedAt,
          },
          capital: {
            max_notional: "500",
          },
          decision_basis: {
            kind: "DIRECT_EXECUTION",
            decision_basis_ref: "DIRECT_EXECUTION@1",
          },
          order_schedule: {
            valid: true,
            schedule_spec: {
              price_distribution: {
                kind: "SINGLE",
                limit_price: "63570",
              },
              amount_distribution: {
                mode: "FIXED",
                direction: "LOW_TO_HIGH",
                base_notional: "500",
                linear_step: "0",
                exponential_ratio: "2",
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
              submission_order: "HIGH_TO_LOW",
              entry_conditions: {
                operator: "ALL",
                items: [{ kind: "DECISION_BASIS_READY" }],
              },
              protection_policy: {
                initial_stop: {
                  distance_bps: "40",
                  trigger_source: "MARK_PRICE",
                  coverage: "EACH_CONFIRMED_FILL",
                },
                take_profit_ladder: null,
                time_exit_seconds: 3600,
              },
              dynamic_rules: [{
                kind: "EXPIRE_REMAINING",
                after_seconds: 2700,
              }],
            },
            normalized_legs: [{
              leg_index: 0,
              leg_count: 1,
              raw_price: "63570",
              price: "63570",
              sizing_price: "63570",
              requested_notional: "500",
              quantity: "0.0078",
              effective_notional: "495.846",
            }],
            instrument_rules: {
              price_tick_size: "0.1",
            },
          },
          trade_result: {
            calculation_complete: false,
            position_quantity: "0",
            fill_count: 0,
            fills: [],
          },
          position_attribution: {
            activation_signed_position: "0",
            venue_account_signed_position: "0",
            attributed_account_signed_position: "0",
            reconciliation_status: "MATCH",
          },
          execution_actions: [{
            execution_action_id: "entry-deadline-action",
            action_kind: "ENTRY",
            state: "OPEN",
            client_order_id: "entry-deadline-client",
            created_at: submittedAt,
            call_started_at: submittedAt,
            updated_at: submittedAt,
            action_terms: {
              price: "63570",
              quantity: "0.0078",
              direction: "SHORT",
              order_type: "LIMIT",
              action_profile: "ENTRY_LIMIT",
              execution_context: {
                venue_policy: {
                  post_only: true,
                },
                dynamic_rules: [{
                  kind: "EXPIRE_REMAINING",
                  after_seconds: 2700,
                }],
              },
            },
          }],
          venue_facts: [{
            kind: "ORDER_STATE",
            action_ref: "entry-deadline-action",
            cutoff: submittedAt,
            payload: {
              status: "WORKING",
            },
          }],
          receipts: [],
          stopped_categories: [],
          stop_evidence: [],
        }),
      });
    },
  );

  await page.goto(`/activations/${activationId}`);

  await expect(page.getByText("未成交委托最迟撤销", { exact: true })).toBeVisible();
  await expect(page.getByText("剩余 45 分钟", { exact: true })).toBeVisible();
  await expect(page.getByText("计划有效期", { exact: true })).toBeVisible();
  await expect(page.getByText("剩余 1 小时", { exact: true })).toBeVisible();
  await page.getByText("剩余 45 分钟", { exact: true }).hover();
  await expect(page.getByRole("tooltip")).toContainText("提交后 2700 秒");
});

test("review summary stays compact while trade records retain classification and exit evidence", async ({ page }) => {
  const reviews = [
    {
      direction: "LONG",
      grossPnl: "1",
      netPnl: "-1",
      commission: "2",
      liquidity: "MAKER",
      exitKind: "EXIT",
      classification: "TRADE_DECISION_ISSUE",
    },
    {
      direction: "LONG",
      grossPnl: "-1",
      netPnl: "-2",
      commission: "1",
      liquidity: "MAKER",
      exitKind: "PROTECTION",
      classification: "TOOLING_ISSUE",
    },
    {
      direction: "SHORT",
      grossPnl: "-1",
      netPnl: "-2",
      commission: "1",
      liquidity: "TAKER",
      exitKind: "EXIT",
      classification: "USABLE_SAMPLE",
    },
  ].map((item, index) => {
    const closedAt = `2026-07-27T00:0${index}:00Z`;
    return {
      review_id: `attribution-review-${index}`,
      activation_id: `attribution-activation-${index}`,
      status: "COMPLETE",
      primary_result: "COMPLETED",
      fact_cutoff: closedAt,
      evaluations: {
        owner_conclusion: {
          result: item.classification,
        },
      },
      trade_context: {
        instrument_ref: "BTCUSDT-PERP",
        direction: item.direction,
        decision_basis_ref: "DIRECT_EXECUTION@1",
        plan_name: `Attribution ${index}`,
      },
      resolved_trade_result: {
        closed: true,
        calculation_complete: true,
        commission_complete: true,
        strategy_attribution_complete: true,
        result_scope: "HALPHA_ATTRIBUTED_ACTIONS",
        gross_pnl: item.grossPnl,
        net_pnl: item.netPnl,
        commission: item.commission,
        first_fill_time: closedAt,
        last_fill_time: closedAt,
        fills: [
          {
            action_kind: "ENTRY",
            liquidity_side: item.liquidity,
            fill_time: closedAt,
          },
          {
            action_kind: item.exitKind,
            liquidity_side: "TAKER",
            fill_time: closedAt,
          },
        ],
      },
    };
  });
  await page.route("**/api/v1/reviews**", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(reviews),
    });
  });

  await page.goto("/reviews");

  await expect(page.getByText("当前闭合样本归因", { exact: true })).toHaveCount(0);
  await expect(page.getByText("账户累计净盈亏", { exact: true }).locator(".."))
    .toContainText("-5.00 USDT");
  await expect(page.getByText("账户累计手续费", { exact: true }).locator(".."))
    .toContainText("4.00 USDT");
  await expect(page.getByText("策略当前连亏", { exact: true }).locator(".."))
    .toContainText("2 笔");
  await expect(page.getByRole("tab", { name: "阶段性复盘" })).toBeVisible();
  const records = page.getByRole("table", { name: "交易与复盘记录" });
  await expect(records).toContainText("交易决策需改进");
  await expect(records).toContainText("工具问题影响");
  await expect(records).toContainText("可用交易样本");
  await expect(records).toContainText("计划退出");
  await expect(records).toContainText("保护止损");
  await page.getByRole("combobox", { name: "复盘分类" }).click();
  await expect(page.getByRole("option", { name: "待评价" })).toBeVisible();
  await page.keyboard.press("Escape");
});

test("one Demo submit-and-start command creates the run snapshot and activation without an intermediate page", async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name === "chromium-narrow",
    "Submit-and-start orchestration is viewport-independent and is covered once on desktop.",
  );
  const calls: string[] = [];
  const idempotencyKeys: string[] = [];
  const quickDraft = syntheticDirectDraft(
    "synthetic-quick-plan",
    1,
    "直接提交并启动 E2E",
  );
  const quickReview = syntheticPlanAiReview(
    "APPROVED",
    quickDraft.draft_version,
    quickDraft.content_digest,
    "synthetic-quick-review",
    null,
    quickDraft.plan_id,
  );
  let quickReviewRequested = false;
  await routeCurrentDemoMarketStream(page);
  await routeReadyDemoExecutor(page);
  await page.route(/\/api\/v1\/plans(?:\?.*)?$/, async (route) => {
    const request = route.request();
    if (request.method() !== "POST") {
      await route.continue();
      return;
    }
    calls.push("SAVE_DRAFT");
    idempotencyKeys.push(await request.headerValue("idempotency-key") ?? "");
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify(quickDraft),
    });
  });
  await page.route("**/api/v1/plans/synthetic-quick-plan", async (route) => {
    if (route.request().method() === "GET" || route.request().method() === "PUT") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(quickDraft),
      });
      return;
    }
    await route.continue();
  });
  await page.route("**/api/v1/plans/synthetic-quick-plan/ai-review/latest", async (route) => {
    await route.fulfill(quickReviewRequested
      ? { contentType: "application/json", body: JSON.stringify(quickReview) }
      : { status: 404, contentType: "application/json", body: JSON.stringify({ detail: { code: "PLAN_AI_REVIEW_NOT_FOUND" } }) });
  });
  await page.route("**/api/v1/plans/synthetic-quick-plan/ai-review", async (route) => {
    calls.push("AI_REVIEW");
    quickReviewRequested = true;
    await route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify(quickReview) });
  });
  await page.route("**/api/v1/plans/synthetic-quick-plan/submit-and-start", async (route) => {
    calls.push("SUBMIT_AND_START");
    idempotencyKeys.push(await route.request().headerValue("idempotency-key") ?? "");
    await new Promise((resolve) => setTimeout(resolve, 150));
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        activation: { activation_id: "synthetic-quick-activation" },
        runtime_real_write_gate: "CLOSED",
        venue_write_created: false,
      }),
    });
  });

  await page.goto("/plans/new?mode=direct");
  await openDirectAiReview(page);
  await expect(page.getByRole("button", { name: "提交 AI 审核", exact: true })).toBeEnabled({ timeout: 20_000 });
  await page.getByRole("button", { name: "提交 AI 审核", exact: true }).click();
  await expect(page.getByTestId("plan-ai-review-approved")).toBeVisible({ timeout: 20_000 });
  const launch = page.getByRole("button", {
    name: "提交并启动",
    exact: true,
  });
  await expect(launch).toBeEnabled({ timeout: 20_000 });
  await launch.click();
  await expect(page.getByRole("button", { name: "正在复核并启动…", exact: true })).toBeVisible();

  await expect(page).toHaveURL(/\/activations\/synthetic-quick-activation$/);
  expect(calls).toEqual([
    "SAVE_DRAFT",
    "AI_REVIEW",
    "SUBMIT_AND_START",
  ]);
  expect(idempotencyKeys).toHaveLength(2);
  expect(idempotencyKeys.every(Boolean)).toBe(true);
});

test("direct submit-and-start leaves the same editable draft after a discipline rejection", async ({ page }, testInfo) => {
  const calls: string[] = [];
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  const rejectedDraft = syntheticDirectDraft(
    "discipline-quick-plan",
    1,
    "纪律拒绝提交并启动 E2E",
  );
  const approvedReview = syntheticPlanAiReview(
    "APPROVED",
    rejectedDraft.draft_version,
    rejectedDraft.content_digest,
    "discipline-quick-review",
    null,
    rejectedDraft.plan_id,
  );
  let approvedReviewRequested = false;
  let disciplineRejectedAtActivation = false;
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => {
    pageErrors.push(error.message);
  });
  await routeCurrentDemoMarketStream(page);
  await routeReadyDemoExecutor(page);
  await page.route(/\/api\/v1\/overview(?:\?.*)?$/, async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        new_risk_discipline: {
          status: disciplineRejectedAtActivation ? "BLOCKED" : "ALLOWED",
          new_risk_allowed: !disciplineRejectedAtActivation,
          blocker_codes: disciplineRejectedAtActivation
            ? ["NEW_RISK_DAILY_LOSS_STOP_REACHED"]
            : [],
          available_notional_capacity: disciplineRejectedAtActivation ? null : "1000",
          max_plan_loss: "100",
          open_risk_limit: "200",
          open_risk_committed: "0",
          day_window_started_at: "2026-08-12T16:00:00Z",
          week_window_started_at: "2026-08-10T16:00:00Z",
          rolling_drawdown_lookback_days: 30,
        },
      }),
    });
  });
  await page.route("**/api/v1/decision-evidence/preview", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      json: {
        capital_scaling_authority: false,
        comparable_trade_count: 0,
        decision_basis_ref: "DIRECT_EXECUTION@1",
        direction: "LONG",
        evidence_grade: "VALIDATION_INTENT",
        excluded_review_count: 0,
        exclusions: {},
        instrument_ref: "BTCUSDT-PERP",
        intent: "VALIDATION",
        limitations: [],
        matched_review_count: 0,
        metrics: {
          average_net_pnl: null,
          best_trade_net_pnl: null,
          commission: "0",
          current_streak_count: 0,
          current_streak_kind: null,
          flat: 0,
          gross_loss: "0",
          gross_profit: "0",
          largest_loss_share_percent: null,
          largest_win_share_percent: null,
          longest_losing_streak: 0,
          longest_winning_streak: 0,
          losses: 0,
          maximum_drawdown: "0",
          net_pnl: "0",
          net_pnl_without_best_trade: null,
          net_pnl_without_worst_trade: null,
          notional_return_percent: null,
          profit_factor: null,
          total_entry_notional: "0",
          trade_count: 0,
          wins: 0,
          worst_trade_net_pnl: null,
        },
        parameter_digest: "c".repeat(64),
        playbook_ref: "E2E_DIRECT_EXECUTION_V1",
        repeatability: {
          average_r_multiple: null,
          bootstrap_block_length: null,
          bootstrap_resamples: 0,
          capital_scaling_authority: false,
          confidence_level_percent: "95",
          early_segment_net_r: null,
          early_segment_trade_count: 0,
          limitations: [],
          live_promotion_authority: false,
          mean_r_lower_confidence_bound: null,
          minimum_profit_factor: "1",
          minimum_trade_count: 0,
          net_r_multiple: null,
          net_r_without_best_trade: null,
          policy_version: "E2E",
          reason_codes: [],
          recent_segment_net_r: null,
          recent_segment_trade_count: 0,
          risk_basis_trade_count: 0,
          status: "NOT_APPLICABLE_VALIDATION",
        },
        repeated_sample_floor: 0,
        sample_identity_digest: null,
        sample_review_refs: [],
        sample_traceability_complete: false,
        setup_family: "OTHER",
        source: "CURRENT_COMPLETED_REVIEWS",
        source_cutoff: null,
      },
    });
  });
  await page.route(/\/api\/v1\/plans(?:\?.*)?$/, async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    calls.push("SAVE_DRAFT");
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify(rejectedDraft),
    });
  });
  await page.route("**/api/v1/plans/discipline-quick-plan", async (route) => {
    if (route.request().method() === "GET" || route.request().method() === "PUT") {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(rejectedDraft) });
      return;
    }
    await route.continue();
  });
  await page.route("**/api/v1/plans/discipline-quick-plan/ai-review/latest", async (route) => {
    await route.fulfill(approvedReviewRequested
      ? { contentType: "application/json", body: JSON.stringify(approvedReview) }
      : { status: 404, contentType: "application/json", body: JSON.stringify({ detail: { code: "PLAN_AI_REVIEW_NOT_FOUND" } }) });
  });
  await page.route("**/api/v1/plans/discipline-quick-plan/ai-review", async (route) => {
    calls.push("AI_REVIEW");
    approvedReviewRequested = true;
    await route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify(approvedReview) });
  });
  await page.route("**/api/v1/plans/discipline-quick-plan/submit-and-start", async (route) => {
    calls.push("SUBMIT_AND_START");
    disciplineRejectedAtActivation = true;
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({
        detail: { code: "NEW_RISK_DAILY_LOSS_STOP_REACHED" },
      }),
    });
  });

  await page.goto("/plans/new?mode=direct");
  await openDirectAiReview(page);
  await page.getByRole("button", { name: "提交 AI 审核", exact: true }).click();
  await expect(page.getByTestId("plan-ai-review-approved")).toBeVisible({ timeout: 20_000 });
  await page.getByRole("button", { name: "提交并启动", exact: true }).click();

  const rejection = page.getByRole("alert").filter({
    hasText: "当前账户纪律拒绝提交并启动",
  });
  await expect(rejection).toContainText("草稿仍可修改");
  await expect(page.getByText("提交并启动 Demo 被新增风险纪律拒绝")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "保存草稿", exact: true })).toBeVisible();
  await expect(page.getByText("已固定", { exact: false })).toHaveCount(0);
  expect(calls).toEqual([
    "SAVE_DRAFT",
    "AI_REVIEW",
    "SUBMIT_AND_START",
  ]);
  expect(
    consoleErrors.filter((message) => (
      !message.includes("status of 409 (Conflict)")
      && !message.includes("status of 404 (Not Found)")
    )),
  ).toEqual([]);
  expect(pageErrors).toEqual([]);
  await testInfo.attach("discipline-rejection.png", {
    body: await page.screenshot({ fullPage: true }),
    contentType: "image/png",
  });
  await testInfo.attach("discipline-rejection-detail.png", {
    body: await rejection.screenshot(),
    contentType: "image/png",
  });
});

test("direct configuration keeps a snapshot refresh failure compact and names a known connection cause", async ({ page }, testInfo) => {
  const evidenceDirectory = process.env.HALPHA_BROWSER_EVIDENCE_DIR;
  await routeCurrentDemoMarketStream(page);
  await routeCurrentDemoMarketWindow(page);
  await routeCurrentDemoMarketContext(page);
  await routeReadyDemoExecutor(page);
  await routeValidOrderSchedulePreview(page);
  await page.route(/\/api\/v1\/overview(?:\?.*)?$/, async (route) => {
    await route.fulfill({
      contentType: "application/json",
      json: {
        account_snapshot_status: "STALE",
        account_snapshot_age_seconds: 91,
        account_observation_failure_at: "2026-08-15T07:00:00Z",
        account_observation_failure_code: "ACCOUNT_SNAPSHOT_QUERY_FAILED_OSERROR",
        account_observation_retry_after_seconds: 30,
        new_risk_discipline: {
          status: "UNKNOWN",
          new_risk_allowed: false,
          blocker_codes: ["ACCOUNT_EQUITY_SNAPSHOT_STALE"],
          available_notional_capacity: null,
          max_plan_loss: null,
          open_risk_limit: null,
          open_risk_committed: null,
          rolling_drawdown_lookback_days: 30,
        },
      },
    });
  });

  await page.goto("/plans/new?mode=direct");

  await expect(page.getByText("纪律上限不可用", { exact: true })).toBeVisible({
    timeout: 15_000,
  });
  await expect(page.getByText("当前交易纪律阻止新增风险", { exact: true })).toHaveCount(0);
  await expect(page.getByText("账户权益事实已过期", { exact: true })).toHaveCount(0);
  const submit = page.getByRole("button", { name: "提交并启动", exact: true });
  await expect(submit).toBeDisabled();
  await submit.locator("..").hover();
  await expect(page.getByRole("tooltip")).toContainText("交易所账户查询连接失败");
  await testInfo.attach("account-observation-failure-compact.png", {
    body: await page.screenshot({
      fullPage: true,
      ...(evidenceDirectory
        ? { path: `${evidenceDirectory}/account-observation-failure-compact.png` }
        : {}),
    }),
    contentType: "image/png",
  });
});

test("direct execution milestones compose entry and exit capabilities without hidden residue", async ({ page }) => {
  test.setTimeout(45_000);
  const tradingWrites: string[] = [];
  await addBrowserScopedCsrfCookie(page);
  await routeCurrentDemoMarketStream(page);
  await routeCurrentDemoMarketWindow(page);
  await routeCurrentDemoMarketContext(page);
  await routeReadyDemoExecutor(page);
  await routeValidOrderSchedulePreview(page);
  await routeDirectDraftAutosave(page, tradingWrites);

  await page.goto("/plans/new?mode=direct");
  await expect(page.getByText("65,001.00", { exact: true })).toHaveText("65,001.00", {
    timeout: 15_000,
  });
  const milestones = page.getByRole("navigation", { name: "计划创建步骤" });
  await expect(milestones.getByRole("button")).toHaveCount(5);
  await expect(page.getByRole("radio", {
    name: "一次性入场",
  })).toBeChecked();
  await expect(milestones.getByRole("button", { name: "1 入场" }))
    .toHaveAttribute("aria-current", "step");

  await page.getByRole("radio", {
    name: "事件触发入场",
  }).click();
  await expect(page.getByRole("heading", { name: "入场前置条件" })).toBeVisible();
  await expect(page.getByRole("button", { name: "移除短时异动" })).toBeVisible();
  await expect(page.getByRole("button", { name: "限价", exact: true }))
    .toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("switch", { name: "Maker only" })).toBeEnabled();

  await page.getByRole("radio", {
    name: "价格阶梯入场",
  }).click();
  await expect(page.getByRole("heading", { name: "入场前置条件" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "市价", exact: true })).toBeDisabled();
  await expect(page.getByRole("switch", { name: "Maker only" })).toBeEnabled();

  await page.getByRole("button", { name: "＋ 添加入场条件或管理规则" }).click();
  await expect(page.getByLabel("入场扩展目录")).toContainText("入场前置条件");
  await expect(page.getByLabel("入场扩展目录")).toContainText("撤单与失效");
  await page.getByRole("button", {
    name: "到价触发 · 标记价达到指定价格",
  }).click();
  await expect(page.getByRole("button", { name: "移除到价触发" })).toBeVisible();
  const markPriceInput = page.getByLabel("标记价格（USDT）");
  await expect(markPriceInput).toBeVisible();
  // Make the test independent from the websocket/reference-price render tick.
  // The product must still reject a blank trigger, but this composition test
  // is validating capability residue rather than the blank-field branch.
  await markPriceInput.fill("65001");

  await openDirectMilestone(page, "2 保护");
  await page.getByRole("button", { name: "＋ 添加成交后动态止损" }).click();
  const dynamicStopCatalog = page.getByLabel("动态止损目录");
  await expect(dynamicStopCatalog).toContainText("离散触发");
  await expect(dynamicStopCatalog).toContainText("连续收紧");
  await page.getByRole("button", {
    name: "峰值比例锁盈 · 达到 1R 后锁定峰值盈利的 50%",
  }).click();
  await expect(page.getByText("连续动态止损", { exact: true })).toBeVisible();

  await openDirectMilestone(page, "3 退出");
  await expect(milestones.getByRole("button", { name: "3 退出" }))
    .toHaveAttribute("aria-current", "step");
  await expect(milestones.getByRole("button", { name: "✓ 入场" })).toBeVisible();
  await expect(milestones.getByRole("button", { name: "✓ 保护" })).toBeVisible();
  await page.getByRole("button", { name: "＋ 添加退出方式" }).click();
  await expect(page.getByLabel("退出方式目录")).toContainText("价格目标");
  await expect(page.getByLabel("退出方式目录")).toContainText("时间约束");
  await expect(page.getByText("连续收益锁定", { exact: true })).toBeVisible();

  await openDirectMilestone(page, "2 保护");
  await page.getByRole("button", { name: "＋ 添加成交后动态止损" }).click();
  await page.getByRole("button", {
    name: "阶梯保盈 · 1R 保本，2R 后保住 1R",
  }).click();
  await openDirectMilestone(page, "3 退出");
  await expect(page.getByText("连续收益锁定", { exact: true })).toHaveCount(0);
  await expect(page.getByText("阶梯保盈", { exact: true })).toBeVisible();
  await page.setViewportSize({ width: 930, height: 925 });
  await expect.poll(async () => page.getByTestId("direct-order-config-scroll")
    .evaluate((element) => element.scrollWidth <= element.clientWidth))
    .toBe(true);

  await milestones.getByRole("button", { name: "4 核对" }).click();
  await expect(page.getByRole("heading", { name: "计划概要" })).toBeVisible();
  await expect(page.getByText("价格区间分批 · 5 档", { exact: true })).toBeVisible();
  await expect(page.getByText("限价 · GTC · 高→低", { exact: true })).toBeVisible();
  await expect(page.getByText(
    "标记价止损 · 100 bps",
    { exact: true },
  )).toBeVisible();
  await expect(page.getByText(/TP1 2R \/ 100% · 阶梯保盈：1R → 止损 0R、2R → 止损 1R/))
    .toBeVisible();
  await expect(page.getByRole("heading", { name: "服务端预览" })).toBeVisible();
  await expect(page.getByText(/预览可保存 ·/)).toBeVisible({ timeout: 20_000 });
  expect(tradingWrites).toEqual([]);
});

test("direct execution uses one live stream while chart timeframes switch", async ({ page }, testInfo) => {
  test.setTimeout(45_000);
  const marketWindowIntervals: string[] = [];
  const marketWindowPurposes: string[] = [];
  const attemptedTradingWrites: string[] = [];
  let websocketConnections = 0;
  let quoteFrames = 0;
  let barFrames = 0;
  let previewRequests = 0;
  const invalidPreviewRequests: unknown[] = [];
  let streamInterval: "15m" | "1h" | "1m" = "15m";
  let nextLiveReferencePrice = 65_001;
  let routedSocket: {
    send: (message: string | Buffer) => void;
  } | null = null;
  const sendCurrentFrames = (interval: "15m" | "1h" | "1m") => {
    if (routedSocket === null) return;
    const timestamp = new Date(Date.now() + 4_000).toISOString();
    const referencePrice = nextLiveReferencePrice;
    const intervalMilliseconds = interval === "1m"
      ? 60_000
      : interval === "15m"
        ? 15 * 60_000
        : 60 * 60_000;
    const openAt = Date.parse("2026-07-23T11:30:00.000Z");
    nextLiveReferencePrice += 1;
    routedSocket.send(JSON.stringify({
      type: "status",
      state: "LIVE",
      source: "BINANCE_DEMO_PUBLIC",
      observed_at: timestamp,
      reason: null,
    }));
    routedSocket.send(JSON.stringify({
      type: "quote",
      instrument_ref: "BTCUSDT-PERP",
      source: "BINANCE_DEMO_PUBLIC",
      source_cutoff: timestamp,
      received_at: timestamp,
      bid_price: String(referencePrice - 1),
      ask_price: String(referencePrice + 1),
      reference_price: String(referencePrice),
    }));
    routedSocket.send(JSON.stringify({
      type: "bar",
      instrument_ref: "BTCUSDT-PERP",
      interval,
      source: "BINANCE_DEMO_PUBLIC",
      source_cutoff: timestamp,
      received_at: timestamp,
      closed: false,
      bar: {
        open_at: new Date(openAt).toISOString(),
        close_at: new Date(openAt + intervalMilliseconds).toISOString(),
        open: String(referencePrice),
        high: String(referencePrice + 5),
        low: String(referencePrice - 5),
        close: String(referencePrice),
        volume: "10",
      },
    }));
    quoteFrames += 1;
    barFrames += 1;
  };
  await page.routeWebSocket(/\/api\/v1\/market-stream/, (socket) => {
    websocketConnections += 1;
    routedSocket = socket;
    sendCurrentFrames(streamInterval);
    const timer = setInterval(() => sendCurrentFrames(streamInterval), 1_000);
    socket.onClose(() => clearInterval(timer));
  });
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/v1/market-window") {
      marketWindowIntervals.push(url.searchParams.get("interval") ?? "");
      marketWindowPurposes.push(url.searchParams.get("purpose") ?? "");
    }
    if (url.pathname === "/api/v1/order-schedules/preview") {
      previewRequests += 1;
      const payload = request.postDataJSON() as {
        spec?: {
          price_distribution?: { kind?: string; limit_price?: string | null };
          venue_policy?: { order_type?: string };
        };
      };
      const pricePlan = payload.spec?.price_distribution;
      if (
        payload.spec?.venue_policy?.order_type === "LIMIT"
        && pricePlan?.kind === "SINGLE"
        && !(Number(pricePlan.limit_price) > 0)
      ) {
        invalidPreviewRequests.push(payload);
      }
    }
  });
  await routeDirectDraftAutosave(page, attemptedTradingWrites);
  await routeCurrentDemoMarketWindow(page);
  await routeCurrentDemoMarketContext(page);
  await routeReadyDemoExecutor(page);
  await routeValidOrderSchedulePreview(page);

  await page.goto("/plans/new");
  await page.getByRole("button", { name: "配置订单计划", exact: true }).click();
  const chartRegion = page.locator('section[aria-labelledby="order-schedule-chart-title"]');
  await expect(chartRegion).toBeVisible();
  await expect(page.getByText("实时", { exact: true }).first()).toBeVisible({
    timeout: 15_000,
  });
  await expect.poll(() => quoteFrames, { timeout: 15_000 }).toBeGreaterThanOrEqual(2);
  await expect.poll(() => barFrames, { timeout: 15_000 }).toBeGreaterThanOrEqual(1);
  await expect(chartRegion.getByRole("status")).toHaveCount(0, { timeout: 15_000 });
  await expect(chartRegion.getByTestId("order-schedule-chart-market-source"))
    .toHaveText("Demo · Binance K线");
  const chart = chartRegion.getByTestId("order-schedule-kline-chart");
  const annotationScaleToggle = chartRegion.getByRole("checkbox", {
    name: "全部价格标注纳入缩放",
  });
  await expect(annotationScaleToggle).not.toBeChecked();
  await expect(chart).toHaveAttribute("data-annotations-in-scale", "false");
  await annotationScaleToggle.check();
  await expect(chart).toHaveAttribute("data-annotations-in-scale", "true");

  const selectInterval = async (interval: "1m" | "1h") => {
    if (testInfo.project.name === "chromium-narrow") {
      await chartRegion.getByLabel("K 线周期").click();
      await page.getByRole("option", { name: interval, exact: true }).click();
    } else {
      await chartRegion
        .getByRole("group", { name: "K 线周期" })
        .getByRole("button", { name: interval, exact: true })
        .click();
    }
    streamInterval = interval;
    sendCurrentFrames(interval);
    await expect(chartRegion.getByRole("heading", {
      name: `${interval} K 线 · 草稿投影`,
      exact: true,
    })).toBeVisible();
    await expect.poll(
      () => marketWindowIntervals.filter((value) => value === interval).length,
      { timeout: 15_000 },
    ).toBeGreaterThanOrEqual(1);
    await expect(chartRegion.getByRole("status")).toHaveCount(0, { timeout: 15_000 });
  };

  await selectInterval("1h");
  await expect(annotationScaleToggle).toBeChecked();
  await expect(chart).toHaveAttribute("data-annotations-in-scale", "true");
  await selectInterval("1m");
  await annotationScaleToggle.uncheck();
  await expect(chart).toHaveAttribute("data-annotations-in-scale", "false");
  await openDirectReview(page);
  const saveButton = page.getByRole("button", { name: "保存草稿", exact: true });
  await expect(saveButton).toBeEnabled({
    timeout: 15_000,
  });
  const previewBaseline = previewRequests;
  const quoteBaseline = quoteFrames;
  for (let index = 0; index < 3; index += 1) {
    sendCurrentFrames(streamInterval);
    await page.waitForTimeout(300);
    expect(
      previewRequests,
      `第 ${index + 1} 个实时行情 tick 不得重发订单计划预览`,
    ).toBe(previewBaseline);
    await expect(saveButton).toBeEnabled();
  }
  await expect.poll(() => quoteFrames).toBeGreaterThanOrEqual(quoteBaseline + 3);
  expect(
    previewRequests,
    "实时行情 tick 不得持续触发订单计划预览",
  ).toBe(previewBaseline);
  expect(websocketConnections).toBe(1);
  expect(new Set(marketWindowPurposes)).toEqual(new Set(["EXECUTION_REVIEW"]));
  expect(invalidPreviewRequests).toEqual([]);
  expect(attemptedTradingWrites).toEqual([]);
  if (testInfo.project.name === "chromium-narrow") {
    const milestones = page.getByTestId("direct-order-milestones");
    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    await expect.poll(async () => (await milestones.boundingBox())?.y ?? -1)
      .toBeGreaterThanOrEqual(95);
  }
});

test("unknown plan creation reuses its request identity until the user changes the intent", async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name === "chromium-narrow",
    "Request identity semantics are viewport-independent and are covered once on desktop.",
  );
  test.setTimeout(45_000);
  const idempotencyKeys: string[] = [];
  let createAttempts = 0;
  await routeCurrentDemoMarketStream(page);

  await page.route(/\/api\/v1\/plans(?:\?.*)?$/, async (route) => {
    const request = route.request();
    if (request.method() !== "POST") {
      await route.continue();
      return;
    }
    createAttempts += 1;
    idempotencyKeys.push(await request.headerValue("idempotency-key") ?? "");
    if (createAttempts === 1) {
      await route.abort("failed");
      return;
    }
    if (createAttempts === 2) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: { code: "SYNTHETIC_RESULT_UNKNOWN" } }),
      });
      return;
    }
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({ plan: { plan_id: "synthetic-plan-create" } }),
    });
  });

  await page.goto("/plans/new");
  await page.getByRole("button", { name: "配置订单计划", exact: true }).click();
  await openDirectReview(page);
  await expect(page.getByRole("heading", { name: "计划信息", exact: true })).toBeVisible();
  await page.getByLabel("计划名称").fill("持久幂等身份验证");
  const saveButton = page.getByRole("button", { name: "保存草稿", exact: true });
  await expect(saveButton).toBeEnabled({ timeout: 20_000 });

  await saveButton.click();
  await expect(page.getByRole("alert").filter({
    hasText: "再次提交会沿用同一请求身份核对原结果",
  })).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "配置订单计划", exact: true }).click();
  await openDirectReview(page);
  await expect(page.getByRole("heading", { name: "计划信息", exact: true })).toBeVisible();
  await page.getByLabel("计划名称").fill("持久幂等身份验证");
  const reloadedSaveButton = page.getByRole("button", { name: "保存草稿", exact: true });
  await expect(reloadedSaveButton).toBeEnabled({ timeout: 20_000 });
  await reloadedSaveButton.click();
  await expect.poll(() => idempotencyKeys.length).toBe(2);
  expect(idempotencyKeys[0]).toBeTruthy();
  expect(idempotencyKeys[1]).toBe(idempotencyKeys[0]);

  await page.getByLabel("计划名称").fill("持久幂等身份验证-新意图");
  await expect(reloadedSaveButton).toBeEnabled({ timeout: 20_000 });
  await reloadedSaveButton.click();
  await expect(page).toHaveURL(/\/plans$/);
  expect(idempotencyKeys).toHaveLength(3);
  expect(idempotencyKeys[2]).toBeTruthy();
  expect(idempotencyKeys[2]).not.toBe(idempotencyKeys[0]);
});

test("an unknown draft update reloads and shows the applied server version", async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name === "chromium-narrow",
    "Unknown-update reconciliation is viewport-independent and is covered once on desktop.",
  );
  const planId = "synthetic-edit-applied";
  let serverVersion = 1;
  let serverPlanName = "原始直接执行草稿";
  let reads = 0;
  await routeCurrentDemoMarketStream(page);
  await page.route(`**/api/v1/plans/${planId}`, async (route) => {
    const request = route.request();
    if (request.method() === "PUT") {
      const payload = request.postDataJSON() as { plan_name?: string };
      serverVersion = 2;
      serverPlanName = payload.plan_name ?? serverPlanName;
      await route.abort("connectionreset");
      return;
    }
    reads += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(syntheticDirectDraft(
        planId,
        serverVersion,
        serverPlanName,
      )),
    });
  });

  await page.goto("/plans");
  await page.evaluate((nextPlanId) => {
    window.history.pushState({}, "", `/plans/${nextPlanId}/edit`);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, planId);
  await expect(page).toHaveURL(new RegExp(`/plans/${planId}/edit$`));
  await openDirectReview(page);
  await expect(page.getByRole("heading", { name: "计划信息", exact: true })).toBeVisible();
  const planName = page.getByLabel("计划名称");
  await expect(planName).toHaveValue("原始直接执行草稿");
  await planName.fill("服务器已应用的草稿");
  const saveButton = page.getByRole("button", { name: "保存计划修改" });
  await expect(saveButton).toBeEnabled({ timeout: 20_000 });
  await saveButton.click();

  await expect(page.getByRole("alert").filter({
    hasText: "服务器草稿已刷新至版本 2",
  })).toBeVisible();
  await expect(planName).toHaveValue("服务器已应用的草稿");
  expect(reads).toBeGreaterThanOrEqual(2);
});

test("a failed unknown-update reload remains blocked until retry succeeds", async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name === "chromium-narrow",
    "Unknown-update reconciliation is viewport-independent and is covered once on desktop.",
  );
  const planId = "synthetic-edit-retry";
  let reads = 0;
  let recoveryAvailable = false;
  await routeCurrentDemoMarketStream(page);
  await page.route(`**/api/v1/plans/${planId}`, async (route) => {
    const request = route.request();
    if (request.method() === "PUT") {
      await route.abort("connectionreset");
      return;
    }
    reads += 1;
    if (reads > 1 && !recoveryAvailable) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: { code: "SYNTHETIC_PLAN_READ_FAILED" } }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(syntheticDirectDraft(
        planId,
        1,
        "服务器原始草稿",
      )),
    });
  });

  await page.goto("/plans");
  await page.evaluate((nextPlanId) => {
    window.history.pushState({}, "", `/plans/${nextPlanId}/edit`);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, planId);
  await expect(page).toHaveURL(new RegExp(`/plans/${planId}/edit$`));
  await openDirectReview(page);
  await expect(page.getByRole("heading", { name: "计划信息", exact: true })).toBeVisible();
  await page.getByLabel("计划名称").fill("尚未确认的本地修改");
  const saveButton = page.getByRole("button", { name: "保存计划修改" });
  await expect(saveButton).toBeEnabled({ timeout: 20_000 });
  await saveButton.click();

  const failedAlert = page.getByRole("alert").filter({
    hasText: "服务器草稿读取失败",
  });
  await expect(failedAlert).toBeAttached({ timeout: 20_000 });
  await failedAlert.scrollIntoViewIfNeeded();
  await expect(failedAlert).toBeVisible();
  await expect(saveButton).toBeDisabled();
  recoveryAvailable = true;
  await failedAlert.getByRole("button", { name: "重新读取草稿" }).click();
  await expect(page.getByRole("alert").filter({
    hasText: "服务器草稿已刷新至版本 1",
  })).toBeVisible();
  await expect(page.getByLabel("计划名称")).toHaveValue("服务器原始草稿");
  await expect(saveButton).toBeEnabled({ timeout: 20_000 });
  expect(reads).toBeGreaterThanOrEqual(3);
});

test("LIVE status with stale source timestamps blocks direct execution", async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name === "chromium-narrow",
    "Timestamp freshness is viewport-independent and is covered once on desktop.",
  );
  const staleAt = new Date(Date.now() - 30_000).toISOString();
  await page.routeWebSocket(/\/api\/v1\/market-stream/, (socket) => {
    socket.send(JSON.stringify({
      type: "status",
      state: "LIVE",
      source: "BINANCE_DEMO_PUBLIC",
      observed_at: new Date().toISOString(),
      reason: null,
    }));
    socket.send(JSON.stringify({
      type: "quote",
      instrument_ref: "BTCUSDT-PERP",
      source: "BINANCE_DEMO_PUBLIC",
      source_cutoff: staleAt,
      received_at: new Date().toISOString(),
      bid_price: "65000",
      ask_price: "65002",
      reference_price: "65001",
    }));
    socket.send(JSON.stringify({
      type: "bar",
      instrument_ref: "BTCUSDT-PERP",
      interval: "15m",
      source: "BINANCE_DEMO_PUBLIC",
      source_cutoff: staleAt,
      received_at: new Date().toISOString(),
      closed: false,
      bar: {
        open_at: "2026-07-23T11:30:00.000Z",
        close_at: "2026-07-23T11:45:00.000Z",
        open: "65000",
        high: "65005",
        low: "64995",
        close: "65001",
        volume: "10",
      },
    }));
  });

  await page.goto("/plans/new");
  await page.getByRole("button", { name: "配置订单计划", exact: true }).click();

  await expect(page.getByText("已过期", { exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: "市价", exact: true }).click();
  await openDirectReview(page);
  await expect(page.getByRole("button", { name: "保存草稿", exact: true })).toBeDisabled();
  const chart = page.getByTestId("order-schedule-kline-chart");
  await expect(chart).not.toHaveAttribute("data-market-live-source");
  await expect(page.getByRole("status").filter({
    hasText: "价格预览与保存已阻断",
  })).toBeVisible();
});

test("an invalid live bar clears the previous bar and keeps direct execution blocked", async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name === "chromium-narrow",
    "Live-bar invalidation is viewport-independent and is covered once on desktop.",
  );
  test.setTimeout(60_000);
  let routedSocket: {
    send: (message: string | Buffer) => void;
  } | null = null;
  let keepCurrentFrames = true;
  let marketWindowRequests = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/v1/market-window") {
      marketWindowRequests += 1;
    }
  });
  const currentTimestamp = () => new Date(Date.now() + 4_000).toISOString();
  const quoteFrame = (timestamp: string) => JSON.stringify({
    type: "quote",
    instrument_ref: "BTCUSDT-PERP",
    source: "BINANCE_DEMO_PUBLIC",
    source_cutoff: timestamp,
    received_at: timestamp,
    bid_price: "65000",
    ask_price: "65002",
    reference_price: "65001",
  });
  const barFrame = (timestamp: string) => {
    const observedAt = Date.parse(timestamp);
    return JSON.stringify({
      type: "bar",
      instrument_ref: "BTCUSDT-PERP",
      interval: "15m",
      source: "BINANCE_DEMO_PUBLIC",
      source_cutoff: timestamp,
      received_at: timestamp,
      closed: false,
      bar: {
        open_at: new Date(observedAt - 5 * 60_000).toISOString(),
        close_at: new Date(observedAt + 10 * 60_000).toISOString(),
        open: "65000",
        high: "65005",
        low: "64995",
        close: "65001",
        volume: "10",
      },
    });
  };

  await page.routeWebSocket(/\/api\/v1\/market-stream/, (socket) => {
    routedSocket = socket;
    const sendCurrentFrames = () => {
      if (!keepCurrentFrames) return;
      const timestamp = currentTimestamp();
      socket.send(JSON.stringify({
        type: "status",
        state: "LIVE",
        source: "BINANCE_DEMO_PUBLIC",
        observed_at: timestamp,
        reason: null,
      }));
      socket.send(quoteFrame(timestamp));
      socket.send(barFrame(timestamp));
    };
    sendCurrentFrames();
    const timer = setInterval(sendCurrentFrames, 1_000);
    socket.onClose(() => clearInterval(timer));
  });
  await routeCurrentDemoMarketWindow(page);
  await routeCurrentDemoMarketContext(page);
  await routeReadyDemoExecutor(page);
  await routeValidOrderSchedulePreview(page);

  await page.goto("/plans/new");
  await page.getByRole("button", { name: "配置订单计划", exact: true }).click();
  const chartRegion = page.locator('section[aria-labelledby="order-schedule-chart-title"]');
  const chart = chartRegion.getByTestId("order-schedule-kline-chart");
  await openDirectReview(page);
  expect(routedSocket).not.toBeNull();
  const initialAt = currentTimestamp();
  routedSocket!.send(quoteFrame(initialAt));
  routedSocket!.send(barFrame(initialAt));
  const saveButton = page.getByRole("button", { name: "保存草稿", exact: true });
  await expect(chart).toHaveAttribute(
    "data-market-live-source",
    "BINANCE_DEMO_PUBLIC",
  );
  await expect(saveButton).toBeEnabled({ timeout: 20_000 });
  await expect.poll(() => marketWindowRequests).toBeGreaterThanOrEqual(1);
  const initialMarketWindowRequests = marketWindowRequests;

  keepCurrentFrames = false;
  const staleAt = new Date(Date.now() - 30_000).toISOString();
  routedSocket!.send(barFrame(staleAt));

  await expect(chart).not.toHaveAttribute("data-market-live-source");
  await expect(chartRegion.getByRole("status")).toContainText(
    "价格预览与保存已阻断",
  );
  await expect(saveButton).toBeDisabled();

  keepCurrentFrames = true;
  const recoveredAt = currentTimestamp();
  routedSocket!.send(quoteFrame(recoveredAt));
  routedSocket!.send(barFrame(recoveredAt));
  await expect.poll(() => marketWindowRequests)
    .toBeGreaterThan(initialMarketWindowRequests);
  await expect(chart).toHaveAttribute(
    "data-market-live-source",
    "BINANCE_DEMO_PUBLIC",
  );
  await expect(saveButton).toBeEnabled({ timeout: 20_000 });
});

test("runtime environment identity change hard-reloads and discards the old planning workspace", async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name === "chromium-narrow",
    "The environment boundary is viewport-independent and is covered once on desktop.",
  );
  test.setTimeout(30_000);
  let statusRequests = 0;
  await page.clock.install();
  await page.route("**/api/v1/settings/status", async (route) => {
    const response = await route.fetch();
    const status = await response.json() as Record<string, unknown>;
    statusRequests += 1;
    const switched = statusRequests > 1;
    await route.fulfill({
      response,
      contentType: "application/json",
      body: JSON.stringify({
        ...status,
        environment_kind: "DEMO",
        environment_id: switched ? "e2e-demo-replacement" : "e2e-demo-primary",
      }),
    });
  });

  await page.goto("/plans/new");
  await page.getByRole("button", { name: "配置订单计划", exact: true }).click();
  await expect(page.getByTestId("direct-execution-workspace")).toBeVisible();

  await page.clock.fastForward(30_500);
  await expect.poll(() => statusRequests, { timeout: 15_000 }).toBeGreaterThanOrEqual(3);
  await expect(page.getByTestId("direct-execution-workspace")).toHaveCount(0);
  await expect(page.getByRole("combobox", { name: "交易上下文" }))
    .toContainText("Demo");
  await expect(page).toHaveURL(/\/plans\/new$/);
});

test("the global trading-context switch discards object identity and navigates only to the selected overview", async ({ page }) => {
  let targetRequestUrl: string | null = null;
  await page.route("**/api/v1/settings/status", async (route) => {
    const response = await route.fetch();
    const status = await response.json() as Record<string, unknown>;
    await route.fulfill({
      response,
      json: {
        ...status,
        environment_kind: "DEMO",
        environment_id: "e2e-demo-primary",
        account_id: "e2e-demo-account",
        venue_account_type: "USDM_DEMO",
        profile: "BINANCE_DEMO",
        trading_contexts: [
          {
            venue_account_type: "USDM_DEMO",
            environment_id: "e2e-demo-primary",
            account_id: "e2e-demo-account",
            url: "http://127.0.0.1:8765/overview",
          },
          {
            venue_account_type: "USDM_COPY_LEAD",
            environment_id: "e2e-live-copy-primary",
            account_id: "e2e-live-copy-account",
            url: "http://127.0.0.1:8766/plans/live-only-id?copyFrom=demo-only-id#orders",
          },
          {
            venue_account_type: "USDM_PERSONAL",
            environment_id: "e2e-live-personal-primary",
            account_id: "e2e-live-personal-account",
            url: "http://127.0.0.1:8767/reviews/personal-only-id",
          },
        ],
      },
    });
  });
  await page.route("http://127.0.0.1:8766/overview", async (route) => {
    targetRequestUrl = route.request().url();
    await new Promise((resolve) => setTimeout(resolve, 150));
    await route.fulfill({
      contentType: "text/html",
      body: "<!doctype html><title>Live target</title><main>Live overview target</main>",
    });
  });

  await page.goto("/overview?activation_id=demo-only-id#facts");
  await page.getByRole("combobox", { name: "交易上下文" }).click();
  await page.getByRole("option", { name: "实盘 · 带单账户" }).click();
  await expect.poll(() => targetRequestUrl)
    .toBe("http://127.0.0.1:8766/overview");
  await expect(page).toHaveURL("http://127.0.0.1:8766/overview");
});

test("an invalid trading-context target is disabled with a keyboard-readable reason", async ({ page }) => {
  await page.route("**/api/v1/settings/status", async (route) => {
    const response = await route.fetch();
    const status = await response.json() as Record<string, unknown>;
    await route.fulfill({
      response,
      json: {
        ...status,
        environment_kind: "DEMO",
        environment_id: "e2e-demo-primary",
        account_id: "e2e-demo-account",
        venue_account_type: "USDM_DEMO",
        profile: "BINANCE_DEMO",
        trading_contexts: [
          {
            venue_account_type: "USDM_DEMO",
            environment_id: "e2e-demo-primary",
            account_id: "e2e-demo-account",
            url: "http://127.0.0.1:8765/overview",
          },
          {
            venue_account_type: "USDM_COPY_LEAD",
            environment_id: "e2e-live-copy-primary",
            account_id: "e2e-live-copy-account",
            url: "https://example.com/overview",
          },
          {
            venue_account_type: "USDM_PERSONAL",
            environment_id: "e2e-live-personal-primary",
            account_id: "e2e-live-personal-account",
            url: "http://127.0.0.1:8767/overview",
          },
        ],
      },
    });
  });

  await page.goto("/overview");
  await page.getByRole("combobox", { name: "交易上下文" }).click();
  await expect(page.getByRole("option", { name: "实盘 · 带单账户" }))
    .toBeDisabled();
  await page.keyboard.press("Escape");
  const reason = page.getByRole("button", {
    name: "至少一个交易上下文入口无效；对应选项已禁用，当前上下文不变。",
  });
  await reason.focus();
  await expect(page.getByRole("tooltip"))
    .toContainText("至少一个交易上下文入口无效；对应选项已禁用，当前上下文不变。");
});

test("Demo chart rejects Live history and Live stream frames before a clean reload", async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name === "chromium-narrow",
    "Market-source isolation is viewport-independent and is covered once on desktop.",
  );
  let routedSocket: {
    send: (message: string | Buffer) => void;
  } | null = null;
  const marketWindowPurposes: string[] = [];
  let serveWrongSource = true;

  await page.route("**/api/v1/market-window?**", async (route) => {
    const url = new URL(route.request().url());
    marketWindowPurposes.push(url.searchParams.get("purpose") ?? "");
    const interval = url.searchParams.get("interval") ?? "15m";
    const intervalMs = interval === "1m"
      ? 60_000
      : interval === "5m"
        ? 5 * 60_000
        : interval === "15m"
          ? 15 * 60_000
          : interval === "1h"
            ? 60 * 60_000
            : interval === "4h"
              ? 4 * 60 * 60_000
              : 24 * 60 * 60_000;
    const requestedStart = Date.parse(url.searchParams.get("start_at") ?? "");
    const startAt = Number.isFinite(requestedStart)
      ? requestedStart
      : Date.parse("2026-07-22T00:00:00Z");
    const bars = Array.from({ length: 12 }, (_value, index) => {
      const openAt = startAt + index * intervalMs;
      const open = 65_000 + index * 4;
      const close = open + (index % 2 === 0 ? 2 : -2);
      return {
        open_at: new Date(openAt).toISOString(),
        close_at: new Date(openAt + intervalMs).toISOString(),
        open: String(open),
        high: String(Math.max(open, close) + 3),
        low: String(Math.min(open, close) - 3),
        close: String(close),
        volume: String(10 + index),
      };
    });
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        instrument_ref: "BTCUSDT-PERP",
        interval,
        source: serveWrongSource ? "BINANCE_LIVE_PUBLIC" : "BINANCE_DEMO_PUBLIC",
        source_cutoff: url.searchParams.get("end_at") ?? "2026-07-23T10:00:00Z",
        bars,
      }),
    });
  });

  const streamBar = (source: string) => {
    const observedAt = new Date(Date.now() + 4_000).toISOString();
    return JSON.stringify({
      type: "bar",
      instrument_ref: "BTCUSDT-PERP",
      interval: "15m",
      source,
      source_cutoff: observedAt,
      received_at: observedAt,
      closed: false,
      bar: {
        open_at: "2026-07-23T10:00:00.000Z",
        close_at: "2026-07-23T10:15:00.000Z",
        open: "65050",
        high: "65060",
        low: "65045",
        close: "65055",
        volume: "20",
      },
    });
  };
  await page.routeWebSocket(/\/api\/v1\/market-stream/, (socket) => {
    routedSocket = socket;
    const observedAt = new Date().toISOString();
    socket.send(JSON.stringify({
      type: "status",
      state: "LIVE",
      source: "BINANCE_DEMO_PUBLIC",
      observed_at: observedAt,
      reason: null,
    }));
    socket.send(JSON.stringify({
      type: "quote",
      instrument_ref: "BTCUSDT-PERP",
      source: "BINANCE_DEMO_PUBLIC",
      source_cutoff: observedAt,
      received_at: observedAt,
      bid_price: "65049",
      ask_price: "65051",
      reference_price: "65050",
    }));
    socket.send(streamBar("BINANCE_LIVE_PUBLIC"));
  });

  await page.goto("/plans/new");
  await page.getByRole("button", { name: "配置订单计划", exact: true }).click();
  const chartRegion = page.locator('section[aria-labelledby="order-schedule-chart-title"]');
  const chart = chartRegion.getByTestId("order-schedule-kline-chart");
  await expect(chartRegion.getByRole("status")).toContainText(
    "K 线来源与当前环境不一致",
  );
  expect(new Set(marketWindowPurposes)).toEqual(new Set(["EXECUTION_REVIEW"]));
  await expect(chart).not.toHaveAttribute("data-market-history-source");
  await expect(chart).not.toHaveAttribute("data-market-live-source");
  await expect(chartRegion.getByRole("status")).toContainText(
    "K 线来源与当前环境不一致",
  );

  expect(routedSocket).not.toBeNull();
  serveWrongSource = false;
  const recoveredAt = new Date().toISOString();
  routedSocket!.send(JSON.stringify({
    type: "status",
    state: "LIVE",
    source: "BINANCE_DEMO_PUBLIC",
    observed_at: recoveredAt,
    reason: "MARKET_STREAM_SOURCE_RECOVERED",
  }));
  routedSocket!.send(JSON.stringify({
    type: "quote",
    instrument_ref: "BTCUSDT-PERP",
    source: "BINANCE_DEMO_PUBLIC",
    source_cutoff: recoveredAt,
    received_at: recoveredAt,
    bid_price: "65049",
    ask_price: "65051",
    reference_price: "65050",
  }));
  routedSocket!.send(streamBar("BINANCE_DEMO_PUBLIC"));
  await chartRegion
    .getByRole("button", { name: "重试 K 线" })
    .evaluate((button: HTMLButtonElement) => button.click())
    .catch(() => undefined);
  await expect(chart).toHaveAttribute(
    "data-market-history-source",
    "BINANCE_DEMO_PUBLIC",
  );
  await expect(chart).toHaveAttribute(
    "data-market-live-source",
    "BINANCE_DEMO_PUBLIC",
  );
  await expect(chartRegion.getByText("Demo · Binance K线", { exact: true })).toBeVisible();
  await expect(chartRegion.getByText("K线实时", { exact: true })).toBeVisible();
});

test("Demo K-line history remains identified while reconnecting clears realtime prices and blocks save", async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name === "chromium-narrow",
    "Market-route independence is viewport-independent and is covered once on desktop.",
  );
  let marketWindowRequests = 0;
  const marketWindowPurposes: string[] = [];

  await page.route("**/api/v1/market-context?**", async (route) => {
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: { code: "DEMO_EXECUTION_MARKET_UNAVAILABLE" } }),
    });
  });
  await page.route("**/api/v1/market-window?**", async (route) => {
    marketWindowRequests += 1;
    const url = new URL(route.request().url());
    marketWindowPurposes.push(url.searchParams.get("purpose") ?? "");
    const interval = url.searchParams.get("interval") ?? "15m";
    const intervalMs = interval === "1m"
      ? 60_000
      : interval === "5m"
        ? 5 * 60_000
        : interval === "15m"
          ? 15 * 60_000
          : interval === "1h"
            ? 60 * 60_000
            : interval === "4h"
              ? 4 * 60 * 60_000
              : 24 * 60 * 60_000;
    const requestedStart = Date.parse(url.searchParams.get("start_at") ?? "");
    const startAt = Number.isFinite(requestedStart)
      ? requestedStart
      : Date.parse("2026-07-22T00:00:00Z");
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        instrument_ref: "BTCUSDT-PERP",
        interval,
        source: "BINANCE_DEMO_PUBLIC",
        source_cutoff: url.searchParams.get("end_at") ?? new Date().toISOString(),
        bars: Array.from({ length: 12 }, (_value, index) => {
          const openAt = startAt + index * intervalMs;
          return {
            open_at: new Date(openAt).toISOString(),
            close_at: new Date(openAt + intervalMs).toISOString(),
            open: String(65_000 + index),
            high: String(65_006 + index),
            low: String(64_996 + index),
            close: String(65_002 + index),
            volume: String(10 + index),
          };
        }),
      }),
    });
  });
  await page.routeWebSocket(/\/api\/v1\/market-stream/, (socket) => {
    const observedAt = new Date().toISOString();
    socket.send(JSON.stringify({
      type: "quote",
      instrument_ref: "BTCUSDT-PERP",
      source: "BINANCE_DEMO_PUBLIC",
      source_cutoff: observedAt,
      received_at: observedAt,
      bid_price: "12345",
      ask_price: "12346",
      reference_price: "12345.5",
    }));
    socket.send(JSON.stringify({
      type: "status",
      state: "RECONNECTING",
      source: "BINANCE_DEMO_PUBLIC",
      observed_at: observedAt,
      reason: "MARKET_STREAM_RECONNECTED",
    }));
  });

  await page.goto("/plans/new");
  await page.getByRole("button", { name: "配置订单计划", exact: true }).click();
  const chartRegion = page.locator('section[aria-labelledby="order-schedule-chart-title"]');
  const chart = chartRegion.getByTestId("order-schedule-kline-chart");

  await expect.poll(() => marketWindowRequests).toBeGreaterThanOrEqual(1);
  expect(new Set(marketWindowPurposes)).toEqual(new Set(["EXECUTION_REVIEW"]));
  await expect(chart).toHaveAttribute(
    "data-market-history-source",
    "BINANCE_DEMO_PUBLIC",
  );
  await expect(page.getByText("重连中", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("12,345.50", { exact: true })).toHaveCount(0);
  await expect(chartRegion.getByText("Demo · Binance K线", { exact: true })).toBeVisible();
  await expect(chart).not.toHaveAttribute("data-market-live-source");
  await expect(chartRegion.getByRole("status")).toContainText("价格预览与保存已阻断");
  await page.getByRole("button", { name: "市价", exact: true }).click();
  await openDirectReview(page);
  await expect(page.getByRole("button", { name: "保存草稿", exact: true })).toBeDisabled();
});

test("direct execution reconnects the local market stream and resynchronizes history", async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name === "chromium-narrow",
    "Transport recovery is viewport-independent and is covered once on desktop.",
  );
  test.setTimeout(30_000);
  const routedSockets: Array<{
    close: (options?: { code?: number; reason?: string }) => Promise<void>;
    send: (message: string | Buffer) => void;
  }> = [];
  let marketWindowRequests = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/v1/market-window") {
      marketWindowRequests += 1;
    }
  });
  await page.routeWebSocket(/\/api\/v1\/market-stream/, (socket) => {
    routedSockets.push(socket);
    const connectionNumber = routedSockets.length;
    const reference = connectionNumber === 1 ? "65000.5" : "65001.5";
    const sendCurrentFrames = () => {
      const observedAt = new Date(Date.now() + 4_000).toISOString();
      socket.send(JSON.stringify({
        type: "status",
        state: "LIVE",
        source: "BINANCE_DEMO_PUBLIC",
        observed_at: observedAt,
        reason: null,
      }));
      socket.send(JSON.stringify({
        type: "quote",
        instrument_ref: "BTCUSDT-PERP",
        source: "BINANCE_DEMO_PUBLIC",
        source_cutoff: observedAt,
        received_at: observedAt,
        bid_price: connectionNumber === 1 ? "65000" : "65001",
        ask_price: connectionNumber === 1 ? "65001" : "65002",
        reference_price: reference,
      }));
      socket.send(JSON.stringify({
        type: "bar",
        instrument_ref: "BTCUSDT-PERP",
        interval: "15m",
        source: "BINANCE_DEMO_PUBLIC",
        source_cutoff: observedAt,
        received_at: observedAt,
        closed: false,
        bar: {
          open_at: "2026-07-23T11:30:00.000Z",
          close_at: "2026-07-23T11:45:00.000Z",
          open: "65000",
          high: "65002",
          low: "64999",
          close: reference,
          volume: "10",
        },
      }));
    };
    const startCurrentFrames = () => {
      sendCurrentFrames();
      const timer = setInterval(sendCurrentFrames, 1_000);
      socket.onClose(() => clearInterval(timer));
    };
    if (connectionNumber === 1) {
      startCurrentFrames();
    } else {
      setTimeout(startCurrentFrames, 1_000);
    }
  });
  await routeCurrentDemoMarketWindow(page);
  await routeCurrentDemoMarketContext(page);
  await routeReadyDemoExecutor(page);
  await routeValidOrderSchedulePreview(page);

  await page.goto("/plans/new");
  await page.getByRole("button", { name: "配置订单计划", exact: true }).click();
  await expect(page.getByText("实时", { exact: true }).first()).toBeVisible({
    timeout: 10_000,
  });
  await expect(page.getByText("65,000.50", { exact: true }).first()).toBeVisible();
  await expect.poll(() => routedSockets.length).toBe(1);
  await expect.poll(() => marketWindowRequests, { timeout: 10_000 })
    .toBeGreaterThanOrEqual(1);
  const initialMarketWindowRequests = marketWindowRequests;
  await openDirectReview(page);

  await routedSockets[0]!.close({ code: 1012, reason: "QUALIFICATION_RECONNECT" });
  await expect(page.getByText("重连中", { exact: true }).first()).toBeVisible();
  const chartRegion = page.locator('section[aria-labelledby="order-schedule-chart-title"]');
  const chart = chartRegion.getByTestId("order-schedule-kline-chart");
  await expect(chartRegion.getByRole("status")).toContainText("价格预览与保存已阻断");
  await expect(chart).not.toHaveAttribute("data-market-live-source");
  await expect(page.getByRole("button", { name: "保存草稿", exact: true })).toBeDisabled();
  await expect.poll(() => routedSockets.length, { timeout: 10_000 }).toBe(2);
  await expect(page.getByText("实时", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("65,001.50", { exact: true }).first()).toBeVisible();
  await expect(chart).toHaveAttribute("data-market-live-source", "BINANCE_DEMO_PUBLIC");
  await expect(page.getByRole("button", { name: "保存草稿", exact: true })).toBeEnabled();
  await expect.poll(() => marketWindowRequests, { timeout: 10_000 })
    .toBeGreaterThan(initialMarketWindowRequests);
});

test("direct execution keeps the K-line chart as the primary annotated workspace", async ({ page }, testInfo) => {
  const attemptedTradingWrites: string[] = [];
  const invalidDraftWrites: unknown[] = [];
  await routeCurrentDemoMarketStream(page);
  await routeCurrentDemoMarketWindow(page);
  await routeCurrentDemoMarketContext(page);
  await routeReadyDemoExecutor(page);
  await routeValidOrderSchedulePreview(page);
  await routeDirectDraftAutosave(page, attemptedTradingWrites);
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if ((request.method() === "POST" && path === "/api/v1/plans")
      || (request.method() === "PUT" && path === "/api/v1/plans/workspace-autosave-fixture")) {
      const payload = request.postDataJSON() as { max_notional?: string };
      if (!(Number(payload.max_notional) > 0)) invalidDraftWrites.push(payload);
    }
  });
  if (testInfo.project.name === "chromium-desktop") {
    await page.setViewportSize({ width: 1123, height: 920 });
  }
  await page.goto("/plans/new");
  await page.getByRole("button", { name: "配置订单计划", exact: true }).click();
  await expect(page.getByRole("heading", { name: "直接执行" })).toBeVisible();
  await expect(page.getByRole("button", { name: /重新选择/ })).toHaveCount(0);
  await expect(page.getByText("已选执行依据", { exact: true })).toHaveCount(0);

  const chartRegion = page.getByRole("region", { name: "15m K 线 · 草稿投影" });
  const chart = chartRegion.getByRole("group", {
    name: /订单计划 15m K 线主图/,
  });
  const chartDetail = chartRegion.locator("details").filter({
    hasText: /图线与操作点/,
  }).first();
  if (!await chartDetail.evaluate((element) => (
    (element as HTMLDetailsElement).open
  ))) {
    await chartDetail.locator("summary").click();
  }
  const priceAnnotations = chartRegion.getByLabel("图中价格标注及等价数值");
  await expect(chartRegion).toBeVisible();
  await expect(chart).toBeVisible();
  await expect(page.getByLabel("限价（USDT）")).not.toHaveValue("");
  await expect(priceAnnotations).toContainText("当前计量参考价");
  await expect(priceAnnotations).toContainText("输入限价");
  await expect(chartRegion.getByRole("list", {
    name: "图中相对和动态价格规则",
  })).toContainText("当前仓位止损 · 100 bps");

  const rangeButton = chartRegion.getByRole("button", { name: "拖动选择区间" });
  await expect(rangeButton).toBeDisabled();
  if (testInfo.project.name === "chromium-desktop") {
    await expect(chartRegion.getByRole("button", { name: "支撑 / 阻力" })).toBeEnabled();
    await expect(chartRegion.getByRole("button", { name: "趋势线" })).toBeEnabled();
  } else {
    await expect(chartRegion.getByRole("button", { name: "支撑 / 阻力" })).toBeDisabled();
    await expect(chartRegion.getByRole("button", { name: "趋势线" })).toBeDisabled();
  }

  await page.getByRole("radio", { name: /价格阶梯入场/ }).click();
  await page.getByLabel("下限（USDT）", { exact: true }).fill("65000");
  await page.getByLabel("上限（USDT）", { exact: true }).fill("66000");
  await page.getByLabel("每档金额（USDT）").fill("100");
  await expect(priceAnnotations).toContainText("区间下限");
  await expect(priceAnnotations).toContainText("区间上限");
  await expect(chartRegion).toContainText("标准化入场 1/5", { timeout: 15_000 });

  await page.getByRole("button", { name: "＋ 添加入场条件或管理规则" }).click();
  await page.getByRole("button", { name: /到价触发/ }).click();
  await expect(priceAnnotations).toContainText("标记价条件 ≥");
  await page.getByRole("button", { name: "＋ 添加入场条件或管理规则" }).click();
  await page.getByRole("button", { name: /价差限制/ }).click();
  await expect(chartRegion.getByRole("list", {
    name: "图中相对和动态价格规则",
  })).toContainText("价差 ≤ 10 bps");

  if (testInfo.project.name === "chromium-desktop") {
    await expect(rangeButton).toBeEnabled();
    await rangeButton.click();
    const dragLayer = chartRegion.getByTestId("order-schedule-range-drag-layer");
    await expect(dragLayer).toBeVisible();
    await chart.press("Escape");
    await expect(dragLayer).toHaveCount(0);

    const beforeRange = await Promise.all([
      page.getByLabel("下限（USDT）", { exact: true }).inputValue(),
      page.getByLabel("上限（USDT）", { exact: true }).inputValue(),
    ]);
    await rangeButton.click();
    const bounds = await dragLayer.boundingBox();
    expect(bounds).not.toBeNull();
    await page.mouse.move(bounds!.x + bounds!.width * .45, bounds!.y + bounds!.height * .25);
    await page.mouse.down();
    await page.mouse.move(
      bounds!.x + bounds!.width * .45,
      bounds!.y + bounds!.height * .72,
      { steps: 6 },
    );
    await page.mouse.up();
    await expect(dragLayer).toHaveCount(0);
    await expect.poll(async () => Promise.all([
      page.getByLabel("下限（USDT）", { exact: true }).inputValue(),
      page.getByLabel("上限（USDT）", { exact: true }).inputValue(),
    ])).not.toEqual(beforeRange);
    await chart.press("Escape");
    await expect(page.getByLabel("下限（USDT）", { exact: true })).toHaveValue(beforeRange[0]!);
    await expect(page.getByLabel("上限（USDT）", { exact: true })).toHaveValue(beforeRange[1]!);
  } else {
    await expect(rangeButton).toBeDisabled();
  }

  await assertAccessible(page, testInfo, `direct-order-chart-${testInfo.project.name}`);
  await testInfo.attach(`direct-order-chart-${testInfo.project.name}.png`, {
    body: await chartRegion.screenshot(),
    contentType: "image/png",
  });

  const layout = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
    viewportHeight: window.innerHeight,
    pageHeight: document.documentElement.scrollHeight,
    configClientHeight: document.querySelector<HTMLElement>("[data-testid='direct-order-config-scroll']")?.clientHeight ?? 0,
    configScrollHeight: document.querySelector<HTMLElement>("[data-testid='direct-order-config-scroll']")?.scrollHeight ?? 0,
  }));
  if (layout.scrollWidth !== layout.clientWidth) {
    await assertNoDocumentHorizontalOverflow(page, testInfo, `direct-chart-${testInfo.project.name}`);
  }
  expect(layout.scrollWidth).toBe(layout.clientWidth);
  if (testInfo.project.name === "chromium-desktop") {
    expect(layout.pageHeight).toBe(layout.viewportHeight);
    expect(layout.configScrollHeight).toBeGreaterThan(layout.configClientHeight);
    await expect(page.getByRole("switch", { name: "Maker only" })).toBeVisible();
    await openDirectMilestone(page, "2 保护");
    await expect(page.getByLabel("初始止损距离（bps）")).toBeVisible();
    await openDirectMilestone(page, "1 入场");
  }

  const capitalLimit = page.getByLabel("资金上限（USDT）");
  await capitalLimit.fill("0");
  await capitalLimit.press("Enter");
  await expectDirectSubmitProblem(page, "计划交易金额必须大于 0。");
  await expect(page.getByRole("button", { name: "保存草稿", exact: true })).toBeDisabled();
  await page.waitForTimeout(300);
  expect(invalidDraftWrites).toEqual([]);
  expect(attemptedTradingWrites).toEqual([]);
  await expect(page).toHaveURL(/\/plans\/workspace-autosave-fixture\/edit$/);
});

test("direct execution binds funding, loss preview, and discipline capacity before launch", async ({ page }, testInfo) => {
  const targetedDisciplineRequests: string[] = [];
  await routeCurrentDemoMarketStream(page);
  await routeCurrentDemoMarketWindow(page);
  await routeCurrentDemoMarketContext(page);
  await routeReadyDemoExecutor(page);
  await routeValidOrderSchedulePreview(page);
  await page.route(/\/api\/v1\/overview(?:\?.*)?$/, async (route) => {
    const url = new URL(route.request().url());
    const target = `${url.searchParams.get("entry_instrument_ref") ?? ""}:${url.searchParams.get("entry_direction") ?? ""}`;
    if (target !== ":") targetedDisciplineRequests.push(target);
    await route.fulfill({
      contentType: "application/json",
      json: {
        new_risk_discipline: {
          status: "ALLOWED",
          new_risk_allowed: true,
          blocker_codes: [],
          account_snapshot_ref: "discipline-snapshot",
          account_snapshot_cutoff: "2026-08-12T15:30:00Z",
          risk_equity: "1000",
          available_notional_capacity: "900",
          max_plan_loss: "100",
          open_risk_limit: "200",
          open_risk_committed: "0",
          open_risk_after_proposal: "0",
          gross_exposure_limit: "2000",
          gross_exposure: "100",
          gross_exposure_after_proposal: "100",
          instrument_ref: "BTCUSDT-PERP",
          instrument_exposure_limit: "1000",
          instrument_exposure: "100",
          instrument_exposure_after_proposal: "100",
          correlation_cluster: "MAJORS",
          correlated_exposure_limit: "1500",
          correlated_exposure: "100",
          correlated_exposure_after_proposal: "100",
          daily_loss_limit: "75",
          daily_loss_measure: "0",
          weekly_loss_limit: "200",
          weekly_loss_measure: "0",
          rolling_peak_equity: "1000",
          rolling_drawdown: "0",
          rolling_drawdown_fraction: "0",
          rolling_drawdown_limit_fraction: "0.04",
          rolling_drawdown_lookback_days: 30,
          open_new_risk_activation_count: 0,
          day_window_started_at: "2026-08-12T16:00:00Z",
          week_window_started_at: "2026-08-09T16:00:00Z",
          evaluated_at: "2026-08-12T15:30:00Z",
        },
      },
    });
  });

  await page.goto("/plans/new?mode=direct");
  const capitalLimit = page.getByLabel("资金上限（USDT）");
  await expect(page.getByText("允许 900 USDT", { exact: true })).toBeVisible({
    timeout: 15_000,
  });
  await expect.poll(() => targetedDisciplineRequests).toContain("BTCUSDT-PERP:LONG");
  await expect(capitalLimit).toHaveValue("900", { timeout: 15_000 });
  await expect(page.getByLabel("按交易纪律比例设定资金上限"))
    .toHaveAttribute("aria-valuenow", "100");
  await expect(page.getByTestId("direct-discipline-summary")).toContainText("最大预计亏损");
  await expect(page.getByTestId("direct-discipline-summary")).toContainText("收益 / 风险（R）");
  await expect(page.getByText("当前计划没有市场入场条件", { exact: false })).toHaveCount(0);
  await capitalLimit.scrollIntoViewIfNeeded();
  await page.screenshot({
    path: `../build/qualification/browser/direct-discipline-allowed-${testInfo.project.name}.png`,
  });

  const long = page.getByRole("button", { name: "做多", exact: true });
  const short = page.getByRole("button", { name: "做空", exact: true });
  await expect(long).toHaveAttribute("aria-pressed", "true");
  await short.click();
  await expect(short).toHaveAttribute("aria-pressed", "true");
  await expect(long).toHaveAttribute("aria-pressed", "false");
  await expect.poll(() => targetedDisciplineRequests).toContain("BTCUSDT-PERP:SHORT");

  if (testInfo.project.name === "chromium-desktop") {
    const marketContext = page.getByTestId("direct-execution-market-context");
    const planEditor = page.getByLabel("直接执行快速配置");
    const [marketBox, editorBox] = await Promise.all([
      marketContext.boundingBox(),
      planEditor.boundingBox(),
    ]);
    expect(marketBox).not.toBeNull();
    expect(editorBox).not.toBeNull();
    expect(Math.abs(marketBox!.y - editorBox!.y)).toBeLessThanOrEqual(1);
    expect(marketBox!.x + marketBox!.width).toBeLessThanOrEqual(editorBox!.x + 1);
  }

  await capitalLimit.fill("901");
  await expect(capitalLimit).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByText("超过当前纪律允许的 900 USDT。", { exact: true })).toBeVisible();
  await page.screenshot({
    path: `../build/qualification/browser/direct-discipline-exceeded-${testInfo.project.name}.png`,
  });
  await openDirectReview(page);
  await expect(page.getByRole("button", { name: "保存草稿", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "提交并启动", exact: true })).toBeDisabled();
  await testInfo.attach(`direct-discipline-cap-${testInfo.project.name}.png`, {
    body: await page.screenshot({ fullPage: true }),
    contentType: "image/png",
  });
});

test("strategy configuration applies the same targeted discipline ceiling", async ({ page }) => {
  const targetedDisciplineRequests: string[] = [];
  await routeReadyDemoExecutor(page);
  await page.route("**/api/v1/strategies", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      json: [{
        strategy_id: "E2E_DISCIPLINE_STRATEGY",
        display_name: "纪律容量验证策略",
        strategy_version: "1.0.0",
        parameter_schema_version: "1.0.0",
        supported_directions: ["LONG", "SHORT"],
        applicable_scenarios: "确定性浏览器验证",
        value_logic: "按固定边界形成策略计划。",
        execution_behavior: "由策略信号触发。",
        economic_scope: {
          allowed_plan_intents: ["VALIDATION"],
          profitability_evidence: "NO_POSITIVE_EXPECTANCY_EVIDENCE",
          evidence_limit: "仅用于纪律配置回归。",
        },
        plan_key_parameters: [],
      }],
    });
  });
  await page.route(/\/api\/v1\/overview(?:\?.*)?$/, async (route) => {
    const url = new URL(route.request().url());
    const target = `${url.searchParams.get("entry_instrument_ref") ?? ""}:${url.searchParams.get("entry_direction") ?? ""}`;
    if (target !== ":") targetedDisciplineRequests.push(target);
    await route.fulfill({
      contentType: "application/json",
      json: {
        new_risk_discipline: {
          status: "ALLOWED",
          new_risk_allowed: true,
          blocker_codes: [],
          available_notional_capacity: "900",
          max_plan_loss: "400",
          open_risk_limit: "600",
          open_risk_committed: "0",
        },
      },
    });
  });

  await page.goto("/plans/new");
  await page.getByRole("button", { name: "配置流程验证", exact: true }).click();
  const tradeAmount = page.getByLabel("交易金额（USDT）");
  await expect(tradeAmount).toHaveValue("500");
  await expect.poll(() => targetedDisciplineRequests).toContain("BTCUSDT-PERP:LONG");
  await expect(tradeAmount).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByText("超过当前交易纪律允许的 400 USDT。", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "保存计划", exact: true })).toBeDisabled();
});

test("direct execution chart keeps its fixed empty state when K-line history fails", async ({ page }) => {
  await page.route("**/api/v1/market-window?**", async (route) => {
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: { code: "MARKET_WINDOW_TEST_FAILURE" } }),
    });
  });
  await page.routeWebSocket(/\/api\/v1\/market-stream/, (socket) => {
    const observedAt = new Date().toISOString();
    socket.send(JSON.stringify({
      type: "status",
      state: "LIVE",
      source: "BINANCE_DEMO_PUBLIC",
      observed_at: observedAt,
      reason: null,
    }));
    socket.send(JSON.stringify({
      type: "quote",
      instrument_ref: "BTCUSDT-PERP",
      source: "BINANCE_DEMO_PUBLIC",
      source_cutoff: observedAt,
      received_at: observedAt,
      bid_price: "65000",
      ask_price: "65002",
      reference_price: "65001",
    }));
  });
  await page.goto("/plans/new");
  await page.getByRole("button", { name: "配置订单计划", exact: true }).click();

  const chartRegion = page.getByRole("region", { name: "15m K 线 · 草稿投影" });
  await expect(chartRegion.getByRole("group", {
    name: /订单计划 15m K 线主图/,
  })).toBeVisible();
  await expect(chartRegion.getByRole("status")).toContainText("K 线窗口读取失败");
  await expect(chartRegion.getByRole("button", { name: "重试 K 线" })).toBeVisible();
  await expect(chartRegion.getByTestId("order-schedule-chart-market-source")).toHaveCount(0);
  await expect(chartRegion.getByRole("status")).toContainText("价格预览与保存已阻断");
  await openDirectReview(page);
  await expect(page.getByRole("button", { name: "保存草稿", exact: true })).toBeDisabled();
  await expect(page.getByText(/^技术预览可保存 ·/)).toHaveCount(0);
  await chartRegion.getByText(/图线与操作点/).click();
  await expect(chartRegion.getByText("价格线显示", { exact: true })).toBeVisible();
});

test("strategy catalog failure keeps direct execution available without inventing qualification evidence", async ({ page }) => {
  await page.route("**/api/v1/strategies", async (route) => {
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: { code: "STRATEGY_CATALOG_TEST_FAILURE" } }),
    });
  });

  await page.goto("/plans/new");

  await expect(page.getByText(
    "策略列表当前不可用；仍可使用上方的直接执行订单计划。",
    { exact: true },
  )).toBeVisible();
  await expect(page.getByRole("button", { name: "配置订单计划", exact: true })).toBeEnabled();
  await expect(page.getByText(
    "当前没有通过费用后收益证据门槛的内置策略。",
    { exact: false },
  )).toHaveCount(0);
  await expect(page.getByRole("region", { name: "可用策略列表" })).toHaveCount(0);
});

test("planning and limited-control surfaces preserve authority and failure boundaries", async ({ page }, testInfo) => {
  test.slow();
  const planName = `[测试] E2E AI Donchian ${Date.now()}`;
  const attemptedControlWrites: string[] = [];
  await page.route(
    /\/api\/v1\/activations\/[^/?#]+\/(?:exit|resume|stop-new-risk|takeover|release-system-stop)(?:\?.*)?$/,
    async (route) => {
      if (route.request().method() === "POST") {
        attemptedControlWrites.push(route.request().url());
        await route.abort();
        return;
      }
      await route.continue();
    },
  );
  await page.goto("/overview");
  await expect(page).toHaveURL(/\/overview$/);
  await expect(page.getByRole("combobox", { name: "交易上下文" }))
    .toContainText("Demo");
  await expect(page.getByText("真实账户交易", { exact: false })).toHaveCount(0);
  await expect(page.getByText(/交易所更新于|等待交易所同步/)).toBeVisible();
  await assertAccessible(page, testInfo, "overview");

  const navigation = page.getByRole("navigation", { name: "工作台导航" });
  if (testInfo.project.name === "chromium-desktop") {
    await expect(page.getByRole("button", { name: "展开导航" })).toBeVisible();
    await expect(navigation.getByRole("button", { name: "总览" })).toBeVisible();
    await expect(navigation.getByText("总览", { exact: true })).toHaveCount(0);
    await assertAccessible(page, testInfo, "overview-navigation-collapsed");
    await page.getByRole("button", { name: "展开导航" }).click();
    await expect(page.getByRole("button", { name: "折叠导航" })).toBeVisible();
    await expect(navigation.getByText("总览", { exact: true })).toBeVisible();
    await page.reload();
    await expect(page.getByRole("button", { name: "折叠导航" })).toBeVisible();
    await page.getByRole("button", { name: "折叠导航" }).click();
    await expect(page.getByRole("button", { name: "展开导航" })).toBeVisible();
    await expect(navigation.getByText("总览", { exact: true })).toHaveCount(0);
  } else {
    await expect(page.getByRole("button", { name: "打开导航" })).toBeVisible();
    await page.getByRole("button", { name: "打开导航" }).click();
    await expect(navigation.getByText("总览", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: /折叠导航|展开导航/ })).toHaveCount(0);
    await navigation.getByRole("button", { name: "总览" }).click();
  }

  await page.goto("/plans/new");
  await expect(page.getByRole("heading", { name: "选择执行依据" })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "交易上下文" }))
    .toContainText("Demo");
  await expect(page.getByLabel("筛选策略")).toBeVisible();
  await expect(page.getByLabel("支持方向")).toBeVisible();
  await expect(page.getByRole("combobox", { name: "排序" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "配置策略计划" })).toHaveCount(0);
  await expect(page.getByText("当前没有通过费用后收益证据门槛的内置策略。", { exact: false })).toBeVisible();
  await page.getByLabel("筛选策略").fill("Donchian");
  await expect(page.getByText("单次 Donchian 突破与 ATR 风险退出", { exact: true })).toBeVisible();
  await expect(page.getByText("仅流程验证", { exact: true })).toBeVisible();
  await page.getByRole("combobox", { name: "排序" }).click();
  await page.getByRole("option", { name: "策略版本（新到旧）" }).click();
  await page.getByRole("button", { name: /展开.*策略介绍/ }).click();
  await expect(page.getByText("策略逻辑", { exact: true })).toBeVisible();
  await assertAccessible(page, testInfo, "strategy-selection");
  await page.getByRole("button", { name: "配置流程验证" }).click();
  await expect(page.getByRole("heading", { name: "配置策略计划" })).toBeVisible();
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
  let strategyChart = page.getByRole("group", {
    name: /策略输入 15m K 线主图/,
  });
  await expect(strategyChart).toBeVisible();
  await expect(page.getByText("当前策略关键价格", { exact: true })).toBeVisible();
  await page.getByText(/策略关键价格 · 价格线/).click();
  await expect(page.getByText("做多突破线", { exact: true })).toBeVisible();
  await expect(page.getByText("做空突破线", { exact: true })).toBeVisible();
  await expect(page.getByText("做多最大追价", { exact: true })).toBeVisible();
  if (testInfo.project.name === "chromium-narrow") {
    await page.getByLabel("K 线周期").click();
    await page.getByRole("option", { name: "4h" }).click();
  } else {
    await page
      .getByRole("group", { name: "K 线周期" })
      .getByRole("button", { name: "4h" })
      .click();
  }
  strategyChart = page.getByRole("group", {
    name: /策略输入 4h K 线主图/,
  });
  await expect(strategyChart).toBeVisible();
  await expect(page.getByRole("button", { name: "保存计划" })).toBeVisible();
  await expect(page.getByRole("button", { name: "重新选择策略" })).toBeVisible();
  await expect(page.getByText("收益证据未支持：", { exact: true })).toBeVisible();
  await expect(page.getByLabel("计划名称")).toHaveValue(/^\[测试\] /);
  await page.getByLabel("计划名称").fill(planName);
  await page.getByRole("combobox", { name: "创建方式" }).click();
  await page.getByRole("option", { name: "AI 创建" }).click();
  await expect(page.getByLabel("交易对象")).toHaveValue("BTCUSDT-PERP");
  await expect(page.getByLabel("交易金额（USDT）")).toHaveValue("500");
  await expect(page.getByText("高级策略参数（可保持默认）")).toBeVisible();
  await assertAccessible(page, testInfo, "new-plan");
  await testInfo.attach("new-plan.png", {
    body: await page.screenshot({ fullPage: true }),
    contentType: "image/png",
  });
  await page.getByRole("button", { name: "保存计划" }).click();
  await expect(page).toHaveURL(/\/plans$/);
  await expect(page.getByRole("heading", { name: "交易计划" })).toBeVisible();
  await expect(page.getByRole("tab", { name: /当前计划/ })).toBeVisible();
  await expect(page.getByRole("tab", { name: /历史计划/ })).toBeVisible();
  await expect(page.getByText("了解当前可用策略", { exact: false })).toHaveCount(0);
  const planCard = page.getByRole("article", { name: `计划 ${planName}` });
  await expect(planCard.getByRole("heading", { name: planName })).toBeVisible();
  await expect(planCard.getByText(/AI 创建 · 创建于 .* UTC\+8/)).toBeVisible();
  await expect(planCard.getByText("BTCUSDT-PERP", { exact: false })).toBeVisible();
  await planCard.getByText("计划配置", { exact: true }).click();
  await expect(planCard.getByText("交易金额", { exact: true })).toBeVisible();
  await expect(planCard.getByText("500.00 USDT", { exact: true }).first()).toBeVisible();
  await expect(planCard.getByText("15m 通道回看", { exact: true })).toBeVisible();
  await expect(planCard.getByText("初始止损", { exact: true })).toBeVisible();
  await expect(page.getByText("策略逻辑", { exact: true })).toHaveCount(0);
  await planCard.getByRole("button", { name: "删除草稿" }).click();
  const deleteDialog = page.getByRole("dialog", { name: "删除草稿？" });
  await expect(deleteDialog.getByText(planName, { exact: false })).toBeVisible();
  await deleteDialog.getByRole("button", { name: "取消" }).click();
  await expect(planCard).toBeVisible();
  await planCard.getByRole("button", { name: "删除草稿" }).click();
  await deleteDialog.getByRole("button", { name: "删除草稿" }).click();
  await expect(planCard).toHaveCount(0);

  await page.goto("/operations");
  await expect(page.getByRole("heading", { name: "故障接管" })).toBeVisible();
  await expect(page.locator(".statusbar .env")).toHaveText("DEMO");
  await expect(page.getByRole("link", { name: "打开 Binance 官方入口" })).toBeVisible();
  const activation = page.locator("article.activation").filter({ hasText: "WRITER_CONTINUITY_LOST" }).first();
  if (await activation.count() === 0) {
    test.info().annotations.push({
      type: "coverage-gap",
      description: "当前运行库没有 WRITER_CONTINUITY_LOST 激活；保留计划流程结果，跳过依赖该状态的故障接管演练。",
    });
    return;
  }
  await expect(activation).toBeVisible();
  const activationId = await activation.getAttribute("data-activation-id");
  expect(activationId).toBeTruthy();
  await expect(activation.getByText("自动执行已暂停", { exact: true }).first()).toBeVisible();
  await expect(activation.getByText("恢复激活", { exact: false })).toHaveCount(0);
  await assertAccessible(page, testInfo, "operations-before");

  const stopControl = activation.locator(".control").filter({ hasText: "停止新增风险" });
  const stopPreviewButton = stopControl.getByRole("button", { name: "查看后果" });
  if (!await stopPreviewButton.isEnabled()) {
    test.info().annotations.push({
      type: "coverage-gap",
      description: "当前 WRITER_CONTINUITY_LOST 激活已处于停止新增风险状态；该状态不再提供重复预览控制。",
    });
    return;
  }
  await stopPreviewButton.click();
  const dialog = page.getByRole("dialog", { name: "确认故障控制" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("停止新增风险", { exact: true })).toBeVisible();
  await expect(dialog.getByText("只停止新的开仓和加仓", { exact: false })).toBeVisible();
  await assertAccessible(page, testInfo, "stop-preview");
  await dialog.getByRole("button", { name: "取消" }).click();

  const exitControl = activation.locator(".control").filter({ hasText: "退出策略" });
  await exitControl.getByRole("button", { name: "查看后果" }).click();
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: "确认退出策略" })).toBeVisible();
  await dialog.getByRole("button", { name: "取消" }).click();
  await expect(page.locator(`article.activation[data-activation-id="${activationId}"]`)).toBeVisible();
  expect(
    attemptedControlWrites,
    "浏览器回归只能检查控制后果，不得操作当前 Demo 或实盘计划",
  ).toEqual([]);
  await assertAccessible(page, testInfo, "operations-after-preview");
  await testInfo.attach("operations-after-preview.png", {
    body: await page.screenshot({ fullPage: true }),
    contentType: "image/png",
  });

  const layout = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
    offenders: [...document.querySelectorAll<HTMLElement>("body *")]
      .filter((element) => !element.closest(".table-scroll"))
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
  await testInfo.attach("operations-layout.json", {
    body: Buffer.from(JSON.stringify(layout, null, 2)),
    contentType: "application/json",
  });
  expect(layout.offenders).toEqual([]);
  expect(layout.scrollWidth).toBe(layout.clientWidth);
});
