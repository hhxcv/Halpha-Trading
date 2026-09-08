from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
import psycopg

from halpha.app.secrets import AppSecrets
from halpha.app.web import create_app
from halpha.app.planning_api import (
    ActivationPayload,
    ControlPayload,
    PlanCreatePayload,
    PostgreSQLPlanningApi,
)
from halpha.live_write_gate import LiveWriteGateStatus
from halpha.configuration import load_settings
from halpha.planning.order_policies import (
    CancelOnShockRule,
    ConditionGroup,
    FullFillLossBudgetSpec,
    InitialStopSpec,
    NumericComparator,
    ProfitRCondition,
    ProtectionPolicy,
    ProtectionStep,
    SteppedProtectionRule,
    TakeProfitLadderSpec,
    TakeProfitLevel,
)
from halpha.planning.models import (
    PlanDecisionContext,
    new_risk_decision_context_incompatibility,
)
from halpha.planning.order_schedule import (
    AmountDistribution,
    EntryProgram,
    EntryProgramKind,
    OrderScheduleSpec,
    ScheduleSubmissionMode,
    SinglePrice,
    VenueOrderPolicy,
    VenueOrderType,
    direct_allowed_action_profiles,
)
from halpha.planning.registry import (
    DIRECT_EXECUTION_REF,
    Direction,
    OneShotParameters,
    build_fixed_plan_basis,
)
from halpha.planning.transitions import ControlIntent
from halpha.scalping.models import ScalpTemplate, ScalpTriggerPayload


NOW = datetime(2026, 7, 18, 12, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]
DECISION_CONTEXT = {
    "rationale": "Validate the bounded plan decision.",
    "evidence": "Current plan inputs and venue facts.",
    "limitations": "Future price, fill and funding remain uncertain.",
    "intent": "PROFIT_SEEKING",
    "setup_family": "BREAKOUT_CONTINUATION",
    "invalidation": "Do not enter after the fixed invalidation boundary.",
    "evidence_cutoff": NOW.isoformat(),
}
VALIDATION_DECISION_CONTEXT = {**DECISION_CONTEXT, "intent": "VALIDATION"}
DIRECT_DECISION_CONTEXT = {
    **DECISION_CONTEXT,
    "playbook_ref": "DIRECT_CAPABILITY_BOUNDARY_V1",
}


def _mock_live_activation(activation_id: str) -> dict[str, object]:
    return {
        "activation_id": activation_id,
        "environment_id": "live-main",
        "environment_kind": "LIVE",
        "authority_class": "LIVE_REAL_CAPITAL",
        "plan_version_ref": "plan-version-live-001",
        "account_ref": "live-account",
        "instrument_ref": "BTCUSDT-PERP",
        "direction": "LONG",
        "decision_basis_ref": "ONE_SHOT_DONCHIAN_ATR@1",
        "framework_strategy_id": "strategy-live-1",
        "order_schedule_snapshot": None,
        "target_exposure": "100",
        "lifecycle": "RUNNING",
        "run_state": "ACTIVE",
        "rule_state": {},
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
    }


def _status(
    *,
    consistent: bool | None = True,
    configured: str = "CLOSED",
    violations: tuple[str, ...] = (),
) -> LiveWriteGateStatus:
    return LiveWriteGateStatus(
        configured_runtime_real_write_gate=configured,
        runtime_real_write_gate="CLOSED",
        product_build_id="a" * 64,
        product_build_consistent=consistent,
        violations=violations,
    )


def _api(status: LiveWriteGateStatus) -> PostgreSQLPlanningApi:
    return PostgreSQLPlanningApi(
        database_name="halpha_live_copy",
        database_role_name="halpha_live_copy_app",
        password=SecretStr("qualification-password"),
        environment_id="binance-live-copy-primary",
        environment_kind="LIVE",
        authority_class="LIVE_REAL_CAPITAL",
        account_ref="binance-usdm-copy-lead-primary",
        product_build_id="a" * 64,
        profile="BINANCE_LIVE_WRITE",
        gate_status_provider=lambda: status,
    )


def _read_only_api() -> PostgreSQLPlanningApi:
    return PostgreSQLPlanningApi(
        database_name="halpha_live_copy",
        database_role_name="halpha_live_copy_app_reader",
        password=SecretStr("qualification-password"),
        environment_id="binance-live-copy-primary",
        environment_kind="LIVE",
        authority_class="LIVE_REAL_CAPITAL",
        account_ref="binance-usdm-copy-lead-primary",
        product_build_id="a" * 64,
        profile="BINANCE_LIVE_READ_ONLY",
        gate_status_provider=lambda: _status(),
    )


def _payload() -> ActivationPayload:
    return ActivationPayload(plan_version_id="plan-version-live-001")


@pytest.mark.parametrize(
    ("status", "reason"),
    (
        (
            _status(consistent=False),
            "LIVE_WRITE_PRODUCT_BUILD_MISMATCH",
        ),
        (
            _status(configured="OPEN"),
            "LIVE_WRITE_GATE_MUST_BE_CLOSED_FOR_ACTIVATION",
        ),
        (
            _status(violations=("LIVE_WRITE_GATE_BINDING_EXPIRED",)),
            "LIVE_WRITE_GATE_BINDING_INVALID_FOR_ACTIVATION",
        ),
        (
            _status(violations=("LIVE_WRITE_GATE_ACCOUNT_SCOPE_MISMATCH",)),
            "LIVE_WRITE_GATE_BINDING_INVALID_FOR_ACTIVATION",
        ),
    ),
)
def test_live_activation_rejects_before_database_mutation(
    status: LiveWriteGateStatus,
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api(status)
    monkeypatch.setattr(
        api,
        "_connect",
        lambda: pytest.fail("database must not be reached"),
    )
    with pytest.raises(ValueError, match=reason):
        api.activate(_payload(), idempotency_key="live-001", observed_at=NOW)


@pytest.mark.parametrize(
    ("status", "reason"),
    (
        (
            _status(consistent=False),
            "LIVE_WRITE_PRODUCT_BUILD_MISMATCH",
        ),
        (
            _status(),
            "LIVE_WRITE_GATE_MUST_BE_OPEN_FOR_SCALP",
        ),
        (
            _status(violations=("LIVE_WRITE_GATE_BINDING_EXPIRED",)),
            "LIVE_WRITE_GATE_BINDING_INVALID_FOR_ACTIVATION",
        ),
    ),
)
def test_live_scalp_rejects_without_an_effective_open_gate(
    status: LiveWriteGateStatus,
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api(status)
    monkeypatch.setattr(
        api,
        "_connect",
        lambda: pytest.fail("database must not be reached"),
    )
    payload = ScalpTriggerPayload(
        instrument_ref="BTCUSDT-PERP",
        direction="LONG",
        template=ScalpTemplate(),
    )
    invalid_preview = SimpleNamespace(full_fill_protection_estimate=None)

    with pytest.raises(ValueError, match=reason):
        api.trigger_scalp_cycle(
            payload,
            idempotency_key="live-scalp-001",
            observed_at=NOW,
            order_schedule_snapshot=invalid_preview,
        )


def test_live_scalp_accepts_an_effective_open_gate_before_preview_validation() -> None:
    status = LiveWriteGateStatus(
        configured_runtime_real_write_gate="OPEN",
        runtime_real_write_gate="OPEN",
        product_build_id="a" * 64,
        product_build_consistent=True,
        authorized_activation_ids=("activation-live-existing",),
    )
    api = _api(status)
    payload = ScalpTriggerPayload(
        instrument_ref="BTCUSDT-PERP",
        direction="LONG",
        template=ScalpTemplate(),
    )

    with pytest.raises(ValueError, match="SCALP_PREVIEW_MISMATCH"):
        api.trigger_scalp_cycle(
            payload,
            idempotency_key="live-scalp-open",
            observed_at=NOW,
            order_schedule_snapshot=SimpleNamespace(
                full_fill_protection_estimate=None,
                valid=False,
            ),
        )


def test_activation_payload_rejects_legacy_capital_authorization_fields() -> None:
    with pytest.raises(ValueError):
        ActivationPayload(
            plan_version_id="plan-version-live-001",
            capital_limit_version_id="legacy-limit",  # type: ignore[call-arg]
        )


def test_plan_payload_rejects_legacy_top_level_strategy_fields() -> None:
    with pytest.raises(ValidationError):
        PlanCreatePayload(
            plan_name="legacy strategy payload",
            creator_kind="AI",
            strategy_id="ONE_SHOT_DONCHIAN_ATR_BREAKOUT",
            parameters={},
            instrument_ref="BTCUSDT-PERP",
            direction="LONG",
            target_exposure="100",
            max_margin="100",
            max_notional="100",
            max_allowed_loss="100",
            valid_minutes=15,
        )


def test_live_plan_rejects_demo_immediate_entry_before_database_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api(_status())
    monkeypatch.setattr(
        api,
        "_connect",
        lambda: pytest.fail("database must not be reached"),
    )
    payload = PlanCreatePayload(
        plan_name="AI live boundary check",
        creator_kind="AI",
        decision_context=VALIDATION_DECISION_CONTEXT,
        decision_basis={
            "kind": "STRATEGY_SIGNAL",
            "decision_basis_ref": "ONE_SHOT_DONCHIAN_ATR_BREAKOUT",
            "parameters": {"demo_immediate_entry": True},
        },
        instrument_ref="BTCUSDT-PERP",
        direction="LONG",
        target_exposure="100",
        max_margin="100",
        max_notional="100",
        max_allowed_loss="100",
        valid_minutes=15,
    )

    with pytest.raises(ValueError, match="DEMO_IMMEDIATE_ENTRY_REQUIRES_DEMO"):
        api.save_new_plan(
            payload,
            idempotency_key="demo-check-live-rejected",
            observed_at=NOW,
        )


def test_validation_only_strategy_rejects_profit_intent_before_database_mutation() -> None:
    with pytest.raises(
        ValidationError,
        match="STRATEGY_DECISION_INTENT_NOT_QUALIFIED",
    ):
        PlanCreatePayload(
            plan_name="Unqualified profit strategy",
            creator_kind="AI",
            decision_context=DECISION_CONTEXT,
            decision_basis={
                "kind": "STRATEGY_SIGNAL",
                "decision_basis_ref": "ONE_SHOT_DONCHIAN_ATR_BREAKOUT",
                "parameters": {"demo_immediate_entry": False},
            },
            instrument_ref="BTCUSDT-PERP",
            direction="LONG",
            target_exposure="100",
            max_margin="100",
            max_notional="100",
            max_allowed_loss="100",
            valid_minutes=15,
        )


def _direct_schedule(**updates: object) -> OrderScheduleSpec:
    values: dict[str, object] = {
        "entry_program": EntryProgram(kind=EntryProgramKind.ONE_TIME),
        "price_distribution": SinglePrice(limit_price="100"),
        "amount_distribution": AmountDistribution(base_notional="20"),
        "protection_policy": ProtectionPolicy(
            initial_stop=InitialStopSpec(distance_bps="100"),
            time_exit_seconds=60,
            full_fill_loss_budget=FullFillLossBudgetSpec(
                entry_fee_bps="2",
                exit_fee_bps="5",
            ),
        ),
    }
    return OrderScheduleSpec(**{**values, **updates})


def _direct_payload(schedule: OrderScheduleSpec) -> PlanCreatePayload:
    return PlanCreatePayload(
        plan_name="AI direct capability boundary",
        creator_kind="AI",
        decision_context=DIRECT_DECISION_CONTEXT,
        decision_basis={
            "kind": "DIRECT_EXECUTION",
            "decision_basis_ref": DIRECT_EXECUTION_REF,
            "parameters": {},
        },
        order_schedule_spec=schedule,
        instrument_ref="BTCUSDT-PERP",
        direction="LONG",
        target_exposure="100",
        max_margin="100",
        max_notional="100",
        max_allowed_loss="100",
        valid_minutes=15,
    )


def test_new_plan_requires_a_complete_decision_context() -> None:
    values = _direct_payload(_direct_schedule()).model_dump(mode="json")
    values.pop("decision_context")
    with pytest.raises(ValidationError):
        PlanCreatePayload.model_validate(values)

    values["decision_context"] = {
        **DIRECT_DECISION_CONTEXT,
        "limitations": "   ",
    }
    with pytest.raises(ValidationError, match="PLAN_DECISION_CONTEXT_INVALID"):
        PlanCreatePayload.model_validate(values)


def test_new_risk_draft_keeps_an_incomplete_experiment_record_for_later_completion() -> None:
    values = _direct_payload(_direct_schedule()).model_dump(mode="json")
    values["decision_context"] = {
        "rationale": "Generic direction guess.",
        "evidence": "Current quote.",
        "limitations": "Future path unknown.",
    }

    payload = PlanCreatePayload.model_validate(values)

    assert new_risk_decision_context_incompatibility(
        decision_basis_kind=payload.decision_basis.kind,
        decision_context=payload.decision_context,
    ) == "PLAN_DECISION_EXPERIMENT_INCOMPLETE"


def test_demo_flow_check_cannot_be_labelled_as_profit_seeking() -> None:
    values = _direct_payload(_direct_schedule()).model_dump(mode="json")
    values["decision_basis"] = {
        "kind": "STRATEGY_SIGNAL",
        "decision_basis_ref": "ONE_SHOT_DONCHIAN_ATR_BREAKOUT",
        "parameters": {"demo_immediate_entry": True},
    }
    values["order_schedule_spec"] = None
    values["decision_context"]["playbook_ref"] = None

    with pytest.raises(
        ValidationError,
        match="DEMO_FLOW_CHECK_REQUIRES_VALIDATION_INTENT",
    ):
        PlanCreatePayload.model_validate(values)


def test_live_read_only_rejects_every_product_mutation_before_database_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _read_only_api()
    monkeypatch.setattr(
        api,
        "_connect",
        lambda: pytest.fail("read-only mutation must not reach the database"),
    )
    payload = _direct_payload(_direct_schedule())
    mutations = (
        lambda: api.save_new_plan(
            payload,
            idempotency_key="ro-create",
            observed_at=NOW,
        ),
        lambda: api.update_plan(
            "plan-ro",
            payload,
            expected_version=1,
            observed_at=NOW,
        ),
        lambda: api.delete_plan("plan-ro", expected_version=1),
        lambda: api.fix_plan(
            "plan-ro",
            idempotency_key="ro-fix",
            expected_version=1,
            observed_at=NOW,
        ),
        lambda: api.activate(
            _payload(),
            idempotency_key="ro-activate",
            observed_at=NOW,
        ),
        lambda: api.submit_control(
            "activation-ro",
            ControlIntent.STOP_NEW_RISK,
            ControlPayload(expected_version=1),
            idempotency_key="ro-control",
            observed_at=NOW,
        ),
    )

    for mutation in mutations:
        with pytest.raises(
            ValueError,
            match="LIVE_READ_ONLY_PRODUCT_MUTATION_FORBIDDEN",
        ):
            mutation()


def test_live_read_only_planning_connections_force_transactions_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def capture_connect(**kwargs: object) -> object:
        observed.update(kwargs)
        return object()

    monkeypatch.setattr(psycopg, "connect", capture_connect)

    _read_only_api()._connect()

    assert observed["options"] == "-c default_transaction_read_only=on"
    assert observed["user"] == "halpha_live_copy_app_reader"


@pytest.mark.parametrize(
    ("schedule", "reason"),
    (
        (
            _direct_schedule(
                entry_conditions=ConditionGroup(
                    items=(
                        ProfitRCondition(
                            comparator=NumericComparator.GTE,
                            threshold_r="1",
                        ),
                    )
                )
            ),
            "DIRECT_EXECUTION_PROFIT_R_UNSUPPORTED",
        ),
        (
            _direct_schedule(
                dynamic_rules=(
                    CancelOnShockRule(
                        window_seconds=5,
                        adverse_move_bps="25",
                        max_triggers=2,
                    ),
                )
            ),
            "DIRECT_EXECUTION_CANCEL_ON_SHOCK_MAX_TRIGGERS_UNSUPPORTED",
        ),
        (
            _direct_schedule(
                submission_mode=ScheduleSubmissionMode.PREPROTECTED_PARALLEL,
            ),
            "PREPROTECTED_PARALLEL_NOT_VERIFIED",
        ),
        (
            _direct_schedule(
                protection_policy=ProtectionPolicy(
                    initial_stop=InitialStopSpec(distance_bps="100"),
                    take_profit_ladder=TakeProfitLadderSpec(
                        levels=(
                            TakeProfitLevel(
                                trigger_r="1",
                                quantity_fraction="0.5",
                            ),
                        )
                    ),
                ),
            ),
            "DIRECT_EXECUTION_TAKE_PROFIT_FRACTION_TOTAL_INVALID",
        ),
        (
            _direct_schedule(
                protection_policy=ProtectionPolicy(
                    initial_stop=InitialStopSpec(distance_bps="100"),
                    take_profit_ladder=TakeProfitLadderSpec(
                        levels=tuple(
                            TakeProfitLevel(
                                trigger_r=str(index),
                                quantity_fraction="0.2",
                            )
                            for index in range(1, 6)
                        )
                    ),
                ),
            ),
            "DIRECT_EXECUTION_TAKE_PROFIT_LEVEL_COUNT_INVALID",
        ),
    ),
)
def test_direct_plan_api_input_rejects_unconsumed_schedule_capabilities(
    schedule: OrderScheduleSpec,
    reason: str,
) -> None:
    with pytest.raises(ValidationError) as error:
        _direct_payload(schedule)

    assert {
        str(item.get("ctx", {}).get("error"))
        for item in error.value.errors(include_url=False)
    } == {reason}


def test_direct_plan_api_input_accepts_single_trigger_shock_rule() -> None:
    payload = _direct_payload(
        _direct_schedule(
            dynamic_rules=(
                CancelOnShockRule(
                    window_seconds=5,
                    adverse_move_bps="25",
                    max_triggers=1,
                ),
            )
        )
    )

    assert payload.order_schedule_spec is not None
    assert payload.order_schedule_spec.dynamic_rules[0].max_triggers == 1


def test_direct_plan_api_input_accepts_bounded_stepped_protection() -> None:
    payload = _direct_payload(
        _direct_schedule(
            dynamic_rules=(
                SteppedProtectionRule(
                    steps=(ProtectionStep(trigger_r="1", stop_r="0"),),
                ),
            )
        )
    )

    assert payload.order_schedule_spec is not None
    assert payload.order_schedule_spec.dynamic_rules[0].kind == (
        "STEPPED_PROTECTION"
    )


def test_direct_limit_plan_gets_only_actions_its_schedule_can_use() -> None:
    assert direct_allowed_action_profiles(_direct_schedule()) == frozenset(
        {
            "ENTRY_LIMIT",
            "PROTECTIVE_STOP_REDUCE_ONLY",
            "CANCEL_ORDER",
            "REDUCE_OR_CLOSE_MARKET",
        }
    )


def test_direct_market_plan_gets_only_configured_take_profit_actions() -> None:
    schedule = _direct_schedule(
        price_distribution=SinglePrice(),
        venue_policy=VenueOrderPolicy(
            order_type=VenueOrderType.MARKET,
            time_in_force=None,
        ),
        protection_policy=ProtectionPolicy(
            initial_stop=InitialStopSpec(distance_bps="100"),
            take_profit_ladder=TakeProfitLadderSpec(
                levels=(
                    TakeProfitLevel(trigger_r="1", quantity_fraction="1"),
                )
            ),
        ),
    )

    assert direct_allowed_action_profiles(schedule) == frozenset(
        {
            "ENTRY_MARKET",
            "PROTECTIVE_STOP_REDUCE_ONLY",
            "REDUCE_OR_CLOSE_MARKET",
            "TAKE_PROFIT_1",
        }
    )


class _Context:
    def __init__(self, *, safety_index_ready: bool = True):
        self._safety_index_ready = safety_index_ready

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def transaction(self):
        return self

    def execute(self, query: str, _parameters):
        assert "pg_catalog.pg_index" in query
        row = (
            (
                True,
                True,
                True,
                ["environment_id", "account_ref"],
                "environment_kind::text = 'LIVE'::text "
                "AND lifecycle::text <> 'COMPLETED'::text",
            )
            if self._safety_index_ready
            else None
        )
        return SimpleNamespace(fetchone=lambda: row)


class _RowsConnection(_Context):
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_args):
        return self

    def fetchall(self):
        return self._rows


class _Document:
    def __init__(self, **values):
        self._values = values

    def model_dump(self, *, mode: str):
        assert mode == "json"
        return dict(self._values)


def test_user_takeover_scope_uses_the_server_command_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _ControlService:
        def __init__(self, _connection, _environment_id):
            pass

        def submit(self, command, **_kwargs):
            captured["command"] = command
            return _Document(receipt_id="receipt-live-001")

    api = _api(_status())
    monkeypatch.setattr(api, "_connect", lambda: _Context())
    monkeypatch.setattr(
        "halpha.app.planning_api.ActivationControlService",
        _ControlService,
    )

    api.submit_control(
        "activation-live-001",
        ControlIntent.USER_TAKEOVER,
        ControlPayload(
            expected_version=1,
            takeover_scope={
                "command_ref": "user-controlled-command",
                "activation_id": "other-activation",
                "cutoff": "2000-01-01T00:00:00+00:00",
                "execution_responsibility": "USER",
            },
        ),
        idempotency_key="takeover-live-001",
        observed_at=NOW,
    )

    command = captured["command"]
    assert command.scope == {
        "command_ref": command.command_id,
        "activation_id": "activation-live-001",
        "cutoff": NOW.isoformat(),
        "execution_responsibility": "USER",
    }


def test_activation_preview_returns_the_fixed_protection_and_exit_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parameters = OneShotParameters(
        direction=Direction.SHORT,
        confirmation_bars_1m=1,
        entry_valid_minutes=30,
        initial_stop_atr_multiple="1.0",
        max_hold_bars_15m=4,
        take_profit_1_r="1.0",
        take_profit_2_r="2.0",
    ).model_dump(mode="json")
    version = SimpleNamespace(
        plan_version_id="plan-version-live-001",
        plan_name=None,
        created_at=None,
        creator_kind=None,
        account_ref="binance-usdm-live-owner-primary",
        instrument_ref="BTCUSDT-PERP",
        direction=Direction.SHORT,
        decision_basis=build_fixed_plan_basis(
            "ONE_SHOT_DONCHIAN_ATR_BREAKOUT",
            parameters,
            product_build_id="b" * 64,
        ),
        decision_context=PlanDecisionContext.model_validate(
            VALIDATION_DECISION_CONTEXT
        ),
            requested_limits=SimpleNamespace(
                max_notional="500",
                max_allowed_loss="500",
                model_dump=lambda **_kwargs: {
                "max_margin": "500",
                "max_notional": "500",
                "max_allowed_loss": "500",
            },
        ),
        order_schedule_spec=None,
        valid_until=NOW,
        allowed_actions=frozenset({"ENTRY"}),
    )

    class _Repository:
        def __init__(self, *_args):
            pass

        @staticmethod
        def get_version(_plan_version_id):
            return version

    api = _api(_status())
    monkeypatch.setattr(api, "_connect", lambda: _Context())
    monkeypatch.setattr(
        "halpha.app.planning_api.PostgreSQLPlanningRepository",
        _Repository,
    )
    monkeypatch.setattr(
        "halpha.app.planning_api.PlanningApplicationService.new_risk_discipline_status",
        lambda *_args, **_kwargs: SimpleNamespace(
            new_risk_allowed=True,
            model_dump=lambda **_kwargs: None,
        ),
    )
    monkeypatch.setattr(
        api,
        "_require_approved_version_ai_review",
        lambda *_args: {
            "review_id": "review-approved",
            "status": "APPROVED",
            "decision": "APPROVE",
            "draft_content_digest": "c" * 64,
        },
    )

    preview = api.activation_preview("plan-version-live-001")

    assert preview["strategy_parameters"] == parameters
    assert preview["strategy_parameters"]["initial_stop_atr_multiple"] == "1"
    assert preview["strategy_parameters"]["max_hold_bars_15m"] == 4
    assert preview["product_build_consistent"] is False
    assert preview["runtime_compatible"] is True
    assert preview["live_activation_eligible"] is True

    version.decision_context = PlanDecisionContext.model_validate(DECISION_CONTEXT)
    incompatible_preview = api.activation_preview("plan-version-live-001")
    assert incompatible_preview["runtime_compatible"] is False
    assert incompatible_preview["runtime_incompatibility_reason"] == (
        "STRATEGY_DECISION_INTENT_NOT_QUALIFIED"
    )
    assert incompatible_preview["live_activation_eligible"] is False


def test_plan_list_marks_old_build_provenance_but_keeps_runtime_compatibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api(_status())
    fixed_basis = build_fixed_plan_basis(
        "ONE_SHOT_DONCHIAN_ATR_BREAKOUT",
        {
            "direction": "SHORT",
            "channel_lookback_15m": 20,
        },
        product_build_id="b" * 64,
    )
    connection = _RowsConnection(
        [
            (
                "plan-001",
                1,
                "c" * 64,
                NOW,
                {
                    "plan_name": "AI short breakout",
                    "created_at": NOW.isoformat(),
                    "creator_kind": "AI",
                    "decision_context": VALIDATION_DECISION_CONTEXT,
                    "decision_basis": {
                        "kind": "STRATEGY_SIGNAL",
                        "decision_basis_ref": "ONE_SHOT_DONCHIAN_ATR_BREAKOUT",
                        "parameters": {
                            "direction": "SHORT",
                            "channel_lookback_15m": 20,
                        },
                    },
                    "instrument_ref": "BTCUSDT-PERP",
                    "direction": "SHORT",
                    "requested_limits": {"max_notional": "500"},
                    "valid_from": NOW.isoformat(),
                    "valid_until": "2026-07-18T13:00:00+00:00",
                },
                "activation-001",
                "RUNNING",
                NOW,
                NOW,
                "plan-version-001",
                NOW,
                "d" * 64,
                "b" * 64,
                NOW.isoformat(),
                fixed_basis.model_dump(mode="json"),
                None,
                None,
                list(fixed_basis.allowed_action_profiles),
                None,
                VALIDATION_DECISION_CONTEXT,
                "review-001",
                {
                    "review_id": "review-001",
                    "status": "APPROVED",
                    "decision": "APPROVE",
                    "reason": "The fixed plan is internally consistent.",
                    "suggestions": [],
                    "configuration": {
                        "model": "gpt-5.6-terra",
                        "reasoning_effort": "medium",
                    },
                },
            )
        ]
    )
    monkeypatch.setattr(api, "_connect", lambda: connection)

    plans = api.list_plans()

    assert plans[0]["fixed_product_build_id"] == "b" * 64
    assert plans[0]["status"] == "RUNNING"
    assert plans[0]["activation_id"] == "activation-001"
    assert plans[0]["fixed_valid_until"] == NOW.isoformat()
    assert plans[0]["product_build_consistent"] is False
    assert plans[0]["runtime_compatible"] is True
    assert plans[0]["runtime_incompatibility_reason"] is None
    assert plans[0]["parameters"]["channel_lookback_15m"] == 20
    assert plans[0]["max_notional"] == "500"
    assert plans[0]["valid_until"] == "2026-07-18T13:00:00+00:00"
    assert plans[0]["plan_name"] == "AI short breakout"
    assert plans[0]["created_at"] == NOW.isoformat()
    assert plans[0]["creator_kind"] == "AI"
    assert plans[0]["ai_review_ref"] == "review-001"
    assert plans[0]["ai_review"] == {
        "review_id": "review-001",
        "status": "APPROVED",
        "decision": "APPROVE",
        "reason": "The fixed plan is internally consistent.",
        "suggestions": [],
        "configuration": {
            "model": "gpt-5.6-terra",
            "reasoning_effort": "medium",
        },
    }


def test_live_activation_uses_the_plan_amount_without_opening_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Service:
        def __init__(self, _connection, _environment_id):
            pass

        def activate_version(self, **values):
            captured.update(values)
            return _Document(activation_id="activation-live-001")

    api = _api(_status())
    monkeypatch.setattr(api, "_connect", lambda: _Context())
    monkeypatch.setattr(
        "halpha.app.planning_api.PlanningApplicationService",
        _Service,
    )
    monkeypatch.setattr(
        "halpha.app.planning_api.require_live_activation_safety_index",
        lambda _connection: None,
    )

    result = api.activate(
        _payload(),
        idempotency_key="live-001",
        observed_at=NOW,
    )

    assert "activation_terms" not in captured
    assert captured["environment_kind"].value == "LIVE"
    assert captured["authority_class"].value == "LIVE_REAL_CAPITAL"
    assert result["venue_write_created"] is False
    assert result["runtime_real_write_gate"] == "CLOSED"


def test_live_activation_requires_the_database_uniqueness_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api(_status())
    monkeypatch.setattr(
        api,
        "_connect",
        lambda: _Context(safety_index_ready=False),
    )
    monkeypatch.setattr(
        "halpha.app.planning_api.PlanningApplicationService",
        lambda *_args: pytest.fail("activation service must not run"),
    )

    with pytest.raises(
        ValueError,
        match="LIVE_WRITE_ACTIVATION_SAFETY_INDEX_UNAVAILABLE",
    ):
        api.activate(
            _payload(),
            idempotency_key="live-index-missing",
            observed_at=NOW,
        )


def test_live_activation_is_created_before_the_bound_executor_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Projection:
        @staticmethod
        def availability():
            return {
                "database_available": True,
                "reason_code": None,
                "server_fact_cutoff": NOW.isoformat(),
            }

        @staticmethod
        def executor_status(_product_build_id):
            return {
                "status": "UNAVAILABLE",
                "checked_at": NOW.isoformat(),
                "product_build_consistent": None,
                "product_build_id": None,
            }

    monkeypatch.setattr(
        "halpha.app.web.evaluate_live_write_gate",
        lambda *_args, **_kwargs: _status(),
    )
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activation_replay",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activation_preview",
        lambda *_args, **_kwargs: {
            "plan_version_id": "plan-version-live-001",
            "order_schedule_spec": None,
            "runtime_compatible": True,
            "runtime_incompatibility_reason": None,
        },
    )
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activate",
        lambda *_args, **_kwargs: {
            "activation": _mock_live_activation("activation-live-001"),
            "venue_write_created": False,
            "runtime_real_write_gate": "CLOSED",
        },
    )
    app = create_app(
        load_settings(ROOT / "config" / "halpha.live-copy-write.example.toml"),
        AppSecrets(
            database_password=SecretStr("live-database-test-secret"),
            csrf_signing_secret=SecretStr("live-csrf-test-secret"),
        ),
        repo_root=ROOT,
        product_build_id="a" * 64,
        projection=_Projection(),
        static_dist=tmp_path / "missing-dist",
    )
    client = TestClient(app, base_url="http://127.0.0.1:8766")
    assert client.get("/api/v1/settings/status").status_code == 200
    csrf = client.cookies.get("halpha_csrf_8766")
    assert csrf

    response = client.post(
        "/api/v1/activations",
        json={"plan_version_id": "plan-version-live-001"},
        headers={
            "Origin": "http://127.0.0.1:8766",
            "X-CSRFToken": csrf,
            "Idempotency-Key": "live-first-activation-001",
        },
    )

    assert response.status_code == 201
    assert response.json()["activation"]["activation_id"] == "activation-live-001"
    assert response.json()["venue_write_created"] is False
    assert response.json()["runtime_real_write_gate"] == "CLOSED"
