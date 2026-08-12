from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import halpha.executor.responsibilities as responsibilities_module

from halpha.capital.models import AuthorityClass, EnvironmentKind, StopCategory
from halpha.planning.models import (
    PlanActivation,
    PlanLifecycle,
    PositionAlignmentSpec,
    ProtectionState,
)
from halpha.planning.order_policies import (
    ProfitLockMode,
    ProfitLockRule,
    ProtectionStep,
    RepriceEntryRule,
    SteppedProtectionRule,
)
from halpha.planning.transitions import (
    enter_exit,
    record_direct_fill,
    record_first_fill,
)
from halpha.executor.responsibilities import (
    PROTECTION_UNKNOWN_EXIT_DELAY,
    UNKNOWN_RISK_CONTROL_SUCCESSOR_DELAY,
    ProductResponsibilityBoundary,
    ProductRiskReductionFacts,
    _fills_have_commissions,
    _has_pending_retryable_entry,
    _position_attribution_proven,
    _position_fact_matches_activation,
)
from halpha.venue_integration.models import (
    ExecutionActionKind,
    ExecutionActionState,
    ExitResponsibilityRole,
    VenueFactKind,
    VenueFactSourceClass,
)
from halpha.venue_integration.facts import (
    collapse_synthetic_reconciliation_fills,
    terminal_fills_complete,
)
from halpha.venue_integration.nautilus_events import NormalizedNautilusEvent


NOW = datetime(2026, 7, 19, 8, tzinfo=UTC)


def _activation(*, first_fill: bool = False) -> PlanActivation:
    activation = PlanActivation(
        activation_id="activation-1",
        environment_id="demo-1",
        environment_kind=EnvironmentKind.DEMO,
        authority_class=AuthorityClass.DEMO_VALIDATION,
        plan_version_ref="plan-version-1",
        account_ref="account-1",
        instrument_ref="BTCUSDT-PERP",
        direction="LONG",
        decision_basis_ref="ONE_SHOT_DONCHIAN_ATR_BREAKOUT@1.0.1",
        framework_strategy_id="HALPHA-TEST",
        target_exposure="0.01",
        rule_state={
            "deadlines": {},
            "condition_judgements": {},
            "last_bar_cursors": {},
        },
        created_at=NOW,
        updated_at=NOW,
    )
    if not first_fill:
        return activation
    return record_first_fill(
        activation,
        entry_action_ref="entry-action",
        fill_fact_ref="fill-fact",
        fill_price="100",
        fill_time=NOW,
        entry_risk_context={
            "trigger_atr": "2",
            "initial_stop_atr_multiple": "1.5",
            "take_profit_1_r": "1.5",
            "take_profit_1_fraction": "0.5",
            "take_profit_2_r": "3",
            "max_hold_bars_15m": 96,
            "indicator_source_digest": "a" * 64,
            "indicator_source_cutoff_ns": 1_773_910_800_000_000_000,
            "quantity_step": "0.001",
            "price_tick_size": "0.1",
            "entry_extension_boundary": "110",
            "sizing_taker_fee_rate": "0.0006",
            "sizing_effective_leverage": "5",
            "instrument_rules_digest": "b" * 64,
        },
        observed_at=NOW,
    )


def _direct_activation_with_time_exit() -> PlanActivation:
    activation = _activation().model_copy(
        update={"decision_basis_ref": "DIRECT_EXECUTION@1"}
    )
    return record_direct_fill(
        activation,
        entry_action_ref="entry-action",
        fill_fact_ref="fill-fact",
        fill_price="100",
        fill_quantity="0.01",
        fill_time=NOW - timedelta(seconds=61),
        protection_policy={
            "initial_stop": {
                "distance_bps": "100",
                "trigger_source": "MARK_PRICE",
                "coverage": "EACH_CONFIRMED_FILL",
            },
            "take_profit_ladder": None,
            "time_exit_seconds": 60,
        },
        price_tick_size="0.1",
        quantity_step="0.001",
        observed_at=NOW - timedelta(seconds=61),
    )


def _facts(
    *,
    current_abs_position: str = "0.01",
    current_reference_price: str | None = None,
    position_fact: object | None = None,
    open_order_client_ids: tuple[str, ...] = (),
    open_algo_client_ids: tuple[str, ...] = (),
    checked_at: datetime = NOW,
    attribution_cutoff: datetime | None = None,
) -> ProductRiskReductionFacts:
    return ProductRiskReductionFacts(
        checked_at=checked_at,
        conservative_price="100",
        available_margin="1000",
        actual_margin_mode="ISOLATED",
        actual_leverage="5",
        activation_current_notional="1",
        account_current_notional="1",
        activation_current_margin="0.2",
        current_abs_position=current_abs_position,
        current_reference_price=current_reference_price,
        position_fact=position_fact,
        open_order_client_ids=open_order_client_ids,
        open_algo_client_ids=open_algo_client_ids,
        attribution_cutoff=attribution_cutoff,
    )


def test_position_alignment_risk_fact_must_match_the_fixed_hedge_side() -> None:
    payload = _activation().model_dump(mode="python")
    payload.update(
        {
            "decision_basis_ref": "DIRECT_EXECUTION@1",
            "position_alignment": PositionAlignmentSpec(
                operation="REDUCE",
                snapshot_ref="account-snapshot-hedge-long",
                fact_cutoff=NOW,
                account_ref="account-1",
                venue_ref="BINANCE_USDM",
                instrument_ref="BTCUSDT-PERP",
                direction="LONG",
                position_side="LONG",
                baseline_quantity="0.3",
                requested_reduction_quantity="0.1",
                target_quantity_after="0.2",
                baseline_entry_price="90",
                baseline_mark_price="100",
            ),
            "lifecycle": PlanLifecycle.EXITING,
            "entry_opportunity_consumed": True,
        }
    )
    activation = PlanActivation.model_validate(payload)
    position_fact = _venue_fact(
        "position-fact-long",
        VenueFactKind.POSITION_STATE,
        position_quantity="0.2",
    )
    position_fact.payload["position_side"] = "LONG"
    facts = _facts(current_abs_position="0.2", position_fact=position_fact)

    assert _position_fact_matches_activation(activation, facts) is True

    position_fact.payload["position_side"] = "SHORT"
    assert _position_fact_matches_activation(activation, facts) is False


def _action(
    action_id: str,
    kind: ExecutionActionKind,
    *,
    state: ExecutionActionState,
    terms: dict[str, object],
    client_order_id: str | None = None,
    cancel_target: dict[str, object] | None = None,
    call_started_at: datetime | None = None,
    not_submitted_reason: str | None = None,
    created_at: datetime = NOW,
) -> SimpleNamespace:
    normalized_terms = dict(terms)
    if kind is ExecutionActionKind.EXIT:
        normalized_terms.setdefault(
            "exit_responsibility_role",
            ExitResponsibilityRole.PRIMARY_EXIT.value,
        )
    return SimpleNamespace(
        execution_action_id=action_id,
        activation_id="activation-1",
        source_identity=action_id,
        action_kind=kind,
        state=state,
        state_version=1,
        action_terms=normalized_terms,
        client_order_id=client_order_id,
        cancel_target=cancel_target,
        call_started_at=call_started_at,
        request_digest=("a" * 64 if call_started_at is not None else None),
        not_submitted_reason=not_submitted_reason,
        created_at=created_at,
    )


def _venue_fact(
    fact_id: str,
    kind: VenueFactKind,
    *,
    status: str | None = None,
    trade_id: str | None = None,
    leaves_quantity: str = "0",
    last_quantity: str | None = None,
    cumulative_filled_quantity: str | None = None,
    position_quantity: str | None = None,
    action_ref: str | None = None,
    activation_ref: str | None = None,
    source_sequence: str = "1",
    source_class: VenueFactSourceClass | None = None,
    received_at: datetime = NOW,
    cutoff: datetime = NOW,
    environment_id: str = "demo-1",
    venue_ref: str = "BINANCE_USDM",
    account_ref: str = "account-1",
    instrument_ref: str = "BTCUSDT-PERP",
) -> SimpleNamespace:
    payload: dict[str, object] = {}
    if status is not None:
        payload["status"] = status
    if trade_id is not None:
        payload["trade_id"] = trade_id
    if kind is VenueFactKind.FILL:
        payload["leaves_quantity"] = leaves_quantity
        if last_quantity is not None:
            payload["last_quantity"] = last_quantity
    if cumulative_filled_quantity is not None:
        payload["cumulative_filled_quantity"] = cumulative_filled_quantity
    if position_quantity is not None:
        payload["position_quantity"] = position_quantity
    return SimpleNamespace(
        venue_fact_id=fact_id,
        environment_id=environment_id,
        venue_ref=venue_ref,
        account_ref=account_ref,
        instrument_ref=instrument_ref,
        kind=kind,
        payload=payload,
        source_time=NOW,
        cutoff=cutoff,
        received_at=received_at,
        action_ref=action_ref,
        activation_ref=activation_ref,
        source_class=(
            VenueFactSourceClass.VENUE_QUERY
            if source_class is None and kind is VenueFactKind.POSITION_STATE
            else source_class or VenueFactSourceClass.VENUE_STREAM
        ),
        source_object_id=fact_id,
        source_sequence=source_sequence,
        content_digest=f"digest-{fact_id}",
    )


@pytest.mark.parametrize(
    ("venue_policy", "reason"),
    (
        (
            {"post_only": True},
            (
                "{'code': -5022, 'msg': 'Due to the order could not be "
                "executed as maker, the Post Only order will be rejected.'}"
            ),
        ),
        (
            {"post_only": False, "price_match": "OPPONENT_10"},
            "{'code': -5037, 'msg': 'Invalid price match'}",
        ),
    ),
)
@pytest.mark.parametrize(
    ("observed_at", "expected"),
    (
        (NOW, True),
        (NOW + timedelta(minutes=5), False),
    ),
)
def test_retryable_entry_policy_rejection_holds_closure_only_inside_entry_window(
    venue_policy: dict[str, object],
    reason: str,
    observed_at: datetime,
    expected: bool,
) -> None:
    activation = _activation().model_copy(
        update={
            "decision_basis_ref": "DIRECT_EXECUTION@1",
            "rule_state": {
                "deadlines": {
                    "entry_valid_until": (NOW + timedelta(minutes=5)).isoformat(),
                }
            },
        }
    )
    action = _action(
        "entry-post-only-attempt-0",
        ExecutionActionKind.ENTRY,
        state=ExecutionActionState.CLOSED,
        terms={
            "quantity": "0.01",
            "execution_context": {
                "venue_policy": venue_policy,
                "order_schedule": {
                    "schedule_digest": "a" * 64,
                    "leg_index": 0,
                    "attempt_index": 0,
                },
            },
        },
    )
    rejection = SimpleNamespace(
        venue_fact_id="post-only-rejected",
        kind=VenueFactKind.ORDER_STATE,
        payload={
            "status": "REJECTED",
            "cumulative_filled_quantity": "0",
            "reason": reason,
        },
        source_time=NOW,
        cutoff=NOW,
        received_at=NOW,
    )
    coordinator = SimpleNamespace(
        list_venue_facts_for_action=lambda _action_id: (rejection,)
    )

    assert (
        _has_pending_retryable_entry(
            activation,
            (action,),
            coordinator,
            observed_at=observed_at,
        )
        is expected
    )


@pytest.mark.parametrize(
    ("reprice_index", "observed_at", "expected"),
    (
        (0, NOW, True),
        (2, NOW, True),
        (3, NOW, False),
        (0, NOW + timedelta(minutes=5), False),
    ),
)
def test_closed_reprice_cancel_holds_closure_until_replacement_or_limit(
    reprice_index: int,
    observed_at: datetime,
    expected: bool,
) -> None:
    activation = _activation().model_copy(
        update={
            "decision_basis_ref": "DIRECT_EXECUTION@1",
            "order_schedule_snapshot": SimpleNamespace(
                schedule_spec=SimpleNamespace(
                    dynamic_rules=(RepriceEntryRule(max_adjustments=3),),
                ),
            ),
            "rule_state": {
                "deadlines": {
                    "entry_valid_until": (NOW + timedelta(minutes=5)).isoformat(),
                }
            },
        }
    )
    entry = _action(
        f"entry-reprice-{reprice_index}",
        ExecutionActionKind.ENTRY,
        state=ExecutionActionState.CLOSED,
        client_order_id=f"entry-client-{reprice_index}",
        terms={
            "quantity": "0.01",
            "execution_context": {
                "venue_policy": {"post_only": True},
                "order_schedule": {
                    "schedule_digest": "a" * 64,
                    "leg_index": 0,
                    "attempt_index": reprice_index,
                    "reprice_index": reprice_index,
                    "retry_reason": (
                        "ENTRY_REPRICE" if reprice_index > 0 else None
                    ),
                },
            },
        },
    )
    cancel = _action(
        f"cancel-reprice-{reprice_index}",
        ExecutionActionKind.CANCEL,
        state=ExecutionActionState.CLOSED,
        cancel_target={"client_order_id": entry.client_order_id},
        terms={
            "causation_ref": (
                f"activation-1:DIRECT_DYNAMIC:DIRECT_ENTRY_REPRICE:"
                f"{entry.execution_action_id}:v1"
            ),
        },
    )
    cancelled = _venue_fact(
        f"entry-reprice-{reprice_index}-cancelled",
        VenueFactKind.ORDER_STATE,
        status="CANCELLED",
        cumulative_filled_quantity="0",
        action_ref=entry.execution_action_id,
    )
    coordinator = SimpleNamespace(
        list_venue_facts_for_action=lambda action_id: (
            (cancelled,) if action_id == entry.execution_action_id else ()
        )
    )

    assert (
        _has_pending_retryable_entry(
            activation,
            (entry, cancel),
            coordinator,
            observed_at=observed_at,
        )
        is expected
    )


class _Coordinator:
    def __init__(self, activation: PlanActivation) -> None:
        self.activation = activation
        self.actions: dict[str, SimpleNamespace] = {}
        self.facts: dict[str, tuple[SimpleNamespace, ...]] = {}
        self.protection_checks = []
        self.protection_replacement_requests: list[dict[str, object]] = []
        self.take_profit_replacement_requests: list[dict[str, object]] = []
        self.take_profit_checks = []
        self.exit_checks = []
        self.exit_requests: list[dict[str, object]] = []
        self.risk_reduction_checks = []
        self.risk_reduction_requests: list[dict[str, object]] = []
        self.cancel_checks = []
        self.cancel_requests: list[dict[str, object]] = []
        self.applied_facts = []
        self.submissions: list[tuple[str, dict[str, object]]] = []
        self.reconciliations: list[dict[str, object]] = []
        self.closures: list[dict[str, object]] = []
        self.unknown_queries: list[tuple[str, datetime]] = []
        self.rejections: list[tuple[str, str]] = []
        self.called_queries: list[str] = []
        self.takeover_calls: list[tuple[str, datetime]] = []
        self.has_external_activity_conflict = False
        self.funding_records: list[dict[str, object]] = []

    def get_activation_snapshot(self, _activation_id: str) -> PlanActivation:
        return self.activation

    def get_execution_action(self, action_id: str) -> SimpleNamespace:
        return self.actions[action_id]

    def list_execution_actions(
        self, _activation_id: str
    ) -> tuple[SimpleNamespace, ...]:
        return tuple(self.actions.values())

    def list_venue_facts_for_action(
        self, action_id: str
    ) -> tuple[SimpleNamespace, ...]:
        return self.facts.get(action_id, ())

    def query_unknown_action_if_due(
        self,
        action_id: str,
        *,
        observed_at: datetime,
    ) -> bool:
        self.unknown_queries.append((action_id, observed_at))
        return True

    def query_called_action_identity(self, action_id: str) -> bool:
        action = self.actions[action_id]
        if action.state not in {
            ExecutionActionState.SUBMITTING,
            ExecutionActionState.UNKNOWN,
            ExecutionActionState.OPEN,
        }:
            return False
        self.called_queries.append(action_id)
        return True

    def external_activity_conflict(self, _activation_id: str) -> bool:
        return self.has_external_activity_conflict

    def apply_persisted_user_takeover(
        self,
        *,
        activation_id: str,
        observed_at: datetime,
    ) -> tuple[SimpleNamespace, ...]:
        self.takeover_calls.append((activation_id, observed_at))
        for action in self.actions.values():
            if action.state is ExecutionActionState.READY:
                action.state = ExecutionActionState.HANDED_OVER
        return tuple(self.actions.values())

    def create_protection_for_fill(self, **kwargs: object) -> SimpleNamespace:
        self.protection_checks.append(kwargs["action_check"])
        action = _action(
            "protection-action",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.READY,
            terms={
                "action_profile": "PROTECTIVE_STOP_REDUCE_ONLY",
                "quantity": "0.01",
                "trigger_price": "97",
            },
        )
        return SimpleNamespace(execution_action=action)

    def create_direct_protection_replacement(
        self,
        **kwargs: object,
    ) -> SimpleNamespace:
        self.protection_replacement_requests.append(dict(kwargs))
        predecessor = self.actions[str(kwargs["predecessor_action_id"])]
        predecessor_context = dict(predecessor.action_terms["execution_context"])
        action = _action(
            str(kwargs["execution_action_id"]),
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.READY,
            terms={
                "action_profile": "PROTECTIVE_STOP_REDUCE_ONLY",
                "quantity": predecessor.action_terms["quantity"],
                "trigger_price": kwargs["target_trigger_price"],
                "execution_context": {
                    **predecessor_context,
                    "protection_replacement": {
                        "step_index": kwargs["step_index"],
                        "predecessor_action_ref": predecessor.execution_action_id,
                        "trigger_price": kwargs["target_trigger_price"],
                    },
                },
            },
            client_order_id=str(kwargs["client_order_id"]),
            created_at=kwargs["observed_at"],
        )
        self.actions[action.execution_action_id] = action
        return SimpleNamespace(execution_action=action)

    def create_direct_take_profit_replacement(
        self,
        **kwargs: object,
    ) -> SimpleNamespace:
        self.take_profit_replacement_requests.append(dict(kwargs))
        predecessor = self.actions[str(kwargs["predecessor_action_id"])]
        predecessor_context = dict(predecessor.action_terms["execution_context"])
        level_context = dict(predecessor_context["direct_take_profit"])
        action = _action(
            str(kwargs["execution_action_id"]),
            ExecutionActionKind.TAKE_PROFIT,
            state=ExecutionActionState.READY,
            terms={
                "action_profile": predecessor.action_terms["action_profile"],
                "quantity": kwargs["action_check"].quantized_quantity,
                "trigger_price": kwargs["target_trigger_price"],
                "execution_context": {
                    **predecessor_context,
                    "direct_take_profit": {
                        **level_context,
                        "trigger_price": kwargs["target_trigger_price"],
                        "quantity": kwargs["action_check"].quantized_quantity,
                    },
                    "take_profit_replacement": {
                        "aggregate_revision": kwargs["aggregate_revision"],
                        "predecessor_action_ref": predecessor.execution_action_id,
                        "trigger_price": kwargs["target_trigger_price"],
                    },
                },
            },
            client_order_id=str(kwargs["client_order_id"]),
            created_at=kwargs["observed_at"],
        )
        self.actions[action.execution_action_id] = action
        return SimpleNamespace(execution_action=action)

    def create_take_profits_for_protected_fill(
        self, **kwargs: object
    ) -> tuple[SimpleNamespace, SimpleNamespace]:
        self.take_profit_checks.extend(kwargs["action_checks"])
        return tuple(
            SimpleNamespace(
                execution_action=_action(
                    f"take-profit-{index}",
                    ExecutionActionKind.TAKE_PROFIT,
                    state=ExecutionActionState.READY,
                    terms={
                        "action_profile": f"TAKE_PROFIT_{index}",
                        "quantity": "0.005",
                        "trigger_price": trigger,
                    },
                )
            )
            for index, trigger in ((1, "104.5"), (2, "109"))
        )

    def process_execution_action(self, action_id: str, **kwargs: object) -> None:
        self.submissions.append((action_id, kwargs["request_payload"]))
        if action_id in self.actions:
            self.actions[action_id].state = ExecutionActionState.SUBMITTING

    def reject_execution_action_before_submission(
        self,
        action_id: str,
        *,
        reason_code: str,
        **_kwargs: object,
    ) -> SimpleNamespace:
        self.rejections.append((action_id, reason_code))
        action = self.actions[action_id]
        action.state = ExecutionActionState.NOT_SUBMITTED
        action.not_submitted_reason = reason_code
        return action

    def apply_venue_fact(
        self, fact: object, **_kwargs: object
    ) -> SimpleNamespace | None:
        self.applied_facts.append(fact)
        action_ref = getattr(fact, "action_ref", None)
        if action_ref is None:
            return None
        action = self.actions[action_ref]
        self.facts[action_ref] = (*self.facts.get(action_ref, ()), fact)
        if getattr(fact, "kind", None) in {
            VenueFactKind.ORDER_STATE,
            VenueFactKind.FILL,
        }:
            action.state = ExecutionActionState.OPEN
        return action

    def create_position_exit(self, **kwargs: object) -> SimpleNamespace:
        self.exit_checks.append(kwargs["action_check"])
        self.exit_requests.append(dict(kwargs))
        action = _action(
            "exit-action",
            ExecutionActionKind.EXIT,
            state=ExecutionActionState.READY,
            terms={
                "action_profile": "REDUCE_OR_CLOSE_MARKET",
                "quantity": kwargs["position_quantity"],
                "exit_responsibility_role": kwargs["exit_responsibility_role"].value,
            },
            client_order_id="e" * 32,
        )
        self.actions[action.execution_action_id] = action
        return SimpleNamespace(execution_action=action)

    def create_take_profit_market_reduction(
        self,
        **kwargs: object,
    ) -> SimpleNamespace:
        self.risk_reduction_checks.append(kwargs["action_check"])
        self.risk_reduction_requests.append(dict(kwargs))
        action = _action(
            str(kwargs["execution_action_id"]),
            ExecutionActionKind.RISK_REDUCTION,
            state=ExecutionActionState.READY,
            terms={
                "action_profile": "REDUCE_OR_CLOSE_MARKET",
                "quantity": kwargs["position_quantity"],
                "execution_context": {
                    "position_fact_ref": kwargs["position_fact_ref"],
                    "rejected_take_profit_action_ref": kwargs[
                        "rejected_take_profit_action_id"
                    ],
                    "rejection_fact_ref": kwargs["rejection_fact_ref"],
                },
            },
            client_order_id=str(kwargs["client_order_id"]),
            created_at=kwargs["observed_at"],
        )
        self.actions[action.execution_action_id] = action
        return SimpleNamespace(execution_action=action)

    def create_cancel_for_action(self, **kwargs: object) -> SimpleNamespace:
        cancel_index = len(self.cancel_checks)
        self.cancel_checks.append(kwargs["action_check"])
        self.cancel_requests.append(dict(kwargs))
        target = self.actions[str(kwargs["target_action_id"])]
        action = _action(
            "cancel-action"
            if cancel_index == 0
            else f"cancel-action-{cancel_index + 1}",
            ExecutionActionKind.CANCEL,
            state=ExecutionActionState.READY,
            terms={"action_profile": "CANCEL_ORDER", "quantity": None},
            cancel_target={
                "client_order_id": target.client_order_id,
                "endpoint": kwargs["target_endpoint"],
            },
        )
        action.source_identity = (
            f"activation-1:CANCEL:{target.execution_action_id}:{kwargs['reason_ref']}"
        )
        self.actions[action.execution_action_id] = action
        return SimpleNamespace(execution_action=action)

    def reconcile_execution_action(
        self,
        action_id: str,
        **kwargs: object,
    ) -> SimpleNamespace:
        self.reconciliations.append({"action_id": action_id, **kwargs})
        action = self.actions[action_id]
        action.state = ExecutionActionState.CLOSED
        return action

    def reconcile_cancel_from_target_fact(
        self,
        action_id: str,
        **kwargs: object,
    ) -> SimpleNamespace:
        self.reconciliations.append({"action_id": action_id, **kwargs})
        action = self.actions[action_id]
        action.state = ExecutionActionState.CLOSED
        return action

    def close_activation(self, **kwargs: object) -> str:
        self.closures.append(kwargs)
        return "c" * 64

    def record_funding_income(self, **kwargs: object) -> tuple[object, ...]:
        self.funding_records.append(dict(kwargs))
        return ()


def test_zero_fill_activation_skips_funding_query() -> None:
    async def scenario() -> int:
        calls = 0

        async def funding_provider(
            _activation: PlanActivation,
            _end_time: datetime,
        ) -> tuple[object, ...]:
            nonlocal calls
            calls += 1
            return ()

        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=_Coordinator(_activation()),
            fact_provider=lambda _activation: asyncio.sleep(0, result=_facts()),
            funding_provider=funding_provider,
            environment_id="demo-1",
        )
        await boundary._sync_funding(_activation(), end_time=NOW, force=True)
        return calls

    assert asyncio.run(scenario()) == 0


def test_direct_entry_waiting_for_first_fill_has_no_aggregate_targets_yet() -> None:
    async def scenario() -> _Coordinator:
        activation = _activation().model_copy(
            update={
                "decision_basis_ref": "DIRECT_EXECUTION@1",
                "order_schedule_snapshot": SimpleNamespace(
                    full_fill_protection_estimate=object(),
                    schedule_spec=SimpleNamespace(dynamic_rules=()),
                ),
            }
        )
        coordinator = _Coordinator(activation)
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.OPEN,
            terms={
                "action_profile": "ENTRY_LIMIT",
                "quantity": "0.01",
            },
            client_order_id="e" * 32,
            call_started_at=NOW,
        )
        coordinator.actions[entry.execution_action_id] = entry
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact(
                "entry-working",
                VenueFactKind.ORDER_STATE,
                status="WORKING",
                action_ref=entry.execution_action_id,
                activation_ref=activation.activation_id,
            ),
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(
                    current_abs_position="0",
                    open_order_client_ids=(entry.client_order_id,),
                ),
            ),
            environment_id="demo-1",
        )

        await boundary.sync(activation.activation_id, force=True)
        boundary.close()
        return coordinator

    coordinator = asyncio.run(scenario())

    assert coordinator.protection_replacement_requests == []
    assert coordinator.take_profit_replacement_requests == []
    assert coordinator.called_queries == []


@pytest.mark.parametrize(
    ("method_name", "reason_code"),
    (
        (
            "_manage_aggregate_entry_protection",
            "PROTECTION_AGGREGATE_TARGET_UNKNOWN",
        ),
        (
            "_manage_aggregate_entry_take_profits",
            "TAKE_PROFIT_AGGREGATE_TARGET_UNKNOWN",
        ),
    ),
)
def test_confirmed_direct_fill_with_missing_aggregate_target_fails_closed(
    method_name: str,
    reason_code: str,
) -> None:
    activation = _activation().model_copy(
        update={
            "decision_basis_ref": "DIRECT_EXECUTION@1",
            "has_entry_fill": True,
            "order_schedule_snapshot": SimpleNamespace(
                full_fill_protection_estimate=object(),
            ),
            "rule_state": {
                "direct_protection": {
                    "fills": {"fill-fact": {}},
                }
            },
        }
    )
    coordinator = _Coordinator(activation)
    loop = asyncio.new_event_loop()
    boundary = ProductResponsibilityBoundary(
        loop=loop,
        coordinator=coordinator,
        fact_provider=lambda _activation: asyncio.sleep(0, result=_facts()),
        environment_id="demo-1",
    )

    try:
        with pytest.raises(ValueError, match=reason_code):
            getattr(boundary, method_name)(activation, _facts(), ())
    finally:
        boundary.close()
        loop.close()


def test_funding_query_failure_is_nonfatal_accounting_unavailability() -> None:
    async def scenario() -> tuple[list[str], list[str], list[dict[str, object]]]:
        responsibility_failures: list[str] = []
        funding_unavailable: list[str] = []
        coordinator = _Coordinator(_activation(first_fill=True))

        async def funding_provider(
            _activation: PlanActivation,
            _end_time: datetime,
        ) -> tuple[object, ...]:
            raise RuntimeError("temporary signed read failure")

        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(0, result=_facts()),
            funding_provider=funding_provider,
            environment_id="demo-1",
            failure_sink=lambda exception: responsibility_failures.append(
                type(exception).__name__
            ),
            funding_fact_unavailable_sink=lambda exception: (
                funding_unavailable.append(type(exception).__name__)
            ),
        )
        await boundary._sync_funding(
            coordinator.activation,
            end_time=NOW,
            force=True,
        )
        return (
            responsibility_failures,
            funding_unavailable,
            coordinator.funding_records,
        )

    responsibility_failures, funding_unavailable, funding_records = asyncio.run(
        scenario()
    )

    assert responsibility_failures == []
    assert funding_unavailable == ["RuntimeError"]
    assert funding_records == []


def test_stepped_protection_places_tighter_stop_before_cancelling_old_stop() -> None:
    rule = SteppedProtectionRule(
        steps=(
            ProtectionStep(trigger_r="1", stop_r="0.5"),
            ProtectionStep(trigger_r="2", stop_r="1"),
        ),
        minimum_update_interval_seconds=5,
        max_adjustments=8,
    )
    activation = _direct_activation_with_time_exit().model_copy(
        update={
            "order_schedule_snapshot": SimpleNamespace(
                schedule_spec=SimpleNamespace(dynamic_rules=(rule,))
            ),
            "protection_state": ProtectionState.WORKING,
        }
    )
    coordinator = _Coordinator(activation)
    original = _action(
        "protection-original",
        ExecutionActionKind.PROTECTION,
        state=ExecutionActionState.OPEN,
        terms={
            "action_profile": "PROTECTIVE_STOP_REDUCE_ONLY",
            "quantity": "0.01",
            "trigger_price": "99",
            "execution_context": {
                "entry_action_ref": "entry-action",
                "fill_fact_ref": "fill-fact",
                "fill_source_identity": "trade-1:1",
                "direct_fill": {},
            },
        },
        client_order_id="a" * 32,
    )
    coordinator.actions[original.execution_action_id] = original
    coordinator.facts[original.execution_action_id] = (
        _venue_fact(
            "protection-original-working",
            VenueFactKind.ORDER_STATE,
            status="WORKING",
        ),
    )
    loop = asyncio.new_event_loop()
    boundary = ProductResponsibilityBoundary(
        loop=loop,
        coordinator=coordinator,
        fact_provider=lambda _activation: asyncio.sleep(0, result=_facts()),
        environment_id="demo-1",
    )
    facts = _facts(current_reference_price="101.2")

    try:
        boundary._manage_dynamic_protection(
            activation,
            facts,
            coordinator.list_execution_actions("activation-1"),
        )
        replacements = [
            action
            for action in coordinator.actions.values()
            if action.action_kind is ExecutionActionKind.PROTECTION
            and isinstance(
                action.action_terms.get("execution_context", {}).get(
                    "protection_replacement"
                ),
                dict,
            )
        ]
        assert len(replacements) == 1
        replacement = replacements[0]
        assert replacement.action_terms["trigger_price"] == "100.5"
        assert coordinator.cancel_requests == []
        assert coordinator.submissions == [
            (
                replacement.execution_action_id,
                {
                    "profile": "PROTECTIVE_STOP_REDUCE_ONLY",
                    "quantity": "0.01",
                    "trigger_price": "100.5",
                },
            )
        ]

        boundary._manage_dynamic_protection(
            activation,
            facts,
            coordinator.list_execution_actions("activation-1"),
        )
        assert coordinator.cancel_requests == []

        replacement.state = ExecutionActionState.OPEN
        coordinator.facts[replacement.execution_action_id] = (
            _venue_fact(
                "protection-replacement-working",
                VenueFactKind.ORDER_STATE,
                status="WORKING",
            ),
        )
        boundary._manage_dynamic_protection(
            activation,
            facts,
            coordinator.list_execution_actions("activation-1"),
        )
        assert len(coordinator.cancel_requests) == 1
        assert coordinator.cancel_requests[0]["target_action_id"] == (
            original.execution_action_id
        )
        assert coordinator.cancel_requests[0]["target_endpoint"] == "ALGO"
    finally:
        boundary.close()
        loop.close()


def test_continuous_profit_lock_quantizes_and_only_tightens_protection() -> None:
    rule = ProfitLockRule(
        mode=ProfitLockMode.RATIO,
        activation_r="1",
        lock_fraction="0.5",
        minimum_step_r="0.25",
        minimum_update_interval_seconds=5,
        max_adjustments=8,
    )
    activation = _direct_activation_with_time_exit().model_copy(
        update={
            "order_schedule_snapshot": SimpleNamespace(
                schedule_spec=SimpleNamespace(dynamic_rules=(rule,))
            ),
            "protection_state": ProtectionState.WORKING,
        }
    )
    coordinator = _Coordinator(activation)
    original = _action(
        "protection-original",
        ExecutionActionKind.PROTECTION,
        state=ExecutionActionState.OPEN,
        terms={
            "action_profile": "PROTECTIVE_STOP_REDUCE_ONLY",
            "quantity": "0.01",
            "trigger_price": "99",
            "execution_context": {
                "entry_action_ref": "entry-action",
                "fill_fact_ref": "fill-fact",
                "fill_source_identity": "trade-1:1",
                "direct_fill": {},
            },
        },
        client_order_id="a" * 32,
    )
    coordinator.actions[original.execution_action_id] = original
    coordinator.facts[original.execution_action_id] = (
        _venue_fact(
            "protection-original-working",
            VenueFactKind.ORDER_STATE,
            status="WORKING",
        ),
    )
    loop = asyncio.new_event_loop()
    boundary = ProductResponsibilityBoundary(
        loop=loop,
        coordinator=coordinator,
        fact_provider=lambda _activation: asyncio.sleep(0, result=_facts()),
        environment_id="demo-1",
    )

    try:
        boundary._manage_dynamic_protection(
            activation,
            _facts(current_reference_price="102.2"),
            coordinator.list_execution_actions("activation-1"),
        )

        assert len(coordinator.protection_replacement_requests) == 1
        assert (
            coordinator.protection_replacement_requests[0]["target_trigger_price"]
            == "101"
        )
        replacement = next(
            action
            for action in coordinator.actions.values()
            if action.execution_action_id != original.execution_action_id
        )
        assert replacement.action_terms["trigger_price"] == "101"
    finally:
        boundary.close()
        loop.close()


def test_aggregate_take_profit_reprice_waits_for_cancel_and_uses_partial_remainder() -> None:
    activation = _activation().model_copy(
        update={
            "decision_basis_ref": "DIRECT_EXECUTION@1",
            "order_schedule_snapshot": SimpleNamespace(
                full_fill_protection_estimate=object(),
            ),
            "rule_state": {
                "direct_protection": {
                    "fills": {"fill-fact": {}},
                    "aggregate_revision": 2,
                    "aggregate_target": {
                        "initial_stop_price": "99",
                        "take_profit_prices": ["108"],
                    },
                }
            },
        }
    )
    coordinator = _Coordinator(activation)
    original = _action(
        "take-profit-original",
        ExecutionActionKind.TAKE_PROFIT,
        state=ExecutionActionState.OPEN,
        terms={
            "action_profile": "TAKE_PROFIT_1",
            "quantity": "0.01",
            "trigger_price": "110",
            "execution_context": {
                "entry_action_ref": "entry-action",
                "protection_action_ref": "protection-action",
                "fill_fact_ref": "fill-fact",
                "fill_source_identity": "trade-1:1",
                "direct_take_profit": {
                    "level_index": 0,
                    "trigger_r": "2",
                    "quantity_fraction": "1",
                    "trigger_price": "110",
                    "quantity": "0.01",
                },
            },
        },
        client_order_id="t" * 32,
    )
    coordinator.actions[original.execution_action_id] = original
    coordinator.facts[original.execution_action_id] = (
        _venue_fact(
            "take-profit-working",
            VenueFactKind.ORDER_STATE,
            status="WORKING",
        ),
    )
    loop = asyncio.new_event_loop()
    boundary = ProductResponsibilityBoundary(
        loop=loop,
        coordinator=coordinator,
        fact_provider=lambda _activation: asyncio.sleep(0, result=_facts()),
        environment_id="demo-1",
    )

    try:
        boundary._manage_aggregate_entry_take_profits(
            activation,
            _facts(),
            coordinator.list_execution_actions("activation-1"),
        )
        assert len(coordinator.cancel_requests) == 1
        assert coordinator.take_profit_replacement_requests == []

        original.state = ExecutionActionState.CLOSED
        coordinator.facts[original.execution_action_id] = (
            _venue_fact(
                "take-profit-partial",
                VenueFactKind.FILL,
                status="PARTIALLY_FILLED",
                trade_id="tp-trade-1",
                last_quantity="0.004",
                leaves_quantity="0.006",
            ),
            _venue_fact(
                "take-profit-cancelled",
                VenueFactKind.ORDER_STATE,
                status="CANCELLED",
                cumulative_filled_quantity="0.004",
            ),
        )
        boundary._manage_aggregate_entry_take_profits(
            activation,
            _facts(current_abs_position="0.006"),
            coordinator.list_execution_actions("activation-1"),
        )

        assert len(coordinator.take_profit_replacement_requests) == 1
        request = coordinator.take_profit_replacement_requests[0]
        assert request["target_trigger_price"] == "108"
        assert request["aggregate_revision"] == 2
        assert request["action_check"].quantized_quantity == "0.006"
        replacement = coordinator.actions[str(request["execution_action_id"])]
        assert replacement.action_terms["quantity"] == "0.006"
        assert coordinator.submissions[-1][1] == {
            "profile": "TAKE_PROFIT_1",
            "quantity": "0.006",
            "trigger_price": "108",
        }
    finally:
        boundary.close()
        loop.close()


def test_cancelled_predecessor_with_working_replacement_does_not_force_exit() -> None:
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_activation(first_fill=True))
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        fill = _venue_fact(
            "fill-fact",
            VenueFactKind.FILL,
            trade_id="entry-trade",
            last_quantity="0.01",
            action_ref=entry.execution_action_id,
            activation_ref="activation-1",
        )
        original = _action(
            "protection-original",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.CLOSED,
            terms={
                "action_profile": "PROTECTIVE_STOP_REDUCE_ONLY",
                "quantity": "0.01",
                "trigger_price": "99",
                "execution_context": {
                    "entry_action_ref": entry.execution_action_id,
                    "fill_fact_ref": fill.venue_fact_id,
                    "fill_source_identity": "trade-1:1",
                },
            },
            client_order_id="a" * 32,
        )
        replacement = _action(
            "protection-replacement",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.OPEN,
            terms={
                "action_profile": "PROTECTIVE_STOP_REDUCE_ONLY",
                "quantity": "0.01",
                "trigger_price": "100",
                "execution_context": {
                    "entry_action_ref": entry.execution_action_id,
                    "fill_fact_ref": fill.venue_fact_id,
                    "fill_source_identity": "trade-1:1",
                    "protection_replacement": {
                        "predecessor_action_ref": original.execution_action_id,
                        "step_index": 0,
                    },
                },
            },
            client_order_id="b" * 32,
        )
        coordinator.actions.update(
            {
                entry.execution_action_id: entry,
                original.execution_action_id: original,
                replacement.execution_action_id: replacement,
            }
        )
        coordinator.facts[entry.execution_action_id] = (fill,)
        coordinator.facts[original.execution_action_id] = (
            _venue_fact(
                "protection-original-cancelled",
                VenueFactKind.ORDER_STATE,
                status="CANCELLED",
            ),
        )
        coordinator.facts[replacement.execution_action_id] = (
            _venue_fact(
                "protection-replacement-working",
                VenueFactKind.ORDER_STATE,
                status="WORKING",
            ),
        )
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.01",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(position_fact=position_fact),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.exit_requests == []


def test_explicit_exit_keeps_plan_exit_cause_after_protection_cancel() -> None:
    async def scenario() -> _Coordinator:
        activation = _activation(first_fill=True).model_copy(
            update={
                "lifecycle": PlanLifecycle.EXITING,
                "protection_state": ProtectionState.GAP,
            }
        )
        coordinator = _Coordinator(activation)
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        fill = _venue_fact(
            "fill-fact",
            VenueFactKind.FILL,
            trade_id="entry-trade",
            last_quantity="0.01",
            action_ref=entry.execution_action_id,
            activation_ref="activation-1",
        )
        protection = _action(
            "protection-cancelled-for-exit",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.CLOSED,
            terms={
                "action_profile": "PROTECTIVE_STOP_REDUCE_ONLY",
                "quantity": "0.01",
                "trigger_price": "99",
                "execution_context": {
                    "entry_action_ref": entry.execution_action_id,
                    "fill_fact_ref": fill.venue_fact_id,
                    "fill_source_identity": "trade-1:1",
                },
            },
            client_order_id="a" * 32,
        )
        coordinator.actions.update(
            {
                entry.execution_action_id: entry,
                protection.execution_action_id: protection,
            }
        )
        coordinator.facts[entry.execution_action_id] = (fill,)
        coordinator.facts[protection.execution_action_id] = (
            _venue_fact(
                "protection-cancelled",
                VenueFactKind.ORDER_STATE,
                status="CANCELLED",
            ),
        )
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.01",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(position_fact=position_fact),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert len(coordinator.exit_requests) == 1
    assert coordinator.exit_requests[0]["reason_ref"].endswith("PLAN_EXIT")


def test_shared_position_replaces_stop_only_after_old_stop_is_cancelled() -> None:
    rule = SteppedProtectionRule(
        steps=(ProtectionStep(trigger_r="1", stop_r="0.5"),),
        minimum_update_interval_seconds=5,
        max_adjustments=8,
    )
    activation = _direct_activation_with_time_exit().model_copy(
        update={
            "order_schedule_snapshot": SimpleNamespace(
                schedule_spec=SimpleNamespace(dynamic_rules=(rule,))
            ),
            "protection_state": ProtectionState.WORKING,
        }
    )
    coordinator = _Coordinator(activation)
    original = _action(
        "protection-original",
        ExecutionActionKind.PROTECTION,
        state=ExecutionActionState.OPEN,
        terms={
            "action_profile": "PROTECTIVE_STOP_REDUCE_ONLY",
            "quantity": "0.01",
            "trigger_price": "99",
            "execution_context": {
                "entry_action_ref": "entry-action",
                "fill_fact_ref": "fill-fact",
                "fill_source_identity": "trade-1:1",
                "direct_fill": {},
            },
        },
        client_order_id="a" * 32,
    )
    coordinator.actions[original.execution_action_id] = original
    coordinator.facts[original.execution_action_id] = (
        _venue_fact(
            "protection-original-working",
            VenueFactKind.ORDER_STATE,
            status="WORKING",
        ),
    )
    position_fact = _venue_fact(
        "shared-position",
        VenueFactKind.POSITION_STATE,
        position_quantity="0.02",
    )
    position_fact.payload.update(
        {
            "activation_position_quantity": "0.01",
            "attributed_account_position_quantity": "0.02",
        }
    )
    facts = _facts(
        current_reference_price="101.2",
        position_fact=position_fact,
    )
    loop = asyncio.new_event_loop()
    boundary = ProductResponsibilityBoundary(
        loop=loop,
        coordinator=coordinator,
        fact_provider=lambda _activation: asyncio.sleep(0, result=facts),
        environment_id="demo-1",
    )

    try:
        boundary._manage_dynamic_protection(
            activation,
            facts,
            coordinator.list_execution_actions("activation-1"),
        )
        replacement = next(
            action
            for action in coordinator.actions.values()
            if isinstance(
                action.action_terms.get("execution_context", {}).get(
                    "protection_replacement"
                ),
                dict,
            )
        )
        assert coordinator.submissions == [
            ("cancel-action", {"profile": "CANCEL_ORDER"})
        ]
        assert replacement.state is ExecutionActionState.READY

        coordinator.facts[original.execution_action_id] = (
            *coordinator.facts[original.execution_action_id],
            _venue_fact(
                "protection-original-cancelled",
                VenueFactKind.ORDER_STATE,
                status="CANCELLED",
            ),
        )
        boundary._resume_ready_non_entry_actions(
            activation,
            facts,
            coordinator.list_execution_actions("activation-1"),
        )
        assert coordinator.submissions[-1] == (
            replacement.execution_action_id,
            {
                "profile": "PROTECTIVE_STOP_REDUCE_ONLY",
                "quantity": "0.01",
                "trigger_price": "100.5",
            },
        )
    finally:
        boundary.close()
        loop.close()


class _SuccessorCoordinator(_Coordinator):
    def create_position_exit(self, **kwargs: object) -> SimpleNamespace:
        self.exit_checks.append(kwargs["action_check"])
        self.exit_requests.append(dict(kwargs))
        action = _action(
            str(kwargs["execution_action_id"]),
            ExecutionActionKind.EXIT,
            state=ExecutionActionState.READY,
            terms={
                "action_profile": "REDUCE_OR_CLOSE_MARKET",
                "quantity": kwargs["position_quantity"],
                "exit_responsibility_role": kwargs["exit_responsibility_role"].value,
            },
            client_order_id=str(kwargs["client_order_id"]),
        )
        action.source_identity = (
            f"activation-1:EXIT:{kwargs['position_fact_ref']}:{kwargs['reason_ref']}"
        )
        self.actions[action.execution_action_id] = action
        return SimpleNamespace(execution_action=action)


def test_user_takeover_hands_over_ready_actions_and_only_queries_called_identity() -> (
    None
):
    async def scenario() -> _Coordinator:
        activation = _activation().model_copy(
            update={
                "lifecycle": PlanLifecycle.USER_TAKEOVER,
                "takeover_scope": {"command_ref": "command-takeover-1"},
            }
        )
        coordinator = _Coordinator(activation)
        coordinator.actions["entry-ready"] = _action(
            "entry-ready",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.READY,
            terms={},
            client_order_id="a" * 32,
        )
        coordinator.actions["entry-open"] = _action(
            "entry-open",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.OPEN,
            terms={},
            client_order_id="b" * 32,
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(0, result=_facts()),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())

    assert coordinator.actions["entry-ready"].state is ExecutionActionState.HANDED_OVER
    assert coordinator.called_queries == ["entry-open"]
    assert coordinator.submissions == []
    assert len(coordinator.takeover_calls) == 1


def test_responsibility_failure_uses_structured_sink_without_default_handler() -> None:
    async def scenario() -> tuple[list[str], list[dict[str, object]]]:
        failures: list[str] = []
        loop_contexts: list[dict[str, object]] = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: loop_contexts.append(context))
        coordinator = _Coordinator(_activation())

        async def failing_facts(_activation):
            raise RuntimeError("private diagnostic detail")

        boundary = ProductResponsibilityBoundary(
            loop=loop,
            coordinator=coordinator,
            fact_provider=failing_facts,
            environment_id="demo-1",
            failure_sink=lambda exception: failures.append(type(exception).__name__),
        )
        boundary._schedule("failure", failing_facts(_activation()))
        with pytest.raises(RuntimeError, match="private diagnostic detail"):
            await boundary.wait_idle()
        await asyncio.sleep(0)
        boundary.close()
        return failures, loop_contexts

    failures, loop_contexts = asyncio.run(scenario())

    assert failures == ["RuntimeError"]
    assert loop_contexts == []


def test_responsibility_boundary_keeps_only_bounded_completed_task_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> ProductResponsibilityBoundary:
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=_Coordinator(_activation()),
            fact_provider=lambda _activation: asyncio.sleep(0, result=_facts()),
            environment_id="demo-1",
        )
        for index in range(3):
            boundary._schedule(f"event-{index}", asyncio.sleep(0))
        await boundary.wait_idle()
        await asyncio.sleep(0)
        return boundary

    monkeypatch.setattr(
        responsibilities_module,
        "COMPLETED_RESPONSIBILITY_TASK_KEY_LIMIT",
        2,
    )
    boundary = asyncio.run(scenario())

    assert boundary._tasks == {}
    assert tuple(boundary._completed_task_keys) == ("event-1", "event-2")
    boundary.close()


def test_responsibility_boundary_prunes_terminal_action_query_history() -> None:
    async def scenario() -> ProductResponsibilityBoundary:
        coordinator = _Coordinator(_activation())
        coordinator.actions["open-action"] = _action(
            "open-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.OPEN,
            terms={},
            call_started_at=NOW,
        )
        coordinator.actions["closed-action"] = _action(
            "closed-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={},
            call_started_at=NOW,
        )

        async def unavailable_facts(_activation):
            raise RuntimeError("stop after cache pruning")

        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=unavailable_facts,
            environment_id="demo-1",
        )
        boundary._last_called_action_query.update(
            {"open-action": 1.0, "closed-action": 1.0}
        )
        with pytest.raises(RuntimeError, match="stop after cache pruning"):
            await boundary.sync("activation-1", force=True)
        return boundary

    boundary = asyncio.run(scenario())

    assert boundary._last_called_action_query == {"open-action": 1.0}
    boundary.close()


def test_responsibility_boundary_releases_runtime_history_when_completed() -> None:
    async def scenario() -> ProductResponsibilityBoundary:
        coordinator = _Coordinator(
            _activation().model_copy(
                update={"lifecycle": PlanLifecycle.COMPLETED}
            )
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(0, result=_facts()),
            environment_id="demo-1",
        )
        boundary._last_fallback_sync["activation-1"] = 1.0
        boundary._last_funding_sync["activation-1"] = 1.0
        boundary._last_called_action_query["old-action"] = 1.0
        await boundary.sync("activation-1", force=True)
        return boundary

    boundary = asyncio.run(scenario())

    assert boundary._last_fallback_sync == {}
    assert boundary._last_funding_sync == {}
    assert boundary._last_called_action_query == {}
    boundary.close()


def test_responsibility_failure_sink_error_is_sanitized() -> None:
    async def scenario() -> list[dict[str, object]]:
        loop_contexts: list[dict[str, object]] = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: loop_contexts.append(context))

        async def failing_facts(_activation):
            raise RuntimeError("private original diagnostic")

        def failed_sink(_exception: BaseException) -> None:
            raise RuntimeError("private sink diagnostic")

        boundary = ProductResponsibilityBoundary(
            loop=loop,
            coordinator=_Coordinator(_activation()),
            fact_provider=failing_facts,
            environment_id="demo-1",
            failure_sink=failed_sink,
        )
        boundary._schedule("failure", failing_facts(_activation()))
        with pytest.raises(RuntimeError, match="private original diagnostic"):
            await boundary.wait_idle()
        await asyncio.sleep(0)
        boundary.close()
        return loop_contexts

    loop_contexts = asyncio.run(scenario())

    assert len(loop_contexts) == 1
    assert (
        loop_contexts[0]["message"]
        == "HALPHA_PRODUCT_RESPONSIBILITY_FAILURE_SINK_FAILED"
    )
    assert loop_contexts[0]["exception_type"] == "RuntimeError"
    assert "exception" not in loop_contexts[0]
    assert "task" not in loop_contexts[0]


def test_user_takeover_closure_preserves_handover_command_identity() -> None:
    async def scenario() -> _Coordinator:
        activation = _activation().model_copy(
            update={
                "lifecycle": PlanLifecycle.USER_TAKEOVER,
                "takeover_scope": {"command_ref": "command-takeover-1"},
            }
        )
        coordinator = _Coordinator(activation)
        coordinator.actions["entry-ready"] = _action(
            "entry-ready",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.READY,
            terms={},
            client_order_id="a" * 32,
        )
        position = _venue_fact(
            "position-zero",
            VenueFactKind.POSITION_STATE,
            position_quantity="0",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(current_abs_position="0", position_fact=position),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())

    assert len(coordinator.closures) == 1
    assert coordinator.closures[0]["user_takeover"] is True
    assert coordinator.closures[0]["handover_command_ref"] == "command-takeover-1"
    assert coordinator.closures[0]["fact_refs"] == ("position-zero",)


def test_user_takeover_closes_venue_downsized_terminal_order_after_account_flat() -> (
    None
):
    async def scenario() -> _Coordinator:
        activation = _activation().model_copy(
            update={
                "lifecycle": PlanLifecycle.USER_TAKEOVER,
                "takeover_scope": {"command_ref": "command-takeover-1"},
            }
        )
        coordinator = _Coordinator(activation)
        entry = _action(
            "entry-closed",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.0012"},
            client_order_id="a" * 32,
        )
        take_profit = _action(
            "take-profit-downsized",
            ExecutionActionKind.TAKE_PROFIT,
            state=ExecutionActionState.OPEN,
            terms={"quantity": "0.0019"},
            client_order_id="b" * 32,
        )
        coordinator.actions = {
            entry.execution_action_id: entry,
            take_profit.execution_action_id: take_profit,
        }
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact(
                "entry-fill",
                VenueFactKind.FILL,
                trade_id="trade-entry",
                last_quantity="0.0012",
                action_ref=entry.execution_action_id,
                activation_ref=activation.activation_id,
            ),
            _venue_fact(
                "entry-fee",
                VenueFactKind.COMMISSION,
                trade_id="trade-entry",
                action_ref=entry.execution_action_id,
                activation_ref=activation.activation_id,
            ),
        )
        coordinator.facts[take_profit.execution_action_id] = (
            _venue_fact(
                "take-profit-terminal",
                VenueFactKind.ORDER_STATE,
                status="FILLED",
                cumulative_filled_quantity="0.0012",
                action_ref=take_profit.execution_action_id,
                activation_ref=activation.activation_id,
            ),
            _venue_fact(
                "take-profit-fill",
                VenueFactKind.FILL,
                trade_id="trade-take-profit",
                last_quantity="0.0012",
                action_ref=take_profit.execution_action_id,
                activation_ref=activation.activation_id,
            ),
            _venue_fact(
                "take-profit-fee",
                VenueFactKind.COMMISSION,
                trade_id="trade-take-profit",
                action_ref=take_profit.execution_action_id,
                activation_ref=activation.activation_id,
            ),
        )
        position = _venue_fact(
            "position-zero-after-takeover",
            VenueFactKind.POSITION_STATE,
            position_quantity="0",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(current_abs_position="0", position_fact=position),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())

    assert coordinator.actions["take-profit-downsized"].state is ExecutionActionState.CLOSED
    assert len(coordinator.reconciliations) == 1
    evidence = coordinator.reconciliations[0]["closure_evidence"]
    assert evidence["user_takeover_downsized_terminal_accounted"] is True
    assert len(coordinator.closures) == 1
    assert coordinator.closures[0]["user_takeover"] is True
    assert coordinator.closures[0]["fact_refs"] == (
        "position-zero-after-takeover",
        "take-profit-fee",
        "take-profit-fill",
        "take-profit-terminal",
    )


def test_late_fill_event_during_user_takeover_never_creates_protection() -> None:
    async def scenario() -> _Coordinator:
        activation = _activation().model_copy(
            update={
                "lifecycle": PlanLifecycle.USER_TAKEOVER,
                "takeover_scope": {"command_ref": "command-takeover-1"},
            }
        )
        coordinator = _Coordinator(activation)
        entry = _action(
            "entry-open",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.OPEN,
            terms={},
            client_order_id="a" * 32,
        )
        coordinator.actions[entry.execution_action_id] = entry
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(0, result=_facts()),
            environment_id="demo-1",
        )
        fill = _venue_fact(
            "late-fill",
            VenueFactKind.FILL,
            trade_id="trade-late",
            last_quantity="0.01",
            action_ref=entry.execution_action_id,
            activation_ref=activation.activation_id,
        )

        boundary.submit_event(NormalizedNautilusEvent(action=entry, facts=(fill,)))
        await boundary.wait_idle()
        return coordinator

    coordinator = asyncio.run(scenario())

    assert coordinator.protection_checks == []
    assert coordinator.submissions == []
    assert coordinator.called_queries == ["entry-open"]


def test_waiting_activation_without_actions_reuses_framework_stream_without_account_poll() -> (
    None
):
    async def scenario() -> int:
        fact_reads = 0

        async def read_facts(_activation: PlanActivation) -> ProductRiskReductionFacts:
            nonlocal fact_reads
            fact_reads += 1
            return _facts()

        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=_Coordinator(_activation()),
            fact_provider=read_facts,
            environment_id="demo-1",
        )

        await boundary.sync("activation-1")
        return fact_reads

    assert asyncio.run(scenario()) == 0


def test_running_responsibility_uses_framework_events_between_bounded_fallback_polls() -> (
    None
):
    async def scenario() -> int:
        fact_reads = 0

        async def read_facts(_activation: PlanActivation) -> ProductRiskReductionFacts:
            nonlocal fact_reads
            fact_reads += 1
            return _facts()

        coordinator = _Coordinator(_activation(first_fill=True))
        coordinator.actions["working-protection"] = _action(
            "working-protection",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.OPEN,
            terms={"quantity": "0.01"},
            client_order_id="a" * 32,
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=read_facts,
            environment_id="demo-1",
        )

        await boundary.sync("activation-1")
        return fact_reads

    assert asyncio.run(scenario()) == 1


def test_position_attribution_mismatch_queries_called_actions_before_retry() -> None:
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_activation(first_fill=True))
        take_profit = _action(
            "take-profit-action",
            ExecutionActionKind.TAKE_PROFIT,
            state=ExecutionActionState.OPEN,
            terms={"quantity": "0.01"},
            client_order_id="b" * 32,
            call_started_at=NOW,
        )
        coordinator.actions[take_profit.execution_action_id] = take_profit

        async def mismatched_facts(
            _activation: PlanActivation,
        ) -> ProductRiskReductionFacts:
            raise ValueError("POSITION_ATTRIBUTION_UNKNOWN")

        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=mismatched_facts,
            environment_id="demo-1",
        )

        with pytest.raises(ValueError, match="POSITION_ATTRIBUTION_UNKNOWN"):
            await boundary.sync("activation-1", force=True)
        with pytest.raises(ValueError, match="POSITION_ATTRIBUTION_UNKNOWN"):
            await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.called_queries == ["take-profit-action"]


def test_position_attribution_mismatch_recovers_triggered_algo_fill_before_exit() -> (
    None
):
    async def scenario() -> tuple[_Coordinator, int]:
        coordinator = _Coordinator(_activation(first_fill=True))
        protection = _action(
            "protection-action",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.OPEN,
            terms={"quantity": "0.01"},
            client_order_id="a" * 32,
            call_started_at=NOW,
        )
        coordinator.actions[protection.execution_action_id] = protection
        reads = 0

        async def facts_after_recovery(
            _activation: PlanActivation,
        ) -> ProductRiskReductionFacts:
            nonlocal reads
            reads += 1
            if not coordinator.applied_facts:
                raise ValueError("POSITION_ATTRIBUTION_UNKNOWN")
            return _facts(current_abs_position="0")

        async def recover_called_action(
            action: object,
        ) -> tuple[SimpleNamespace, ...]:
            assert action is protection
            return (
                _venue_fact(
                    "recovered-stop-fill",
                    VenueFactKind.FILL,
                    trade_id="9001",
                    last_quantity="0.01",
                    action_ref=protection.execution_action_id,
                    activation_ref="activation-1",
                    source_class=VenueFactSourceClass.VENUE_QUERY,
                ),
            )

        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=facts_after_recovery,
            called_action_recovery_fact_provider=recover_called_action,
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator, reads

    coordinator, reads = asyncio.run(scenario())
    assert reads == 2
    assert [fact.venue_fact_id for fact in coordinator.applied_facts] == [
        "recovered-stop-fill"
    ]
    assert coordinator.called_queries == []


def test_missing_open_algo_identity_triggers_bounded_read_only_query() -> None:
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_activation(first_fill=True))
        protection = _action(
            "protection-action",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.OPEN,
            terms={"quantity": "0.01"},
            client_order_id="a" * 32,
            call_started_at=NOW,
        )
        coordinator.actions[protection.execution_action_id] = protection
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(current_abs_position="0.01"),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.called_queries == ["protection-action"]


def test_missing_open_exit_identity_triggers_bounded_read_only_query() -> None:
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(
            enter_exit(_activation(first_fill=True), observed_at=NOW)
        )
        exit_action = _action(
            "exit-action",
            ExecutionActionKind.EXIT,
            state=ExecutionActionState.OPEN,
            terms={
                "action_profile": "REDUCE_OR_CLOSE_MARKET",
                "quantity": "0.01",
            },
            client_order_id="d" * 32,
            call_started_at=NOW,
        )
        coordinator.actions[exit_action.execution_action_id] = exit_action
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(
                    current_abs_position="0",
                    position_fact=_venue_fact(
                        "position-zero",
                        VenueFactKind.POSITION_STATE,
                        position_quantity="0",
                    ),
                ),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.called_queries == ["exit-action"]


def test_risk_reduction_fill_triggers_immediate_framework_event_sync() -> None:
    async def scenario() -> int:
        fact_reads = 0

        async def read_facts(_activation: PlanActivation) -> ProductRiskReductionFacts:
            nonlocal fact_reads
            fact_reads += 1
            return _facts()

        coordinator = _Coordinator(_activation(first_fill=True))
        take_profit = _action(
            "take-profit-action",
            ExecutionActionKind.TAKE_PROFIT,
            state=ExecutionActionState.OPEN,
            terms={"quantity": "0.005"},
            client_order_id="b" * 32,
        )
        coordinator.actions[take_profit.execution_action_id] = take_profit
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=read_facts,
            environment_id="demo-1",
        )
        fill = SimpleNamespace(
            kind=VenueFactKind.FILL,
            content_digest="f" * 64,
            payload={},
        )

        await boundary.sync("activation-1")
        boundary.submit_event(
            NormalizedNautilusEvent(action=take_profit, facts=(fill,))
        )
        await boundary.wait_idle()
        return fact_reads

    assert asyncio.run(scenario()) == 2


def test_entry_fill_creates_and_submits_one_reduce_only_protection() -> None:
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_activation())
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            # The exchange position stream can lag the authoritative fill
            # callback by one reconciliation cycle.
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(current_abs_position="0"),
            ),
            environment_id="demo-1",
        )
        fill = SimpleNamespace(
            kind=VenueFactKind.FILL,
            content_digest="c" * 64,
            action_ref="entry-action",
            activation_ref="activation-1",
            payload={"last_quantity": "0.01"},
            source_class=VenueFactSourceClass.VENUE_STREAM,
            source_object_id="trade-1",
            source_sequence="1",
            venue_fact_id="fill-fact",
            cutoff=NOW,
        )
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.OPEN,
            terms={},
        )

        boundary.submit_event(NormalizedNautilusEvent(action=entry, facts=(fill,)))
        await boundary.wait_idle()
        return coordinator

    coordinator = asyncio.run(scenario())

    assert len(coordinator.protection_checks) == 1
    check = coordinator.protection_checks[0]
    assert check.control_category is StopCategory.PROTECTION
    assert check.current_abs_position == "0.01"
    assert check.would_reverse_position is False
    assert coordinator.submissions == [
        (
            "protection-action",
            {
                "profile": "PROTECTIVE_STOP_REDUCE_ONLY",
                "quantity": "0.01",
                "trigger_price": "97",
            },
        )
    ]


def test_sync_retries_persisted_entry_fill_after_transient_protection_failure() -> None:
    class TransientProtectionCoordinator(_Coordinator):
        def __init__(self, activation: PlanActivation) -> None:
            super().__init__(activation)
            self.protection_attempts = 0

        def create_protection_for_fill(self, **kwargs: object) -> SimpleNamespace:
            self.protection_attempts += 1
            if self.protection_attempts == 1:
                raise RuntimeError("TRANSIENT_PROTECTION_TRANSACTION_FAILURE")
            fill = kwargs["fill_fact"]
            action = _action(
                "protection-replayed",
                ExecutionActionKind.PROTECTION,
                state=ExecutionActionState.READY,
                terms={
                    "action_profile": "PROTECTIVE_STOP_REDUCE_ONLY",
                    "quantity": "0.01",
                    "trigger_price": "97",
                    "execution_context": {
                        "fill_fact_ref": fill.venue_fact_id,
                    },
                },
                client_order_id="a" * 32,
            )
            self.actions[action.execution_action_id] = action
            return SimpleNamespace(execution_action=action)

    async def scenario() -> TransientProtectionCoordinator:
        coordinator = TransientProtectionCoordinator(_activation(first_fill=True))
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.OPEN,
            terms={"quantity": "0.01"},
        )
        coordinator.actions[entry.execution_action_id] = entry
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact(
                "fill-fact",
                VenueFactKind.FILL,
                trade_id="entry-trade",
                last_quantity="0.01",
                action_ref=entry.execution_action_id,
                activation_ref="activation-1",
            ),
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(current_abs_position="0"),
            ),
            environment_id="demo-1",
        )

        with pytest.raises(
            RuntimeError,
            match="TRANSIENT_PROTECTION_TRANSACTION_FAILURE",
        ):
            await boundary.sync("activation-1", force=True)
        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.protection_attempts == 2
    assert coordinator.submissions == [
        (
            "protection-replayed",
            {
                "profile": "PROTECTIVE_STOP_REDUCE_ONLY",
                "quantity": "0.01",
                "trigger_price": "97",
            },
        )
    ]


@pytest.mark.parametrize(
    ("protection_state", "terminal_status"),
    (
        (ExecutionActionState.NOT_SUBMITTED, None),
        (ExecutionActionState.OPEN, "REJECTED"),
        (ExecutionActionState.OPEN, "EXPIRED"),
    ),
)
def test_failed_protection_forms_attributed_market_exit(
    protection_state: ExecutionActionState,
    terminal_status: str | None,
) -> None:
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_activation(first_fill=True))
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        coordinator.actions[entry.execution_action_id] = entry
        fill = _venue_fact(
            "fill-fact",
            VenueFactKind.FILL,
            trade_id="entry-trade",
            last_quantity="0.01",
            action_ref=entry.execution_action_id,
            activation_ref="activation-1",
        )
        coordinator.facts[entry.execution_action_id] = (fill,)
        protection = _action(
            "failed-protection",
            ExecutionActionKind.PROTECTION,
            state=protection_state,
            terms={
                "action_profile": "PROTECTIVE_STOP_REDUCE_ONLY",
                "quantity": "0.01",
                "execution_context": {"fill_fact_ref": fill.venue_fact_id},
            },
            client_order_id="a" * 32,
        )
        coordinator.actions[protection.execution_action_id] = protection
        if terminal_status is not None:
            coordinator.facts[protection.execution_action_id] = (
                _venue_fact(
                    "protection-terminal",
                    VenueFactKind.ORDER_STATE,
                    status=terminal_status,
                    cumulative_filled_quantity="0",
                ),
            )
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.01",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(position_fact=position_fact),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.exit_requests[0]["reason_ref"].endswith("PROTECTION_GAP")
    assert coordinator.submissions[-1] == (
        "exit-action",
        {"profile": "REDUCE_OR_CLOSE_MARKET", "quantity": "0.01"},
    )


@pytest.mark.parametrize(
    ("protection_age", "exit_expected"),
    (
        (PROTECTION_UNKNOWN_EXIT_DELAY - timedelta(seconds=1), False),
        (PROTECTION_UNKNOWN_EXIT_DELAY, True),
    ),
)
def test_unknown_protection_has_a_bounded_exit_window(
    protection_age: timedelta,
    exit_expected: bool,
) -> None:
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_activation(first_fill=True))
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        coordinator.actions[entry.execution_action_id] = entry
        fill = _venue_fact(
            "fill-fact",
            VenueFactKind.FILL,
            trade_id="entry-trade",
            last_quantity="0.01",
            action_ref=entry.execution_action_id,
            activation_ref="activation-1",
        )
        coordinator.facts[entry.execution_action_id] = (fill,)
        protection = _action(
            "unknown-protection",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.UNKNOWN,
            terms={
                "action_profile": "PROTECTIVE_STOP_REDUCE_ONLY",
                "quantity": "0.01",
                "execution_context": {"fill_fact_ref": fill.venue_fact_id},
            },
            client_order_id="a" * 32,
            call_started_at=NOW - protection_age,
        )
        coordinator.actions[protection.execution_action_id] = protection
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.01",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(position_fact=position_fact),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.unknown_queries == [("unknown-protection", NOW)]
    assert bool(coordinator.exit_requests) is exit_expected
    if exit_expected:
        assert coordinator.exit_requests[0]["reason_ref"].endswith(
            "PROTECTION_RESULT_UNKNOWN"
        )
        assert coordinator.submissions[-1] == (
            "exit-action",
            {"profile": "REDUCE_OR_CLOSE_MARKET", "quantity": "0.01"},
        )
    else:
        assert coordinator.submissions == []


def test_unknown_protection_resolved_by_same_sync_query_does_not_force_exit() -> None:
    class ResolvingCoordinator(_Coordinator):
        def query_unknown_action_if_due(
            self,
            action_id: str,
            *,
            observed_at: datetime,
        ) -> bool:
            super().query_unknown_action_if_due(
                action_id,
                observed_at=observed_at,
            )
            self.actions[action_id].state = ExecutionActionState.OPEN
            self.facts[action_id] = (
                _venue_fact(
                    "protection-working",
                    VenueFactKind.ORDER_STATE,
                    status="WORKING",
                ),
            )
            return True

    async def scenario() -> ResolvingCoordinator:
        coordinator = ResolvingCoordinator(_activation(first_fill=True))
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        fill = _venue_fact(
            "fill-fact",
            VenueFactKind.FILL,
            trade_id="entry-trade",
            last_quantity="0.01",
            action_ref=entry.execution_action_id,
            activation_ref="activation-1",
        )
        protection = _action(
            "unknown-protection",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.UNKNOWN,
            terms={
                "action_profile": "PROTECTIVE_STOP_REDUCE_ONLY",
                "quantity": "0.01",
                "execution_context": {"fill_fact_ref": fill.venue_fact_id},
            },
            client_order_id="a" * 32,
            call_started_at=NOW - PROTECTION_UNKNOWN_EXIT_DELAY,
        )
        coordinator.actions[entry.execution_action_id] = entry
        coordinator.actions[protection.execution_action_id] = protection
        coordinator.facts[entry.execution_action_id] = (fill,)
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(
                    position_fact=_venue_fact(
                        "position-current",
                        VenueFactKind.POSITION_STATE,
                        position_quantity="0.01",
                    )
                ),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.unknown_queries == [("unknown-protection", NOW)]
    assert coordinator.exit_requests == []
    assert coordinator.submissions == []


def test_stale_unknown_protection_reduces_proven_position_despite_unknown_entry() -> (
    None
):
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_activation(first_fill=True))
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        fill = _venue_fact(
            "fill-fact",
            VenueFactKind.FILL,
            trade_id="entry-trade",
            last_quantity="0.01",
            action_ref=entry.execution_action_id,
            activation_ref="activation-1",
        )
        protection = _action(
            "unknown-protection",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.UNKNOWN,
            terms={
                "action_profile": "PROTECTIVE_STOP_REDUCE_ONLY",
                "quantity": "0.01",
                "execution_context": {"fill_fact_ref": fill.venue_fact_id},
            },
            client_order_id="a" * 32,
            call_started_at=NOW - PROTECTION_UNKNOWN_EXIT_DELAY,
        )
        unknown_entry = _action(
            "unknown-entry",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.UNKNOWN,
            terms={"quantity": "0.01"},
            client_order_id="b" * 32,
            call_started_at=NOW - PROTECTION_UNKNOWN_EXIT_DELAY,
        )
        coordinator.actions.update(
            {
                entry.execution_action_id: entry,
                protection.execution_action_id: protection,
                unknown_entry.execution_action_id: unknown_entry,
            }
        )
        coordinator.facts[entry.execution_action_id] = (fill,)
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(
                    position_fact=_venue_fact(
                        "position-current",
                        VenueFactKind.POSITION_STATE,
                        position_quantity="0.01",
                    )
                ),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert {action_id for action_id, _observed_at in coordinator.unknown_queries} == {
        "unknown-protection",
        "unknown-entry",
    }
    assert coordinator.exit_requests[0]["position_quantity"] == "0.01"
    assert coordinator.submissions == [
        (
            "exit-action",
            {"profile": "REDUCE_OR_CLOSE_MARKET", "quantity": "0.01"},
        )
    ]


def test_repeated_sync_reuses_stale_unknown_protection_exit() -> None:
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_activation(first_fill=True))
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        fill = _venue_fact(
            "fill-fact",
            VenueFactKind.FILL,
            trade_id="entry-trade",
            last_quantity="0.01",
            action_ref=entry.execution_action_id,
            activation_ref="activation-1",
        )
        protection = _action(
            "unknown-protection",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.UNKNOWN,
            terms={
                "action_profile": "PROTECTIVE_STOP_REDUCE_ONLY",
                "quantity": "0.01",
                "execution_context": {"fill_fact_ref": fill.venue_fact_id},
            },
            client_order_id="a" * 32,
            call_started_at=NOW - PROTECTION_UNKNOWN_EXIT_DELAY,
        )
        coordinator.actions.update(
            {
                entry.execution_action_id: entry,
                protection.execution_action_id: protection,
            }
        )
        coordinator.facts[entry.execution_action_id] = (fill,)

        async def facts_provider(_activation: PlanActivation):
            return _facts(
                position_fact=_venue_fact(
                    "position-current",
                    VenueFactKind.POSITION_STATE,
                    position_quantity="0.01",
                )
            )

        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=facts_provider,
            environment_id="demo-1",
        )
        await boundary.sync("activation-1", force=True)
        restarted_boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=facts_provider,
            environment_id="demo-1",
        )
        await restarted_boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert len(coordinator.exit_requests) == 1
    assert coordinator.exit_requests[0]["reason_ref"].endswith(
        "PROTECTION_RESULT_UNKNOWN"
    )
    assert coordinator.submissions == [
        ("exit-action", {"profile": "REDUCE_OR_CLOSE_MARKET", "quantity": "0.01"})
    ]


def test_unprotectable_fill_callback_immediately_forms_attributed_market_exit() -> None:
    class UnprotectableFillCoordinator(_Coordinator):
        def create_protection_for_fill(self, **kwargs: object) -> SimpleNamespace:
            self.protection_checks.append(kwargs["action_check"])
            self.activation = self.activation.model_copy(
                update={
                    "has_entry_fill": True,
                    "entry_opportunity_consumed": True,
                    "protection_state": ProtectionState.GAP,
                    "state_version": self.activation.state_version + 1,
                }
            )
            return SimpleNamespace(execution_action=None)

    async def scenario() -> UnprotectableFillCoordinator:
        coordinator = UnprotectableFillCoordinator(_activation())
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        fill = _venue_fact(
            "fill-invalid-protection-price",
            VenueFactKind.FILL,
            trade_id="entry-trade",
            last_quantity="0.01",
            action_ref=entry.execution_action_id,
            activation_ref="activation-1",
        )
        coordinator.actions[entry.execution_action_id] = entry
        coordinator.facts[entry.execution_action_id] = (fill,)
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.01",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(position_fact=position_fact),
            ),
            environment_id="demo-1",
        )

        boundary.submit_event(NormalizedNautilusEvent(action=entry, facts=(fill,)))
        await boundary.wait_idle()
        return coordinator

    coordinator = asyncio.run(scenario())
    assert len(coordinator.protection_checks) == 2
    assert coordinator.exit_requests[0]["reason_ref"].endswith("PROTECTION_GAP")
    assert coordinator.submissions == [
        ("exit-action", {"profile": "REDUCE_OR_CLOSE_MARKET", "quantity": "0.01"})
    ]


def test_protection_gap_exit_does_not_wait_for_entry_commission() -> None:
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_activation(first_fill=True))
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.OPEN,
            terms={"quantity": "0.01"},
        )
        fill = _venue_fact(
            "fill-fact",
            VenueFactKind.FILL,
            trade_id="entry-trade",
            last_quantity="0.01",
            action_ref=entry.execution_action_id,
            activation_ref="activation-1",
        )
        coordinator.actions[entry.execution_action_id] = entry
        coordinator.facts[entry.execution_action_id] = (
            fill,
            _venue_fact(
                "entry-filled",
                VenueFactKind.ORDER_STATE,
                status="FILLED",
                cumulative_filled_quantity="0.01",
            ),
        )
        protection = _action(
            "protection-denied",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.NOT_SUBMITTED,
            terms={
                "quantity": "0.01",
                "execution_context": {"fill_fact_ref": fill.venue_fact_id},
            },
        )
        coordinator.actions[protection.execution_action_id] = protection
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.01",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(position_fact=position_fact),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.exit_requests[0]["reason_ref"].endswith("PROTECTION_GAP")
    assert coordinator.closures == []
    assert coordinator.submissions[-1] == (
        "exit-action",
        {"profile": "REDUCE_OR_CLOSE_MARKET", "quantity": "0.01"},
    )


def test_terminal_entry_overfill_does_not_block_attributed_gap_exit() -> None:
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_activation(first_fill=True))
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.OPEN,
            terms={"quantity": "0.01"},
        )
        first_fill = _venue_fact(
            "entry-fill-1",
            VenueFactKind.FILL,
            trade_id="entry-trade-1",
            leaves_quantity="0.01",
            last_quantity="0.01",
            action_ref=entry.execution_action_id,
            activation_ref="activation-1",
        )
        final_fill = _venue_fact(
            "entry-fill-2",
            VenueFactKind.FILL,
            trade_id="entry-trade-2",
            leaves_quantity="0",
            last_quantity="0.01",
            action_ref=entry.execution_action_id,
            activation_ref="activation-1",
            source_sequence="2",
        )
        protection = _action(
            "protection-denied",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.NOT_SUBMITTED,
            terms={
                "quantity": "0.01",
                "execution_context": {
                    "fill_fact_ref": first_fill.venue_fact_id,
                },
            },
        )
        coordinator.actions.update(
            {
                entry.execution_action_id: entry,
                protection.execution_action_id: protection,
            }
        )
        coordinator.facts[entry.execution_action_id] = (
            first_fill,
            final_fill,
        )
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.02",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(
                    current_abs_position="0.02",
                    position_fact=position_fact,
                ),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert len(coordinator.exit_requests) == 1
    assert coordinator.exit_requests[0]["position_quantity"] == "0.02"
    assert coordinator.exit_requests[0]["reason_ref"].endswith("PROTECTION_GAP")
    assert coordinator.submissions[-1] == (
        "exit-action",
        {"profile": "REDUCE_OR_CLOSE_MARKET", "quantity": "0.02"},
    )


@pytest.mark.parametrize("terminal_status", ("CANCELLED", "EXPIRED"))
def test_accounted_terminal_entry_overfill_does_not_block_gap_exit(
    terminal_status: str,
) -> None:
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_activation(first_fill=True))
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.OPEN,
            terms={"quantity": "0.01"},
        )
        fills = (
            _venue_fact(
                "entry-fill-1",
                VenueFactKind.FILL,
                trade_id="entry-trade-1",
                leaves_quantity="0.01",
                last_quantity="0.01",
            ),
            _venue_fact(
                "entry-fill-2",
                VenueFactKind.FILL,
                trade_id="entry-trade-2",
                leaves_quantity="0.01",
                last_quantity="0.01",
                source_sequence="2",
            ),
        )
        terminal = _venue_fact(
            f"entry-{terminal_status.lower()}",
            VenueFactKind.ORDER_STATE,
            status=terminal_status,
            cumulative_filled_quantity="0.02",
            source_sequence="3",
        )
        protection = _action(
            "protection-denied",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.NOT_SUBMITTED,
            terms={
                "quantity": "0.01",
                "execution_context": {
                    "fill_fact_ref": fills[0].venue_fact_id,
                },
            },
        )
        coordinator.actions.update(
            {
                entry.execution_action_id: entry,
                protection.execution_action_id: protection,
            }
        )
        coordinator.facts[entry.execution_action_id] = (*fills, terminal)
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.02",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(
                    current_abs_position="0.02",
                    position_fact=position_fact,
                ),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert len(coordinator.exit_requests) == 1
    assert coordinator.exit_requests[0]["position_quantity"] == "0.02"
    assert coordinator.exit_requests[0]["reason_ref"].endswith("PROTECTION_GAP")


def test_working_protection_quantity_gap_cancels_before_attributed_exit() -> None:
    async def scenario() -> _Coordinator:
        activation = _activation(first_fill=True).model_copy(
            update={"protection_state": ProtectionState.GAP}
        )
        coordinator = _Coordinator(activation)
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        fill = _venue_fact(
            "fill-fact",
            VenueFactKind.FILL,
            trade_id="entry-trade",
            last_quantity="0.01",
            action_ref=entry.execution_action_id,
            activation_ref="activation-1",
        )
        protection = _action(
            "protection-action",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.OPEN,
            terms={
                "quantity": "0.01",
                "execution_context": {
                    "entry_action_ref": entry.execution_action_id,
                    "fill_fact_ref": fill.venue_fact_id,
                    "fill_source_identity": "entry-fill-source",
                },
            },
            client_order_id="a" * 32,
        )
        working = _venue_fact(
            "protection-working-smaller",
            VenueFactKind.ORDER_STATE,
            status="WORKING",
            action_ref=protection.execution_action_id,
            activation_ref="activation-1",
        )
        working.payload["venue_order_quantity"] = "0.005"
        coordinator.actions.update(
            {
                entry.execution_action_id: entry,
                protection.execution_action_id: protection,
            }
        )
        coordinator.facts[entry.execution_action_id] = (fill,)
        coordinator.facts[protection.execution_action_id] = (working,)
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.01",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(position_fact=position_fact),
            ),
            environment_id="demo-1",
        )

        boundary.submit_event(
            NormalizedNautilusEvent(
                action=protection,
                facts=(working,),
            )
        )
        await boundary.wait_idle()
        assert coordinator.exit_requests == []
        coordinator.facts[protection.execution_action_id] = (
            *coordinator.facts[protection.execution_action_id],
            _venue_fact(
                "protection-cancelled",
                VenueFactKind.ORDER_STATE,
                status="CANCELLED",
                action_ref=protection.execution_action_id,
                activation_ref="activation-1",
            ),
        )
        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.take_profit_checks == []
    assert coordinator.cancel_requests[0]["target_action_id"] == ("protection-action")
    assert len(coordinator.exit_requests) == 1
    assert coordinator.exit_requests[0]["position_quantity"] == "0.01"
    assert coordinator.exit_requests[0]["reason_ref"].endswith("PROTECTION_GAP")
    assert coordinator.submissions[-1] == (
        "exit-action",
        {"profile": "REDUCE_OR_CLOSE_MARKET", "quantity": "0.01"},
    )


def test_protection_denied_callback_immediately_forms_exit_without_venue_fact() -> None:
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_activation(first_fill=True))
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        fill = _venue_fact(
            "fill-fact",
            VenueFactKind.FILL,
            trade_id="entry-trade",
            last_quantity="0.01",
            action_ref=entry.execution_action_id,
            activation_ref="activation-1",
        )
        protection = _action(
            "protection-denied",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.NOT_SUBMITTED,
            terms={
                "quantity": "0.01",
                "execution_context": {"fill_fact_ref": fill.venue_fact_id},
            },
        )
        coordinator.actions.update(
            {
                entry.execution_action_id: entry,
                protection.execution_action_id: protection,
            }
        )
        coordinator.facts[entry.execution_action_id] = (fill,)
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.01",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(position_fact=position_fact),
            ),
            environment_id="demo-1",
        )

        boundary.submit_event(
            NormalizedNautilusEvent(
                action=protection,
                facts=(),
                definitely_not_submitted=True,
            )
        )
        await boundary.wait_idle()
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.exit_requests[0]["reason_ref"].endswith("PROTECTION_GAP")
    assert coordinator.submissions == [
        ("exit-action", {"profile": "REDUCE_OR_CLOSE_MARKET", "quantity": "0.01"})
    ]


def test_existing_gap_still_persists_late_fill_responsibility_before_exit() -> None:
    class ExistingGapCoordinator(_Coordinator):
        def __init__(self, activation: PlanActivation) -> None:
            super().__init__(activation)
            self.gap_fill_refs: list[str] = []

        def create_protection_for_fill(self, **kwargs: object) -> SimpleNamespace:
            fill = kwargs["fill_fact"]
            self.gap_fill_refs.append(fill.venue_fact_id)
            return SimpleNamespace(execution_action=None)

    async def scenario() -> ExistingGapCoordinator:
        activation = _activation().model_copy(
            update={"protection_state": ProtectionState.GAP}
        )
        coordinator = ExistingGapCoordinator(activation)
        entry = _action(
            "entry-late-fill",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        late_fill = _venue_fact(
            "late-fill-after-gap",
            VenueFactKind.FILL,
            trade_id="late-entry-trade",
            last_quantity="0.01",
            action_ref=entry.execution_action_id,
            activation_ref="activation-1",
        )
        coordinator.actions[entry.execution_action_id] = entry
        coordinator.facts[entry.execution_action_id] = (late_fill,)
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.01",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(position_fact=position_fact),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.gap_fill_refs == ["late-fill-after-gap"]
    assert coordinator.exit_requests[0]["reason_ref"].endswith("PROTECTION_GAP")


def test_sync_recovers_take_profit_from_persisted_working_protection() -> None:
    class PersistingTakeProfitCoordinator(_Coordinator):
        def create_take_profits_for_protected_fill(
            self,
            **kwargs: object,
        ) -> tuple[SimpleNamespace, SimpleNamespace]:
            existing = tuple(
                action
                for action in self.actions.values()
                if action.action_kind is ExecutionActionKind.TAKE_PROFIT
            )
            if existing:
                return tuple(
                    SimpleNamespace(execution_action=action) for action in existing
                )  # type: ignore[return-value]
            results = super().create_take_profits_for_protected_fill(**kwargs)
            for result in results:
                action = result.execution_action
                self.actions[action.execution_action_id] = action
            return results

    async def scenario() -> PersistingTakeProfitCoordinator:
        coordinator = PersistingTakeProfitCoordinator(_activation(first_fill=True))
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        fill = _venue_fact(
            "fill-fact",
            VenueFactKind.FILL,
            trade_id="entry-trade",
            last_quantity="0.01",
            action_ref=entry.execution_action_id,
            activation_ref="activation-1",
        )
        protection = _action(
            "protection-working",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.OPEN,
            terms={
                "quantity": "0.01",
                "execution_context": {
                    "entry_action_ref": entry.execution_action_id,
                    "fill_fact_ref": fill.venue_fact_id,
                    "fill_source_identity": "entry-trade:1",
                },
            },
        )
        coordinator.actions.update(
            {
                entry.execution_action_id: entry,
                protection.execution_action_id: protection,
            }
        )
        coordinator.facts[entry.execution_action_id] = (fill,)
        coordinator.facts[protection.execution_action_id] = (
            _venue_fact(
                "protection-working-fact",
                VenueFactKind.ORDER_STATE,
                status="WORKING",
            ),
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(0, result=_facts()),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert [action_id for action_id, _payload in coordinator.submissions] == [
        "take-profit-1",
        "take-profit-2",
    ]
    assert len(coordinator.take_profit_checks) == 2
    assert all(
        check.current_abs_position == "0.01"
        and check.would_reverse_position is False
        for check in coordinator.take_profit_checks
    )


def test_take_profit_immediate_trigger_rejection_uses_one_market_reduction() -> None:
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_activation(first_fill=True))
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.02"},
        )
        entry_fill = _venue_fact(
            "entry-fill",
            VenueFactKind.FILL,
            trade_id="entry-trade",
            last_quantity="0.02",
            action_ref=entry.execution_action_id,
            activation_ref="activation-1",
        )
        protection = _action(
            "protection-working",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.OPEN,
            terms={
                "action_profile": "PROTECTIVE_STOP_REDUCE_ONLY",
                "quantity": "0.02",
                "execution_context": {
                    "fill_fact_ref": entry_fill.venue_fact_id,
                    "protection_replacement": {"step_index": 0},
                },
            },
            client_order_id="p" * 32,
        )
        rejected_take_profit = _action(
            "take-profit-crossed",
            ExecutionActionKind.TAKE_PROFIT,
            state=ExecutionActionState.OPEN,
            terms={
                "action_profile": "TAKE_PROFIT_1",
                "quantity": "0.01",
                "execution_context": {
                    "venue_policy": {
                        "order_type": "MARKET_IF_TOUCHED",
                        "post_only": False,
                    }
                },
            },
            client_order_id="t" * 32,
            call_started_at=NOW - timedelta(seconds=1),
        )
        rejection = _venue_fact(
            "take-profit-rejected",
            VenueFactKind.ORDER_STATE,
            status="REJECTED",
            cumulative_filled_quantity="0",
            action_ref=rejected_take_profit.execution_action_id,
        )
        rejection.payload["reason"] = (
            "{'code': -2021, 'msg': 'Order would immediately trigger.'}"
        )
        coordinator.actions = {
            action.execution_action_id: action
            for action in (entry, protection, rejected_take_profit)
        }
        coordinator.facts = {
            entry.execution_action_id: (entry_fill,),
            protection.execution_action_id: (
                _venue_fact(
                    "protection-working-fact",
                    VenueFactKind.ORDER_STATE,
                    status="WORKING",
                    action_ref=protection.execution_action_id,
                ),
            ),
            rejected_take_profit.execution_action_id: (rejection,),
        }
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.02",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(
                    current_abs_position="0.02",
                    position_fact=position_fact,
                    open_algo_client_ids=(protection.client_order_id,),
                ),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert len(coordinator.risk_reduction_requests) == 1
    request = coordinator.risk_reduction_requests[0]
    assert request["rejected_take_profit_action_id"] == "take-profit-crossed"
    assert request["rejection_fact_ref"] == "take-profit-rejected"
    assert request["position_quantity"] == "0.01"
    assert coordinator.submissions[-1][1] == {
        "profile": "REDUCE_OR_CLOSE_MARKET",
        "quantity": "0.01",
    }
    check = coordinator.risk_reduction_checks[0]
    assert check.current_abs_position == "0.02"
    assert check.post_action_abs_position == "0.01"
    assert check.would_reverse_position is False
    assert "take-profit-crossed" not in coordinator.called_queries


def test_direct_time_exit_survives_account_new_risk_stop_when_attribution_is_proven() -> (
    None
):
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_direct_activation_with_time_exit())
        coordinator.has_external_activity_conflict = True
        coordinator.actions["entry-action"] = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        coordinator.facts["entry-action"] = (
            _venue_fact(
                "entry-fill",
                VenueFactKind.FILL,
                trade_id="entry-trade",
                last_quantity="0.01",
            ),
        )
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.01",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(position_fact=position_fact),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert len(coordinator.exit_requests) == 1
    assert coordinator.exit_requests[0]["position_quantity"] == "0.01"
    assert coordinator.submissions == [
        ("exit-action", {"profile": "REDUCE_OR_CLOSE_MARKET", "quantity": "0.01"})
    ]


def test_direct_time_exit_deadline_bypasses_throttled_fallback_once() -> None:
    async def scenario() -> tuple[_Coordinator, int, int, int]:
        coordinator = _Coordinator(_direct_activation_with_time_exit())
        coordinator.actions["entry-action"] = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        coordinator.facts["entry-action"] = (
            _venue_fact(
                "entry-fill",
                VenueFactKind.FILL,
                trade_id="entry-trade",
                last_quantity="0.01",
            ),
        )
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.01",
        )
        fact_queries = 0

        async def facts(_activation: PlanActivation) -> ProductRiskReductionFacts:
            nonlocal fact_queries
            fact_queries += 1
            return _facts(position_fact=position_fact)

        loop = asyncio.get_running_loop()
        boundary = ProductResponsibilityBoundary(
            loop=loop,
            coordinator=coordinator,
            fact_provider=facts,
            environment_id="demo-1",
        )
        boundary._last_fallback_sync["activation-1"] = loop.time()

        await boundary.sync("activation-1")
        assert fact_queries == 0
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await boundary.wait_idle()
        handles_before_close = len(boundary._direct_time_exit_handles)
        boundary.close()
        wake_state_after_close = len(boundary._direct_time_exit_woken)
        return (
            coordinator,
            fact_queries,
            handles_before_close,
            wake_state_after_close,
        )

    coordinator, fact_queries, handles, wake_state = asyncio.run(scenario())

    assert fact_queries == 1
    assert len(coordinator.exit_requests) == 1
    assert coordinator.exit_requests[0]["reason_ref"].endswith("DIRECT_TIME_EXIT")
    assert handles == 0
    assert wake_state == 0


def test_direct_time_exit_keeps_causation_during_reducer_handoff_gap() -> None:
    async def scenario() -> _Coordinator:
        activation = _direct_activation_with_time_exit().model_copy(
            update={"protection_state": ProtectionState.GAP}
        )
        coordinator = _Coordinator(activation)
        coordinator.actions["entry-action"] = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        coordinator.facts["entry-action"] = (
            _venue_fact(
                "entry-fill",
                VenueFactKind.FILL,
                trade_id="entry-trade",
                last_quantity="0.01",
            ),
        )
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.01",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(position_fact=position_fact),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert len(coordinator.exit_requests) == 1
    assert coordinator.exit_requests[0]["reason_ref"].endswith("DIRECT_TIME_EXIT")


def test_direct_time_exit_cancels_open_entry_and_reduces_proven_fill() -> None:
    async def scenario() -> tuple[_Coordinator, int]:
        coordinator = _Coordinator(_direct_activation_with_time_exit())
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.OPEN,
            terms={"quantity": "0.02"},
            client_order_id="b" * 32,
        )
        coordinator.actions[entry.execution_action_id] = entry
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact("entry-working", VenueFactKind.ORDER_STATE, status="WORKING"),
            _venue_fact(
                "entry-fill",
                VenueFactKind.FILL,
                trade_id="entry-trade",
                leaves_quantity="0.01",
                last_quantity="0.01",
            ),
            _venue_fact(
                "entry-commission",
                VenueFactKind.COMMISSION,
                trade_id="entry-trade",
            ),
        )
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.01",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(position_fact=position_fact),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        submissions_after_first_sync = len(coordinator.submissions)
        assert coordinator.exit_requests[0]["position_quantity"] == "0.01"
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact(
                "entry-fill",
                VenueFactKind.FILL,
                trade_id="entry-trade",
                leaves_quantity="0.01",
                last_quantity="0.01",
            ),
            _venue_fact(
                "entry-commission",
                VenueFactKind.COMMISSION,
                trade_id="entry-trade",
            ),
            _venue_fact(
                "entry-cancelled",
                VenueFactKind.ORDER_STATE,
                status="CANCELLED",
                cumulative_filled_quantity="0.01",
            ),
        )
        await boundary.sync("activation-1", force=True)
        return coordinator, submissions_after_first_sync

    coordinator, submissions_after_first_sync = asyncio.run(scenario())
    assert submissions_after_first_sync == 2
    assert coordinator.cancel_requests[0]["target_endpoint"] == "ORDINARY"
    assert len(coordinator.exit_requests) == 1
    assert coordinator.submissions[-1] == (
        "exit-action",
        {"profile": "REDUCE_OR_CLOSE_MARKET", "quantity": "0.01"},
    )


def test_direct_time_exit_does_not_retry_unknown_cancel_without_query_fact() -> None:
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_direct_activation_with_time_exit())
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.OPEN,
            terms={"quantity": "0.02"},
            client_order_id="b" * 32,
        )
        entry.state_version = 7
        coordinator.actions[entry.execution_action_id] = entry
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact("entry-working", VenueFactKind.ORDER_STATE, status="WORKING"),
        )
        existing_cancel = _action(
            "entry-cancel-unknown",
            ExecutionActionKind.CANCEL,
            state=ExecutionActionState.UNKNOWN,
            terms={"action_profile": "CANCEL_ORDER"},
            cancel_target={
                "client_order_id": entry.client_order_id,
                "endpoint": "ORDINARY",
            },
            call_started_at=NOW - UNKNOWN_RISK_CONTROL_SUCCESSOR_DELAY,
        )
        coordinator.actions[existing_cancel.execution_action_id] = existing_cancel
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(
                    current_abs_position="0",
                    position_fact=_venue_fact(
                        "position-current",
                        VenueFactKind.POSITION_STATE,
                        position_quantity="0",
                    ),
                ),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.cancel_requests == []
    assert coordinator.unknown_queries == [("entry-cancel-unknown", NOW)]
    assert coordinator.exit_requests == []


def test_direct_time_exit_forms_one_cancel_successor_after_post_call_query() -> None:
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_direct_activation_with_time_exit())
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.OPEN,
            terms={"quantity": "0.02"},
            client_order_id="b" * 32,
        )
        coordinator.actions[entry.execution_action_id] = entry
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact(
                "entry-working-query",
                VenueFactKind.ORDER_STATE,
                status="WORKING",
                source_class=VenueFactSourceClass.VENUE_QUERY,
            ),
        )
        original_cancel = _action(
            "entry-cancel-unknown",
            ExecutionActionKind.CANCEL,
            state=ExecutionActionState.UNKNOWN,
            terms={"action_profile": "CANCEL_ORDER"},
            cancel_target={
                "client_order_id": entry.client_order_id,
                "endpoint": "ORDINARY",
            },
            call_started_at=NOW - UNKNOWN_RISK_CONTROL_SUCCESSOR_DELAY,
        )
        coordinator.actions[original_cancel.execution_action_id] = original_cancel
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(
                    current_abs_position="0",
                    position_fact=_venue_fact(
                        "position-current",
                        VenueFactKind.POSITION_STATE,
                        position_quantity="0",
                    ),
                ),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert len(coordinator.cancel_requests) == 1
    assert (
        "CANCEL_SUCCESSOR:entry-cancel-unknown"
        in coordinator.cancel_requests[0]["reason_ref"]
    )
    assert coordinator.cancel_requests[0]["target_action_id"] == "entry-action"
    assert coordinator.submissions == [("cancel-action", {"profile": "CANCEL_ORDER"})]
    assert coordinator.unknown_queries == [
        ("entry-cancel-unknown", NOW),
        ("entry-cancel-unknown", NOW),
    ]


def test_restart_rejects_ready_cancel_when_target_is_already_terminal() -> None:
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_activation())
        target = _action(
            "entry-terminal",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.OPEN,
            terms={"quantity": "0.01"},
            client_order_id="b" * 32,
        )
        cancel = _action(
            "cancel-ready",
            ExecutionActionKind.CANCEL,
            state=ExecutionActionState.READY,
            terms={"action_profile": "CANCEL_ORDER"},
            cancel_target={
                "client_order_id": target.client_order_id,
                "endpoint": "ORDINARY",
            },
        )
        coordinator.actions.update(
            {
                target.execution_action_id: target,
                cancel.execution_action_id: cancel,
            }
        )
        coordinator.facts[target.execution_action_id] = (
            _venue_fact(
                "entry-cancelled",
                VenueFactKind.ORDER_STATE,
                status="CANCELLED",
                cumulative_filled_quantity="0",
            ),
        )
        position_fact = _venue_fact(
            "position-zero",
            VenueFactKind.POSITION_STATE,
            position_quantity="0",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(
                    current_abs_position="0",
                    position_fact=position_fact,
                ),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.rejections == [
        ("cancel-ready", "CANCEL_TARGET_ALREADY_TERMINAL")
    ]
    assert coordinator.submissions == []


def test_startup_recovery_does_not_defer_ready_protection() -> None:
    async def scenario() -> tuple[_Coordinator, int]:
        coordinator = _Coordinator(_activation(first_fill=True))
        protection = _action(
            "protection-ready",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.READY,
            terms={
                "action_profile": "PROTECTIVE_STOP_REDUCE_ONLY",
                "quantity": "0.01",
                "trigger_price": "97",
            },
        )
        coordinator.actions[protection.execution_action_id] = protection
        fact_reads = 0

        async def read_facts(_activation: PlanActivation) -> ProductRiskReductionFacts:
            nonlocal fact_reads
            fact_reads += 1
            return _facts()

        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=read_facts,
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator, fact_reads

    coordinator, fact_reads = asyncio.run(scenario())
    assert fact_reads == 1
    assert coordinator.submissions == [
        (
            "protection-ready",
            {
                "profile": "PROTECTIVE_STOP_REDUCE_ONLY",
                "quantity": "0.01",
                "trigger_price": "97",
            },
        )
    ]


def test_restart_resubmits_ready_protection_that_was_proven_never_called() -> None:
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_activation(first_fill=True))
        protection = _action(
            "protection-ready",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.READY,
            terms={
                "action_profile": "PROTECTIVE_STOP_REDUCE_ONLY",
                "quantity": "0.01",
                "trigger_price": "97",
            },
            client_order_id="a" * 32,
        )
        coordinator.actions[protection.execution_action_id] = protection
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(0, result=_facts()),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.submissions == [
        (
            "protection-ready",
            {
                "profile": "PROTECTIVE_STOP_REDUCE_ONLY",
                "quantity": "0.01",
                "trigger_price": "97",
            },
        )
    ]
    assert (
        coordinator.actions["protection-ready"].state is ExecutionActionState.SUBMITTING
    )


def test_sync_queries_unknown_action_by_original_identity() -> None:
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_activation())
        unknown = _action(
            "entry-action-unknown",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.UNKNOWN,
            terms={},
            client_order_id="0123456789abcdef0123456789abcdef",
        )
        coordinator.actions[unknown.execution_action_id] = unknown
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(0, result=_facts()),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1")
        await boundary.sync("activation-1")
        return coordinator

    coordinator = asyncio.run(scenario())

    assert coordinator.unknown_queries == [("entry-action-unknown", NOW)]


def test_unknown_cancel_uses_framework_original_identity_query() -> None:
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_activation(first_fill=True))
        target = _action(
            "protection-action",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.OPEN,
            terms={"quantity": "0.01"},
            client_order_id="a" * 32,
        )
        cancel = _action(
            "cancel-action-unknown",
            ExecutionActionKind.CANCEL,
            state=ExecutionActionState.UNKNOWN,
            terms={"action_profile": "CANCEL_ORDER"},
            cancel_target={"client_order_id": "a" * 32, "endpoint": "ALGO"},
        )
        coordinator.actions[target.execution_action_id] = target
        coordinator.facts[target.execution_action_id] = (
            _venue_fact(
                "protection-working", VenueFactKind.ORDER_STATE, status="WORKING"
            ),
        )
        coordinator.actions[cancel.execution_action_id] = cancel
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(0, result=_facts()),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1")
        return coordinator

    coordinator = asyncio.run(scenario())

    assert coordinator.unknown_queries == [("cancel-action-unknown", NOW)]
    assert coordinator.applied_facts == []
    assert coordinator.actions["protection-action"].state is ExecutionActionState.OPEN


def test_working_protection_creates_and_submits_two_fixed_take_profits() -> None:
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_activation(first_fill=True))
        protection = _action(
            "protection-action",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.OPEN,
            terms={
                "quantity": "0.01",
                "execution_context": {
                    "entry_action_ref": "entry-action",
                    "fill_fact_ref": "fill-fact",
                    "fill_source_identity": "trade-1:1",
                },
            },
        )
        coordinator.actions[protection.execution_action_id] = protection
        coordinator.facts[protection.execution_action_id] = (
            _venue_fact(
                "protection-working", VenueFactKind.ORDER_STATE, status="WORKING"
            ),
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(0, result=_facts()),
            environment_id="demo-1",
        )
        working = SimpleNamespace(
            kind=VenueFactKind.ORDER_STATE,
            payload={"status": "WORKING"},
        )

        boundary.submit_event(
            NormalizedNautilusEvent(action=protection, facts=(working,))
        )
        await boundary.wait_idle()
        return coordinator

    coordinator = asyncio.run(scenario())

    assert [check.quantized_quantity for check in coordinator.take_profit_checks] == [
        "0.005",
        "0.005",
    ]
    assert [item[0] for item in coordinator.submissions] == [
        "take-profit-1",
        "take-profit-2",
    ]
    assert [item[1]["trigger_price"] for item in coordinator.submissions] == [
        "104.5",
        "109",
    ]


def test_exiting_activation_uses_only_its_virtual_share_of_merged_position() -> None:
    async def scenario() -> _Coordinator:
        activation = enter_exit(_activation(first_fill=True), observed_at=NOW)
        coordinator = _Coordinator(activation)
        coordinator.actions["entry-action"] = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        coordinator.facts["entry-action"] = (
            _venue_fact(
                "entry-fill",
                VenueFactKind.FILL,
                trade_id="entry-trade",
                last_quantity="0.01",
            ),
        )
        position_fact = _venue_fact(
            "position-fact",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.03",
        )
        position_fact.payload.update(
            {
                "activation_position_quantity": "0.01",
                "attributed_account_position_quantity": "0.03",
            }
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(position_fact=position_fact),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1")
        return coordinator

    coordinator = asyncio.run(scenario())

    assert len(coordinator.exit_checks) == 1
    assert coordinator.exit_checks[0].risk_class.value == "RISK_REDUCING"
    assert coordinator.exit_requests[0]["exit_responsibility_role"] is (
        ExitResponsibilityRole.PRIMARY_EXIT
    )
    assert coordinator.exit_requests[0]["position_quantity"] == "0.01"
    assert coordinator.submissions == [
        (
            "exit-action",
            {"profile": "REDUCE_OR_CLOSE_MARKET", "quantity": "0.01"},
        )
    ]


def test_exiting_activation_cancels_own_protection_before_market_exit() -> None:
    async def scenario() -> _Coordinator:
        activation = enter_exit(_activation(first_fill=True), observed_at=NOW)
        coordinator = _Coordinator(activation)
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        protection = _action(
            "protection-action",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.OPEN,
            terms={"quantity": "0.01"},
            client_order_id="a" * 32,
        )
        coordinator.actions.update(
            {
                entry.execution_action_id: entry,
                protection.execution_action_id: protection,
            }
        )
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact(
                "entry-fill",
                VenueFactKind.FILL,
                trade_id="entry-trade",
                last_quantity="0.01",
            ),
        )
        coordinator.facts[protection.execution_action_id] = (
            _venue_fact(
                "protection-working",
                VenueFactKind.ORDER_STATE,
                status="WORKING",
            ),
        )
        position_fact = _venue_fact(
            "position-fact",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.01",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(position_fact=position_fact),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        assert coordinator.exit_requests == []
        coordinator.facts[protection.execution_action_id] = (
            *coordinator.facts[protection.execution_action_id],
            _venue_fact(
                "protection-cancelled",
                VenueFactKind.ORDER_STATE,
                status="CANCELLED",
            ),
        )
        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())

    assert [request["target_action_id"] for request in coordinator.cancel_requests] == [
        "protection-action"
    ]
    assert coordinator.exit_requests[0]["position_quantity"] == "0.01"
    assert [action_id for action_id, _payload in coordinator.submissions] == [
        "cancel-action",
        "exit-action",
    ]


def test_rejected_exit_recheck_uses_new_event_identity_but_one_action_identity() -> (
    None
):
    class RejectingExitCoordinator(_Coordinator):
        def create_position_exit(self, **kwargs: object) -> SimpleNamespace:
            self.exit_checks.append(kwargs["action_check"])
            self.exit_requests.append(dict(kwargs))
            return SimpleNamespace(execution_action=None)

    async def scenario() -> RejectingExitCoordinator:
        activation = enter_exit(_activation(first_fill=True), observed_at=NOW)
        coordinator = RejectingExitCoordinator(activation)
        coordinator.actions["entry-action"] = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        coordinator.facts["entry-action"] = (
            _venue_fact(
                "entry-fill",
                VenueFactKind.FILL,
                trade_id="entry-trade",
                last_quantity="0.01",
            ),
        )
        facts = iter(
            _facts(
                position_fact=_venue_fact(
                    f"position-fact-{index}",
                    VenueFactKind.POSITION_STATE,
                    position_quantity="0.01",
                )
            )
            for index in (1, 2)
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(0, result=next(facts)),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1")
        await boundary.sync("activation-1")
        return coordinator

    coordinator = asyncio.run(scenario())

    first, second = coordinator.exit_requests
    assert first["plan_event_id"] != second["plan_event_id"]
    assert first["execution_action_id"] == second["execution_action_id"]
    assert first["client_order_id"] == second["client_order_id"]


@pytest.mark.parametrize(
    "predecessor_state",
    (ExecutionActionState.NOT_SUBMITTED, ExecutionActionState.CLOSED),
)
def test_residual_position_forms_one_successor_after_resolved_exit(
    predecessor_state: ExecutionActionState,
) -> None:
    async def scenario() -> _SuccessorCoordinator:
        activation = enter_exit(_activation(first_fill=True), observed_at=NOW)
        coordinator = _SuccessorCoordinator(activation)
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        predecessor = _action(
            "exit-predecessor",
            ExecutionActionKind.EXIT,
            state=predecessor_state,
            terms={
                "action_profile": "REDUCE_OR_CLOSE_MARKET",
                "quantity": "0.01",
            },
            client_order_id="d" * 32,
        )
        predecessor.source_identity = "activation-1:EXIT:PLAN_EXIT"
        coordinator.actions.update(
            {
                entry.execution_action_id: entry,
                predecessor.execution_action_id: predecessor,
            }
        )
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact(
                "entry-fill",
                VenueFactKind.FILL,
                trade_id="entry-trade",
                last_quantity="0.01",
            ),
        )
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.01",
            source_class=VenueFactSourceClass.VENUE_QUERY,
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(position_fact=position_fact),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert len(coordinator.exit_requests) == 1
    request = coordinator.exit_requests[0]
    assert "EXIT_SUCCESSOR:exit-predecessor" in request["reason_ref"]
    assert request["exit_responsibility_role"] is (
        ExitResponsibilityRole.EXIT_SUCCESSOR
    )
    assert request["position_quantity"] == "0.01"
    assert request["execution_action_id"] != "exit-predecessor"


@pytest.mark.parametrize(
    (
        "elapsed",
        "predecessor_state",
        "position_source",
        "position_received_at",
        "fact_position",
        "expected_successors",
        "expected_error",
    ),
    (
        (
            UNKNOWN_RISK_CONTROL_SUCCESSOR_DELAY - timedelta(seconds=1),
            ExecutionActionState.UNKNOWN,
            VenueFactSourceClass.VENUE_QUERY,
            NOW,
            "0.01",
            0,
            None,
        ),
        (
            -timedelta(seconds=1),
            ExecutionActionState.OPEN,
            VenueFactSourceClass.VENUE_QUERY,
            NOW,
            "0.01",
            0,
            None,
        ),
        (
            UNKNOWN_RISK_CONTROL_SUCCESSOR_DELAY,
            ExecutionActionState.UNKNOWN,
            VenueFactSourceClass.VENUE_QUERY,
            NOW,
            "0.01",
            1,
            None,
        ),
        (
            UNKNOWN_RISK_CONTROL_SUCCESSOR_DELAY,
            ExecutionActionState.UNKNOWN,
            VenueFactSourceClass.VENUE_STREAM,
            NOW,
            "0.01",
            0,
            "POSITION_ATTRIBUTION_UNKNOWN",
        ),
        (
            UNKNOWN_RISK_CONTROL_SUCCESSOR_DELAY,
            ExecutionActionState.UNKNOWN,
            VenueFactSourceClass.VENUE_QUERY,
            NOW - UNKNOWN_RISK_CONTROL_SUCCESSOR_DELAY - timedelta(seconds=1),
            "0.01",
            0,
            "POSITION_ATTRIBUTION_UNKNOWN",
        ),
        (
            UNKNOWN_RISK_CONTROL_SUCCESSOR_DELAY,
            ExecutionActionState.SUBMITTING,
            VenueFactSourceClass.VENUE_QUERY,
            NOW,
            "0.01",
            1,
            None,
        ),
        (
            UNKNOWN_RISK_CONTROL_SUCCESSOR_DELAY,
            ExecutionActionState.OPEN,
            VenueFactSourceClass.VENUE_QUERY,
            NOW,
            "0.01",
            1,
            None,
        ),
    ),
)
def test_unknown_exit_forms_at_most_one_successor_for_proven_residual(
    elapsed: timedelta,
    predecessor_state: ExecutionActionState,
    position_source: VenueFactSourceClass,
    position_received_at: datetime,
    fact_position: str,
    expected_successors: int,
    expected_error: str | None,
) -> None:
    async def scenario() -> _SuccessorCoordinator:
        activation = enter_exit(_activation(first_fill=True), observed_at=NOW)
        coordinator = _SuccessorCoordinator(activation)
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.02"},
        )
        predecessor = _action(
            "exit-unknown",
            ExecutionActionKind.EXIT,
            state=predecessor_state,
            terms={
                "action_profile": "REDUCE_OR_CLOSE_MARKET",
                "quantity": "0.01",
            },
            client_order_id="d" * 32,
            call_started_at=NOW - elapsed,
        )
        predecessor.source_identity = "activation-1:EXIT:PLAN_EXIT"
        coordinator.actions.update(
            {
                entry.execution_action_id: entry,
                predecessor.execution_action_id: predecessor,
            }
        )
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact(
                "entry-fill",
                VenueFactKind.FILL,
                trade_id="entry-trade",
                last_quantity="0.02",
            ),
        )
        coordinator.facts[predecessor.execution_action_id] = (
            _venue_fact(
                "exit-partial-fill",
                VenueFactKind.FILL,
                trade_id="exit-trade",
                last_quantity="0.01",
                leaves_quantity="0.01",
            ),
            _venue_fact(
                "exit-partial-commission",
                VenueFactKind.COMMISSION,
                trade_id="exit-trade",
            ),
        )
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity=fact_position,
            source_class=position_source,
            received_at=position_received_at,
            cutoff=position_received_at,
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(position_fact=position_fact),
            ),
            environment_id="demo-1",
        )

        if expected_error is None:
            await boundary.sync("activation-1", force=True)
            await boundary.sync("activation-1", force=True)
        else:
            with pytest.raises(ValueError, match=expected_error):
                await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert len(coordinator.exit_requests) == expected_successors
    if expected_successors:
        assert coordinator.called_queries == ["exit-unknown"]
        assert (
            "EXIT_SUCCESSOR:exit-unknown" in coordinator.exit_requests[0]["reason_ref"]
        )
        assert coordinator.exit_requests[0]["position_quantity"] == "0.01"


def test_terminal_partial_exit_forms_successor_for_exact_residual() -> None:
    async def scenario() -> _SuccessorCoordinator:
        activation = enter_exit(_activation(first_fill=True), observed_at=NOW)
        coordinator = _SuccessorCoordinator(activation)
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.02"},
        )
        predecessor = _action(
            "exit-partial",
            ExecutionActionKind.EXIT,
            state=ExecutionActionState.OPEN,
            terms={
                "action_profile": "REDUCE_OR_CLOSE_MARKET",
                "quantity": "0.02",
            },
            client_order_id="d" * 32,
        )
        predecessor.source_identity = "activation-1:EXIT:PLAN_EXIT"
        coordinator.actions.update(
            {
                entry.execution_action_id: entry,
                predecessor.execution_action_id: predecessor,
            }
        )
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact(
                "entry-fill",
                VenueFactKind.FILL,
                trade_id="entry-trade",
                last_quantity="0.02",
            ),
        )
        coordinator.facts[predecessor.execution_action_id] = (
            _venue_fact(
                "exit-fill",
                VenueFactKind.FILL,
                trade_id="exit-trade",
                last_quantity="0.01",
                leaves_quantity="0.01",
            ),
            _venue_fact(
                "exit-commission",
                VenueFactKind.COMMISSION,
                trade_id="exit-trade",
            ),
            _venue_fact(
                "exit-cancelled",
                VenueFactKind.ORDER_STATE,
                status="CANCELLED",
                cumulative_filled_quantity="0.01",
            ),
        )
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.01",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(position_fact=position_fact),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert len(coordinator.exit_requests) == 1
    assert coordinator.exit_requests[0]["position_quantity"] == "0.01"
    assert "EXIT_SUCCESSOR:exit-partial" in coordinator.exit_requests[0]["reason_ref"]


def test_partial_bounded_exit_successor_does_not_form_a_chain() -> None:
    async def scenario() -> _SuccessorCoordinator:
        activation = enter_exit(_activation(first_fill=True), observed_at=NOW)
        coordinator = _SuccessorCoordinator(activation)
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.02"},
        )
        successor = _action(
            "exit-bounded-successor",
            ExecutionActionKind.EXIT,
            state=ExecutionActionState.CLOSED,
            terms={
                "action_profile": "REDUCE_OR_CLOSE_MARKET",
                "quantity": "0.02",
            },
            client_order_id="d" * 32,
        )
        successor.action_terms["exit_responsibility_role"] = (
            ExitResponsibilityRole.EXIT_SUCCESSOR.value
        )
        successor.source_identity = "opaque-successor-identity"
        coordinator.actions.update(
            {
                entry.execution_action_id: entry,
                successor.execution_action_id: successor,
            }
        )
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact(
                "entry-fill",
                VenueFactKind.FILL,
                trade_id="entry-trade",
                last_quantity="0.02",
            ),
        )
        coordinator.facts[successor.execution_action_id] = (
            _venue_fact(
                "successor-partial-fill",
                VenueFactKind.FILL,
                trade_id="successor-trade",
                last_quantity="0.01",
                leaves_quantity="0.01",
            ),
            _venue_fact(
                "successor-commission",
                VenueFactKind.COMMISSION,
                trade_id="successor-trade",
            ),
        )
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.01",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(position_fact=position_fact),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.exit_requests == []


def test_uncalled_stale_exit_successor_is_replaced_without_a_second_venue_call() -> (
    None
):
    async def scenario() -> _SuccessorCoordinator:
        activation = enter_exit(_activation(first_fill=True), observed_at=NOW)
        coordinator = _SuccessorCoordinator(activation)
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.02"},
        )
        predecessor = _action(
            "exit-unknown",
            ExecutionActionKind.EXIT,
            state=ExecutionActionState.UNKNOWN,
            terms={
                "action_profile": "REDUCE_OR_CLOSE_MARKET",
                "quantity": "0.01",
            },
            client_order_id="d" * 32,
            call_started_at=NOW - UNKNOWN_RISK_CONTROL_SUCCESSOR_DELAY,
        )
        stale_successor = _action(
            "exit-successor-ready",
            ExecutionActionKind.EXIT,
            state=ExecutionActionState.READY,
            terms={
                "action_profile": "REDUCE_OR_CLOSE_MARKET",
                "quantity": "0.02",
            },
            client_order_id="e" * 32,
        )
        stale_successor.action_terms["exit_responsibility_role"] = (
            ExitResponsibilityRole.EXIT_SUCCESSOR.value
        )
        stale_successor.source_identity = "opaque-stale-successor-identity"
        coordinator.actions.update(
            {
                entry.execution_action_id: entry,
                predecessor.execution_action_id: predecessor,
                stale_successor.execution_action_id: stale_successor,
            }
        )
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact(
                "entry-fill",
                VenueFactKind.FILL,
                trade_id="entry-trade",
                last_quantity="0.02",
            ),
        )
        coordinator.facts[predecessor.execution_action_id] = (
            _venue_fact(
                "exit-fill",
                VenueFactKind.FILL,
                trade_id="exit-trade",
                last_quantity="0.01",
                leaves_quantity="0.01",
            ),
            _venue_fact(
                "exit-commission",
                VenueFactKind.COMMISSION,
                trade_id="exit-trade",
            ),
        )
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.01",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(position_fact=position_fact),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.rejections == [
        ("exit-successor-ready", "EXIT_POSITION_CHANGED_BEFORE_SUBMISSION")
    ]
    assert coordinator.called_queries == ["exit-unknown"]
    assert len(coordinator.exit_requests) == 1
    request = coordinator.exit_requests[0]
    assert request["position_quantity"] == "0.01"
    assert "EXIT_SUCCESSOR:exit-unknown:LOCAL_REPLACEMENT:" in request["reason_ref"]
    assert [payload["quantity"] for _, payload in coordinator.submissions] == ["0.01"]


def test_closed_successor_claims_post_snapshot_entry_fills_once_and_only_to_budget() -> (
    None
):
    async def scenario() -> _SuccessorCoordinator:
        activation = enter_exit(_activation(first_fill=True), observed_at=NOW)
        coordinator = _SuccessorCoordinator(activation)
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.023"},
        )
        successor = _action(
            "exit-bounded-successor",
            ExecutionActionKind.EXIT,
            state=ExecutionActionState.CLOSED,
            terms={
                "action_profile": "REDUCE_OR_CLOSE_MARKET",
                "quantity": "0.013",
            },
            client_order_id="d" * 32,
            call_started_at=NOW - timedelta(seconds=20),
            created_at=NOW - timedelta(seconds=20),
        )
        successor.action_terms["exit_responsibility_role"] = (
            ExitResponsibilityRole.EXIT_SUCCESSOR.value
        )
        successor.source_identity = "opaque-closed-successor-identity"
        coordinator.actions.update(
            {
                entry.execution_action_id: entry,
                successor.execution_action_id: successor,
            }
        )
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact(
                "entry-fill-baseline",
                VenueFactKind.FILL,
                trade_id="entry-trade-baseline",
                last_quantity="0.02",
                received_at=NOW - timedelta(seconds=30),
                cutoff=NOW - timedelta(seconds=30),
            ),
            _venue_fact(
                "entry-fill-late",
                VenueFactKind.FILL,
                trade_id="entry-trade-late",
                last_quantity="0.003",
                received_at=NOW - timedelta(seconds=10),
                cutoff=NOW - timedelta(seconds=10),
            ),
        )
        coordinator.facts[successor.execution_action_id] = (
            _venue_fact(
                "successor-fill",
                VenueFactKind.FILL,
                trade_id="successor-trade",
                last_quantity="0.013",
            ),
            _venue_fact(
                "successor-commission",
                VenueFactKind.COMMISSION,
                trade_id="successor-trade",
            ),
        )
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.01",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(position_fact=position_fact),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert len(coordinator.exit_requests) == 1
    request = coordinator.exit_requests[0]
    assert request["position_quantity"] == "0.003"
    assert "POST_SUCCESSOR_LATE_ENTRY_CLEANUP:" in request["reason_ref"]
    assert request["exit_responsibility_role"] is (
        ExitResponsibilityRole.POST_SUCCESSOR_LATE_ENTRY_CLEANUP
    )
    assert [payload["quantity"] for _, payload in coordinator.submissions] == ["0.003"]


def test_same_direction_manual_position_is_not_auto_closed() -> None:
    async def scenario() -> _SuccessorCoordinator:
        activation = enter_exit(_activation(first_fill=True), observed_at=NOW)
        coordinator = _SuccessorCoordinator(activation)
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        coordinator.actions[entry.execution_action_id] = entry
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact(
                "entry-fill",
                VenueFactKind.FILL,
                trade_id="entry-trade",
                last_quantity="0.01",
            ),
        )
        position_fact = _venue_fact(
            "position-with-manual-addition",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.02",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(
                    current_abs_position="0.02",
                    position_fact=position_fact,
                ),
            ),
            environment_id="demo-1",
        )

        with pytest.raises(ValueError, match="POSITION_ATTRIBUTION_UNKNOWN"):
            await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.exit_requests == []


def test_exit_fill_during_position_read_waits_for_refresh_without_failure() -> (
    None
):
    async def scenario() -> _SuccessorCoordinator:
        activation = enter_exit(_activation(first_fill=True), observed_at=NOW)
        coordinator = _SuccessorCoordinator(activation)
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        exit_action = _action(
            "exit-action",
            ExecutionActionKind.EXIT,
            state=ExecutionActionState.OPEN,
            terms={
                "action_profile": "REDUCE_OR_CLOSE_MARKET",
                "quantity": "0.01",
            },
            client_order_id="e" * 32,
            call_started_at=NOW,
        )
        coordinator.actions = {
            entry.execution_action_id: entry,
            exit_action.execution_action_id: exit_action,
        }
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact(
                "entry-fill",
                VenueFactKind.FILL,
                trade_id="entry-trade",
                last_quantity="0.01",
            ),
        )
        after_read = NOW + timedelta(milliseconds=1)
        coordinator.facts[exit_action.execution_action_id] = (
            _venue_fact(
                "exit-fill-after-position-read",
                VenueFactKind.FILL,
                trade_id="exit-trade",
                last_quantity="0.01",
                received_at=after_read,
                cutoff=after_read,
            ),
        )
        stale_position = _venue_fact(
            "position-before-exit-fill",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.01",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(
                    current_abs_position="0.01",
                    position_fact=stale_position,
                    checked_at=after_read + timedelta(milliseconds=1),
                    attribution_cutoff=NOW,
                ),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.exit_requests == []
    assert coordinator.called_queries == []


def test_superseded_account_fact_read_is_not_a_runtime_failure() -> None:
    async def scenario() -> _SuccessorCoordinator:
        activation = enter_exit(_activation(first_fill=True), observed_at=NOW)
        coordinator = _SuccessorCoordinator(activation)
        coordinator.actions["entry-action"] = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )

        async def superseded(_activation: PlanActivation) -> ProductRiskReductionFacts:
            raise ValueError("ACCOUNT_FACT_SUPERSEDED")

        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=superseded,
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.exit_requests == []
    assert coordinator.submissions == []


@pytest.mark.parametrize(
    "position_overrides",
    (
        {"environment_id": "demo-other"},
        {"venue_ref": "OTHER_VENUE"},
        {"account_ref": "account-other"},
        {"instrument_ref": "ETHUSDT-PERP"},
        {"source_class": VenueFactSourceClass.VENUE_STREAM},
    ),
)
def test_exit_rejects_position_fact_outside_activation_scope(
    position_overrides: dict[str, object],
) -> None:
    async def scenario() -> _SuccessorCoordinator:
        activation = enter_exit(_activation(first_fill=True), observed_at=NOW)
        coordinator = _SuccessorCoordinator(activation)
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        coordinator.actions[entry.execution_action_id] = entry
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact(
                "entry-fill",
                VenueFactKind.FILL,
                trade_id="entry-trade",
                last_quantity="0.01",
            ),
        )
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.01",
            **position_overrides,
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(position_fact=position_fact),
            ),
            environment_id="demo-1",
        )

        with pytest.raises(ValueError, match="POSITION_ATTRIBUTION_UNKNOWN"):
            await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.exit_requests == []
    assert coordinator.applied_facts == []


def test_account_new_risk_stop_does_not_strand_proven_position_exit() -> None:
    async def scenario() -> _SuccessorCoordinator:
        activation = enter_exit(_activation(first_fill=True), observed_at=NOW)
        coordinator = _SuccessorCoordinator(activation)
        coordinator.has_external_activity_conflict = True
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        coordinator.actions[entry.execution_action_id] = entry
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact(
                "entry-fill",
                VenueFactKind.FILL,
                trade_id="entry-trade",
                last_quantity="0.01",
            ),
        )
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.01",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(position_fact=position_fact),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert len(coordinator.exit_requests) == 1
    assert coordinator.exit_requests[0]["position_quantity"] == "0.01"
    assert coordinator.applied_facts


def test_wrong_scope_zero_position_fact_cannot_close_activation() -> None:
    async def scenario() -> _Coordinator:
        activation = enter_exit(_activation(first_fill=True), observed_at=NOW)
        coordinator = _Coordinator(activation)
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        exit_action = _action(
            "exit-action",
            ExecutionActionKind.EXIT,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
            client_order_id="e" * 32,
            call_started_at=NOW - timedelta(seconds=1),
        )
        coordinator.actions.update(
            {
                entry.execution_action_id: entry,
                exit_action.execution_action_id: exit_action,
            }
        )
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact(
                "entry-fill",
                VenueFactKind.FILL,
                trade_id="entry-trade",
                last_quantity="0.01",
            ),
        )
        coordinator.facts[exit_action.execution_action_id] = (
            _venue_fact(
                "exit-fill",
                VenueFactKind.FILL,
                trade_id="exit-trade",
                last_quantity="0.01",
            ),
            _venue_fact(
                "exit-commission",
                VenueFactKind.COMMISSION,
                trade_id="exit-trade",
            ),
        )
        position_fact = _venue_fact(
            "position-zero",
            VenueFactKind.POSITION_STATE,
            position_quantity="0",
            account_ref="account-other",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(
                    current_abs_position="0",
                    position_fact=position_fact,
                ),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.closures == []
    assert coordinator.applied_facts == []


def test_ready_exit_is_not_recovered_against_a_manual_same_direction_position() -> None:
    async def scenario() -> _Coordinator:
        activation = enter_exit(_activation(first_fill=True), observed_at=NOW)
        coordinator = _Coordinator(activation)
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        coordinator.actions[entry.execution_action_id] = entry
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact(
                "entry-fill",
                VenueFactKind.FILL,
                trade_id="entry-trade",
                last_quantity="0.01",
            ),
        )
        ready_exit = _action(
            "exit-ready",
            ExecutionActionKind.EXIT,
            state=ExecutionActionState.READY,
            terms={
                "action_profile": "REDUCE_OR_CLOSE_MARKET",
                "quantity": "0.01",
            },
            client_order_id="e" * 32,
        )
        coordinator.actions[ready_exit.execution_action_id] = ready_exit
        position_fact = _venue_fact(
            "position-with-manual-addition",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.02",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(
                    current_abs_position="0.02",
                    position_fact=position_fact,
                ),
            ),
            environment_id="demo-1",
        )

        with pytest.raises(ValueError, match="POSITION_ATTRIBUTION_UNKNOWN"):
            await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.submissions == []
    assert coordinator.actions["exit-ready"].state is ExecutionActionState.READY


def test_ready_exit_with_stale_quantity_is_replaced_from_current_attributed_position() -> (
    None
):
    async def scenario() -> _SuccessorCoordinator:
        activation = enter_exit(_activation(first_fill=True), observed_at=NOW)
        coordinator = _SuccessorCoordinator(activation)
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        coordinator.actions[entry.execution_action_id] = entry
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact(
                "entry-fill",
                VenueFactKind.FILL,
                trade_id="entry-trade",
                last_quantity="0.01",
            ),
        )
        reduction = _action(
            "protection-filled",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.005"},
        )
        coordinator.actions[reduction.execution_action_id] = reduction
        coordinator.facts[reduction.execution_action_id] = (
            _venue_fact(
                "protection-fill",
                VenueFactKind.FILL,
                trade_id="protection-trade",
                last_quantity="0.005",
            ),
        )
        stale_exit = _action(
            "exit-ready",
            ExecutionActionKind.EXIT,
            state=ExecutionActionState.READY,
            terms={
                "action_profile": "REDUCE_OR_CLOSE_MARKET",
                "quantity": "0.01",
            },
            client_order_id="e" * 32,
        )
        coordinator.actions[stale_exit.execution_action_id] = stale_exit
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.005",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(
                    current_abs_position="0.005",
                    position_fact=position_fact,
                ),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.rejections == [
        ("exit-ready", "EXIT_POSITION_CHANGED_BEFORE_SUBMISSION")
    ]
    assert len(coordinator.exit_requests) == 1
    assert coordinator.exit_requests[0]["position_quantity"] == "0.005"
    assert [item[1]["quantity"] for item in coordinator.submissions] == ["0.005"]


def test_late_halpha_fill_unblocks_attributed_exit_on_next_sync() -> None:
    async def scenario() -> _SuccessorCoordinator:
        activation = enter_exit(_activation(first_fill=True), observed_at=NOW)
        coordinator = _SuccessorCoordinator(activation)
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.02"},
        )
        coordinator.actions[entry.execution_action_id] = entry
        first_fill = _venue_fact(
            "entry-fill-1",
            VenueFactKind.FILL,
            trade_id="entry-trade-1",
            last_quantity="0.01",
        )
        coordinator.facts[entry.execution_action_id] = (first_fill,)
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.02",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(
                    current_abs_position="0.02",
                    position_fact=position_fact,
                ),
            ),
            environment_id="demo-1",
        )

        with pytest.raises(ValueError, match="POSITION_ATTRIBUTION_UNKNOWN"):
            await boundary.sync("activation-1", force=True)
        coordinator.facts[entry.execution_action_id] = (
            first_fill,
            _venue_fact(
                "entry-fill-2",
                VenueFactKind.FILL,
                trade_id="entry-trade-2",
                last_quantity="0.01",
            ),
        )
        await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert len(coordinator.exit_requests) == 1
    assert coordinator.exit_requests[0]["position_quantity"] == "0.02"


def test_external_open_order_identity_blocks_auto_exit() -> None:
    async def scenario() -> _SuccessorCoordinator:
        activation = enter_exit(_activation(first_fill=True), observed_at=NOW)
        coordinator = _SuccessorCoordinator(activation)
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
            client_order_id="b" * 32,
        )
        coordinator.actions[entry.execution_action_id] = entry
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact(
                "entry-fill",
                VenueFactKind.FILL,
                trade_id="entry-trade",
                last_quantity="0.01",
            ),
        )
        position_fact = _venue_fact(
            "position-current",
            VenueFactKind.POSITION_STATE,
            position_quantity="0.01",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=replace(
                    _facts(position_fact=position_fact),
                    open_order_client_ids=("external-order",),
                ),
            ),
            environment_id="demo-1",
        )

        with pytest.raises(ValueError, match="POSITION_ATTRIBUTION_UNKNOWN"):
            await boundary.sync("activation-1", force=True)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.exit_requests == []


def test_flat_exiting_activation_cancels_remaining_algo_protection() -> None:
    async def scenario() -> _Coordinator:
        activation = enter_exit(_activation(first_fill=True), observed_at=NOW)
        coordinator = _Coordinator(activation)
        protection = _action(
            "protection-action",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.OPEN,
            terms={"quantity": "0.01"},
            client_order_id="a" * 32,
        )
        coordinator.actions[protection.execution_action_id] = protection
        coordinator.facts[protection.execution_action_id] = (
            _venue_fact(
                "protection-working", VenueFactKind.ORDER_STATE, status="WORKING"
            ),
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(
                    current_abs_position="0",
                ),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1")
        return coordinator

    coordinator = asyncio.run(scenario())

    assert len(coordinator.cancel_checks) == 1
    assert coordinator.cancel_checks[0].risk_class.value == "RISK_NEUTRAL"
    assert coordinator.cancel_requests[0]["target_endpoint"] == "ALGO"
    assert coordinator.submissions == [("cancel-action", {"profile": "CANCEL_ORDER"})]


def test_flat_exiting_activation_cancels_working_entry_at_ordinary_endpoint() -> None:
    async def scenario() -> _Coordinator:
        activation = enter_exit(_activation(), observed_at=NOW)
        coordinator = _Coordinator(activation)
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.OPEN,
            terms={"quantity": "0.01"},
            client_order_id="b" * 32,
        )
        coordinator.actions[entry.execution_action_id] = entry
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact("entry-working", VenueFactKind.ORDER_STATE, status="WORKING"),
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(current_abs_position="0"),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1")
        return coordinator

    coordinator = asyncio.run(scenario())

    assert len(coordinator.cancel_requests) == 1
    assert coordinator.cancel_requests[0]["target_action_id"] == "entry-action"
    assert coordinator.cancel_requests[0]["target_endpoint"] == "ORDINARY"
    assert coordinator.submissions == [("cancel-action", {"profile": "CANCEL_ORDER"})]


def test_running_activation_cancels_sibling_orders_after_stop_flattens_position() -> (
    None
):
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_activation(first_fill=True))
        stop = _action(
            "protection-action",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.OPEN,
            terms={"quantity": "0.01"},
            client_order_id="a" * 32,
        )
        coordinator.actions[stop.execution_action_id] = stop
        coordinator.facts[stop.execution_action_id] = (
            _venue_fact(
                "stop-fill",
                VenueFactKind.FILL,
                trade_id="stop-trade",
            ),
        )
        for index in (1, 2):
            take_profit = _action(
                f"take-profit-{index}",
                ExecutionActionKind.TAKE_PROFIT,
                state=ExecutionActionState.OPEN,
                terms={"quantity": "0.005"},
                client_order_id=str(index) * 32,
            )
            coordinator.actions[take_profit.execution_action_id] = take_profit
            coordinator.facts[take_profit.execution_action_id] = (
                _venue_fact(
                    f"take-profit-{index}-working",
                    VenueFactKind.ORDER_STATE,
                    status="WORKING",
                ),
            )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(
                    current_abs_position="0",
                    open_algo_client_ids=("1" * 32, "2" * 32),
                ),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1")
        return coordinator

    coordinator = asyncio.run(scenario())

    assert coordinator.exit_requests == []
    assert len(coordinator.cancel_checks) == 2
    assert coordinator.submissions == [
        ("cancel-action", {"profile": "CANCEL_ORDER"}),
        ("cancel-action-2", {"profile": "CANCEL_ORDER"}),
    ]


def test_partial_take_profit_keeps_remaining_protection_while_position_exists() -> None:
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_activation(first_fill=True))
        take_profit = _action(
            "take-profit-filled",
            ExecutionActionKind.TAKE_PROFIT,
            state=ExecutionActionState.OPEN,
            terms={"quantity": "0.005"},
            client_order_id="1" * 32,
        )
        coordinator.actions[take_profit.execution_action_id] = take_profit
        coordinator.facts[take_profit.execution_action_id] = (
            _venue_fact(
                "take-profit-fill",
                VenueFactKind.FILL,
                trade_id="take-profit-trade",
            ),
        )
        protection = _action(
            "protection-action",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.OPEN,
            terms={"quantity": "0.01"},
            client_order_id="a" * 32,
        )
        coordinator.actions[protection.execution_action_id] = protection
        coordinator.facts[protection.execution_action_id] = (
            _venue_fact(
                "protection-working", VenueFactKind.ORDER_STATE, status="WORKING"
            ),
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(
                    current_abs_position="0.005",
                    open_algo_client_ids=("a" * 32,),
                ),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1")
        return coordinator

    coordinator = asyncio.run(scenario())

    assert coordinator.cancel_checks == []
    assert coordinator.exit_requests == []
    assert coordinator.closures == []


def test_flat_terminal_actions_are_reconciled_and_activation_closes() -> None:
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_activation(first_fill=True))
        for action_id, kind in (
            ("entry-action", ExecutionActionKind.ENTRY),
            ("protection-action", ExecutionActionKind.PROTECTION),
        ):
            coordinator.actions[action_id] = _action(
                action_id,
                kind,
                state=ExecutionActionState.OPEN,
                terms={"quantity": "0.01"},
                client_order_id=(
                    "a" * 32 if kind is ExecutionActionKind.PROTECTION else None
                ),
            )
            trade_id = f"{action_id}-trade"
            coordinator.facts[action_id] = (
                _venue_fact(
                    f"{action_id}-ORDER_STATE",
                    VenueFactKind.ORDER_STATE,
                    status="FILLED",
                    cumulative_filled_quantity="0.01",
                ),
                _venue_fact(
                    f"{action_id}-FILL",
                    VenueFactKind.FILL,
                    trade_id=trade_id,
                    last_quantity="0.01",
                ),
                _venue_fact(
                    f"{action_id}-COMMISSION",
                    VenueFactKind.COMMISSION,
                    trade_id=trade_id,
                ),
            )
        position_fact = _venue_fact(
            "position-zero-fact",
            VenueFactKind.POSITION_STATE,
            position_quantity="0",
        )
        coordinator.actions["cancel-action"] = _action(
            "cancel-action",
            ExecutionActionKind.CANCEL,
            state=ExecutionActionState.UNKNOWN,
            terms={"action_profile": "CANCEL_ORDER"},
            cancel_target={
                "client_order_id": coordinator.actions[
                    "protection-action"
                ].client_order_id,
                "endpoint": "ALGO",
            },
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(
                    current_abs_position="0",
                    position_fact=position_fact,
                ),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1")
        return coordinator

    coordinator = asyncio.run(scenario())

    assert [item["action_id"] for item in coordinator.reconciliations] == [
        "cancel-action",
        "entry-action",
        "protection-action",
    ]
    assert len(coordinator.closures) == 1
    assert coordinator.closures[0]["position_zero"] is True
    assert coordinator.closures[0]["open_order_refs"] == ()


def test_terminal_fill_completion_keeps_monotonic_cumulative_query_proof() -> None:
    action = _action(
        "entry-action",
        ExecutionActionKind.ENTRY,
        state=ExecutionActionState.OPEN,
        terms={"quantity": "0.0231"},
    )
    facts = (
        _venue_fact(
            "query-expired-partial",
            VenueFactKind.ORDER_STATE,
            status="EXPIRED",
            cumulative_filled_quantity="0.005",
        ),
        _venue_fact(
            "partial-fill",
            VenueFactKind.FILL,
            trade_id="entry-trade",
            last_quantity="0.005",
            leaves_quantity="0.0181",
        ),
        _venue_fact(
            "later-replayed-expired-zero",
            VenueFactKind.ORDER_STATE,
            status="EXPIRED",
            cumulative_filled_quantity="0",
            cutoff=NOW + timedelta(seconds=1),
            received_at=NOW + timedelta(seconds=1),
        ),
    )

    assert terminal_fills_complete(action, facts)


def test_terminal_fill_completion_waits_when_only_zero_precedes_partial_fill() -> None:
    action = _action(
        "entry-action",
        ExecutionActionKind.ENTRY,
        state=ExecutionActionState.OPEN,
        terms={"quantity": "0.0231"},
    )
    facts = (
        _venue_fact(
            "expired-before-final-fill",
            VenueFactKind.ORDER_STATE,
            status="EXPIRED",
            cumulative_filled_quantity="0",
        ),
        _venue_fact(
            "late-partial-fill",
            VenueFactKind.FILL,
            trade_id="entry-trade",
            last_quantity="0.005",
            leaves_quantity="0.0181",
            cutoff=NOW + timedelta(seconds=1),
            received_at=NOW + timedelta(seconds=1),
        ),
    )

    assert not terminal_fills_complete(action, facts)


def test_flat_activation_closes_after_real_trade_supersedes_recovery_fill() -> None:
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_activation(first_fill=True))
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.CLOSED,
            terms={"quantity": "0.01"},
        )
        protection = _action(
            "protection-action",
            ExecutionActionKind.PROTECTION,
            state=ExecutionActionState.OPEN,
            terms={"quantity": "0.01"},
            client_order_id="a" * 32,
        )
        take_profit = _action(
            "take-profit-action",
            ExecutionActionKind.TAKE_PROFIT,
            state=ExecutionActionState.OPEN,
            terms={"quantity": "0.01"},
            client_order_id="b" * 32,
        )
        coordinator.actions = {
            action.execution_action_id: action
            for action in (entry, protection, take_profit)
        }
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact(
                "entry-fill",
                VenueFactKind.FILL,
                trade_id="entry-trade",
                last_quantity="0.01",
            ),
        )
        coordinator.facts[protection.execution_action_id] = (
            _venue_fact(
                "protection-rejected",
                VenueFactKind.ORDER_STATE,
                status="REJECTED",
                cumulative_filled_quantity="0",
            ),
        )

        synthetic_fill = _venue_fact(
            "synthetic-fill",
            VenueFactKind.FILL,
            trade_id="12345678-1234-5678-1234-567812345678",
            last_quantity="0.01",
            action_ref=take_profit.execution_action_id,
            activation_ref="activation-1",
        )
        synthetic_fill.payload.update(
            {
                "reconciliation": True,
                "event_type": "OrderFilled",
                "client_order_id": take_profit.client_order_id,
                "venue_order_ref": "actual-order-1",
                "last_price": "101",
                "order_side": "SELL",
            }
        )
        real_fill = _venue_fact(
            "real-fill",
            VenueFactKind.FILL,
            trade_id="venue-trade-1",
            last_quantity="0.01",
            action_ref=take_profit.execution_action_id,
            activation_ref="activation-1",
        )
        real_fill.payload.update(
            {
                "reconciliation": True,
                "event_type": "BinanceUserTrade",
                "client_order_id": take_profit.client_order_id,
                "venue_order_ref": "actual-order-1",
                "last_price": "101",
                "order_side": "SELL",
            }
        )
        coordinator.facts[take_profit.execution_action_id] = (
            _venue_fact(
                "actual-order-filled",
                VenueFactKind.ORDER_STATE,
                status="FILLED",
                cumulative_filled_quantity="0.01",
            ),
            synthetic_fill,
            real_fill,
            _venue_fact(
                "real-commission",
                VenueFactKind.COMMISSION,
                trade_id="venue-trade-1",
            ),
            _venue_fact(
                "wrapper-cancelled",
                VenueFactKind.ORDER_STATE,
                status="CANCELLED",
                cumulative_filled_quantity="0",
                received_at=NOW + timedelta(milliseconds=1),
                cutoff=NOW + timedelta(milliseconds=1),
            ),
        )
        position_fact = _venue_fact(
            "position-zero-fact",
            VenueFactKind.POSITION_STATE,
            position_quantity="0",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(
                    current_abs_position="0",
                    position_fact=position_fact,
                ),
            ),
            environment_id="demo-1",
        )

        closure_facts = _facts(
            current_abs_position="0",
            position_fact=position_fact,
        )
        assert [
            fact.payload["trade_id"]
            for fact in collapse_synthetic_reconciliation_fills(
                coordinator.facts[take_profit.execution_action_id]
            )
            if fact.kind is VenueFactKind.FILL
        ] == ["venue-trade-1"]
        assert _position_attribution_proven(
            coordinator.activation,
            closure_facts,
            tuple(coordinator.actions.values()),
            coordinator,
        )
        assert _fills_have_commissions(
            coordinator.facts[take_profit.execution_action_id]
        )
        await boundary._try_close_activation(
            coordinator.activation,
            closure_facts,
        )
        return coordinator

    coordinator = asyncio.run(scenario())

    assert [item["action_id"] for item in coordinator.reconciliations] == [
        "protection-action",
        "take-profit-action",
    ]
    assert len(coordinator.closures) == 1
    assert coordinator.closures[0]["position_zero"] is True


@pytest.mark.parametrize("activation_first_fill", (False, True))
def test_running_entry_fill_does_not_close_before_risk_reduction_fill(
    activation_first_fill: bool,
) -> None:
    async def scenario() -> _Coordinator:
        coordinator = _Coordinator(_activation(first_fill=activation_first_fill))
        entry = _action(
            "entry-action",
            ExecutionActionKind.ENTRY,
            state=ExecutionActionState.OPEN,
            terms={},
        )
        coordinator.actions[entry.execution_action_id] = entry
        coordinator.facts[entry.execution_action_id] = (
            _venue_fact(
                "entry-action-ORDER_STATE",
                VenueFactKind.ORDER_STATE,
                status="FILLED",
            ),
            _venue_fact(
                "entry-action-FILL",
                VenueFactKind.FILL,
                trade_id="entry-trade",
            ),
            _venue_fact(
                "entry-action-COMMISSION",
                VenueFactKind.COMMISSION,
                trade_id="entry-trade",
            ),
        )
        position_fact = _venue_fact(
            "transient-zero-position-fact",
            VenueFactKind.POSITION_STATE,
            position_quantity="0",
        )
        boundary = ProductResponsibilityBoundary(
            loop=asyncio.get_running_loop(),
            coordinator=coordinator,
            fact_provider=lambda _activation: asyncio.sleep(
                0,
                result=_facts(
                    current_abs_position="0",
                    position_fact=position_fact,
                ),
            ),
            environment_id="demo-1",
        )

        await boundary.sync("activation-1")
        return coordinator

    coordinator = asyncio.run(scenario())

    assert coordinator.reconciliations == []
    assert coordinator.closures == []
