import { useEffect } from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  LinearProgress,
  MenuItem,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import AutoAwesomeOutlined from "@mui/icons-material/AutoAwesomeOutlined";
import CheckCircleOutlined from "@mui/icons-material/CheckCircleOutlined";
import HighlightOffOutlined from "@mui/icons-material/HighlightOffOutlined";
import ErrorOutlined from "@mui/icons-material/ErrorOutlined";

import {
  planAiReviewStreamUrl,
  type PlanAiReview,
  type PlanAiReviewConfiguration,
} from "../api/client";
import { formatUserVisibleTime } from "../format";
import { surfaceFrameSx } from "../theme";

type PlanAiReviewPanelProps = {
  review: PlanAiReview | null;
  reviewMatchesDraft: boolean;
  staleReason: "PLAN_CHANGED" | "CONFIGURATION_CHANGED" | "PROMPT_UPGRADED" | "MARKET_CONTEXT_EXPIRED" | null;
  requesting: boolean;
  requestError: string | null;
  requestBlockedReasons: readonly string[];
  configuration: PlanAiReviewConfiguration;
  onConfigurationChange: (configuration: PlanAiReviewConfiguration) => void;
  onRequest: () => void;
  onReviewUpdate: (review: PlanAiReview) => void;
};

const terminalStatuses = new Set<PlanAiReview["status"]>([
  "APPROVED",
  "REJECTED",
  "FAILED",
]);

const failureLabels: Record<string, string> = {
  PLAN_AI_REVIEW_CODEX_CLI_UNAVAILABLE: "本机 Codex CLI 当前不可用",
  PLAN_AI_REVIEW_WORKSPACE_UNAVAILABLE: "审核工作目录当前不可写",
  PLAN_AI_REVIEW_TIMEOUT: "AI 审核超时",
  PLAN_AI_REVIEW_PROVIDER_FAILED: "Codex CLI 未正常完成",
  PLAN_AI_REVIEW_OUTPUT_TOO_LARGE: "AI 审核输出超过安全上限",
  PLAN_AI_REVIEW_DIAGNOSTIC_TOO_LARGE: "AI 审核诊断输出超过安全上限",
  PLAN_AI_REVIEW_OUTPUT_INVALID: "AI 输出未通过固定格式校验",
  PLAN_AI_REVIEW_OUTPUT_MISSING: "AI 未返回可用结论",
  PLAN_AI_REVIEW_CONTEXT_INVALID: "计划或行情上下文未能形成有效审核输入",
  PLAN_AI_REVIEW_CONTEXT_UNAVAILABLE: "审核所需上下文当前不可用",
  PLAN_AI_REVIEW_CONTEXT_TEMPORARILY_UNAVAILABLE: "行情数据连接暂时超时，已完成重试仍未恢复",
  PLAN_AI_REVIEW_DRAFT_CHANGED: "审核开始前草稿已经变化",
  PLAN_AI_REVIEW_INTERRUPTED: "应用退出或审核任务被中断",
  PLAN_AI_REVIEW_INTERNAL_FAILURE: "审核服务未能完成启动",
  PLAN_AI_REVIEW_FAILED: "历史审核未能完成启动",
};

function statusLabel(
  review: PlanAiReview,
  {
    approved,
    rejected,
    failed,
  }: {
    approved: boolean;
    rejected: boolean;
    failed: boolean;
  },
): string {
  if (approved) return "AI 审核结论：批准";
  if (rejected) return "AI 审核结论：拒绝";
  if (failed) return "审核未完成";
  return review.status === "QUEUED" ? "等待 AI 审核" : "AI 审核中";
}

export default function PlanAiReviewPanel({
  review,
  reviewMatchesDraft,
  staleReason,
  requesting,
  requestError,
  requestBlockedReasons,
  configuration,
  onConfigurationChange,
  onRequest,
  onReviewUpdate,
}: PlanAiReviewPanelProps) {
  useEffect(() => {
    if (!review || terminalStatuses.has(review.status)) return undefined;
    const socket = new WebSocket(planAiReviewStreamUrl(review.review_id));
    socket.onmessage = (event) => {
      try {
        const next = JSON.parse(String(event.data)) as PlanAiReview;
        if (next.review_id === review.review_id) onReviewUpdate(next);
      } catch {
        // The regular query poll remains the fallback for malformed frames.
      }
    };
    return () => socket.close();
  }, [onReviewUpdate, review?.review_id, review?.status]);

  const running = review?.status === "QUEUED" || review?.status === "RUNNING";
  const active = reviewMatchesDraft && running;
  // Status alone is not an AI conclusion.  The terminal decision must carry
  // the exact enum produced by the structured reviewer output before the UI
  // may call it an AI approval or rejection.
  const approved = reviewMatchesDraft
    && review?.status === "APPROVED"
    && review.decision === "APPROVE";
  const rejected = reviewMatchesDraft
    && review?.status === "REJECTED"
    && review.decision === "REJECT";
  const terminalDecisionInvalid = Boolean(
    reviewMatchesDraft
    && review
    && ((review.status === "APPROVED" && review.decision !== "APPROVE")
      || (review.status === "REJECTED" && review.decision !== "REJECT")),
  );
  const failed = reviewMatchesDraft
    && (review?.status === "FAILED" || terminalDecisionInvalid);
  const stale = Boolean(review && !reviewMatchesDraft);
  const canRequest = !requesting && !running && requestBlockedReasons.length === 0;

  return (
    <Box component="section" aria-labelledby="plan-ai-review-title" sx={{ p: 1.5 }}>
      <Stack direction="row" spacing={1} sx={{ alignItems: "flex-start", justifyContent: "space-between" }}>
        <Box sx={{ minWidth: 0 }}>
          <Stack direction="row" spacing={.75} sx={{ alignItems: "center" }}>
            <AutoAwesomeOutlined sx={{ fontSize: 18, color: "warning.dark" }} />
            <Typography id="plan-ai-review-title" component="h2" variant="subtitle2" sx={{ fontWeight: 850 }}>
              AI 交易审核
            </Typography>
          </Stack>
        </Box>
        {review && reviewMatchesDraft ? (
          <Chip
            size="small"
            label={statusLabel(review, { approved, rejected, failed })}
            color={approved ? "success" : rejected || failed ? "error" : "warning"}
            variant={active ? "outlined" : "filled"}
            sx={{ flex: "0 0 auto", fontWeight: 800 }}
          />
        ) : null}
      </Stack>

      <Stack direction={{ xs: "column", sm: "row" }} spacing={1} sx={{ mt: 1.25 }}>
        <TextField
          select
          size="small"
          label="审核模型"
          value={configuration.model}
          disabled={running || requesting}
          onChange={(event) => onConfigurationChange({
            ...configuration,
            model: event.target.value as PlanAiReviewConfiguration["model"],
          })}
          sx={{ minWidth: 180 }}
        >
          <MenuItem value="gpt-5.6-terra">GPT-5.6 Terra</MenuItem>
          <MenuItem value="gpt-5.6-sol">GPT-5.6 Sol</MenuItem>
          <MenuItem value="gpt-5.6-luna">GPT-5.6 Luna</MenuItem>
        </TextField>
        <TextField
          select
          size="small"
          label="推理强度"
          value={configuration.reasoning_effort}
          disabled={running || requesting}
          onChange={(event) => onConfigurationChange({
            ...configuration,
            reasoning_effort: event.target.value as PlanAiReviewConfiguration["reasoning_effort"],
          })}
          sx={{ minWidth: 150 }}
        >
          <MenuItem value="low">低</MenuItem>
          <MenuItem value="medium">中等</MenuItem>
          <MenuItem value="high">高</MenuItem>
          <MenuItem value="xhigh">很高</MenuItem>
          <MenuItem value="max">最大</MenuItem>
          <MenuItem value="ultra">极限</MenuItem>
        </TextField>
      </Stack>

      {running || requesting ? (
        <Box aria-live="polite" sx={{ mt: 1.5 }}>
          <LinearProgress aria-label="AI 正在审核交易计划" color="warning" />
          <Typography variant="body2" sx={{ mt: 1, fontWeight: 750 }}>
            {requesting ? "正在保存当前草稿并收集审核上下文…" : review?.progress_message}
          </Typography>
          {review?.public_output ? (
            <Box
              data-testid="plan-ai-review-live-output"
              sx={{
                ...surfaceFrameSx,
                mt: 1.25,
                p: 1.25,
                bgcolor: "#F8FAFC",
                maxHeight: 180,
                overflow: "auto",
              }}
            >
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: .5 }}>
                AI 实时输出
              </Typography>
              <Typography
                component="pre"
                variant="caption"
                sx={{ m: 0, whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontFamily: "monospace" }}
              >
                {review.public_output}
              </Typography>
            </Box>
          ) : null}
        </Box>
      ) : null}

      {approved && review ? (
        <Alert
          severity="success"
          icon={<CheckCircleOutlined fontSize="inherit" />}
          data-testid="plan-ai-review-approved"
          sx={{ mt: 1.5 }}
        >
          <Typography variant="body2" sx={{ fontWeight: 850 }}>
            AI 审核结论：批准
          </Typography>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: .35 }}>
            AI 审核理由
          </Typography>
          <Typography variant="body2" sx={{ mt: .45, whiteSpace: "pre-wrap" }}>{review.reason}</Typography>
          {(review.suggestions ?? []).length > 0 ? (
            <Box component="ul" sx={{ my: .75, pl: 2.25 }}>
              {(review.suggestions ?? []).map((item) => <li key={item}>{item}</li>)}
            </Box>
          ) : null}
        </Alert>
      ) : null}

      {rejected && review ? (
        <Alert
          severity="error"
          icon={<HighlightOffOutlined fontSize="inherit" />}
          data-testid="plan-ai-review-rejected"
          sx={{ mt: 1.5 }}
        >
          <Typography variant="body2" sx={{ fontWeight: 850 }}>
            AI 审核结论：拒绝
          </Typography>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: .35 }}>
            AI 审核理由
          </Typography>
          <Typography variant="body2" sx={{ mt: .45, whiteSpace: "pre-wrap" }}>{review.reason}</Typography>
          {(review.suggestions ?? []).length > 0 ? (
            <Box component="ul" sx={{ my: .75, pl: 2.25 }}>
              {(review.suggestions ?? []).map((item) => <li key={item}>{item}</li>)}
            </Box>
          ) : null}
          <Typography variant="caption" sx={{ display: "block", mt: .5 }}>
            返回“核对”或前序步骤修改后，再提交一份绑定新草稿版本的审核。
          </Typography>
        </Alert>
      ) : null}

      {failed && review ? (
        <Alert severity="error" data-testid="plan-ai-review-failed" sx={{ mt: 1.5 }}>
          <Stack direction="row" spacing={.75} sx={{ alignItems: "center", flexWrap: "wrap" }} useFlexGap>
            <Chip size="small" color="error" variant="outlined" label="非 AI 审核结论" />
            <Typography variant="body2" sx={{ fontWeight: 850 }}>审核未完成</Typography>
          </Stack>
          <Typography variant="body2" sx={{ mt: .4 }}>
            本次审核未产生 AI 的“批准”或“拒绝”结论。
          </Typography>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: .65 }}>
            失败原因
          </Typography>
          <Typography variant="body2" sx={{ mt: .15 }}>
            {terminalDecisionInvalid
              ? "审核记录缺少与状态一致的 AI 审核结论。"
              : failureLabels[review.failure_code ?? ""]
                ?? (review.failure_code
                  ? `审核服务失败（${review.failure_code}）`
                  : "AI 审核服务当前未能给出有效结论")}。
          </Typography>
          <Typography variant="caption" sx={{ display: "block", mt: .65 }}>
            当前草稿不能提交；恢复失败原因后，请重新提交 AI 审核。
          </Typography>
        </Alert>
      ) : null}

      {stale ? (
        <Alert severity="warning" data-testid="plan-ai-review-stale" sx={{ mt: 1.5 }}>
          <Typography variant="body2" sx={{ fontWeight: 850 }}>历史审核记录已失效</Typography>
          <Typography variant="body2" sx={{ mt: .4 }}>
            {staleReason === "MARKET_CONTEXT_EXPIRED"
              ? "上次 AI 审核曾给出批准，但其市场依据现已过期；这不是 AI 拒绝。"
              : staleReason === "PROMPT_UPGRADED"
              ? "审核输入已升级；此前结论不能用于当前版本。"
              : staleReason === "CONFIGURATION_CHANGED"
                ? "审核模型或推理强度已变更；此前结论不能用于当前设置。"
                : "当前可编辑配置已变更；此前结论不能用于当前草稿。"}
          </Typography>
          <Typography variant="caption" sx={{ display: "block", mt: .65 }}>
            这不是当前草稿的 AI 审核结论；请按当前配置重新提交审核。
          </Typography>
        </Alert>
      ) : null}

      {requestError ? (
        <Alert severity="error" variant="outlined" sx={{ mt: 1.25 }}>{requestError}</Alert>
      ) : null}

      {review && reviewMatchesDraft ? (
        <Stack direction="row" spacing={1} sx={{ mt: 1.25, flexWrap: "wrap" }} useFlexGap>
          <Chip size="small" variant="outlined" label={`草稿 v${review.draft_version}`} />
          {review.market_source_cutoff ? (
            <Chip size="small" variant="outlined" label={`行情截止 ${formatUserVisibleTime(review.market_source_cutoff)}`} />
          ) : null}
        </Stack>
      ) : null}

      {!approved ? (
        <Tooltip
          arrow
          placement="top"
          disableHoverListener={requestBlockedReasons.length === 0}
          title={requestBlockedReasons.length > 0 ? (
              <Box component="ul" sx={{ m: 0, pl: 2, py: .25, maxWidth: 360 }}>
                {requestBlockedReasons.map((reason) => <li key={reason}>{reason}</li>)}
              </Box>
          ) : "提交当前计划"}
        >
          <Box sx={{ display: "flex", alignItems: "center", gap: .75, mt: 1.5 }}>
            {requestBlockedReasons.length > 0 ? (
              <ErrorOutlined color="error" fontSize="small" aria-label="AI 审核条件未满足" />
            ) : null}
            <Button
              type="button"
              variant="contained"
              color="warning"
              fullWidth
              disabled={!canRequest}
              onClick={onRequest}
            >
              {requesting
                ? "正在提交…"
                : running
                  ? "AI 审核进行中…"
                  : rejected || failed || stale
                    ? "按当前计划重新提交 AI 审核"
                    : "提交 AI 审核"}
            </Button>
          </Box>
        </Tooltip>
      ) : null}

    </Box>
  );
}
