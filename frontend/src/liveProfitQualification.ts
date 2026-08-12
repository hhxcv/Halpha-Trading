import type { LiveProfitQualification } from "./api/client";


type QualificationPresentation = {
  label: string;
  severity: "info" | "warning" | "error" | "success";
};

const statusPresentation: Record<
  LiveProfitQualification["status"],
  QualificationPresentation
> = {
  NOT_APPLICABLE_DEMO: { label: "Demo 不需要跨环境证据", severity: "info" },
  NOT_APPLICABLE_VALIDATION: { label: "验证目的不需要收益证据", severity: "info" },
  NOT_APPLICABLE_POSITION_DISPOSITION: { label: "既有持仓处置不需要收益证据", severity: "info" },
  NOT_CONFIGURED: { label: "尚未配置 Demo 证据包", severity: "warning" },
  INVALID: { label: "证据包不可验证", severity: "error" },
  NOT_MATCHED: { label: "证据包与当前计划不匹配", severity: "error" },
  STALE: { label: "Demo 证据已超过 7 天", severity: "warning" },
  AMBIGUOUS: { label: "存在多个当前匹配证据包", severity: "error" },
  ELIGIBLE_INPUT: { label: "Demo 证据输入已核验", severity: "success" },
};

export const liveProfitQualificationBlockerLabels: Record<string, string> = {
  LIVE_PROFIT_QUALIFICATION_PLAN_INTENT_INVALID: "计划缺少明确的盈利导向身份",
  LIVE_PROFIT_QUALIFICATION_TARGET_INVALID: "目标实盘账户类型不受支持",
  LIVE_PROFIT_QUALIFICATION_NOT_CONFIGURED: "未配置证据包绝对路径与固定摘要",
  LIVE_PROFIT_QUALIFICATION_ARTIFACT_UNREADABLE: "证据文件不存在、过大、格式错误或不可读",
  LIVE_PROFIT_QUALIFICATION_DIGEST_MISMATCH: "文件内容与所有者固定摘要不一致",
  LIVE_PROFIT_QUALIFICATION_COHORT_MISMATCH: "工具、方向、规则、参数、交易形态或剧本不匹配",
  LIVE_PROFIT_QUALIFICATION_TIME_INVALID: "证据时间晚于当前事实或时间格式无效",
  LIVE_PROFIT_QUALIFICATION_EVIDENCE_STALE: "可比样本最新事实已超过 7 天",
  LIVE_PROFIT_QUALIFICATION_MULTIPLE_CURRENT_ARTIFACTS: "同一计划匹配多个当前证据，无法唯一选择",
};

export function liveProfitQualificationPresentation(
  qualification: LiveProfitQualification,
): QualificationPresentation {
  return statusPresentation[qualification.status];
}

export function liveProfitQualificationSatisfied(
  qualification: LiveProfitQualification | null | undefined,
): boolean {
  return Boolean(
    qualification
    && (qualification.required === false || qualification.eligible_input === true),
  );
}

export function qualificationFractionPercent(value: string): string {
  const fraction = Number(value);
  if (!Number.isFinite(fraction)) return "未知";
  return `${new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 3,
  }).format(fraction * 100)}%`;
}
