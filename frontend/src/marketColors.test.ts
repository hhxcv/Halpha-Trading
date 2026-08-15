import { describe, expect, it } from "vitest";

import {
  financialToneClassName,
  financialToneForSignedValue,
  marketToneForDirection,
} from "./marketColors";

describe("market color semantics", () => {
  it("maps long and short to market movement without choosing red or green", () => {
    expect(marketToneForDirection("LONG")).toBe("up");
    expect(marketToneForDirection("SHORT")).toBe("down");
    expect(marketToneForDirection("UNKNOWN")).toBeUndefined();
  });

  it("keeps zero and unknown neutral while preserving signed direction", () => {
    expect(financialToneForSignedValue("1.25")).toBe("profit");
    expect(financialToneForSignedValue("-0.1")).toBe("loss");
    expect(financialToneForSignedValue("0")).toBeUndefined();
    expect(financialToneForSignedValue("UNKNOWN")).toBeUndefined();
  });

  it("maps economic signs onto the active K-line palette classes", () => {
    expect(financialToneClassName(financialToneForSignedValue("1.25"))).toBe("market-tone-up");
    expect(financialToneClassName(financialToneForSignedValue("-0.1"))).toBe("market-tone-down");
  });
});
