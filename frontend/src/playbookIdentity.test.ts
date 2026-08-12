import { describe, expect, it } from "vitest";

import { isValidPlaybookRef, normalizePlaybookRef } from "./playbookIdentity";


describe("playbook identity", () => {
  it("normalizes surrounding whitespace without changing the stable identity", () => {
    expect(normalizePlaybookRef("  BTC_BREAKOUT_V1  ")).toBe("BTC_BREAKOUT_V1");
  });

  it.each([
    "BTC_BREAKOUT_V1",
    "monitor:btc-breakout:v2",
    "pullback.long-15m",
  ])("accepts a reusable machine-readable reference: %s", (value) => {
    expect(isValidPlaybookRef(value)).toBe(true);
  });

  it.each([
    "",
    "BTC BREAKOUT V1",
    "逐笔信号",
    "-leading-separator",
    `A${"x".repeat(96)}`,
  ])("rejects an ambiguous or unstable reference: %s", (value) => {
    expect(isValidPlaybookRef(value)).toBe(false);
  });
});
