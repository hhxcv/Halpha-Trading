from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

from halpha.capital.models import AuthorityClass, EnvironmentKind, RiskClass
from halpha.domain_values import content_digest
from halpha.executor.coordinator import (
    HalphaCoordinator,
    OrderScheduleCapRejected,
    _aggregate_protection_projection,
    _is_frozen_planned_scale_in_action,
    _protection_projection_state,
    _submission_block_reason,
)
from halpha.outcomes.models import (
    EvidencePurpose,
    PrimaryResult,
    Review,
    ReviewRevisionReason,
    ReviewStatus,
)
from halpha.outcomes.service import OutcomeApplicationService
from halpha.planning.models import (
    PlanActivation,
    PlanLifecycle,
    ProtectionState,
    RunState,
)
from halpha.planning.order_policies import (
    InitialStopSpec,
    ProtectionPolicy,
    RepriceEntryRule,
)
from halpha.planning.order_schedule import (
    AmountDistribution,
    InstrumentOrderRules,
    OrderScheduleSpec,
    PriceDistribution,
    SinglePrice,
    VenueOrderPolicy,
    VenueOrderType,
    compile_order_schedule,
)
from halpha.planning.order_schedule_actions import (
    materialize_direct_schedule,
    materialize_direct_schedule_reprice,
    materialize_direct_schedule_retry,
)
from halpha.planning.registry import DIRECT_EXECUTION_REF, Direction
from halpha.planning.transitions import record_direct_fill
from halpha.venue_integration.models import (
    BINANCE_USDM_VENUE_REF,
    ExecutionActionKind,
    ExecutionActionState,
    VenueFact,
    VenueFactKind,
    VenueFactSourceClass,
)
from halpha.venue_integration.binance_funding import FundingIncomeRecord
from halpha.venue_integration.facts import build_venue_fact
from halpha.venue_integration.gateway import VenueDefinitelyNotSubmitted
from halpha.venue_integration.nautilus_events import NormalizedNautilusEvent
from halpha.venue_integration.service import VenueFactApplicationResult


def _action(kind: ExecutionActionKind, state: ExecutionActionState):
    return SimpleNamespace(action_kind=kind, state=state)


def _order_fact(status: str):
    observed_at = datetime(2026, 7, 23, tzinfo=UTC)
    return SimpleNamespace(
        kind=VenueFactKind.ORDER_STATE,
        payload={"status": status},
        source_time=observed_at,
        cutoff=observed_at,
        received_at=observed_at,
        venue_fact_id=f"order-{status.lower()}",
    )


def _condition_top_of_book_fact(
    activation: PlanActivation,
    *,
    condition_cutoff: datetime,
) -> VenueFact:
    source = "BINANCE_DEMO_PUBLIC"
    source_time = condition_cutoff - timedelta(seconds=1)
    payload = {
        "bid_price": "99.9",
        "ask_price": "100.1",
        "unit": "USDT",
        "source": source,
    }
    source_object_id = (
        f"{source}:{activation.instrument_ref}:{VenueFactKind.TOP_OF_BOOK.value}"
    )
    source_sequence = content_digest(
        {
            "source_time": source_time,
            "payload": payload,
        }
    )
    venue_fact_id = str(
        uuid5(
            NAMESPACE_URL,
            ":".join(
                (
                    "halpha",
                    activation.environment_id,
                    source_object_id,
                    source_sequence,
                )
            ),
        )
    )
    return build_venue_fact(
        venue_fact_id=venue_fact_id,
        environment_id=activation.environment_id,
        venue_ref=BINANCE_USDM_VENUE_REF,
        account_ref=None,
        instrument_ref=activation.instrument_ref,
        kind=VenueFactKind.TOP_OF_BOOK,
        source_class=VenueFactSourceClass.VENUE_STREAM,
        source_object_id=source_object_id,
        source_sequence=source_sequence,
        source_time=source_time,
        received_at=condition_cutoff,
        cutoff=condition_cutoff,
        payload=payload,
    )


def _direct_schedule_fixture(
    *,
    level_count: int = 3,
    market: bool = False,
    reprice: bool = False,
) -> tuple[PlanActivation, tuple, datetime]:
    created_at = datetime(2026, 7, 23, tzinfo=UTC)
    entry_valid_until = created_at + timedelta(hours=1)
    rules = InstrumentOrderRules(
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
        source_cutoff=created_at.isoformat(),
    )
    spec = OrderScheduleSpec(
        price_distribution=(
            SinglePrice()
            if market
            else SinglePrice(limit_price="100")
            if reprice
            else PriceDistribution(
                lower_price="90",
                upper_price="110",
                level_count=level_count,
            )
        ),
        amount_distribution=AmountDistribution(
            base_notional="100" if market or reprice else "20"
        ),
        venue_policy=(
            VenueOrderPolicy(
                order_type=VenueOrderType.MARKET,
                time_in_force=None,
            )
            if market
            else VenueOrderPolicy(post_only=True)
        ),
        protection_policy=ProtectionPolicy(
            initial_stop=InitialStopSpec(distance_bps="100")
        ),
        dynamic_rules=(RepriceEntryRule(),) if reprice else (),
    )
    snapshot = compile_order_schedule(
        spec,
        rules,
        venue_ref="BINANCE_USDM",
        instrument_ref="BTCUSDT-PERP",
        direction=Direction.LONG,
        max_notional="100",
        schedule_ref="plan-version-001",
        reference_price="100",
    )
    assert snapshot.valid
    activation = PlanActivation(
        activation_id="activation",
        environment_id="demo",
        environment_kind=EnvironmentKind.DEMO,
        authority_class=AuthorityClass.DEMO_VALIDATION,
        plan_version_ref="plan-version-001",
        account_ref="demo-account",
        instrument_ref="BTCUSDT-PERP",
        direction=Direction.LONG,
        decision_basis_ref=DIRECT_EXECUTION_REF,
        framework_strategy_id="HALPHA-INTERNAL-001",
        order_schedule_snapshot=snapshot,
        target_exposure="100",
        rule_state={"deadlines": {"entry_valid_until": entry_valid_until.isoformat()}},
        created_at=created_at,
        updated_at=created_at,
    )
    legs = materialize_direct_schedule(
        activation,
        entry_valid_until=entry_valid_until,
    )
    return activation, legs, entry_valid_until


def _with_conflicting_schedule_digest(legs: tuple) -> tuple:
    first = legs[0]
    execution_context = dict(first.proposed_action.execution_context)
    execution_context["order_schedule"] = {
        **execution_context["order_schedule"],
        "schedule_digest": "f" * 64,
    }
    return (
        first.model_copy(
            update={
                "proposed_action": first.proposed_action.model_copy(
                    update={"execution_context": execution_context}
                )
            }
        ),
        *legs[1:],
    )


def test_working_protection_projects_working() -> None:
    assert (
        _protection_projection_state(
            _action(
                ExecutionActionKind.PROTECTION,
                ExecutionActionState.OPEN,
            ),
            _order_fact("WORKING"),
        )
        is ProtectionState.WORKING
    )


def test_terminal_unfilled_protection_projects_gap() -> None:
    for status in ("CANCELLED", "REJECTED", "EXPIRED"):
        assert (
            _protection_projection_state(
                _action(ExecutionActionKind.PROTECTION, ExecutionActionState.OPEN),
                _order_fact(status),
            )
            is ProtectionState.GAP
        )


def test_non_protection_or_non_projectable_state_has_no_projection() -> None:
    assert (
        _protection_projection_state(
            _action(ExecutionActionKind.ENTRY, ExecutionActionState.OPEN),
            _order_fact("WORKING"),
        )
        is None
    )


def test_account_funding_is_allocated_exactly_once_across_virtual_positions() -> None:
    first = _direct_schedule_fixture()[0]
    second = first.model_copy(
        update={
            "activation_id": "activation-2",
            "plan_version_ref": "plan-version-002",
            "framework_strategy_id": "HALPHA-INTERNAL-002",
        }
    )
    event_time = first.created_at + timedelta(minutes=5)
    actions = {
        first.activation_id: (
            SimpleNamespace(
                activation_id=first.activation_id,
                account_ref=first.account_ref,
                execution_action_id="entry-1",
                action_kind=ExecutionActionKind.ENTRY,
                action_terms={"quantity": "0.01", "price": "100"},
                client_order_id="1" * 32,
                state=ExecutionActionState.CLOSED,
                created_at=first.created_at,
            ),
        ),
        second.activation_id: (
            SimpleNamespace(
                activation_id=second.activation_id,
                account_ref=second.account_ref,
                execution_action_id="entry-2",
                action_kind=ExecutionActionKind.ENTRY,
                action_terms={"quantity": "0.02", "price": "100"},
                client_order_id="2" * 32,
                state=ExecutionActionState.CLOSED,
                created_at=second.created_at,
            ),
        ),
    }
    facts = {
        "entry-1": (
            SimpleNamespace(
                kind=VenueFactKind.FILL,
                action_ref="entry-1",
                activation_ref=first.activation_id,
                venue_fact_id="fill-1",
                payload={"last_quantity": "0.01"},
                source_time=event_time - timedelta(minutes=1),
            ),
        ),
        "entry-2": (
            SimpleNamespace(
                kind=VenueFactKind.FILL,
                action_ref="entry-2",
                activation_ref=second.activation_id,
                venue_fact_id="fill-2",
                payload={"last_quantity": "0.02"},
                source_time=event_time - timedelta(minutes=1),
            ),
        ),
    }
    stored_by_source: dict[str, VenueFact] = {}

    class Connection:
        @staticmethod
        def transaction():
            return nullcontext()

    def apply_fact(*, fact: VenueFact, **_kwargs):
        stored_by_source[str(fact.source_object_id)] = fact
        return None

    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = Connection()
    coordinator._environment_id = first.environment_id
    coordinator._venue_ref = BINANCE_USDM_VENUE_REF
    coordinator._planning = SimpleNamespace(
        get_activation=lambda _activation_id: first,
        list_account_instrument_activations=lambda **_kwargs: (first, second),
    )
    coordinator._action_repository = SimpleNamespace(
        list_for_activation=lambda activation_id: actions[activation_id]
    )
    coordinator._fact_repository = SimpleNamespace(
        list_for_action=lambda action_id: facts[action_id],
        find_by_source=lambda fact: stored_by_source.get(str(fact.source_object_id)),
    )
    coordinator._execution = SimpleNamespace(apply_venue_fact=apply_fact)
    coordinator._refresh_completed_reviews_after_commit = lambda **_kwargs: None
    record = FundingIncomeRecord(
        transaction_id="funding-1",
        symbol="BTCUSDT",
        income="-0.00000005",
        asset="USDT",
        source_time=event_time,
    )

    inserted = coordinator.record_funding_income(
        activation_id=first.activation_id,
        records=(record,),
        observed_at=event_time + timedelta(seconds=1),
    )
    replay = coordinator.record_funding_income(
        activation_id=second.activation_id,
        records=(record,),
        observed_at=event_time + timedelta(seconds=2),
    )

    allocations = tuple(
        fact
        for fact in inserted
        if fact.payload["record_type"] == "ACTIVATION_FUNDING_ALLOCATION"
    )
    assert len(inserted) == 3
    assert replay == ()
    assert {fact.activation_ref: fact.payload["income"] for fact in allocations} == {
        first.activation_id: "-0.00000002",
        second.activation_id: "-0.00000003",
    }
    assert sum(
        (Decimal(fact.payload["income"]) for fact in allocations),
        Decimal(0),
    ) == Decimal(record.income)


def test_late_working_fact_does_not_erase_existing_protection_gap() -> None:
    activation = _direct_schedule_fixture()[0].model_copy(
        update={"protection_state": ProtectionState.GAP}
    )
    updates: list[dict[str, object]] = []
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._planning = SimpleNamespace(
        get_activation=lambda *_args, **_kwargs: activation,
        update_protection_projection=lambda **kwargs: updates.append(kwargs),
    )
    coordinator._action_repository = SimpleNamespace(
        list_for_activation=lambda _activation_id: ()
    )
    coordinator._fact_repository = SimpleNamespace(
        list_for_action=lambda _action_id: ()
    )
    action = SimpleNamespace(
        action_kind=ExecutionActionKind.PROTECTION,
        activation_id=activation.activation_id,
    )

    coordinator._apply_protection_projection_from_fact(
        action=action,
        fact=_order_fact("WORKING"),
        observed_at=activation.updated_at,
    )

    assert updates == []


def test_protection_projection_reduces_all_fill_responsibilities() -> None:
    activation = _direct_schedule_fixture()[0]
    entry = SimpleNamespace(
        execution_action_id="entry",
        action_kind=ExecutionActionKind.ENTRY,
        action_terms={},
    )
    first = SimpleNamespace(
        execution_action_id="protection-1",
        action_kind=ExecutionActionKind.PROTECTION,
        state=ExecutionActionState.OPEN,
        action_terms={
            "quantity": "1",
            "execution_context": {"fill_fact_ref": "fill-1"},
        },
    )
    second = SimpleNamespace(
        execution_action_id="protection-2",
        action_kind=ExecutionActionKind.PROTECTION,
        state=ExecutionActionState.SUBMITTING,
        action_terms={
            "quantity": "1",
            "execution_context": {"fill_fact_ref": "fill-2"},
        },
    )
    facts = {
        "entry": (
            SimpleNamespace(kind=VenueFactKind.FILL, venue_fact_id="fill-1"),
            SimpleNamespace(kind=VenueFactKind.FILL, venue_fact_id="fill-2"),
        ),
        "protection-1": (_order_fact("WORKING"),),
        "protection-2": (),
    }

    mixed = _aggregate_protection_projection(
        activation,
        (entry, first, second),
        lambda action_id: facts[action_id],
    )
    facts["protection-2"] = (_order_fact("WORKING"),)
    covered = _aggregate_protection_projection(
        activation,
        (entry, first, second),
        lambda action_id: facts[action_id],
    )
    first.state = ExecutionActionState.NOT_SUBMITTED
    gap = _aggregate_protection_projection(
        activation,
        (entry, first, second),
        lambda action_id: facts[action_id],
    )

    assert mixed is ProtectionState.UNKNOWN
    assert covered is ProtectionState.WORKING
    assert gap is ProtectionState.GAP


def test_protection_projection_treats_venue_quantity_drift_as_gap() -> None:
    activation = _direct_schedule_fixture()[0]
    entry = SimpleNamespace(
        execution_action_id="entry",
        action_kind=ExecutionActionKind.ENTRY,
        action_terms={},
    )
    protection = SimpleNamespace(
        execution_action_id="protection",
        action_kind=ExecutionActionKind.PROTECTION,
        state=ExecutionActionState.OPEN,
        action_terms={
            "quantity": "1",
            "execution_context": {"fill_fact_ref": "fill-1"},
        },
    )
    working = _order_fact("WORKING")
    working.payload["venue_order_quantity"] = "0.5"
    facts = {
        "entry": (SimpleNamespace(kind=VenueFactKind.FILL, venue_fact_id="fill-1"),),
        "protection": (working,),
    }

    projection = _aggregate_protection_projection(
        activation,
        (entry, protection),
        lambda action_id: facts[action_id],
    )

    assert projection is ProtectionState.GAP


def test_protection_projection_keeps_coverage_when_tighter_replacement_is_working() -> (
    None
):
    activation = _direct_schedule_fixture()[0]
    entry = SimpleNamespace(
        execution_action_id="entry",
        action_kind=ExecutionActionKind.ENTRY,
        action_terms={},
    )
    old = SimpleNamespace(
        execution_action_id="protection-old",
        action_kind=ExecutionActionKind.PROTECTION,
        state=ExecutionActionState.CLOSED,
        action_terms={
            "quantity": "1",
            "execution_context": {"fill_fact_ref": "fill-1"},
        },
    )
    replacement = SimpleNamespace(
        execution_action_id="protection-replacement",
        action_kind=ExecutionActionKind.PROTECTION,
        state=ExecutionActionState.OPEN,
        action_terms={
            "quantity": "1",
            "execution_context": {
                "fill_fact_ref": "fill-1",
                "protection_replacement": {"step_index": 0},
            },
        },
    )
    facts = {
        "entry": (SimpleNamespace(kind=VenueFactKind.FILL, venue_fact_id="fill-1"),),
        "protection-old": (_order_fact("CANCELLED"),),
        "protection-replacement": (_order_fact("WORKING"),),
    }

    projection = _aggregate_protection_projection(
        activation,
        (entry, old, replacement),
        lambda action_id: facts[action_id],
    )

    assert projection is ProtectionState.WORKING


def test_protection_projection_keeps_replacement_handoff_unknown_not_gap() -> None:
    activation = _direct_schedule_fixture()[0]
    entry = SimpleNamespace(
        execution_action_id="entry",
        action_kind=ExecutionActionKind.ENTRY,
        action_terms={},
    )
    old = SimpleNamespace(
        execution_action_id="protection-old",
        action_kind=ExecutionActionKind.PROTECTION,
        state=ExecutionActionState.CLOSED,
        action_terms={
            "quantity": "1",
            "execution_context": {"fill_fact_ref": "fill-1"},
        },
    )
    replacement = SimpleNamespace(
        execution_action_id="protection-replacement",
        action_kind=ExecutionActionKind.PROTECTION,
        state=ExecutionActionState.READY,
        action_terms={
            "quantity": "1",
            "execution_context": {
                "fill_fact_ref": "fill-1",
                "protection_replacement": {"step_index": 0},
            },
        },
    )
    facts = {
        "entry": (SimpleNamespace(kind=VenueFactKind.FILL, venue_fact_id="fill-1"),),
        "protection-old": (_order_fact("CANCELLED"),),
        "protection-replacement": (),
    }

    projection = _aggregate_protection_projection(
        activation,
        (entry, old, replacement),
        lambda action_id: facts[action_id],
    )

    assert projection is ProtectionState.UNKNOWN


def test_unprotectable_direct_fill_is_persisted_as_gap_without_action() -> None:
    activation = _direct_schedule_fixture(level_count=2)[0]
    observed_at = activation.updated_at + timedelta(seconds=1)
    fill = SimpleNamespace(
        kind=VenueFactKind.FILL,
        action_ref="entry-action",
        activation_ref=activation.activation_id,
        payload={"last_price": "100", "last_quantity": "0.1"},
        source_time=observed_at,
        cutoff=observed_at,
        venue_fact_id="fill-invalid-target",
        source_class=VenueFactSourceClass.VENUE_STREAM,
        source_object_id="trade-invalid-target",
        source_sequence="1",
        content_digest="c" * 64,
    )
    entry_action = SimpleNamespace(
        execution_action_id="entry-action",
        activation_id=activation.activation_id,
        action_kind=ExecutionActionKind.ENTRY,
        action_terms={
            "execution_context": {
                "protection_policy": ProtectionPolicy(
                    initial_stop=InitialStopSpec(distance_bps="1")
                ).model_dump(mode="json"),
                "order_schedule": {
                    "price_tick_size": "1",
                    "quantity_step": "0.1",
                },
            }
        },
    )
    recorded_events: list[dict[str, object]] = []
    projections: list[dict[str, object]] = []
    persisted_activations: list[PlanActivation] = []

    def persist_direct_fill(**values: object) -> PlanActivation:
        values = dict(values)
        values.pop("activation_id")
        persisted = record_direct_fill(activation, **values)
        persisted_activations.append(persisted)
        return persisted

    def record_event(**values: object) -> SimpleNamespace:
        recorded_events.append(values)
        return SimpleNamespace(**values)

    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._execution = SimpleNamespace(
        apply_venue_fact=lambda **_kwargs: entry_action
    )
    coordinator._planning = SimpleNamespace(
        record_direct_fill=persist_direct_fill,
        record_plan_event=record_event,
        update_protection_projection=lambda **values: projections.append(values),
    )

    result = coordinator.create_protection_for_fill(
        fill_fact=fill,
        plan_event_id="plan-event-protection-gap",
        execution_action_id="must-not-be-created",
        action_check=object(),
        observed_at=observed_at,
    )

    assert result.execution_action is None
    assert persisted_activations[0].has_entry_fill
    direct_fill = persisted_activations[0].rule_state["direct_protection"]["fills"][
        fill.venue_fact_id
    ]
    assert direct_fill["protection_error"] == "PROTECTION_PRICE_INVALID"
    assert recorded_events[0]["no_action_reason"] == "PROTECTION_PRICE_INVALID"
    assert projections[0]["protection_state"] is ProtectionState.GAP
    assert (
        _protection_projection_state(
            _action(
                ExecutionActionKind.PROTECTION,
                ExecutionActionState.UNKNOWN,
            ),
            SimpleNamespace(kind=VenueFactKind.COMMISSION, payload={}),
        )
        is None
    )


def test_direct_pre_submit_rejection_is_replay_safe_and_auditable() -> None:
    observed_at = datetime(2026, 7, 23, 8, 1, tzinfo=UTC)
    recorded_events: list[dict[str, object]] = []

    def record_event(**values: object) -> SimpleNamespace:
        recorded_events.append(values)
        return SimpleNamespace(**values)

    coordinator = object.__new__(HalphaCoordinator)
    coordinator._environment_id = "demo"
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._planning = SimpleNamespace(record_plan_event=record_event)

    first = coordinator.record_direct_pre_submit_rejection(
        activation_id="activation-direct",
        execution_action_id="entry-leg-1",
        reason_code="ACCOUNT_MARGIN_MODE_NOT_ISOLATED",
        observed_at=observed_at,
    )
    second = coordinator.record_direct_pre_submit_rejection(
        activation_id="activation-direct",
        execution_action_id="entry-leg-1",
        reason_code="ACCOUNT_MARGIN_MODE_NOT_ISOLATED",
        observed_at=observed_at,
    )

    assert first.plan_event_id == second.plan_event_id
    assert first.input_digest == second.input_digest
    assert first.rule_id == "DIRECT_PRE_SUBMIT"
    assert first.no_action_reason == "ACCOUNT_MARGIN_MODE_NOT_ISOLATED"
    assert first.condition_judgement.next_responsibility == "EXECUTOR_RETRY"
    assert recorded_events[0]["source_identity"] == (
        "activation-direct:DIRECT_PRE_SUBMIT:"
        "entry-leg-1:ACCOUNT_MARGIN_MODE_NOT_ISOLATED"
    )


def test_executor_runtime_reattached_is_replay_safe_and_informational() -> None:
    observed_at = datetime(2026, 7, 28, 6, 44, 6, tzinfo=UTC)
    recorded_events: list[dict[str, object]] = []

    def record_event(**values: object) -> SimpleNamespace:
        recorded_events.append(values)
        return SimpleNamespace(**values)

    coordinator = object.__new__(HalphaCoordinator)
    coordinator._environment_id = "demo"
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._planning = SimpleNamespace(record_plan_event=record_event)

    first = coordinator.record_executor_runtime_reattached(
        activation_id="activation-direct",
        observed_at=observed_at,
    )
    second = coordinator.record_executor_runtime_reattached(
        activation_id="activation-direct",
        observed_at=observed_at,
    )

    assert first.plan_event_id == second.plan_event_id
    assert first.input_digest == second.input_digest
    assert first.rule_id == "EXECUTOR_RUNTIME_CONTINUITY"
    assert first.no_action_reason == "EXECUTOR_RUNTIME_REATTACHED"
    assert first.condition_judgement is None
    assert first.capital_decision == {
        "accepted": False,
        "reason_code": "NOT_APPLICABLE_RUNTIME_CONTINUITY",
    }
    assert recorded_events[0]["source_cutoff"] == observed_at


def test_paused_activation_blocks_only_risk_increasing_submission() -> None:
    paused = SimpleNamespace(
        lifecycle=PlanLifecycle.RUNNING,
        run_state=RunState.PAUSED,
    )
    increasing = SimpleNamespace(action_class=RiskClass.RISK_INCREASING)
    neutral = SimpleNamespace(action_class=RiskClass.RISK_NEUTRAL)
    reducing = SimpleNamespace(action_class=RiskClass.RISK_REDUCING)

    assert _submission_block_reason(increasing, paused) == "NEW_RISK_STOPPED"
    assert _submission_block_reason(neutral, paused) is None
    assert _submission_block_reason(reducing, paused) is None


def test_user_takeover_or_completion_blocks_every_submission_class() -> None:
    action = SimpleNamespace(action_class=RiskClass.RISK_REDUCING)
    for lifecycle in (PlanLifecycle.USER_TAKEOVER, PlanLifecycle.COMPLETED):
        activation = SimpleNamespace(
            lifecycle=lifecycle,
            run_state=RunState.PAUSED,
        )
        assert _submission_block_reason(action, activation) == "USER_TAKEOVER_ACTIVE"


def test_live_submission_guard_rechecks_the_exact_activation() -> None:
    observed: list[str] = []
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._environment_kind = "LIVE"
    coordinator._live_write_submission_guard = observed.append

    coordinator._require_current_live_write_gate("activation-live-001")

    assert observed == ["activation-live-001"]


def test_live_submission_guard_fails_closed_without_leaking_internal_error() -> None:
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._environment_kind = "LIVE"

    def fail(_activation_id: str) -> None:
        raise ValueError("sensitive-detail")

    coordinator._live_write_submission_guard = fail

    with pytest.raises(RuntimeError, match="^RUNTIME_REAL_WRITE_GATE_CLOSED$"):
        coordinator._require_current_live_write_gate("activation-live-001")


def test_unknown_nautilus_result_records_specific_reason_without_terminal_fact() -> (
    None
):
    observed_at = datetime(2026, 7, 20, 4, 59, tzinfo=UTC)
    action = SimpleNamespace(execution_action_id="execution-action-unknown")
    recorded: list[dict[str, object]] = []
    normalized = NormalizedNautilusEvent(
        action=action,
        facts=(),
        result_unknown=True,
        unknown_reason="VENUE_SUBMISSION_RESULT_UNKNOWN",
    )

    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._execution = SimpleNamespace(
        record_submission_unknown=lambda execution_action_id, **values: recorded.append(
            {"execution_action_id": execution_action_id, **values}
        )
    )
    normalizer = SimpleNamespace(normalize=lambda _event, **_values: normalized)

    result = coordinator.handle_nautilus_order_event(
        normalizer,
        object(),
        observed_at=observed_at,
    )

    assert result is normalized
    assert recorded == [
        {
            "execution_action_id": "execution-action-unknown",
            "reason": "VENUE_SUBMISSION_RESULT_UNKNOWN",
            "next_query_at": observed_at + timedelta(seconds=10),
            "observed_at": observed_at,
        }
    ]


def test_denied_protection_atomically_projects_aggregate_gap() -> None:
    observed_at = datetime(2026, 7, 23, 6, 45, tzinfo=UTC)
    activation = _direct_schedule_fixture()[0].model_copy(
        update={"protection_state": ProtectionState.UNKNOWN}
    )
    entry = SimpleNamespace(
        execution_action_id="entry-action",
        activation_id=activation.activation_id,
        action_kind=ExecutionActionKind.ENTRY,
        action_terms={},
    )
    protection = SimpleNamespace(
        execution_action_id="protection-denied",
        activation_id=activation.activation_id,
        action_kind=ExecutionActionKind.PROTECTION,
        state=ExecutionActionState.SUBMITTING,
        action_terms={"execution_context": {"fill_fact_ref": "entry-fill"}},
    )
    entry_fill = SimpleNamespace(
        kind=VenueFactKind.FILL,
        venue_fact_id="entry-fill",
    )
    projected: list[ProtectionState] = []

    class Planning:
        current = activation

        @classmethod
        def get_activation(cls, *_args, **_kwargs):
            return cls.current

        @classmethod
        def update_protection_projection(cls, **values):
            projected.append(values["protection_state"])
            cls.current = cls.current.model_copy(
                update={"protection_state": values["protection_state"]}
            )
            return cls.current

    def deny(_action_id: str, **_kwargs: object) -> SimpleNamespace:
        protection.state = ExecutionActionState.NOT_SUBMITTED
        return protection

    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._planning = Planning()
    coordinator._execution = SimpleNamespace(record_definitely_not_submitted=deny)
    coordinator._action_repository = SimpleNamespace(
        list_for_activation=lambda _activation_id: (entry, protection),
    )
    coordinator._fact_repository = SimpleNamespace(
        list_for_action=lambda action_id: (
            (entry_fill,) if action_id == entry.execution_action_id else ()
        )
    )
    normalizer = SimpleNamespace(
        normalize=lambda _event, **_kwargs: NormalizedNautilusEvent(
            action=protection,
            facts=(),
            definitely_not_submitted=True,
        )
    )

    coordinator.handle_nautilus_order_event(
        normalizer,
        object(),
        observed_at=observed_at,
    )

    assert projected == [ProtectionState.GAP]


def test_due_unknown_action_query_uses_only_its_persisted_identity() -> None:
    observed_at = datetime(2026, 7, 20, 5, 0, tzinfo=UTC)
    action = SimpleNamespace(execution_action_id="execution-action-unknown")
    prepared: list[dict[str, object]] = []
    queried: list[str] = []

    def prepare(execution_action_id: str, **values: object) -> object:
        prepared.append({"execution_action_id": execution_action_id, **values})
        return action

    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._execution = SimpleNamespace(prepare_due_unknown_query=prepare)
    coordinator._gate = SimpleNamespace(query_original_identity=queried.append)

    attempted = coordinator.query_unknown_action_if_due(
        action.execution_action_id,
        observed_at=observed_at,
    )

    assert attempted is True
    assert queried == ["execution-action-unknown"]
    assert prepared == [
        {
            "execution_action_id": "execution-action-unknown",
            "next_query_at": observed_at + timedelta(seconds=10),
            "observed_at": observed_at,
        }
    ]


def test_startup_recovery_queries_submitting_unknown_and_open_without_releasing_on_dispatch() -> (
    None
):
    observed_at = datetime(2026, 7, 23, 6, 0, tzinfo=UTC)
    actions = (
        SimpleNamespace(
            execution_action_id="startup-submitting",
            activation_id="activation-a",
            state=ExecutionActionState.UNKNOWN,
        ),
        SimpleNamespace(
            execution_action_id="startup-unknown",
            activation_id="activation-a",
            state=ExecutionActionState.UNKNOWN,
        ),
        SimpleNamespace(
            execution_action_id="startup-open",
            activation_id="activation-b",
            state=ExecutionActionState.OPEN,
        ),
    )
    queried: list[str] = []

    def query(action_id: str) -> None:
        queried.append(action_id)
        if action_id == "startup-open":
            raise ConnectionError("query transport unavailable")

    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._execution = SimpleNamespace(
        prepare_startup_reconciliation=lambda **_kwargs: actions
    )
    coordinator._action_repository = SimpleNamespace(
        get=lambda action_id: next(
            action for action in actions if action.execution_action_id == action_id
        )
    )
    coordinator._gate = SimpleNamespace(query_original_identity=query)
    coordinator.arm_startup_recovery_barrier()

    recovered = coordinator.initialize_startup_recovery(observed_at=observed_at)
    dispatched = coordinator.query_prepared_startup_recovery(observed_at=observed_at)

    assert recovered == actions
    assert dispatched == ("startup-submitting", "startup-unknown")
    assert queried == [action.execution_action_id for action in actions]
    assert coordinator.startup_recovery_complete() is False
    assert coordinator.startup_recovery_allows_submission("activation-a") is False
    assert coordinator.startup_recovery_allows_submission("activation-b") is False
    assert (
        coordinator.startup_recovery_allows_submission("activation-unaffected") is True
    )

    # Dispatch success and one transport failure are both non-authoritative.
    assert (
        coordinator.retry_startup_recovery_queries(
            observed_at=observed_at + timedelta(seconds=9)
        )
        == ()
    )
    retried = coordinator.retry_startup_recovery_queries(
        observed_at=observed_at + timedelta(seconds=10)
    )
    assert retried == ("startup-submitting", "startup-unknown")
    assert coordinator.startup_recovery_complete() is False
    assert queried == [
        *(action.execution_action_id for action in actions),
        *(action.execution_action_id for action in actions),
    ]


def test_startup_recovery_prepares_before_query_and_skips_identity_resolved_by_framework() -> (
    None
):
    observed_at = datetime(2026, 7, 23, 6, 15, tzinfo=UTC)
    actions = (
        SimpleNamespace(
            execution_action_id="startup-resolved-by-framework",
            activation_id="activation-a",
        ),
        SimpleNamespace(
            execution_action_id="startup-still-pending",
            activation_id="activation-b",
        ),
    )
    queried: list[str] = []
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._execution = SimpleNamespace(
        prepare_startup_reconciliation=lambda **_kwargs: actions
    )
    coordinator._gate = SimpleNamespace(query_original_identity=queried.append)
    coordinator.arm_startup_recovery_barrier()

    recovered = coordinator.initialize_startup_recovery(observed_at=observed_at)
    coordinator._resolve_startup_recovery_actions(("startup-resolved-by-framework",))
    dispatched = coordinator.query_prepared_startup_recovery(
        observed_at=observed_at + timedelta(seconds=10)
    )

    assert recovered == actions
    assert dispatched == ("startup-still-pending",)
    assert queried == ["startup-still-pending"]
    assert coordinator.startup_recovery_pending_action_ids() == (
        "startup-still-pending",
    )


def test_startup_recovery_releases_called_action_after_user_handover() -> None:
    observed_at = datetime(2026, 7, 23, 6, 20, tzinfo=UTC)
    action = SimpleNamespace(
        execution_action_id="startup-handed-over",
        activation_id="activation-a",
        state=ExecutionActionState.UNKNOWN,
    )
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._execution = SimpleNamespace(
        prepare_startup_reconciliation=lambda **_kwargs: (action,)
    )
    coordinator._action_repository = SimpleNamespace(get=lambda _action_id: action)
    coordinator.arm_startup_recovery_barrier()
    coordinator.initialize_startup_recovery(observed_at=observed_at)

    assert coordinator.startup_recovery_complete() is False
    action.state = ExecutionActionState.HANDED_OVER

    assert coordinator.refresh_startup_recovery_state() == (
        action.execution_action_id,
    )
    assert coordinator.startup_recovery_complete() is True
    assert coordinator.startup_recovery_allows_submission("activation-a") is True


@pytest.mark.parametrize("inserted", (True, False))
def test_authoritative_startup_query_fact_releases_barrier_only_after_commit(
    inserted: bool,
) -> None:
    observed_at = datetime(2026, 7, 23, 6, 30, tzinfo=UTC)
    action = SimpleNamespace(
        execution_action_id="startup-open",
        activation_id="activation-a",
        action_kind=ExecutionActionKind.ENTRY,
        state=ExecutionActionState.OPEN,
    )
    trace: list[str] = []

    class Transaction:
        def __enter__(self):
            trace.append("BEGIN")

        def __exit__(self, *_args):
            trace.append("COMMIT")

    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=Transaction)
    coordinator._execution = SimpleNamespace(
        prepare_startup_reconciliation=lambda **_kwargs: (action,),
        apply_venue_fact_with_result=lambda *, fact, **_kwargs: (
            VenueFactApplicationResult(
                canonical_fact=fact,
                action=action,
                inserted=inserted,
            )
        ),
    )
    coordinator._action_repository = SimpleNamespace(
        get=lambda _action_id: action,
        find_open_cancel_for_target=lambda _client_order_id: None,
        list_open_cancels_for_target=lambda _client_order_id: (),
    )
    coordinator._gate = SimpleNamespace(query_original_identity=lambda _action_id: None)
    coordinator.arm_startup_recovery_barrier()
    coordinator.initialize_startup_recovery(
        observed_at=observed_at,
        resolution_sink=lambda activation_id, action_id: trace.append(
            f"RESOLVED:{activation_id}:{action_id}"
        ),
    )
    coordinator.query_prepared_startup_recovery(observed_at=observed_at)
    trace.clear()
    fact = SimpleNamespace(
        kind=VenueFactKind.ORDER_STATE,
        payload={"status": "WORKING"},
        source_time=observed_at + timedelta(seconds=1),
        cutoff=observed_at + timedelta(seconds=1),
        received_at=observed_at + timedelta(seconds=1),
        venue_fact_id="startup-query-fact",
    )
    normalized = NormalizedNautilusEvent(
        action=action,
        facts=(fact,),
        client_order_id="persisted-client-id",
    )
    normalizer = SimpleNamespace(normalize=lambda _event, **_kwargs: normalized)

    result = coordinator.handle_nautilus_order_event(
        normalizer,
        object(),
        observed_at=observed_at + timedelta(seconds=1),
    )

    assert result.facts == ((fact,) if inserted else ())
    assert trace == [
        "BEGIN",
        "COMMIT",
        "RESOLVED:activation-a:startup-open",
    ]
    assert coordinator.startup_recovery_complete() is True
    assert coordinator.startup_recovery_allows_submission("activation-a") is True


def test_stream_and_query_trade_replay_is_not_propagated_downstream_twice() -> None:
    observed_at = datetime(2026, 7, 23, 6, 35, tzinfo=UTC)
    action = SimpleNamespace(
        execution_action_id="entry-action",
        activation_id="activation-a",
        action_kind=ExecutionActionKind.ENTRY,
        state=ExecutionActionState.OPEN,
    )

    def trade_fact(
        *,
        fact_id: str,
        kind: VenueFactKind,
        source_class: VenueFactSourceClass,
        source_sequence: str,
        received_at: datetime,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            venue_fact_id=fact_id,
            kind=kind,
            source_class=source_class,
            source_object_id="trade-1",
            source_sequence=source_sequence,
            source_time=observed_at,
            received_at=received_at,
            cutoff=received_at,
            payload=(
                {"leaves_quantity": "0.001"}
                if kind is VenueFactKind.FILL
                else {"amount": "0.03 USDT", "currency": "USDT"}
            ),
            content_digest="f" * 64,
        )

    stream_fill = trade_fact(
        fact_id="fill-economic-version",
        kind=VenueFactKind.FILL,
        source_class=VenueFactSourceClass.VENUE_STREAM,
        source_sequence="stream-fill-event",
        received_at=observed_at,
    )
    stream_fee = trade_fact(
        fact_id="fee-economic-version",
        kind=VenueFactKind.COMMISSION,
        source_class=VenueFactSourceClass.VENUE_STREAM,
        source_sequence="stream-fee-event",
        received_at=observed_at,
    )
    query_fill = trade_fact(
        fact_id="fill-economic-version",
        kind=VenueFactKind.FILL,
        source_class=VenueFactSourceClass.VENUE_QUERY,
        source_sequence="query-fill-result",
        received_at=observed_at + timedelta(seconds=1),
    )
    query_fee = trade_fact(
        fact_id="fee-economic-version",
        kind=VenueFactKind.COMMISSION,
        source_class=VenueFactSourceClass.VENUE_QUERY,
        source_sequence="query-fee-result",
        received_at=observed_at + timedelta(seconds=1),
    )
    normalizations = iter(
        (
            NormalizedNautilusEvent(
                action=action,
                facts=(stream_fill, stream_fee),
            ),
            NormalizedNautilusEvent(
                action=action,
                facts=(query_fill, query_fee),
            ),
        )
    )
    persisted: dict[str, object] = {}
    protection_updates: list[object] = []
    received_events: list[object] = []

    def apply_fact(*, fact: object, **_kwargs: object) -> VenueFactApplicationResult:
        canonical = persisted.get(fact.venue_fact_id)
        inserted = canonical is None
        if inserted:
            persisted[fact.venue_fact_id] = fact
            canonical = fact
        return VenueFactApplicationResult(
            canonical_fact=canonical,
            action=action,
            inserted=inserted,
        )

    normalizer = SimpleNamespace(
        normalize=lambda event, **_kwargs: (
            received_events.append(event),
            next(normalizations),
        )[1]
    )
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._execution = SimpleNamespace(
        apply_venue_fact_with_result=apply_fact,
    )
    coordinator._apply_protection_projection_from_fact = lambda **values: (
        protection_updates.append(values["fact"])
        if values["fact"].kind is VenueFactKind.FILL
        else None
    )
    event = object()

    first_result = coordinator.handle_nautilus_order_event(
        normalizer,
        event,
        observed_at=observed_at,
    )
    duplicate_result = coordinator.handle_nautilus_order_event(
        normalizer,
        event,
        observed_at=observed_at + timedelta(seconds=1),
    )

    assert received_events == [event, event]
    assert first_result.facts == (stream_fill, stream_fee)
    assert duplicate_result.facts == ()
    assert tuple(persisted.values()) == (stream_fill, stream_fee)
    assert protection_updates == [stream_fill]


@pytest.mark.parametrize(
    "terminal_state",
    (
        ExecutionActionState.NOT_SUBMITTED,
        ExecutionActionState.HANDED_OVER,
    ),
)
def test_late_terminal_entry_fill_is_retained_and_stops_new_risk(
    terminal_state: ExecutionActionState,
) -> None:
    observed_at = datetime(2026, 7, 23, 6, 36, tzinfo=UTC)
    action = SimpleNamespace(
        execution_action_id="entry-action-terminal",
        activation_id="activation-a",
        action_kind=ExecutionActionKind.ENTRY,
        state=terminal_state,
    )
    fact = SimpleNamespace(
        venue_fact_id="late-entry-fill",
        kind=VenueFactKind.FILL,
        source_class=VenueFactSourceClass.VENUE_STREAM,
        source_object_id="trade-late-entry",
        source_sequence="late-entry-event",
        source_time=observed_at,
        received_at=observed_at,
        cutoff=observed_at,
        payload={"leaves_quantity": "0.001"},
        content_digest="a" * 64,
    )
    stop_calls: list[dict[str, object]] = []
    normalizer = SimpleNamespace(
        normalize=lambda _event, **_kwargs: NormalizedNautilusEvent(
            action=action,
            facts=(fact,),
        )
    )
    transaction_state = {"active": False}

    class _Transaction:
        def __enter__(self):
            transaction_state["active"] = True

        def __exit__(self, *_args):
            transaction_state["active"] = False

    def apply_fact(**_kwargs):
        assert transaction_state["active"] is True
        return VenueFactApplicationResult(
            canonical_fact=fact,
            action=action,
            inserted=True,
        )

    def stop_new_risk(**values):
        assert transaction_state["active"] is True
        stop_calls.append(values)

    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=_Transaction)
    coordinator._execution = SimpleNamespace(apply_venue_fact_with_result=apply_fact)
    coordinator._capital = SimpleNamespace(
        stop_new_risk_for_attributed_action_anomaly=stop_new_risk,
        stop_new_risk_for_external_activity=lambda **_values: pytest.fail(
            "an action-bound late fill is not external account activity"
        ),
    )
    coordinator._environment_kind = "DEMO"
    coordinator._authority_class = "DEMO_VALIDATION"
    coordinator._account_ref = "demo-owner"
    coordinator._apply_protection_projection_from_fact = lambda **_kwargs: pytest.fail(
        "a terminal late entry fill must not mutate plan projection"
    )

    result = coordinator.handle_nautilus_order_event(
        normalizer,
        object(),
        observed_at=observed_at,
    )

    assert result.facts == (fact,)
    assert len(stop_calls) == 1
    assert stop_calls[0]["evidence_digest"] == fact.content_digest
    assert stop_calls[0]["account_ref"] == "demo-owner"


def test_entry_quantity_conflict_stops_new_risk_but_keeps_protection_duty() -> None:
    observed_at = datetime(2026, 7, 23, 6, 37, tzinfo=UTC)
    action = SimpleNamespace(
        execution_action_id="entry-action-quantity-conflict",
        activation_id="activation-a",
        action_kind=ExecutionActionKind.ENTRY,
        state=ExecutionActionState.OPEN,
    )
    fact = SimpleNamespace(
        venue_fact_id="entry-fill-quantity-conflict",
        kind=VenueFactKind.FILL,
        source_class=VenueFactSourceClass.VENUE_STREAM,
        source_object_id="trade-entry-quantity-conflict",
        source_sequence="entry-quantity-conflict-event",
        source_time=observed_at,
        received_at=observed_at,
        cutoff=observed_at,
        payload={"last_quantity": "0.003", "leaves_quantity": "0"},
        content_digest="b" * 64,
    )
    stop_calls: list[dict[str, object]] = []
    projection_calls: list[dict[str, object]] = []
    normalizer = SimpleNamespace(
        normalize=lambda _event, **_kwargs: NormalizedNautilusEvent(
            action=action,
            facts=(fact,),
        )
    )
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._execution = SimpleNamespace(
        apply_venue_fact_with_result=lambda **_kwargs: VenueFactApplicationResult(
            canonical_fact=fact,
            action=action,
            inserted=True,
            action_quantity_conflict=True,
        )
    )
    coordinator._capital = SimpleNamespace(
        stop_new_risk_for_attributed_action_anomaly=(
            lambda **values: stop_calls.append(values)
        ),
        stop_new_risk_for_external_activity=lambda **_values: pytest.fail(
            "an action-bound quantity anomaly is not external account activity"
        ),
    )
    coordinator._environment_kind = "DEMO"
    coordinator._authority_class = "DEMO_VALIDATION"
    coordinator._account_ref = "demo-owner"
    coordinator._apply_protection_projection_from_fact = lambda **values: (
        projection_calls.append(values)
    )

    result = coordinator.handle_nautilus_order_event(
        normalizer,
        object(),
        observed_at=observed_at,
    )

    assert result.facts == (fact,)
    assert len(stop_calls) == 1
    assert stop_calls[0]["evidence_digest"] == fact.content_digest
    assert projection_calls == [
        {
            "action": action,
            "fact": fact,
            "observed_at": observed_at,
        }
    ]


def test_unattributed_venue_fact_remains_an_external_activity_conflict() -> None:
    observed_at = datetime(2026, 7, 23, 6, 38, tzinfo=UTC)
    fact = SimpleNamespace(
        venue_fact_id="unattributed-fill",
        activation_ref=None,
        attribution_class=None,
        impact_scope=None,
        kind=VenueFactKind.FILL,
        payload={"last_quantity": "0.001", "leaves_quantity": "0"},
        source_time=observed_at,
        cutoff=observed_at,
        received_at=observed_at,
        content_digest="c" * 64,
    )
    external_stops: list[dict[str, object]] = []
    normalizer = SimpleNamespace(
        normalize=lambda _event, **_kwargs: NormalizedNautilusEvent(
            action=None,
            facts=(fact,),
        )
    )
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._execution = SimpleNamespace(
        apply_venue_fact_with_result=lambda **_kwargs: VenueFactApplicationResult(
            canonical_fact=fact,
            action=None,
            inserted=True,
        )
    )
    coordinator._capital = SimpleNamespace(
        stop_new_risk_for_external_activity=(
            lambda **values: external_stops.append(values)
        ),
        stop_new_risk_for_attributed_action_anomaly=lambda **_values: pytest.fail(
            "an unattributed fact must not be downgraded to an action anomaly"
        ),
    )
    coordinator._environment_kind = "DEMO"
    coordinator._authority_class = "DEMO_VALIDATION"
    coordinator._account_ref = "demo-owner"

    result = coordinator.handle_nautilus_order_event(
        normalizer,
        object(),
        observed_at=observed_at,
    )

    assert result.facts == (fact,)
    assert len(external_stops) == 1
    assert external_stops[0]["evidence_digest"] == content_digest(
        (fact.content_digest,)
    )


@pytest.mark.parametrize(
    ("status", "reconciliation", "expected_stop_count"),
    [
        ("WORKING", False, 1),
        ("WORKING", True, 1),
        ("CANCELLED", False, 0),
        ("REJECTED", False, 0),
        ("EXPIRED", False, 0),
    ],
)
def test_unattributed_order_history_only_stops_for_current_working_activity(
    status: str,
    reconciliation: bool,
    expected_stop_count: int,
) -> None:
    observed_at = datetime(2026, 7, 23, 6, 38, tzinfo=UTC)
    fact = SimpleNamespace(
        venue_fact_id=f"unattributed-order-{status}-{reconciliation}",
        activation_ref=None,
        attribution_class=None,
        impact_scope=None,
        kind=VenueFactKind.ORDER_STATE,
        payload={
            "status": status,
            "reconciliation": reconciliation,
        },
        source_time=observed_at - timedelta(hours=1),
        cutoff=observed_at,
        received_at=observed_at,
        content_digest="e" * 64,
    )
    external_stops: list[dict[str, object]] = []
    normalizer = SimpleNamespace(
        normalize=lambda _event, **_kwargs: NormalizedNautilusEvent(
            action=None,
            facts=(fact,),
        )
    )
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._execution = SimpleNamespace(
        apply_venue_fact_with_result=lambda **_kwargs: VenueFactApplicationResult(
            canonical_fact=fact,
            action=None,
            inserted=True,
        )
    )
    coordinator._capital = SimpleNamespace(
        stop_new_risk_for_external_activity=(
            lambda **values: external_stops.append(values)
        ),
        stop_new_risk_for_attributed_action_anomaly=lambda **_values: pytest.fail(
            "an unattributed order fact is not an attributed action anomaly"
        ),
    )
    coordinator._environment_kind = "DEMO"
    coordinator._authority_class = "DEMO_VALIDATION"
    coordinator._account_ref = "demo-owner"

    result = coordinator.handle_nautilus_order_event(
        normalizer,
        object(),
        observed_at=observed_at,
    )

    assert result.facts == (fact,)
    assert len(external_stops) == expected_stop_count


@pytest.mark.parametrize(
    ("reconciliation", "source_offset", "expected_stop_count"),
    [
        (False, timedelta(hours=-24), 1),
        (True, timedelta(minutes=-61), 0),
        (True, timedelta(minutes=-59), 1),
    ],
)
def test_unattributed_fill_distinguishes_live_or_outage_activity_from_old_history(
    reconciliation: bool,
    source_offset: timedelta,
    expected_stop_count: int,
) -> None:
    observed_at = datetime(2026, 7, 23, 6, 38, tzinfo=UTC)
    fact = SimpleNamespace(
        venue_fact_id=f"unattributed-fill-{reconciliation}-{source_offset}",
        activation_ref=None,
        attribution_class=None,
        impact_scope=None,
        kind=VenueFactKind.FILL,
        payload={"reconciliation": reconciliation},
        source_time=observed_at + source_offset,
        cutoff=observed_at,
        received_at=observed_at,
        content_digest="f" * 64,
    )
    external_stops: list[dict[str, object]] = []
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._execution = SimpleNamespace(
        apply_venue_fact_with_result=lambda **_kwargs: VenueFactApplicationResult(
            canonical_fact=fact,
            action=None,
            inserted=True,
        )
    )
    coordinator._capital = SimpleNamespace(
        stop_new_risk_for_external_activity=(
            lambda **values: external_stops.append(values)
        ),
        stop_new_risk_for_attributed_action_anomaly=lambda **_values: pytest.fail(
            "an unattributed fill is not an attributed action anomaly"
        ),
    )
    coordinator._environment_kind = "DEMO"
    coordinator._authority_class = "DEMO_VALIDATION"
    coordinator._account_ref = "demo-owner"
    coordinator._unattributed_reconciliation_not_before = observed_at - timedelta(
        minutes=60
    )
    normalizer = SimpleNamespace(
        normalize=lambda _event, **_kwargs: NormalizedNautilusEvent(
            action=None,
            facts=(fact,),
        )
    )

    coordinator.handle_nautilus_order_event(
        normalizer,
        object(),
        observed_at=observed_at,
    )

    assert len(external_stops) == expected_stop_count


def test_actionless_reconciliation_of_owned_order_does_not_stop_account() -> None:
    observed_at = datetime(2026, 7, 23, 6, 39, tzinfo=UTC)
    owned_action = SimpleNamespace(
        execution_action_id="owned-protection-action",
        activation_id="activation-a",
        action_kind=ExecutionActionKind.PROTECTION,
        state=ExecutionActionState.OPEN,
    )
    fact = SimpleNamespace(
        venue_fact_id="owned-order-reconciliation",
        activation_ref="activation-a",
        attribution_class="HALPHA_EXECUTION",
        impact_scope=None,
        kind=VenueFactKind.ORDER_STATE,
        payload={"status": "WORKING"},
        source_time=observed_at,
        cutoff=observed_at,
        received_at=observed_at,
        content_digest="d" * 64,
    )
    normalizer = SimpleNamespace(
        normalize=lambda _event, **_kwargs: NormalizedNautilusEvent(
            action=None,
            facts=(fact,),
        )
    )
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._execution = SimpleNamespace(
        apply_venue_fact_with_result=lambda **_kwargs: VenueFactApplicationResult(
            canonical_fact=fact,
            action=owned_action,
            inserted=True,
        )
    )
    coordinator._capital = SimpleNamespace(
        stop_new_risk_for_external_activity=lambda **_values: pytest.fail(
            "a venue-order reconciliation attributed to a persisted action is owned"
        ),
        stop_new_risk_for_attributed_action_anomaly=lambda **_values: pytest.fail(
            "an unchanged owned order is not an action anomaly"
        ),
    )
    coordinator._environment_kind = "DEMO"
    coordinator._authority_class = "DEMO_VALIDATION"
    coordinator._account_ref = "demo-owner"
    coordinator._apply_protection_projection_from_fact = lambda **_values: None

    result = coordinator.handle_nautilus_order_event(
        normalizer,
        object(),
        observed_at=observed_at,
    )

    assert result.facts == (fact,)


def test_demo_submission_path_does_not_add_a_second_gate() -> None:
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._environment_kind = "DEMO"
    coordinator._live_write_submission_guard = lambda _activation_id: pytest.fail(
        "Demo must not invoke the LIVE deployment gate"
    )

    coordinator._require_current_live_write_gate("activation-demo-001")


def test_open_entry_responsibility_blocks_a_second_distinct_bar_action() -> None:
    event = SimpleNamespace(capital_decision={"accepted": False})
    observed: dict[str, object] = {}

    class Planning:
        @staticmethod
        def get_activation(activation_id: str, *, for_update: bool = False):
            observed["locked_activation"] = (activation_id, for_update)
            return object()

        @staticmethod
        def consume_strategy_proposal(**values):
            observed["planning_values"] = values
            return event

    class Execution:
        @staticmethod
        def create_execution_action(**_values):
            pytest.fail("an open entry responsibility must not create another action")

    coordinator = object.__new__(HalphaCoordinator)
    coordinator._environment_kind = "DEMO"
    coordinator._live_write_submission_guard = None
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._planning = Planning()
    coordinator._action_repository = SimpleNamespace(
        has_open_entry_responsibility=lambda _activation_id: True
    )
    coordinator._execution = Execution()
    proposal = SimpleNamespace(activation_id="activation-demo-001")

    result = coordinator.consume_strategy_proposal(
        plan_event_id="plan-event-second-bar",
        execution_action_id="execution-action-second-bar",
        proposal=proposal,
        action_check=object(),
        created_at=datetime(2026, 7, 20, 5, 26, tzinfo=UTC),
    )

    assert result.execution_action is None
    assert observed["locked_activation"] == ("activation-demo-001", True)
    assert observed["planning_values"]["entry_responsibility_open"] is True


@pytest.mark.parametrize("with_market_fact", (True, False))
def test_order_schedule_establishes_all_local_actions_in_one_transaction(
    with_market_fact: bool,
) -> None:
    observed_at = datetime(2026, 7, 23, 1, 0, tzinfo=UTC)
    entered: list[str] = []

    class Transaction:
        def __enter__(self):
            entered.append("BEGIN")

        def __exit__(self, *_args):
            entered.append("END")

    activation, legs, _entry_valid_until = _direct_schedule_fixture()
    checks = tuple(
        SimpleNamespace(
            checked_at=observed_at,
            quantized_quantity=leg.proposed_action.quantity,
        )
        for leg in legs
    )
    recorded: list[dict[str, object]] = []
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=Transaction)
    coordinator._planning = SimpleNamespace(
        get_activation=lambda *_args, **_kwargs: activation
    )
    coordinator._action_repository = SimpleNamespace(
        list_for_activation=lambda _activation_id: ()
    )
    coordinator._capital = SimpleNamespace(
        check_current_action=lambda _check: SimpleNamespace(accepted=True),
        new_risk_discipline_status=lambda **_kwargs: SimpleNamespace(
            new_risk_allowed=True
        ),
    )
    coordinator._new_risk_discipline_policy = object()
    coordinator._environment_id = activation.environment_id
    coordinator._environment_kind = activation.environment_kind.value
    coordinator._venue_ref = BINANCE_USDM_VENUE_REF
    condition_fact = _condition_top_of_book_fact(
        activation,
        condition_cutoff=observed_at - timedelta(seconds=2),
    )
    coordinator._execution = SimpleNamespace(
        apply_venue_fact_with_result=lambda **_kwargs: VenueFactApplicationResult(
            canonical_fact=condition_fact,
            action=None,
            inserted=True,
        )
    )

    def record(**values):
        recorded.append(values)
        return (
            SimpleNamespace(plan_event_id=values["plan_event_id"]),
            SimpleNamespace(execution_action_id=values["execution_action_id"]),
        )

    coordinator._record_proposed_action = record

    results = coordinator.consume_order_schedule_atomic(
        activation_id="activation",
        legs=legs,
        action_checks=checks,
        observed_at=observed_at,
        condition_source_cutoff=observed_at - timedelta(seconds=2),
        condition_evidence={"evaluation": {"result": "TRUE"}},
        condition_facts=((condition_fact,) if with_market_fact else ()),
    )

    assert entered == ["BEGIN", "END"]
    assert [item["execution_action_id"] for item in recorded] == [
        item.execution_action_id for item in legs
    ]
    assert all(
        item["source_cutoff"] == observed_at - timedelta(seconds=2)
        and item["condition_judgement"].source_cutoff
        == observed_at - timedelta(seconds=2)
        and item["condition_judgement"].fact_refs
        == ((condition_fact.venue_fact_id,) if with_market_fact else ())
        and item["action_check"].checked_at == observed_at
        for item in recorded
    )
    assert [item.execution_action.execution_action_id for item in results] == [
        item["execution_action_id"] for item in recorded
    ]


def test_market_schedule_persists_exact_runtime_quantity_adjustment() -> None:
    observed_at = datetime(2026, 7, 23, 1, 0, tzinfo=UTC)
    activation, legs, _entry_valid_until = _direct_schedule_fixture(market=True)
    check = SimpleNamespace(
        checked_at=observed_at,
        quantized_quantity="0.9",
        conservative_price="101",
    )
    recorded: list[dict[str, object]] = []
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._planning = SimpleNamespace(
        get_activation=lambda *_args, **_kwargs: activation
    )
    coordinator._action_repository = SimpleNamespace(
        list_for_activation=lambda _activation_id: ()
    )
    coordinator._capital = SimpleNamespace(
        check_current_action=lambda _check: SimpleNamespace(accepted=True),
        new_risk_discipline_status=lambda **_kwargs: SimpleNamespace(
            new_risk_allowed=True
        ),
    )
    coordinator._new_risk_discipline_policy = object()
    coordinator._environment_id = activation.environment_id
    coordinator._environment_kind = activation.environment_kind.value
    coordinator._venue_ref = BINANCE_USDM_VENUE_REF

    def record(**values):
        recorded.append(values)
        return (
            SimpleNamespace(plan_event_id=values["plan_event_id"]),
            SimpleNamespace(execution_action_id=values["execution_action_id"]),
        )

    coordinator._record_proposed_action = record

    coordinator.consume_order_schedule_atomic(
        activation_id=activation.activation_id,
        legs=legs,
        action_checks=(check,),
        observed_at=observed_at,
    )

    assert len(recorded) == 1
    proposed = recorded[0]["proposed_action"]
    assert proposed.quantity == "0.9"
    assert proposed.execution_context["runtime_market_sizing"] == {
        "planned_quantity": "1",
        "submitted_quantity": "0.9",
        "requested_notional": "100",
        "conservative_price": "101",
        "quantity_step": "0.1",
    }
    assert recorded[0]["input_digest"] != legs[0].input_digest


def test_post_only_retry_is_created_only_from_the_latest_proven_attempt() -> None:
    observed_at = datetime(2026, 7, 23, 0, 30, tzinfo=UTC)
    condition_cutoff = observed_at - timedelta(seconds=2)
    activation, legs, _entry_valid_until = _direct_schedule_fixture(level_count=2)
    base = legs[0]
    retry = materialize_direct_schedule_retry(
        activation,
        base,
        attempt_index=1,
    )
    previous = SimpleNamespace(
        execution_action_id=base.execution_action_id,
        activation_id=activation.activation_id,
        action_kind=ExecutionActionKind.ENTRY,
        action_terms=base.proposed_action.model_dump(mode="python"),
        state=ExecutionActionState.CLOSED,
    )
    rejected_fact = SimpleNamespace(
        venue_fact_id="post-only-rejected",
        kind=VenueFactKind.ORDER_STATE,
        payload={
            "status": "REJECTED",
            "cumulative_filled_quantity": "0",
            "reason": (
                "{'code': -5022, 'msg': 'Due to the order could not be "
                "executed as maker, the Post Only order will be rejected.'}"
            ),
        },
        source_time=condition_cutoff,
        cutoff=condition_cutoff,
        received_at=condition_cutoff,
    )
    condition_fact = _condition_top_of_book_fact(
        activation,
        condition_cutoff=condition_cutoff,
    )
    recorded: list[dict[str, object]] = []
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._planning = SimpleNamespace(
        get_activation=lambda *_args, **_kwargs: activation
    )
    coordinator._action_repository = SimpleNamespace(
        get=lambda _action_id, for_update=False: previous,
        list_for_activation=lambda _activation_id: (previous,),
    )
    coordinator._fact_repository = SimpleNamespace(
        list_for_action=lambda _action_id: (rejected_fact,)
    )
    coordinator._capital = SimpleNamespace(
        check_current_action=lambda _check: SimpleNamespace(accepted=True)
    )
    coordinator._execution = SimpleNamespace(
        apply_venue_fact_with_result=lambda **_kwargs: VenueFactApplicationResult(
            canonical_fact=condition_fact,
            action=None,
            inserted=True,
        )
    )
    coordinator._environment_id = activation.environment_id
    coordinator._environment_kind = activation.environment_kind.value
    coordinator._venue_ref = BINANCE_USDM_VENUE_REF

    def record(**values):
        recorded.append(values)
        return (
            SimpleNamespace(plan_event_id=values["plan_event_id"]),
            SimpleNamespace(execution_action_id=values["execution_action_id"]),
        )

    coordinator._record_proposed_action = record
    result = coordinator.consume_order_schedule_retry(
        activation_id=activation.activation_id,
        retry_leg=retry,
        previous_action_id=previous.execution_action_id,
        action_check=SimpleNamespace(
            checked_at=observed_at,
            quantized_quantity=retry.proposed_action.quantity,
        ),
        observed_at=observed_at,
        condition_source_cutoff=condition_cutoff,
        condition_facts=(condition_fact,),
        condition_evidence={"evaluation": {"result": "TRUE"}},
    )

    assert result.execution_action.execution_action_id == retry.execution_action_id
    assert recorded[0]["rule_id"] == "DIRECT_ENTRY_POLICY_RETRY"
    assert recorded[0]["source_identity"] == retry.source_identity
    assert recorded[0]["client_order_id"] == retry.client_order_id
    assert recorded[0]["condition_judgement"].fact_refs == (
        condition_fact.venue_fact_id,
    )


def test_reprice_is_created_only_after_matching_cancel_and_zero_fill_proof() -> None:
    observed_at = datetime(2026, 7, 23, 0, 30, tzinfo=UTC)
    condition_cutoff = observed_at - timedelta(seconds=2)
    activation, legs, _entry_valid_until = _direct_schedule_fixture(reprice=True)
    base = legs[0]
    replacement = materialize_direct_schedule_reprice(
        activation,
        base,
        attempt_index=1,
        replacement_price="99.8",
        reprice_index=1,
    )
    previous = SimpleNamespace(
        execution_action_id=base.execution_action_id,
        activation_id=activation.activation_id,
        client_order_id=base.client_order_id,
        action_kind=ExecutionActionKind.ENTRY,
        action_terms=base.proposed_action.model_dump(mode="python"),
        state=ExecutionActionState.CLOSED,
    )
    cancel = SimpleNamespace(
        execution_action_id="reprice-cancel",
        activation_id=activation.activation_id,
        client_order_id=None,
        action_kind=ExecutionActionKind.CANCEL,
        action_terms={
            "causation_ref": (
                f"{activation.activation_id}:DIRECT_DYNAMIC:"
                f"DIRECT_ENTRY_REPRICE:{previous.execution_action_id}:v1"
            )
        },
        cancel_target={"client_order_id": previous.client_order_id},
        state=ExecutionActionState.CLOSED,
    )
    cancelled_fact = SimpleNamespace(
        venue_fact_id="entry-cancelled-zero-fill",
        kind=VenueFactKind.ORDER_STATE,
        payload={
            "status": "CANCELLED",
            "cumulative_filled_quantity": "0",
        },
        source_time=condition_cutoff,
        cutoff=condition_cutoff,
        received_at=condition_cutoff,
    )
    condition_fact = _condition_top_of_book_fact(
        activation,
        condition_cutoff=condition_cutoff,
    )
    recorded: list[dict[str, object]] = []
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._planning = SimpleNamespace(
        get_activation=lambda *_args, **_kwargs: activation
    )
    coordinator._action_repository = SimpleNamespace(
        get=lambda action_id, for_update=False: (
            previous if action_id == previous.execution_action_id else cancel
        ),
        list_for_activation=lambda _activation_id: (previous, cancel),
    )
    coordinator._fact_repository = SimpleNamespace(
        list_for_action=lambda action_id: (
            (cancelled_fact,)
            if action_id == previous.execution_action_id
            else ()
        )
    )
    coordinator._capital = SimpleNamespace(
        check_current_action=lambda _check: SimpleNamespace(accepted=True)
    )
    coordinator._execution = SimpleNamespace(
        apply_venue_fact_with_result=lambda **_kwargs: VenueFactApplicationResult(
            canonical_fact=condition_fact,
            action=None,
            inserted=True,
        )
    )
    coordinator._environment_id = activation.environment_id
    coordinator._environment_kind = activation.environment_kind.value
    coordinator._venue_ref = BINANCE_USDM_VENUE_REF

    def record(**values):
        recorded.append(values)
        return (
            SimpleNamespace(plan_event_id=values["plan_event_id"]),
            SimpleNamespace(execution_action_id=values["execution_action_id"]),
        )

    coordinator._record_proposed_action = record
    result = coordinator.consume_order_schedule_reprice(
        activation_id=activation.activation_id,
        replacement_leg=replacement,
        previous_action_id=previous.execution_action_id,
        cancel_action_id=cancel.execution_action_id,
        action_check=SimpleNamespace(
            checked_at=observed_at,
            quantized_quantity=replacement.proposed_action.quantity,
        ),
        observed_at=observed_at,
        condition_source_cutoff=condition_cutoff,
        condition_facts=(condition_fact,),
        condition_evidence={"evaluation": {"result": "TRUE"}},
    )

    assert result.execution_action.execution_action_id == (
        replacement.execution_action_id
    )
    assert recorded[0]["rule_id"] == "DIRECT_ENTRY_REPRICE"
    assert recorded[0]["proposed_action"].price == "99.8"
    assert recorded[0]["client_order_id"] == replacement.client_order_id


def test_reprice_rejects_cancel_without_matching_reprice_reason() -> None:
    observed_at = datetime(2026, 7, 23, 0, 30, tzinfo=UTC)
    activation, legs, _entry_valid_until = _direct_schedule_fixture(reprice=True)
    base = legs[0]
    replacement = materialize_direct_schedule_reprice(
        activation,
        base,
        attempt_index=1,
        replacement_price="99.8",
        reprice_index=1,
    )
    previous = SimpleNamespace(
        execution_action_id=base.execution_action_id,
        activation_id=activation.activation_id,
        client_order_id=base.client_order_id,
        action_kind=ExecutionActionKind.ENTRY,
        action_terms=base.proposed_action.model_dump(mode="python"),
        state=ExecutionActionState.CLOSED,
    )
    cancel = SimpleNamespace(
        execution_action_id="unrelated-cancel",
        activation_id=activation.activation_id,
        client_order_id=None,
        action_kind=ExecutionActionKind.CANCEL,
        action_terms={"causation_ref": "DIRECT_ENTRY_INVALIDATION_PRICE"},
        cancel_target={"client_order_id": previous.client_order_id},
        state=ExecutionActionState.CLOSED,
    )
    cancelled_fact = SimpleNamespace(
        venue_fact_id="entry-cancelled-zero-fill",
        kind=VenueFactKind.ORDER_STATE,
        payload={
            "status": "CANCELLED",
            "cumulative_filled_quantity": "0",
        },
        source_time=observed_at,
        cutoff=observed_at,
        received_at=observed_at,
    )
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._planning = SimpleNamespace(
        get_activation=lambda *_args, **_kwargs: activation
    )
    coordinator._action_repository = SimpleNamespace(
        get=lambda action_id, for_update=False: (
            previous if action_id == previous.execution_action_id else cancel
        ),
        list_for_activation=lambda _activation_id: (previous, cancel),
    )
    coordinator._fact_repository = SimpleNamespace(
        list_for_action=lambda _action_id: (cancelled_fact,)
    )
    coordinator._environment_id = activation.environment_id
    coordinator._environment_kind = activation.environment_kind.value
    coordinator._venue_ref = BINANCE_USDM_VENUE_REF

    with pytest.raises(
        ValueError,
        match="ORDER_SCHEDULE_REPRICE_CANCEL_INVALID",
    ):
        coordinator.consume_order_schedule_reprice(
            activation_id=activation.activation_id,
            replacement_leg=replacement,
            previous_action_id=previous.execution_action_id,
            cancel_action_id=cancel.execution_action_id,
            action_check=SimpleNamespace(
                checked_at=observed_at,
                quantized_quantity=replacement.proposed_action.quantity,
            ),
            observed_at=observed_at,
            condition_source_cutoff=observed_at,
            condition_facts=(),
            condition_evidence={"evaluation": {"result": "TRUE"}},
        )


def test_post_only_retry_rejects_a_generic_post_only_error() -> None:
    observed_at = datetime(2026, 7, 23, 0, 30, tzinfo=UTC)
    activation, legs, _entry_valid_until = _direct_schedule_fixture(level_count=2)
    retry = materialize_direct_schedule_retry(
        activation,
        legs[0],
        attempt_index=1,
    )
    previous = SimpleNamespace(
        execution_action_id=legs[0].execution_action_id,
        activation_id=activation.activation_id,
        action_kind=ExecutionActionKind.ENTRY,
        action_terms=legs[0].proposed_action.model_dump(mode="python"),
        state=ExecutionActionState.CLOSED,
    )
    unrelated_rejection = SimpleNamespace(
        venue_fact_id="post-only-parameter-error",
        kind=VenueFactKind.ORDER_STATE,
        payload={
            "status": "REJECTED",
            "cumulative_filled_quantity": "0",
            "reason": "Post Only parameter is unsupported for this order type",
        },
        source_time=observed_at,
        cutoff=observed_at,
        received_at=observed_at,
    )
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._planning = SimpleNamespace(
        get_activation=lambda *_args, **_kwargs: activation
    )
    coordinator._action_repository = SimpleNamespace(
        get=lambda _action_id, for_update=False: previous,
    )
    coordinator._fact_repository = SimpleNamespace(
        list_for_action=lambda _action_id: (unrelated_rejection,)
    )
    coordinator._environment_id = activation.environment_id
    coordinator._environment_kind = activation.environment_kind.value
    coordinator._venue_ref = BINANCE_USDM_VENUE_REF

    with pytest.raises(
        ValueError,
        match="ORDER_SCHEDULE_RETRY_PREDECESSOR_INVALID",
    ):
        coordinator.consume_order_schedule_retry(
            activation_id=activation.activation_id,
            retry_leg=retry,
            previous_action_id=previous.execution_action_id,
            action_check=SimpleNamespace(
                checked_at=observed_at,
                quantized_quantity=retry.proposed_action.quantity,
            ),
            observed_at=observed_at,
            condition_source_cutoff=observed_at,
            condition_facts=(),
            condition_evidence={"evaluation": {"result": "TRUE"}},
        )


@pytest.mark.parametrize(
    "tamper",
    (
        lambda fact: fact.model_copy(update={"venue_ref": "BINANCE"}),
        lambda fact: fact.model_copy(
            update={"source_class": VenueFactSourceClass.VENUE_QUERY}
        ),
        lambda fact: fact.model_copy(
            update={"source_object_id": f"{fact.source_object_id}:WRONG"}
        ),
        lambda fact: fact.model_copy(update={"source_sequence": "0" * 64}),
        lambda fact: fact.model_copy(
            update={
                "payload": {
                    **fact.payload,
                    "source": "BINANCE_LIVE_PUBLIC",
                }
            }
        ),
        lambda fact: fact.model_copy(
            update={"venue_fact_id": "00000000-0000-0000-0000-000000000000"}
        ),
    ),
    ids=(
        "legacy-venue-ref",
        "query-source-class",
        "wrong-source-object",
        "wrong-source-sequence",
        "cross-environment-source",
        "wrong-stable-fact-id",
    ),
)
def test_order_schedule_rejects_condition_fact_identity_tampering(tamper) -> None:
    observed_at = datetime(2026, 7, 23, 1, 0, tzinfo=UTC)
    condition_cutoff = observed_at - timedelta(seconds=2)
    activation, legs, _entry_valid_until = _direct_schedule_fixture(level_count=2)
    condition_fact = tamper(
        _condition_top_of_book_fact(
            activation,
            condition_cutoff=condition_cutoff,
        )
    )
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._planning = SimpleNamespace(
        get_activation=lambda *_args, **_kwargs: activation
    )
    coordinator._action_repository = SimpleNamespace(
        list_for_activation=lambda _activation_id: ()
    )
    coordinator._capital = SimpleNamespace(
        check_current_action=lambda _check: pytest.fail(
            "invalid condition identity must fail before CAP"
        )
    )
    coordinator._execution = SimpleNamespace(
        apply_venue_fact_with_result=lambda **_kwargs: pytest.fail(
            "invalid condition identity must not be persisted"
        )
    )
    coordinator._environment_id = activation.environment_id
    coordinator._environment_kind = activation.environment_kind.value
    coordinator._venue_ref = BINANCE_USDM_VENUE_REF

    with pytest.raises(
        ValueError,
        match="ORDER_SCHEDULE_CONDITION_FACT_INVALID",
    ):
        coordinator.consume_order_schedule_atomic(
            activation_id=activation.activation_id,
            legs=legs,
            action_checks=tuple(
                SimpleNamespace(checked_at=observed_at) for _leg in legs
            ),
            observed_at=observed_at,
            condition_source_cutoff=condition_cutoff,
            condition_evidence={"evaluation": {"result": "TRUE"}},
            condition_facts=(condition_fact,),
        )


def test_order_schedule_cap_rejection_happens_before_any_local_action() -> None:
    observed_at = datetime(2026, 7, 23, 1, 0, tzinfo=UTC)
    activation, legs, _entry_valid_until = _direct_schedule_fixture(level_count=2)
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._planning = SimpleNamespace(
        get_activation=lambda *_args, **_kwargs: activation
    )
    coordinator._action_repository = SimpleNamespace(
        list_for_activation=lambda _activation_id: ()
    )
    coordinator._capital = SimpleNamespace(
        check_current_action=lambda _check: SimpleNamespace(
            accepted=False,
            reason_code="ACTION_LIMIT_EXCEEDED",
        )
    )
    coordinator._record_proposed_action = lambda **_kwargs: pytest.fail(
        "a rejected schedule must not append a partial event or action"
    )

    with pytest.raises(
        OrderScheduleCapRejected,
        match="ORDER_SCHEDULE_CAP_REJECTED:ACTION_LIMIT_EXCEEDED",
    ) as exc_info:
        coordinator.consume_order_schedule_atomic(
            activation_id="activation",
            legs=legs,
            action_checks=tuple(
                SimpleNamespace(checked_at=observed_at) for _leg in legs
            ),
            observed_at=observed_at,
        )
    assert exc_info.value.rejections == tuple(
        (leg.execution_action_id, "ACTION_LIMIT_EXCEEDED") for leg in legs
    )


def test_partial_schedule_action_set_is_not_auto_repaired() -> None:
    observed_at = datetime(2026, 7, 23, 1, 0, tzinfo=UTC)
    activation, legs, _entry_valid_until = _direct_schedule_fixture(level_count=2)
    existing = SimpleNamespace(
        execution_action_id=legs[0].execution_action_id,
        action_terms={"execution_context": legs[0].proposed_action.execution_context},
    )
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._planning = SimpleNamespace(
        get_activation=lambda *_args, **_kwargs: activation
    )
    coordinator._action_repository = SimpleNamespace(
        list_for_activation=lambda _activation_id: (existing,)
    )
    coordinator._capital = SimpleNamespace(
        check_current_action=lambda _check: pytest.fail(
            "partial local responsibility must fail before CAP"
        )
    )

    with pytest.raises(
        ValueError,
        match="ORDER_SCHEDULE_LOCAL_RESPONSIBILITY_CONFLICT",
    ):
        coordinator.consume_order_schedule_atomic(
            activation_id="activation",
            legs=legs,
            action_checks=(
                SimpleNamespace(checked_at=observed_at),
                SimpleNamespace(checked_at=observed_at),
            ),
            observed_at=observed_at,
        )


@pytest.mark.parametrize(
    "tamper",
    [
        lambda legs: tuple(reversed(legs)),
        lambda legs: (
            legs[0].model_copy(update={"input_digest": "0" * 64}),
            *legs[1:],
        ),
        lambda legs: (
            legs[0].model_copy(
                update={"execution_action_id": "00000000-0000-0000-0000-000000000000"}
            ),
            *legs[1:],
        ),
        lambda legs: (
            legs[0].model_copy(
                update={"plan_event_id": "00000000-0000-0000-0000-000000000000"}
            ),
            *legs[1:],
        ),
        lambda legs: (
            legs[0].model_copy(update={"client_order_id": "0" * 32}),
            *legs[1:],
        ),
        _with_conflicting_schedule_digest,
        lambda legs: (
            legs[0].model_copy(
                update={
                    "proposed_action": legs[0].proposed_action.model_copy(
                        update={"quantity": "999"}
                    )
                }
            ),
            *legs[1:],
        ),
    ],
)
def test_order_schedule_rejects_non_authoritative_materialization(tamper) -> None:
    observed_at = datetime(2026, 7, 23, 1, 0, tzinfo=UTC)
    activation, legs, _entry_valid_until = _direct_schedule_fixture(level_count=2)
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._planning = SimpleNamespace(
        get_activation=lambda *_args, **_kwargs: activation
    )
    coordinator._action_repository = SimpleNamespace(
        list_for_activation=lambda _activation_id: pytest.fail(
            "non-authoritative legs must fail before reading existing actions"
        )
    )
    coordinator._capital = SimpleNamespace(
        check_current_action=lambda _check: pytest.fail(
            "non-authoritative legs must fail before CAP"
        )
    )

    with pytest.raises(
        ValueError,
        match="ORDER_SCHEDULE_MATERIALIZATION_MISMATCH",
    ):
        coordinator.consume_order_schedule_atomic(
            activation_id="activation",
            legs=tamper(legs),
            action_checks=tuple(
                SimpleNamespace(checked_at=observed_at) for _leg in legs
            ),
            observed_at=observed_at,
        )


def test_order_schedule_rejects_existing_action_from_another_schedule_digest() -> None:
    observed_at = datetime(2026, 7, 23, 1, 0, tzinfo=UTC)
    activation, legs, _entry_valid_until = _direct_schedule_fixture(level_count=2)
    conflicting = SimpleNamespace(
        execution_action_id="conflicting-action",
        action_terms={
            "execution_context": {"order_schedule": {"schedule_digest": "f" * 64}}
        },
    )
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._planning = SimpleNamespace(
        get_activation=lambda *_args, **_kwargs: activation
    )
    coordinator._action_repository = SimpleNamespace(
        list_for_activation=lambda _activation_id: (conflicting,)
    )
    coordinator._capital = SimpleNamespace(
        check_current_action=lambda _check: pytest.fail(
            "a conflicting persisted digest must fail before CAP"
        )
    )

    with pytest.raises(ValueError, match="ORDER_SCHEDULE_DIGEST_CONFLICT"):
        coordinator.consume_order_schedule_atomic(
            activation_id="activation",
            legs=legs,
            action_checks=tuple(
                SimpleNamespace(checked_at=observed_at) for _leg in legs
            ),
            observed_at=observed_at,
        )


def test_exact_uuid_absence_closes_only_an_unknown_action() -> None:
    observed_at = datetime(2026, 7, 20, 5, 40, tzinfo=UTC)
    unknown = SimpleNamespace(
        execution_action_id="entry-action-unknown",
        state=ExecutionActionState.UNKNOWN,
    )
    recorded: list[dict[str, object]] = []
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._action_repository = SimpleNamespace(
        get=lambda _action_id, for_update=False: unknown
    )
    coordinator._execution = SimpleNamespace(
        record_definitely_not_submitted=lambda action_id, **values: recorded.append(
            {"action_id": action_id, **values}
        )
    )

    coordinator.record_unknown_action_not_submitted(
        unknown.execution_action_id,
        reason_code="VENUE_QUERY_PROVED_ABSENT",
        observed_at=observed_at,
    )

    assert recorded == [
        {
            "action_id": "entry-action-unknown",
            "reason_code": "VENUE_QUERY_PROVED_ABSENT",
            "observed_at": observed_at,
        }
    ]


@pytest.mark.parametrize(
    ("entry_point", "initial_result", "initial_state"),
    (
        (
            "unknown_absence",
            PrimaryResult.RESULT_UNKNOWN,
            ExecutionActionState.UNKNOWN,
        ),
        (
            "execution_reconciliation",
            PrimaryResult.PARTIAL,
            ExecutionActionState.OPEN,
        ),
        (
            "cancel_reconciliation",
            PrimaryResult.PARTIAL,
            ExecutionActionState.OPEN,
        ),
    ),
)
def test_late_terminal_action_preserves_v1_and_appends_converged_v2(
    monkeypatch: pytest.MonkeyPatch,
    entry_point: str,
    initial_result: PrimaryResult,
    initial_state: ExecutionActionState,
) -> None:
    activation_id = "activation-completed-late-action"
    action_id = "execution-action-late"
    initial_at = datetime(2026, 7, 20, 5, 30, tzinfo=UTC)
    observed_at = initial_at + timedelta(minutes=10)
    trace: list[str] = []
    initial_input_refs = {
        "activation": {"state_version": 4},
        "execution_actions": [
            {
                "execution_action_id": action_id,
                "state_version": 1,
                "state": initial_state.value,
            }
        ],
    }
    initial_fields = {
        "review_id": "10000000-0000-0000-0000-000000000001",
        "review_version": 1,
        "environment_id": "demo-main",
        "activation_id": activation_id,
        "previous_version": None,
        "revision_reason": ReviewRevisionReason.INITIAL_DERIVATION,
        "status": ReviewStatus.DRAFT,
        "primary_result": initial_result,
        "fact_cutoff": initial_at,
        "input_refs": initial_input_refs,
        "input_digest": content_digest(initial_input_refs),
        "account_result": {
            "classification": (
                "UNKNOWN"
                if initial_result is PrimaryResult.RESULT_UNKNOWN
                else "NO_EXTERNAL_CHANGE"
            ),
            "venue_fact_refs": [],
            "missing_refs": [],
            "trade_result": None,
        },
        "open_responsibilities": {
            "execution_action_refs": [action_id],
            "unknown_action_refs": (
                [action_id] if initial_result is PrimaryResult.RESULT_UNKNOWN else []
            ),
            "responsibility_owner": "HALPHA",
            "takeover_scope": None,
        },
        "evaluations": {
            "owner_conclusion": {
                "result": "UNKNOWN",
                "reason": "",
                "evidence_refs": [],
            }
        },
        "evidence_purpose": EvidencePurpose.SYSTEM_MECHANISM_EVIDENCE,
        "created_at": initial_at,
    }
    versions = [
        Review(
            **initial_fields,
            content_digest=content_digest(initial_fields),
        )
    ]

    class Reviews:
        @staticmethod
        def lock_activation(current_activation_id: str) -> None:
            assert current_activation_id == activation_id

        @staticmethod
        def get_latest_for_activation(
            current_activation_id: str,
        ) -> Review:
            assert current_activation_id == activation_id
            return versions[-1]

        @staticmethod
        def insert_review(review: Review) -> None:
            trace.append("OUT_INSERT_V2")
            versions.append(review)

    converged_input_refs = {
        "activation": {"state_version": 4},
        "execution_actions": [
            {
                "execution_action_id": action_id,
                "state_version": 2,
                "state": ExecutionActionState.CLOSED.value,
            }
        ],
    }
    converged_basis = {
        "input_refs": converged_input_refs,
        "primary_result": PrimaryResult.NO_ACTION,
        "account_result": {
            "classification": "NO_EXTERNAL_CHANGE",
            "venue_fact_refs": [],
            "missing_refs": [],
            "trade_result": None,
        },
        "open_responsibilities": {
            "execution_action_refs": [],
            "unknown_action_refs": [],
            "responsibility_owner": "HALPHA",
            "takeover_scope": None,
        },
        "evaluations": initial_fields["evaluations"],
        "evidence_purpose": EvidencePurpose.SYSTEM_MECHANISM_EVIDENCE,
    }
    outcome_service = object.__new__(OutcomeApplicationService)
    outcome_service._environment_id = "demo-main"
    outcome_service._repository = Reviews()
    outcome_service._collect_basis = lambda current_activation_id, *, fact_cutoff: (
        converged_basis
    )
    monkeypatch.setattr(
        "halpha.executor.coordinator.OutcomeApplicationService",
        lambda *_args: outcome_service,
    )

    class Transaction:
        def __enter__(self) -> None:
            trace.append("BEGIN")

        def __exit__(self, exc_type, *_args) -> bool:
            trace.append("ROLLBACK" if exc_type is not None else "COMMIT")
            return False

    initial_action = SimpleNamespace(
        execution_action_id=action_id,
        activation_id=activation_id,
        state=initial_state,
    )
    terminal_action = SimpleNamespace(
        execution_action_id=action_id,
        activation_id=activation_id,
        state=ExecutionActionState.CLOSED,
    )

    def close_action(*_args, **_kwargs):
        trace.append("AUTHORITATIVE_ACTION_MUTATION")
        return terminal_action

    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=Transaction)
    coordinator._environment_id = "demo-main"
    coordinator._planning = SimpleNamespace(
        get_activation=lambda _activation_id: SimpleNamespace(
            lifecycle=PlanLifecycle.COMPLETED
        )
    )
    coordinator._action_repository = SimpleNamespace(
        get=lambda _action_id, **_kwargs: initial_action
    )
    coordinator._execution = SimpleNamespace(
        record_definitely_not_submitted=close_action,
        reconcile_execution_action=close_action,
        reconcile_cancel_from_target_fact=close_action,
    )

    if entry_point == "unknown_absence":
        result = coordinator.record_unknown_action_not_submitted(
            action_id,
            reason_code="VENUE_QUERY_PROVED_ABSENT",
            observed_at=observed_at,
        )
    elif entry_point == "execution_reconciliation":
        result = coordinator.reconcile_execution_action(
            action_id,
            closure_evidence={"position_zero": True},
            venue_fact_refs=("fact-terminal",),
            observed_at=observed_at,
        )
    else:
        result = coordinator.reconcile_cancel_from_target_fact(
            action_id,
            target_fact=SimpleNamespace(activation_ref=activation_id),
            observed_at=observed_at,
        )

    assert result is terminal_action
    assert trace == [
        "BEGIN",
        "AUTHORITATIVE_ACTION_MUTATION",
        "COMMIT",
        "BEGIN",
        "OUT_INSERT_V2",
        "COMMIT",
    ]
    assert len(versions) == 2
    assert versions[0].review_version == 1
    assert versions[0].status is ReviewStatus.DRAFT
    assert versions[0].primary_result is initial_result
    assert versions[1].review_version == 2
    assert versions[1].previous_version == 1
    assert (
        versions[1].revision_reason is ReviewRevisionReason.AUTHORITATIVE_FACTS_CHANGED
    )
    assert versions[1].status is ReviewStatus.DRAFT
    assert versions[1].primary_result is PrimaryResult.NO_ACTION
    assert versions[1].open_responsibilities["execution_action_refs"] == []
    assert versions[1].open_responsibilities["unknown_action_refs"] == []

    coordinator._refresh_completed_reviews_after_commit(
        terminal_actions=(terminal_action,),
        fact_cutoff=observed_at,
        observed_at=observed_at,
    )
    assert len(versions) == 2


def test_non_completed_activation_does_not_create_review_for_terminal_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = datetime(2026, 7, 20, 5, 40, tzinfo=UTC)
    action = SimpleNamespace(
        execution_action_id="execution-action-running",
        activation_id="activation-running",
        state=ExecutionActionState.CLOSED,
    )
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._environment_id = "demo-main"
    coordinator._planning = SimpleNamespace(
        get_activation=lambda _activation_id: SimpleNamespace(
            lifecycle=PlanLifecycle.RUNNING
        )
    )
    coordinator._execution = SimpleNamespace(
        reconcile_execution_action=lambda *_args, **_kwargs: action
    )
    monkeypatch.setattr(
        "halpha.executor.coordinator.OutcomeApplicationService",
        lambda *_args: pytest.fail(
            "a non-completed activation must not create an OUT review"
        ),
    )

    result = coordinator.reconcile_execution_action(
        action.execution_action_id,
        closure_evidence={"position_zero": True},
        venue_fact_refs=("fact-terminal",),
        observed_at=observed_at,
    )

    assert result is action


@pytest.mark.parametrize(
    "entry_point",
    (
        "apply_venue_fact",
        "nautilus_callback",
        "unattributed_account_fact",
    ),
)
def test_late_fact_refreshes_completed_review_only_after_fact_commit(
    monkeypatch: pytest.MonkeyPatch,
    entry_point: str,
) -> None:
    observed_at = datetime(2026, 7, 20, 5, 45, tzinfo=UTC)
    activation_id = "activation-completed-late-fact"
    action = SimpleNamespace(
        execution_action_id="execution-action-closed",
        activation_id=activation_id,
        action_kind=ExecutionActionKind.ENTRY,
        state=ExecutionActionState.CLOSED,
    )
    fact = SimpleNamespace(
        venue_fact_id="late-commission-fact",
        activation_ref=(
            None if entry_point == "unattributed_account_fact" else activation_id
        ),
        impact_scope=(
            {"account_episode_activation_id": activation_id}
            if entry_point == "unattributed_account_fact"
            else None
        ),
        kind=VenueFactKind.COMMISSION,
        payload={},
        cutoff=observed_at,
    )
    trace: list[str] = []
    reviews: list[tuple[str, datetime, datetime]] = []

    class Transaction:
        def __enter__(self) -> None:
            trace.append("BEGIN")

        def __exit__(self, exc_type, *_args) -> bool:
            trace.append("ROLLBACK" if exc_type is not None else "COMMIT")
            return False

    def apply_fact(**_kwargs):
        trace.append("AUTHORITATIVE_FACT_MUTATION")
        return None if entry_point == "unattributed_account_fact" else action

    def apply_fact_with_result(*, fact, **_kwargs):
        return VenueFactApplicationResult(
            canonical_fact=fact,
            action=apply_fact(),
            inserted=True,
        )

    class Outcomes:
        def __init__(self, _connection, _environment_id):
            pass

        @staticmethod
        def update_activation_review(
            current_activation_id: str,
            *,
            fact_cutoff: datetime,
            observed_at: datetime,
        ) -> None:
            trace.append("OUT_REFRESH")
            reviews.append((current_activation_id, fact_cutoff, observed_at))

    monkeypatch.setattr(
        "halpha.executor.coordinator.OutcomeApplicationService",
        Outcomes,
    )
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=Transaction)
    coordinator._environment_id = "demo-main"
    coordinator._planning = SimpleNamespace(
        get_activation=lambda _activation_id: SimpleNamespace(
            lifecycle=PlanLifecycle.COMPLETED
        )
    )
    coordinator._execution = SimpleNamespace(
        apply_venue_fact=apply_fact,
        apply_venue_fact_with_result=apply_fact_with_result,
    )
    coordinator._action_repository = SimpleNamespace(
        find_open_cancel_for_target=lambda _client_order_id: None,
        list_open_cancels_for_target=lambda _client_order_id: (),
    )

    if entry_point in {"apply_venue_fact", "unattributed_account_fact"}:
        result = coordinator.apply_venue_fact(fact, observed_at=observed_at)
        if entry_point == "unattributed_account_fact":
            assert result is None
        else:
            assert result is action
    else:
        normalized = NormalizedNautilusEvent(action=action, facts=(fact,))
        normalizer = SimpleNamespace(normalize=lambda _event, **_kwargs: normalized)
        result = coordinator.handle_nautilus_order_event(
            normalizer,
            object(),
            observed_at=observed_at,
        )
        assert result is normalized

    assert trace == [
        "BEGIN",
        "AUTHORITATIVE_FACT_MUTATION",
        "COMMIT",
        "BEGIN",
        "OUT_REFRESH",
        "COMMIT",
    ]
    assert reviews == [(activation_id, observed_at, observed_at)]


def test_outcome_failure_cannot_undo_terminal_action_commit(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    observed_at = datetime(2026, 7, 20, 5, 50, tzinfo=UTC)
    activation_id = "activation-completed-out-failure"
    trace: list[str] = []
    unknown = SimpleNamespace(
        execution_action_id="execution-action-out-failure",
        activation_id=activation_id,
        state=ExecutionActionState.UNKNOWN,
    )
    terminal = SimpleNamespace(
        execution_action_id=unknown.execution_action_id,
        activation_id=activation_id,
        state=ExecutionActionState.NOT_SUBMITTED,
    )

    class Transaction:
        def __enter__(self) -> None:
            trace.append("BEGIN")

        def __exit__(self, exc_type, *_args) -> bool:
            trace.append("ROLLBACK" if exc_type is not None else "COMMIT")
            return False

    def close_action(*_args, **_kwargs):
        trace.append("AUTHORITATIVE_ACTION_MUTATION")
        return terminal

    class FailingOutcomes:
        def __init__(self, _connection, _environment_id):
            pass

        @staticmethod
        def update_activation_review(*_args, **_kwargs) -> None:
            trace.append("OUT_FAILURE")
            raise RuntimeError("OUT_UNAVAILABLE")

    monkeypatch.setattr(
        "halpha.executor.coordinator.OutcomeApplicationService",
        FailingOutcomes,
    )
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=Transaction)
    coordinator._environment_id = "demo-main"
    coordinator._planning = SimpleNamespace(
        get_activation=lambda _activation_id: SimpleNamespace(
            lifecycle=PlanLifecycle.COMPLETED
        )
    )
    coordinator._action_repository = SimpleNamespace(
        get=lambda _action_id, **_kwargs: unknown
    )
    coordinator._execution = SimpleNamespace(
        record_definitely_not_submitted=close_action
    )

    result = coordinator.record_unknown_action_not_submitted(
        unknown.execution_action_id,
        reason_code="VENUE_QUERY_PROVED_ABSENT",
        observed_at=observed_at,
    )

    assert result is terminal
    assert trace == [
        "BEGIN",
        "AUTHORITATIVE_ACTION_MUTATION",
        "COMMIT",
        "BEGIN",
        "OUT_FAILURE",
        "ROLLBACK",
    ]
    assert "Failed to refresh completed activation review" in caplog.text


def test_terminal_target_fact_reconciles_its_open_cancel_action() -> None:
    observed_at = datetime(2026, 7, 20, 1, 35, tzinfo=UTC)
    target_client_order_id = "a" * 32
    target = SimpleNamespace(
        activation_id="activation-demo-001",
        action_kind=ExecutionActionKind.PROTECTION,
        state=ExecutionActionState.OPEN,
    )
    cancels = (
        SimpleNamespace(execution_action_id="cancel-action-predecessor"),
        SimpleNamespace(execution_action_id="cancel-action-successor"),
    )
    reconciled: list[tuple[str, object, datetime]] = []

    class Execution:
        @staticmethod
        def apply_venue_fact(**_values):
            return target

        @staticmethod
        def reconcile_cancel_from_target_fact(
            action_id: str,
            *,
            target_fact: object,
            observed_at: datetime,
        ) -> None:
            reconciled.append((action_id, target_fact, observed_at))

    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._execution = Execution()
    coordinator._planning = SimpleNamespace(
        get_activation=lambda *_args, **_kwargs: SimpleNamespace(
            protection_state=ProtectionState.UNKNOWN
        ),
        update_protection_projection=lambda **_values: None,
    )
    coordinator._action_repository = SimpleNamespace(
        list_for_activation=lambda _activation_id: (),
        list_open_cancels_for_target=lambda client_id: (
            cancels if client_id == target_client_order_id else ()
        ),
    )
    coordinator._fact_repository = SimpleNamespace(
        list_for_action=lambda _action_id: ()
    )

    facts = (
        SimpleNamespace(
            venue_fact_id="cancelled-fact",
            kind=VenueFactKind.ORDER_STATE,
            payload={
                "status": "CANCELLED",
                "client_order_id": target_client_order_id,
            },
            source_time=observed_at,
            cutoff=observed_at,
            received_at=observed_at,
        ),
        SimpleNamespace(
            venue_fact_id="filled-fact",
            kind=VenueFactKind.FILL,
            payload={
                "leaves_quantity": "0",
                "client_order_id": target_client_order_id,
            },
            source_time=observed_at,
            cutoff=observed_at,
            received_at=observed_at,
        ),
    )

    for fact in facts:
        updated = coordinator.apply_venue_fact(fact, observed_at=observed_at)
        assert updated is target

    assert reconciled == [
        (cancel.execution_action_id, fact, observed_at)
        for fact in facts
        for cancel in cancels
    ]


def test_expired_empty_entry_window_closes_and_creates_review(monkeypatch) -> None:
    observed_at = datetime(2026, 7, 20, 1, tzinfo=UTC)
    deadline = datetime(2026, 7, 20, 0, 59, tzinfo=UTC)
    expired = SimpleNamespace(
        has_entry_fill=False,
        pending_action_digest=None,
    )
    event = SimpleNamespace(
        plan_event_id="plan-event-expired-001",
        source_cutoff=deadline,
    )
    completed = SimpleNamespace(lifecycle=PlanLifecycle.COMPLETED)
    completion: dict[str, object] = {}
    reviews: list[tuple[str, datetime, datetime]] = []

    class Planning:
        @staticmethod
        def expire_entry_deadline(**values):
            assert values["activation_id"] == "activation-demo-expired"
            assert values["observed_at"] == observed_at
            return expired, event

        @staticmethod
        def complete_with_execution_closure(**values):
            completion.update(values)
            return completed

    class Outcomes:
        def __init__(self, _connection, _environment_id):
            pass

        @staticmethod
        def update_activation_review(
            activation_id: str,
            *,
            fact_cutoff: datetime,
            observed_at: datetime,
        ) -> None:
            reviews.append((activation_id, fact_cutoff, observed_at))

    monkeypatch.setattr(
        "halpha.executor.coordinator.OutcomeApplicationService",
        Outcomes,
    )
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._environment_id = "demo-main"
    coordinator._planning = Planning()
    coordinator._action_repository = SimpleNamespace(
        list_for_activation=lambda _activation_id: ()
    )

    result, result_event = coordinator.expire_empty_entry_window(
        activation_id="activation-demo-expired",
        observed_at=observed_at,
    )

    assert result is completed
    assert result_event is event
    assert completion["result_ref"]
    assert len(str(completion["closure_digest"])) == 64
    assert reviews == [("activation-demo-expired", deadline, observed_at)]


def test_remaining_entry_expiry_consumes_before_plan_deadline_without_early_completion() -> (
    None
):
    observed_at = datetime(2026, 7, 20, 1, tzinfo=UTC)
    source_cutoff = observed_at - timedelta(seconds=1)
    consumed = SimpleNamespace(
        entry_opportunity_consumed=True,
        lifecycle=PlanLifecycle.RUNNING,
    )
    event = SimpleNamespace(source_cutoff=source_cutoff)
    calls: list[dict[str, object]] = []

    class Planning:
        @staticmethod
        def expire_remaining_entry_opportunity(**values):
            calls.append(values)
            return consumed, event

    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._planning = Planning()

    result, result_event = coordinator.expire_remaining_entry_opportunity(
        activation_id="activation-demo-remaining-expired",
        source_cutoff=source_cutoff,
        observed_at=observed_at,
    )

    assert result is consumed
    assert result_event is event
    assert calls == [
        {
            "activation_id": "activation-demo-remaining-expired",
            "plan_event_id": calls[0]["plan_event_id"],
            "source_cutoff": source_cutoff,
            "observed_at": observed_at,
        }
    ]
    assert isinstance(calls[0]["plan_event_id"], str)


def test_invalidated_empty_entry_opportunity_closes_and_creates_review(
    monkeypatch,
) -> None:
    observed_at = datetime(2026, 7, 20, 1, tzinfo=UTC)
    source_cutoff = observed_at - timedelta(seconds=1)
    evidence = {
        "checks": [
            {
                "kind": "INVALIDATION_PRICE",
                "configured_price": "95",
                "observed_mark_price": "96",
                "result": "TRUE",
            }
        ]
    }
    invalidated = SimpleNamespace(
        has_entry_fill=False,
        pending_action_digest=None,
    )
    event = SimpleNamespace(
        plan_event_id="plan-event-invalidated-001",
        source_cutoff=source_cutoff,
    )
    completed = SimpleNamespace(lifecycle=PlanLifecycle.COMPLETED)
    completion: dict[str, object] = {}
    reviews: list[tuple[str, datetime, datetime]] = []

    class Planning:
        @staticmethod
        def invalidate_entry_opportunity(**values):
            assert values["activation_id"] == "activation-demo-invalidated"
            assert values["source_cutoff"] == source_cutoff
            assert values["evidence"] == evidence
            return invalidated, event

        @staticmethod
        def complete_with_execution_closure(**values):
            completion.update(values)
            return completed

    class Outcomes:
        def __init__(self, _connection, _environment_id):
            pass

        @staticmethod
        def update_activation_review(
            activation_id: str,
            *,
            fact_cutoff: datetime,
            observed_at: datetime,
        ) -> None:
            reviews.append((activation_id, fact_cutoff, observed_at))

    monkeypatch.setattr(
        "halpha.executor.coordinator.OutcomeApplicationService",
        Outcomes,
    )
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._environment_id = "demo-main"
    coordinator._planning = Planning()
    coordinator._action_repository = SimpleNamespace(
        list_for_activation=lambda _activation_id: ()
    )

    result, result_event = coordinator.invalidate_empty_entry_opportunity(
        activation_id="activation-demo-invalidated",
        source_cutoff=source_cutoff,
        evidence=evidence,
        observed_at=observed_at,
    )

    assert result is completed
    assert result_event is event
    assert completion["result_ref"]
    assert len(str(completion["closure_digest"])) == 64
    assert reviews == [("activation-demo-invalidated", source_cutoff, observed_at)]


def test_live_gate_closing_after_submitting_records_not_submitted_without_venue_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activation_id = "activation-live-001"
    action_id = "execution-action-live-001"
    observed_at = datetime(2026, 7, 18, 13, 0, tzinfo=UTC)
    action_terms = {
        "instrument_ref": "BTCUSDT-PERP",
        "action_profile": "ENTRY_MARKET",
        "quantity": "0.001",
        "direction": "LONG",
    }
    action = SimpleNamespace(
        execution_action_id=action_id,
        environment_id="live-main",
        environment_kind=EnvironmentKind.LIVE,
        authority_class=AuthorityClass.LIVE_REAL_CAPITAL,
        activation_id=activation_id,
        account_ref="live-owner",
        action_kind=ExecutionActionKind.ENTRY,
        action_class=RiskClass.RISK_INCREASING,
        action_terms=action_terms,
    )
    prepared = SimpleNamespace(
        **vars(action),
        state=ExecutionActionState.SUBMITTING,
        state_digest="d" * 64,
    )
    action_check = SimpleNamespace(
        environment_id="live-main",
        environment_kind=EnvironmentKind.LIVE,
        authority_class=AuthorityClass.LIVE_REAL_CAPITAL,
        activation_id=activation_id,
        account_ref="live-owner",
        instrument_ref="BTCUSDT-PERP",
        action_profile="ENTRY_MARKET",
        risk_class=RiskClass.RISK_INCREASING,
        quantized_quantity="0.001",
    )
    gate_checks: list[str] = []

    def current_gate_guard(current_activation_id: str) -> None:
        gate_checks.append(current_activation_id)
        if len(gate_checks) == 2:
            raise RuntimeError("binding-revoked-after-submitting")

    recorded: list[tuple[str, str]] = []
    dispatch_locks: list[tuple[object, str, str]] = []

    def dispatch_lock(
        connection: object,
        *,
        environment_id: str,
        activation_id: str,
    ):
        dispatch_locks.append((connection, environment_id, activation_id))
        return nullcontext()

    monkeypatch.setattr(
        "halpha.executor.coordinator.serialize_activation_dispatch",
        dispatch_lock,
    )

    class ExecutionService:
        @staticmethod
        def prepare_submission(*args, **kwargs):
            assert args == (action_id,)
            assert kwargs["observed_at"] == observed_at
            return prepared

        @staticmethod
        def record_definitely_not_submitted(
            execution_action_id: str,
            *,
            reason_code: str,
            observed_at: datetime,
        ):
            assert observed_at == datetime(2026, 7, 18, 13, 0, tzinfo=UTC)
            recorded.append((execution_action_id, reason_code))
            values = {
                **vars(prepared),
                "state": ExecutionActionState.NOT_SUBMITTED,
                "not_submitted_reason": reason_code,
            }
            return SimpleNamespace(**values)

    coordinator = object.__new__(HalphaCoordinator)
    coordinator._environment_kind = "LIVE"
    coordinator._runtime_real_write_gate = "OPEN"
    coordinator._live_write_activation_ids = frozenset({activation_id})
    coordinator._live_write_submission_guard = current_gate_guard
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._action_repository = SimpleNamespace(
        get=lambda execution_action_id, **_kwargs: action
    )
    coordinator._planning = SimpleNamespace(
        get_activation=lambda current_activation_id, **_kwargs: SimpleNamespace(
            activation_id=current_activation_id,
            environment_id="live-main",
            account_ref="live-owner",
            instrument_ref="BTCUSDT-PERP",
            direction="LONG",
            lifecycle=PlanLifecycle.RUNNING,
            run_state=RunState.ACTIVE,
        )
    )
    coordinator._capital = SimpleNamespace(
        check_current_action=lambda _check: SimpleNamespace(accepted=True),
        new_risk_discipline_status=lambda **_kwargs: SimpleNamespace(
            new_risk_allowed=True
        ),
    )
    coordinator._new_risk_discipline_policy = object()
    coordinator._execution = ExecutionService()
    coordinator._gate = SimpleNamespace(
        authorize_committed_submission=lambda *_args, **_kwargs: pytest.fail(
            "a closed runtime gate must not authorize a venue call"
        ),
        execute_once=lambda *_args, **_kwargs: pytest.fail(
            "a closed runtime gate must not execute a venue call"
        ),
    )

    result = coordinator.process_execution_action(
        action_id,
        action_check=action_check,
        request_payload={"order_type": "MARKET", "quantity": "0.001"},
        observed_at=observed_at,
    )

    assert gate_checks == [activation_id, activation_id]
    assert dispatch_locks == [(coordinator._connection, "live-main", activation_id)]
    assert recorded == [(action_id, "RUNTIME_REAL_WRITE_GATE_CLOSED")]
    assert result.venue_called is False
    assert result.reason_code == "RUNTIME_REAL_WRITE_GATE_CLOSED"
    assert result.execution_action.state is ExecutionActionState.NOT_SUBMITTED


def test_final_dispatch_rejects_risk_increase_when_account_discipline_changes() -> None:
    observed_at = datetime(2026, 7, 18, 13, 6, tzinfo=UTC)
    action = SimpleNamespace(
        execution_action_id="action-discipline-blocked",
        environment_id="demo-main",
        environment_kind=EnvironmentKind.DEMO,
        authority_class=AuthorityClass.DEMO_VALIDATION,
        activation_id="activation-1",
        account_ref="demo-owner",
        action_kind=ExecutionActionKind.ENTRY,
        action_class=RiskClass.RISK_INCREASING,
        state=ExecutionActionState.READY,
        action_terms={
            "instrument_ref": "BTCUSDT-PERP",
            "action_profile": "ENTRY_MARKET",
            "quantity": "0.001",
            "direction": "LONG",
        },
    )
    action_check = SimpleNamespace(
        environment_id="demo-main",
        environment_kind=EnvironmentKind.DEMO,
        authority_class=AuthorityClass.DEMO_VALIDATION,
        activation_id="activation-1",
        account_ref="demo-owner",
        instrument_ref="BTCUSDT-PERP",
        action_profile="ENTRY_MARKET",
        risk_class=RiskClass.RISK_INCREASING,
        quantized_quantity="0.001",
    )
    records: list[tuple[str, str]] = []
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._environment_kind = "DEMO"
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._action_repository = SimpleNamespace(get=lambda *_args, **_kwargs: action)
    coordinator._planning = SimpleNamespace(
        get_activation=lambda *_args, **_kwargs: SimpleNamespace(
            activation_id="activation-1",
            environment_id="demo-main",
            account_ref="demo-owner",
            instrument_ref="BTCUSDT-PERP",
            direction=Direction.LONG,
            lifecycle=PlanLifecycle.RUNNING,
            run_state=RunState.ACTIVE,
        )
    )
    coordinator._capital = SimpleNamespace(
        check_current_action=lambda _check: SimpleNamespace(accepted=True),
        new_risk_discipline_status=lambda **kwargs: (
            assert_discipline_runtime_check(kwargs)
        ),
    )
    coordinator._new_risk_discipline_policy = "same-policy"
    coordinator._execution = SimpleNamespace(
        record_definitely_not_submitted=lambda action_id, **kwargs: (
            records.append((action_id, kwargs["reason_code"]))
            or SimpleNamespace(
                **{**vars(action), "state": ExecutionActionState.NOT_SUBMITTED}
            )
        ),
        prepare_submission=lambda *_args, **_kwargs: pytest.fail(
            "blocked account discipline must not prepare a venue submission"
        ),
    )
    result = coordinator._process_execution_action_serialized(
        action.execution_action_id,
        action_check=action_check,
        request_payload={},
        observed_at=observed_at,
    )

    assert records == [(action.execution_action_id, "NEW_RISK_DAILY_LOSS_STOP_REACHED")]
    assert result.venue_called is False
    assert result.reason_code == "NEW_RISK_DAILY_LOSS_STOP_REACHED"


def assert_discipline_runtime_check(kwargs: dict[str, object]) -> SimpleNamespace:
    assert kwargs["account_ref"] == "demo-owner"
    assert kwargs["policy"] == "same-policy"
    assert kwargs["entry_instrument_ref"] == "BTCUSDT-PERP"
    assert kwargs["entry_direction"] == "LONG"
    assert kwargs["is_frozen_scale_in"] is False
    assert kwargs["lock"] is True
    return SimpleNamespace(
        new_risk_allowed=False,
        blocker_codes=("NEW_RISK_DAILY_LOSS_STOP_REACHED",),
    )


def test_frozen_multi_leg_scale_in_exception_requires_exact_original_action() -> None:
    activation, legs, _entry_valid_until = _direct_schedule_fixture(level_count=2)
    activation = activation.model_copy(update={"has_entry_fill": True})
    original = legs[1]
    action = SimpleNamespace(
        execution_action_id=original.execution_action_id,
        source_identity=original.source_identity,
        client_order_id=original.client_order_id,
        action_kind=ExecutionActionKind.ENTRY,
        action_terms={
            "instrument_ref": original.proposed_action.instrument_ref,
            "direction": original.proposed_action.direction.value,
            "action_profile": original.proposed_action.action_profile,
            "order_type": original.proposed_action.order_type,
            "price": original.proposed_action.price,
            "close_position": original.proposed_action.close_position,
            "reduce_only": original.proposed_action.reduce_only,
            "causation_ref": original.proposed_action.causation_ref,
            "execution_context": original.proposed_action.execution_context,
        },
    )
    retry = materialize_direct_schedule_retry(
        activation,
        original,
        attempt_index=1,
    )
    retry_action = SimpleNamespace(
        execution_action_id=retry.execution_action_id,
        source_identity=retry.source_identity,
        client_order_id=retry.client_order_id,
        action_kind=ExecutionActionKind.ENTRY,
        action_terms={
            "instrument_ref": retry.proposed_action.instrument_ref,
            "direction": retry.proposed_action.direction.value,
            "action_profile": retry.proposed_action.action_profile,
            "order_type": retry.proposed_action.order_type,
            "price": retry.proposed_action.price,
            "close_position": retry.proposed_action.close_position,
            "reduce_only": retry.proposed_action.reduce_only,
            "causation_ref": retry.proposed_action.causation_ref,
            "execution_context": retry.proposed_action.execution_context,
        },
    )

    assert _is_frozen_planned_scale_in_action(action, activation) is True
    assert _is_frozen_planned_scale_in_action(retry_action, activation) is False


def test_single_leg_or_unfilled_schedule_cannot_use_scale_in_exception() -> None:
    single_activation, single_legs, _entry_valid_until = _direct_schedule_fixture(
        market=True,
    )
    single_activation = single_activation.model_copy(update={"has_entry_fill": True})
    original = single_legs[0]
    action = SimpleNamespace(
        execution_action_id=original.execution_action_id,
        source_identity=original.source_identity,
        client_order_id=original.client_order_id,
        action_kind=ExecutionActionKind.ENTRY,
        action_terms={
            "instrument_ref": original.proposed_action.instrument_ref,
            "direction": original.proposed_action.direction.value,
            "action_profile": original.proposed_action.action_profile,
            "order_type": original.proposed_action.order_type,
            "price": original.proposed_action.price,
            "close_position": original.proposed_action.close_position,
            "reduce_only": original.proposed_action.reduce_only,
            "causation_ref": original.proposed_action.causation_ref,
            "execution_context": original.proposed_action.execution_context,
        },
    )

    assert _is_frozen_planned_scale_in_action(action, single_activation) is False


@pytest.mark.parametrize(
    ("risk_class", "action_profile", "maintenance_stop", "definitely_not_submitted"),
    (
        (RiskClass.RISK_REDUCING, "STOP_MARKET", False, False),
        (RiskClass.RISK_NEUTRAL, "CANCEL_ORDER", False, False),
        (RiskClass.RISK_REDUCING, "STOP_MARKET", False, True),
        (RiskClass.RISK_REDUCING, "STOP_MARKET", True, False),
    ),
)
def test_live_closed_gate_and_startup_recovery_do_not_block_bound_risk_control_action(
    risk_class: RiskClass,
    action_profile: str,
    maintenance_stop: bool,
    definitely_not_submitted: bool,
) -> None:
    activation_id = "activation-live-risk-control"
    action_id = f"execution-action-{risk_class.value.lower()}"
    observed_at = datetime(2026, 7, 18, 13, 5, tzinfo=UTC)
    action_terms = {
        "instrument_ref": "BTCUSDT-PERP",
        "action_profile": action_profile,
        "quantity": "0.001",
        "direction": "LONG",
    }
    action = SimpleNamespace(
        execution_action_id=action_id,
        environment_id="live-main",
        environment_kind=EnvironmentKind.LIVE,
        authority_class=AuthorityClass.LIVE_REAL_CAPITAL,
        activation_id=activation_id,
        account_ref="live-owner",
        action_kind=(
            ExecutionActionKind.CANCEL
            if risk_class is RiskClass.RISK_NEUTRAL
            else ExecutionActionKind.PROTECTION
        ),
        action_class=risk_class,
        action_terms=action_terms,
    )
    prepared = SimpleNamespace(
        **vars(action),
        state=ExecutionActionState.SUBMITTING,
        state_digest="e" * 64,
    )
    action_check = SimpleNamespace(
        environment_id="live-main",
        environment_kind=EnvironmentKind.LIVE,
        authority_class=AuthorityClass.LIVE_REAL_CAPITAL,
        activation_id=activation_id,
        account_ref="live-owner",
        instrument_ref="BTCUSDT-PERP",
        action_profile=action_profile,
        risk_class=risk_class,
        quantized_quantity="0.001",
    )
    venue_calls: list[object] = []
    unknown_records: list[tuple[str, str]] = []
    not_submitted_records: list[tuple[str, str]] = []

    class ExecutionService:
        @staticmethod
        def prepare_submission(*args, **kwargs):
            assert args == (action_id,)
            assert kwargs["observed_at"] == observed_at
            return prepared

        @staticmethod
        def record_submission_unknown(
            execution_action_id: str,
            *,
            reason: str,
            next_query_at: datetime,
            observed_at: datetime,
        ):
            assert next_query_at == observed_at + timedelta(seconds=10)
            unknown_records.append((execution_action_id, reason))
            return SimpleNamespace(
                **{**vars(prepared), "state": ExecutionActionState.UNKNOWN}
            )

        @staticmethod
        def record_definitely_not_submitted(
            execution_action_id: str,
            *,
            reason_code: str,
            observed_at: datetime,
        ):
            assert observed_at == datetime(2026, 7, 18, 13, 5, tzinfo=UTC)
            not_submitted_records.append((execution_action_id, reason_code))
            return SimpleNamespace(
                **{**vars(prepared), "state": ExecutionActionState.NOT_SUBMITTED}
            )

    def execute_once(permit: object) -> None:
        venue_calls.append(permit)
        if definitely_not_submitted:
            raise VenueDefinitelyNotSubmitted("NO_VENUE_CALL")
        raise TimeoutError("venue result unknown")

    coordinator = object.__new__(HalphaCoordinator)
    coordinator._environment_kind = "LIVE"
    coordinator._runtime_real_write_gate = "CLOSED"
    coordinator._live_write_activation_ids = frozenset({activation_id})
    coordinator._live_write_submission_guard = lambda _activation_id: pytest.fail(
        "closing the new-risk gate must not disable protection, cancellation, or exit"
    )
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._action_repository = SimpleNamespace(
        get=lambda execution_action_id, **_kwargs: action
    )
    coordinator._planning = SimpleNamespace(
        get_activation=lambda current_activation_id, **_kwargs: SimpleNamespace(
            activation_id=current_activation_id,
            environment_id="live-main",
            account_ref="live-owner",
            instrument_ref="BTCUSDT-PERP",
            direction="LONG",
            lifecycle=PlanLifecycle.RUNNING,
            run_state=RunState.PAUSED,
        )
    )
    coordinator._capital = SimpleNamespace(
        check_current_action=lambda _check: SimpleNamespace(accepted=True)
    )
    coordinator._execution = ExecutionService()
    coordinator._gate = SimpleNamespace(
        authorize_committed_submission=lambda execution_action_id, **_kwargs: (
            execution_action_id
        ),
        execute_once=execute_once,
    )
    coordinator.arm_startup_recovery_barrier()
    if maintenance_stop:
        coordinator.disable_venue_mutations()

    result = coordinator._process_execution_action_serialized(
        action_id,
        action_check=action_check,
        request_payload={"action_profile": action_profile, "quantity": "0.001"},
        observed_at=observed_at,
    )

    if maintenance_stop:
        assert venue_calls == []
        assert unknown_records == []
        assert not_submitted_records == [(action_id, "MAINTENANCE_STOP")]
        assert result.venue_called is False
        assert result.reason_code == "MAINTENANCE_STOP"
        assert result.execution_action.state is ExecutionActionState.NOT_SUBMITTED
    elif definitely_not_submitted:
        assert venue_calls == [action_id]
        assert unknown_records == []
        assert not_submitted_records == [
            (action_id, "VENUE_CLIENT_DEFINITELY_NOT_SUBMITTED")
        ]
        assert result.venue_called is False
        assert result.reason_code == "NOT_SUBMITTED"
        assert result.execution_action.state is ExecutionActionState.NOT_SUBMITTED
    else:
        assert venue_calls == [action_id]
        assert unknown_records == [(action_id, "VENUE_CALL_UNCERTAIN:TimeoutError")]
        assert not_submitted_records == []
        assert result.venue_called is True
        assert result.reason_code == "SUBMISSION_RESULT_UNKNOWN"
        assert result.execution_action.state is ExecutionActionState.UNKNOWN


def test_final_dispatch_rejects_action_direction_opposite_to_activation() -> None:
    action = SimpleNamespace(
        execution_action_id="action-opposite-direction",
        environment_id="demo-main",
        environment_kind=EnvironmentKind.DEMO,
        authority_class=AuthorityClass.DEMO_VALIDATION,
        activation_id="activation-1",
        account_ref="demo-owner",
        action_kind=ExecutionActionKind.ENTRY,
        action_class=RiskClass.RISK_INCREASING,
        state=ExecutionActionState.READY,
        action_terms={
            "instrument_ref": "BTCUSDT-PERP",
            "action_profile": "ENTRY_MARKET",
            "quantity": "0.001",
            "direction": "SHORT",
        },
    )
    action_check = SimpleNamespace(
        environment_id="demo-main",
        environment_kind=EnvironmentKind.DEMO,
        authority_class=AuthorityClass.DEMO_VALIDATION,
        activation_id="activation-1",
        account_ref="demo-owner",
        instrument_ref="BTCUSDT-PERP",
        action_profile="ENTRY_MARKET",
        risk_class=RiskClass.RISK_INCREASING,
        quantized_quantity="0.001",
    )
    activation = SimpleNamespace(
        activation_id="activation-1",
        environment_id="demo-main",
        account_ref="demo-owner",
        instrument_ref="BTCUSDT-PERP",
        direction=Direction.LONG,
        lifecycle=PlanLifecycle.RUNNING,
        run_state=RunState.ACTIVE,
    )
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._environment_kind = "DEMO"
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._action_repository = SimpleNamespace(
        get=lambda *_args, **_kwargs: action
    )
    coordinator._planning = SimpleNamespace(
        get_activation=lambda *_args, **_kwargs: activation
    )
    coordinator._capital = SimpleNamespace(
        check_current_action=lambda *_args, **_kwargs: pytest.fail(
            "direction mismatch must fail before capital or venue processing"
        )
    )
    coordinator._execution = SimpleNamespace(
        prepare_submission=lambda *_args, **_kwargs: pytest.fail(
            "direction mismatch must not prepare a venue submission"
        )
    )

    with pytest.raises(ValueError, match="ACTION_SCOPE_MISMATCH"):
        coordinator._process_execution_action_serialized(
            action.execution_action_id,
            action_check=action_check,
            request_payload={"order_type": "MARKET", "quantity": "0.001"},
            observed_at=datetime(2026, 7, 18, 13, 6, tzinfo=UTC),
        )
    assert action.state is ExecutionActionState.READY


def test_final_action_check_rejects_side_opposite_to_position_alignment() -> None:
    action = SimpleNamespace(
        environment_id="demo-main",
        environment_kind=EnvironmentKind.DEMO,
        authority_class=AuthorityClass.DEMO_VALIDATION,
        activation_id="activation-1",
        account_ref="demo-owner",
        action_class=RiskClass.RISK_REDUCING,
        action_terms={
            "instrument_ref": "BTCUSDT-PERP",
            "action_profile": "REDUCE_OR_CLOSE_MARKET",
            "quantity": "0.001",
            "direction": "LONG",
            "position_side": "SHORT",
        },
    )
    action_check = SimpleNamespace(
        environment_id="demo-main",
        environment_kind=EnvironmentKind.DEMO,
        authority_class=AuthorityClass.DEMO_VALIDATION,
        activation_id="activation-1",
        account_ref="demo-owner",
        instrument_ref="BTCUSDT-PERP",
        action_profile="REDUCE_OR_CLOSE_MARKET",
        risk_class=RiskClass.RISK_REDUCING,
        quantized_quantity="0.001",
    )
    activation = SimpleNamespace(
        activation_id="activation-1",
        environment_id="demo-main",
        account_ref="demo-owner",
        instrument_ref="BTCUSDT-PERP",
        direction=Direction.LONG,
        position_alignment=SimpleNamespace(position_side="LONG"),
    )

    with pytest.raises(ValueError, match="ACTION_SCOPE_MISMATCH"):
        HalphaCoordinator._validate_action_check(
            action,
            action_check,
            activation,
        )


def test_final_dispatch_keeps_ready_entry_blocked_during_startup_recovery() -> None:
    action = SimpleNamespace(
        execution_action_id="action-startup-pending",
        activation_id="activation-pending",
        action_class=RiskClass.RISK_INCREASING,
        state=ExecutionActionState.READY,
    )
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._action_repository = SimpleNamespace(
        get=lambda *_args, **_kwargs: action
    )
    coordinator.arm_startup_recovery_barrier()
    coordinator._execution = SimpleNamespace(
        prepare_submission=lambda *_args, **_kwargs: pytest.fail(
            "startup recovery must not prepare a venue submission"
        )
    )

    with pytest.raises(RuntimeError, match="STARTUP_RECOVERY_PENDING"):
        coordinator._process_execution_action_serialized(
            action.execution_action_id,
            action_check=SimpleNamespace(),
            request_payload={},
            observed_at=datetime(2026, 7, 18, 13, 7, tzinfo=UTC),
        )
    assert action.state is ExecutionActionState.READY


def test_final_dispatch_uses_current_attribution_check_under_account_new_risk_stop() -> (
    None
):
    action = SimpleNamespace(
        execution_action_id="action-external-conflict",
        environment_id="demo-main",
        environment_kind=EnvironmentKind.DEMO,
        authority_class=AuthorityClass.DEMO_VALIDATION,
        activation_id="activation-1",
        account_ref="demo-owner",
        action_kind=ExecutionActionKind.EXIT,
        action_class=RiskClass.RISK_REDUCING,
        state=ExecutionActionState.READY,
        action_terms={
            "instrument_ref": "BTCUSDT-PERP",
            "action_profile": "REDUCE_OR_CLOSE_MARKET",
            "quantity": "0.001",
            "direction": "LONG",
            "position_side": "BOTH",
        },
    )
    activation = SimpleNamespace(
        activation_id="activation-1",
        environment_id="demo-main",
        account_ref="demo-owner",
        instrument_ref="BTCUSDT-PERP",
        direction=Direction.LONG,
        lifecycle=PlanLifecycle.EXITING,
        run_state=RunState.ACTIVE,
    )
    action_check = SimpleNamespace(
        environment_id="demo-main",
        environment_kind=EnvironmentKind.DEMO,
        authority_class=AuthorityClass.DEMO_VALIDATION,
        activation_id="activation-1",
        account_ref="demo-owner",
        instrument_ref="BTCUSDT-PERP",
        action_profile="REDUCE_OR_CLOSE_MARKET",
        risk_class=RiskClass.RISK_REDUCING,
        quantized_quantity="0.001",
    )
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._environment_kind = "DEMO"
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._action_repository = SimpleNamespace(
        get=lambda *_args, **_kwargs: action
    )
    coordinator._planning = SimpleNamespace(
        get_activation=lambda *_args, **_kwargs: activation
    )
    coordinator._capital = SimpleNamespace(
        external_activity_conflict=lambda _activation_id: True,
        check_current_action=lambda *_args, **_kwargs: SimpleNamespace(
            accepted=False,
            reason_code="ATTRIBUTION_UNKNOWN",
        ),
    )
    coordinator._execution = SimpleNamespace(
        prepare_submission=lambda *_args, **_kwargs: pytest.fail(
            "external conflict must not prepare a venue submission"
        )
    )

    result = coordinator._process_execution_action_serialized(
        action.execution_action_id,
        action_check=action_check,
        request_payload={},
        observed_at=datetime(2026, 7, 18, 13, 8, tzinfo=UTC),
    )
    assert result.venue_called is False
    assert result.reason_code == "ATTRIBUTION_UNKNOWN"
    assert action.state is ExecutionActionState.READY


def test_close_activation_does_not_reapply_account_new_risk_stop() -> None:
    trace: list[str] = []
    coordinator = object.__new__(HalphaCoordinator)
    coordinator._connection = SimpleNamespace(transaction=lambda: nullcontext())
    coordinator._capital = SimpleNamespace(
        external_activity_conflict=lambda _activation_id: pytest.fail(
            "a NEW_RISK stop must not override the caller's exact closure proof"
        )
    )

    def evaluate_closure(_activation_id: str, **kwargs: object) -> str:
        assert trace == ["ACTIVATION_LOCK"]
        assert kwargs["external_activity_conflict"] is False
        raise ValueError("CLOSURE_PROOF_REACHED")

    coordinator._execution = SimpleNamespace(
        evaluate_activation_closure=evaluate_closure
    )
    coordinator._planning = SimpleNamespace(
        get_activation=lambda *_args, **_kwargs: (
            trace.append("ACTIVATION_LOCK")
            or SimpleNamespace(protection_state=ProtectionState.WORKING)
        )
    )

    with pytest.raises(ValueError, match="CLOSURE_PROOF_REACHED"):
        coordinator.close_activation(
            activation_id="activation-1",
            cutoff=datetime(2026, 7, 18, 13, 9, tzinfo=UTC),
            position_zero=True,
            open_order_refs=(),
            external_activity_conflict=False,
            user_takeover=False,
            handover_command_ref=None,
            fact_refs=("position-fact-1",),
            observed_at=datetime(2026, 7, 18, 13, 9, 1, tzinfo=UTC),
        )


def test_live_coordinator_accepts_closed_gate_only_for_bound_risk_control() -> None:
    def guard(_activation_id: str) -> None:
        return None

    coordinator = HalphaCoordinator(
        object(),
        object(),
        environment_id="live-main",
        environment_kind="LIVE",
        authority_class="LIVE_REAL_CAPITAL",
        execution_profile_ref="BINANCE_LIVE_WRITE",
        account_ref="live-owner",
        runtime_real_write_gate="CLOSED",
        live_write_activation_ids=("activation-live-risk-control",),
        live_write_submission_guard=guard,
        live_write_risk_control_only=True,
    )

    assert coordinator._runtime_real_write_gate == "CLOSED"
    assert coordinator._live_write_risk_control_only is True
    with pytest.raises(ValueError, match="EXECUTION_PROFILE_MISMATCH"):
        HalphaCoordinator(
            object(),
            object(),
            environment_id="live-main",
            environment_kind="LIVE",
            authority_class="LIVE_REAL_CAPITAL",
            execution_profile_ref="BINANCE_LIVE_WRITE",
            account_ref="live-owner",
            runtime_real_write_gate="CLOSED",
            live_write_activation_ids=("activation-live-risk-control",),
            live_write_submission_guard=guard,
        )
