import { describe, expect, it } from "vitest";

import {
  DEFAULT_SCALP_TEMPLATE,
  buildScalpScheduleSpec,
  readScalpAnchor,
  readScalpTemplate,
  saveScalpAnchor,
  saveScalpTemplate,
  validateScalpTemplate,
} from "./scalpingWorkbench";

class MemoryStorage {
  private readonly values = new Map<string, string>();

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }
}

describe("scalping template preferences", () => {
  it("keeps templates isolated by environment and account", () => {
    const storage = new MemoryStorage();
    const personal = { ...DEFAULT_SCALP_TEMPLATE, notional: "250" };

    expect(saveScalpTemplate("live", "personal", personal, storage)).toBe(true);
    expect(readScalpTemplate("live", "personal", storage)?.notional).toBe("250");
    expect(readScalpTemplate("live", "copy", storage)).toBeNull();
    expect(readScalpTemplate("demo", "personal", storage)).toBeNull();
  });

  it("keeps first visits and unavailable storage explicitly unsaved", () => {
    expect(readScalpTemplate("demo", "account", new MemoryStorage())).toBeNull();
    expect(readScalpTemplate("demo", "account", null)).toBeNull();
    expect(saveScalpTemplate("demo", "account", DEFAULT_SCALP_TEMPLATE, null)).toBe(false);
  });

  it.each([
    "{broken",
    JSON.stringify({ version: 1, template: { ...DEFAULT_SCALP_TEMPLATE, initial_stop_bps: "0" } }),
    JSON.stringify({ version: 1, template: { ...DEFAULT_SCALP_TEMPLATE, entry_mode: "OTHER" } }),
  ])("does not treat corrupt or invalid stored content as a saved template: %s", (raw) => {
    const storage = new MemoryStorage();
    storage.setItem("halpha.scalp-template.v1:demo:account", raw);

    expect(readScalpTemplate("demo", "account", storage)).toBeNull();
  });

  it("keeps a previously saved template's legacy market mode", () => {
    const storage = new MemoryStorage();
    storage.setItem("halpha.scalp-template.v1:demo:account", JSON.stringify({
      version: 1,
      template: { ...DEFAULT_SCALP_TEMPLATE, entry_mode: undefined },
    }));
    expect(readScalpTemplate("demo", "account", storage)).toEqual(DEFAULT_SCALP_TEMPLATE);
  });

  it("persists only a valid explicit from-now anchor", () => {
    const storage = new MemoryStorage();
    const anchor = "2026-08-16T08:00:00.000Z";

    saveScalpAnchor("demo", "account", anchor, storage);
    saveScalpAnchor("demo", "other", "not-a-time", storage);

    expect(readScalpAnchor("demo", "account", storage)).toBe(anchor);
    expect(readScalpAnchor("demo", "other", storage)).toBeNull();
  });
});

describe("scalping schedule projection", () => {
  it("builds the same one-shot protected market shape as the server", () => {
    const template = {
      ...DEFAULT_SCALP_TEMPLATE,
      notional: "321",
      initial_stop_bps: "42",
      take_profit_r: "1.7",
      max_holding_seconds: 150,
      max_spread_bps: "3.5",
    };

    const spec = buildScalpScheduleSpec(template);

    expect(spec.entry_program?.kind).toBe("ONE_TIME");
    expect(spec.venue_policy.order_type).toBe("MARKET");
    expect(spec.submission_mode).toBe("SERIAL_PROTECTED");
    expect(spec.amount_distribution.base_notional).toBe("321");
    expect(spec.entry_conditions.items).toEqual([
      { kind: "DECISION_BASIS_READY" },
      { kind: "SPREAD_BPS", maximum_bps: "3.5" },
    ]);
    expect(spec.protection_policy?.initial_stop.distance_bps).toBe("42");
    expect(spec.protection_policy?.take_profit_ladder?.levels).toEqual([
      { trigger_r: "1.7", quantity_fraction: "1" },
    ]);
    expect(spec.protection_policy?.time_exit_seconds).toBe(150);
  });

  it("builds a same-side price-one maker-only limit entry", () => {
    const spec = buildScalpScheduleSpec(
      { ...DEFAULT_SCALP_TEMPLATE, entry_mode: "MAKER_ONLY_SAME_SIDE" },
      "99.5",
    );

    expect(spec.price_distribution).toEqual({ kind: "SINGLE", limit_price: "99.5" });
    expect(spec.venue_policy).toMatchObject({
      order_type: "LIMIT",
      time_in_force: "GTC",
      post_only: true,
    });
  });

  it("blocks empty, out-of-range, or non-integer values", () => {
    expect(validateScalpTemplate({
      ...DEFAULT_SCALP_TEMPLATE,
      notional: "",
      initial_stop_bps: "5001",
      take_profit_r: "0.01",
      max_holding_seconds: 30.5,
    })).toMatchObject({
      notional: expect.any(String),
      initial_stop_bps: expect.any(String),
      take_profit_r: expect.any(String),
      max_holding_seconds: expect.any(String),
    });
  });
});
