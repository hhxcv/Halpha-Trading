import { describe, expect, it } from "vitest";

import {
  accountObservationBlockerText,
  maxPlanLossFractionNote,
  newRiskDisciplineBlockerCodesFromFailureCode,
  newRiskDisciplineRecoveryGuidance,
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

describe("newRiskDisciplineRecoveryGuidance", () => {
  it("labels the daily window boundary as an earliest recheck, not a promised recovery", () => {
    const text = newRiskDisciplineRecoveryGuidance({
      blocker_codes: ["NEW_RISK_DAILY_LOSS_STOP_REACHED"],
      day_window_started_at: "2026-08-12T16:00:00Z",
    }).map((item) => item.text).join("\n");

    expect(text).toContain("2026-08-14 00:00:00");
    expect(text).toContain("不是恢复承诺");
  });

  it("does not invent a recovery time for a floating-loss add block", () => {
    const text = newRiskDisciplineRecoveryGuidance({
      blocker_codes: ["NEW_RISK_LOSING_POSITION_ADD_PROHIBITED"],
    }).map((item) => item.text).join("\n");

    expect(text).toContain("不再浮亏或已不存在");
    expect(text).toContain("没有按时间自动恢复");
  });
});

describe("newRiskDisciplineBlockerCodesFromFailureCode", () => {
  it("keeps only known discipline codes from a compound server rejection", () => {
    expect(newRiskDisciplineBlockerCodesFromFailureCode(
      "NEW_RISK_DAILY_LOSS_STOP_REACHED;UNRELATED_FAILURE",
    )).toEqual(["NEW_RISK_DAILY_LOSS_STOP_REACHED"]);
  });
});

describe("accountObservationBlockerText", () => {
  it("does not call a stale snapshot a network problem without an observed cause", () => {
    expect(accountObservationBlockerText({
      account_snapshot_status: "STALE",
      account_snapshot_age_seconds: 91,
      account_observation_failure_code: null,
    } as never)).toBe("账户权益快照未刷新（91 秒）");
  });

  it("names an observed connection failure precisely", () => {
    expect(accountObservationBlockerText({
      account_snapshot_status: "STALE",
      account_snapshot_age_seconds: 91,
      account_observation_failure_code: "ACCOUNT_SNAPSHOT_QUERY_FAILED_OSERROR",
    } as never)).toBe("交易所账户查询连接失败");
  });

  it("does not mislabel an exchange HTTP response error as a network failure", () => {
    expect(accountObservationBlockerText({
      account_snapshot_status: "STALE",
      account_snapshot_age_seconds: 91,
      account_observation_failure_code: "ACCOUNT_SNAPSHOT_QUERY_FAILED_HTTPERROR",
    } as never)).toBe("交易所账户查询返回错误");
  });
});
