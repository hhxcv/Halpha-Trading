import { describe, expect, it } from "vitest";

import {
  sequenceEvidencePercent,
  sequenceEvidenceR,
  sequenceEvidenceUsdt,
} from "./ReviewSequenceEvidencePanel";


describe("review sequence evidence formatting", () => {
  it("keeps missing accounting and path evidence unknown", () => {
    expect(sequenceEvidenceUsdt(null)).toBe("未知");
    expect(sequenceEvidencePercent(null)).toBe("未知");
    expect(sequenceEvidenceR(null)).toBe("未知");
  });

  it("preserves measured zero and directional signs", () => {
    expect(sequenceEvidenceUsdt("0")).toBe("0.00 USDT");
    expect(sequenceEvidenceUsdt("-2.5")).toBe("-2.50 USDT");
    expect(sequenceEvidencePercent("1.25")).toBe("+1.25%");
    expect(sequenceEvidenceR("0")).toBe("0.00R");
  });
});
