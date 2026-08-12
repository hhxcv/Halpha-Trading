import { describe, expect, it } from "vitest";

import {
  decisionEvidenceCurrentStreak,
  decisionEvidencePercent,
  decisionEvidenceR,
  decisionEvidenceRatio,
  decisionEvidenceUsdt,
} from "./DecisionEvidencePanel";


describe("decision evidence value formatting", () => {
  it("keeps unavailable metrics unknown instead of coercing null to zero", () => {
    expect(decisionEvidenceUsdt(null)).toBe("未知");
    expect(decisionEvidencePercent(null)).toBe("未知");
    expect(decisionEvidenceRatio(null)).toBe("未知");
    expect(decisionEvidenceR(null)).toBe("未知");
  });

  it("renders exact zero only when the API actually returned zero", () => {
    expect(decisionEvidenceUsdt("0")).toBe("0.00 USDT");
    expect(decisionEvidencePercent("0")).toBe("0.00%");
    expect(decisionEvidenceRatio("0")).toBe("0.00 : 1");
    expect(decisionEvidenceR("0")).toBe("0.00R");
  });

  it("renders the current exact-cohort run without implying persistence", () => {
    expect(decisionEvidenceCurrentStreak("LOSS", 3)).toBe("连亏 3 笔");
    expect(decisionEvidenceCurrentStreak(null, 0)).toBe("无可比交易");
  });
});
