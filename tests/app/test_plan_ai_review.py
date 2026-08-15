from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from halpha.app import plan_ai_review as plan_ai_review_module
from halpha.app.plan_ai_review import (
    PLAN_AI_REVIEW_APPROVAL_MAX_AGE,
    PLAN_AI_REVIEW_MAX_OUTPUT_BYTES,
    PLAN_AI_REVIEW_INPUT_VERSION,
    CodexCliPlanReviewer,
    PlanAiReviewConfiguration,
    PlanAiReviewRecord,
    PlanAiReviewResult,
    build_plan_ai_review_input,
    plan_ai_review_approval_is_current,
    plan_ai_review_approval_valid_until,
    plan_ai_review_request_digest,
)
from halpha.app.planning_api import PostgreSQLPlanningApi


NOW = datetime(2026, 8, 13, 2, tzinfo=UTC)


def test_review_input_contains_the_exact_plan_and_only_a_bounded_discipline_summary() -> None:
    draft = {
        "draft_version": 4,
        "content_digest": "a" * 64,
        "content": {
            "plan_name": "BTC continuation",
            "creator_kind": "HUMAN",
            "environment_id": "private-environment",
            "environment_kind": "DEMO",
            "authority_class": "DEMO_VALIDATION",
            "account_ref": "private-account",
            "venue_ref": "BINANCE_USDM",
            "instrument_ref": "BTCUSDT-PERP",
            "direction": "LONG",
            "target_exposure": "500",
            "requested_limits": {
                "max_margin": "500",
                "max_notional": "500",
                "max_allowed_loss": "5",
            },
            "valid_from": NOW.isoformat(),
            "valid_until": NOW.replace(hour=3).isoformat(),
            "decision_context": {
                "intent": "PROFIT_SEEKING",
                "setup_family": "BREAKOUT_CONTINUATION",
                "rationale": "Price accepted above resistance with bounded risk.",
                "evidence": "internal field not sent separately",
                "limitations": "internal field not sent separately",
                "evidence_cutoff": NOW.isoformat(),
            },
            "decision_basis": {
                "kind": "DIRECT_EXECUTION",
                "decision_basis_ref": "DIRECT_EXECUTION@1",
                "parameters": {},
            },
            "order_schedule_spec": {"entry_program": {"kind": "ONE_TIME"}},
            "allowed_actions": ["ENTRY_LIMIT", "CANCEL_ORDER"],
            "host_path": "synthetic-review-context/workspace",
        },
    }
    discipline = {
        "status": "BLOCKED",
        "new_risk_allowed": False,
        "blocker_codes": ["NEW_RISK_MAX_PLAN_LOSS_EXCEEDED"],
        "account_snapshot_ref": "private-snapshot-ref",
        "max_plan_loss": "4",
        "minimum_reward_risk_ratio": "1",
        "available_notional_capacity": "300",
        "daily_loss_limit": "10",
        "daily_loss_measure": "3",
        "rolling_drawdown_limit_fraction": "0.1",
        "evaluated_at": NOW.isoformat(),
    }

    result = build_plan_ai_review_input(
        draft=draft,
        market_context={"reference_price": "100", "source_cutoff": NOW.isoformat()},
        market_windows={
            "one_minute": {"bars": []},
            "four_hour": {"interval": "4h", "bars": []},
        },
        execution_preview={"valid": True, "full_fill_protection_estimate": {"maximum_projected_loss": "5"}},
        discipline=discipline,
    )

    assert result["schema_version"] == PLAN_AI_REVIEW_INPUT_VERSION
    assert result["plan_binding"] == {
        "draft_version": 4,
        "draft_content_digest": "a" * 64,
    }
    assert result["plan"]["requested_limits"]["max_allowed_loss"] == "5"
    assert result["account_discipline_summary"]["max_plan_loss"] == "4"
    assert result["account_discipline_summary"]["minimum_reward_risk_ratio"] == "1"
    assert result["account_discipline_summary"]["daily_loss_measure"] == "3"
    assert result["market_windows"]["four_hour"]["interval"] == "4h"
    rendered = repr(result)
    assert "private-account" not in rendered
    assert "private-environment" not in rendered
    assert "private-snapshot-ref" not in rendered
    assert "synthetic-review-context/workspace" not in rendered


def test_review_result_requires_one_of_the_two_fixed_decisions() -> None:
    approved = PlanAiReviewResult(
        decision="APPROVE",
        reason="计划内部一致，保护与退出均可执行。",
        suggestions=(),
    )

    assert approved.decision.value == "APPROVE"
    with pytest.raises(ValidationError):
        PlanAiReviewResult(
            decision="MAYBE",
            reason="无法形成固定结论。",
            suggestions=(),
        )


def test_review_request_identity_changes_with_model_configuration() -> None:
    base = {
        "plan_id": "plan-1",
        "draft_version": 1,
        "draft_content_digest": "a" * 64,
    }
    assert plan_ai_review_request_digest(
        **base,
        configuration=PlanAiReviewConfiguration(),
    ) != plan_ai_review_request_digest(
        **base,
        configuration=PlanAiReviewConfiguration(
            model="gpt-5.6-sol", reasoning_effort="high"
        ),
    )


def test_review_configuration_accepts_luna_and_all_reasoning_efforts() -> None:
    assert PlanAiReviewConfiguration(
        model="gpt-5.6-luna", reasoning_effort="ultra"
    ).model.value == "gpt-5.6-luna"
    assert {
        PlanAiReviewConfiguration(reasoning_effort=effort).reasoning_effort.value
        for effort in ("low", "medium", "high", "xhigh", "max", "ultra")
    } == {"low", "medium", "high", "xhigh", "max", "ultra"}


def test_fixed_version_review_snapshot_keeps_the_saved_configuration() -> None:
    review = PlanAiReviewRecord(
        review_id="review-001",
        environment_id="demo",
        plan_id="plan-001",
        draft_version=2,
        draft_content_digest="a" * 64,
        prompt_version="HALPHA_PLAN_AI_REVIEW_V3",
        configuration=PlanAiReviewConfiguration(
            model="gpt-5.6-terra",
            reasoning_effort="medium",
        ),
        status="APPROVED",
        market_context_digest="b" * 64,
        market_source_cutoff=NOW,
        decision="APPROVE",
        reason="计划边界完整。",
        suggestions=("按固定保护执行。",),
        progress_message="AI 审核已批准",
        created_at=NOW,
        completed_at=NOW,
        updated_at=NOW,
    )

    snapshot = PostgreSQLPlanningApi._review_activation_snapshot(review)

    assert snapshot["review_id"] == "review-001"
    assert snapshot["configuration"] == {
        "model": "gpt-5.6-terra",
        "reasoning_effort": "medium",
    }
    assert snapshot["reason"] == "计划边界完整。"


def test_approval_has_a_short_market_context_deadline_without_rewriting_ai_decision() -> None:
    review = PlanAiReviewRecord(
        review_id="review-current-001",
        environment_id="demo",
        plan_id="plan-001",
        draft_version=2,
        draft_content_digest="a" * 64,
        prompt_version="HALPHA_PLAN_AI_REVIEW_V4",
        configuration=PlanAiReviewConfiguration(),
        status="APPROVED",
        market_context_digest="b" * 64,
        market_source_cutoff=NOW,
        decision="APPROVE",
        reason="Current market input is internally consistent.",
        suggestions=(),
        progress_message="approved",
        created_at=NOW,
        completed_at=NOW,
        updated_at=NOW,
    )

    assert plan_ai_review_approval_valid_until(review) == (
        NOW + PLAN_AI_REVIEW_APPROVAL_MAX_AGE
    )
    assert plan_ai_review_approval_is_current(review, observed_at=NOW)
    assert not plan_ai_review_approval_is_current(
        review,
        observed_at=NOW + PLAN_AI_REVIEW_APPROVAL_MAX_AGE + timedelta(seconds=1),
    )
    assert review.decision.value == "APPROVE"


def test_fixed_prompt_uses_precollected_protection_context_without_local_write() -> None:
    assert "止损与止盈相对当前 ATR" in plan_ai_review_module._FIXED_PROMPT
    assert "允许使用网页搜索核对公开市场事实" in plan_ai_review_module._FIXED_PROMPT
    assert "不得读取本地文件" in plan_ai_review_module._FIXED_PROMPT
    assert "不得因为希望得到未提供的清算密集带" in plan_ai_review_module._FIXED_PROMPT


def test_fixed_prompt_inlines_the_entire_bounded_review_input() -> None:
    prompt = plan_ai_review_module._review_prompt(
        {"market_context": {"atr_14": "12"}, "plan": {"plan_name": "BTC"}}
    )

    assert "<review_input>" in prompt
    assert '"atr_14": "12"' in prompt
    assert '"plan_name": "BTC"' in prompt


def test_codex_reviewer_reports_an_unwritable_workspace_precisely(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "not-a-directory"
    workspace.write_text("blocked", encoding="utf-8")

    async def exercise() -> None:
        reviewer = CodexCliPlanReviewer(temporary_root=workspace)

        async def emit(_progress: Any) -> None:
            return None

        await reviewer.review({"safe": "synthetic"}, PlanAiReviewConfiguration(), emit)

    with pytest.raises(
        RuntimeError,
        match="PLAN_AI_REVIEW_WORKSPACE_UNAVAILABLE",
    ):
        asyncio.run(exercise())


def test_codex_reviewer_keeps_bounded_diagnostics_out_of_structured_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeStdin:
        payload: bytes = b""

        def write(self, payload: bytes) -> None:
            assert payload
            self.payload += payload

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = FakeStdin()
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            message = json.dumps(
                {
                    "decision": "APPROVE",
                    "reason": "合成计划通过。",
                    "suggestions": [],
                },
                ensure_ascii=False,
            )
            event = json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": message},
                },
                ensure_ascii=False,
            )
            self.stdout.feed_data((event + "\n").encode())
            self.stdout.feed_eof()
            self.stderr.feed_data(b"x" * (PLAN_AI_REVIEW_MAX_OUTPUT_BYTES + 1))
            self.stderr.feed_eof()
            self.returncode: int | None = 0

        async def wait(self) -> int:
            assert self.returncode is not None
            return self.returncode

        def kill(self) -> None:
            self.returncode = -1

    captured_args: tuple[Any, ...] = ()
    captured_stdin: FakeStdin | None = None

    async def fake_create_subprocess_exec(*args: Any, **_kwargs: Any) -> FakeProcess:
        nonlocal captured_args, captured_stdin
        captured_args = args
        process = FakeProcess()
        captured_stdin = process.stdin
        return process

    monkeypatch.setattr(
        plan_ai_review_module,
        "_codex_launch_command",
        lambda: ("codex",),
    )
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    async def exercise() -> PlanAiReviewResult:
        reviewer = CodexCliPlanReviewer(temporary_root=tmp_path / "reviews")

        async def emit(_progress: Any) -> None:
            return None

        return await reviewer.review(
            {"safe": "synthetic"}, PlanAiReviewConfiguration(), emit
        )

    result = asyncio.run(exercise())

    assert result.decision.value == "APPROVE"
    assert 'shell_environment_policy.inherit="core"' in captured_args
    assert "shell_environment_policy.ignore_default_excludes=false" in captured_args
    assert "--ignore-rules" in captured_args
    assert "gpt-5.6-terra" in captured_args
    assert 'model_reasoning_effort="medium"' in captured_args
    assert "tools.web_search=true" in captured_args
    assert captured_stdin is not None
    assert '"safe": "synthetic"' in captured_stdin.payload.decode("utf-8")
