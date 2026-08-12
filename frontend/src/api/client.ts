import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

export type Overview = components["schemas"]["OverviewResponse"];
export type AccountPositionOperationPreviewPayload = components["schemas"]["AccountPositionOperationPreviewPayload"];
export type AccountPositionOperationPreview = components["schemas"]["AccountPositionOperationPreviewResponse"];
export type SettingsStatus = components["schemas"]["SettingsStatusResponse"];
export type MarketContext = components["schemas"]["MarketContext"];
export type MarketWindow = components["schemas"]["MarketWindow"];
export type MarketInterval = MarketWindow["interval"];
export type MarketWindowPurpose = "EXECUTION_REVIEW";
export type PlanCreatePayload = components["schemas"]["PlanCreatePayload"];
export type PlanDraftPayload = components["schemas"]["PlanDraftPayload"];
export type DraftDecisionBasis = components["schemas"]["DraftDecisionBasis"];
export type ActivationPayload = components["schemas"]["ActivationPayload"];
export type ControlPayload = components["schemas"]["ControlPayload"];
export type SystemStopReleasePayload = components["schemas"]["SystemStopReleasePayload"];
export type ReviewCompletionPayload = components["schemas"]["ReviewCompletionPayload"];
export type StageReviewCreatePayload = components["schemas"]["StageReviewCreatePayload"];
export type DecisionEvidencePreviewPayload = components["schemas"]["DecisionEvidencePreviewPayload"];
export type PlaybookQualificationExportPayload = components["schemas"]["PlaybookQualificationExportPayload"];

export type OrderScheduleDirection = components["schemas"]["Direction"];
export type OrderScheduleEntryProgram = components["schemas"]["EntryProgram-Output"];
export type OrderSchedulePriceMatch = components["schemas"]["BinancePriceMatch"];
export type OrderScheduleCondition = components["schemas"]["ConditionGroup-Output"]["items"][number];
export type OrderScheduleProtectionPolicy = components["schemas"]["ProtectionPolicy-Output"];
export type OrderScheduleTransportSpec = components["schemas"]["OrderScheduleSpec-Output"];
export type OrderScheduleSpec = Omit<
  OrderScheduleTransportSpec,
  "entry_program" | "protection_policy"
> & {
  entry_program: OrderScheduleEntryProgram;
  protection_policy: OrderScheduleProtectionPolicy;
};
export type OrderScheduleDynamicRule = OrderScheduleSpec["dynamic_rules"][number];
export type OrderSchedulePreviewPayload = components["schemas"]["OrderSchedulePreviewPayload"];
export type OrderSchedulePreview = components["schemas"]["OrderSchedulePreview"];
export type OrderSchedulePreviewLeg = OrderSchedulePreview["normalized_legs"][number];
export type OrderScheduleFullFillProtectionEstimate = NonNullable<
  OrderSchedulePreview["full_fill_protection_estimate"]
>;
export type OrderSchedulePreviewIssue = OrderSchedulePreview["issues"][number];

export type PlanKeyParameterDefinition = components["schemas"]["PlanKeyParameterDefinition"];
export type StrategySummary = components["schemas"]["StrategySummaryResponse"];
export type PlanSummary = components["schemas"]["PlanSummaryResponse"];
export type PlanDraft = components["schemas"]["TradePlanDraft"];
export type ActivationSummary = components["schemas"]["ActivationSummaryResponse"];
export type ActivationCreateResult = components["schemas"]["ActivationCreateResponse"];
export type ActivationDetail = components["schemas"]["ActivationDetailResponse"];
export type ActivationPreview = components["schemas"]["ActivationPreviewResponse"];
export type ActivationTimelineEntry = components["schemas"]["ActivationTimelineEntryResponse"];
export type ControlPreview = components["schemas"]["ControlPreviewResponse"];
export type SystemStopReleasePreview = components["schemas"]["SystemStopReleasePreviewResponse"];
export type SystemStopRelease = components["schemas"]["SystemStopReleaseResponse"];
export type TestEmailResult = components["schemas"]["TestEmailResponse"];
export type PlanDeleteResult = components["schemas"]["PlanDeleteResponse"];
export type PlanVersion = components["schemas"]["TradePlanVersion"];
export type Receipt = components["schemas"]["ReceiptResponse"];
export type Review = components["schemas"]["ReviewResponse"];
export type ExecutionFeeEvidence = components["schemas"]["ExecutionFeeEvidenceResponse"];
export type DecisionEvidence = components["schemas"]["DecisionEvidenceResponse"];
export type PlaybookQualificationExport = components["schemas"]["PlaybookQualificationExportResponse"];
export type LiveProfitQualification = components["schemas"]["LiveProfitQualificationStatus"];
export type ReviewPricePathEvidence = components["schemas"]["ReviewPricePathEvidenceResponse"];
export type ReviewPricePathInterval = ReviewPricePathEvidence["interval"];
export type ReviewSequenceEvidence = components["schemas"]["ReviewSequenceEvidenceResponse"];
export type ReviewSequenceScope = ReviewSequenceEvidence["scope"];
export type ReviewDocument = components["schemas"]["Review"];
export type ReviewHistory = components["schemas"]["ReviewHistoryResponse"];
export type ReviewCompletion = components["schemas"]["ReviewCompletionResponse"];
export type StageReview = components["schemas"]["StageReview"];
export class ApiFailure extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: unknown;

  constructor(status: number, code: string, detail: unknown = null) {
    super(code);
    this.name = "ApiFailure";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

export function isUnknownMutationResult(error: unknown): boolean {
  return !(error instanceof ApiFailure)
    || error.status < 400
    || error.status === 408
    || error.status >= 500;
}

const api = createClient<paths>({
  baseUrl: "",
  credentials: "same-origin",
  headers: { Accept: "application/json" },
});

export function cookieValue(cookieHeader: string, name: string): string | null {
  for (const part of cookieHeader.split(";")) {
    const [rawName, ...rawValue] = part.trim().split("=");
    if (rawName === name) {
      return decodeURIComponent(rawValue.join("="));
    }
  }
  return null;
}

export function csrfCookieNameForLocation(
  location: Pick<Location, "port" | "protocol">,
): string {
  const port = location.port
    || (location.protocol === "https:" ? "443" : "80");
  return `halpha_csrf_${port}`;
}

function csrfHeader(): Record<string, string> {
  const token = cookieValue(
    document.cookie,
    csrfCookieNameForLocation(window.location),
  );
  if (!token) {
    throw new ApiFailure(403, "CSRF_COOKIE_MISSING");
  }
  return { "X-CSRFToken": token };
}

function errorCode(error: unknown, fallback: string): string {
  if (typeof error !== "object" || error === null) return fallback;
  const detail = "detail" in error ? error.detail : null;
  if (typeof detail !== "object" || detail === null) return fallback;
  const code = "code" in detail ? detail.code : null;
  return typeof code === "string" ? code : fallback;
}

export async function getSettingsStatus(): Promise<SettingsStatus> {
  const { data, error, response } = await api.GET("/api/v1/settings/status");
  if (!data) {
    throw new ApiFailure(response.status, errorCode(error, "SETTINGS_STATUS_FAILED"));
  }
  return data;
}

export async function getOverview(): Promise<Overview> {
  const { data, error, response } = await api.GET("/api/v1/overview");
  if (!data) {
    throw new ApiFailure(response.status, errorCode(error, "OVERVIEW_FAILED"));
  }
  return data;
}

export async function previewAccountPositionOperation(
  payload: AccountPositionOperationPreviewPayload,
): Promise<AccountPositionOperationPreview> {
  const { data, error, response } = await api.POST(
    "/api/v1/account-position-operations/preview",
    {
      body: payload,
      headers: csrfHeader(),
    },
  );
  if (!data) {
    throw new ApiFailure(
      response.status,
      errorCode(error, "ACCOUNT_POSITION_OPERATION_PREVIEW_FAILED"),
      error,
    );
  }
  return data;
}

export async function sendTestEmail(): Promise<TestEmailResult> {
  const { data, error, response } = await api.POST("/api/v1/settings/test-email", {
    headers: csrfHeader(),
  });
  if (!data) throw new ApiFailure(response.status, errorCode(error, "TEST_EMAIL_FAILED"));
  return data;
}

export async function getStrategies(): Promise<StrategySummary[]> {
  const { data, error, response } = await api.GET("/api/v1/strategies");
  if (!data) throw new ApiFailure(response.status, errorCode(error, "STRATEGIES_FAILED"));
  return data;
}

export async function getMarketContext(
  instrumentRef: string,
  channelLookback15m: number,
  stopReferenceInterval: MarketInterval = "15m",
): Promise<MarketContext> {
  const { data, error, response } = await api.GET("/api/v1/market-context", {
    params: {
      query: {
        instrument_ref: instrumentRef,
        channel_lookback_15m: channelLookback15m,
        stop_reference_interval: stopReferenceInterval,
      },
    },
  });
  if (!data) throw new ApiFailure(response.status, errorCode(error, "MARKET_CONTEXT_FAILED"));
  return data;
}

export async function getMarketWindow(
  instrumentRef: string,
  startAt: string,
  endAt: string,
  interval: MarketInterval,
  purpose: MarketWindowPurpose = "EXECUTION_REVIEW",
): Promise<MarketWindow> {
  // Every product chart reads the current runtime environment. A caller cannot
  // opt into a second venue source and accidentally mix Demo and Live bars.
  const query = {
    instrument_ref: instrumentRef,
    start_at: startAt,
    end_at: endAt,
    interval,
    purpose,
  };
  const { data, error, response } = await api.GET("/api/v1/market-window", {
    params: {
      query,
    },
  });
  if (!data) throw new ApiFailure(response.status, errorCode(error, "MARKET_WINDOW_FAILED"));
  return data;
}

export async function previewOrderSchedule(
  payload: OrderSchedulePreviewPayload,
  signal?: AbortSignal,
): Promise<OrderSchedulePreview> {
  const { data, error, response } = await api.POST("/api/v1/order-schedules/preview", {
    body: payload,
    headers: csrfHeader(),
    signal,
  });
  if (!data) {
    throw new ApiFailure(
      response.status,
      errorCode(error, "ORDER_SCHEDULE_PREVIEW_FAILED"),
      error,
    );
  }
  return data;
}

export async function getPlans(): Promise<PlanSummary[]> {
  const { data, error, response } = await api.GET("/api/v1/plans");
  if (!data) throw new ApiFailure(response.status, errorCode(error, "PLANS_FAILED"));
  return data;
}

export async function getPlan(planId: string): Promise<PlanDraft> {
  const { data, error, response } = await api.GET("/api/v1/plans/{plan_id}", {
    params: { path: { plan_id: planId } },
  });
  if (!data) throw new ApiFailure(response.status, errorCode(error, "PLAN_FAILED"));
  return data;
}

export async function createPlan(
  payload: PlanCreatePayload,
  idempotencyKey: string,
): Promise<PlanDraft> {
  const { data, error, response } = await api.POST("/api/v1/plans", {
    body: payload,
    params: { header: { "Idempotency-Key": idempotencyKey } },
    headers: csrfHeader(),
  });
  if (!data) throw new ApiFailure(response.status, errorCode(error, "PLAN_CREATE_FAILED"));
  return data;
}

export async function deletePlan(planId: string, draftVersion: number): Promise<PlanDeleteResult> {
  const { data, error, response } = await api.DELETE("/api/v1/plans/{plan_id}", {
    params: {
      path: { plan_id: planId },
      header: { "If-Match": String(draftVersion) },
    },
    headers: csrfHeader(),
  });
  if (!data) throw new ApiFailure(response.status, errorCode(error, "PLAN_DELETE_FAILED"));
  return data;
}

export async function updatePlan(
  planId: string,
  draftVersion: number,
  payload: PlanDraftPayload,
): Promise<PlanDraft> {
  const { data, error, response } = await api.PUT("/api/v1/plans/{plan_id}", {
    body: payload,
    params: {
      path: { plan_id: planId },
      header: { "If-Match": String(draftVersion) },
    },
    headers: csrfHeader(),
  });
  if (!data) throw new ApiFailure(response.status, errorCode(error, "PLAN_UPDATE_FAILED"));
  return data;
}

export async function fixPlan(
  planId: string,
  draftVersion: number,
  idempotencyKey: string,
): Promise<PlanVersion> {
  const { data, error, response } = await api.POST("/api/v1/plans/{plan_id}/fix", {
    params: { path: { plan_id: planId }, header: { "Idempotency-Key": idempotencyKey, "If-Match": String(draftVersion) } },
    headers: csrfHeader(),
  });
  if (!data) throw new ApiFailure(response.status, errorCode(error, "PLAN_FIX_FAILED"));
  return data;
}

export async function getActivationPreview(planVersionId: string): Promise<ActivationPreview> {
  const { data, error, response } = await api.POST("/api/v1/plan-versions/{plan_version_id}/activation-preview", {
    params: { path: { plan_version_id: planVersionId } },
    headers: csrfHeader(),
  });
  if (!data) throw new ApiFailure(response.status, errorCode(error, "ACTIVATION_PREVIEW_FAILED"));
  return data;
}

export async function createActivation(
  payload: ActivationPayload,
  idempotencyKey: string,
): Promise<ActivationCreateResult> {
  const { data, error, response } = await api.POST("/api/v1/activations", {
    body: payload,
    params: { header: { "Idempotency-Key": idempotencyKey } },
    headers: csrfHeader(),
  });
  if (!data) throw new ApiFailure(response.status, errorCode(error, "ACTIVATION_CREATE_FAILED"));
  return data;
}

export async function getActivations(): Promise<ActivationSummary[]> {
  const { data, error, response } = await api.GET("/api/v1/activations");
  if (!data) throw new ApiFailure(response.status, errorCode(error, "ACTIVATIONS_FAILED"));
  return data;
}

export async function getActivation(activationId: string): Promise<ActivationDetail> {
  const { data, error, response } = await api.GET("/api/v1/activations/{activation_id}", {
    params: { path: { activation_id: activationId } },
  });
  if (!data) throw new ApiFailure(response.status, errorCode(error, "ACTIVATION_FAILED"));
  return data;
}

export async function getActivationTimeline(activationId: string): Promise<ActivationTimelineEntry[]> {
  const { data, error, response } = await api.GET("/api/v1/activations/{activation_id}/timeline", {
    params: { path: { activation_id: activationId } },
  });
  if (!data) throw new ApiFailure(response.status, errorCode(error, "ACTIVATION_TIMELINE_FAILED"));
  return data;
}

export async function getReviews(): Promise<Review[]> {
  const { data, error, response } = await api.GET("/api/v1/reviews");
  if (!data) throw new ApiFailure(response.status, errorCode(error, "REVIEWS_FAILED"));
  return data;
}

export async function getReviewSequenceEvidence({
  scope,
  rangeStart,
  rangeEnd,
  includePricePath = false,
  interval = "1m",
}: {
  scope: ReviewSequenceScope;
  rangeStart?: string;
  rangeEnd?: string;
  includePricePath?: boolean;
  interval?: ReviewPricePathInterval;
}): Promise<ReviewSequenceEvidence> {
  const { data, error, response } = await api.GET(
    "/api/v1/review-sequence-evidence",
    {
      params: {
        query: {
          scope,
          range_start: rangeStart,
          range_end: rangeEnd,
          include_price_path: includePricePath,
          interval,
        },
      },
    },
  );
  if (!data) {
    throw new ApiFailure(
      response.status,
      errorCode(error, "REVIEW_SEQUENCE_EVIDENCE_FAILED"),
      error,
    );
  }
  return data;
}

export async function getExecutionFeeEvidence(
  instrumentRef: string,
): Promise<ExecutionFeeEvidence> {
  const { data, error, response } = await api.GET("/api/v1/execution-fee-evidence", {
    params: { query: { instrument_ref: instrumentRef } },
  });
  if (!data) {
    throw new ApiFailure(response.status, errorCode(error, "EXECUTION_FEE_EVIDENCE_FAILED"));
  }
  return data;
}

export async function previewDecisionEvidence(
  payload: DecisionEvidencePreviewPayload,
): Promise<DecisionEvidence> {
  const { data, error, response } = await api.POST(
    "/api/v1/decision-evidence/preview",
    {
      body: payload,
      headers: csrfHeader(),
    },
  );
  if (!data) {
    throw new ApiFailure(
      response.status,
      errorCode(error, "DECISION_EVIDENCE_PREVIEW_FAILED"),
      error,
    );
  }
  return data;
}

export async function exportPlaybookQualification(
  payload: PlaybookQualificationExportPayload,
): Promise<PlaybookQualificationExport> {
  const { data, error, response } = await api.POST(
    "/api/v1/playbook-qualification/export",
    {
      body: payload,
      headers: csrfHeader(),
    },
  );
  if (!data) {
    throw new ApiFailure(
      response.status,
      errorCode(error, "PLAYBOOK_QUALIFICATION_EXPORT_FAILED"),
      error,
    );
  }
  return data;
}

export async function getStageReviews(): Promise<StageReview[]> {
  const { data, error, response } = await api.GET("/api/v1/stage-reviews");
  if (!data) throw new ApiFailure(response.status, errorCode(error, "STAGE_REVIEWS_FAILED"));
  return data;
}

export async function createStageReview(
  payload: StageReviewCreatePayload,
  idempotencyKey: string,
): Promise<StageReview> {
  const { data, error, response } = await api.POST("/api/v1/stage-reviews", {
    params: { header: { "Idempotency-Key": idempotencyKey } },
    body: payload,
    headers: csrfHeader(),
  });
  if (!data) throw new ApiFailure(response.status, errorCode(error, "STAGE_REVIEW_CREATE_FAILED"));
  return data;
}

export async function getReview(reviewId: string): Promise<ReviewHistory> {
  const { data, error, response } = await api.GET("/api/v1/reviews/{review_id}", {
    params: { path: { review_id: reviewId } },
  });
  if (!data) throw new ApiFailure(response.status, errorCode(error, "REVIEW_FAILED"));
  return data;
}

export async function getReviewPricePathEvidence(
  reviewId: string,
  reviewVersion: number,
  interval: ReviewPricePathInterval,
): Promise<ReviewPricePathEvidence> {
  const { data, error, response } = await api.GET(
    "/api/v1/reviews/{review_id}/price-path-evidence",
    {
      params: {
        path: { review_id: reviewId },
        query: { review_version: reviewVersion, interval },
      },
    },
  );
  if (!data) {
    throw new ApiFailure(
      response.status,
      errorCode(error, "REVIEW_PRICE_PATH_EVIDENCE_FAILED"),
      error,
    );
  }
  return data;
}

export async function refreshReview(reviewId: string, expectedVersion: number): Promise<ReviewDocument> {
  const { data, error, response } = await api.PUT("/api/v1/reviews/{review_id}", {
    params: { path: { review_id: reviewId } },
    body: { expected_version: expectedVersion },
    headers: csrfHeader(),
  });
  if (!data) throw new ApiFailure(response.status, errorCode(error, "REVIEW_REFRESH_FAILED"));
  return data;
}

export async function completeReview(
  reviewId: string,
  payload: ReviewCompletionPayload,
): Promise<ReviewCompletion> {
  const { data, error, response } = await api.POST("/api/v1/reviews/{review_id}/complete", {
    params: { path: { review_id: reviewId } },
    body: payload,
    headers: csrfHeader(),
  });
  if (!data) throw new ApiFailure(response.status, errorCode(error, "REVIEW_COMPLETE_FAILED"));
  return data;
}

export type ControlIntent = components["schemas"]["ControlIntent"];

export async function previewControl(activationId: string, intent: ControlIntent): Promise<ControlPreview> {
  const { data, error, response } = await api.POST("/api/v1/activations/{activation_id}/control-preview", {
    params: { path: { activation_id: activationId }, query: { intent } },
    headers: csrfHeader(),
  });
  if (!data) throw new ApiFailure(response.status, errorCode(error, "CONTROL_PREVIEW_FAILED"));
  return data;
}

export async function submitActivationControl(
  activationId: string,
  intent: ControlIntent,
  payload: ControlPayload,
  idempotencyKey: string,
): Promise<Receipt> {
  const options = { params: { path: { activation_id: activationId }, header: { "Idempotency-Key": idempotencyKey } }, body: payload, headers: csrfHeader() };
  const result =
    intent === "STOP_NEW_RISK" ? await api.POST("/api/v1/activations/{activation_id}/stop-new-risk", options) :
    intent === "RESUME_ACTIVATION" ? await api.POST("/api/v1/activations/{activation_id}/resume", options) :
    intent === "EXIT_STRATEGY" ? await api.POST("/api/v1/activations/{activation_id}/exit", options) :
    await api.POST("/api/v1/activations/{activation_id}/takeover", options);
  if (!result.data) throw new ApiFailure(result.response.status, errorCode(result.error, "CONTROL_SUBMIT_FAILED"));
  return result.data;
}

export async function previewSystemStopRelease(
  activationId: string,
): Promise<SystemStopReleasePreview> {
  const { data, error, response } = await api.GET(
    "/api/v1/activations/{activation_id}/system-stop-release-preview",
    { params: { path: { activation_id: activationId } } },
  );
  if (!data) {
    throw new ApiFailure(
      response.status,
      errorCode(error, "SYSTEM_STOP_RELEASE_PREVIEW_FAILED"),
    );
  }
  return data;
}

export async function releaseSystemStop(
  activationId: string,
  payload: SystemStopReleasePayload,
  idempotencyKey: string,
): Promise<SystemStopRelease> {
  const { data, error, response } = await api.POST(
    "/api/v1/activations/{activation_id}/release-system-stop",
    {
      params: {
        path: { activation_id: activationId },
        header: { "Idempotency-Key": idempotencyKey },
      },
      body: payload,
      headers: csrfHeader(),
    },
  );
  if (!data) {
    throw new ApiFailure(
      response.status,
      errorCode(error, "SYSTEM_STOP_RELEASE_FAILED"),
    );
  }
  return data;
}
