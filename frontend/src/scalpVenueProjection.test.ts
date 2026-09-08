import { describe, expect, it } from "vitest";

import { scalpVenueProjection } from "./scalpVenueProjection";

const action = (overrides: Record<string, unknown>) => ({
  execution_action_id: "entry-1",
  action_kind: "ENTRY",
  state: "OPEN",
  action_terms: { price: "100" },
  ...overrides,
});

const fact = (overrides: Record<string, unknown>) => ({
  venue_fact_id: "fact-1",
  kind: "ORDER_STATE",
  action_ref: "entry-1",
  cutoff: "2026-08-16T08:00:00Z",
  payload: { status: "NEW" },
  ...overrides,
});

describe("scalping venue projection", () => {
  it("does not call a local OPEN action an exchange order without a venue fact", () => {
    const projection = scalpVenueProjection({
      execution_actions: [action({ action_terms: { price: "100" } })],
      venue_facts: [],
    } as never);

    expect(projection.status).toBe("PENDING_VENUE");
    expect(projection.priceAnnotations).toHaveLength(0);
  });

  it("shows a working order line only after the exchange confirms it", () => {
    const projection = scalpVenueProjection({
      execution_actions: [action({ action_terms: { price: "100" } })],
      venue_facts: [fact({})],
    } as never);

    expect(projection).toMatchObject({ status: "WORKING", label: "交易所已挂单" });
    expect(projection.priceAnnotations).toMatchObject([
      { label: "交易所挂单", price: 100, authority: "SERVER_FACT" },
    ]);
  });

  it("uses confirmed fills for cost and working protection orders for stop and target", () => {
    const projection = scalpVenueProjection({
      execution_actions: [
        action({ action_terms: {} }),
        action({
          execution_action_id: "stop-1",
          action_kind: "PROTECTION",
          action_terms: { trigger_price: "98" },
        }),
        action({
          execution_action_id: "target-1",
          action_kind: "TAKE_PROFIT",
          action_terms: { trigger_price: "103" },
        }),
      ],
      venue_facts: [
        fact({
          venue_fact_id: "entry-fill",
          kind: "FILL",
          payload: { trade_id: "trade-1", last_price: "100", last_quantity: "0.3", leaves_quantity: "0" },
        }),
        fact({ venue_fact_id: "stop-working", action_ref: "stop-1" }),
        fact({ venue_fact_id: "target-working", action_ref: "target-1" }),
      ],
    } as never);

    expect(projection).toMatchObject({ status: "FILLED", label: "已成交" });
    expect(projection.priceAnnotations).toMatchObject([
      { label: "成交成本", price: 100, authority: "SERVER_FACT" },
      { label: "交易所止损", price: 98, authority: "SERVER_FACT" },
      { label: "交易所止盈", price: 103, authority: "SERVER_FACT" },
    ]);
  });

  it("keeps an unknown exchange outcome explicit", () => {
    const projection = scalpVenueProjection({
      execution_actions: [action({ state: "UNKNOWN" })],
      venue_facts: [],
    } as never);

    expect(projection).toMatchObject({ status: "UNKNOWN", label: "交易所结果待确认" });
  });

  it("uses the latest fill rather than keeping a historical partial fill forever", () => {
    const partial = fact({
      venue_fact_id: "partial", kind: "FILL", source_time: "2026-08-16T08:00:00Z",
      payload: { trade_id: "trade-1", last_price: "100", last_quantity: "0.3", leaves_quantity: "0.7" },
    });
    const full = fact({
      venue_fact_id: "full", kind: "FILL", source_time: "2026-08-16T08:00:02Z",
      payload: { trade_id: "trade-2", last_price: "102", last_quantity: "0.7", leaves_quantity: "0" },
    });
    expect(scalpVenueProjection({
      execution_actions: [action({})], venue_facts: [partial],
    } as never).status).toBe("PARTIALLY_FILLED");
    const complete = scalpVenueProjection({
      execution_actions: [action({})], venue_facts: [full, partial],
    } as never);
    expect(complete.status).toBe("FILLED");
    expect(complete.priceAnnotations.find((line) => line.role === "RUNTIME_ENTRY")?.price)
      .toBeCloseTo(101.4);
  });

  it("uses a confirmed filled order state but does not call a cancelled partial order fully filled", () => {
    const partial = fact({
      kind: "FILL",
      payload: { trade_id: "trade-1", last_price: "100", last_quantity: "0.3", leaves_quantity: "0.7" },
    });
    const project = (status: string) => scalpVenueProjection({
      execution_actions: [action({})],
      venue_facts: [partial, fact({
        venue_fact_id: "terminal", source_time: "2026-08-16T08:00:02Z", payload: { status },
      })],
    } as never);
    expect(project("FILLED").status).toBe("FILLED");
    expect(project("CANCELED").status).toBe("PARTIALLY_FILLED");
  });

  it("does not present cancelled protection as pending after a completed cycle", () => {
    const projection = scalpVenueProjection({
      activation: { lifecycle: "COMPLETED" },
      execution_actions: [action({ state: "CLOSED", action_terms: {} })],
      venue_facts: [fact({
        kind: "FILL",
        payload: { trade_id: "trade-1", last_price: "100", last_quantity: "0.3", leaves_quantity: "0" },
      })],
    } as never);

    expect(projection).toMatchObject({ status: "CLOSED", label: "周期已结束" });
  });
});
