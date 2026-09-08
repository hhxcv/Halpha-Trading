from __future__ import annotations

from datetime import UTC, datetime
import psycopg
import pytest
from pydantic import SecretStr, ValidationError

from halpha.app.outcomes_api import (
    DecisionEvidencePreviewPayload,
    OutcomesApiUnavailable,
    PostgreSQLOutcomesApi,
    ReviewCompletionPayload,
    ReviewRefreshPayload,
    StageReviewCreatePayload,
    summarize_decision_evidence,
    summarize_execution_fee_evidence,
)
from halpha.domain_values import content_digest
from halpha.planning.live_profit_qualification import (
    build_playbook_qualification_artifact,
)
from halpha.planning.registry import DecisionBasisKind


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(
        self,
        *,
        omit_exit_fact: bool = False,
        exit_fact_digest: str = "exit-fill-digest",
        decision_basis_ref: str = "ONE_SHOT_DONCHIAN_ATR_BREAKOUT",
        order_schedule_snapshot: dict[str, object] | None = None,
        parameter_digest: str = "parameter-digest",
        decision_context: dict[str, object] | None = None,
    ) -> None:
        self._omit_exit_fact = omit_exit_fact
        self._exit_fact_digest = exit_fact_digest
        self._decision_basis_ref = decision_basis_ref
        self._order_schedule_snapshot = order_schedule_snapshot
        self._parameter_digest = parameter_digest
        self._decision_context = decision_context or {
            "rationale": "A fixed hypothesis.",
            "evidence": "Current market facts.",
            "limitations": "Future path unknown.",
            "intent": "PROFIT_SEEKING",
            "setup_family": "BREAKOUT_CONTINUATION",
            "playbook_ref": "BTC_BREAKOUT_V1",
            "invalidation": "The fixed boundary is breached.",
            "evidence_cutoff": "2026-07-20T00:00:00+00:00",
        }

    def execute(self, query, parameters):
        if "FROM halpha.venue_fact" in query:
            assert parameters == (
                "demo-main",
                ["activation-1"],
                ["activation-1"],
            )
        else:
            assert parameters == ("demo-main", ["activation-1"])
        if "trade_plan_version" in query:
            return _Rows(
                [
                    (
                        "activation-1",
                        "BTCUSDT-PERP",
                        "LONG",
                        self._decision_basis_ref,
                        "300",
                        datetime(2026, 7, 20, 1, tzinfo=UTC),
                        datetime(2026, 7, 20, 2, tzinfo=UTC),
                        "AI BTC breakout",
                        "2026-07-20T00:30:00+00:00",
                        "AI",
                        self._order_schedule_snapshot,
                        None,
                        self._parameter_digest,
                        self._decision_context,
                        "30",
                    )
                ]
            )
        if "FROM halpha.execution_action" in query:
            return _Rows(
                [
                    (
                        "activation-1",
                        "entry-action",
                        "ENTRY",
                    ),
                    (
                        "activation-1",
                        "exit-action",
                        "EXIT",
                    ),
                ]
            )
        assert "FROM halpha.venue_fact" in query
        rows = [
            (
                "activation-1",
                "entry-fill",
                1,
                "FILL",
                "entry-fill-digest",
                {
                    "trade_id": "trade-1",
                    "last_price": "100",
                    "last_quantity": "1",
                },
                "entry-action",
                datetime(2026, 7, 20, 1, tzinfo=UTC),
                None,
                "HALPHA_EXECUTION",
            ),
            (
                "activation-1",
                "entry-fee",
                1,
                "COMMISSION",
                "entry-fee-digest",
                {"trade_id": "trade-1", "amount": "0.1", "currency": "USDT"},
                "entry-action",
                datetime(2026, 7, 20, 1, tzinfo=UTC),
                None,
                "HALPHA_EXECUTION",
            ),
            (
                "activation-1",
                "exit-fill",
                1,
                "FILL",
                self._exit_fact_digest,
                {
                    "trade_id": "trade-2",
                    "last_price": "101",
                    "last_quantity": "1",
                },
                "exit-action",
                datetime(2026, 7, 20, 1, 5, tzinfo=UTC),
                None,
                "HALPHA_EXECUTION",
            ),
            (
                "activation-1",
                "exit-fee",
                1,
                "COMMISSION",
                "exit-fee-digest",
                {"trade_id": "trade-2", "amount": "0.1", "currency": "USDT"},
                "exit-action",
                datetime(2026, 7, 20, 1, 5, tzinfo=UTC),
                None,
                "HALPHA_EXECUTION",
            ),
        ]
        return _Rows(
            [row for row in rows if not self._omit_exit_fact or row[1] != "exit-fill"]
        )


def _review() -> dict[str, object]:
    return {
        "review_id": "review-1",
        "activation_id": "activation-1",
        "input_refs": {
            "execution_actions": [
                {
                    "execution_action_id": "entry-action",
                    "state_version": 2,
                    "state_digest": "entry-action-digest",
                },
                {
                    "execution_action_id": "exit-action",
                    "state_version": 3,
                    "state_digest": "exit-action-digest",
                },
            ],
            "venue_facts": [
                {
                    "venue_fact_id": "entry-fill",
                    "schema_version": 1,
                    "kind": "FILL",
                    "content_digest": "entry-fill-digest",
                },
                {
                    "venue_fact_id": "entry-fee",
                    "schema_version": 1,
                    "kind": "COMMISSION",
                    "content_digest": "entry-fee-digest",
                },
                {
                    "venue_fact_id": "exit-fill",
                    "schema_version": 1,
                    "kind": "FILL",
                    "content_digest": "exit-fill-digest",
                },
                {
                    "venue_fact_id": "exit-fee",
                    "schema_version": 1,
                    "kind": "COMMISSION",
                    "content_digest": "exit-fee-digest",
                },
            ],
        },
        "account_result": {
            "trade_result": {
                "average_exit_price": None,
                "net_pnl": "999",
            }
        },
    }


def test_outcomes_database_failure_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_connect(**kwargs):
        raise psycopg.OperationalError("secret-database-detail")

    monkeypatch.setattr(psycopg, "connect", fail_connect)
    api = PostgreSQLOutcomesApi(
        database_name="halpha_demo",
        database_role_name="halpha_demo_app",
        password=SecretStr("secret-password"),
        environment_id="demo-main",
    )
    with pytest.raises(
        OutcomesApiUnavailable,
        match="OUTCOMES_DATABASE_UNAVAILABLE type=OperationalError",
    ) as captured:
        api._connect()
    rendered = str(captured.value)
    assert "secret-database-detail" not in rendered
    assert "secret-password" not in rendered


def test_live_read_only_outcomes_reject_mutation_before_database_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = PostgreSQLOutcomesApi(
        database_name="halpha_live_copy",
        database_role_name="halpha_live_copy_app_reader",
        password=SecretStr("secret-password"),
        environment_id="live-main",
        read_only=True,
    )
    monkeypatch.setattr(
        api,
        "_connect",
        lambda: pytest.fail("read-only mutation must not reach the database"),
    )

    with pytest.raises(
        ValueError,
        match="LIVE_READ_ONLY_PRODUCT_MUTATION_FORBIDDEN",
    ):
        api.refresh_review(
            "review-live",
            ReviewRefreshPayload(expected_version=1),
        )

    with pytest.raises(
        ValueError,
        match="LIVE_READ_ONLY_PRODUCT_MUTATION_FORBIDDEN",
    ):
        api.create_stage_review(
            StageReviewCreatePayload(
                title="实盘只读验证",
                range_start=datetime(2026, 7, 20, tzinfo=UTC),
                range_end=datetime(2026, 7, 21, tzinfo=UTC),
                problem_analysis="只验证边界。",
                improvement_plan="保持只读。",
                creator_kind="HUMAN",
            ),
            idempotency_key="live-read-only-stage-review",
        )


def test_live_read_only_outcomes_connections_force_transactions_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def capture_connect(**kwargs: object) -> object:
        observed.update(kwargs)
        return object()

    monkeypatch.setattr(psycopg, "connect", capture_connect)
    api = PostgreSQLOutcomesApi(
        database_name="halpha_live_copy",
        database_role_name="halpha_live_copy_app_reader",
        password=SecretStr("secret-password"),
        environment_id="live-main",
        read_only=True,
    )

    api._connect()

    assert observed["options"] == "-c default_transaction_read_only=on"
    assert observed["user"] == "halpha_live_copy_app_reader"


def test_review_completion_contract_excludes_historical_classifications() -> None:
    payload = ReviewCompletionPayload(
        expected_version=1,
        conclusion="TOOLING_ISSUE",
        note="订单状态提示错误，影响退出判断",
    )
    assert payload.conclusion.value == "TOOLING_ISSUE"
    validation_payload = ReviewCompletionPayload(
        expected_version=1,
        conclusion="VALIDATION_TRADE",
        note="验证主动退出责任闭环",
    )
    assert validation_payload.conclusion.value == "VALIDATION_TRADE"

    with pytest.raises(ValidationError):
        ReviewCompletionPayload(
            expected_version=1,
            conclusion="UNKNOWN",
            note="",
        )


def test_review_projection_adds_compact_trade_context() -> None:
    api = PostgreSQLOutcomesApi(
        database_name="halpha_demo",
        database_role_name="halpha_demo_app",
        password=SecretStr("secret-password"),
        environment_id="demo-main",
    )

    result = api._attach_trade_context(
        _Connection(),  # type: ignore[arg-type]
        [_review()],
    )

    assert result[0]["trade_context"] == {
        "instrument_ref": "BTCUSDT-PERP",
        "direction": "LONG",
        "strategy_id": "ONE_SHOT_DONCHIAN_ATR_BREAKOUT",
        "decision_basis_ref": "ONE_SHOT_DONCHIAN_ATR_BREAKOUT",
        "trade_amount": "300",
        "activation_started_at": "2026-07-20T01:00:00+00:00",
        "activation_updated_at": "2026-07-20T02:00:00+00:00",
        "plan_name": "AI BTC breakout",
        "plan_created_at": "2026-07-20T00:30:00+00:00",
        "plan_creator_kind": "AI",
        "order_schedule_snapshot": None,
        "position_alignment": None,
        "parameter_digest": "parameter-digest",
        "decision_context": {
            "rationale": "A fixed hypothesis.",
            "evidence": "Current market facts.",
            "limitations": "Future path unknown.",
            "intent": "PROFIT_SEEKING",
            "setup_family": "BREAKOUT_CONTINUATION",
            "playbook_ref": "BTC_BREAKOUT_V1",
            "invalidation": "The fixed boundary is breached.",
            "evidence_cutoff": "2026-07-20T00:00:00+00:00",
        },
        "max_allowed_loss": "30",
    }
    assert result[0]["resolved_trade_result"] == {
        "fill_count": 2,
        "fills": [
            {
                "trade_id": "trade-1",
                "action_kind": "ENTRY",
                "price": "100",
                "quantity": "1",
                "notional": "100",
                "order_side": None,
                "liquidity_side": None,
                "fee": "0.1",
                "fee_currency": "USDT",
                "fill_time": "2026-07-20T01:00:00+00:00",
            },
            {
                "trade_id": "trade-2",
                "action_kind": "EXIT",
                "price": "101",
                "quantity": "1",
                "notional": "101",
                "order_side": None,
                "liquidity_side": None,
                "fee": "0.1",
                "fee_currency": "USDT",
                "fill_time": "2026-07-20T01:05:00+00:00",
            },
        ],
        "position_quantity": "0",
        "average_entry_price": "100",
        "average_exit_price": "101",
        "entry_notional": "100",
        "fill_cash_flow": "1",
        "commission": "0.2",
            "commission_complete": True,
            "execution_cost_complete": True,
            "funding": "0",
        "funding_record_count": 0,
        "funding_complete": False,
        "calculation_complete": True,
        "closed": True,
        "gross_pnl": "1",
        "net_pnl": "0.8",
        "currency": "USDT",
        "funding_included": False,
        "fill_times_complete": True,
        "first_fill_time": "2026-07-20T01:00:00+00:00",
        "last_fill_time": "2026-07-20T01:05:00+00:00",
        "holding_duration_seconds": "300",
        "result_scope": "HALPHA_ATTRIBUTED_ACTIONS",
        "external_closure_fill_count": 0,
        "strategy_attribution_complete": True,
        "unresolved_refs": [],
    }


def _fee_review(
    *,
    activation_id: str,
    instrument_ref: str = "BTCUSDT-PERP",
    closed: bool = True,
    fills: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "activation_id": activation_id,
        "trade_context": {"instrument_ref": instrument_ref},
        "resolved_trade_result": {
            "closed": closed,
            "calculation_complete": True,
            "commission_complete": True,
            "strategy_attribution_complete": True,
            "fills": fills,
        },
    }


def test_execution_fee_evidence_uses_conservative_rate_from_latest_exact_fills() -> None:
    result = summarize_execution_fee_evidence(
        [
            _fee_review(
                activation_id="activation-1",
                fills=[
                    {
                        "trade_id": "maker-latest",
                        "notional": "100",
                        "liquidity_side": "MAKER",
                        "fee": "0.02",
                        "fee_currency": "USDT",
                        "fill_time": "2026-07-31T03:37:33+00:00",
                    },
                    {
                        "trade_id": "taker-latest",
                        "notional": "100",
                        "liquidity_side": "TAKER",
                        "fee": "0.04",
                        "fee_currency": "USDT",
                        "fill_time": "2026-07-31T03:42:45+00:00",
                    },
                ],
            ),
            _fee_review(
                activation_id="activation-2",
                fills=[
                    {
                        "trade_id": "taker-recent-high",
                        "notional": "100",
                        "liquidity_side": "TAKER",
                        "fee": "0.045",
                        "fee_currency": "USDT",
                        "fill_time": "2026-07-31T03:40:00+00:00",
                    },
                    {
                        "trade_id": "taker-old-outlier",
                        "notional": "100",
                        "liquidity_side": "TAKER",
                        "fee": "0.09",
                        "fee_currency": "USDT",
                        "fill_time": "2026-07-01T00:00:00+00:00",
                    },
                ],
            ),
        ],
        instrument_ref="BTCUSDT-PERP",
        sample_limit=2,
    )

    assert result == {
        "instrument_ref": "BTCUSDT-PERP",
        "source": "RECENT_ATTRIBUTED_COMPLETED_FILLS",
        "calculation": "MAX_RATE_OF_LATEST_FILLS",
        "sample_limit": 2,
        "maker": {
            "conservative_rate_bps": "2",
            "sample_count": 1,
            "latest_fill_time": "2026-07-31T03:37:33+00:00",
        },
        "taker": {
            "conservative_rate_bps": "4.5",
            "sample_count": 2,
            "latest_fill_time": "2026-07-31T03:42:45+00:00",
        },
        "source_cutoff": "2026-07-31T03:42:45+00:00",
    }


def test_execution_fee_evidence_keeps_missing_or_unreliable_samples_unknown() -> None:
    result = summarize_execution_fee_evidence(
        [
            _fee_review(
                activation_id="open",
                closed=False,
                fills=[
                    {
                        "trade_id": "open-maker",
                        "notional": "100",
                        "liquidity_side": "MAKER",
                        "fee": "0.02",
                        "fee_currency": "USDT",
                        "fill_time": "2026-07-31T03:37:33+00:00",
                    },
                ],
            ),
            _fee_review(
                activation_id="other-instrument",
                instrument_ref="ETHUSDT-PERP",
                fills=[],
            ),
            _fee_review(
                activation_id="missing-liquidity",
                fills=[
                    {
                        "trade_id": "unknown-role",
                        "notional": "100",
                        "liquidity_side": None,
                        "fee": "0.04",
                        "fee_currency": "USDT",
                        "fill_time": "2026-07-31T03:42:45+00:00",
                    },
                ],
            ),
        ],
        instrument_ref="BTCUSDT-PERP",
    )

    assert result["maker"] is None
    assert result["taker"] is None
    assert result["source_cutoff"] is None


def test_execution_fee_evidence_rejects_an_empty_sample_window() -> None:
    with pytest.raises(ValueError, match="EXECUTION_FEE_SAMPLE_LIMIT_INVALID"):
        summarize_execution_fee_evidence(
            [],
            instrument_ref="BTCUSDT-PERP",
            sample_limit=0,
        )


def _decision_review(
    *,
    review_id: str,
    net_pnl: str,
    classification: str = "USABLE_SAMPLE",
    intent: str = "PROFIT_SEEKING",
    setup_family: str = "BREAKOUT_CONTINUATION",
    playbook_ref: str | None = "BTC_BREAKOUT_V1",
    status: str = "COMPLETE",
    reliable: bool = True,
    max_allowed_loss: str | None = "10",
    fact_cutoff: str = "2026-07-20T01:00:00+00:00",
    workflow_kind: str | None = None,
) -> dict[str, object]:
    return {
        "review_id": review_id,
        "review_version": 1,
        "content_digest": content_digest({"review_id": review_id}),
        "status": status,
        "primary_result": "COMPLETED",
        "account_result": {
            "classification": "ATTRIBUTED_FACTS_AVAILABLE",
            "missing_refs": [],
        },
        "open_responsibilities": {
            "execution_action_refs": [],
            "unknown_action_refs": [],
        },
        "fact_cutoff": fact_cutoff,
        "evaluations": {
            "owner_conclusion": {
                "result": classification,
                "reason": "",
                "evidence_refs": [],
            }
        },
        "trade_context": {
            "instrument_ref": "BTCUSDT-PERP",
            "direction": "LONG",
            "decision_basis_ref": "DIRECT_EXECUTION@1",
            "parameter_digest": "parameter-digest",
            "decision_context": {
                "intent": intent,
                "setup_family": setup_family,
                "playbook_ref": playbook_ref,
            },
            "max_allowed_loss": max_allowed_loss,
            "workflow_kind": workflow_kind,
        },
        "resolved_trade_result": {
            "calculation_complete": reliable,
            "execution_cost_complete": reliable,
            "closed": reliable,
            "strategy_attribution_complete": reliable,
            "net_pnl": net_pnl,
            "commission": "2",
            "entry_notional": "1000",
        },
    }


def test_decision_evidence_keeps_decision_losses_and_excludes_validation() -> None:
    result = summarize_decision_evidence(
        [
            _decision_review(
                review_id="win",
                net_pnl="10",
                fact_cutoff="2026-07-20T01:00:00+00:00",
            ),
            _decision_review(
                review_id="decision-loss",
                net_pnl="-30",
                classification="TRADE_DECISION_ISSUE",
                fact_cutoff="2026-07-20T02:00:00+00:00",
            ),
            _decision_review(
                review_id="validation-win",
                net_pnl="100",
                intent="VALIDATION",
                classification="VALIDATION_TRADE",
                fact_cutoff="2026-07-20T03:00:00+00:00",
            ),
            _decision_review(
                review_id="tooling",
                net_pnl="50",
                classification="TOOLING_ISSUE",
                fact_cutoff="2026-07-20T04:00:00+00:00",
            ),
        ],
        instrument_ref="BTCUSDT-PERP",
        direction="LONG",
        decision_basis_ref="DIRECT_EXECUTION@1",
        parameter_digest="parameter-digest",
        intent="PROFIT_SEEKING",
        setup_family="BREAKOUT_CONTINUATION",
        playbook_ref="BTC_BREAKOUT_V1",
    )

    assert result["matched_review_count"] == 4
    assert result["comparable_trade_count"] == 2
    assert result["excluded_review_count"] == 2
    assert result["exclusions"] == {
        "TOOLING_ISSUE": 1,
        "VALIDATION_INTENT": 1,
    }
    assert result["evidence_grade"] == "SINGLE_DIGIT_ANECDOTAL"
    assert result["capital_scaling_authority"] is False
    assert result["sample_traceability_complete"] is True
    assert [
        item["review_id"] for item in result["sample_review_refs"]
    ] == ["win", "decision-loss"]
    assert result["sample_identity_digest"] == content_digest(
        result["sample_review_refs"]
    )
    assert result["source_cutoff"] == "2026-07-20T04:00:00+00:00"
    assert result["metrics"] == {
        "trade_count": 2,
        "wins": 1,
        "losses": 1,
        "flat": 0,
        "net_pnl": "-20",
        "commission": "4",
        "average_net_pnl": "-10",
        "gross_profit": "10",
        "gross_loss": "30",
        "profit_factor": "0.3333333333333333333333333333",
        "total_entry_notional": "2000",
        "notional_return_percent": "-1",
        "maximum_drawdown": "30",
        "worst_trade_net_pnl": "-30",
        "net_pnl_without_worst_trade": "10",
        "largest_loss_share_percent": "100",
        "best_trade_net_pnl": "10",
        "net_pnl_without_best_trade": "-30",
        "largest_win_share_percent": "100",
        "longest_winning_streak": 1,
        "longest_losing_streak": 1,
        "current_streak_kind": "LOSS",
        "current_streak_count": 1,
    }
    assert result["repeatability"] == {
        "status": "NOT_READY",
        "reason_codes": ["MINIMUM_SAMPLE_NOT_MET"],
        "policy_version": "PLAYBOOK_REPEATABILITY_SCREEN@1",
        "minimum_trade_count": 30,
        "minimum_profit_factor": "1.2",
        "confidence_level_percent": "95",
        "bootstrap_resamples": 4000,
        "bootstrap_block_length": 2,
        "risk_basis_trade_count": 2,
        "net_r_multiple": "-2",
        "average_r_multiple": "-1",
        "net_r_without_best_trade": "-3",
        "early_segment_trade_count": 1,
        "early_segment_net_r": "1",
        "recent_segment_trade_count": 1,
        "recent_segment_net_r": "-3",
        "mean_r_lower_confidence_bound": "-1",
        "live_promotion_authority": False,
        "capital_scaling_authority": False,
        "limitations": [
            "筛查只消费交易前已固定为同一签名的费用后结果；规则身份填写错误会使结论失真。",
            "移动区块自举只缓解短程连续性影响，不能证明未来市场分布、独立性或资金容量不变。",
            "通过仅表示证据值得进入下一步评估，不自动允许实盘、加本金或提高单笔风险。",
        ],
    }


def test_decision_evidence_requires_an_exact_signature_and_reliable_result() -> None:
    wrong_setup = _decision_review(
        review_id="wrong-setup",
        net_pnl="10",
        setup_family="REVERSAL",
    )
    wrong_playbook = _decision_review(
        review_id="wrong-playbook",
        net_pnl="10",
        playbook_ref="BTC_BREAKOUT_V2",
    )
    unreliable = _decision_review(
        review_id="unreliable",
        net_pnl="10",
        reliable=False,
    )
    result = summarize_decision_evidence(
        [wrong_setup, wrong_playbook, unreliable],
        instrument_ref="BTCUSDT-PERP",
        direction="LONG",
        decision_basis_ref="DIRECT_EXECUTION@1",
        parameter_digest="parameter-digest",
        intent="VALIDATION",
        setup_family="BREAKOUT_CONTINUATION",
        playbook_ref="BTC_BREAKOUT_V1",
    )

    assert result["matched_review_count"] == 1
    assert result["comparable_trade_count"] == 0
    assert result["exclusions"] == {"UNRELIABLE_RESULT": 1}
    assert result["evidence_grade"] == "VALIDATION_INTENT"
    assert result["metrics"]["net_pnl"] == "0"


def test_direct_decision_evidence_excludes_history_without_playbook_identity() -> None:
    result = summarize_decision_evidence(
        [
            _decision_review(
                review_id="historical-unknown-rule",
                net_pnl="25",
                playbook_ref=None,
            )
        ],
        instrument_ref="BTCUSDT-PERP",
        direction="LONG",
        decision_basis_ref="DIRECT_EXECUTION@1",
        parameter_digest="parameter-digest",
        intent="PROFIT_SEEKING",
        setup_family="BREAKOUT_CONTINUATION",
        playbook_ref="BTC_BREAKOUT_V1",
    )

    assert result["matched_review_count"] == 0
    assert result["comparable_trade_count"] == 0
    assert result["playbook_ref"] == "BTC_BREAKOUT_V1"


def test_scalp_evidence_uses_reliable_cycles_without_manual_review_completion() -> None:
    result = summarize_decision_evidence(
        [
            _decision_review(
                review_id="scalp-cycle-1",
                net_pnl="12",
                status="DRAFT",
                classification="",
                setup_family="OTHER",
                playbook_ref="SCALP_TEMPLATE:abc",
                workflow_kind="SCALP_CYCLE",
            )
        ],
        instrument_ref="BTCUSDT-PERP",
        direction="LONG",
        decision_basis_ref="DIRECT_EXECUTION@1",
        parameter_digest="parameter-digest",
        intent="PROFIT_SEEKING",
        setup_family="OTHER",
        playbook_ref="SCALP_TEMPLATE:abc",
    )

    assert result["matched_review_count"] == 1
    assert result["comparable_trade_count"] == 1
    assert result["exclusions"] == {}
    assert result["metrics"]["net_pnl"] == "12"


@pytest.mark.parametrize(
    ("section", "patch"),
    [
        ("account_result", None),
        ("account_result", {"classification": "UNKNOWN"}),
        ("account_result", {"classification": "EXTERNAL_POSITION_DISPOSITION"}),
        ("account_result", {"missing_refs": ["plan_version"]}),
        ("account_result", {"missing_refs": None}),
        ("open_responsibilities", None),
        ("open_responsibilities", {"execution_action_refs": ["pending-exit"]}),
        ("open_responsibilities", {"unknown_action_refs": ["unknown-entry"]}),
        ("open_responsibilities", {"unknown_action_refs": None}),
        ("resolved_trade_result", {"calculation_complete": False}),
        ("resolved_trade_result", {"execution_cost_complete": False}),
        ("resolved_trade_result", {"strategy_attribution_complete": False}),
        ("resolved_trade_result", {"closed": False}),
    ],
)
def test_scalp_evidence_excludes_unresolved_machine_facts(
    section: str,
    patch: dict[str, object] | None,
) -> None:
    review = _decision_review(
        review_id="unresolved-scalp", net_pnl="12", status="DRAFT",
        classification="", setup_family="OTHER", playbook_ref="SCALP_TEMPLATE:abc",
        workflow_kind="SCALP_CYCLE",
    )
    review[section] = (
        {**review[section], **patch} if patch is not None else None
    )

    result = summarize_decision_evidence(
        [review], instrument_ref="BTCUSDT-PERP", direction="LONG",
        decision_basis_ref="DIRECT_EXECUTION@1", parameter_digest="parameter-digest",
        intent="PROFIT_SEEKING", setup_family="OTHER", playbook_ref="SCALP_TEMPLATE:abc",
    )

    assert result["comparable_trade_count"] == 0
    assert result["metrics"]["net_pnl"] == "0"
    assert result["exclusions"] == {"UNRELIABLE_RESULT": 1}
    assert result["sample_review_refs"] == []


@pytest.mark.parametrize("unreliable_kind", ["UNKNOWN_ACTION", "MISSING_FEES", "INCOMPLETE_ATTRIBUTION"])
def test_unreliable_scalp_cannot_complete_a_live_qualification_cohort(
    unreliable_kind: str,
) -> None:
    reviews = [
        _decision_review(
            review_id=f"scalp-{index:02d}", net_pnl="12", status="DRAFT",
            classification="", setup_family="OTHER", playbook_ref="SCALP_TEMPLATE:abc",
            workflow_kind="SCALP_CYCLE", fact_cutoff=f"2026-07-20T01:{index:02d}:00+00:00",
        )
        for index in range(30)
    ]
    parameter_digest = content_digest({})
    for review in reviews:
        review["trade_context"]["parameter_digest"] = parameter_digest

    def evidence():
        return summarize_decision_evidence(
            reviews, instrument_ref="BTCUSDT-PERP", direction="LONG",
            decision_basis_ref="DIRECT_EXECUTION@1", parameter_digest=parameter_digest,
            intent="PROFIT_SEEKING", setup_family="OTHER", playbook_ref="SCALP_TEMPLATE:abc",
        )

    def export(value):
        return build_playbook_qualification_artifact(
            value, source_environment_id="binance-demo-primary",
            source_product_build_id="a" * 64, target_venue_account_type="USDM_PERSONAL",
            decision_basis_kind=DecisionBasisKind.DIRECT_EXECUTION,
            issued_at=datetime(2026, 7, 20, 2, tzinfo=UTC),
        )[0]

    artifact = export(evidence())
    assert artifact.comparable_trade_count == 30
    assert artifact.live_activation_authority is False

    latest = reviews[-1]
    if unreliable_kind == "UNKNOWN_ACTION":
        latest["primary_result"] = "RESULT_UNKNOWN"
        latest["account_result"]["classification"] = "UNKNOWN"
        latest["open_responsibilities"]["unknown_action_refs"] = ["unknown-entry"]
    elif unreliable_kind == "MISSING_FEES":
        latest["resolved_trade_result"]["execution_cost_complete"] = False
    else:
        latest["resolved_trade_result"]["strategy_attribution_complete"] = False

    screened = evidence()
    assert screened["comparable_trade_count"] == 29
    assert screened["exclusions"] == {"UNRELIABLE_RESULT": 1}
    assert all(ref["review_id"] != "scalp-29" for ref in screened["sample_review_refs"])
    with pytest.raises(ValueError, match="PLAYBOOK_QUALIFICATION_EVIDENCE_NOT_CANDIDATE"):
        export(screened)


def test_direct_decision_evidence_preview_requires_a_playbook_identity() -> None:
    with pytest.raises(
        ValidationError,
        match="PLAN_DECISION_PLAYBOOK_REF_REQUIRED",
    ):
        DecisionEvidencePreviewPayload.model_validate(
            {
                "instrument_ref": "BTCUSDT-PERP",
                "direction": "LONG",
                "decision_basis": {
                    "kind": "DIRECT_EXECUTION",
                    "decision_basis_ref": "DIRECT_EXECUTION@1",
                    "parameters": {},
                },
                "intent": "PROFIT_SEEKING",
                "setup_family": "BREAKOUT_CONTINUATION",
            }
        )


def test_review_projection_keeps_direct_execution_as_a_decision_basis() -> None:
    api = PostgreSQLOutcomesApi(
        database_name="halpha_demo",
        database_role_name="halpha_demo_app",
        password=SecretStr("secret-password"),
        environment_id="demo-main",
    )

    result = api._attach_trade_context(
        _Connection(decision_basis_ref="DIRECT_EXECUTION@1"),  # type: ignore[arg-type]
        [_review()],
    )[0]["trade_context"]

    assert result["decision_basis_ref"] == "DIRECT_EXECUTION@1"
    assert result["strategy_id"] is None


def test_review_projection_keeps_the_frozen_order_schedule_snapshot() -> None:
    api = PostgreSQLOutcomesApi(
        database_name="halpha_demo",
        database_role_name="halpha_demo_app",
        password=SecretStr("secret-password"),
        environment_id="demo-main",
    )
    snapshot = {
        "schedule_spec": {
            "price_distribution": {
                "kind": "LADDER",
                "lower_price": "63886.16961898",
                "upper_price": "64112.24705542",
                "level_count": 10,
            }
        },
        "instrument_rules": {"price_tick_size": "0.1"},
        "normalized_legs": [
            {"leg_index": 0, "price": "63886.2"},
            {"leg_index": 9, "price": "64112.2"},
        ],
    }

    result = api._attach_trade_context(
        _Connection(  # type: ignore[arg-type]
            decision_basis_ref="DIRECT_EXECUTION@1",
            order_schedule_snapshot=snapshot,
        ),
        [_review()],
    )[0]["trade_context"]

    assert result["order_schedule_snapshot"] == snapshot


def test_review_projection_keeps_result_unknown_when_a_referenced_fact_is_missing() -> None:
    api = PostgreSQLOutcomesApi(
        database_name="halpha_demo",
        database_role_name="halpha_demo_app",
        password=SecretStr("secret-password"),
        environment_id="demo-main",
    )

    result = api._attach_trade_context(
        _Connection(omit_exit_fact=True),  # type: ignore[arg-type]
        [_review()],
    )[0]["resolved_trade_result"]

    assert result["calculation_complete"] is False
    assert result["gross_pnl"] is None
    assert result["net_pnl"] is None
    assert result["unresolved_refs"] == ["venue_fact:exit-fill"]


def test_review_projection_rejects_a_snapshot_digest_mismatch() -> None:
    api = PostgreSQLOutcomesApi(
        database_name="halpha_demo",
        database_role_name="halpha_demo_app",
        password=SecretStr("secret-password"),
        environment_id="demo-main",
    )

    result = api._attach_trade_context(
        _Connection(exit_fact_digest="different-digest"),  # type: ignore[arg-type]
        [_review()],
    )[0]["resolved_trade_result"]

    assert result["calculation_complete"] is False
    assert result["gross_pnl"] is None
    assert result["net_pnl"] is None
    assert result["unresolved_refs"] == [
        "venue_fact:exit-fill:snapshot_mismatch"
    ]
