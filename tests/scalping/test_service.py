from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import SecretStr

from halpha.app.planning_api import ActivationPayload, PostgreSQLPlanningApi
from halpha.capital.models import AuthorityClass, EnvironmentKind, NewRiskDisciplinePolicy
from halpha.domain_values import content_digest
from halpha.live_write_gate import LiveWriteGateStatus
from halpha.planning.models import (
    PlanDecisionContext,
    RequestedLimits,
    TradePlanContent,
)
from halpha.planning.order_schedule import (
    InstrumentOrderRules,
    compile_order_schedule,
    direct_allowed_action_profiles,
)
from halpha.planning.registry import Direction, DraftDecisionBasis
from halpha.planning.service import PlanningApplicationService
from halpha.scalping.models import (
    ScalpCycleRecord,
    ScalpTemplate,
    ScalpTriggerPayload,
    scalp_playbook_ref,
)
from halpha.planning.transitions import ControlIntent
from halpha.user_workbench.commands import build_command, initial_receipt


NOW = datetime(2026, 8, 16, tzinfo=UTC)


def _inputs(*, entry_mode: str = "MARKET"):
    template = ScalpTemplate(entry_mode=entry_mode)
    maker_price = "100" if entry_mode == "MAKER_ONLY_SAME_SIDE" else None
    spec = template.order_schedule_spec(maker_limit_price=maker_price)
    snapshot = compile_order_schedule(
        spec,
        InstrumentOrderRules(
            source="BINANCE_DEMO_EXCHANGE_INFO",
            min_price="0.1",
            max_price="1000000",
            price_tick_size="0.1",
            limit_quantity_step="0.01",
            min_limit_quantity="0.01",
            max_limit_quantity="1000",
            market_quantity_step="0.1",
            min_market_quantity="0.1",
            max_market_quantity="100",
            min_notional="5",
            source_cutoff=NOW.isoformat(),
        ),
        venue_ref="BINANCE_USDM",
        instrument_ref="BTCUSDT-PERP",
        direction=Direction.LONG,
        max_notional=template.notional,
        schedule_ref="version-scalp",
        reference_price="100" if maker_price is None else None,
        evaluated_at=NOW,
    )
    assert snapshot.valid
    assert snapshot.full_fill_protection_estimate is not None
    content = TradePlanContent(
        plan_name="Scalp test",
        created_at=NOW,
        creator_kind="HUMAN",
        decision_context=PlanDecisionContext(
            rationale="人工选择方向并触发剥头皮周期",
            intent="PROFIT_SEEKING",
            setup_family="OTHER",
            playbook_ref=scalp_playbook_ref(template),
            evidence_cutoff=NOW,
        ),
        decision_basis=DraftDecisionBasis(
            kind="DIRECT_EXECUTION",
            decision_basis_ref="DIRECT_EXECUTION@1",
            parameters={},
        ),
        order_schedule_spec=spec,
        environment_id="demo-main",
        environment_kind="DEMO",
        authority_class="DEMO_VALIDATION",
        account_ref="demo-account",
        venue_ref="BINANCE_USDM",
        instrument_ref="BTCUSDT-PERP",
        direction="LONG",
        target_exposure=template.notional,
        requested_limits=RequestedLimits(
            max_margin=template.notional,
            max_notional=template.notional,
            max_allowed_loss=snapshot.full_fill_protection_estimate.maximum_projected_loss,
        ),
        valid_from=NOW,
        valid_until=NOW + timedelta(minutes=15),
        allowed_actions=direct_allowed_action_profiles(spec),
        terms={
            "one_entry_cycle": True,
            "resume_policy": "MANUAL_PLAN_RESUME",
            "workflow_kind": "SCALP_CYCLE",
            "scalp_template": template.model_dump(mode="json"),
            "scalp_template_digest": template.digest,
            "scalp_entry_limit_price": maker_price,
        },
    )
    cycle = ScalpCycleRecord(
        cycle_id="cycle-scalp",
        environment_id=content.environment_id,
        account_ref=content.account_ref,
        instrument_ref=content.instrument_ref,
        direction=content.direction,
        template=template,
        template_digest=template.digest,
        request_digest=content_digest({"request": "scalp"}),
        idempotency_key="scalp",
        plan_id="plan-scalp",
        plan_version_id="version-scalp",
        activation_id="activation-scalp",
        triggered_at=NOW,
    )
    return content, cycle, snapshot


@pytest.fixture
def service_state(monkeypatch: pytest.MonkeyPatch):
    drafts, versions, activations, cycles = {}, {}, {}, {}
    planning = SimpleNamespace(
        save_draft=lambda draft, **_: drafts.__setitem__(draft.plan_id, draft),
        get_draft=lambda plan_id, **_: drafts[plan_id],
        has_activation=lambda plan_id: any(
            versions[item.plan_version_ref].plan_id == plan_id
            for item in activations.values()
        ),
        insert_version=lambda version: versions.__setitem__(version.plan_version_id, version),
        get_version=lambda version_id: versions[version_id],
        lock_and_list_open_instrument_activations=lambda **_: (),
        insert_activation=lambda activation: activations.__setitem__(
            activation.activation_id, activation
        ),
    )
    scalp = SimpleNamespace(
        lock_open_cycle_scope=Mock(),
        has_open_cycle=lambda **_: bool(cycles),
        insert=lambda cycle: cycles.__setitem__(cycle.cycle_id, cycle),
    )
    monkeypatch.setattr(
        "halpha.planning.service.PostgreSQLScalpRepository", lambda *_: scalp
    )
    service = PlanningApplicationService(None, "demo-main")
    service._planning = planning
    monkeypatch.setattr(
        service,
        "new_risk_discipline_status",
        lambda **_: SimpleNamespace(new_risk_allowed=True, blocker_codes=()),
    )
    return service, SimpleNamespace(
        drafts=drafts, versions=versions, activations=activations, cycles=cycles, scalp=scalp
    )


def _create(service, content, cycle, snapshot):
    return service.create_scalp_cycle(
        cycle=cycle,
        content=content,
        environment_kind=EnvironmentKind.DEMO,
        authority_class=AuthorityClass.DEMO_VALIDATION,
        product_build_id="a" * 64,
        order_schedule_snapshot=snapshot,
        live_profit_qualification_checker=None,
        new_risk_discipline_policy=NewRiskDisciplinePolicy(),
    )


@pytest.mark.parametrize("entry_mode", ("MARKET", "MAKER_ONLY_SAME_SIDE"))
def test_only_dedicated_cycle_command_can_activate_scalp_snapshot(
    service_state, entry_mode: str
) -> None:
    service, state = service_state
    content, cycle, snapshot = _inputs(entry_mode=entry_mode)

    version, activation = _create(service, content, cycle, snapshot)

    assert version.ai_review_ref is None
    assert activation.order_schedule_snapshot == snapshot
    assert state.cycles == {cycle.cycle_id: cycle}
    assert state.activations == {cycle.activation_id: activation}
    review_checker = Mock(side_effect=AssertionError("ordinary entry must reject first"))
    with pytest.raises(ValueError, match="SCALP_CYCLE_INTERNAL_COMMAND_REQUIRED"):
        service.activate_version(
            plan_version_id=version.plan_version_id,
            activation_id="untracked-second-activation",
            environment_kind=EnvironmentKind.DEMO,
            authority_class=AuthorityClass.DEMO_VALIDATION,
            observed_at=NOW,
            order_schedule_snapshot=snapshot,
            ai_review_checker=review_checker,
        )
    assert state.activations == {cycle.activation_id: activation}
    review_checker.assert_not_called()


def test_normal_snapshot_builder_rejects_scalp_marker_even_with_review(service_state) -> None:
    service, state = service_state
    content, cycle, _ = _inputs()
    service.create_draft(plan_id=cycle.plan_id, content=content, observed_at=NOW)

    with pytest.raises(ValueError, match="SCALP_CYCLE_INTERNAL_COMMAND_REQUIRED"):
        service.build_activation_snapshot(
            plan_id=cycle.plan_id,
            expected_draft_version=1,
            plan_version_id=cycle.plan_version_id,
            product_build_id="a" * 64,
            snapshot_at=NOW,
            ai_review_ref="ordinary-review",
        )
    assert not state.versions


def test_ordinary_direct_plan_still_requires_ai_review(service_state) -> None:
    service, _ = service_state
    content, cycle, _ = _inputs()
    ordinary = content.model_copy(update={"terms": {}})
    service.create_draft(plan_id=cycle.plan_id, content=ordinary, observed_at=NOW)

    with pytest.raises(ValueError, match="PLAN_AI_REVIEW_REQUIRED"):
        service.build_activation_snapshot(
            plan_id=cycle.plan_id,
            expected_draft_version=1,
            plan_version_id=cycle.plan_version_id,
            product_build_id="a" * 64,
            snapshot_at=NOW,
        )


@pytest.mark.parametrize(
    "update",
    (
        {"account_ref": "another-account"},
        {"instrument_ref": "ETHUSDT-PERP"},
        {"template_digest": "0" * 64},
        {"triggered_at": NOW + timedelta(seconds=1)},
    ),
)
def test_internal_cycle_binding_rejects_mismatched_record_before_writes(
    service_state, update: dict[str, object]
) -> None:
    service, state = service_state
    content, cycle, snapshot = _inputs()

    with pytest.raises(ValueError, match="SCALP_CYCLE_IDENTITY_MISMATCH"):
        _create(service, content, cycle.model_copy(update=update), snapshot)
    assert not state.drafts
    assert not state.activations
    assert not state.cycles


def test_internal_cycle_command_rechecks_account_wide_open_cycle(service_state) -> None:
    service, state = service_state
    content, cycle, snapshot = _inputs()
    _create(service, content, cycle, snapshot)
    another = cycle.model_copy(update={"cycle_id": "other", "plan_id": "other-plan"})

    with pytest.raises(ValueError, match="SCALP_CYCLE_ALREADY_OPEN"):
        _create(service, content, another, snapshot)
    assert len(state.drafts) == len(state.activations) == len(state.cycles) == 1


def test_internal_cycle_cannot_skip_cap_admission(service_state, monkeypatch) -> None:
    service, state = service_state
    content, cycle, snapshot = _inputs()
    monkeypatch.setattr(
        service,
        "new_risk_discipline_status",
        lambda **_: SimpleNamespace(
            new_risk_allowed=False,
            blocker_codes=("NEW_RISK_DISCIPLINE_DAILY_LOSS_STOP",),
        ),
    )

    with pytest.raises(ValueError, match="NEW_RISK_DISCIPLINE_DAILY_LOSS_STOP"):
        _create(service, content, cycle, snapshot)
    assert not state.activations
    assert not state.cycles


def test_internal_cycle_rejects_a_compiled_snapshot_from_another_template(
    service_state,
) -> None:
    service, state = service_state
    content, cycle, _ = _inputs()
    _, _, maker_snapshot = _inputs(entry_mode="MAKER_ONLY_SAME_SIDE")

    with pytest.raises(ValueError, match="ORDER_SCHEDULE_SNAPSHOT_MISMATCH"):
        _create(service, content, cycle, maker_snapshot)
    assert not state.activations
    assert not state.cycles


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def transaction(self):
        return nullcontext(self)


def _api(monkeypatch: pytest.MonkeyPatch, **updates) -> PostgreSQLPlanningApi:
    api = PostgreSQLPlanningApi(
        database_name="unused-test-database",
        database_role_name="unused-test-role",
        password=SecretStr("unused-test-password"),
        environment_id="demo-main",
        environment_kind="DEMO",
        authority_class="DEMO_VALIDATION",
        account_ref="demo-account",
        product_build_id="a" * 64,
        **updates,
    )
    monkeypatch.setattr(api, "_connect", lambda: _Connection())
    return api


def test_ordinary_api_cannot_reactivate_an_existing_scalp_version(
    service_state, monkeypatch
) -> None:
    service, state = service_state
    content, cycle, snapshot = _inputs()
    _create(service, content, cycle, snapshot)
    api = _api(monkeypatch)
    monkeypatch.setattr(
        "halpha.app.planning_api.PlanningApplicationService", lambda *_: service
    )

    with pytest.raises(ValueError, match="SCALP_CYCLE_INTERNAL_COMMAND_REQUIRED"):
        api.activate(
            ActivationPayload(
                plan_version_id=cycle.plan_version_id,
                expected_schedule_digest=snapshot.schedule_digest,
            ),
            idempotency_key="ordinary-second-activation",
            observed_at=NOW,
            order_schedule_snapshot=snapshot,
        )
    assert len(state.activations) == len(state.cycles) == 1


@pytest.mark.parametrize("lookup_state", ("found", "absent", "other-account"))
def test_cycle_lookup_is_current_account_scoped_and_does_not_mutate(
    service_state, monkeypatch, lookup_state: str
) -> None:
    service, state = service_state
    content, cycle, snapshot = _inputs()
    _, activation = _create(service, content, cycle, snapshot)
    found = (
        None
        if lookup_state == "absent"
        else cycle.model_copy(update={"account_ref": "another-account"})
        if lookup_state == "other-account"
        else cycle
    )
    api = _api(monkeypatch, profile="BINANCE_LIVE_READ_ONLY")
    monkeypatch.setattr(
        "halpha.app.planning_api.PostgreSQLScalpRepository",
        lambda *_: SimpleNamespace(get_by_idempotency=lambda _key: found),
    )
    monkeypatch.setattr(
        "halpha.app.planning_api.PostgreSQLPlanningRepository",
        lambda *_: SimpleNamespace(get_activation=lambda _id: activation),
    )

    result = api.scalp_cycle_lookup(idempotency_key=cycle.idempotency_key)

    if lookup_state == "found":
        assert result.cycle == cycle
        assert result.activation["activation_id"] == activation.activation_id
        assert result.venue_write_created is False
    else:
        assert result is None
    assert len(state.drafts) == len(state.activations) == len(state.cycles) == 1


def test_trigger_rechecks_effective_write_gate_after_account_lock(monkeypatch) -> None:
    gate = {"open": True}

    def status():
        return LiveWriteGateStatus(
            product_build_id="a" * 64,
            product_build_consistent=True,
            configured_runtime_real_write_gate="OPEN" if gate["open"] else "CLOSED",
            runtime_real_write_gate="OPEN" if gate["open"] else "CLOSED",
        )

    api = _api(monkeypatch, profile="BINANCE_LIVE_WRITE", gate_status_provider=status)
    _, _, initial_snapshot = _inputs()
    payload = ScalpTriggerPayload(
        instrument_ref="BTCUSDT-PERP", direction="LONG", template=ScalpTemplate()
    )
    preview = api.scalp_cycle_preview(payload, idempotency_key="gate-changed")
    snapshot = compile_order_schedule(
        payload.template.order_schedule_spec(),
        initial_snapshot.instrument_rules,
        venue_ref="BINANCE_USDM",
        instrument_ref=payload.instrument_ref,
        direction=payload.direction,
        max_notional=payload.template.notional,
        schedule_ref=preview["plan_version_id"],
        reference_price="100",
        evaluated_at=NOW,
    )
    monkeypatch.setattr(
        "halpha.app.planning_api.PostgreSQLScalpRepository",
        lambda *_: SimpleNamespace(
            lock_open_cycle_scope=lambda **_: gate.update(open=False),
            get_by_idempotency=lambda _key: None,
        ),
    )
    monkeypatch.setattr(
        "halpha.app.planning_api.PlanningApplicationService",
        lambda *_: pytest.fail("closed gate must reject before creating any cycle"),
    )

    with pytest.raises(ValueError, match="LIVE_WRITE_GATE_MUST_BE_OPEN_FOR_SCALP"):
        api.trigger_scalp_cycle(
            payload,
            idempotency_key="gate-changed",
            observed_at=NOW,
            order_schedule_snapshot=snapshot,
        )


@pytest.mark.parametrize("provider", (None, lambda: False))
def test_trigger_requires_current_executor_readiness_inside_the_transaction(
    monkeypatch, provider
) -> None:
    api = _api(monkeypatch, scalp_executor_ready_provider=provider)
    _, _, initial_snapshot = _inputs()
    payload = ScalpTriggerPayload(
        instrument_ref="BTCUSDT-PERP", direction="LONG", template=ScalpTemplate()
    )
    preview = api.scalp_cycle_preview(payload, idempotency_key="executor-changed")
    snapshot = compile_order_schedule(
        payload.template.order_schedule_spec(),
        initial_snapshot.instrument_rules,
        venue_ref="BINANCE_USDM",
        instrument_ref=payload.instrument_ref,
        direction=payload.direction,
        max_notional=payload.template.notional,
        schedule_ref=preview["plan_version_id"],
        reference_price="100",
        evaluated_at=NOW,
    )
    lock = Mock()
    monkeypatch.setattr(
        "halpha.app.planning_api.PostgreSQLScalpRepository",
        lambda *_: SimpleNamespace(
            lock_open_cycle_scope=lock,
            get_by_idempotency=lambda _key: None,
        ),
    )
    monkeypatch.setattr(
        "halpha.app.planning_api.PlanningApplicationService",
        lambda *_: pytest.fail("not-ready executor must reject before any cycle write"),
    )

    with pytest.raises(ValueError, match="EXECUTOR_NOT_READY"):
        api.trigger_scalp_cycle(
            payload,
            idempotency_key="executor-changed",
            observed_at=NOW,
            order_schedule_snapshot=snapshot,
        )
    lock.assert_called_once_with(account_ref="demo-account")


@pytest.mark.parametrize(
    "lookup_state", ("found", "absent", "other-account", "other-target", "other-intent")
)
def test_exit_lookup_returns_only_the_exact_current_account_exit(
    monkeypatch, lookup_state: str
) -> None:
    command = build_command(
        command_id="exit-command",
        environment_id="demo-main",
        owner_scope="local-owner",
        idempotency_key="original-exit",
        activation_id="another-target" if lookup_state == "other-target" else "activation",
        expected_version=1,
        intent=(
            ControlIntent.STOP_NEW_RISK
            if lookup_state == "other-intent"
            else ControlIntent.EXIT_STRATEGY
        ),
        scope={},
        parameters={},
        submitted_at=NOW,
    )
    receipt = initial_receipt(command, receipt_id="exit-receipt", processing_owner="TRADEPLAN")
    found = None if lookup_state == "absent" else (command, receipt)
    api = _api(monkeypatch, profile="BINANCE_LIVE_READ_ONLY")
    monkeypatch.setattr(
        "halpha.app.planning_api.PostgreSQLCommandRepository",
        lambda *_: SimpleNamespace(find_by_idempotency=lambda *_: found),
    )
    monkeypatch.setattr(
        "halpha.app.planning_api.PostgreSQLPlanningRepository",
        lambda *_: SimpleNamespace(
            get_activation=lambda _id: SimpleNamespace(
                account_ref=("another-account" if lookup_state == "other-account" else "demo-account")
            )
        ),
    )

    result = api.exit_receipt_lookup("activation", idempotency_key="original-exit")

    if lookup_state == "found":
        assert result == receipt.model_dump(mode="json", exclude={"environment_id"})
    else:
        assert result is None
