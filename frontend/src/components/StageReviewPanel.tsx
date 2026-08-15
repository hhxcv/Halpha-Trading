import {
  Alert,
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  LinearProgress,
  MenuItem,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import {
  ApiFailure,
  createStageReview,
  getStageReviews,
  isUnknownMutationResult,
  type StageReviewCreatePayload,
} from "../api/client";
import { formatUserVisibleTime } from "../format";
import { financialToneClassName, financialToneForSignedValue } from "../marketColors";
import {
  clearPersistentRequestIdentity,
  persistentRequestIdentity,
  type StableRequestIdentity,
} from "../requestIdentity";
import { surfaceFrameSx } from "../theme";

type StageReviewForm = {
  title: string;
  rangeStart: string;
  rangeEnd: string;
  problemAnalysis: string;
  improvementPlan: string;
  creatorKind: "HUMAN" | "AI";
};

function recordOf(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function stringOf(record: Record<string, unknown>, key: string, fallback = ""): string {
  const value = record[key];
  return value === null || value === undefined ? fallback : String(value);
}

function numberOf(record: Record<string, unknown>, key: string): number | null {
  const value = Number(record[key]);
  return Number.isFinite(value) ? value : null;
}

function localDateTimeInput(value: string): string {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function inclusiveMinuteEnd(value: Date): string {
  return new Date(value.getTime() + 60_000 - 1).toISOString();
}

function signedUsdt(value: unknown): string {
  const number = Number(value);
  if (!Number.isFinite(number)) return "未知";
  return `${new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
    signDisplay: "exceptZero",
  }).format(number)} USDT`;
}

function returnPercent(value: unknown): string {
  const number = Number(value);
  if (!Number.isFinite(number)) return "未知";
  return `${new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
    signDisplay: "exceptZero",
  }).format(number)}%`;
}

function defaultForm(reviewFactCutoffs: string[]): StageReviewForm {
  const times = reviewFactCutoffs
    .map((value) => Date.parse(value))
    .filter(Number.isFinite)
    .sort((left, right) => left - right);
  const start = times[0] ?? Date.now();
  const end = times.at(-1) ?? Date.now();
  const endDate = new Date(end);
  return {
    title: `${endDate.toLocaleDateString("zh-CN", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    })} 阶段性复盘`,
    rangeStart: localDateTimeInput(new Date(start).toISOString()),
    rangeEnd: localDateTimeInput(new Date(end).toISOString()),
    problemAnalysis: "",
    improvementPlan: "",
    creatorKind: "HUMAN",
  };
}

export default function StageReviewPanel({
  environmentId,
  liveReadOnly,
  reviewFactCutoffs,
}: {
  environmentId: string;
  liveReadOnly: boolean;
  reviewFactCutoffs: string[];
}) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["stage-reviews", environmentId],
    queryFn: getStageReviews,
  });
  const [dialogOpen, setDialogOpen] = useState(false);
  const [form, setForm] = useState<StageReviewForm>(() => defaultForm(reviewFactCutoffs));
  const requestIdentityRef = useRef<StableRequestIdentity | null>(null);
  const requestScope = `stage-review:${environmentId}`;

  useEffect(() => {
    setDialogOpen(false);
    setForm(defaultForm(reviewFactCutoffs));
    requestIdentityRef.current = null;
  }, [environmentId]);

  const payload = useMemo<StageReviewCreatePayload | null>(() => {
    const start = new Date(form.rangeStart);
    const end = new Date(form.rangeEnd);
    if (
      !form.title.trim()
      || !form.problemAnalysis.trim()
      || !form.improvementPlan.trim()
      || !Number.isFinite(start.getTime())
      || !Number.isFinite(end.getTime())
      || start > end
    ) {
      return null;
    }
    return {
      title: form.title.trim(),
      range_start: start.toISOString(),
      range_end: inclusiveMinuteEnd(end),
      problem_analysis: form.problemAnalysis.trim(),
      improvement_plan: form.improvementPlan.trim(),
      creator_kind: form.creatorKind,
    };
  }, [form]);

  const mutation = useMutation({
    mutationFn: async () => {
      if (payload === null) throw new Error("STAGE_REVIEW_FORM_INCOMPLETE");
      const fingerprint = JSON.stringify(payload);
      const identity = persistentRequestIdentity(
        requestIdentityRef.current,
        requestScope,
        fingerprint,
      );
      requestIdentityRef.current = identity;
      return createStageReview(payload, identity.idempotencyKey);
    },
    onSuccess: async () => {
      clearPersistentRequestIdentity(requestScope);
      requestIdentityRef.current = null;
      setDialogOpen(false);
      setForm(defaultForm(reviewFactCutoffs));
      await queryClient.invalidateQueries({
        queryKey: ["stage-reviews", environmentId],
      });
    },
  });

  const stageReviews = query.data ?? [];
  const openDialog = () => {
    setForm(defaultForm(reviewFactCutoffs));
    setDialogOpen(true);
  };
  const updateForm = <Key extends keyof StageReviewForm>(
    key: Key,
    value: StageReviewForm[Key],
  ) => {
    setForm((current) => ({ ...current, [key]: value }));
  };

  return (
    <Box component="section" aria-label="阶段性复盘">
      <Stack
        direction={{ xs: "column", sm: "row" }}
        spacing={1}
        sx={{ mb: 1.5, alignItems: { sm: "center" }, justifyContent: "space-between" }}
      >
        <Box>
          <Typography variant="h2">阶段性复盘</Typography>
          <Typography variant="caption" color="text.secondary">
            固定指定时间范围的样本、指标、问题判断和改进方案；创建后不会随新交易自动变化。
          </Typography>
        </Box>
        <Button
          variant="contained"
          onClick={openDialog}
          disabled={liveReadOnly || reviewFactCutoffs.length === 0}
        >
          新建阶段性复盘
        </Button>
      </Stack>

      {query.isPending && <LinearProgress aria-label="正在读取阶段性复盘" />}
      {query.isError && <Alert severity="error">阶段性复盘当前不可读；不会显示缓存替代内容。</Alert>}
      {!query.isPending && !query.isError && stageReviews.length === 0 && (
        <Alert severity="info" variant="outlined">
          {liveReadOnly
            ? "实盘只读环境尚无阶段性复盘；当前不能创建，已有记录仍会在这里显示。"
            : "尚无阶段性复盘。选择一段历史计划后，可把当时的总体判断和改进方案固定保存。"}
        </Alert>
      )}
      <Stack spacing={1.25}>
        {stageReviews.map((item) => {
          const metrics = recordOf(item.metrics_snapshot);
          const sourceCount = numberOf(metrics, "review_count") ?? 0;
          const reliableCount = numberOf(metrics, "reliable_trade_count") ?? 0;
          const pendingCount = numberOf(metrics, "pending_evaluation_count") ?? 0;
          return (
            <Box
              component="article"
              key={stringOf(item, "stage_review_id")}
              sx={{ ...surfaceFrameSx, p: { xs: 1.5, sm: 2 } }}
            >
              <Stack
                direction={{ xs: "column", sm: "row" }}
                spacing={1}
                sx={{ justifyContent: "space-between" }}
              >
                <Box>
                  <Typography variant="h3">{stringOf(item, "title")}</Typography>
                  <Typography variant="caption" color="text.secondary">
                    {formatUserVisibleTime(stringOf(item, "range_start"))}
                    {" 至 "}
                    {formatUserVisibleTime(stringOf(item, "range_end"))}
                    {" · "}
                    {sourceCount} 个计划
                    {" · "}
                    {stringOf(item, "creator_kind") === "AI" ? "AI 创建" : "本人创建"}
                  </Typography>
                </Box>
                <Typography variant="caption" color="text.secondary">
                  创建于 {formatUserVisibleTime(stringOf(item, "created_at"))}
                </Typography>
              </Stack>
              <Box sx={{
                display: "grid",
                gridTemplateColumns: {
                  xs: "repeat(2, minmax(0, 1fr))",
                  md: "repeat(5, minmax(0, 1fr))",
                },
                gap: 1,
                mt: 1.5,
              }}>
                {[
                  { label: "可靠闭合", value: `${reliableCount} 笔` },
                  { label: "累计净盈亏", value: signedUsdt(metrics.net_pnl), tone: financialToneForSignedValue(metrics.net_pnl) },
                  { label: "累计净回报", value: returnPercent(metrics.notional_return_percent), tone: financialToneForSignedValue(metrics.notional_return_percent) },
                  { label: "累计手续费", value: signedUsdt(-Math.abs(Number(metrics.commission))), tone: financialToneForSignedValue(-1) },
                  { label: "待评价", value: `${pendingCount} 笔` },
                ].map((item) => (
                  <Box key={item.label}>
                    <Typography variant="caption" color="text.secondary">{item.label}</Typography>
                    <Typography className={`mono ${financialToneClassName(item.tone) ?? ""}`} sx={{ fontWeight: 750 }}>{item.value}</Typography>
                  </Box>
                ))}
              </Box>
              <Box component="details" sx={{ mt: 1.5 }}>
                <Typography component="summary" variant="body2" sx={{ cursor: "pointer", fontWeight: 700 }}>
                  查看问题判断与改进方案
                </Typography>
                <Box sx={{
                  display: "grid",
                  gridTemplateColumns: { xs: "1fr", md: "1fr 1fr" },
                  gap: 1.5,
                  mt: 1.25,
                }}>
                  <Box>
                    <Typography variant="caption" color="text.secondary">问题判断</Typography>
                    <Typography variant="body2" sx={{ whiteSpace: "pre-wrap" }}>
                      {stringOf(item, "problem_analysis")}
                    </Typography>
                  </Box>
                  <Box>
                    <Typography variant="caption" color="text.secondary">改进方案</Typography>
                    <Typography variant="body2" sx={{ whiteSpace: "pre-wrap" }}>
                      {stringOf(item, "improvement_plan")}
                    </Typography>
                  </Box>
                </Box>
              </Box>
            </Box>
          );
        })}
      </Stack>

      <Dialog
        open={dialogOpen}
        onClose={() => !mutation.isPending && setDialogOpen(false)}
        fullWidth
        maxWidth="md"
      >
        <DialogTitle>新建阶段性复盘</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            保存范围内当前单次复盘版本和指标快照。以后新增交易或修改单次复盘不会改写本记录。
          </Typography>
          <Box sx={{
            display: "grid",
            gridTemplateColumns: { xs: "1fr", sm: "1fr 1fr" },
            gap: 1.5,
          }}>
            <TextField
              label="标题"
              value={form.title}
              onChange={(event) => updateForm("title", event.target.value)}
              slotProps={{ htmlInput: { maxLength: 160 } }}
              sx={{ gridColumn: { sm: "1 / -1" } }}
            />
            <TextField
              label="范围开始"
              type="datetime-local"
              value={form.rangeStart}
              onChange={(event) => updateForm("rangeStart", event.target.value)}
              slotProps={{ inputLabel: { shrink: true } }}
            />
            <TextField
              label="范围结束（含该分钟）"
              type="datetime-local"
              value={form.rangeEnd}
              onChange={(event) => updateForm("rangeEnd", event.target.value)}
              slotProps={{ inputLabel: { shrink: true } }}
            />
            <TextField
              select
              label="创建者"
              value={form.creatorKind}
              onChange={(event) => updateForm(
                "creatorKind",
                event.target.value as StageReviewForm["creatorKind"],
              )}
            >
              <MenuItem value="HUMAN">本人创建</MenuItem>
              <MenuItem value="AI">AI 创建</MenuItem>
            </TextField>
            <Box sx={{ display: { xs: "none", sm: "block" } }} />
            <TextField
              label="问题判断"
              value={form.problemAnalysis}
              onChange={(event) => updateForm("problemAnalysis", event.target.value)}
              multiline
              minRows={4}
              slotProps={{ htmlInput: { maxLength: 8000 } }}
            />
            <TextField
              label="改进方案"
              value={form.improvementPlan}
              onChange={(event) => updateForm("improvementPlan", event.target.value)}
              multiline
              minRows={4}
              slotProps={{ htmlInput: { maxLength: 8000 } }}
            />
          </Box>
          {mutation.isError && (
            <Alert severity={isUnknownMutationResult(mutation.error) ? "warning" : "error"} sx={{ mt: 1.5 }}>
              {isUnknownMutationResult(mutation.error)
                ? "提交结果暂不确定；再次提交会沿用同一请求身份查询或复用结果。"
                : `创建失败：${mutation.error instanceof ApiFailure ? mutation.error.code : "输入或范围无效"}`}
            </Alert>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDialogOpen(false)} disabled={mutation.isPending}>取消</Button>
          <Button
            variant="contained"
            disabled={payload === null || mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? "正在保存…" : "保存阶段性复盘"}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
