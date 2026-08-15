export type PlanAiReviewRecordPresentation = {
  status: "APPROVED" | "REJECTED" | "FAILED" | "PENDING" | "UNAVAILABLE" | "MISSING";
  label: string;
  tone: "success" | "error" | "warning" | "default";
  reason: string | null;
  suggestions: string[];
  completedAt: string | null;
  marketSourceCutoff: string | null;
  configurationLabel: string | null;
};

function recordOf(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function textOf(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0
    ? value.trim()
    : null;
}

const reviewerModelLabels: Record<string, string> = {
  "gpt-5.6-terra": "GPT-5.6 Terra",
  "gpt-5.6-sol": "GPT-5.6 Sol",
  "gpt-5.6-luna": "GPT-5.6 Luna",
};

const reasoningEffortLabels: Record<string, string> = {
  low: "低",
  medium: "中等",
  high: "高",
  xhigh: "很高",
  max: "最大",
  ultra: "极限",
};

function configurationLabel(review: Record<string, unknown>): string | null {
  const configuration = recordOf(review.configuration);
  const model = textOf(configuration.model);
  const effort = textOf(configuration.reasoning_effort);
  if (!model && !effort) return null;
  return [
    model ? reviewerModelLabels[model] ?? model : null,
    effort ? reasoningEffortLabels[effort] ?? effort : null,
  ].filter((value): value is string => Boolean(value)).join(" · ");
}

/**
 * Normalize one immutable, version-bound review for read-only plan surfaces.
 * This deliberately has no "latest review" lookup: the supplied record must
 * be the review reference frozen with the plan version.
 */
export function planAiReviewRecordPresentation(
  review: unknown,
  reviewRef: string | null | undefined = null,
): PlanAiReviewRecordPresentation {
  const source = recordOf(review);
  if (Object.keys(source).length === 0) {
    return reviewRef
      ? {
        status: "UNAVAILABLE",
        label: "AI 审核记录暂不可读",
        tone: "warning",
        reason: null,
        suggestions: [],
        completedAt: null,
        marketSourceCutoff: null,
        configurationLabel: null,
      }
      : {
        status: "MISSING",
        label: "未保留 AI 审核记录",
        tone: "warning",
        reason: null,
        suggestions: [],
        completedAt: null,
        marketSourceCutoff: null,
        configurationLabel: null,
      };
  }

  const status = textOf(source.status)?.toUpperCase() ?? "";
  const decision = textOf(source.decision)?.toUpperCase() ?? "";
  const suggestions = Array.isArray(source.suggestions)
    ? source.suggestions
      .map(textOf)
      .filter((item): item is string => item !== null)
    : [];
  const shared = {
    reason: textOf(source.reason),
    suggestions,
    completedAt: textOf(source.completed_at),
    marketSourceCutoff: textOf(source.market_source_cutoff),
    configurationLabel: configurationLabel(source),
  };

  if (status === "APPROVED" && decision === "APPROVE") {
    return { status: "APPROVED", label: "AI 审核结论：批准", tone: "success", ...shared };
  }
  if (status === "REJECTED" && decision === "REJECT") {
    return { status: "REJECTED", label: "AI 审核结论：拒绝", tone: "error", ...shared };
  }
  if (status === "FAILED") {
    return { status: "FAILED", label: "AI 审核未完成", tone: "error", ...shared };
  }
  return { status: "PENDING", label: "AI 审核未形成结论", tone: "warning", ...shared };
}
