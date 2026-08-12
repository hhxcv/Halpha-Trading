import { describe, expect, it } from "vitest";

import {
  liveProfitQualificationPresentation,
  liveProfitQualificationSatisfied,
  qualificationFractionPercent,
} from "./liveProfitQualification";


const qualification = {
  status: "ELIGIBLE_INPUT",
  required: true,
  eligible_input: true,
  blocker_codes: [],
} as const;

describe("live profit qualification", () => {
  it("treats only a verified required input as satisfied", () => {
    expect(liveProfitQualificationSatisfied(qualification as never)).toBe(true);
    expect(liveProfitQualificationSatisfied({
      ...qualification,
      status: "STALE",
      eligible_input: false,
    } as never)).toBe(false);
    expect(liveProfitQualificationSatisfied(undefined)).toBe(false);
  });

  it("presents eligible and stale states distinctly", () => {
    expect(liveProfitQualificationPresentation(qualification as never)).toEqual({
      label: "Demo 证据输入已核验",
      severity: "success",
    });
    expect(liveProfitQualificationPresentation({
      ...qualification,
      status: "STALE",
      eligible_input: false,
    } as never).severity).toBe("warning");
  });

  it("renders the account-risk fractions as percentages", () => {
    expect(qualificationFractionPercent("0.0075")).toBe("0.75%");
    expect(qualificationFractionPercent("0.015")).toBe("1.5%");
  });
});
