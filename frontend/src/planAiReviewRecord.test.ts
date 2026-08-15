import { describe, expect, it } from "vitest";

import { planAiReviewRecordPresentation } from "./planAiReviewRecord";

describe("plan AI review record presentation", () => {
  it("presents the fixed approval and its saved reviewer configuration", () => {
    expect(planAiReviewRecordPresentation({
      status: "APPROVED",
      decision: "APPROVE",
      reason: "保护与退出边界完整。",
      suggestions: ["按固定止损执行。"],
      completed_at: "2026-08-13T14:00:00+00:00",
      market_source_cutoff: "2026-08-13T13:59:00+00:00",
      configuration: {
        model: "gpt-5.6-terra",
        reasoning_effort: "medium",
      },
    })).toMatchObject({
      status: "APPROVED",
      label: "AI 审核结论：批准",
      tone: "success",
      configurationLabel: "GPT-5.6 Terra · 中等",
    });
  });

  it("keeps an AI rejection distinct from a record that did not form a conclusion", () => {
    expect(planAiReviewRecordPresentation({
      status: "REJECTED",
      decision: "REJECT",
    }).label).toBe("AI 审核结论：拒绝");
    expect(planAiReviewRecordPresentation({
      status: "FAILED",
    }).label).toBe("AI 审核未完成");
  });

  it("does not silently turn a missing frozen reference into an approval", () => {
    expect(planAiReviewRecordPresentation(null).status).toBe("MISSING");
    expect(planAiReviewRecordPresentation(null, "review-001").status).toBe("UNAVAILABLE");
  });
});
