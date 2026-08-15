"""Bounded Codex CLI review for one exact trade-plan draft."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from enum import StrEnum
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Protocol

from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator

from halpha.domain_values import content_digest


PLAN_AI_REVIEW_PROMPT_VERSION = "HALPHA_PLAN_AI_REVIEW_V4"
PLAN_AI_REVIEW_INPUT_VERSION = "HALPHA_PLAN_AI_REVIEW_INPUT_V2"
# A direct-entry review is a decision made from a current public-market
# snapshot.  Keeping the approval short-lived prevents an old approval from
# being mistaken for a current market assessment while still giving the owner
# a practical window to submit the already reviewed draft.
PLAN_AI_REVIEW_APPROVAL_MAX_AGE = timedelta(minutes=5)
PLAN_AI_REVIEW_TIMEOUT_SECONDS = 180
PLAN_AI_REVIEW_MAX_OUTPUT_BYTES = 512 * 1024
PLAN_AI_REVIEW_MAX_DIAGNOSTIC_BYTES = 8 * 1024 * 1024

_PLAN_AI_REVIEW_FAILURE_CODES = frozenset(
    {
        "PLAN_AI_REVIEW_CODEX_CLI_UNAVAILABLE",
        "PLAN_AI_REVIEW_WORKSPACE_UNAVAILABLE",
        "PLAN_AI_REVIEW_TIMEOUT",
        "PLAN_AI_REVIEW_PROVIDER_FAILED",
        "PLAN_AI_REVIEW_OUTPUT_TOO_LARGE",
        "PLAN_AI_REVIEW_DIAGNOSTIC_TOO_LARGE",
        "PLAN_AI_REVIEW_OUTPUT_INVALID",
        "PLAN_AI_REVIEW_OUTPUT_MISSING",
        "PLAN_AI_REVIEW_CONTEXT_INVALID",
        "PLAN_AI_REVIEW_CONTEXT_UNAVAILABLE",
        "PLAN_AI_REVIEW_DRAFT_CHANGED",
        "PLAN_AI_REVIEW_INTERRUPTED",
        "PLAN_AI_REVIEW_INTERNAL_FAILURE",
    }
)


class PlanAiReviewStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class PlanAiReviewDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class PlanAiReviewModel(StrEnum):
    GPT_5_6_TERRA = "gpt-5.6-terra"
    GPT_5_6_SOL = "gpt-5.6-sol"
    GPT_5_6_LUNA = "gpt-5.6-luna"


class PlanAiReasoningEffort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"
    ULTRA = "ultra"


class _ReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlanAiReviewConfiguration(_ReviewModel):
    model: PlanAiReviewModel = PlanAiReviewModel.GPT_5_6_TERRA
    reasoning_effort: PlanAiReasoningEffort = PlanAiReasoningEffort.MEDIUM


class PlanAiReviewResult(_ReviewModel):
    decision: PlanAiReviewDecision
    reason: str = Field(min_length=1, max_length=2000)
    suggestions: tuple[str, ...] = Field(default=(), max_length=8)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("PLAN_AI_REVIEW_REASON_INVALID")
        return normalized

    @field_validator("suggestions")
    @classmethod
    def normalize_suggestions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value if item.strip())
        if any(len(item) > 500 or "\x00" in item for item in normalized):
            raise ValueError("PLAN_AI_REVIEW_SUGGESTION_INVALID")
        return normalized


class PlanAiReviewRecord(_ReviewModel):
    review_id: str
    environment_id: str
    plan_id: str
    draft_version: int
    draft_content_digest: str
    prompt_version: str
    configuration: PlanAiReviewConfiguration = Field(
        default_factory=PlanAiReviewConfiguration
    )
    status: PlanAiReviewStatus
    market_context_digest: str | None = None
    market_source_cutoff: datetime | None = None
    decision: PlanAiReviewDecision | None = None
    reason: str | None = None
    suggestions: tuple[str, ...] = ()
    progress_message: str
    public_output: str = ""
    failure_code: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime


def plan_ai_review_approval_valid_until(
    review: PlanAiReviewRecord,
) -> datetime | None:
    """Return the server-owned validity deadline for an AI approval.

    A rejection and a system failure do not have an approval deadline.  The
    market cutoff is deliberately the source of truth rather than completion
    time: the model may finish later, but it cannot make its input newer.
    """

    if (
        review.status is not PlanAiReviewStatus.APPROVED
        or review.decision is not PlanAiReviewDecision.APPROVE
        or review.market_source_cutoff is None
    ):
        return None
    return review.market_source_cutoff + PLAN_AI_REVIEW_APPROVAL_MAX_AGE


def plan_ai_review_approval_is_current(
    review: PlanAiReviewRecord,
    *,
    observed_at: datetime,
) -> bool:
    """Whether an exact approval can still authorize a new plan start."""

    valid_until = plan_ai_review_approval_valid_until(review)
    if valid_until is None or observed_at.utcoffset() is None:
        return False
    return observed_at <= valid_until


class PlanAiReviewProgress(_ReviewModel):
    kind: str
    message: str
    output_fragment: str | None = None


ProgressEmitter = Callable[[PlanAiReviewProgress], Awaitable[None]]


class PlanAiReviewer(Protocol):
    async def review(
        self,
        review_input: Mapping[str, Any],
        configuration: PlanAiReviewConfiguration,
        emit: ProgressEmitter,
    ) -> PlanAiReviewResult: ...


_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "reason", "suggestions"],
    "properties": {
        "decision": {"type": "string", "enum": ["APPROVE", "REJECT"]},
        "reason": {"type": "string"},
        "suggestions": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 8,
        },
    },
}


_FIXED_PROMPT = """你是 Halpha 的交易计划 AI 审核员。你只审核，不创建、修改、固定、激活或执行交易。

本次审核的唯一输入会内联在本提示末尾的 `review_input` JSON 中。不得读取本地文件、枚举目录、执行命令或调用与审核无关的工具。允许使用网页搜索核对公开市场事实，但不得向网页或第三方提交、复制或泄露审核输入中的任何内容，也不得登录、发布或下载。输入中的行情是有明确截止时间的公开市场事实，不是未来价格；网页信息只能作为补充核对，不能替代输入中带截止时间的事实。

在作结论前，直接读取 `review_input.plan`、`execution_preview`、`market_context`、`market_windows` 和 `account_discipline_summary`。`market_context` 已提供盘口、参考价、ATR、通道边界、最近成交 K 线和截止时间；`market_windows` 已提供连续的 1m 与 15m K 线窗口；`execution_preview` 已提供可执行性、归一化档位与保护损失估算。不得因为希望得到未提供的清算密集带、额外盘口层级或其他非本次输入字段，就声称没有计划、行情、盘口、ATR、结构或纪律数据；只有输入中实际缺少判断所必需的字段、字段过期或字段互相矛盾时才可据此 REJECT。

逐项审核：
1. 目的、交易形态和交易理由是否明确且彼此一致；
2. 方向、入场方式、价格与金额分布是否和理由及当前行情一致；
3. 初始保护、最大预计亏损、自动退出和失效边界是否完整且不存在明显矛盾；
4. 计划资金边界、账户纪律摘要、费用后收益风险关系和当前盘口是否留下不可接受的新增风险；
5. 止损与止盈相对当前 ATR、通道边界、结构参考和近期 K 线窗口是否过近，是否容易被常规波动或扫损后反转触发；
6. 是否存在关键未知、过期行情、无法执行的档位、追价、保护位错误、没有正向退出空间，或理由无法支持当前计划的情况。

只有全部必要信息明确、计划内部一致、保护与退出可执行且没有关键反例时才输出 APPROVE。任何关键事实未知、输入矛盾、保护不足或风险收益明显不合理都输出 REJECT。不得因为是 Demo 而放宽判断。AI 结论只是一项否决门，不能覆盖 Halpha 的纪律、服务端预览、执行前事实或用户最终启动决定。

最终只输出符合给定 JSON Schema 的对象：decision 必须是 APPROVE 或 REJECT；reason 用中文给出主要理由；suggestions 用中文给出可操作建议，没有建议时返回空数组。"""


_REVIEW_CONTEXT_GUARDRAIL = """\
本次 `plan.decision_context.evidence_cutoff` 由服务端在本次审核请求开始时，
与 `market_context.source_cutoff` 一并重新绑定；它不是用户留在旧草稿中的
历史主张。不得仅因为草稿打开较早、该字段早于页面显示时间，或理由没有重复
书写“当前”，就以“过期理由”拒绝。

`market_windows` 中每个窗口都带有自己的 interval、连续 K 线和截止点。交易
理由引用某个时间周期时，只能使用同一周期的已提供窗口核对；只有该窗口实际
缺失、断档、明确过期，或与其他输入存在可指明的矛盾时，才可以据此拒绝。不得
把未提供的时间周期臆测为计划缺项，也不得用网络搜索的较晚市场信息替代本次
带截止点的审核输入。
"""


def _review_prompt(review_input: Mapping[str, Any]) -> str:
    """Inline the bounded review context so the reviewer cannot miss its only input."""

    serialized = json.dumps(review_input, ensure_ascii=False, sort_keys=True)
    return (
        f"{_FIXED_PROMPT}\n\n{_REVIEW_CONTEXT_GUARDRAIL}"
        f"\n<review_input>\n{serialized}\n</review_input>"
    )


def plan_ai_review_request_digest(
    *,
    plan_id: str,
    draft_version: int,
    draft_content_digest: str,
    configuration: PlanAiReviewConfiguration,
    prompt_version: str = PLAN_AI_REVIEW_PROMPT_VERSION,
) -> str:
    return content_digest(
        {
            "plan_id": plan_id,
            "draft_version": draft_version,
            "draft_content_digest": draft_content_digest,
            "prompt_version": prompt_version,
            "configuration": configuration.model_dump(mode="json"),
        }
    )


def build_plan_ai_review_input(
    *,
    draft: Mapping[str, Any],
    market_context: Mapping[str, Any],
    market_windows: Mapping[str, Any],
    execution_preview: Mapping[str, Any] | None,
    discipline: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return the only bounded product and public-market payload sent to Codex."""

    content = dict(draft["content"])
    decision_context = dict(content.get("decision_context") or {})
    requested_limits = dict(content["requested_limits"])
    safe_plan = {
        "plan_name": content.get("plan_name"),
        "environment_kind": str(content["environment_kind"]),
        "venue_ref": str(content["venue_ref"]),
        "instrument_ref": str(content["instrument_ref"]),
        "direction": str(content["direction"]),
        "target_exposure": str(content["target_exposure"]),
        "requested_limits": requested_limits,
        "valid_from": str(content["valid_from"]),
        "valid_until": str(content["valid_until"]),
        "decision_context": {
            "intent": decision_context.get("intent"),
            "setup_family": decision_context.get("setup_family"),
            "rationale": decision_context.get("rationale"),
            "evidence_cutoff": decision_context.get("evidence_cutoff"),
        },
        "decision_basis": content["decision_basis"],
        "order_schedule_spec": content.get("order_schedule_spec"),
        "allowed_actions": sorted(str(item) for item in content["allowed_actions"]),
    }
    safe_discipline = None
    if discipline is not None:
        safe_discipline = {
            "status": discipline.get("status"),
            "new_risk_allowed": discipline.get("new_risk_allowed"),
            "blocker_codes": list(discipline.get("blocker_codes") or ()),
            "max_plan_loss": discipline.get("max_plan_loss"),
            "minimum_reward_risk_ratio": discipline.get(
                "minimum_reward_risk_ratio"
            ),
            "available_notional_capacity": discipline.get(
                "available_notional_capacity"
            ),
            "open_risk_limit": discipline.get("open_risk_limit"),
            "open_risk_committed": discipline.get("open_risk_committed"),
            "open_risk_after_proposal": discipline.get(
                "open_risk_after_proposal"
            ),
            "gross_exposure_limit": discipline.get("gross_exposure_limit"),
            "gross_exposure": discipline.get("gross_exposure"),
            "gross_exposure_after_proposal": discipline.get(
                "gross_exposure_after_proposal"
            ),
            "instrument_exposure_limit": discipline.get(
                "instrument_exposure_limit"
            ),
            "instrument_exposure": discipline.get("instrument_exposure"),
            "instrument_exposure_after_proposal": discipline.get(
                "instrument_exposure_after_proposal"
            ),
            "correlation_cluster": discipline.get("correlation_cluster"),
            "correlated_exposure_limit": discipline.get(
                "correlated_exposure_limit"
            ),
            "correlated_exposure": discipline.get("correlated_exposure"),
            "correlated_exposure_after_proposal": discipline.get(
                "correlated_exposure_after_proposal"
            ),
            "daily_loss_limit": discipline.get("daily_loss_limit"),
            "daily_loss_measure": discipline.get("daily_loss_measure"),
            "weekly_loss_limit": discipline.get("weekly_loss_limit"),
            "weekly_loss_measure": discipline.get("weekly_loss_measure"),
            "rolling_drawdown_fraction": discipline.get(
                "rolling_drawdown_fraction"
            ),
            "rolling_drawdown_limit_fraction": discipline.get(
                "rolling_drawdown_limit_fraction"
            ),
            "open_new_risk_activation_count": discipline.get(
                "open_new_risk_activation_count"
            ),
            "account_snapshot_cutoff": discipline.get(
                "account_snapshot_cutoff"
            ),
            "evaluated_at": discipline.get("evaluated_at"),
        }
    return {
        "schema_version": PLAN_AI_REVIEW_INPUT_VERSION,
        "prompt_version": PLAN_AI_REVIEW_PROMPT_VERSION,
        "plan_binding": {
            "draft_version": int(draft["draft_version"]),
            "draft_content_digest": str(draft["content_digest"]),
        },
        "plan": safe_plan,
        "execution_preview": execution_preview,
        "market_context": dict(market_context),
        "market_windows": dict(market_windows),
        "account_discipline_summary": safe_discipline,
    }


def _codex_launch_command() -> tuple[str, ...]:
    resolved = shutil.which("codex")
    if resolved is None:
        raise RuntimeError("PLAN_AI_REVIEW_CODEX_CLI_UNAVAILABLE")
    executable = Path(resolved)
    if executable.suffix.casefold() in {".cmd", ".ps1"}:
        node = shutil.which("node")
        entrypoint = (
            executable.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        )
        if node is None or not entrypoint.is_file():
            raise RuntimeError("PLAN_AI_REVIEW_CODEX_CLI_UNAVAILABLE")
        return (node, str(entrypoint))
    return (str(executable),)


def _public_event(event: Mapping[str, Any]) -> PlanAiReviewProgress | None:
    event_type = str(event.get("type") or "")
    if event_type == "thread.started":
        return PlanAiReviewProgress(kind="STATUS", message="Codex 审核会话已建立")
    if event_type == "turn.started":
        return PlanAiReviewProgress(kind="STATUS", message="AI 正在核对计划与行情")
    if event_type in {"item.started", "item.updated"}:
        item = event.get("item")
        if isinstance(item, Mapping) and item.get("type") == "agent_message":
            text = item.get("text") or item.get("delta")
            if isinstance(text, str) and text.strip():
                return PlanAiReviewProgress(
                    kind="OUTPUT",
                    message="AI 正在形成审核结论",
                    output_fragment=text[-4000:],
                )
    if event_type == "turn.completed":
        return PlanAiReviewProgress(kind="STATUS", message="AI 输出完成，正在校验格式")
    return None


def _completed_agent_message(event: Mapping[str, Any]) -> str | None:
    if event.get("type") != "item.completed":
        return None
    item = event.get("item")
    if not isinstance(item, Mapping) or item.get("type") != "agent_message":
        return None
    text = item.get("text")
    return text if isinstance(text, str) and text.strip() else None


class CodexCliPlanReviewer:
    """Run one ephemeral, read-only Codex review with bounded I/O."""

    def __init__(
        self,
        *,
        temporary_root: Path,
        timeout_seconds: int = PLAN_AI_REVIEW_TIMEOUT_SECONDS,
    ) -> None:
        self._temporary_root = temporary_root
        self._timeout_seconds = timeout_seconds

    async def review(
        self,
        review_input: Mapping[str, Any],
        configuration: PlanAiReviewConfiguration,
        emit: ProgressEmitter,
    ) -> PlanAiReviewResult:
        try:
            self._temporary_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise RuntimeError("PLAN_AI_REVIEW_WORKSPACE_UNAVAILABLE") from None
        command = _codex_launch_command()
        await emit(PlanAiReviewProgress(kind="STATUS", message="正在准备固定审核上下文"))
        try:
            review_directory = tempfile.TemporaryDirectory(
                prefix="review-",
                dir=self._temporary_root,
            )
        except OSError:
            raise RuntimeError("PLAN_AI_REVIEW_WORKSPACE_UNAVAILABLE") from None
        with review_directory as raw_directory:
            directory = Path(raw_directory)
            schema_path = directory / "review_output.schema.json"
            try:
                schema_path.write_text(
                    json.dumps(_OUTPUT_SCHEMA, ensure_ascii=False, sort_keys=True),
                    encoding="utf-8",
                )
            except OSError:
                raise RuntimeError("PLAN_AI_REVIEW_WORKSPACE_UNAVAILABLE") from None
            args = (
                *command,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--model",
                configuration.model.value,
                "-c",
                f'model_reasoning_effort="{configuration.reasoning_effort.value}"',
                "-c",
                'shell_environment_policy.inherit="core"',
                "-c",
                'shell_environment_policy.include_only=["PATH","PATHEXT","SYSTEMROOT","WINDIR","COMSPEC","TEMP","TMP"]',
                "-c",
                "shell_environment_policy.ignore_default_excludes=false",
                "-c",
                "tools.web_search=true",
                "--json",
                "--output-schema",
                str(schema_path),
                "--color",
                "never",
                "-",
            )
            creation_flags = (
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            )
            try:
                process = await asyncio.create_subprocess_exec(
                    *args,
                    cwd=directory,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=creation_flags,
                    limit=256 * 1024,
                )
            except (OSError, ValueError):
                raise RuntimeError("PLAN_AI_REVIEW_CODEX_CLI_UNAVAILABLE") from None
            await emit(PlanAiReviewProgress(kind="STATUS", message="Codex CLI 已启动"))
            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None
            process.stdin.write(_review_prompt(review_input).encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()

            output_bytes = 0
            diagnostic_bytes = 0
            final_message: str | None = None

            async def consume_stdout() -> None:
                nonlocal output_bytes, final_message
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        return
                    output_bytes += len(line)
                    if output_bytes > PLAN_AI_REVIEW_MAX_OUTPUT_BYTES:
                        raise RuntimeError("PLAN_AI_REVIEW_OUTPUT_TOO_LARGE")
                    try:
                        event = json.loads(line.decode("utf-8"))
                    except (UnicodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(event, Mapping):
                        continue
                    if message := _completed_agent_message(event):
                        final_message = message
                    if progress := _public_event(event):
                        await emit(progress)

            async def consume_stderr() -> None:
                nonlocal diagnostic_bytes
                while True:
                    chunk = await process.stderr.read(8192)
                    if not chunk:
                        return
                    diagnostic_bytes += len(chunk)
                    if diagnostic_bytes > PLAN_AI_REVIEW_MAX_DIAGNOSTIC_BYTES:
                        raise RuntimeError(
                            "PLAN_AI_REVIEW_DIAGNOSTIC_TOO_LARGE"
                        )

            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        consume_stdout(),
                        consume_stderr(),
                        process.wait(),
                    ),
                    timeout=self._timeout_seconds,
                )
            except TimeoutError:
                process.kill()
                await process.wait()
                raise RuntimeError("PLAN_AI_REVIEW_TIMEOUT") from None
            except BaseException:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                raise
            if process.returncode != 0:
                raise RuntimeError("PLAN_AI_REVIEW_PROVIDER_FAILED")
            if final_message is None:
                raise RuntimeError("PLAN_AI_REVIEW_OUTPUT_MISSING")
            try:
                payload = json.loads(final_message)
                return PlanAiReviewResult.model_validate(payload)
            except (json.JSONDecodeError, ValueError, TypeError):
                raise RuntimeError("PLAN_AI_REVIEW_OUTPUT_INVALID") from None


class PostgreSQLPlanAiReviewStore:
    """Persist the latest state while keeping review identity immutable."""

    def __init__(self, connection: Connection[Any], environment_id: str) -> None:
        self._connection = connection
        self._environment_id = environment_id

    @staticmethod
    def _record(row: Any) -> PlanAiReviewRecord:
        return PlanAiReviewRecord(
            review_id=str(row[0]),
            environment_id=str(row[1]),
            plan_id=str(row[2]),
            draft_version=int(row[3]),
            draft_content_digest=str(row[4]),
            prompt_version=str(row[5]),
            configuration=PlanAiReviewConfiguration(
                model=str(row[6]), reasoning_effort=str(row[7])
            ),
            status=str(row[8]),
            market_context_digest=(str(row[9]) if row[9] is not None else None),
            market_source_cutoff=row[10],
            decision=(str(row[11]) if row[11] is not None else None),
            reason=(str(row[12]) if row[12] is not None else None),
            suggestions=tuple(str(item) for item in (row[13] or ())),
            progress_message=str(row[14]),
            public_output=str(row[15] or ""),
            failure_code=(str(row[16]) if row[16] is not None else None),
            created_at=row[17],
            started_at=row[18],
            completed_at=row[19],
            updated_at=row[20],
        )

    def get(self, review_id: str, *, for_update: bool = False) -> PlanAiReviewRecord:
        suffix = " FOR UPDATE" if for_update else ""
        row = self._connection.execute(
            """
            SELECT review_id, environment_id, plan_id, draft_version,
                   draft_content_digest, prompt_version, review_model, reasoning_effort,
                   status,
                   market_context_digest, market_source_cutoff, decision,
                   reason, suggestions, progress_message, public_output,
                   failure_code, created_at, started_at, completed_at, updated_at
            FROM halpha.plan_ai_review
            WHERE environment_id = %s AND review_id = %s
            """ + suffix,
            (self._environment_id, review_id),
        ).fetchone()
        if row is None:
            raise ValueError("PLAN_AI_REVIEW_NOT_FOUND")
        return self._record(row)

    def by_idempotency_key(self, idempotency_key: str) -> PlanAiReviewRecord | None:
        """Return the original review for a retry before mutating its draft."""

        row = self._connection.execute(
            """
            SELECT review_id
            FROM halpha.plan_ai_review
            WHERE environment_id = %s AND idempotency_key = %s
            """,
            (self._environment_id, idempotency_key),
        ).fetchone()
        return self.get(str(row[0])) if row is not None else None

    def latest_for_plan(self, plan_id: str) -> PlanAiReviewRecord | None:
        row = self._connection.execute(
            """
            SELECT review_id, environment_id, plan_id, draft_version,
                   draft_content_digest, prompt_version, review_model, reasoning_effort,
                   status,
                   market_context_digest, market_source_cutoff, decision,
                   reason, suggestions, progress_message, public_output,
                   failure_code, created_at, started_at, completed_at, updated_at
            FROM halpha.plan_ai_review
            WHERE environment_id = %s AND plan_id = %s
            ORDER BY created_at DESC, review_id DESC
            LIMIT 1
            """,
            (self._environment_id, plan_id),
        ).fetchone()
        return self._record(row) if row is not None else None

    def create_queued(
        self,
        *,
        review_id: str,
        plan_id: str,
        draft_version: int,
        draft_content_digest: str,
        configuration: PlanAiReviewConfiguration,
        idempotency_key: str,
        observed_at: datetime,
    ) -> PlanAiReviewRecord:
        request_digest = plan_ai_review_request_digest(
            plan_id=plan_id,
            draft_version=draft_version,
            draft_content_digest=draft_content_digest,
            configuration=configuration,
        )
        cursor = self._connection.execute(
            """
            INSERT INTO halpha.plan_ai_review (
                review_id, environment_id, plan_id, draft_version,
                draft_content_digest, idempotency_key, request_digest,
                prompt_version, review_model, reasoning_effort, status, suggestions, progress_message,
                public_output, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'QUEUED', '[]'::jsonb,
                '等待收集审核上下文', '', %s, %s
            )
            ON CONFLICT (environment_id, idempotency_key) DO NOTHING
            """,
            (
                review_id,
                self._environment_id,
                plan_id,
                draft_version,
                draft_content_digest,
                idempotency_key,
                request_digest,
                PLAN_AI_REVIEW_PROMPT_VERSION,
                configuration.model.value,
                configuration.reasoning_effort.value,
                observed_at,
                observed_at,
            ),
        )
        if cursor.rowcount == 1:
            return self.get(review_id)
        row = self._connection.execute(
            """
            SELECT review_id, request_digest
            FROM halpha.plan_ai_review
            WHERE environment_id = %s AND idempotency_key = %s
            """,
            (self._environment_id, idempotency_key),
        ).fetchone()
        if row is None or str(row[1]) != request_digest:
            raise ValueError("IDEMPOTENCY_CONTENT_CONFLICT")
        return self.get(str(row[0]))

    def mark_running(
        self,
        review_id: str,
        *,
        market_context_digest: str,
        market_source_cutoff: datetime,
        observed_at: datetime,
    ) -> PlanAiReviewRecord:
        self._connection.execute(
            """
            UPDATE halpha.plan_ai_review
            SET status = 'RUNNING', market_context_digest = %s,
                market_source_cutoff = %s, progress_message = %s,
                started_at = COALESCE(started_at, %s), updated_at = %s
            WHERE environment_id = %s AND review_id = %s AND status = 'QUEUED'
            """,
            (
                market_context_digest,
                market_source_cutoff,
                "AI 正在审核计划",
                observed_at,
                observed_at,
                self._environment_id,
                review_id,
            ),
        )
        return self.get(review_id)

    def update_progress(
        self,
        review_id: str,
        *,
        message: str,
        public_output: str,
        observed_at: datetime,
    ) -> PlanAiReviewRecord:
        self._connection.execute(
            """
            UPDATE halpha.plan_ai_review
            SET progress_message = %s, public_output = %s, updated_at = %s
            WHERE environment_id = %s AND review_id = %s AND status = 'RUNNING'
            """,
            (
                message[:240],
                public_output[-8000:],
                observed_at,
                self._environment_id,
                review_id,
            ),
        )
        return self.get(review_id)

    def complete(
        self,
        review_id: str,
        *,
        result: PlanAiReviewResult,
        observed_at: datetime,
    ) -> PlanAiReviewRecord:
        status = (
            PlanAiReviewStatus.APPROVED
            if result.decision is PlanAiReviewDecision.APPROVE
            else PlanAiReviewStatus.REJECTED
        )
        self._connection.execute(
            """
            UPDATE halpha.plan_ai_review
            SET status = %s, decision = %s, reason = %s, suggestions = %s,
                progress_message = %s, completed_at = %s, updated_at = %s
            WHERE environment_id = %s AND review_id = %s AND status = 'RUNNING'
            """,
            (
                status.value,
                result.decision.value,
                result.reason,
                Jsonb(list(result.suggestions)),
                "AI 审核已批准" if status is PlanAiReviewStatus.APPROVED else "AI 审核未批准",
                observed_at,
                observed_at,
                self._environment_id,
                review_id,
            ),
        )
        return self.get(review_id)

    def fail(
        self,
        review_id: str,
        *,
        failure_code: str,
        observed_at: datetime,
    ) -> PlanAiReviewRecord:
        self._connection.execute(
            """
            UPDATE halpha.plan_ai_review
            SET status = 'FAILED', failure_code = %s,
                progress_message = 'AI 审核失败，未形成批准',
                completed_at = %s, updated_at = %s
            WHERE environment_id = %s AND review_id = %s
              AND status IN ('QUEUED', 'RUNNING')
            """,
            (
                failure_code[:96],
                observed_at,
                observed_at,
                self._environment_id,
                review_id,
            ),
        )
        return self.get(review_id)

    def approved_for_draft(
        self,
        *,
        plan_id: str,
        draft_version: int,
        draft_content_digest: str,
    ) -> PlanAiReviewRecord | None:
        row = self._connection.execute(
            """
            SELECT review_id
            FROM halpha.plan_ai_review
            WHERE environment_id = %s AND plan_id = %s
              AND draft_version = %s AND draft_content_digest = %s
              AND prompt_version = %s AND status = 'APPROVED'
              AND decision = 'APPROVE'
            ORDER BY completed_at DESC, review_id DESC
            LIMIT 1
            """,
            (
                self._environment_id,
                plan_id,
                draft_version,
                draft_content_digest,
                PLAN_AI_REVIEW_PROMPT_VERSION,
            ),
        ).fetchone()
        return self.get(str(row[0])) if row is not None else None


class PlanAiReviewCoordinator:
    """Own background review tasks and bounded public progress subscribers."""

    def __init__(
        self,
        *,
        reviewer: PlanAiReviewer,
        mark_running: Callable[[str, str, datetime], PlanAiReviewRecord],
        update_progress: Callable[[str, str, str], PlanAiReviewRecord],
        complete: Callable[[str, PlanAiReviewResult], PlanAiReviewRecord],
        fail: Callable[[str, str], PlanAiReviewRecord],
    ) -> None:
        self._reviewer = reviewer
        self._mark_running = mark_running
        self._update_progress = update_progress
        self._complete = complete
        self._fail = fail
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._subscribers: dict[str, set[asyncio.Queue[PlanAiReviewRecord]]] = {}

    async def _publish(self, record: PlanAiReviewRecord) -> None:
        for queue in tuple(self._subscribers.get(record.review_id, ())):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(record)

    def start(
        self,
        *,
        record: PlanAiReviewRecord,
        review_input: Mapping[str, Any],
        market_context_digest: str,
        market_source_cutoff: datetime,
    ) -> None:
        if record.status is not PlanAiReviewStatus.QUEUED:
            return
        existing = self._tasks.get(record.review_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._run(
                record.review_id,
                review_input,
                record.configuration,
                market_context_digest,
                market_source_cutoff,
            ),
            name=f"plan-ai-review:{record.review_id}",
        )
        self._tasks[record.review_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(record.review_id, None))

    async def _run(
        self,
        review_id: str,
        review_input: Mapping[str, Any],
        configuration: PlanAiReviewConfiguration,
        market_context_digest: str,
        market_source_cutoff: datetime,
    ) -> None:
        public_output = ""
        try:
            record = await asyncio.to_thread(
                self._mark_running,
                review_id,
                market_context_digest,
                market_source_cutoff,
            )
            await self._publish(record)

            async def emit(progress: PlanAiReviewProgress) -> None:
                nonlocal public_output
                if progress.output_fragment:
                    public_output = progress.output_fragment[-8000:]
                updated = await asyncio.to_thread(
                    self._update_progress,
                    review_id,
                    progress.message,
                    public_output,
                )
                await self._publish(updated)

            result = await self._reviewer.review(review_input, configuration, emit)
            completed = await asyncio.to_thread(self._complete, review_id, result)
            await self._publish(completed)
        except asyncio.CancelledError:
            failed = await asyncio.to_thread(
                self._fail,
                review_id,
                "PLAN_AI_REVIEW_INTERRUPTED",
            )
            await self._publish(failed)
            raise
        except Exception as exc:
            code = str(exc)
            if code not in _PLAN_AI_REVIEW_FAILURE_CODES:
                code = "PLAN_AI_REVIEW_INTERNAL_FAILURE"
            failed = await asyncio.to_thread(self._fail, review_id, code[:96])
            await self._publish(failed)

    async def subscribe(self, review_id: str):
        queue: asyncio.Queue[PlanAiReviewRecord] = asyncio.Queue(maxsize=16)
        self._subscribers.setdefault(review_id, set()).add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            subscribers = self._subscribers.get(review_id)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(review_id, None)

    async def close(self) -> None:
        tasks = tuple(task for task in self._tasks.values() if not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
