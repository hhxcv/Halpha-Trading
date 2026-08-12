import { describe, expect, it } from "vitest";

import {
  reviewPricePathPercent,
  reviewPricePathPrice,
  reviewPricePathR,
} from "./ReviewPricePathEvidencePanel";


describe("review price-path formatting", () => {
  it("keeps unavailable evidence unknown instead of coercing null to zero", () => {
    expect(reviewPricePathPercent(null)).toBe("未知");
    expect(reviewPricePathPrice(null)).toBe("未知");
    expect(reviewPricePathR(null)).toBe("未知");
  });

  it("preserves actual zero excursion as a measured value", () => {
    expect(reviewPricePathPercent("0")).toBe("0.00%");
    expect(reviewPricePathR("0")).toBe("0.00R");
  });
});
