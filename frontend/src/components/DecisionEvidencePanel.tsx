import {
  Alert,
  Box,
  Button,
  Chip,
  LinearProgress,
  Stack,
  Typography,
} from "@mui/material";
import { DownloadOutlined } from "@mui/icons-material";

import type { DecisionEvidence } from "../api/client";
import { formatUserVisibleTime } from "../format";
import { surfaceFrameSx } from "../theme";
import FactGrid from "./FactGrid";


export type QualificationTarget = "USDM_COPY_LEAD" | "USDM_PERSONAL";

const qualificationTargetLabels: Record<QualificationTarget, string> = {
  USDM_COPY_LEAD: "带单账户",
  USDM_PERSONAL: "个人账户",
};

const gradePresentation: Record<
  DecisionEvidence["evidence_grade"],
  { label: string; severity: "info" | "warning" | "success" }
> = {
  VALIDATION_INTENT: {
    label: "机制验证，不进入收益证据",
    severity: "info",
  },
  NO_COMPARABLE_SAMPLE: {
    label: "没有完全同类的可比交易",
    severity: "warning",
  },
  SINGLE_DIGIT_ANECDOTAL: {
    label: "仅个位数样本，属于轶事证据",
    severity: "warning",
  },
  REPEATED_OBSERVATION_UNPROVEN: {
    label: "已有重复观察，仍未证明稳定性",
    severity: "info",
  },
};

const exclusionLabels: Record<string, string> = {
  VALIDATION_INTENT: "验证性计划",
  MISSING_DECISION_INTENT: "旧记录缺少交易目的",
  PENDING_REVIEW: "待完成复盘",
  TOOLING_ISSUE: "工具问题",
  VALIDATION_TRADE: "验证性交易",
  NO_TRADE: "未形成交易",
  INSUFFICIENT_EVIDENCE: "证据不足",
  UNRELIABLE_RESULT: "结果不可可靠计算",
  MISSING_CLASSIFICATION: "缺少复盘分类",
};

const repeatabilityPresentation: Record<
  DecisionEvidence["repeatability"]["status"],
  { label: string; severity: "info" | "warning" | "success" }
> = {
  NOT_APPLICABLE_VALIDATION: {
    label: "验证目的不进行收益可重复性筛查",
    severity: "info",
  },
  NOT_READY: {
    label: "同剧本证据尚未达到下一步评估条件",
    severity: "warning",
  },
  EVIDENCE_CANDIDATE: {
    label: "同剧本证据可进入下一步评估",
    severity: "success",
  },
};

const repeatabilityReasonLabels: Record<string, string> = {
  MINIMUM_SAMPLE_NOT_MET: "样本未达到最低数量",
  RISK_BASIS_INCOMPLETE: "冻结风险 R 基准不完整",
  NET_EXPECTANCY_NOT_POSITIVE: "费用后累计 R 不为正",
  PROFIT_FACTOR_MARGIN_NOT_MET: "盈利因子余量不足",
  BEST_TRADE_DEPENDENCE: "结果依赖最佳单笔",
  EARLY_SEGMENT_NOT_POSITIVE: "前段样本不为正",
  RECENT_SEGMENT_NOT_POSITIVE: "最近样本不为正",
  MEAN_R_CONFIDENCE_NOT_POSITIVE: "平均 R 置信下界不为正",
};

export function decisionEvidenceUsdt(value: string | null | undefined): string {
  if (value === null || value === undefined || value.trim().length === 0) {
    return "未知";
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "未知";
  return `${new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 6,
    signDisplay: "exceptZero",
  }).format(parsed)} USDT`;
}

export function decisionEvidencePercent(value: string | null | undefined): string {
  if (value === null || value === undefined || value.trim().length === 0) {
    return "未知";
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "未知";
  return `${new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
    signDisplay: "exceptZero",
  }).format(parsed)}%`;
}

export function decisionEvidenceRatio(value: string | null | undefined): string {
  if (value === null || value === undefined || value.trim().length === 0) {
    return "未知";
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "未知";
  return `${new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 3,
  }).format(parsed)} : 1`;
}

export function decisionEvidenceR(value: string | null | undefined): string {
  if (value === null || value === undefined || value.trim().length === 0) {
    return "未知";
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "未知";
  return `${new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
    signDisplay: "exceptZero",
  }).format(parsed)}R`;
}

export function decisionEvidenceCurrentStreak(
  kind: "WIN" | "LOSS" | "FLAT" | null,
  count: number,
): string {
  if (kind === null || count <= 0) return "无可比交易";
  const label = kind === "WIN" ? "连盈" : kind === "LOSS" ? "连亏" : "连续持平";
  return `${label} ${count} 笔`;
}

export default function DecisionEvidencePanel({
  evidence,
  pending,
  failed,
  qualificationExportAvailable = false,
  qualificationExportPending = false,
  qualificationExportTarget = null,
  qualificationExportError = null,
  qualificationExportSuccess = null,
  onExportQualification,
}: {
  evidence: DecisionEvidence | undefined;
  pending: boolean;
  failed: boolean;
  qualificationExportAvailable?: boolean;
  qualificationExportPending?: boolean;
  qualificationExportTarget?: QualificationTarget | null;
  qualificationExportError?: string | null;
  qualificationExportSuccess?: {
    target: QualificationTarget;
    digest: string;
  } | null;
  onExportQualification?: (target: QualificationTarget) => void;
}) {
  const qualificationCandidate = Boolean(
    evidence
    && evidence.intent === "PROFIT_SEEKING"
    && evidence.repeatability.status === "EVIDENCE_CANDIDATE"
    && evidence.sample_traceability_complete
    && evidence.sample_identity_digest,
  );
  return (
    <Box
      component="section"
      aria-labelledby="decision-evidence-title"
      sx={{ ...surfaceFrameSx, p: { xs: 1.5, sm: 2 }, mt: 2 }}
    >
      <Typography id="decision-evidence-title" variant="h3">
        完全同类历史证据
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mt: .5 }}>
        只比较同环境、同工具、同方向、同决策依据、同参数摘要和同交易形态；直接执行还必须是同一交易剧本，不会用“相近但不同”的订单凑样本。
      </Typography>
      {pending && <LinearProgress aria-label="正在核对完全同类历史样本" sx={{ mt: 1.5 }} />}
      {failed && <Alert severity="warning" variant="outlined" sx={{ mt: 1.5 }}>
        历史证据当前不可读。不能把读取失败解释为“没有历史亏损”或“策略安全”；本次计划仍只能按探索性风险对待。
      </Alert>}
      {evidence && <>
        {evidence.playbook_ref && <Chip
          size="small"
          variant="outlined"
          label={`交易剧本 ${evidence.playbook_ref}`}
          sx={{ mt: 1.25 }}
        />}
        <Alert
          severity={gradePresentation[evidence.evidence_grade].severity}
          variant="outlined"
          sx={{ mt: 1.5 }}
        >
          <strong>{gradePresentation[evidence.evidence_grade].label}。</strong>
          {evidence.intent === "VALIDATION"
            ? " 本计划的目的不是获取市场收益；即使盈利，也不得计入策略胜率和期望。"
            : ` 当前找到 ${evidence.comparable_trade_count} 笔费用后可比交易；${evidence.repeated_sample_floor} 笔只是进入重复观察的最低数量，不是 Alpha 证明。`}
        </Alert>
        <FactGrid
          columns={3}
          dense
          facts={[
            { label: "可比交易", value: String(evidence.metrics.trade_count), note: `${evidence.matched_review_count} 条同签名复盘，排除 ${evidence.excluded_review_count} 条` },
            { label: "胜 / 负 / 平", value: `${evidence.metrics.wins} / ${evidence.metrics.losses} / ${evidence.metrics.flat}` },
            { label: "累计费用后盈亏", value: decisionEvidenceUsdt(evidence.metrics.net_pnl) },
            { label: "单笔平均", value: decisionEvidenceUsdt(evidence.metrics.average_net_pnl) },
            { label: "名义金额回报", value: decisionEvidencePercent(evidence.metrics.notional_return_percent), note: "净盈亏 ÷ 全部入场名义金额；不是账户收益率" },
            { label: "盈利因子", value: decisionEvidenceRatio(evidence.metrics.profit_factor) },
            { label: "累计手续费", value: decisionEvidenceUsdt(evidence.metrics.commission) },
            { label: "样本路径最大回撤", value: decisionEvidenceUsdt(evidence.metrics.maximum_drawdown) },
            { label: "最差单笔", value: decisionEvidenceUsdt(evidence.metrics.worst_trade_net_pnl) },
            { label: "最长连盈 / 连亏", value: `${evidence.metrics.longest_winning_streak} / ${evidence.metrics.longest_losing_streak} 笔` },
            { label: "当前同剧本连续结果", value: decisionEvidenceCurrentStreak(evidence.metrics.current_streak_kind, evidence.metrics.current_streak_count) },
          ]}
        />
        {evidence.metrics.worst_trade_net_pnl !== null && <Alert severity="info" variant="outlined" sx={{ mt: 1.5 }}>
          若事后剔除最差一笔，累计净盈亏会变为 {decisionEvidenceUsdt(evidence.metrics.net_pnl_without_worst_trade)}；
          该笔占全部亏损 {decisionEvidencePercent(evidence.metrics.largest_loss_share_percent)}。
          这只是尾部敏感性，不能作为“本来会盈利”的业绩；只有交易前已经固定且真实执行的规则，才能证明该损失可避免。
        </Alert>}
        <Box sx={{ borderTop: 1, borderColor: "divider", mt: 1.75, pt: 1.5 }}>
          <Typography variant="subtitle2">同剧本可重复性筛查</Typography>
          <Alert
            severity={repeatabilityPresentation[evidence.repeatability.status].severity}
            variant="outlined"
            sx={{ mt: 1 }}
          >
            <strong>{repeatabilityPresentation[evidence.repeatability.status].label}。</strong>
            {evidence.repeatability.status === "NOT_APPLICABLE_VALIDATION"
              ? " 本次只验证软件、机制或执行闭环，盈亏不能取得策略收益证据。"
              : evidence.repeatability.status === "EVIDENCE_CANDIDATE"
                ? " 当前结果同时通过样本、风险归一化、最佳单笔依赖、前后时间段和置信下界筛查；这仍不开放实盘、加本金或提高风险。"
                : ` 当前有 ${evidence.repeatability.reason_codes.length} 项未满足；继续按同一规则积累前瞻样本，不能靠改标签或删除亏损过关。`}
          </Alert>
          <FactGrid
            columns={3}
            dense
            facts={[
              {
                label: "冻结风险覆盖",
                value: `${evidence.repeatability.risk_basis_trade_count} / ${evidence.metrics.trade_count} 笔`,
                note: "每笔费用后盈亏 ÷ 交易前冻结最大允许损失",
              },
              {
                label: "累计 / 平均 R",
                value: `${decisionEvidenceR(evidence.repeatability.net_r_multiple)} / ${decisionEvidenceR(evidence.repeatability.average_r_multiple)}`,
              },
              {
                label: "剔除最佳单笔后",
                value: decisionEvidenceR(evidence.repeatability.net_r_without_best_trade),
                note: evidence.metrics.best_trade_net_pnl === null
                  ? "当前没有盈利单"
                  : `最佳单笔 ${decisionEvidenceUsdt(evidence.metrics.best_trade_net_pnl)}，占全部盈利 ${decisionEvidencePercent(evidence.metrics.largest_win_share_percent)}`,
              },
              {
                label: `前段 ${evidence.repeatability.early_segment_trade_count} 笔`,
                value: decisionEvidenceR(evidence.repeatability.early_segment_net_r),
              },
              {
                label: `最近 ${evidence.repeatability.recent_segment_trade_count} 笔`,
                value: decisionEvidenceR(evidence.repeatability.recent_segment_net_r),
              },
              {
                label: `${evidence.repeatability.confidence_level_percent}% 平均 R 下界`,
                value: decisionEvidenceR(evidence.repeatability.mean_r_lower_confidence_bound),
                note: evidence.repeatability.bootstrap_block_length === null
                  ? "风险基准不足，无法计算"
                  : `移动区块 ${evidence.repeatability.bootstrap_block_length} 笔；只用于保守筛查`,
              },
            ]}
          />
          {evidence.repeatability.reason_codes.length > 0 && <Stack direction="row" spacing={.75} useFlexGap sx={{ mt: 1, flexWrap: "wrap" }}>
            <Typography variant="caption" color="text.secondary" sx={{ alignSelf: "center" }}>未满足：</Typography>
            {evidence.repeatability.reason_codes.map((reason) => (
              <Chip
                key={reason}
                size="small"
                variant="outlined"
                label={repeatabilityReasonLabels[reason] ?? reason}
              />
            ))}
          </Stack>}
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
            策略 {evidence.repeatability.policy_version}：至少 {evidence.repeatability.minimum_trade_count} 笔，盈利因子门槛 {evidence.repeatability.minimum_profit_factor}；通过也没有实盘晋级或资本放大授权。
          </Typography>
        </Box>
        {qualificationExportAvailable && <Box sx={{ borderTop: 1, borderColor: "divider", mt: 1.75, pt: 1.5 }}>
          <Typography variant="subtitle2">Demo → Live 证据交接</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: .5 }}>
            仅将这次完全同类样本与筛查结果冻结为本地 JSON。下载不会创建实盘计划、不会开闸，也不会授权加本金；带单账户与个人账户必须分别导出和配置。
          </Typography>
          {!qualificationCandidate && <Alert severity="info" variant="outlined" sx={{ mt: 1 }}>
            只有达到“同剧本证据可进入下一步评估”、覆盖全部样本身份且每笔都有冻结风险基准时才能导出。继续交易不会自动晋级，必须保持同一交易前规则并完成复盘。
          </Alert>}
          {qualificationExportError && <Alert severity="error" variant="outlined" sx={{ mt: 1 }}>
            证据包未导出：{qualificationExportError}
          </Alert>}
          {qualificationExportSuccess && <Alert severity="success" variant="outlined" sx={{ mt: 1 }}>
            已下载面向{qualificationTargetLabels[qualificationExportSuccess.target]}的证据包；配置时必须同时固定文件绝对路径与摘要 {qualificationExportSuccess.digest.slice(0, 16)}…，并把文件保存在 Git 仓库之外。
          </Alert>}
          <Stack direction={{ xs: "column", sm: "row" }} spacing={1} sx={{ mt: 1.25 }}>
            {(["USDM_COPY_LEAD", "USDM_PERSONAL"] as const).map((target) => (
              <Button
                key={target}
                variant="outlined"
                startIcon={<DownloadOutlined />}
                disabled={!qualificationCandidate || qualificationExportPending || !onExportQualification}
                onClick={() => onExportQualification?.(target)}
              >
                {qualificationExportPending && qualificationExportTarget === target
                  ? `正在生成${qualificationTargetLabels[target]}证据…`
                  : `导出至${qualificationTargetLabels[target]}`}
              </Button>
            ))}
          </Stack>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
            Live 只接受 7 天内、目标账户类型和完整计划身份完全匹配、且摘要与所有者配置一致的单一证据包；通过仍只是激活必要输入。
          </Typography>
        </Box>}
        {Object.keys(evidence.exclusions).length > 0 && <Stack direction="row" spacing={.75} useFlexGap sx={{ mt: 1.5, flexWrap: "wrap" }}>
          <Typography variant="caption" color="text.secondary" sx={{ alignSelf: "center" }}>未计入：</Typography>
          {Object.entries(evidence.exclusions).map(([reason, count]) => (
            <Chip
              key={reason}
              size="small"
              variant="outlined"
              label={`${exclusionLabels[reason] ?? reason} ${count}`}
            />
          ))}
        </Stack>}
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1.25 }}>
          {evidence.source_cutoff
            ? `复盘证据截止 ${formatUserVisibleTime(evidence.source_cutoff)}`
            : "当前没有同签名复盘截止点"}；该结果不具有加本金或放大风险的授权效力。
        </Typography>
      </>}
    </Box>
  );
}
