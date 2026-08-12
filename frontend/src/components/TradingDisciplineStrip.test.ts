import { describe, expect, it } from "vitest";

import {
  maxPlanLossFractionNote,
  tradingAccessNotice,
} from "./TradingDisciplineStrip";


describe("tradingAccessNotice", () => {
  it("does not present a live read-only account as mutation-ready", () => {
    expect(tradingAccessNotice({
      environment_kind: "LIVE",
      profile: "BINANCE_LIVE_READ_ONLY",
      runtime_real_write_gate: "CLOSED",
    })).toContain("实盘只读观察");
  });

  it("distinguishes a closed live write gate from risk-discipline capacity", () => {
    expect(tradingAccessNotice({
      environment_kind: "LIVE",
      profile: "BINANCE_LIVE_WRITE",
      runtime_real_write_gate: "CLOSED",
    })).toContain("实盘写门关闭");
  });

  it("does not treat the Demo gate field as a live-write blocker", () => {
    expect(tradingAccessNotice({
      environment_kind: "DEMO",
      profile: "BINANCE_DEMO",
      runtime_real_write_gate: "CLOSED",
    })).toBeNull();
  });
});

describe("maxPlanLossFractionNote", () => {
  it("derives the displayed cap from the current discipline facts", () => {
    expect(maxPlanLossFractionNote("1000", "7.5"))
      .toBe("当前上限为纪律权益的 0.75%");
  });

  it("does not invent a percentage when the equity basis is unavailable", () => {
    expect(maxPlanLossFractionNote(null, null))
      .toBe("由当前环境配置和纪律权益计算");
  });
});
