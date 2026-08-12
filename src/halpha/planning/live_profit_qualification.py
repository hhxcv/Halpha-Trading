"""Demo evidence handoff required before a profit-seeking Live activation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from halpha.capital.models import (
    ACCOUNT_PORTFOLIO_RISK_POLICY_VERSION,
    DAILY_LOSS_STOP_FRACTION,
    MAX_CORRELATED_EXPOSURE_FRACTION,
    MAX_GROSS_EXPOSURE_FRACTION,
    MAX_INSTRUMENT_EXPOSURE_FRACTION,
    MAX_OPEN_RISK_FRACTION,
    MAX_PLAN_LOSS_FRACTION,
    ROLLING_DRAWDOWN_STOP_FRACTION,
    WEEKLY_LOSS_STOP_FRACTION,
    EnvironmentKind,
)
from halpha.domain_values import (
    canonical_decimal,
    content_digest,
    decimal_from_string,
)
from halpha.outcomes.repeatability import DEFAULT_PLAYBOOK_REPEATABILITY_POLICY
from halpha.planning.models import (
    PLAYBOOK_REF_PATTERN,
    PlanDecisionIntent,
    PlanSetupFamily,
    TradePlanVersion,
)
from halpha.planning.registry import DecisionBasisKind, Direction


PLAYBOOK_QUALIFICATION_SCHEMA = "HALPHA_PLAYBOOK_QUALIFICATION@1"
PLAYBOOK_QUALIFICATION_MAX_AGE = timedelta(days=7)
PLAYBOOK_QUALIFICATION_MAX_BYTES = 1_000_000
PLAYBOOK_QUALIFICATION_FUTURE_TOLERANCE = timedelta(minutes=5)
EXPECTED_DEMO_SOURCE_ENVIRONMENT_ID = "binance-demo-primary"
LIVE_TARGET_ACCOUNT_TYPES = frozenset({"USDM_COPY_LEAD", "USDM_PERSONAL"})


class QualificationReference(Protocol):
    artifact_path: str
    expected_content_digest: str


class QualificationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class PlaybookQualificationCohort(QualificationModel):
    venue_ref: Literal["BINANCE_USDM"] = "BINANCE_USDM"
    instrument_ref: str
    direction: Direction
    decision_basis_kind: DecisionBasisKind
    decision_basis_ref: str
    parameter_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent: Literal["PROFIT_SEEKING"] = "PROFIT_SEEKING"
    setup_family: PlanSetupFamily
    playbook_ref: str | None = Field(
        default=None,
        min_length=1,
        max_length=96,
        pattern=PLAYBOOK_REF_PATTERN,
    )

    @model_validator(mode="after")
    def playbook_matches_basis(self) -> "PlaybookQualificationCohort":
        if self.decision_basis_kind is DecisionBasisKind.DIRECT_EXECUTION:
            if self.playbook_ref is None:
                raise ValueError("PLAYBOOK_QUALIFICATION_PLAYBOOK_REQUIRED")
        elif self.playbook_ref is not None:
            raise ValueError("PLAYBOOK_QUALIFICATION_PLAYBOOK_UNEXPECTED")
        return self


class PlaybookQualificationSampleRef(QualificationModel):
    review_id: str = Field(min_length=1, max_length=160)
    review_version: int = Field(ge=1)
    review_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    fact_cutoff: datetime

    @field_validator("fact_cutoff")
    @classmethod
    def cutoff_is_aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("PLAYBOOK_QUALIFICATION_TIME_INVALID")
        return value.astimezone(UTC)


class PlaybookQualificationArtifact(QualificationModel):
    schema_: Literal[PLAYBOOK_QUALIFICATION_SCHEMA] = Field(
        default=PLAYBOOK_QUALIFICATION_SCHEMA,
        alias="schema",
    )
    source_environment_kind: Literal["DEMO"] = "DEMO"
    source_environment_id: str
    source_product_build_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_venue_account_type: Literal["USDM_COPY_LEAD", "USDM_PERSONAL"]
    issued_at: datetime
    evidence_cutoff: datetime
    cohort: PlaybookQualificationCohort
    sample_review_refs: tuple[PlaybookQualificationSampleRef, ...]
    sample_identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    comparable_trade_count: int = Field(ge=1)
    risk_basis_trade_count: int = Field(ge=1)
    screen_status: Literal["EVIDENCE_CANDIDATE"] = "EVIDENCE_CANDIDATE"
    repeatability_policy_version: str
    minimum_trade_count: int = Field(ge=1)
    minimum_profit_factor: str
    gross_profit: str
    gross_loss: str
    profit_factor: str | None
    net_r_multiple: str
    average_r_multiple: str
    net_r_without_best_trade: str
    early_segment_net_r: str
    recent_segment_net_r: str
    mean_r_lower_confidence_bound: str
    live_activation_authority: Literal[False] = False
    capital_scaling_authority: Literal[False] = False
    limitations: tuple[str, ...] = Field(min_length=1)

    @field_validator("issued_at", "evidence_cutoff")
    @classmethod
    def times_are_aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("PLAYBOOK_QUALIFICATION_TIME_INVALID")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def sample_and_screen_are_complete(self) -> "PlaybookQualificationArtifact":
        policy = DEFAULT_PLAYBOOK_REPEATABILITY_POLICY
        expected_minimum_profit_factor = canonical_decimal(
            policy.minimum_profit_factor
        )
        if self.source_environment_id != EXPECTED_DEMO_SOURCE_ENVIRONMENT_ID:
            raise ValueError("PLAYBOOK_QUALIFICATION_SOURCE_INVALID")
        if self.issued_at < self.evidence_cutoff:
            raise ValueError("PLAYBOOK_QUALIFICATION_TIME_INVALID")
        if len(self.sample_review_refs) != self.comparable_trade_count:
            raise ValueError("PLAYBOOK_QUALIFICATION_SAMPLE_COUNT_MISMATCH")
        if len(
            {(item.review_id, item.review_version) for item in self.sample_review_refs}
        ) != len(self.sample_review_refs):
            raise ValueError("PLAYBOOK_QUALIFICATION_SAMPLE_IDENTITY_DUPLICATED")
        if self.evidence_cutoff != max(
            item.fact_cutoff for item in self.sample_review_refs
        ):
            raise ValueError("PLAYBOOK_QUALIFICATION_SAMPLE_CUTOFF_MISMATCH")
        if self.risk_basis_trade_count != self.comparable_trade_count:
            raise ValueError("PLAYBOOK_QUALIFICATION_RISK_BASIS_INCOMPLETE")
        if (
            self.repeatability_policy_version != policy.policy_version
            or self.minimum_trade_count != policy.minimum_trade_count
            or self.minimum_profit_factor != expected_minimum_profit_factor
            or self.comparable_trade_count < policy.minimum_trade_count
        ):
            raise ValueError("PLAYBOOK_QUALIFICATION_POLICY_MISMATCH")
        if content_digest(self.sample_review_refs) != self.sample_identity_digest:
            raise ValueError("PLAYBOOK_QUALIFICATION_SAMPLE_DIGEST_MISMATCH")

        gross_profit = _artifact_decimal(
            self.gross_profit,
            "PLAYBOOK_QUALIFICATION_GROSS_PROFIT_INVALID",
            positive=True,
        )
        gross_loss = _artifact_decimal(
            self.gross_loss,
            "PLAYBOOK_QUALIFICATION_GROSS_LOSS_INVALID",
            non_negative=True,
        )
        if gross_loss == 0:
            if self.profit_factor is not None:
                raise ValueError("PLAYBOOK_QUALIFICATION_PROFIT_FACTOR_INVALID")
        else:
            if self.profit_factor is None:
                raise ValueError("PLAYBOOK_QUALIFICATION_PROFIT_FACTOR_INVALID")
            profit_factor = _artifact_decimal(
                self.profit_factor,
                "PLAYBOOK_QUALIFICATION_PROFIT_FACTOR_INVALID",
                positive=True,
            )
            if (
                profit_factor != gross_profit / gross_loss
                or profit_factor < policy.minimum_profit_factor
            ):
                raise ValueError("PLAYBOOK_QUALIFICATION_PROFIT_FACTOR_INVALID")

        for value in (
            self.net_r_multiple,
            self.average_r_multiple,
            self.net_r_without_best_trade,
            self.early_segment_net_r,
            self.recent_segment_net_r,
            self.mean_r_lower_confidence_bound,
        ):
            _artifact_decimal(
                value,
                "PLAYBOOK_QUALIFICATION_REPEATABILITY_INVALID",
                positive=True,
            )
        return self


QualificationStatusValue = Literal[
    "NOT_APPLICABLE_DEMO",
    "NOT_APPLICABLE_VALIDATION",
    "NOT_APPLICABLE_POSITION_DISPOSITION",
    "NOT_CONFIGURED",
    "INVALID",
    "NOT_MATCHED",
    "STALE",
    "AMBIGUOUS",
    "ELIGIBLE_INPUT",
]


class LiveProfitQualificationStatus(QualificationModel):
    status: QualificationStatusValue
    required: bool
    eligible_input: bool
    blocker_codes: tuple[str, ...]
    artifact_content_digest: str | None = None
    source_environment_id: str | None = None
    evidence_cutoff: datetime | None = None
    target_venue_account_type: str | None = None
    comparable_trade_count: int | None = None
    risk_basis_trade_count: int | None = None
    repeatability_policy_version: str | None = None
    maximum_evidence_age_days: int = PLAYBOOK_QUALIFICATION_MAX_AGE.days
    portfolio_risk_policy_version: str = ACCOUNT_PORTFOLIO_RISK_POLICY_VERSION
    portfolio_max_plan_loss_fraction: str = MAX_PLAN_LOSS_FRACTION
    portfolio_max_open_risk_fraction: str = MAX_OPEN_RISK_FRACTION
    portfolio_max_gross_exposure_fraction: str = MAX_GROSS_EXPOSURE_FRACTION
    portfolio_max_instrument_exposure_fraction: str = (
        MAX_INSTRUMENT_EXPOSURE_FRACTION
    )
    portfolio_max_correlated_exposure_fraction: str = (
        MAX_CORRELATED_EXPOSURE_FRACTION
    )
    portfolio_daily_loss_stop_fraction: str = DAILY_LOSS_STOP_FRACTION
    portfolio_weekly_loss_stop_fraction: str = WEEKLY_LOSS_STOP_FRACTION
    portfolio_rolling_drawdown_stop_fraction: str = (
        ROLLING_DRAWDOWN_STOP_FRACTION
    )
    live_activation_authority: Literal[False] = False
    capital_scaling_authority: Literal[False] = False
    limitations: tuple[str, ...] = (
        "该结果只是盈利导向 Live 激活的必要证据输入，不是用户决定、写门或交易所动作授权。",
        "Demo 证据不能替代 Live 账户事实、CAP、EXE、保护、退出和用户明确激活。",
        "微型实盘结果仍须独立复盘；通过不能自动提高风险或增加本金。",
    )

    @property
    def admission_satisfied(self) -> bool:
        return not self.required or self.eligible_input


def build_playbook_qualification_artifact(
    decision_evidence: Mapping[str, Any],
    *,
    source_environment_id: str,
    source_product_build_id: str,
    target_venue_account_type: str,
    decision_basis_kind: DecisionBasisKind,
    issued_at: datetime,
) -> tuple[PlaybookQualificationArtifact, str]:
    """Freeze a candidate screen into an auditable, content-addressed handoff."""

    if source_environment_id != EXPECTED_DEMO_SOURCE_ENVIRONMENT_ID:
        raise ValueError("PLAYBOOK_QUALIFICATION_EXPORT_REQUIRES_DEMO")
    if target_venue_account_type not in LIVE_TARGET_ACCOUNT_TYPES:
        raise ValueError("PLAYBOOK_QUALIFICATION_TARGET_INVALID")
    if decision_evidence.get("intent") != PlanDecisionIntent.PROFIT_SEEKING.value:
        raise ValueError("PLAYBOOK_QUALIFICATION_PROFIT_INTENT_REQUIRED")
    repeatability = decision_evidence.get("repeatability")
    metrics = decision_evidence.get("metrics")
    sample_refs_raw = decision_evidence.get("sample_review_refs")
    if not isinstance(repeatability, Mapping) or not isinstance(metrics, Mapping):
        raise ValueError("PLAYBOOK_QUALIFICATION_EVIDENCE_INVALID")
    if repeatability.get("status") != "EVIDENCE_CANDIDATE":
        raise ValueError("PLAYBOOK_QUALIFICATION_EVIDENCE_NOT_CANDIDATE")
    if decision_evidence.get("sample_traceability_complete") is not True:
        raise ValueError("PLAYBOOK_QUALIFICATION_TRACEABILITY_INCOMPLETE")
    if not isinstance(sample_refs_raw, Sequence) or isinstance(
        sample_refs_raw,
        (str, bytes),
    ):
        raise ValueError("PLAYBOOK_QUALIFICATION_TRACEABILITY_INCOMPLETE")
    sample_refs = tuple(
        PlaybookQualificationSampleRef.model_validate(item)
        for item in sample_refs_raw
    )
    if not sample_refs:
        raise ValueError("PLAYBOOK_QUALIFICATION_TRACEABILITY_INCOMPLETE")
    expected_sample_digest = content_digest(sample_refs)
    if decision_evidence.get("sample_identity_digest") != expected_sample_digest:
        raise ValueError("PLAYBOOK_QUALIFICATION_SAMPLE_DIGEST_MISMATCH")
    evidence_cutoff = max(item.fact_cutoff for item in sample_refs)
    required_screen_values = {
        key: repeatability.get(key)
        for key in (
            "net_r_multiple",
            "average_r_multiple",
            "net_r_without_best_trade",
            "early_segment_net_r",
            "recent_segment_net_r",
            "mean_r_lower_confidence_bound",
        )
    }
    required_metrics = {
        key: metrics.get(key)
        for key in ("gross_profit", "gross_loss")
    }
    if any(value is None for value in required_screen_values.values()) or any(
        value is None for value in required_metrics.values()
    ):
        raise ValueError("PLAYBOOK_QUALIFICATION_EVIDENCE_INCOMPLETE")
    artifact = PlaybookQualificationArtifact(
        source_environment_id=source_environment_id,
        source_product_build_id=source_product_build_id,
        target_venue_account_type=target_venue_account_type,
        issued_at=issued_at,
        evidence_cutoff=evidence_cutoff,
        cohort=PlaybookQualificationCohort(
            instrument_ref=str(decision_evidence["instrument_ref"]),
            direction=Direction(str(decision_evidence["direction"])),
            decision_basis_kind=decision_basis_kind,
            decision_basis_ref=str(decision_evidence["decision_basis_ref"]),
            parameter_digest=str(decision_evidence["parameter_digest"]),
            setup_family=PlanSetupFamily(str(decision_evidence["setup_family"])),
            playbook_ref=(
                str(decision_evidence["playbook_ref"])
                if decision_evidence.get("playbook_ref") is not None
                else None
            ),
        ),
        sample_review_refs=sample_refs,
        sample_identity_digest=expected_sample_digest,
        decision_evidence_digest=content_digest(decision_evidence),
        comparable_trade_count=int(decision_evidence["comparable_trade_count"]),
        risk_basis_trade_count=int(repeatability["risk_basis_trade_count"]),
        repeatability_policy_version=str(repeatability["policy_version"]),
        minimum_trade_count=int(repeatability["minimum_trade_count"]),
        minimum_profit_factor=str(repeatability["minimum_profit_factor"]),
        gross_profit=str(required_metrics["gross_profit"]),
        gross_loss=str(required_metrics["gross_loss"]),
        profit_factor=(
            str(metrics["profit_factor"])
            if metrics.get("profit_factor") is not None
            else None
        ),
        net_r_multiple=str(required_screen_values["net_r_multiple"]),
        average_r_multiple=str(required_screen_values["average_r_multiple"]),
        net_r_without_best_trade=str(
            required_screen_values["net_r_without_best_trade"]
        ),
        early_segment_net_r=str(required_screen_values["early_segment_net_r"]),
        recent_segment_net_r=str(required_screen_values["recent_segment_net_r"]),
        mean_r_lower_confidence_bound=str(
            required_screen_values["mean_r_lower_confidence_bound"]
        ),
        limitations=(
            "证据包固定 Demo 的完整同类复盘身份和筛查结果；内容摘要变化后必须重新由所有者选择。",
            "证据包只作为目标 Live 上下文的必要输入，不创建计划、激活、写门或交易所动作。",
            "证据超过新鲜度边界、剧本变化或目标上下文不符时必须重新从 Demo 形成。",
        ),
    )
    return artifact, content_digest(artifact)


def evaluate_live_profit_qualification(
    version: TradePlanVersion,
    *,
    environment_kind: EnvironmentKind,
    target_venue_account_type: str,
    references: Sequence[QualificationReference],
    observed_at: datetime,
) -> LiveProfitQualificationStatus:
    """Match one immutable plan to one fresh, explicitly pinned Demo artifact."""

    if environment_kind is EnvironmentKind.DEMO:
        return _status("NOT_APPLICABLE_DEMO", required=False)
    if getattr(version, "position_alignment", None) is not None:
        return _status("NOT_APPLICABLE_POSITION_DISPOSITION", required=False)
    context = version.decision_context
    if context is not None and context.intent is PlanDecisionIntent.VALIDATION:
        return _status("NOT_APPLICABLE_VALIDATION", required=False)
    if context is None or context.intent is not PlanDecisionIntent.PROFIT_SEEKING:
        return _status(
            "INVALID",
            required=True,
            blocker_codes=("LIVE_PROFIT_QUALIFICATION_PLAN_INTENT_INVALID",),
        )
    if target_venue_account_type not in LIVE_TARGET_ACCOUNT_TYPES:
        return _status(
            "INVALID",
            required=True,
            blocker_codes=("LIVE_PROFIT_QUALIFICATION_TARGET_INVALID",),
        )
    if not references:
        return _status(
            "NOT_CONFIGURED",
            required=True,
            blocker_codes=("LIVE_PROFIT_QUALIFICATION_NOT_CONFIGURED",),
        )
    if observed_at.utcoffset() is None:
        return _status(
            "INVALID",
            required=True,
            blocker_codes=("LIVE_PROFIT_QUALIFICATION_TIME_INVALID",),
        )
    observed_utc = observed_at.astimezone(UTC)
    artifacts: list[tuple[PlaybookQualificationArtifact, str]] = []
    for reference in references:
        try:
            artifact = _read_artifact(Path(reference.artifact_path))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            return _status(
                "INVALID",
                required=True,
                blocker_codes=("LIVE_PROFIT_QUALIFICATION_ARTIFACT_UNREADABLE",),
            )
        actual_digest = content_digest(artifact)
        if actual_digest != reference.expected_content_digest:
            return _status(
                "INVALID",
                required=True,
                blocker_codes=("LIVE_PROFIT_QUALIFICATION_DIGEST_MISMATCH",),
            )
        artifacts.append((artifact, actual_digest))

    matching = [
        item
        for item in artifacts
        if _artifact_matches_plan(
            item[0],
            version,
            target_venue_account_type=target_venue_account_type,
        )
    ]
    if not matching:
        return _status(
            "NOT_MATCHED",
            required=True,
            blocker_codes=("LIVE_PROFIT_QUALIFICATION_COHORT_MISMATCH",),
        )

    current: list[tuple[PlaybookQualificationArtifact, str]] = []
    time_invalid = False
    for artifact, digest in matching:
        if (
            artifact.evidence_cutoff
            > observed_utc + PLAYBOOK_QUALIFICATION_FUTURE_TOLERANCE
            or artifact.issued_at
            > observed_utc + PLAYBOOK_QUALIFICATION_FUTURE_TOLERANCE
        ):
            time_invalid = True
            continue
        if observed_utc - artifact.evidence_cutoff <= PLAYBOOK_QUALIFICATION_MAX_AGE:
            current.append((artifact, digest))
    if time_invalid:
        return _status(
            "INVALID",
            required=True,
            blocker_codes=("LIVE_PROFIT_QUALIFICATION_TIME_INVALID",),
        )
    if not current:
        newest = max(matching, key=lambda item: item[0].evidence_cutoff)
        return _status_from_artifact(
            "STALE",
            newest,
            eligible=False,
            blocker_codes=("LIVE_PROFIT_QUALIFICATION_EVIDENCE_STALE",),
        )
    if len(current) != 1:
        newest = max(current, key=lambda item: item[0].evidence_cutoff)
        return _status_from_artifact(
            "AMBIGUOUS",
            newest,
            eligible=False,
            blocker_codes=("LIVE_PROFIT_QUALIFICATION_MULTIPLE_CURRENT_ARTIFACTS",),
        )
    return _status_from_artifact(
        "ELIGIBLE_INPUT",
        current[0],
        eligible=True,
        blocker_codes=(),
    )


def require_live_profit_qualification(
    status: LiveProfitQualificationStatus,
) -> None:
    if status.admission_satisfied:
        return
    raise ValueError(
        status.blocker_codes[0]
        if status.blocker_codes
        else "LIVE_PROFIT_QUALIFICATION_REQUIRED"
    )


def _read_artifact(path: Path) -> PlaybookQualificationArtifact:
    if _path_has_indirection(path) or not path.is_file():
        raise ValueError("PLAYBOOK_QUALIFICATION_ARTIFACT_INVALID")
    raw = path.read_bytes()
    if len(raw) > PLAYBOOK_QUALIFICATION_MAX_BYTES:
        raise ValueError("PLAYBOOK_QUALIFICATION_ARTIFACT_INVALID")
    payload = json.loads(raw.decode("utf-8"))
    return PlaybookQualificationArtifact.model_validate(payload)


def _path_has_indirection(path: Path) -> bool:
    for item in (path, *path.parents):
        is_junction = getattr(item, "is_junction", None)
        if item.is_symlink() or (callable(is_junction) and is_junction()):
            return True
    return False


def _artifact_matches_plan(
    artifact: PlaybookQualificationArtifact,
    version: TradePlanVersion,
    *,
    target_venue_account_type: str,
) -> bool:
    context = version.decision_context
    if context is None:
        return False
    cohort = artifact.cohort
    return (
        artifact.target_venue_account_type == target_venue_account_type
        and cohort.venue_ref == version.venue_ref
        and cohort.instrument_ref == version.instrument_ref
        and cohort.direction is version.direction
        and cohort.decision_basis_kind is version.decision_basis.kind
        and cohort.decision_basis_ref == version.decision_basis.decision_basis_ref
        and cohort.parameter_digest == version.decision_basis.parameter_digest
        and cohort.intent == context.intent.value
        and cohort.setup_family is context.setup_family
        and cohort.playbook_ref == context.playbook_ref
    )


def _status(
    status: QualificationStatusValue,
    *,
    required: bool,
    blocker_codes: tuple[str, ...] = (),
) -> LiveProfitQualificationStatus:
    return LiveProfitQualificationStatus(
        status=status,
        required=required,
        eligible_input=False,
        blocker_codes=blocker_codes,
    )


def _status_from_artifact(
    status: QualificationStatusValue,
    item: tuple[PlaybookQualificationArtifact, str],
    *,
    eligible: bool,
    blocker_codes: tuple[str, ...],
) -> LiveProfitQualificationStatus:
    artifact, digest = item
    return LiveProfitQualificationStatus(
        status=status,
        required=True,
        eligible_input=eligible,
        blocker_codes=blocker_codes,
        artifact_content_digest=digest,
        source_environment_id=artifact.source_environment_id,
        evidence_cutoff=artifact.evidence_cutoff,
        target_venue_account_type=artifact.target_venue_account_type,
        comparable_trade_count=artifact.comparable_trade_count,
        risk_basis_trade_count=artifact.risk_basis_trade_count,
        repeatability_policy_version=artifact.repeatability_policy_version,
    )


def _artifact_decimal(
    value: str,
    code: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    try:
        return decimal_from_string(
            value,
            code=code,
            positive=positive,
            non_negative=non_negative,
        )
    except ValueError:
        raise ValueError(code) from None
