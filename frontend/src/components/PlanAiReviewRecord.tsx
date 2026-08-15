import AutoAwesomeOutlined from "@mui/icons-material/AutoAwesomeOutlined";
import { Box, Chip, Stack, Typography, type SxProps, type Theme } from "@mui/material";

import { formatUserVisibleTime } from "../format";
import {
  planAiReviewRecordPresentation,
  type PlanAiReviewRecordPresentation,
} from "../planAiReviewRecord";

type PlanAiReviewRecordProps = {
  review: unknown;
  reviewRef?: string | null;
  required?: boolean;
  compact?: boolean;
  defaultOpen?: boolean;
  sx?: SxProps<Theme>;
};

function reviewMeta(record: PlanAiReviewRecordPresentation): string[] {
  return [
    record.configurationLabel,
    record.completedAt ? `审核于 ${formatUserVisibleTime(record.completedAt)}` : null,
    record.marketSourceCutoff
      ? `行情截止 ${formatUserVisibleTime(record.marketSourceCutoff)}`
      : null,
  ].filter((item): item is string => Boolean(item));
}

/** A single, read-only review record that is frozen with a plan version. */
export default function PlanAiReviewRecord({
  review,
  reviewRef,
  required = false,
  compact = false,
  defaultOpen = false,
  sx,
}: PlanAiReviewRecordProps) {
  const presentation = planAiReviewRecordPresentation(review, reviewRef);
  const hasReview = presentation.status !== "MISSING";
  if (!required && !hasReview) return null;
  const meta = reviewMeta(presentation);
  const hasDetails = Boolean(
    presentation.reason
    || presentation.suggestions.length > 0
    || meta.length > 0,
  );

  if (compact) {
    return (
      <Stack
        direction="row"
        spacing={0.75}
        useFlexGap
        aria-label="AI 审核记录"
        sx={{ alignItems: "center", flexWrap: "wrap", ...sx }}
      >
        <Chip
          size="small"
          color={presentation.tone}
          variant="outlined"
          label={presentation.label}
          sx={{ fontWeight: 750 }}
        />
        {presentation.completedAt ? (
          <Typography variant="caption" color="text.secondary">
            {formatUserVisibleTime(presentation.completedAt)}
          </Typography>
        ) : null}
      </Stack>
    );
  }

  return (
    <Box
      component="section"
      aria-label="AI 审核记录"
      sx={{
        mt: 2,
        pt: 1.5,
        borderTop: 1,
        borderColor: "divider",
        ...sx,
      }}
    >
      <Stack
        direction={{ xs: "column", sm: "row" }}
        spacing={1}
        sx={{ alignItems: { sm: "center" }, justifyContent: "space-between" }}
      >
        <Stack direction="row" spacing={0.75} sx={{ alignItems: "center", minWidth: 0 }}>
          <AutoAwesomeOutlined sx={{ color: "warning.dark", fontSize: 18, flexShrink: 0 }} />
          <Typography component="h2" variant="subtitle2" sx={{ fontWeight: 850 }}>
            AI 审核记录
          </Typography>
        </Stack>
        <Chip
          size="small"
          color={presentation.tone}
          variant="outlined"
          label={presentation.label}
          sx={{ alignSelf: { xs: "flex-start", sm: "center" }, fontWeight: 750 }}
        />
      </Stack>
      {hasDetails ? (
        <Box
          component="details"
          open={defaultOpen || undefined}
          sx={{
            mt: 1,
            "& > summary": {
              cursor: "pointer",
              color: "text.secondary",
              fontSize: 13,
              fontWeight: 700,
            },
          }}
        >
          <Box component="summary">审核理由与建议</Box>
          <Box sx={{ mt: 1.25, minWidth: 0 }}>
            {meta.length > 0 ? (
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1 }}>
                {meta.join(" · ")}
              </Typography>
            ) : null}
            {presentation.reason ? (
              <>
                <Typography variant="caption" color="text.secondary">审核理由</Typography>
                <Typography variant="body2" sx={{ mt: 0.35, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                  {presentation.reason}
                </Typography>
              </>
            ) : null}
            {presentation.suggestions.length > 0 ? (
              <Box component="ul" sx={{ mb: 0, mt: presentation.reason ? 1 : 0, pl: 2.25 }}>
                {presentation.suggestions.map((suggestion, index) => (
                  <li key={`${index}:${suggestion}`}>
                    <Typography variant="body2" component="span">{suggestion}</Typography>
                  </li>
                ))}
              </Box>
            ) : null}
          </Box>
        </Box>
      ) : null}
    </Box>
  );
}
