"""Executor application coordinator; it owns ordering, not domain records."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_DOWN, localcontext
from threading import RLock
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from psycopg import Connection

from halpha.capital.models import (
    ActivationCapitalBoundary,
    ActionCheckInput,
    AuthorityClass,
    EnvironmentKind,
    NewRiskDisciplinePolicy,
    RiskClass,
)
from halpha.capital.service import CapitalApplicationService
from halpha.domain_values import canonical_decimal, content_digest
from halpha.position_attribution import (
    AccountInstrumentAttribution,
    activation_position_attribution,
    allocate_funding_income,
    account_instrument_attribution,
)
from halpha.outcomes.service import OutcomeApplicationService, review_id_for_activation
from halpha.planning.models import (
    ConditionJudgement,
    ConditionResult,
    PlanActivation,
    PlanEvent,
    PlanLifecycle,
    ProposedAction,
    ProtectionState,
    RunState,
)
from halpha.planning.order_schedule_actions import (
    MaterializedOrderLeg,
    materialize_direct_schedule,
    materialize_direct_schedule_reprice,
    materialize_direct_schedule_retry,
)
from halpha.planning.order_policies import RepriceEntryRule, RuntimeConditionState
from halpha.planning.service import PlanningApplicationService
from halpha.planning.strategies.one_shot import StrategyProposal
from halpha.planning.transitions import (
    proposed_cancel_for_action,
    proposed_direct_protection_from_fill,
    proposed_direct_protection_replacement,
    proposed_direct_take_profit_replacement,
    proposed_direct_take_profits_from_fill,
    proposed_protection_from_fill,
    proposed_reduce_or_close_position,
    proposed_take_profit_market_reduction,
    proposed_take_profits_from_fill,
    venue_source_identity,
)
from halpha.venue_integration.facts import (
    action_quantity_conflict,
    build_activation_allocation_fact,
    build_venue_fact,
    collapse_synthetic_reconciliation_fills,
    order_is_working,
    terminal_fills_complete,
    terminal_order_status,
)
from halpha.venue_integration.binance_funding import FundingIncomeRecord
from halpha.venue_integration.gateway import (
    PersistedActionGate,
    VenueDefinitelyNotSubmitted,
)
from halpha.venue_integration.dispatch_lock import serialize_activation_dispatch
from halpha.venue_integration.models import (
    BINANCE_USDM_VENUE_REF,
    ExecutionAction,
    ExecutionActionKind,
    ExecutionActionState,
    ExitResponsibilityRole,
    VenueFactKind,
    VenueFact,
    VenueFactSourceClass,
)
from halpha.venue_integration.nautilus_events import (
    NautilusExecutionEventNormalizer,
    NormalizedNautilusEvent,
)
from halpha.venue_integration.rejections import (
    VenueRejectionDisposition,
    venue_rejection_disposition,
)
from halpha.venue_integration.repository import (
    PostgreSQLExecutionActionRepository,
    PostgreSQLVenueFactRepository,
)
from halpha.venue_integration.service import ExecutionApplicationService


LOGGER = logging.getLogger(__name__)


def _condition_fact_matches_runtime_identity(
    fact: VenueFact,
    activation: PlanActivation,
    *,
    expected_venue_ref: str,
    expected_source: str,
    source_cutoff: datetime,
) -> bool:
    """Verify one condition fact was built from this runtime's public stream."""

    if fact.kind not in {VenueFactKind.TOP_OF_BOOK, VenueFactKind.MARK_PRICE}:
        return False
    expected_source_object_id = (
        f"{expected_source}:{activation.instrument_ref}:{fact.kind.value}"
    )
    expected_source_sequence = content_digest(
        {
            "source_time": fact.source_time,
            "payload": fact.payload,
        }
    )
    expected_fact_id = str(
        uuid5(
            NAMESPACE_URL,
            ":".join(
                (
                    "halpha",
                    activation.environment_id,
                    expected_source_object_id,
                    expected_source_sequence,
                )
            ),
        )
    )
    return (
        fact.environment_id == activation.environment_id
        and fact.venue_ref == expected_venue_ref
        and fact.instrument_ref == activation.instrument_ref
        and fact.account_ref is None
        and fact.action_ref is None
        and fact.source_class is VenueFactSourceClass.VENUE_STREAM
        and fact.source_object_id == expected_source_object_id
        and fact.source_sequence == expected_source_sequence
        and fact.venue_fact_id == expected_fact_id
        and fact.source_time is not None
        and fact.source_time <= fact.received_at
        and fact.cutoff == fact.received_at
        and fact.received_at <= source_cutoff
        and fact.payload.get("source") == expected_source
    )


@dataclass(frozen=True, slots=True)
class CoordinatedProposalResult:
    plan_event: PlanEvent
    execution_action: ExecutionAction | None


@dataclass(frozen=True, slots=True)
class ProcessExecutionResult:
    execution_action: ExecutionAction
    venue_called: bool
    reason_code: str


class OrderScheduleCapRejected(ValueError):
    """Atomic schedule rejection with exact per-leg capital reasons."""

    def __init__(self, rejections: tuple[tuple[str, str], ...]) -> None:
        if not rejections:
            raise ValueError("ORDER_SCHEDULE_CAP_REJECTION_REQUIRED")
        self.rejections = rejections
        reason_codes = ",".join(
            dict.fromkeys(reason_code for _, reason_code in rejections)
        )
        super().__init__(f"ORDER_SCHEDULE_CAP_REJECTED:{reason_codes}")


def _runtime_schedule_proposed_action(
    item: MaterializedOrderLeg,
    check: ActionCheckInput,
) -> ProposedAction:
    """Bind an allowed market downsize to the persisted execution action."""

    proposed = item.proposed_action
    submitted_text = check.quantized_quantity
    if submitted_text == proposed.quantity:
        return proposed
    schedule_context = proposed.execution_context.get("order_schedule")
    if (
        proposed.order_type != "MARKET"
        or not isinstance(schedule_context, dict)
        or proposed.quantity is None
    ):
        raise ValueError("ORDER_SCHEDULE_RUNTIME_QUANTITY_CONFLICT")
    try:
        planned = Decimal(proposed.quantity)
        submitted = Decimal(submitted_text)
        price = Decimal(check.conservative_price)
        requested_notional = Decimal(item.leg.requested_notional)
        step = Decimal(str(schedule_context["quantity_step"]))
    except (ArithmeticError, KeyError, TypeError, ValueError):
        raise ValueError("ORDER_SCHEDULE_RUNTIME_QUANTITY_CONFLICT") from None
    if min(planned, submitted, price, requested_notional, step) <= 0:
        raise ValueError("ORDER_SCHEDULE_RUNTIME_QUANTITY_CONFLICT")
    with localcontext() as context:
        context.prec = 128
        expected = min(
            planned,
            (requested_notional / price / step).to_integral_value(rounding=ROUND_DOWN)
            * step,
        )
    if submitted != expected:
        raise ValueError("ORDER_SCHEDULE_RUNTIME_QUANTITY_CONFLICT")
    execution_context = {
        **proposed.execution_context,
        "runtime_market_sizing": {
            "planned_quantity": proposed.quantity,
            "submitted_quantity": submitted_text,
            "requested_notional": item.leg.requested_notional,
            "conservative_price": check.conservative_price,
            "quantity_step": canonical_decimal(step),
        },
    }
    return proposed.model_copy(
        update={
            "quantity": submitted_text,
            "execution_context": execution_context,
        }
    )


def _outcome_activation_id_for_fact(fact: VenueFact) -> str | None:
    activation_ref = getattr(fact, "activation_ref", None)
    if isinstance(activation_ref, str):
        return activation_ref
    impact_scope = getattr(fact, "impact_scope", None)
    if not isinstance(impact_scope, dict):
        return None
    activation_id = impact_scope.get("account_episode_activation_id")
    return activation_id if isinstance(activation_id, str) else None


def _unattributed_fact_requires_account_stop(
    fact: VenueFact,
    *,
    reconciliation_not_before: datetime | None,
) -> bool:
    """Distinguish current external risk from terminal startup history."""

    if fact.kind is VenueFactKind.FILL:
        if fact.payload.get("reconciliation") is not True:
            return True
        source_time = fact.source_time
        if reconciliation_not_before is None or source_time is None:
            return True
        return source_time >= reconciliation_not_before
    if fact.kind is not VenueFactKind.ORDER_STATE:
        return False
    # A reconciled WORKING order is still a current venue responsibility. Only
    # terminal order history may be ignored; otherwise a resting external order
    # can change exposure after reconnect without stopping new Halpha risk.
    return fact.payload.get("status") == "WORKING"


def _protection_projection_state(
    action: ExecutionAction,
    fact: VenueFact,
) -> ProtectionState | None:
    if action.action_kind is not ExecutionActionKind.PROTECTION:
        return None
    if fact.kind is not VenueFactKind.ORDER_STATE:
        return None
    status = str(fact.payload.get("status", "")).upper()
    if status in {"WORKING", "NEW", "ACCEPTED", "ACKNOWLEDGED"}:
        return ProtectionState.WORKING
    if status in {"CANCELLED", "CANCELED", "REJECTED", "EXPIRED"}:
        return ProtectionState.GAP
    return None


def _aggregate_protection_projection(
    activation: PlanActivation,
    actions: tuple[ExecutionAction, ...],
    facts_for_action: Callable[[str], tuple[VenueFact, ...]],
) -> ProtectionState | None:
    """Reduce all confirmed fills and exact protection duties to one projection."""

    if activation.protection_state in {
        ProtectionState.GAP,
        ProtectionState.CLOSED,
    }:
        return activation.protection_state

    fill_refs: set[str] = set()
    protections_by_fill: dict[str, list[ExecutionAction]] = {}
    for action in actions:
        if action.action_kind is ExecutionActionKind.ENTRY:
            fill_refs.update(
                fact.venue_fact_id
                for fact in facts_for_action(action.execution_action_id)
                if fact.kind is VenueFactKind.FILL
            )
            continue
        if action.action_kind is not ExecutionActionKind.PROTECTION:
            continue
        context = action.action_terms.get("execution_context")
        fill_ref = context.get("fill_fact_ref") if isinstance(context, dict) else None
        if isinstance(fill_ref, str):
            protections_by_fill.setdefault(fill_ref, []).append(action)

    if not fill_refs:
        return None

    any_unknown = False
    for fill_ref in fill_refs:
        protections = protections_by_fill.get(fill_ref, [])
        if not protections:
            any_unknown = True
            continue
        covered = False
        failed = False
        pending = False
        for protection in protections:
            if protection.state is ExecutionActionState.NOT_SUBMITTED:
                failed = True
                continue
            protection_facts = facts_for_action(protection.execution_action_id)
            if action_quantity_conflict(protection, protection_facts):
                return ProtectionState.GAP
            terminal = terminal_order_status(protection_facts)
            if terminal in {"CANCELLED", "REJECTED", "EXPIRED"}:
                failed = True
                continue
            if terminal == "FILLED" or order_is_working(protection_facts):
                covered = True
                continue
            pending = True
        if not covered:
            # A tighter replacement can legitimately be persisted or in flight
            # while its predecessor is being cancelled.  Treat that bounded
            # hand-off as unresolved coverage; only declare a gap when every
            # protection candidate for this fill has definitively failed.
            if pending:
                any_unknown = True
            elif failed:
                return ProtectionState.GAP
            else:
                any_unknown = True
    return ProtectionState.UNKNOWN if any_unknown else ProtectionState.WORKING


def _submission_block_reason(
    action: ExecutionAction,
    activation: PlanActivation,
) -> str | None:
    """Pause new risk without disabling protection, cancellation, or exit duties."""

    if activation.lifecycle in {
        PlanLifecycle.USER_TAKEOVER,
        PlanLifecycle.COMPLETED,
    }:
        return "USER_TAKEOVER_ACTIVE"
    if (
        activation.run_state is not RunState.ACTIVE
        and action.action_class is RiskClass.RISK_INCREASING
    ):
        return "NEW_RISK_STOPPED"
    return None


def _is_frozen_planned_scale_in_action(
    action: ExecutionAction,
    activation: PlanActivation,
) -> bool:
    """Recognize only an original, multi-leg entry after this plan has entered.

    The exception is intentionally reconstructed from the immutable activation
    snapshot rather than trusted from an action flag.  Retried or repriced
    entries, a one-time entry, and a plan without an earlier fill remain normal
    new-risk attempts.
    """

    if (
        action.action_kind is not ExecutionActionKind.ENTRY
        or getattr(activation, "order_schedule_snapshot", None) is None
        or not getattr(activation, "has_entry_fill", False)
    ):
        return False
    rule_state = getattr(activation, "rule_state", None)
    deadlines = rule_state.get("deadlines") if isinstance(rule_state, dict) else None
    deadline_value = (
        deadlines.get("entry_valid_until") if isinstance(deadlines, dict) else None
    )
    if not isinstance(deadline_value, str):
        return False
    try:
        entry_valid_until = datetime.fromisoformat(deadline_value)
    except ValueError:
        return False
    if entry_valid_until.utcoffset() is None:
        return False
    terms = action.action_terms
    context = terms.get("execution_context")
    schedule = context.get("order_schedule") if isinstance(context, dict) else None
    if not isinstance(schedule, dict):
        return False
    leg_index = schedule.get("leg_index")
    if (
        not isinstance(leg_index, int)
        or schedule.get("attempt_index", 0) != 0
        or any(
            key in schedule
            for key in ("retry_reason", "replacement_price", "reprice_index")
        )
    ):
        return False
    try:
        expected = next(
            (
                item
                for item in materialize_direct_schedule(
                    activation,
                    entry_valid_until=entry_valid_until,
                )
                if item.leg.leg_index == leg_index
            ),
            None,
        )
    except (TypeError, ValueError):
        return False
    if expected is None or expected.leg.leg_count < 2:
        return False
    proposed = expected.proposed_action
    return (
        getattr(action, "execution_action_id", None) == expected.execution_action_id
        and getattr(action, "source_identity", None) == expected.source_identity
        and getattr(action, "client_order_id", None) == expected.client_order_id
        and schedule == proposed.execution_context["order_schedule"]
        and terms.get("instrument_ref") == proposed.instrument_ref
        and terms.get("direction") == proposed.direction.value
        and terms.get("action_profile") == proposed.action_profile
        and terms.get("order_type") == proposed.order_type
        and terms.get("price") == proposed.price
        and terms.get("close_position") is proposed.close_position
        and terms.get("reduce_only") is proposed.reduce_only
        and terms.get("causation_ref") == proposed.causation_ref
    )


class HalphaCoordinator:
    """Compose TRADEPLAN -> CAP -> EXE with no direct cross-owner table writes."""

    def __init__(
        self,
        connection: Connection[Any],
        gate: PersistedActionGate,
        *,
        environment_id: str,
        environment_kind: str,
        authority_class: str,
        execution_profile_ref: str,
        account_ref: str,
        venue_ref: str = BINANCE_USDM_VENUE_REF,
        runtime_real_write_gate: str = "CLOSED",
        live_write_activation_ids: tuple[str, ...] = (),
        live_write_submission_guard: Callable[[str], None] | None = None,
        live_write_risk_control_only: bool = False,
        unattributed_reconciliation_not_before: datetime | None = None,
        new_risk_discipline_policy: NewRiskDisciplinePolicy | None = None,
    ) -> None:
        if environment_kind == "DEMO":
            if (
                authority_class != "DEMO_VALIDATION"
                or execution_profile_ref != "BINANCE_DEMO"
            ):
                raise ValueError("EXECUTION_PROFILE_MISMATCH")
        elif environment_kind == "LIVE":
            if (
                authority_class != "LIVE_REAL_CAPITAL"
                or execution_profile_ref != "BINANCE_LIVE_WRITE"
                or runtime_real_write_gate not in {"CLOSED", "OPEN"}
                or not live_write_activation_ids
                or live_write_submission_guard is None
                or (
                    runtime_real_write_gate == "CLOSED"
                    and not live_write_risk_control_only
                )
                or (runtime_real_write_gate == "OPEN" and live_write_risk_control_only)
            ):
                raise ValueError("EXECUTION_PROFILE_MISMATCH")
        else:
            raise ValueError("EXECUTION_PROFILE_MISMATCH")
        if venue_ref != BINANCE_USDM_VENUE_REF:
            raise ValueError("EXECUTION_VENUE_REF_MISMATCH")
        if (
            unattributed_reconciliation_not_before is not None
            and unattributed_reconciliation_not_before.utcoffset() is None
        ):
            raise ValueError("RECONCILIATION_HISTORY_TIMEZONE_REQUIRED")
        self._connection = connection
        self._gate = gate
        self._environment_id = environment_id
        self._environment_kind = environment_kind
        self._authority_class = authority_class
        self._execution_profile_ref = execution_profile_ref
        self._account_ref = account_ref
        self._venue_ref = venue_ref
        self._runtime_real_write_gate = runtime_real_write_gate
        self._live_write_activation_ids = frozenset(live_write_activation_ids)
        self._live_write_submission_guard = live_write_submission_guard
        self._live_write_risk_control_only = live_write_risk_control_only
        self._unattributed_reconciliation_not_before = (
            unattributed_reconciliation_not_before.astimezone(UTC)
            if unattributed_reconciliation_not_before is not None
            else None
        )
        self._new_risk_discipline_policy = (
            new_risk_discipline_policy or NewRiskDisciplinePolicy()
        )
        self._planning = PlanningApplicationService(connection, environment_id)
        self._capital = CapitalApplicationService(connection, environment_id)
        self._action_repository = PostgreSQLExecutionActionRepository(
            connection, environment_id
        )
        self._fact_repository = PostgreSQLVenueFactRepository(
            connection, environment_id
        )
        self._execution = ExecutionApplicationService(
            self._action_repository,
            self._fact_repository,
            environment_id=environment_id,
            environment_kind=environment_kind,
            authority_class=authority_class,
            execution_profile_ref=execution_profile_ref,
            account_ref=account_ref,
        )
        self._startup_recovery_lock = RLock()
        self._startup_recovery_armed = False
        self._startup_recovery_initialized = False
        self._startup_recovery_pending: dict[str, str] = {}
        self._startup_recovery_next_query_at: dict[str, datetime] = {}
        self._startup_recovery_resolution_sink: Callable[[str, str], None] | None = None
        self._venue_mutation_lock = RLock()
        self._venue_mutations_enabled = True

    def get_activation_snapshot(self, activation_id: str) -> PlanActivation:
        return self._planning.get_activation(activation_id)

    def record_runtime_condition_state(
        self,
        *,
        activation_id: str,
        state_key: str,
        state: RuntimeConditionState,
    ) -> PlanActivation:
        with self._connection.transaction():
            return self._planning.record_runtime_condition_state(
                activation_id=activation_id,
                state_key=state_key,
                state=state,
            )

    def get_execution_action(self, execution_action_id: str) -> ExecutionAction:
        return self._action_repository.get(execution_action_id)

    def list_execution_actions(
        self,
        activation_id: str,
    ) -> tuple[ExecutionAction, ...]:
        return self._action_repository.list_for_activation(activation_id)

    def account_instrument_attribution(
        self,
        activation_id: str,
        *,
        as_of: datetime | None = None,
    ) -> AccountInstrumentAttribution:
        """Return the target virtual position and the same-symbol account sum."""

        with self._connection.transaction():
            activation = self._planning.get_activation(activation_id)
            return account_instrument_attribution(
                activation,
                self._planning.list_account_instrument_activations(
                    account_ref=activation.account_ref,
                    instrument_ref=activation.instrument_ref,
                ),
                self._action_repository.list_for_activation,
                self._fact_repository.list_for_action,
                as_of=as_of,
            )

    def record_funding_income(
        self,
        *,
        activation_id: str,
        records: tuple[FundingIncomeRecord, ...],
        observed_at: datetime,
    ) -> tuple[VenueFact, ...]:
        """Persist account funding and exact per-activation allocations."""

        persisted: list[VenueFact] = []
        changed_activation_ids: set[str] = set()
        with self._connection.transaction():
            target = self._planning.get_activation(activation_id)
            activations = self._planning.list_account_instrument_activations(
                account_ref=target.account_ref,
                instrument_ref=target.instrument_ref,
            )
            for record in records:
                positions: dict[str, str] = {}
                for activation in activations:
                    if activation.created_at > record.source_time:
                        continue
                    attribution = activation_position_attribution(
                        activation,
                        self._action_repository.list_for_activation(
                            activation.activation_id
                        ),
                        self._fact_repository.list_for_action,
                        as_of=record.source_time,
                    )
                    if Decimal(attribution.signed_position) != 0:
                        positions[activation.activation_id] = (
                            attribution.signed_position
                        )
                allocations = allocate_funding_income(
                    record.income,
                    positions,
                )
                aggregate_fact_id = str(
                    uuid5(
                        NAMESPACE_URL,
                        (
                            f"urn:halpha:{self._environment_id}:funding:"
                            f"{target.account_ref}:{target.instrument_ref}:"
                            f"{record.transaction_id}"
                        ),
                    )
                )
                aggregate = build_venue_fact(
                    venue_fact_id=aggregate_fact_id,
                    environment_id=self._environment_id,
                    venue_ref=self._venue_ref,
                    account_ref=target.account_ref,
                    instrument_ref=target.instrument_ref,
                    kind=VenueFactKind.FUNDING,
                    source_class=VenueFactSourceClass.VENUE_QUERY,
                    source_object_id=(
                        f"{target.account_ref}:{target.instrument_ref}:"
                        f"{record.transaction_id}"
                    ),
                    source_sequence="1",
                    source_time=record.source_time,
                    received_at=observed_at,
                    cutoff=observed_at,
                    payload={
                        "record_type": "ACCOUNT_FUNDING",
                        "transaction_id": record.transaction_id,
                        "symbol": record.symbol,
                        "income": record.income,
                        "currency": record.asset,
                        "allocation_count": len(allocations),
                    },
                )
                existing_aggregate = self._fact_repository.find_by_source(aggregate)
                if existing_aggregate is None:
                    self._execution.apply_venue_fact(
                        fact=aggregate,
                        observed_at=observed_at,
                    )
                    persisted.append(aggregate)
                else:
                    aggregate = existing_aggregate
                for allocation in allocations:
                    allocation_fact = build_activation_allocation_fact(
                        venue_fact_id=str(
                            uuid5(
                                NAMESPACE_URL,
                                (
                                    f"urn:halpha:{self._environment_id}:"
                                    f"funding-allocation:"
                                    f"{record.transaction_id}:"
                                    f"{allocation.activation_id}"
                                ),
                            )
                        ),
                        environment_id=self._environment_id,
                        venue_ref=self._venue_ref,
                        account_ref=target.account_ref,
                        instrument_ref=target.instrument_ref,
                        kind=VenueFactKind.FUNDING,
                        source_object_id=(
                            f"{target.account_ref}:{target.instrument_ref}:"
                            f"{record.transaction_id}:"
                            f"{allocation.activation_id}"
                        ),
                        source_sequence="1",
                        source_time=record.source_time,
                        received_at=observed_at,
                        cutoff=observed_at,
                        payload={
                            "record_type": "ACTIVATION_FUNDING_ALLOCATION",
                            "transaction_id": record.transaction_id,
                            "income": allocation.income,
                            "currency": record.asset,
                            "activation_signed_position": (allocation.signed_position),
                            "account_signed_position": canonical_decimal(
                                sum(
                                    (Decimal(value) for value in positions.values()),
                                    Decimal(0),
                                )
                            ),
                            "aggregate_income": record.income,
                            "allocation_method": (
                                "ABS_VIRTUAL_POSITION_LARGEST_REMAINDER"
                            ),
                        },
                        activation_ref=allocation.activation_id,
                        aggregate_fact_ref=aggregate.venue_fact_id,
                    )
                    if self._fact_repository.find_by_source(allocation_fact) is None:
                        self._execution.apply_venue_fact(
                            fact=allocation_fact,
                            observed_at=observed_at,
                        )
                        persisted.append(allocation_fact)
                        changed_activation_ids.add(allocation.activation_id)
        if changed_activation_ids:
            self._refresh_completed_reviews_after_commit(
                changed_fact_activation_ids=tuple(sorted(changed_activation_ids)),
                fact_cutoff=max(
                    (
                        fact.cutoff
                        for fact in persisted
                        if fact.activation_ref in changed_activation_ids
                    ),
                    default=observed_at,
                ),
                observed_at=observed_at,
            )
        return tuple(persisted)

    def has_open_entry_responsibility(self, activation_id: str) -> bool:
        return self._action_repository.has_open_entry_responsibility(activation_id)

    def has_unclosed_called_responsibility(self, activation_id: str) -> bool:
        return self._action_repository.has_unclosed_called_responsibility(activation_id)

    def new_risk_allowed(self, activation_id: str) -> bool:
        with self._connection.transaction():
            return self._capital.new_risk_allowed(activation_id)

    def external_activity_conflict(self, activation_id: str) -> bool:
        with self._connection.transaction():
            return self._capital.external_activity_conflict(activation_id)

    def list_venue_facts_for_action(
        self,
        execution_action_id: str,
    ) -> tuple[VenueFact, ...]:
        return self._fact_repository.list_for_action(execution_action_id)

    def _refresh_completed_reviews_after_commit(
        self,
        *,
        terminal_actions: tuple[ExecutionAction | None, ...] = (),
        changed_fact_activation_ids: tuple[str, ...] = (),
        fact_cutoff: datetime,
        observed_at: datetime,
    ) -> None:
        """Best-effort OUT correction after authoritative EXE/DAT commits.

        OUT owns a derived, versioned projection.  Its transaction must never
        roll back a terminal action or an accepted venue fact, and repeated
        calls are safe because ``update_activation_review`` reuses an unchanged
        basis.
        """

        # Narrow coordinator unit tests intentionally bypass ``__init__``.
        if not hasattr(self, "_planning") or not hasattr(self, "_environment_id"):
            return
        activation_ids = dict.fromkeys(
            (
                action.activation_id
                for action in terminal_actions
                if action is not None
                and action.state
                in {
                    ExecutionActionState.NOT_SUBMITTED,
                    ExecutionActionState.CLOSED,
                    ExecutionActionState.HANDED_OVER,
                }
            ),
        )
        activation_ids.update(
            (activation_id, None)
            for activation_id in changed_fact_activation_ids
            if activation_id
        )
        for activation_id in activation_ids:
            try:
                with self._connection.transaction():
                    activation = self._planning.get_activation(activation_id)
                    if activation.lifecycle is not PlanLifecycle.COMPLETED:
                        continue
                    OutcomeApplicationService(
                        self._connection,
                        self._environment_id,
                    ).update_activation_review(
                        activation_id,
                        fact_cutoff=fact_cutoff,
                        observed_at=observed_at,
                    )
            except Exception:
                LOGGER.exception(
                    "Failed to refresh completed activation review after authoritative "
                    "commit: activation_id=%s",
                    activation_id,
                )

    def _refresh_protection_projection(
        self,
        *,
        activation_id: str,
        observed_at: datetime,
    ) -> None:
        """Recompute aggregate coverage instead of trusting one order callback."""

        activation = self._planning.get_activation(activation_id, for_update=True)
        projection = _aggregate_protection_projection(
            activation,
            self._action_repository.list_for_activation(activation_id),
            self._fact_repository.list_for_action,
        )
        if projection is None:
            return
        if projection is activation.protection_state:
            return
        if (
            activation.protection_state is ProtectionState.NONE
            and projection is ProtectionState.WORKING
        ):
            activation = self._planning.update_protection_projection(
                activation_id=activation_id,
                protection_state=ProtectionState.UNKNOWN,
                pending_action_digest=None,
                observed_at=observed_at,
            )
        self._planning.update_protection_projection(
            activation_id=activation.activation_id,
            protection_state=projection,
            pending_action_digest=None,
            observed_at=observed_at,
        )

    def _apply_protection_projection_from_fact(
        self,
        *,
        action: ExecutionAction,
        fact: VenueFact,
        observed_at: datetime,
    ) -> None:
        if action.action_kind is ExecutionActionKind.ENTRY:
            if fact.kind is not VenueFactKind.FILL:
                return
        elif action.action_kind is ExecutionActionKind.PROTECTION:
            if fact.kind not in {VenueFactKind.FILL, VenueFactKind.ORDER_STATE}:
                return
        else:
            return
        self._refresh_protection_projection(
            activation_id=action.activation_id,
            observed_at=observed_at,
        )

    def expire_empty_entry_window(
        self,
        *,
        activation_id: str,
        observed_at: datetime,
    ) -> tuple[PlanActivation, PlanEvent]:
        """Close one expired activation when it never created venue responsibility."""

        with self._connection.transaction():
            expired, event = self._planning.expire_entry_deadline(
                activation_id=activation_id,
                plan_event_id=str(uuid4()),
                observed_at=observed_at,
            )
            if (
                expired.has_entry_fill
                or expired.pending_action_digest is not None
                or self._action_repository.list_for_activation(activation_id)
            ):
                return expired, event
            result_ref = review_id_for_activation(
                self._environment_id,
                activation_id,
            )
            closure_digest = content_digest(
                {
                    "environment_id": self._environment_id,
                    "activation_id": activation_id,
                    "reason": "ENTRY_WINDOW_EXPIRED",
                    "plan_event_id": event.plan_event_id,
                    "entry_deadline": event.source_cutoff,
                    "has_entry_fill": False,
                    "execution_action_count": 0,
                }
            )
            completed = self._planning.complete_with_execution_closure(
                activation_id=activation_id,
                closure_digest=closure_digest,
                result_ref=result_ref,
                observed_at=observed_at,
            )
            OutcomeApplicationService(
                self._connection,
                self._environment_id,
            ).update_activation_review(
                activation_id,
                fact_cutoff=event.source_cutoff,
                observed_at=observed_at,
            )
            return completed, event

    def expire_remaining_entry_opportunity(
        self,
        *,
        activation_id: str,
        source_cutoff: datetime,
        observed_at: datetime,
    ) -> tuple[PlanActivation, PlanEvent]:
        """Consume entry at its post-submission deadline before canceling the order."""

        with self._connection.transaction():
            return self._planning.expire_remaining_entry_opportunity(
                activation_id=activation_id,
                plan_event_id=str(uuid4()),
                source_cutoff=source_cutoff,
                observed_at=observed_at,
            )

    def invalidate_empty_entry_opportunity(
        self,
        *,
        activation_id: str,
        source_cutoff: datetime,
        evidence: dict[str, object],
        observed_at: datetime,
    ) -> tuple[PlanActivation, PlanEvent]:
        """Close an unfilled activation after its frozen market thesis is invalidated."""

        with self._connection.transaction():
            invalidated, event = self._planning.invalidate_entry_opportunity(
                activation_id=activation_id,
                plan_event_id=str(uuid4()),
                source_cutoff=source_cutoff,
                evidence=evidence,
                observed_at=observed_at,
            )
            if (
                invalidated.has_entry_fill
                or invalidated.pending_action_digest is not None
                or self._action_repository.list_for_activation(activation_id)
            ):
                return invalidated, event
            result_ref = review_id_for_activation(
                self._environment_id,
                activation_id,
            )
            closure_digest = content_digest(
                {
                    "environment_id": self._environment_id,
                    "activation_id": activation_id,
                    "reason": "ENTRY_MARKET_INVALIDATED",
                    "plan_event_id": event.plan_event_id,
                    "source_cutoff": event.source_cutoff,
                    "evidence": evidence,
                    "has_entry_fill": False,
                    "execution_action_count": 0,
                }
            )
            completed = self._planning.complete_with_execution_closure(
                activation_id=activation_id,
                closure_digest=closure_digest,
                result_ref=result_ref,
                observed_at=observed_at,
            )
            OutcomeApplicationService(
                self._connection,
                self._environment_id,
            ).update_activation_review(
                activation_id,
                fact_cutoff=event.source_cutoff,
                observed_at=observed_at,
            )
            return completed, event

    def get_entry_sizing_boundary(
        self, activation_id: str
    ) -> ActivationCapitalBoundary:
        with self._connection.transaction():
            return self._capital.get_plan_boundary(activation_id)

    def record_strategy_proposal_rejection(
        self,
        *,
        plan_event_id: str,
        proposal: StrategyProposal,
        reason_code: str,
        observed_at: datetime,
    ) -> PlanEvent:
        """Persist a fail-closed proposal outcome without creating an EXE action."""

        with self._connection.transaction():
            return self._planning.record_plan_event(
                plan_event_id=plan_event_id,
                activation_id=proposal.activation_id,
                rule_id=proposal.rule_id,
                source_identity=proposal.source_identity,
                source_cutoff=proposal.source_cutoff,
                input_digest=proposal.input_digest,
                reason_code=reason_code,
                proposed_action=None,
                no_action_reason=reason_code,
                condition_judgement=ConditionJudgement(
                    rule_id=proposal.rule_id,
                    source_identity=proposal.source_identity,
                    source_cutoff=proposal.source_cutoff,
                    input_digest=proposal.input_digest,
                    result=ConditionResult.UNKNOWN,
                    reason_code=reason_code,
                    next_responsibility="NONE",
                ),
                capital_decision={
                    "accepted": False,
                    "reason_code": f"NOT_EVALUATED_{reason_code}",
                },
                created_at=observed_at,
            )

    def record_direct_pre_submit_rejection(
        self,
        *,
        activation_id: str,
        execution_action_id: str,
        reason_code: str,
        observed_at: datetime,
    ) -> PlanEvent:
        """Persist one replay-safe reason why a direct leg did not reach submission."""

        rule_id = "DIRECT_PRE_SUBMIT"
        source_identity = (
            f"{activation_id}:{rule_id}:{execution_action_id}:{reason_code}"
        )
        input_digest = content_digest(
            {
                "activation_id": activation_id,
                "execution_action_id": execution_action_id,
                "reason_code": reason_code,
            }
        )
        plan_event_id = str(
            uuid5(
                NAMESPACE_URL,
                (f"urn:halpha:{self._environment_id}:plan-event:{source_identity}"),
            )
        )
        with self._connection.transaction():
            return self._planning.record_plan_event(
                plan_event_id=plan_event_id,
                activation_id=activation_id,
                rule_id=rule_id,
                source_identity=source_identity,
                source_cutoff=observed_at,
                input_digest=input_digest,
                reason_code=reason_code,
                proposed_action=None,
                no_action_reason=reason_code,
                condition_judgement=ConditionJudgement(
                    rule_id=rule_id,
                    source_identity=source_identity,
                    source_cutoff=observed_at,
                    input_digest=input_digest,
                    result=ConditionResult.UNKNOWN,
                    reason_code=reason_code,
                    next_responsibility="EXECUTOR_RETRY",
                ),
                capital_decision={
                    "accepted": False,
                    "reason_code": f"NOT_EVALUATED_{reason_code}",
                },
                created_at=observed_at,
            )

    def record_executor_runtime_reattached(
        self,
        *,
        activation_id: str,
        observed_at: datetime,
    ) -> PlanEvent:
        """Record that a restarted executor resumed one persisted activation."""

        rule_id = "EXECUTOR_RUNTIME_CONTINUITY"
        reason_code = "EXECUTOR_RUNTIME_REATTACHED"
        source_identity = f"{activation_id}:{rule_id}:{observed_at.isoformat()}"
        input_digest = content_digest(
            {
                "activation_id": activation_id,
                "observed_at": observed_at,
                "reason_code": reason_code,
            }
        )
        plan_event_id = str(
            uuid5(
                NAMESPACE_URL,
                (f"urn:halpha:{self._environment_id}:plan-event:{source_identity}"),
            )
        )
        with self._connection.transaction():
            return self._planning.record_plan_event(
                plan_event_id=plan_event_id,
                activation_id=activation_id,
                rule_id=rule_id,
                source_identity=source_identity,
                source_cutoff=observed_at,
                input_digest=input_digest,
                reason_code=reason_code,
                proposed_action=None,
                no_action_reason=reason_code,
                condition_judgement=None,
                capital_decision={
                    "accepted": False,
                    "reason_code": "NOT_APPLICABLE_RUNTIME_CONTINUITY",
                },
                created_at=observed_at,
            )

    def reject_execution_action_before_submission(
        self,
        execution_action_id: str,
        *,
        reason_code: str,
        observed_at: datetime,
    ) -> ExecutionAction:
        """Close a READY action when the second fresh-fact check fails."""

        with self._connection.transaction():
            action = self._action_repository.get(
                execution_action_id,
                for_update=True,
            )
            if action.state is not ExecutionActionState.READY:
                return action
            updated = self._execution.record_definitely_not_submitted(
                execution_action_id,
                reason_code=reason_code,
                observed_at=observed_at,
            )
        self._refresh_completed_reviews_after_commit(
            terminal_actions=(updated,),
            fact_cutoff=observed_at,
            observed_at=observed_at,
        )
        return updated

    def record_unknown_action_not_submitted(
        self,
        execution_action_id: str,
        *,
        reason_code: str,
        observed_at: datetime,
    ) -> ExecutionAction:
        """Close only an unresolved action proven absent by its original identity."""

        with self._connection.transaction():
            action = self._action_repository.get(
                execution_action_id,
                for_update=True,
            )
            if action.state is not ExecutionActionState.UNKNOWN:
                return action
            updated = self._execution.record_definitely_not_submitted(
                execution_action_id,
                reason_code=reason_code,
                observed_at=observed_at,
            )
        self._refresh_completed_reviews_after_commit(
            terminal_actions=(updated,),
            fact_cutoff=observed_at,
            observed_at=observed_at,
        )
        return updated

    def reconcile_execution_action(
        self,
        execution_action_id: str,
        *,
        closure_evidence: dict[str, Any],
        venue_fact_refs: tuple[str, ...],
        observed_at: datetime,
    ) -> ExecutionAction:
        with self._connection.transaction():
            updated = self._execution.reconcile_execution_action(
                execution_action_id,
                closure_evidence=closure_evidence,
                venue_fact_refs=venue_fact_refs,
                observed_at=observed_at,
            )
        self._refresh_completed_reviews_after_commit(
            terminal_actions=(updated,),
            fact_cutoff=observed_at,
            observed_at=observed_at,
        )
        return updated

    def reconcile_retryable_entry_rejection(
        self,
        execution_action_id: str,
        *,
        observed_at: datetime,
    ) -> ExecutionAction:
        """Close a proven zero-fill policy rejection without treating it as failure."""

        with self._connection.transaction():
            action = self._action_repository.get(
                execution_action_id,
                for_update=True,
            )
            if action.state is ExecutionActionState.CLOSED:
                return action
            facts = self._fact_repository.list_for_action(execution_action_id)
            disposition = venue_rejection_disposition(action, facts)
            if (
                action.state is not ExecutionActionState.OPEN
                or disposition
                not in {
                    VenueRejectionDisposition.RETRYABLE_POST_ONLY,
                    VenueRejectionDisposition.RETRYABLE_PRICE_MATCH,
                }
            ):
                raise ValueError("ENTRY_POLICY_RETRY_REJECTION_UNPROVEN")
            fact_refs = tuple(fact.venue_fact_id for fact in facts)
            updated = self._execution.reconcile_execution_action(
                execution_action_id,
                closure_evidence={
                    "order_terminal": True,
                    "terminal_order_status": "REJECTED",
                    "fills_complete": True,
                    "fees_complete": True,
                    "position_effect_known": True,
                    "position_effect": "ZERO",
                    "disposition": disposition.value,
                },
                venue_fact_refs=fact_refs,
                observed_at=observed_at,
            )
        return updated

    def reconcile_cancel_from_target_fact(
        self,
        execution_action_id: str,
        *,
        target_fact: VenueFact,
        observed_at: datetime,
    ) -> ExecutionAction:
        with self._connection.transaction():
            updated = self._execution.reconcile_cancel_from_target_fact(
                execution_action_id,
                target_fact=target_fact,
                observed_at=observed_at,
            )
        fact_activation_id = _outcome_activation_id_for_fact(target_fact)
        self._refresh_completed_reviews_after_commit(
            terminal_actions=(updated,),
            changed_fact_activation_ids=(
                (fact_activation_id,) if fact_activation_id is not None else ()
            ),
            fact_cutoff=observed_at,
            observed_at=observed_at,
        )
        return updated

    def arm_startup_recovery_barrier(self) -> None:
        """Fail closed until startup has enumerated every called action."""

        self._ensure_startup_recovery_fields()
        with self._startup_recovery_lock:
            self._startup_recovery_armed = True
            self._startup_recovery_initialized = False
            self._startup_recovery_pending.clear()
            self._startup_recovery_next_query_at.clear()

    def startup_recovery_complete(self) -> bool:
        self._ensure_startup_recovery_fields()
        with self._startup_recovery_lock:
            return not self._startup_recovery_armed or (
                self._startup_recovery_initialized
                and not self._startup_recovery_pending
            )

    def startup_recovery_pending_action_ids(self) -> tuple[str, ...]:
        self._ensure_startup_recovery_fields()
        with self._startup_recovery_lock:
            return tuple(sorted(self._startup_recovery_pending))

    def startup_recovery_allows_submission(self, activation_id: str) -> bool:
        """Keep only activations with unresolved startup identities fail closed."""

        self._ensure_startup_recovery_fields()
        with self._startup_recovery_lock:
            if not self._startup_recovery_armed:
                return True
            if not self._startup_recovery_initialized:
                return False
            return activation_id not in self._startup_recovery_pending.values()

    def initialize_startup_recovery(
        self,
        *,
        observed_at: datetime,
        resolution_sink: Callable[[str, str], None] | None = None,
    ) -> tuple[ExecutionAction, ...]:
        """Make crash-window actions query-only before Nautilus starts.

        This must run exactly once before ``TradingNode.run`` so reconciliation
        callbacks cannot arrive before the durable pending set exists.
        """

        with self._connection.transaction():
            unresolved = self._execution.prepare_startup_reconciliation(
                observed_at=observed_at,
            )
        self._ensure_startup_recovery_fields()
        with self._startup_recovery_lock:
            self._startup_recovery_armed = True
            self._startup_recovery_initialized = True
            self._startup_recovery_resolution_sink = resolution_sink
            self._startup_recovery_pending = {
                action.execution_action_id: action.activation_id
                for action in unresolved
            }
            self._startup_recovery_next_query_at = {
                action.execution_action_id: observed_at + timedelta(seconds=10)
                for action in unresolved
            }
        return unresolved

    def query_prepared_startup_recovery(
        self,
        *,
        observed_at: datetime,
    ) -> tuple[str, ...]:
        """Query only identities still pending after Nautilus reconciliation.

        Dispatch is deliberately separate from initialization: Nautilus may
        absorb a generated EXTERNAL order during startup, and querying that
        already-resolved identity again must not be required for continuity.
        """

        self._ensure_startup_recovery_fields()
        with self._startup_recovery_lock:
            if not self._startup_recovery_initialized:
                raise RuntimeError("STARTUP_RECOVERY_NOT_INITIALIZED")
            pending_ids = tuple(self._startup_recovery_pending)
            for action_id in pending_ids:
                self._startup_recovery_next_query_at[action_id] = (
                    observed_at + timedelta(seconds=10)
                )
        dispatched: list[str] = []
        for action_id in pending_ids:
            try:
                self._gate.query_original_identity(action_id)
            except Exception as exc:
                # A query transport failure is not evidence about venue state.
                # The action remains barrier-pending for a later same-UUID query.
                LOGGER.warning(
                    "Startup recovery query failed; action remains pending: "
                    "execution_action_id=%s error_type=%s",
                    action_id,
                    type(exc).__name__,
                )
                continue
            dispatched.append(action_id)
        return tuple(dispatched)

    def retry_startup_recovery_queries(
        self,
        *,
        observed_at: datetime,
    ) -> tuple[str, ...]:
        """Retry due read-only queries without changing identity or releasing failure."""

        self.refresh_startup_recovery_state()
        self._ensure_startup_recovery_fields()
        with self._startup_recovery_lock:
            due = tuple(
                action_id
                for action_id, next_query_at in (
                    self._startup_recovery_next_query_at.items()
                )
                if action_id in self._startup_recovery_pending
                and observed_at >= next_query_at
            )
            for action_id in due:
                self._startup_recovery_next_query_at[action_id] = (
                    observed_at + timedelta(seconds=10)
                )
        dispatched: list[str] = []
        for action_id in due:
            try:
                self._gate.query_original_identity(action_id)
            except Exception as exc:
                # A failed read is not authoritative and never opens submission.
                LOGGER.warning(
                    "Startup recovery retry failed; action remains pending: "
                    "execution_action_id=%s error_type=%s",
                    action_id,
                    type(exc).__name__,
                )
                continue
            dispatched.append(action_id)
        return tuple(dispatched)

    def refresh_startup_recovery_state(self) -> tuple[str, ...]:
        """Release actions explicitly closed outside the normal event callback."""

        self._ensure_startup_recovery_fields()
        with self._startup_recovery_lock:
            pending_ids = tuple(self._startup_recovery_pending)
        terminal_ids: list[str] = []
        for action_id in pending_ids:
            try:
                action = self._action_repository.get(action_id)
            except Exception as exc:
                LOGGER.warning(
                    "Startup recovery state refresh failed; action remains pending: "
                    "execution_action_id=%s error_type=%s",
                    action_id,
                    type(exc).__name__,
                )
                continue
            if action.state in {
                ExecutionActionState.NOT_SUBMITTED,
                ExecutionActionState.CLOSED,
                ExecutionActionState.HANDED_OVER,
            }:
                terminal_ids.append(action_id)
        self._resolve_startup_recovery_actions(tuple(terminal_ids))
        return tuple(terminal_ids)

    def _resolve_startup_recovery_actions(
        self,
        action_ids: tuple[str, ...],
    ) -> None:
        if not action_ids:
            return
        self._ensure_startup_recovery_fields()
        resolved: list[tuple[str, str]] = []
        with self._startup_recovery_lock:
            for action_id in action_ids:
                activation_id = self._startup_recovery_pending.pop(action_id, None)
                self._startup_recovery_next_query_at.pop(action_id, None)
                if activation_id is not None:
                    resolved.append((activation_id, action_id))
            sink = self._startup_recovery_resolution_sink
        if sink is not None:
            for activation_id, action_id in resolved:
                sink(activation_id, action_id)

    def _ensure_startup_recovery_fields(self) -> None:
        # Several narrow unit tests construct the coordinator without __init__.
        if not hasattr(self, "_startup_recovery_lock"):
            self._startup_recovery_lock = RLock()
            self._startup_recovery_armed = False
            self._startup_recovery_initialized = False
            self._startup_recovery_pending = {}
            self._startup_recovery_next_query_at = {}
            self._startup_recovery_resolution_sink = None

    def _ensure_venue_mutation_fields(self) -> None:
        # Several narrow unit tests construct the coordinator without __init__.
        if not hasattr(self, "_venue_mutation_lock"):
            self._venue_mutation_lock = RLock()
            self._venue_mutations_enabled = True

    def disable_venue_mutations(self) -> None:
        """Make a maintenance stop a final in-process exchange-write boundary."""

        self._ensure_venue_mutation_fields()
        with self._venue_mutation_lock:
            self._venue_mutations_enabled = False

    def query_unknown_action_if_due(
        self,
        execution_action_id: str,
        *,
        observed_at: datetime,
    ) -> bool:
        """Query one unresolved action by its original UUID at a limited rate."""

        with self._connection.transaction():
            action = self._execution.prepare_due_unknown_query(
                execution_action_id,
                next_query_at=observed_at + timedelta(seconds=10),
                observed_at=observed_at,
            )
        if action is None:
            return False
        try:
            self._gate.query_original_identity(action.execution_action_id)
        except Exception as exc:
            # Query transport failure does not change the unresolved responsibility.
            LOGGER.warning(
                "Unknown-action query failed; responsibility remains unresolved: "
                "execution_action_id=%s error_type=%s",
                execution_action_id,
                type(exc).__name__,
            )
        return True

    def query_called_action_identity(self, execution_action_id: str) -> bool:
        """Issue one read-only query for a called action without changing identity."""

        action = self._action_repository.get(execution_action_id)
        if action.state not in {
            ExecutionActionState.SUBMITTING,
            ExecutionActionState.UNKNOWN,
            ExecutionActionState.OPEN,
        }:
            return False
        try:
            self._gate.query_original_identity(execution_action_id)
        except Exception as exc:
            # An asynchronous query and a transport failure are both resolved
            # only by a later authoritative callback.  Neither permits a write.
            LOGGER.warning(
                "Called-action query failed; responsibility remains unresolved: "
                "execution_action_id=%s error_type=%s",
                execution_action_id,
                type(exc).__name__,
            )
        return True

    def build_nautilus_event_normalizer(
        self,
        *,
        leaves_quantity_for_client_order_id: Callable[[str], str | None] | None = None,
        filled_quantity_for_client_order_id: Callable[[str], str | None] | None = None,
        order_quantity_for_client_order_id: Callable[[str], str | None] | None = None,
        query_was_recently_dispatched: Callable[[str], bool] | None = None,
    ) -> NautilusExecutionEventNormalizer:
        return NautilusExecutionEventNormalizer(
            self._action_repository.find_order_action_by_client_id,
            environment_id=self._environment_id,
            action_for_venue_order_ref=(
                self._action_repository.find_order_action_by_venue_order_ref
            ),
            venue_ref=self._venue_ref,
            leaves_quantity_for_client_order_id=leaves_quantity_for_client_order_id,
            filled_quantity_for_client_order_id=filled_quantity_for_client_order_id,
            order_quantity_for_client_order_id=order_quantity_for_client_order_id,
            cancel_action_for_target=self._action_repository.find_open_cancel_for_target,
            query_was_recently_dispatched=query_was_recently_dispatched,
        )

    def handle_nautilus_order_event(
        self,
        normalizer: NautilusExecutionEventNormalizer,
        event: object,
        *,
        observed_at: datetime,
    ) -> NormalizedNautilusEvent:
        """Persist callback facts and return only newly inserted canonical facts."""

        resolved_action_ids: list[str] = []
        review_terminal_actions: list[ExecutionAction] = []
        review_fact_activation_ids: list[str] = []
        inserted_facts: list[VenueFact] = []
        unattributed_inserted_facts: list[VenueFact] = []
        authoritative_action_fact_observed = False
        with self._connection.transaction():
            normalized = normalizer.normalize(event, received_at=observed_at)
            action = normalized.action
            if normalized.definitely_not_submitted and action is not None:
                denied = self._execution.record_definitely_not_submitted(
                    action.execution_action_id,
                    reason_code="NAUTILUS_ORDER_DENIED",
                    observed_at=observed_at,
                )
                if denied.action_kind is ExecutionActionKind.PROTECTION:
                    self._refresh_protection_projection(
                        activation_id=denied.activation_id,
                        observed_at=observed_at,
                    )
                review_terminal_actions.append(denied)
                resolved_action_ids.append(action.execution_action_id)
            elif normalized.result_unknown and action is not None:
                retry_delay_seconds = max(
                    10.0,
                    normalized.retry_after_seconds or 0.0,
                )
                self._execution.record_submission_unknown(
                    action.execution_action_id,
                    reason=normalized.unknown_reason or "VENUE_RESULT_UNKNOWN",
                    next_query_at=observed_at
                    + timedelta(seconds=retry_delay_seconds),
                    observed_at=observed_at,
                )
                if normalized.retry_after_seconds is not None:
                    self._ensure_startup_recovery_fields()
                    with self._startup_recovery_lock:
                        if (
                            action.execution_action_id
                            in self._startup_recovery_pending
                        ):
                            candidate = observed_at + timedelta(
                                seconds=retry_delay_seconds
                            )
                            current = self._startup_recovery_next_query_at.get(
                                action.execution_action_id
                            )
                            self._startup_recovery_next_query_at[
                                action.execution_action_id
                            ] = (
                                candidate
                                if current is None
                                else max(current, candidate)
                            )
            else:
                for fact in normalized.facts:
                    application = self._execution.apply_venue_fact_with_result(
                        fact=fact,
                        observed_at=observed_at,
                    )
                    if (
                        action is not None
                        and application.action is not None
                        and application.action.execution_action_id
                        == action.execution_action_id
                    ):
                        authoritative_action_fact_observed = True
                    if not application.inserted:
                        continue
                    fact = application.canonical_fact
                    inserted_facts.append(fact)
                    if (
                        application.action is None
                        and getattr(fact, "attribution_class", None) is None
                        and _unattributed_fact_requires_account_stop(
                            fact,
                            reconciliation_not_before=getattr(
                                self,
                                "_unattributed_reconciliation_not_before",
                                None,
                            ),
                        )
                    ):
                        unattributed_inserted_facts.append(fact)
                    updated = application.action
                    if updated is not None:
                        review_fact_activation_ids.append(updated.activation_id)
                        late_entry_fill = (
                            fact.kind is VenueFactKind.FILL
                            and updated.action_kind is ExecutionActionKind.ENTRY
                            and updated.state is not ExecutionActionState.OPEN
                        )
                        if application.action_quantity_conflict or late_entry_fill:
                            # Retain the authoritative, action-bound fact.
                            # Quantity drift or a terminal-late entry fill stops
                            # new risk without pretending that a known Halpha
                            # order is unrelated external account activity.
                            self._capital.stop_new_risk_for_attributed_action_anomaly(
                                stop_state_version_id=str(uuid4()),
                                environment_kind=EnvironmentKind(
                                    self._environment_kind
                                ),
                                authority_class=AuthorityClass(self._authority_class),
                                account_ref=self._account_ref,
                                evidence_digest=fact.content_digest,
                                observed_at=observed_at,
                            )
                        if application.action_quantity_conflict:
                            LOGGER.error(
                                "Venue action quantity conflict retained: "
                                "activation_id=%s action_id=%s fact_id=%s",
                                updated.activation_id,
                                updated.execution_action_id,
                                fact.venue_fact_id,
                            )
                        if not late_entry_fill:
                            self._apply_protection_projection_from_fact(
                                action=updated,
                                fact=fact,
                                observed_at=observed_at,
                            )
                    elif (
                        fact_activation_id := _outcome_activation_id_for_fact(fact)
                    ) is not None:
                        review_fact_activation_ids.append(fact_activation_id)
                    if (
                        terminal_order_status((fact,)) is not None
                        and normalized.client_order_id is not None
                    ):
                        cancel_actions = (
                            self._action_repository.list_open_cancels_for_target(
                                normalized.client_order_id
                            )
                        )
                        for cancel_action in cancel_actions:
                            reconciled = (
                                self._execution.reconcile_cancel_from_target_fact(
                                    cancel_action.execution_action_id,
                                    target_fact=fact,
                                    observed_at=observed_at,
                                )
                            )
                            review_terminal_actions.append(reconciled)
                            resolved_action_ids.append(
                                cancel_action.execution_action_id
                            )
                if action is not None and authoritative_action_fact_observed:
                    # A venue-backed fact is the completion signal for the exact
                    # startup query. It may already be persisted from a prior
                    # callback; only downstream propagation requires insertion.
                    # OrderSubmitted alone intentionally is not sufficient.
                    resolved_action_ids.append(action.execution_action_id)
                if action is None and unattributed_inserted_facts:
                    evidence_digest = content_digest(
                        tuple(
                            fact.content_digest for fact in unattributed_inserted_facts
                        )
                    )
                    self._capital.stop_new_risk_for_external_activity(
                        stop_state_version_id=str(uuid4()),
                        environment_kind=EnvironmentKind(self._environment_kind),
                        authority_class=AuthorityClass(self._authority_class),
                        account_ref=self._account_ref,
                        evidence_digest=evidence_digest,
                        observed_at=observed_at,
                    )
        # Never open the barrier before the fact transaction has committed.
        self._resolve_startup_recovery_actions(tuple(resolved_action_ids))
        self._refresh_completed_reviews_after_commit(
            terminal_actions=tuple(review_terminal_actions),
            changed_fact_activation_ids=tuple(review_fact_activation_ids),
            fact_cutoff=observed_at,
            observed_at=observed_at,
        )
        propagated_facts = tuple(inserted_facts)
        if propagated_facts == normalized.facts:
            return normalized
        return replace(normalized, facts=propagated_facts)

    def consume_strategy_proposal(
        self,
        *,
        plan_event_id: str,
        execution_action_id: str,
        proposal: StrategyProposal,
        action_check: ActionCheckInput,
        created_at: datetime,
        client_order_id: str | None = None,
    ) -> CoordinatedProposalResult:
        """Commit PlanEvent and accepted ExecutionAction in one DB transaction."""

        if not self.startup_recovery_allows_submission(proposal.activation_id):
            raise RuntimeError("STARTUP_RECOVERY_PENDING")
        if (
            self._environment_kind == "LIVE"
            and proposal.activation_id not in self._live_write_activation_ids
        ):
            raise RuntimeError("LIVE_WRITE_ACTIVATION_SCOPE_MISMATCH")
        self._require_current_live_write_gate(proposal.activation_id)
        with self._connection.transaction():
            # The activation lock serializes entry creation. An unknown order is
            # already an open responsibility even before a fill reaches TRADEPLAN.
            self._planning.get_activation(proposal.activation_id, for_update=True)
            event = self._planning.consume_strategy_proposal(
                plan_event_id=plan_event_id,
                proposal=proposal,
                action_check=action_check,
                entry_responsibility_open=(
                    self._action_repository.has_open_entry_responsibility(
                        proposal.activation_id
                    )
                ),
                created_at=created_at,
            )
            if not bool(event.capital_decision.get("accepted")):
                return CoordinatedProposalResult(event, None)
            action = self._execution.create_execution_action(
                execution_action_id=execution_action_id,
                plan_event=event,
                observed_at=created_at,
                client_order_id=client_order_id,
            )
            return CoordinatedProposalResult(event, action)

    def process_execution_action(
        self,
        execution_action_id: str,
        *,
        action_check: ActionCheckInput,
        request_payload: dict[str, Any],
        observed_at: datetime,
    ) -> ProcessExecutionResult:
        """Serialize owner control against one final venue mutation attempt."""

        action = self._action_repository.get(execution_action_id)
        with serialize_activation_dispatch(
            self._connection,
            environment_id=action.environment_id,
            activation_id=action.activation_id,
        ):
            return self._process_execution_action_serialized(
                execution_action_id,
                action_check=action_check,
                request_payload=request_payload,
                observed_at=observed_at,
            )

    def _process_execution_action_serialized(
        self,
        execution_action_id: str,
        *,
        action_check: ActionCheckInput,
        request_payload: dict[str, Any],
        observed_at: datetime,
    ) -> ProcessExecutionResult:
        """Persist SUBMITTING, call once, then normalize while holding the lock."""

        action = self._action_repository.get(execution_action_id)
        if (
            action.action_class is RiskClass.RISK_INCREASING
            and not self.startup_recovery_allows_submission(action.activation_id)
        ):
            raise RuntimeError("STARTUP_RECOVERY_PENDING")
        with self._connection.transaction():
            action = self._action_repository.get(execution_action_id, for_update=True)
            if (
                self._environment_kind == "LIVE"
                and action.activation_id not in self._live_write_activation_ids
            ):
                raise RuntimeError("LIVE_WRITE_ACTIVATION_SCOPE_MISMATCH")
            if (
                self._environment_kind == "LIVE"
                and action.action_class is RiskClass.RISK_INCREASING
            ):
                if self._runtime_real_write_gate != "OPEN":
                    raise RuntimeError("RUNTIME_REAL_WRITE_GATE_CLOSED")
                self._require_current_live_write_gate(action.activation_id)
            activation = self._planning.get_activation(
                action.activation_id, for_update=True
            )
            block_reason = _submission_block_reason(action, activation)
            if block_reason is not None:
                raise RuntimeError(block_reason)
            self._validate_action_check(action, action_check, activation)
            decision = self._capital.check_current_action(action_check)
            if not decision.accepted:
                if decision.reason_code in {"VALUATION_UNKNOWN", "ATTRIBUTION_UNKNOWN"}:
                    return ProcessExecutionResult(
                        action,
                        venue_called=False,
                        reason_code=decision.reason_code,
                    )
                rejected = self._execution.record_definitely_not_submitted(
                    execution_action_id,
                    reason_code=decision.reason_code,
                    observed_at=observed_at,
                )
                return ProcessExecutionResult(
                    rejected,
                    venue_called=False,
                    reason_code=decision.reason_code,
                )
            if action.action_class is RiskClass.RISK_INCREASING:
                discipline = self._capital.new_risk_discipline_status(
                    account_ref=action.account_ref,
                    policy=self._new_risk_discipline_policy,
                    observed_at=observed_at,
                    entry_instrument_ref=action.action_terms.get("instrument_ref"),
                    entry_direction=action.action_terms.get("direction"),
                    is_frozen_scale_in=_is_frozen_planned_scale_in_action(
                        action,
                        activation,
                    ),
                    lock=True,
                )
                if not discipline.new_risk_allowed:
                    reason_code = (
                        discipline.blocker_codes[0]
                        if discipline.blocker_codes
                        else "NEW_RISK_DISCIPLINE_UNKNOWN"
                    )
                    rejected = self._execution.record_definitely_not_submitted(
                        execution_action_id,
                        reason_code=reason_code,
                        observed_at=observed_at,
                    )
                    return ProcessExecutionResult(
                        rejected,
                        venue_called=False,
                        reason_code=reason_code,
                    )
            prepared = self._execution.prepare_submission(
                execution_action_id,
                capital_decision=decision,
                request_payload=request_payload,
                observed_at=observed_at,
            )

        if prepared.action_class is RiskClass.RISK_INCREASING:
            try:
                self._require_current_live_write_gate(prepared.activation_id)
            except RuntimeError:
                with self._connection.transaction():
                    not_submitted = self._execution.record_definitely_not_submitted(
                        prepared.execution_action_id,
                        reason_code="RUNTIME_REAL_WRITE_GATE_CLOSED",
                        observed_at=observed_at,
                    )
                return ProcessExecutionResult(
                    not_submitted,
                    venue_called=False,
                    reason_code="RUNTIME_REAL_WRITE_GATE_CLOSED",
                )

        self._ensure_venue_mutation_fields()
        with self._venue_mutation_lock:
            if not self._venue_mutations_enabled:
                with self._connection.transaction():
                    not_submitted = self._execution.record_definitely_not_submitted(
                        prepared.execution_action_id,
                        reason_code="MAINTENANCE_STOP",
                        observed_at=observed_at,
                    )
                return ProcessExecutionResult(
                    not_submitted,
                    venue_called=False,
                    reason_code="MAINTENANCE_STOP",
                )
            permit = self._gate.authorize_committed_submission(
                prepared.execution_action_id,
                expected_state_digest=prepared.state_digest,
            )
            try:
                receipt = self._gate.execute_once(permit)
            except VenueDefinitelyNotSubmitted:
                with self._connection.transaction():
                    not_submitted = self._execution.record_definitely_not_submitted(
                        prepared.execution_action_id,
                        reason_code="VENUE_CLIENT_DEFINITELY_NOT_SUBMITTED",
                        observed_at=observed_at,
                    )
                return ProcessExecutionResult(
                    not_submitted,
                    venue_called=False,
                    reason_code="NOT_SUBMITTED",
                )
            except Exception as exc:
                # All unclassified client failures are uncertain: error type is not
                # treated as proof that the stable identity never reached the venue.
                with self._connection.transaction():
                    unknown = self._execution.record_submission_unknown(
                        prepared.execution_action_id,
                        reason=f"VENUE_CALL_UNCERTAIN:{type(exc).__name__}",
                        next_query_at=observed_at + timedelta(seconds=10),
                        observed_at=observed_at,
                    )
                return ProcessExecutionResult(
                    unknown,
                    venue_called=True,
                    reason_code="SUBMISSION_RESULT_UNKNOWN",
                )

            fact = build_venue_fact(
                venue_fact_id=str(uuid4()),
                environment_id=self._environment_id,
                venue_ref=self._venue_ref,
                account_ref=self._account_ref,
                instrument_ref=str(prepared.action_terms["instrument_ref"]),
                kind=VenueFactKind.ORDER_STATE,
                source_class=VenueFactSourceClass.VENUE_QUERY,
                source_object_id=receipt.source_object_id,
                source_sequence=receipt.source_sequence,
                source_time=receipt.source_time,
                received_at=observed_at,
                cutoff=observed_at,
                payload={**receipt.payload, "status": receipt.status},
                action=prepared,
            )
            with self._connection.transaction():
                updated = self._execution.apply_venue_fact(
                    fact=fact,
                    observed_at=observed_at,
                )
        if updated is None:
            raise RuntimeError("VENUE_FACT_ATTRIBUTION_INVALID")
        self._refresh_completed_reviews_after_commit(
            changed_fact_activation_ids=(updated.activation_id,),
            fact_cutoff=observed_at,
            observed_at=observed_at,
        )
        return ProcessExecutionResult(
            updated,
            venue_called=True,
            reason_code=f"VENUE_{updated.state.value}",
        )

    def _require_current_live_write_gate(self, activation_id: str) -> None:
        if self._environment_kind != "LIVE":
            return
        guard = self._live_write_submission_guard
        if guard is None:
            raise RuntimeError("RUNTIME_REAL_WRITE_GATE_CLOSED")
        try:
            guard(activation_id)
        except Exception:
            raise RuntimeError("RUNTIME_REAL_WRITE_GATE_CLOSED") from None

    def apply_venue_fact(
        self,
        fact: VenueFact,
        *,
        observed_at: datetime,
    ) -> ExecutionAction | None:
        """Apply one DAT fact and update only TRADEPLAN's protection projection."""

        review_terminal_actions: list[ExecutionAction] = []
        with self._connection.transaction():
            updated = self._execution.apply_venue_fact(
                fact=fact,
                observed_at=observed_at,
            )
            if updated is not None:
                self._apply_protection_projection_from_fact(
                    action=updated,
                    fact=fact,
                    observed_at=observed_at,
                )
            if terminal_order_status((fact,)) is not None:
                target_client_order_id = fact.payload.get("client_order_id")
                if isinstance(target_client_order_id, str):
                    cancel_actions = (
                        self._action_repository.list_open_cancels_for_target(
                            target_client_order_id
                        )
                    )
                    for cancel_action in cancel_actions:
                        reconciled = self._execution.reconcile_cancel_from_target_fact(
                            cancel_action.execution_action_id,
                            target_fact=fact,
                            observed_at=observed_at,
                        )
                        review_terminal_actions.append(reconciled)
        fact_activation_id = (
            updated.activation_id
            if updated is not None
            else _outcome_activation_id_for_fact(fact)
        )
        self._refresh_completed_reviews_after_commit(
            terminal_actions=tuple(review_terminal_actions),
            changed_fact_activation_ids=(
                (fact_activation_id,) if fact_activation_id is not None else ()
            ),
            fact_cutoff=observed_at,
            observed_at=observed_at,
        )
        return updated

    def create_protection_for_fill(
        self,
        *,
        fill_fact: VenueFact,
        plan_event_id: str,
        execution_action_id: str,
        action_check: ActionCheckInput,
        observed_at: datetime,
        client_order_id: str | None = None,
    ) -> CoordinatedProposalResult:
        """Persist a confirmed fill and its explicit protection in one transaction."""

        if fill_fact.kind is not VenueFactKind.FILL or fill_fact.action_ref is None:
            raise ValueError("PROTECTION_UNKNOWN")
        with self._connection.transaction():
            entry_action = self._execution.apply_venue_fact(
                fact=fill_fact,
                observed_at=observed_at,
            )
            if (
                entry_action is None
                or entry_action.action_kind is not ExecutionActionKind.ENTRY
            ):
                raise ValueError("VENUE_FACT_ATTRIBUTION_INVALID")
            execution_context = entry_action.action_terms.get("execution_context", {})
            if not isinstance(execution_context, dict):
                raise ValueError("PROTECTION_UNKNOWN")
            context = execution_context.get("entry_risk_context")
            direct_policy = execution_context.get("protection_policy")
            schedule_context = execution_context.get("order_schedule")
            fill_price = fill_fact.payload.get("last_price")
            fill_quantity = fill_fact.payload.get("last_quantity")
            if not isinstance(fill_price, str) or not isinstance(fill_quantity, str):
                raise ValueError("PROTECTION_UNKNOWN")
            fill_time = fill_fact.source_time or fill_fact.cutoff
            if isinstance(context, dict):
                activation = self._planning.record_first_fill(
                    activation_id=entry_action.activation_id,
                    entry_action_ref=entry_action.execution_action_id,
                    fill_fact_ref=fill_fact.venue_fact_id,
                    fill_price=fill_price,
                    fill_time=fill_time,
                    entry_risk_context=context,
                    observed_at=observed_at,
                )
            elif isinstance(direct_policy, dict) and isinstance(
                schedule_context,
                dict,
            ):
                price_tick_size = schedule_context.get("price_tick_size")
                quantity_step = schedule_context.get("quantity_step")
                if not isinstance(price_tick_size, str) or not isinstance(
                    quantity_step,
                    str,
                ):
                    raise ValueError("PROTECTION_UNKNOWN")
                activation = self._planning.record_direct_fill(
                    activation_id=entry_action.activation_id,
                    entry_action_ref=entry_action.execution_action_id,
                    fill_fact_ref=fill_fact.venue_fact_id,
                    fill_price=fill_price,
                    fill_quantity=fill_quantity,
                    fill_time=fill_time,
                    protection_policy=direct_policy,
                    price_tick_size=price_tick_size,
                    quantity_step=quantity_step,
                    observed_at=observed_at,
                )
            else:
                raise ValueError("PROTECTION_UNKNOWN")
            source_identity = venue_source_identity(
                activation_id=activation.activation_id,
                rule_id="PROTECTION_AFTER_FILL",
                source_class=fill_fact.source_class.value,
                source_object_id=fill_fact.source_object_id,
                source_sequence_or_version=fill_fact.source_sequence,
            )
            if activation.protection_state is ProtectionState.GAP:
                event = self._record_protection_gap_event(
                    plan_event_id=plan_event_id,
                    activation=activation,
                    source_identity=source_identity,
                    fill_fact=fill_fact,
                    reason_code="PROTECTION_GAP_ALREADY_PRESENT",
                    observed_at=observed_at,
                )
                return CoordinatedProposalResult(event, None)
            try:
                proposed = (
                    proposed_protection_from_fill(
                        activation,
                        entry_action_ref=entry_action.execution_action_id,
                        fill_fact_ref=fill_fact.venue_fact_id,
                        fill_source_identity=source_identity,
                        fill_quantity=fill_quantity,
                    )
                    if isinstance(context, dict)
                    else proposed_direct_protection_from_fill(
                        activation,
                        entry_action_ref=entry_action.execution_action_id,
                        fill_fact_ref=fill_fact.venue_fact_id,
                        fill_source_identity=source_identity,
                    )
                )
            except ValueError as exc:
                reason_code = str(exc)
                if reason_code not in {
                    "PROTECTION_GAP",
                    "PROTECTION_PRICE_INVALID",
                }:
                    raise
                event = self._record_protection_gap_event(
                    plan_event_id=plan_event_id,
                    activation=activation,
                    source_identity=source_identity,
                    fill_fact=fill_fact,
                    reason_code=reason_code,
                    observed_at=observed_at,
                )
                self._planning.update_protection_projection(
                    activation_id=activation.activation_id,
                    protection_state=ProtectionState.GAP,
                    pending_action_digest=None,
                    observed_at=observed_at,
                )
                return CoordinatedProposalResult(event, None)
            event, action = self._record_proposed_action(
                plan_event_id=plan_event_id,
                execution_action_id=execution_action_id,
                activation_id=activation.activation_id,
                rule_id="PROTECTION_AFTER_FILL",
                source_identity=source_identity,
                source_cutoff=fill_fact.cutoff,
                input_digest=fill_fact.content_digest,
                proposed_action=proposed,
                action_check=action_check,
                observed_at=observed_at,
                client_order_id=client_order_id,
            )
            if action is None:
                self._planning.update_protection_projection(
                    activation_id=activation.activation_id,
                    protection_state=ProtectionState.GAP,
                    pending_action_digest=None,
                    observed_at=observed_at,
                )
            else:
                self._planning.update_protection_projection(
                    activation_id=activation.activation_id,
                    protection_state=ProtectionState.UNKNOWN,
                    pending_action_digest=action.state_digest,
                    observed_at=observed_at,
                )
            return CoordinatedProposalResult(event, action)

    def create_direct_protection_replacement(
        self,
        *,
        activation_id: str,
        predecessor_action_id: str,
        target_trigger_price: str,
        step_index: int,
        replacement_kind: str,
        plan_event_id: str,
        execution_action_id: str,
        action_check: ActionCheckInput,
        observed_at: datetime,
        client_order_id: str,
    ) -> CoordinatedProposalResult:
        """Persist one typed stop replacement with its safe hand-off metadata."""

        with self._connection.transaction():
            activation = self._planning.get_activation(
                activation_id,
                for_update=True,
            )
            predecessor = self._action_repository.get(
                predecessor_action_id,
                for_update=True,
            )
            if (
                predecessor.activation_id != activation.activation_id
                or predecessor.action_kind is not ExecutionActionKind.PROTECTION
                or predecessor.state is not ExecutionActionState.OPEN
                or not order_is_working(
                    self._fact_repository.list_for_action(
                        predecessor.execution_action_id
                    )
                )
            ):
                raise ValueError("DYNAMIC_PROTECTION_PREDECESSOR_NOT_WORKING")
            context = predecessor.action_terms.get("execution_context")
            quantity = predecessor.action_terms.get("quantity")
            trigger_price = predecessor.action_terms.get("trigger_price")
            if (
                not isinstance(context, dict)
                or not isinstance(quantity, str)
                or not isinstance(trigger_price, str)
                or not all(
                    isinstance(context.get(key), str)
                    for key in (
                        "entry_action_ref",
                        "fill_fact_ref",
                        "fill_source_identity",
                    )
                )
            ):
                raise ValueError("PROTECTION_UNKNOWN")
            proposed = proposed_direct_protection_replacement(
                activation,
                predecessor_action_ref=predecessor.execution_action_id,
                predecessor_trigger_price=trigger_price,
                entry_action_ref=str(context["entry_action_ref"]),
                fill_fact_ref=str(context["fill_fact_ref"]),
                fill_source_identity=str(context["fill_source_identity"]),
                fill_quantity=quantity,
                target_trigger_price=target_trigger_price,
                step_index=step_index,
                replacement_kind=replacement_kind,
            )
            source_identity = (
                f"{activation.activation_id}:{replacement_kind}:"
                f"{context['fill_fact_ref']}:{step_index}"
            )
            event, action = self._record_proposed_action(
                plan_event_id=plan_event_id,
                execution_action_id=execution_action_id,
                activation_id=activation.activation_id,
                rule_id=replacement_kind,
                source_identity=source_identity,
                source_cutoff=observed_at,
                input_digest=proposed.causation_ref,
                proposed_action=proposed,
                action_check=action_check,
                observed_at=observed_at,
                client_order_id=client_order_id,
            )
            return CoordinatedProposalResult(event, action)

    def _record_protection_gap_event(
        self,
        *,
        plan_event_id: str,
        activation: PlanActivation,
        source_identity: str,
        fill_fact: VenueFact,
        reason_code: str,
        observed_at: datetime,
    ) -> PlanEvent:
        """Append an auditable no-action result for an unprotectable fill."""

        return self._planning.record_plan_event(
            plan_event_id=plan_event_id,
            activation_id=activation.activation_id,
            rule_id="PROTECTION_AFTER_FILL",
            source_identity=source_identity,
            source_cutoff=fill_fact.cutoff,
            input_digest=fill_fact.content_digest,
            reason_code=reason_code,
            proposed_action=None,
            no_action_reason=reason_code,
            condition_judgement=ConditionJudgement(
                rule_id="PROTECTION_AFTER_FILL",
                source_identity=source_identity,
                source_cutoff=fill_fact.cutoff,
                input_digest=fill_fact.content_digest,
                result=ConditionResult.UNKNOWN,
                reason_code=reason_code,
                next_responsibility="NONE",
            ),
            capital_decision={
                "accepted": False,
                "reason_code": f"NOT_EVALUATED_{reason_code}",
            },
            created_at=observed_at,
        )

    def create_take_profits_for_protected_fill(
        self,
        *,
        protection_action_id: str,
        fill_fact_ref: str,
        fill_source_identity: str,
        fill_quantity: str,
        plan_event_ids: tuple[str, str],
        execution_action_ids: tuple[str, str],
        action_checks: tuple[ActionCheckInput, ActionCheckInput],
        observed_at: datetime,
        client_order_ids: tuple[str | None, str | None] = (None, None),
    ) -> tuple[CoordinatedProposalResult, CoordinatedProposalResult]:
        with self._connection.transaction():
            protection = self._action_repository.get(
                protection_action_id,
                for_update=True,
            )
            if (
                protection.action_kind is not ExecutionActionKind.PROTECTION
                or not order_is_working(
                    self._fact_repository.list_for_action(protection_action_id)
                )
            ):
                raise ValueError("PROTECTION_UNKNOWN")
            activation = self._planning.get_activation(
                protection.activation_id,
                for_update=True,
            )
            entry_action_ref = protection.action_terms.get("execution_context", {}).get(
                "entry_action_ref"
            )
            if not isinstance(entry_action_ref, str):
                raise ValueError("PROTECTION_UNKNOWN")
            proposed_actions = proposed_take_profits_from_fill(
                activation,
                entry_action_ref=entry_action_ref,
                protection_action_ref=protection.execution_action_id,
                fill_fact_ref=fill_fact_ref,
                fill_source_identity=fill_source_identity,
                fill_quantity=fill_quantity,
            )
            results = []
            for index, (proposed, event_id, action_id, check, client_id) in enumerate(
                zip(
                    proposed_actions,
                    plan_event_ids,
                    execution_action_ids,
                    action_checks,
                    client_order_ids,
                    strict=True,
                ),
                start=1,
            ):
                source_identity = f"{fill_source_identity}:TAKE_PROFIT_{index}"
                event, action = self._record_proposed_action(
                    plan_event_id=event_id,
                    execution_action_id=action_id,
                    activation_id=activation.activation_id,
                    rule_id=f"TAKE_PROFIT_{index}_AFTER_PROTECTION",
                    source_identity=source_identity,
                    source_cutoff=observed_at,
                    input_digest=proposed.causation_ref,
                    proposed_action=proposed,
                    action_check=check,
                    observed_at=observed_at,
                    client_order_id=client_id,
                )
                results.append(CoordinatedProposalResult(event, action))
            return results[0], results[1]

    def create_direct_take_profits_for_protected_fill(
        self,
        *,
        protection_action_id: str,
        fill_fact_ref: str,
        fill_source_identity: str,
        plan_event_ids: tuple[str, ...],
        execution_action_ids: tuple[str, ...],
        action_checks: tuple[ActionCheckInput, ...],
        observed_at: datetime,
        client_order_ids: tuple[str | None, ...],
    ) -> tuple[CoordinatedProposalResult, ...]:
        """Persist an arbitrary bounded direct TP ladder in one transaction."""

        with self._connection.transaction():
            protection = self._action_repository.get(
                protection_action_id,
                for_update=True,
            )
            if (
                protection.action_kind is not ExecutionActionKind.PROTECTION
                or not order_is_working(
                    self._fact_repository.list_for_action(protection_action_id)
                )
            ):
                raise ValueError("PROTECTION_UNKNOWN")
            activation = self._planning.get_activation(
                protection.activation_id,
                for_update=True,
            )
            context = protection.action_terms.get("execution_context", {})
            entry_action_ref = context.get("entry_action_ref")
            if not isinstance(entry_action_ref, str) or not isinstance(
                context.get("direct_fill"),
                dict,
            ):
                raise ValueError("PROTECTION_UNKNOWN")
            proposed_actions = proposed_direct_take_profits_from_fill(
                activation,
                entry_action_ref=entry_action_ref,
                protection_action_ref=protection.execution_action_id,
                fill_fact_ref=fill_fact_ref,
                fill_source_identity=fill_source_identity,
            )
            if not (
                len(proposed_actions)
                == len(plan_event_ids)
                == len(execution_action_ids)
                == len(action_checks)
                == len(client_order_ids)
            ):
                raise ValueError("TAKE_PROFIT_RESPONSIBILITY_COUNT_MISMATCH")
            results: list[CoordinatedProposalResult] = []
            for index, (proposed, event_id, action_id, check, client_id) in enumerate(
                zip(
                    proposed_actions,
                    plan_event_ids,
                    execution_action_ids,
                    action_checks,
                    client_order_ids,
                    strict=True,
                ),
                start=1,
            ):
                source_identity = f"{fill_source_identity}:DIRECT_TAKE_PROFIT_{index}"
                event, action = self._record_proposed_action(
                    plan_event_id=event_id,
                    execution_action_id=action_id,
                    activation_id=activation.activation_id,
                    rule_id=f"DIRECT_TAKE_PROFIT_{index}_AFTER_PROTECTION",
                    source_identity=source_identity,
                    source_cutoff=observed_at,
                    input_digest=proposed.causation_ref,
                    proposed_action=proposed,
                    action_check=check,
                    observed_at=observed_at,
                    client_order_id=client_id,
                )
                results.append(CoordinatedProposalResult(event, action))
            return tuple(results)

    def create_direct_take_profit_replacement(
        self,
        *,
        activation_id: str,
        predecessor_action_id: str,
        target_trigger_price: str,
        aggregate_revision: int,
        plan_event_id: str,
        execution_action_id: str,
        action_check: ActionCheckInput,
        observed_at: datetime,
        client_order_id: str,
    ) -> CoordinatedProposalResult | None:
        """Replace one cancelled aggregate TP using its proven remainder."""

        with self._connection.transaction():
            activation = self._planning.get_activation(
                activation_id,
                for_update=True,
            )
            predecessor = self._action_repository.get(
                predecessor_action_id,
                for_update=True,
            )
            facts = self._fact_repository.list_for_action(
                predecessor.execution_action_id
            )
            definitely_not_submitted = (
                predecessor.state is ExecutionActionState.NOT_SUBMITTED
                and predecessor.not_submitted_reason is not None
            )
            venue_terminal = (
                predecessor.state is ExecutionActionState.CLOSED
                and terminal_order_status(facts) in {"CANCELLED", "EXPIRED"}
                and terminal_fills_complete(predecessor, facts)
            )
            if (
                predecessor.activation_id != activation.activation_id
                or predecessor.action_kind is not ExecutionActionKind.TAKE_PROFIT
                or not (definitely_not_submitted or venue_terminal)
            ):
                raise ValueError("TAKE_PROFIT_REPLACEMENT_PREDECESSOR_UNRESOLVED")
            try:
                requested = Decimal(str(predecessor.action_terms["quantity"]))
                filled = (
                    Decimal(0)
                    if definitely_not_submitted
                    else sum(
                        (
                            Decimal(str(fact.payload["last_quantity"]))
                            for fact in collapse_synthetic_reconciliation_fills(facts)
                            if fact.kind is VenueFactKind.FILL
                        ),
                        Decimal(0),
                    )
                )
            except (KeyError, TypeError, ValueError):
                raise ValueError("TAKE_PROFIT_REPLACEMENT_QUANTITY_INVALID") from None
            remaining = requested - filled
            if remaining < 0:
                raise ValueError("TAKE_PROFIT_REPLACEMENT_QUANTITY_INVALID")
            if remaining == 0:
                return None
            normalized_remaining = canonical_decimal(remaining)
            if Decimal(action_check.quantized_quantity) != remaining:
                raise ValueError("TAKE_PROFIT_REPLACEMENT_CHECK_MISMATCH")
            context = predecessor.action_terms.get("execution_context")
            level_context = (
                context.get("direct_take_profit")
                if isinstance(context, dict)
                else None
            )
            if (
                not isinstance(context, dict)
                or not isinstance(level_context, dict)
                or not isinstance(level_context.get("level_index"), int)
                or not all(
                    isinstance(context.get(key), str)
                    for key in (
                        "entry_action_ref",
                        "protection_action_ref",
                        "fill_fact_ref",
                        "fill_source_identity",
                    )
                )
            ):
                raise ValueError("TAKE_PROFIT_UNKNOWN")
            valid_until_value = predecessor.action_terms.get("valid_until")
            if isinstance(valid_until_value, str):
                try:
                    valid_until = datetime.fromisoformat(valid_until_value)
                except ValueError:
                    raise ValueError("TAKE_PROFIT_UNKNOWN") from None
            elif isinstance(valid_until_value, datetime) or valid_until_value is None:
                valid_until = valid_until_value
            else:
                raise ValueError("TAKE_PROFIT_UNKNOWN")
            proposed = proposed_direct_take_profit_replacement(
                activation,
                predecessor_action_ref=predecessor.execution_action_id,
                protection_action_ref=str(context["protection_action_ref"]),
                entry_action_ref=str(context["entry_action_ref"]),
                fill_fact_ref=str(context["fill_fact_ref"]),
                fill_source_identity=str(context["fill_source_identity"]),
                level_index=int(level_context["level_index"]),
                remaining_quantity=normalized_remaining,
                target_trigger_price=target_trigger_price,
                aggregate_revision=aggregate_revision,
                valid_until=valid_until,
            )
            source_identity = (
                f"{activation.activation_id}:ENTRY_AGGREGATE_TAKE_PROFIT_REBALANCE:"
                f"{context['fill_fact_ref']}:{level_context['level_index']}:"
                f"{aggregate_revision}"
            )
            event, action = self._record_proposed_action(
                plan_event_id=plan_event_id,
                execution_action_id=execution_action_id,
                activation_id=activation.activation_id,
                rule_id="ENTRY_AGGREGATE_TAKE_PROFIT_REBALANCE",
                source_identity=source_identity,
                source_cutoff=observed_at,
                input_digest=proposed.causation_ref,
                proposed_action=proposed,
                action_check=action_check,
                observed_at=observed_at,
                client_order_id=client_order_id,
            )
            return CoordinatedProposalResult(event, action)

    def create_cancel_for_action(
        self,
        *,
        activation_id: str,
        target_action_id: str,
        target_endpoint: str,
        plan_event_id: str,
        execution_action_id: str,
        action_check: ActionCheckInput,
        reason_ref: str,
        observed_at: datetime,
        client_order_id: str | None = None,
    ) -> CoordinatedProposalResult:
        with self._connection.transaction():
            target = self._action_repository.get(target_action_id, for_update=True)
            if target.activation_id != activation_id or target.client_order_id is None:
                raise ValueError("CANCEL_TARGET_INVALID")
            activation = self._planning.get_activation(
                activation_id,
                for_update=True,
            )
            proposed = proposed_cancel_for_action(
                activation,
                target_client_order_id=target.client_order_id,
                target_endpoint=target_endpoint,
                causation_ref=reason_ref,
            )
            event, action = self._record_proposed_action(
                plan_event_id=plan_event_id,
                execution_action_id=execution_action_id,
                activation_id=activation.activation_id,
                rule_id="CANCEL_OPEN_RESPONSIBILITY",
                source_identity=(
                    f"{activation.activation_id}:CANCEL:{target.execution_action_id}:{reason_ref}"
                ),
                source_cutoff=observed_at,
                input_digest=content_digest(
                    {
                        "target_action_id": target.execution_action_id,
                        "target_state_digest": target.state_digest,
                        "reason_ref": reason_ref,
                    }
                ),
                proposed_action=proposed,
                action_check=action_check,
                observed_at=observed_at,
                client_order_id=client_order_id,
            )
            return CoordinatedProposalResult(event, action)

    def create_take_profit_market_reduction(
        self,
        *,
        activation_id: str,
        rejected_take_profit_action_id: str,
        rejection_fact_ref: str,
        position_quantity: str,
        position_fact_ref: str,
        reason_ref: str,
        plan_event_id: str,
        execution_action_id: str,
        action_check: ActionCheckInput,
        observed_at: datetime,
        client_order_id: str | None = None,
    ) -> CoordinatedProposalResult:
        """Persist one bounded market successor for an already-crossed TP."""

        with self._connection.transaction():
            activation = self._planning.get_activation(activation_id, for_update=True)
            if activation.lifecycle is not PlanLifecycle.RUNNING:
                raise RuntimeError("TAKE_PROFIT_SUCCESSOR_NOT_RUNNING")
            predecessor = self._action_repository.get(
                rejected_take_profit_action_id,
                for_update=True,
            )
            predecessor_facts = self._fact_repository.list_for_action(
                rejected_take_profit_action_id
            )
            rejection_fact = next(
                (
                    fact
                    for fact in predecessor_facts
                    if fact.venue_fact_id == rejection_fact_ref
                    and fact.kind is VenueFactKind.ORDER_STATE
                    and str(fact.payload.get("status", "")).upper() == "REJECTED"
                ),
                None,
            )
            if (
                predecessor.activation_id != activation_id
                or predecessor.action_kind is not ExecutionActionKind.TAKE_PROFIT
                or rejection_fact is None
                or venue_rejection_disposition(predecessor, predecessor_facts)
                is not VenueRejectionDisposition.TAKE_PROFIT_TRIGGER_ALREADY_CROSSED
            ):
                raise ValueError("TAKE_PROFIT_SUCCESSOR_EVIDENCE_INVALID")
            predecessor_quantity = Decimal(
                str(predecessor.action_terms.get("quantity", "0"))
            )
            reduction_quantity = Decimal(position_quantity)
            if (
                predecessor_quantity <= 0
                or reduction_quantity <= 0
                or reduction_quantity > predecessor_quantity
            ):
                raise ValueError("TAKE_PROFIT_SUCCESSOR_QUANTITY_INVALID")
            proposed = proposed_take_profit_market_reduction(
                activation,
                quantity=position_quantity,
                causation_ref=reason_ref,
                position_fact_ref=position_fact_ref,
                rejected_take_profit_action_ref=predecessor.execution_action_id,
                rejection_fact_ref=rejection_fact.venue_fact_id,
            )
            source_identity = (
                f"{activation.activation_id}:TAKE_PROFIT_TRIGGER_CROSSED:"
                f"{predecessor.execution_action_id}:{rejection_fact.venue_fact_id}"
            )
            event, action = self._record_proposed_action(
                plan_event_id=plan_event_id,
                execution_action_id=execution_action_id,
                activation_id=activation.activation_id,
                rule_id="TAKE_PROFIT_TRIGGER_CROSSED_MARKET_REDUCTION",
                source_identity=source_identity,
                source_cutoff=rejection_fact.cutoff,
                input_digest=content_digest(
                    {
                        "predecessor_action_id": predecessor.execution_action_id,
                        "rejection_fact_ref": rejection_fact.venue_fact_id,
                        "position_fact_ref": position_fact_ref,
                        "position_quantity": position_quantity,
                        "reason_ref": reason_ref,
                    }
                ),
                proposed_action=proposed,
                action_check=action_check,
                observed_at=observed_at,
                client_order_id=client_order_id,
            )
            return CoordinatedProposalResult(event, action)

    def create_position_exit(
        self,
        *,
        activation_id: str,
        position_quantity: str,
        position_fact_ref: str,
        exit_responsibility_role: ExitResponsibilityRole,
        reason_ref: str,
        plan_event_id: str,
        execution_action_id: str,
        action_check: ActionCheckInput,
        observed_at: datetime,
        client_order_id: str | None = None,
    ) -> CoordinatedProposalResult:
        with self._connection.transaction():
            activation = self._planning.get_activation(activation_id, for_update=True)
            if activation.lifecycle not in {
                PlanLifecycle.RUNNING,
                PlanLifecycle.EXITING,
            }:
                raise RuntimeError("USER_TAKEOVER_ACTIVE")
            proposed = proposed_reduce_or_close_position(
                activation,
                position_quantity=position_quantity,
                causation_ref=reason_ref,
                position_fact_ref=position_fact_ref,
            )
            proposed = proposed.model_copy(
                update={
                    "execution_context": {
                        **proposed.execution_context,
                        "exit_responsibility_role": exit_responsibility_role.value,
                    }
                }
            )
            position_side = proposed.execution_context.get("position_side")
            event, action = self._record_proposed_action(
                plan_event_id=plan_event_id,
                execution_action_id=execution_action_id,
                activation_id=activation.activation_id,
                rule_id="REDUCE_OR_CLOSE_POSITION",
                source_identity=(
                    f"{activation.activation_id}:EXIT:{position_fact_ref}:{reason_ref}"
                ),
                source_cutoff=observed_at,
                input_digest=content_digest(
                    {
                        "position_fact_ref": position_fact_ref,
                        "position_quantity": position_quantity,
                        "position_side": position_side,
                        "exit_responsibility_role": exit_responsibility_role.value,
                        "reason_ref": reason_ref,
                    }
                ),
                proposed_action=proposed,
                action_check=action_check,
                observed_at=observed_at,
                client_order_id=client_order_id,
            )
            return CoordinatedProposalResult(event, action)

    def apply_persisted_user_takeover(
        self,
        *,
        activation_id: str,
        observed_at: datetime,
    ) -> tuple[ExecutionAction, ...]:
        with self._connection.transaction():
            activation = self._planning.get_activation(activation_id, for_update=True)
            if activation.lifecycle is not PlanLifecycle.USER_TAKEOVER:
                raise ValueError("USER_TAKEOVER_NOT_PERSISTED")
            return self._execution.apply_user_takeover(
                activation_id,
                observed_at=observed_at,
            )

    def close_activation(
        self,
        *,
        activation_id: str,
        cutoff: datetime,
        position_zero: bool,
        open_order_refs: tuple[str, ...],
        external_activity_conflict: bool,
        user_takeover: bool,
        handover_command_ref: str | None,
        fact_refs: tuple[str, ...],
        observed_at: datetime,
    ) -> str:
        """Bind closure/release atomically, then derive OUT without rollback coupling."""

        with self._connection.transaction():
            activation = self._planning.get_activation(
                activation_id,
                for_update=True,
            )
            closure_digest = self._execution.evaluate_activation_closure(
                activation_id,
                cutoff=cutoff,
                position_zero=position_zero,
                open_order_refs=open_order_refs,
                external_activity_conflict=external_activity_conflict,
                user_takeover=user_takeover,
                handover_command_ref=handover_command_ref,
                fact_refs=fact_refs,
            )
            if activation.protection_state in {
                ProtectionState.UNKNOWN,
                ProtectionState.GAP,
                ProtectionState.WORKING,
            }:
                self._planning.update_protection_projection(
                    activation_id=activation_id,
                    protection_state=ProtectionState.CLOSED,
                    pending_action_digest=None,
                    observed_at=observed_at,
                )
            self._planning.complete_with_execution_closure(
                activation_id=activation_id,
                closure_digest=closure_digest,
                result_ref=review_id_for_activation(
                    self._environment_id, activation_id
                ),
                observed_at=observed_at,
            )
        # OUT failure is deliberately outside the closure/release transaction.
        # Restart recovery discovers the same completed activation and retries
        # this idempotent review identity without replaying a venue action.
        with self._connection.transaction():
            OutcomeApplicationService(
                self._connection, self._environment_id
            ).update_activation_review(
                activation_id,
                fact_cutoff=cutoff,
                observed_at=observed_at,
            )
        return closure_digest

    def _record_proposed_action(
        self,
        *,
        plan_event_id: str,
        execution_action_id: str,
        activation_id: str,
        rule_id: str,
        source_identity: str,
        source_cutoff: datetime,
        input_digest: str,
        proposed_action: ProposedAction,
        action_check: ActionCheckInput,
        observed_at: datetime,
        client_order_id: str | None,
        condition_judgement: ConditionJudgement | None = None,
    ) -> tuple[PlanEvent, ExecutionAction | None]:
        self._validate_proposed_action_check(
            proposed_action,
            action_check,
            activation_id=activation_id,
        )
        decision = self._capital.check_current_action(action_check)
        event = self._planning.record_plan_event(
            plan_event_id=plan_event_id,
            activation_id=activation_id,
            rule_id=rule_id,
            source_identity=source_identity,
            source_cutoff=source_cutoff,
            input_digest=input_digest,
            reason_code=(
                "PROPOSED_ACTION_CAP_ACCEPTED"
                if decision.accepted
                else "PROPOSED_ACTION_CAP_REJECTED"
            ),
            proposed_action=proposed_action,
            no_action_reason=None,
            condition_judgement=condition_judgement,
            capital_decision=decision.model_dump(mode="json"),
            created_at=observed_at,
        )
        if not decision.accepted:
            return event, None
        action = self._execution.create_execution_action(
            execution_action_id=execution_action_id,
            plan_event=event,
            observed_at=observed_at,
            client_order_id=client_order_id,
        )
        return event, action

    def consume_proposed_action(
        self,
        *,
        plan_event_id: str,
        execution_action_id: str,
        activation_id: str,
        rule_id: str,
        source_identity: str,
        source_cutoff: datetime,
        input_digest: str,
        proposed_action: ProposedAction,
        action_check: ActionCheckInput,
        observed_at: datetime,
        client_order_id: str | None = None,
    ) -> CoordinatedProposalResult:
        """Public coordination boundary for non-strategy rule actions."""

        with self._connection.transaction():
            event, action = self._record_proposed_action(
                plan_event_id=plan_event_id,
                execution_action_id=execution_action_id,
                activation_id=activation_id,
                rule_id=rule_id,
                source_identity=source_identity,
                source_cutoff=source_cutoff,
                input_digest=input_digest,
                proposed_action=proposed_action,
                action_check=action_check,
                observed_at=observed_at,
                client_order_id=client_order_id,
            )
            return CoordinatedProposalResult(event, action)

    def consume_order_schedule_atomic(
        self,
        *,
        activation_id: str,
        legs: tuple[MaterializedOrderLeg, ...],
        action_checks: tuple[ActionCheckInput, ...],
        observed_at: datetime,
        condition_evidence: dict[str, Any] | None = None,
        condition_source_cutoff: datetime | None = None,
        condition_facts: tuple[VenueFact, ...] = (),
    ) -> tuple[CoordinatedProposalResult, ...]:
        """Establish every local schedule responsibility before any venue call."""

        if not legs or len(legs) != len(action_checks):
            raise ValueError("ORDER_SCHEDULE_ACTION_COUNT_INVALID")
        if condition_evidence is not None and condition_source_cutoff is None:
            raise ValueError("ORDER_SCHEDULE_CONDITION_CUTOFF_REQUIRED")
        if condition_evidence is None and condition_facts:
            raise ValueError("ORDER_SCHEDULE_CONDITION_EVIDENCE_REQUIRED")
        if condition_source_cutoff is not None and (
            condition_source_cutoff.utcoffset() is None
            or observed_at.utcoffset() is None
            or condition_source_cutoff > observed_at
        ):
            raise ValueError("ORDER_SCHEDULE_CONDITION_CUTOFF_INVALID")
        with self._connection.transaction():
            activation = self._planning.get_activation(
                activation_id,
                for_update=True,
            )
            deadlines = activation.rule_state.get("deadlines")
            entry_deadline_value = (
                deadlines.get("entry_valid_until")
                if isinstance(deadlines, dict)
                else None
            )
            if not isinstance(entry_deadline_value, str):
                raise ValueError("ENTRY_DEADLINE_INVALID")
            try:
                entry_valid_until = datetime.fromisoformat(entry_deadline_value)
            except ValueError:
                raise ValueError("ENTRY_DEADLINE_INVALID") from None
            if entry_valid_until.utcoffset() is None:
                raise ValueError("ENTRY_DEADLINE_INVALID")

            authoritative_legs = materialize_direct_schedule(
                activation,
                entry_valid_until=entry_valid_until,
            )
            if legs != authoritative_legs:
                raise ValueError("ORDER_SCHEDULE_MATERIALIZATION_MISMATCH")

            snapshot = activation.order_schedule_snapshot
            if snapshot is None:
                raise ValueError("ORDER_SCHEDULE_SNAPSHOT_MISMATCH")
            if condition_evidence is not None:
                expected_condition_source = {
                    "DEMO": "BINANCE_DEMO_PUBLIC",
                    "LIVE": "BINANCE_LIVE_PUBLIC",
                }.get(self._environment_kind)
                if expected_condition_source is None:
                    raise ValueError("ORDER_SCHEDULE_CONDITION_FACT_INVALID")
                if (
                    activation.environment_id != self._environment_id
                    or activation.environment_kind.value != self._environment_kind
                    or snapshot.venue_ref != self._venue_ref
                    or self._venue_ref != BINANCE_USDM_VENUE_REF
                    or any(
                        not _condition_fact_matches_runtime_identity(
                            fact,
                            activation,
                            expected_venue_ref=self._venue_ref,
                            expected_source=expected_condition_source,
                            source_cutoff=condition_source_cutoff,
                        )
                        for fact in condition_facts
                    )
                ):
                    raise ValueError("ORDER_SCHEDULE_CONDITION_FACT_INVALID")
            schedule_digest = snapshot.schedule_digest
            expected_action_ids = {
                item.execution_action_id for item in authoritative_legs
            }
            all_schedule_actions: list[ExecutionAction] = []
            for action in self._action_repository.list_for_activation(activation_id):
                schedule_context = action.action_terms.get(
                    "execution_context",
                    {},
                ).get("order_schedule")
                if isinstance(schedule_context, dict) and schedule_context:
                    all_schedule_actions.append(action)
                    if schedule_context.get("schedule_digest") != schedule_digest:
                        raise ValueError("ORDER_SCHEDULE_DIGEST_CONFLICT")
            existing_schedule_actions = tuple(
                action
                for action in all_schedule_actions
                if action.action_terms.get("execution_context", {})
                .get("order_schedule", {})
                .get("schedule_digest")
                == schedule_digest
            )
            existing_ids = {
                action.execution_action_id for action in existing_schedule_actions
            }
            if existing_ids and existing_ids != expected_action_ids:
                raise ValueError("ORDER_SCHEDULE_LOCAL_RESPONSIBILITY_CONFLICT")

            # Lock and check the complete economic action before appending any
            # event or action. A rejection rolls back the whole local schedule.
            decisions = tuple(
                self._capital.check_current_action(check) for check in action_checks
            )
            rejections = tuple(
                (item.execution_action_id, decision.reason_code)
                for item, decision in zip(
                    authoritative_legs,
                    decisions,
                    strict=True,
                )
                if not decision.accepted
            )
            if rejections:
                raise OrderScheduleCapRejected(rejections)

            condition_fact_refs: tuple[str, ...] = ()
            persisted_condition_evidence = condition_evidence
            if condition_evidence is not None:
                canonical_facts: list[VenueFact] = []
                for fact in condition_facts:
                    application = self._execution.apply_venue_fact_with_result(
                        fact=fact,
                        observed_at=observed_at,
                    )
                    canonical_facts.append(application.canonical_fact)
                condition_fact_refs = tuple(
                    fact.venue_fact_id for fact in canonical_facts
                )
                persisted_condition_evidence = {
                    **condition_evidence,
                    "fact_refs": condition_fact_refs,
                }

            results: list[CoordinatedProposalResult] = []
            for item, check, decision in zip(
                legs,
                action_checks,
                decisions,
                strict=True,
            ):
                proposed_action = _runtime_schedule_proposed_action(item, check)
                runtime_quantity_adjusted = (
                    proposed_action.quantity != item.proposed_action.quantity
                )
                if (
                    persisted_condition_evidence is not None
                    or runtime_quantity_adjusted
                ):
                    digest_payload: dict[str, Any] = {
                        "materialized_input_digest": item.input_digest,
                    }
                    if persisted_condition_evidence is not None:
                        digest_payload["entry_condition_evidence"] = (
                            persisted_condition_evidence
                        )
                    if runtime_quantity_adjusted:
                        digest_payload["runtime_market_sizing"] = (
                            proposed_action.execution_context["runtime_market_sizing"]
                        )
                    event_input_digest = content_digest(digest_payload)
                else:
                    event_input_digest = item.input_digest
                event, action = self._record_proposed_action(
                    plan_event_id=item.plan_event_id,
                    execution_action_id=item.execution_action_id,
                    activation_id=activation_id,
                    rule_id="DIRECT_ORDER_SCHEDULE_LEG",
                    source_identity=item.source_identity,
                    source_cutoff=condition_source_cutoff or check.checked_at,
                    input_digest=event_input_digest,
                    proposed_action=proposed_action,
                    action_check=check,
                    observed_at=observed_at,
                    client_order_id=item.client_order_id,
                    condition_judgement=(
                        ConditionJudgement(
                            rule_id="DIRECT_ORDER_SCHEDULE_CONDITIONS",
                            source_identity=item.source_identity,
                            source_cutoff=condition_source_cutoff,
                            input_digest=event_input_digest,
                            fact_refs=condition_fact_refs,
                            result=ConditionResult.TRUE,
                            reason_code="DIRECT_ENTRY_CONDITIONS_TRUE",
                            next_responsibility="EXECUTION_ACTION",
                        )
                        if condition_evidence is not None
                        else None
                    ),
                )
                if action is None:
                    raise OrderScheduleCapRejected(
                        ((item.execution_action_id, decision.reason_code),)
                    )
                results.append(CoordinatedProposalResult(event, action))
            return tuple(results)

    def consume_order_schedule_retry(
        self,
        *,
        activation_id: str,
        retry_leg: MaterializedOrderLeg,
        previous_action_id: str,
        action_check: ActionCheckInput,
        observed_at: datetime,
        condition_source_cutoff: datetime,
        condition_facts: tuple[VenueFact, ...],
        condition_evidence: dict[str, Any],
    ) -> CoordinatedProposalResult:
        """Create one new stable attempt after a proven zero-fill Post Only race."""

        with self._connection.transaction():
            activation = self._planning.get_activation(
                activation_id,
                for_update=True,
            )
            if (
                activation.lifecycle is not PlanLifecycle.RUNNING
                or activation.run_state is not RunState.ACTIVE
                or activation.entry_opportunity_consumed
            ):
                raise ValueError("ORDER_SCHEDULE_RETRY_NOT_ALLOWED")
            deadlines = activation.rule_state.get("deadlines")
            deadline_raw = (
                deadlines.get("entry_valid_until")
                if isinstance(deadlines, dict)
                else None
            )
            if not isinstance(deadline_raw, str):
                raise ValueError("ENTRY_DEADLINE_INVALID")
            try:
                entry_valid_until = datetime.fromisoformat(deadline_raw)
            except ValueError:
                raise ValueError("ENTRY_DEADLINE_INVALID") from None
            if (
                entry_valid_until.utcoffset() is None
                or observed_at.utcoffset() is None
                or condition_source_cutoff.utcoffset() is None
                or observed_at >= entry_valid_until
                or condition_source_cutoff > observed_at
            ):
                raise ValueError("ORDER_SCHEDULE_RETRY_NOT_ALLOWED")

            base_legs = materialize_direct_schedule(
                activation,
                entry_valid_until=entry_valid_until,
            )
            retry_context = retry_leg.proposed_action.execution_context.get(
                "order_schedule"
            )
            if not isinstance(retry_context, dict):
                raise ValueError("ORDER_SCHEDULE_RETRY_INVALID")
            leg_index = retry_context.get("leg_index")
            attempt_index = retry_context.get("attempt_index")
            retry_reason = retry_context.get("retry_reason")
            replacement_price = retry_context.get("replacement_price")
            reprice_index = retry_context.get("reprice_index", 0)
            if (
                not isinstance(leg_index, int)
                or not isinstance(attempt_index, int)
                or attempt_index < 1
                or retry_reason
                not in {
                    "POST_ONLY_WOULD_TAKE_RACE",
                    "PRICE_MATCH_TEMPORARILY_UNAVAILABLE",
                }
                or (
                    replacement_price is not None
                    and not isinstance(replacement_price, str)
                )
                or not isinstance(reprice_index, int)
                or reprice_index < 0
            ):
                raise ValueError("ORDER_SCHEDULE_RETRY_INVALID")
            base = next(
                (item for item in base_legs if item.leg.leg_index == leg_index),
                None,
            )
            if base is None or retry_leg != materialize_direct_schedule_retry(
                activation,
                base,
                attempt_index=attempt_index,
                retry_reason=retry_reason,
                replacement_price=replacement_price,
                reprice_index=reprice_index,
            ):
                raise ValueError("ORDER_SCHEDULE_RETRY_MISMATCH")

            previous = self._action_repository.get(
                previous_action_id,
                for_update=True,
            )
            previous_context = previous.action_terms.get(
                "execution_context",
                {},
            ).get("order_schedule", {})
            if (
                previous.activation_id != activation_id
                or previous.state is not ExecutionActionState.CLOSED
                or previous_context.get("schedule_digest")
                != retry_context.get("schedule_digest")
                or previous_context.get("leg_index") != leg_index
                or previous_context.get("attempt_index", 0) != attempt_index - 1
                or previous_context.get("replacement_price")
                != replacement_price
                or previous_context.get("reprice_index", 0) != reprice_index
            ):
                raise ValueError("ORDER_SCHEDULE_RETRY_PREDECESSOR_INVALID")
            previous_disposition = venue_rejection_disposition(
                previous,
                self._fact_repository.list_for_action(previous_action_id),
            )
            expected_retry_reason = {
                VenueRejectionDisposition.RETRYABLE_POST_ONLY: (
                    "POST_ONLY_WOULD_TAKE_RACE"
                ),
                VenueRejectionDisposition.RETRYABLE_PRICE_MATCH: (
                    "PRICE_MATCH_TEMPORARILY_UNAVAILABLE"
                ),
            }.get(previous_disposition)
            if retry_context.get("retry_reason") != expected_retry_reason:
                raise ValueError("ORDER_SCHEDULE_RETRY_PREDECESSOR_INVALID")

            schedule_actions = tuple(
                action
                for action in self._action_repository.list_for_activation(activation_id)
                if isinstance(
                    action.action_terms.get("execution_context"),
                    dict,
                )
                and action.action_terms["execution_context"]
                .get("order_schedule", {})
                .get("schedule_digest")
                == retry_context.get("schedule_digest")
                and action.action_terms["execution_context"]
                .get("order_schedule", {})
                .get("leg_index")
                == leg_index
            )
            latest_attempt = max(
                (
                    action.action_terms["execution_context"]["order_schedule"].get(
                        "attempt_index",
                        0,
                    )
                    for action in schedule_actions
                ),
                default=-1,
            )
            if latest_attempt != attempt_index - 1:
                raise ValueError("ORDER_SCHEDULE_RETRY_PREDECESSOR_INVALID")

            snapshot = activation.order_schedule_snapshot
            expected_source = {
                "DEMO": "BINANCE_DEMO_PUBLIC",
                "LIVE": "BINANCE_LIVE_PUBLIC",
            }.get(self._environment_kind)
            if (
                snapshot is None
                or expected_source is None
                or activation.environment_id != self._environment_id
                or activation.environment_kind.value != self._environment_kind
                or snapshot.venue_ref != self._venue_ref
                or self._venue_ref != BINANCE_USDM_VENUE_REF
                or any(
                    not _condition_fact_matches_runtime_identity(
                        fact,
                        activation,
                        expected_venue_ref=self._venue_ref,
                        expected_source=expected_source,
                        source_cutoff=condition_source_cutoff,
                    )
                    for fact in condition_facts
                )
            ):
                raise ValueError("ORDER_SCHEDULE_CONDITION_FACT_INVALID")

            decision = self._capital.check_current_action(action_check)
            if not decision.accepted:
                raise OrderScheduleCapRejected(
                    ((retry_leg.execution_action_id, decision.reason_code),)
                )
            canonical_facts: list[VenueFact] = []
            for fact in condition_facts:
                application = self._execution.apply_venue_fact_with_result(
                    fact=fact,
                    observed_at=observed_at,
                )
                canonical_facts.append(application.canonical_fact)
            fact_refs = tuple(fact.venue_fact_id for fact in canonical_facts)
            persisted_evidence = {
                **condition_evidence,
                "fact_refs": fact_refs,
                "retry_of_action_id": previous_action_id,
                "attempt_index": attempt_index,
            }
            proposed_action = _runtime_schedule_proposed_action(
                retry_leg,
                action_check,
            )
            event_input_digest = content_digest(
                {
                    "materialized_input_digest": retry_leg.input_digest,
                    "entry_condition_evidence": persisted_evidence,
                }
            )
            event, action = self._record_proposed_action(
                plan_event_id=retry_leg.plan_event_id,
                execution_action_id=retry_leg.execution_action_id,
                activation_id=activation_id,
                rule_id="DIRECT_ENTRY_POLICY_RETRY",
                source_identity=retry_leg.source_identity,
                source_cutoff=condition_source_cutoff,
                input_digest=event_input_digest,
                proposed_action=proposed_action,
                action_check=action_check,
                observed_at=observed_at,
                client_order_id=retry_leg.client_order_id,
                condition_judgement=ConditionJudgement(
                    rule_id="DIRECT_ORDER_SCHEDULE_CONDITIONS",
                    source_identity=retry_leg.source_identity,
                    source_cutoff=condition_source_cutoff,
                    input_digest=event_input_digest,
                    fact_refs=fact_refs,
                    result=ConditionResult.TRUE,
                    reason_code="DIRECT_ENTRY_CONDITIONS_TRUE",
                    next_responsibility="EXECUTION_ACTION",
                ),
            )
            if action is None:
                raise OrderScheduleCapRejected(
                    ((retry_leg.execution_action_id, decision.reason_code),)
                )
            return CoordinatedProposalResult(event, action)

    def consume_order_schedule_reprice(
        self,
        *,
        activation_id: str,
        replacement_leg: MaterializedOrderLeg,
        previous_action_id: str,
        cancel_action_id: str,
        action_check: ActionCheckInput,
        observed_at: datetime,
        condition_source_cutoff: datetime,
        condition_facts: tuple[VenueFact, ...],
        condition_evidence: dict[str, Any],
    ) -> CoordinatedProposalResult:
        """Create one bounded entry replacement after cancel and zero-fill proof."""

        with self._connection.transaction():
            activation = self._planning.get_activation(
                activation_id,
                for_update=True,
            )
            if (
                activation.lifecycle is not PlanLifecycle.RUNNING
                or activation.run_state is not RunState.ACTIVE
                or activation.entry_opportunity_consumed
            ):
                raise ValueError("ORDER_SCHEDULE_REPRICE_NOT_ALLOWED")
            deadlines = activation.rule_state.get("deadlines")
            deadline_raw = (
                deadlines.get("entry_valid_until")
                if isinstance(deadlines, dict)
                else None
            )
            if not isinstance(deadline_raw, str):
                raise ValueError("ENTRY_DEADLINE_INVALID")
            try:
                entry_valid_until = datetime.fromisoformat(deadline_raw)
            except ValueError:
                raise ValueError("ENTRY_DEADLINE_INVALID") from None
            if (
                entry_valid_until.utcoffset() is None
                or observed_at.utcoffset() is None
                or condition_source_cutoff.utcoffset() is None
                or observed_at >= entry_valid_until
                or condition_source_cutoff > observed_at
            ):
                raise ValueError("ORDER_SCHEDULE_REPRICE_NOT_ALLOWED")

            replacement_context = (
                replacement_leg.proposed_action.execution_context.get(
                    "order_schedule"
                )
            )
            if not isinstance(replacement_context, dict):
                raise ValueError("ORDER_SCHEDULE_REPRICE_INVALID")
            leg_index = replacement_context.get("leg_index")
            attempt_index = replacement_context.get("attempt_index")
            replacement_price = replacement_context.get("replacement_price")
            reprice_index = replacement_context.get("reprice_index")
            if (
                replacement_context.get("retry_reason") != "ENTRY_REPRICE"
                or not isinstance(leg_index, int)
                or not isinstance(attempt_index, int)
                or attempt_index < 1
                or not isinstance(replacement_price, str)
                or not isinstance(reprice_index, int)
                or reprice_index < 1
            ):
                raise ValueError("ORDER_SCHEDULE_REPRICE_INVALID")
            base_legs = materialize_direct_schedule(
                activation,
                entry_valid_until=entry_valid_until,
            )
            base = next(
                (item for item in base_legs if item.leg.leg_index == leg_index),
                None,
            )
            if base is None or replacement_leg != materialize_direct_schedule_reprice(
                activation,
                base,
                attempt_index=attempt_index,
                replacement_price=replacement_price,
                reprice_index=reprice_index,
            ):
                raise ValueError("ORDER_SCHEDULE_REPRICE_MISMATCH")

            all_actions = self._action_repository.list_for_activation(activation_id)
            schedule_actions = tuple(
                action
                for action in all_actions
                if isinstance(
                    action.action_terms.get("execution_context"),
                    dict,
                )
                and action.action_terms["execution_context"]
                .get("order_schedule", {})
                .get("schedule_digest")
                == replacement_context.get("schedule_digest")
                and action.action_terms["execution_context"]
                .get("order_schedule", {})
                .get("leg_index")
                == leg_index
            )
            previous = self._action_repository.get(
                previous_action_id,
                for_update=True,
            )
            previous_context = previous.action_terms.get(
                "execution_context",
                {},
            ).get("order_schedule", {})
            previous_facts = self._fact_repository.list_for_action(
                previous_action_id
            )
            latest_attempt = max(
                (
                    action.action_terms["execution_context"]["order_schedule"].get(
                        "attempt_index",
                        0,
                    )
                    for action in schedule_actions
                ),
                default=-1,
            )
            existing_reprices = sum(
                1
                for action in schedule_actions
                if (
                    action.action_terms["execution_context"]
                    .get("order_schedule", {})
                    .get("retry_reason")
                    == "ENTRY_REPRICE"
                )
            )
            reprice_rule = next(
                (
                    rule
                    for rule in (
                        activation.order_schedule_snapshot.schedule_spec.dynamic_rules
                        if activation.order_schedule_snapshot is not None
                        else ()
                    )
                    if isinstance(rule, RepriceEntryRule)
                ),
                None,
            )
            if (
                previous.activation_id != activation_id
                or previous.state is not ExecutionActionState.CLOSED
                or previous.client_order_id is None
                or previous_context.get("schedule_digest")
                != replacement_context.get("schedule_digest")
                or previous_context.get("leg_index") != leg_index
                or previous_context.get("attempt_index", 0)
                != attempt_index - 1
                or latest_attempt != attempt_index - 1
                or reprice_rule is None
                or existing_reprices + 1 != reprice_index
                or reprice_index > reprice_rule.max_adjustments
                or terminal_order_status(previous_facts)
                not in {"CANCELLED", "EXPIRED"}
                or not terminal_fills_complete(previous, previous_facts)
                or any(
                    fact.kind is VenueFactKind.FILL for fact in previous_facts
                )
            ):
                raise ValueError("ORDER_SCHEDULE_REPRICE_PREDECESSOR_INVALID")

            cancel = self._action_repository.get(
                cancel_action_id,
                for_update=True,
            )
            if (
                cancel.activation_id != activation_id
                or cancel.action_kind is not ExecutionActionKind.CANCEL
                or cancel.state is not ExecutionActionState.CLOSED
                or not isinstance(cancel.cancel_target, dict)
                or cancel.cancel_target.get("client_order_id")
                != previous.client_order_id
                or ":DIRECT_ENTRY_REPRICE:" not in str(
                    cancel.action_terms.get("causation_ref", "")
                )
            ):
                raise ValueError("ORDER_SCHEDULE_REPRICE_CANCEL_INVALID")

            snapshot = activation.order_schedule_snapshot
            expected_source = {
                "DEMO": "BINANCE_DEMO_PUBLIC",
                "LIVE": "BINANCE_LIVE_PUBLIC",
            }.get(self._environment_kind)
            if (
                snapshot is None
                or expected_source is None
                or activation.environment_id != self._environment_id
                or activation.environment_kind.value != self._environment_kind
                or snapshot.venue_ref != self._venue_ref
                or self._venue_ref != BINANCE_USDM_VENUE_REF
                or any(
                    not _condition_fact_matches_runtime_identity(
                        fact,
                        activation,
                        expected_venue_ref=self._venue_ref,
                        expected_source=expected_source,
                        source_cutoff=condition_source_cutoff,
                    )
                    for fact in condition_facts
                )
            ):
                raise ValueError("ORDER_SCHEDULE_CONDITION_FACT_INVALID")

            decision = self._capital.check_current_action(action_check)
            if not decision.accepted:
                raise OrderScheduleCapRejected(
                    ((replacement_leg.execution_action_id, decision.reason_code),)
                )
            canonical_facts: list[VenueFact] = []
            for fact in condition_facts:
                application = self._execution.apply_venue_fact_with_result(
                    fact=fact,
                    observed_at=observed_at,
                )
                canonical_facts.append(application.canonical_fact)
            fact_refs = tuple(fact.venue_fact_id for fact in canonical_facts)
            persisted_evidence = {
                **condition_evidence,
                "fact_refs": fact_refs,
                "reprice_of_action_id": previous_action_id,
                "cancel_action_id": cancel_action_id,
                "attempt_index": attempt_index,
                "reprice_index": reprice_index,
                "replacement_price": replacement_price,
            }
            proposed_action = _runtime_schedule_proposed_action(
                replacement_leg,
                action_check,
            )
            event_input_digest = content_digest(
                {
                    "materialized_input_digest": replacement_leg.input_digest,
                    "entry_condition_evidence": persisted_evidence,
                }
            )
            event, action = self._record_proposed_action(
                plan_event_id=replacement_leg.plan_event_id,
                execution_action_id=replacement_leg.execution_action_id,
                activation_id=activation_id,
                rule_id="DIRECT_ENTRY_REPRICE",
                source_identity=replacement_leg.source_identity,
                source_cutoff=condition_source_cutoff,
                input_digest=event_input_digest,
                proposed_action=proposed_action,
                action_check=action_check,
                observed_at=observed_at,
                client_order_id=replacement_leg.client_order_id,
                condition_judgement=ConditionJudgement(
                    rule_id="DIRECT_ORDER_SCHEDULE_CONDITIONS",
                    source_identity=replacement_leg.source_identity,
                    source_cutoff=condition_source_cutoff,
                    input_digest=event_input_digest,
                    fact_refs=fact_refs,
                    result=ConditionResult.TRUE,
                    reason_code="DIRECT_ENTRY_CONDITIONS_TRUE",
                    next_responsibility="EXECUTION_ACTION",
                ),
            )
            if action is None:
                raise OrderScheduleCapRejected(
                    ((replacement_leg.execution_action_id, decision.reason_code),)
                )
            return CoordinatedProposalResult(event, action)

    @staticmethod
    def _validate_action_check(
        action: ExecutionAction,
        check: ActionCheckInput,
        activation: PlanActivation,
    ) -> None:
        terms = action.action_terms
        activation_direction = getattr(
            activation.direction, "value", activation.direction
        )
        alignment = getattr(activation, "position_alignment", None)
        expected_position_side = (
            alignment.position_side if alignment is not None else "BOTH"
        )
        side_matches_activation = (
            terms.get("position_side") == expected_position_side
            if terms.get("action_profile") == "REDUCE_OR_CLOSE_MARKET"
            else terms.get("position_side") is None
        )
        if (
            check.environment_id != action.environment_id
            or check.environment_kind is not action.environment_kind
            or check.authority_class is not action.authority_class
            or check.activation_id != action.activation_id
            or check.account_ref != action.account_ref
            or check.instrument_ref != terms.get("instrument_ref")
            or check.action_profile != terms.get("action_profile")
            or check.risk_class is not action.action_class
            or check.quantized_quantity != (terms.get("quantity") or "0")
            or activation.activation_id != action.activation_id
            or activation.environment_id != action.environment_id
            or activation.account_ref != action.account_ref
            or activation.instrument_ref != terms.get("instrument_ref")
            or activation_direction != terms.get("direction")
            or not side_matches_activation
        ):
            raise ValueError("ACTION_SCOPE_MISMATCH")

    def _validate_proposed_action_check(
        self,
        proposed: ProposedAction,
        check: ActionCheckInput,
        *,
        activation_id: str,
    ) -> None:
        expected_risk = (
            "RISK_INCREASING"
            if proposed.action_kind.value == "ENTRY"
            else (
                "RISK_NEUTRAL"
                if proposed.action_kind.value == "CANCEL"
                else "RISK_REDUCING"
            )
        )
        expected_category = (
            "NEW_RISK"
            if proposed.action_kind.value == "ENTRY"
            else (
                "PROTECTION"
                if proposed.action_kind.value == "PROTECTION"
                else "RISK_REDUCTION_OR_ORDER_MANAGEMENT"
            )
        )
        if (
            check.environment_id != proposed.environment_id
            or check.environment_kind.value != self._environment_kind
            or check.authority_class.value != self._authority_class
            or check.activation_id != activation_id
            or check.account_ref != self._account_ref
            or check.instrument_ref != proposed.instrument_ref
            or check.action_profile != proposed.action_profile
            or check.risk_class.value != expected_risk
            or check.control_category.value != expected_category
            or check.quantized_quantity != (proposed.quantity or "0")
        ):
            raise ValueError("PLAN_BOUNDARY_MISMATCH")
