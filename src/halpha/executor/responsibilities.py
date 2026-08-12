"""Run the concrete post-entry responsibilities for the one-shot strategy."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_FLOOR, ROUND_UP
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from halpha.capital.models import ActionCheckInput, RiskClass, StopCategory
from halpha.domain_values import canonical_decimal, content_digest
from halpha.planning.models import PlanActivation, PlanLifecycle, ProtectionState
from halpha.planning.order_policies import (
    ProfitLockMode,
    ProfitLockRule,
    RepriceEntryRule,
    SteppedProtectionRule,
)
from halpha.planning.registry import DIRECT_EXECUTION_REF, Direction
from halpha.planning.transitions import (
    proposed_direct_take_profits_from_fill,
    proposed_take_profits_from_fill,
    venue_source_identity,
)
from halpha.venue_integration.facts import (
    collapse_synthetic_reconciliation_fills,
    order_is_working,
    terminal_fills_accounted_for_exit,
    terminal_fills_accounted_for_user_takeover,
    terminal_fills_complete,
    terminal_order_status,
)
from halpha.venue_integration.models import (
    BINANCE_USDM_VENUE_REF,
    ExecutionAction,
    ExecutionActionKind,
    ExecutionActionState,
    ExitResponsibilityRole,
    VenueFact,
    VenueFactKind,
    VenueFactSourceClass,
)
from halpha.venue_integration.nautilus_events import NormalizedNautilusEvent
from halpha.venue_integration.binance_funding import FundingIncomeRecord
from halpha.venue_integration.rejections import (
    VenueRejectionDisposition,
    venue_rejection_disposition,
)

from .direct_protection import direct_time_exit_at


PROTECTION_UNKNOWN_EXIT_DELAY = timedelta(seconds=15)
UNKNOWN_RISK_CONTROL_SUCCESSOR_DELAY = timedelta(seconds=15)
CALLED_ACTION_RECONCILIATION_INTERVAL_SECONDS = 10.0
COMPLETED_RESPONSIBILITY_TASK_KEY_LIMIT = 4096


class ProductResponsibilityCoordinator(Protocol):
    def get_activation_snapshot(self, activation_id: str) -> PlanActivation: ...

    def get_execution_action(self, execution_action_id: str) -> ExecutionAction: ...

    def list_execution_actions(
        self, activation_id: str
    ) -> tuple[ExecutionAction, ...]: ...

    def list_venue_facts_for_action(
        self, execution_action_id: str
    ) -> tuple[VenueFact, ...]: ...

    def create_protection_for_fill(self, **kwargs: Any) -> Any: ...

    def create_direct_protection_replacement(self, **kwargs: Any) -> Any: ...

    def create_take_profits_for_protected_fill(self, **kwargs: Any) -> Any: ...

    def create_direct_take_profits_for_protected_fill(self, **kwargs: Any) -> Any: ...

    def create_direct_take_profit_replacement(self, **kwargs: Any) -> Any: ...

    def process_execution_action(
        self, execution_action_id: str, **kwargs: Any
    ) -> Any: ...

    def apply_venue_fact(
        self, fact: VenueFact, **kwargs: Any
    ) -> ExecutionAction | None: ...

    def create_position_exit(self, **kwargs: Any) -> Any: ...

    def create_take_profit_market_reduction(self, **kwargs: Any) -> Any: ...

    def create_cancel_for_action(self, **kwargs: Any) -> Any: ...

    def reconcile_execution_action(
        self, execution_action_id: str, **kwargs: Any
    ) -> ExecutionAction: ...

    def reconcile_cancel_from_target_fact(
        self,
        execution_action_id: str,
        *,
        target_fact: VenueFact,
        observed_at: datetime,
    ) -> ExecutionAction: ...

    def query_unknown_action_if_due(
        self, execution_action_id: str, **kwargs: Any
    ) -> bool: ...

    def query_called_action_identity(self, execution_action_id: str) -> bool: ...

    def apply_persisted_user_takeover(
        self, *, activation_id: str, observed_at: datetime
    ) -> tuple[ExecutionAction, ...]: ...

    def reject_execution_action_before_submission(
        self, execution_action_id: str, **kwargs: Any
    ) -> ExecutionAction: ...

    def close_activation(self, **kwargs: Any) -> str: ...

    def record_funding_income(self, **kwargs: Any) -> tuple[VenueFact, ...]: ...


@dataclass(frozen=True, slots=True)
class ProductRiskReductionFacts:
    checked_at: datetime
    conservative_price: str
    available_margin: str
    actual_margin_mode: str
    actual_leverage: str
    activation_current_notional: str
    account_current_notional: str
    activation_current_margin: str
    current_abs_position: str
    current_reference_price: str | None = None
    position_fact: VenueFact | None = None
    open_order_client_ids: tuple[str, ...] = ()
    open_algo_client_ids: tuple[str, ...] = ()
    attribution_cutoff: datetime | None = None

    def action_check(
        self,
        activation: PlanActivation,
        *,
        action_profile: str,
        control_category: StopCategory,
        quantity: str,
        confirmed_position_floor: str | None = None,
    ) -> ActionCheckInput:
        current = Decimal(self.current_abs_position)
        if confirmed_position_floor is not None:
            confirmed = Decimal(confirmed_position_floor)
            if confirmed < 0:
                raise ValueError("CONFIRMED_POSITION_FLOOR_INVALID")
            current = max(current, confirmed)
        requested = Decimal(quantity)
        would_reverse = requested > current
        post_action = max(current - requested, Decimal("0"))
        effective_current = canonical_decimal(current)
        return ActionCheckInput(
            environment_id=activation.environment_id,
            environment_kind=activation.environment_kind,
            authority_class=activation.authority_class,
            activation_id=activation.activation_id,
            account_ref=activation.account_ref,
            instrument_ref=activation.instrument_ref,
            action_profile=action_profile,
            control_category=control_category,
            risk_class=RiskClass.RISK_REDUCING,
            checked_at=self.checked_at,
            quantized_quantity=quantity,
            conservative_price=self.conservative_price,
            activation_current_notional=self.activation_current_notional,
            account_current_notional=self.account_current_notional,
            activation_current_margin=self.activation_current_margin,
            account_dynamic_available_margin=self.available_margin,
            actual_margin_mode=self.actual_margin_mode,
            actual_leverage=self.actual_leverage,
            post_action_abs_position=canonical_decimal(post_action),
            current_abs_position=effective_current,
            would_reverse_position=would_reverse,
        )

    def cancel_check(self, activation: PlanActivation) -> ActionCheckInput:
        """Build the risk-neutral check for cancelling an existing order identity."""

        return ActionCheckInput(
            environment_id=activation.environment_id,
            environment_kind=activation.environment_kind,
            authority_class=activation.authority_class,
            activation_id=activation.activation_id,
            account_ref=activation.account_ref,
            instrument_ref=activation.instrument_ref,
            action_profile="CANCEL_ORDER",
            control_category=StopCategory.RISK_REDUCTION_OR_ORDER_MANAGEMENT,
            risk_class=RiskClass.RISK_NEUTRAL,
            checked_at=self.checked_at,
            quantized_quantity="0",
            conservative_price=self.conservative_price,
            activation_current_notional=self.activation_current_notional,
            account_current_notional=self.account_current_notional,
            activation_current_margin=self.activation_current_margin,
            account_dynamic_available_margin=self.available_margin,
            actual_margin_mode=self.actual_margin_mode,
            actual_leverage=self.actual_leverage,
            post_action_abs_position=self.current_abs_position,
            current_abs_position=self.current_abs_position,
            would_reverse_position=False,
        )


RiskReductionFactProvider = Callable[
    [PlanActivation], Awaitable[ProductRiskReductionFacts]
]
FundingFactProvider = Callable[
    [PlanActivation, datetime], Awaitable[tuple[FundingIncomeRecord, ...]]
]
CalledActionRecoveryFactProvider = Callable[
    [ExecutionAction], Awaitable[tuple[VenueFact, ...]]
]


class ProductResponsibilityBoundary:
    """Create protection and take-profit actions from persisted venue facts."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        coordinator: ProductResponsibilityCoordinator,
        fact_provider: RiskReductionFactProvider,
        funding_provider: FundingFactProvider | None = None,
        called_action_recovery_fact_provider: (
            CalledActionRecoveryFactProvider | None
        ) = None,
        environment_id: str,
        failure_sink: Callable[[BaseException], None] | None = None,
        funding_fact_unavailable_sink: Callable[[BaseException], None] | None = None,
    ) -> None:
        self._loop = loop
        self._coordinator = coordinator
        self._fact_provider = fact_provider
        self._funding_provider = funding_provider
        self._called_action_recovery_fact_provider = (
            called_action_recovery_fact_provider
        )
        self._environment_id = environment_id
        self._failure_sink = failure_sink
        self._funding_fact_unavailable_sink = funding_fact_unavailable_sink
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._completed_task_keys: OrderedDict[str, None] = OrderedDict()
        self._last_fallback_sync: dict[str, float] = {}
        self._last_funding_sync: dict[str, float] = {}
        self._last_called_action_query: dict[str, float] = {}
        self._direct_time_exit_handles: dict[
            str,
            tuple[datetime, asyncio.TimerHandle],
        ] = {}
        self._direct_time_exit_woken: dict[str, datetime] = {}

    def submit_event(self, event: NormalizedNautilusEvent) -> None:
        action = event.action
        if action is None:
            return
        activation = self._coordinator.get_activation_snapshot(action.activation_id)
        if activation.lifecycle is PlanLifecycle.COMPLETED:
            return
        if activation.lifecycle is PlanLifecycle.USER_TAKEOVER:
            source = ":".join(fact.content_digest for fact in event.facts)
            self._schedule(
                f"TAKEOVER_SYNC:{action.activation_id}:{source}",
                self.sync(action.activation_id, force=True),
            )
            return
        if action.action_kind is ExecutionActionKind.ENTRY:
            for fact in event.facts:
                if fact.kind is VenueFactKind.FILL:
                    self._schedule(
                        f"PROTECTION:{fact.content_digest}",
                        self._protect_fill_and_sync(fact),
                    )
        if action.action_kind is ExecutionActionKind.PROTECTION and any(
            fact.kind is VenueFactKind.ORDER_STATE
            and fact.payload.get("status") == "WORKING"
            for fact in event.facts
        ):
            if activation.protection_state is ProtectionState.GAP:
                self._schedule(
                    f"SYNC:{action.activation_id}:PROTECTION_QUANTITY_GAP",
                    self.sync(action.activation_id, force=True),
                )
            else:
                self._schedule(
                    f"TAKE_PROFIT:{action.execution_action_id}",
                    self._create_take_profits(action.execution_action_id),
                )
        if (
            action.action_kind is ExecutionActionKind.PROTECTION
            and event.definitely_not_submitted
        ):
            # OrderDenied has no venue fact by definition, but the coordinator
            # has already persisted NOT_SUBMITTED.  Reconcile immediately so a
            # confirmed position does not wait for the periodic fallback.
            self._schedule(
                f"SYNC:{action.activation_id}:PROTECTION_DENIED:{action.execution_action_id}",
                self.sync(action.activation_id, force=True),
            )
        if action.action_kind is not ExecutionActionKind.ENTRY and any(
            fact.kind is VenueFactKind.FILL
            or (
                fact.kind is VenueFactKind.ORDER_STATE
                and fact.payload.get("status")
                in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
            )
            for fact in event.facts
        ):
            # Nautilus owns the live order stream and reconciliation. React to
            # its decisive events immediately; the periodic path remains only
            # a bounded fallback for a missed or delayed callback.
            source = ":".join(fact.content_digest for fact in event.facts)
            self._schedule(
                f"SYNC:{action.activation_id}:{source}",
                self.sync(action.activation_id, force=True),
            )

    def resume(self, activation_id: str) -> None:
        activation = self._coordinator.get_activation_snapshot(activation_id)
        self._arm_direct_time_exit(activation)
        if activation.lifecycle is PlanLifecycle.COMPLETED:
            return
        if activation.lifecycle is PlanLifecycle.USER_TAKEOVER:
            self._schedule(
                f"TAKEOVER_RESUME:{activation_id}",
                self.sync(activation_id, force=True),
            )
            return
        self._schedule(f"RESUME:{activation_id}", self._resume(activation_id))

    async def sync(self, activation_id: str, *, force: bool = False) -> None:
        """Advance only venue-backed responsibilities already persisted for one activation."""

        activation = self._coordinator.get_activation_snapshot(activation_id)
        if activation.lifecycle is PlanLifecycle.COMPLETED:
            self._clear_direct_time_exit_wake(activation_id)
            self._last_fallback_sync.pop(activation_id, None)
            self._last_funding_sync.pop(activation_id, None)
            self._last_called_action_query.clear()
            return
        if activation.lifecycle is PlanLifecycle.USER_TAKEOVER:
            self._clear_direct_time_exit_wake(activation_id)
            self._last_called_action_query.clear()
            await self._sync_user_takeover(activation, force=force)
            return
        self._arm_direct_time_exit(activation, skip_due_wake=force)
        actions = self._coordinator.list_execution_actions(activation_id)
        active_called_action_ids = {
            action.execution_action_id
            for action in actions
            if action.call_started_at is not None
            and action.state
            in {
                ExecutionActionState.SUBMITTING,
                ExecutionActionState.UNKNOWN,
                ExecutionActionState.OPEN,
            }
        }
        for action_id in tuple(self._last_called_action_query):
            if action_id not in active_called_action_ids:
                self._last_called_action_query.pop(action_id, None)
        # While a plan is only waiting for entry, Nautilus owns market streaming,
        # strategy evaluation and account cache updates. There is no persisted
        # execution responsibility for this fallback loop to reconcile yet.
        if activation.lifecycle is PlanLifecycle.RUNNING and not actions:
            return
        now = self._loop.time()
        last_sync = self._last_fallback_sync.get(activation_id)
        if (
            not force
            and activation.lifecycle is PlanLifecycle.RUNNING
            and last_sync is not None
            and now - last_sync < 10.0
        ):
            return
        self._last_fallback_sync[activation_id] = now
        try:
            facts = await self._consistent_risk_facts(activation)
            if facts is None:
                return
        except Exception as exc:
            if _failure_reason_code(exc) == "POSITION_ATTRIBUTION_UNKNOWN":
                # A venue-side reducer fill can change the account position
                # before a delayed or missed user-stream callback updates the
                # activation's virtual position. Query only identities Halpha
                # already called; never infer that an unexplained account delta
                # belongs to this activation.
                recovered = await self._query_called_actions_if_due(actions)
                if recovered:
                    facts = await self._consistent_risk_facts(
                        self._coordinator.get_activation_snapshot(activation_id)
                    )
                    if facts is None:
                        return
                else:
                    raise
            else:
                raise
        await self._query_actions_missing_from_open_order_facts(actions, facts)
        if isinstance(facts.position_fact, VenueFact) and (
            _position_fact_matches_activation(activation, facts)
        ):
            self._coordinator.apply_venue_fact(
                facts.position_fact,
                observed_at=facts.checked_at,
            )
        await self._sync_funding(
            activation,
            end_time=facts.checked_at,
            force=False,
        )
        protection_gap = await self._replay_entry_fill_protections(
            activation,
            actions,
            facts,
        )
        activation = self._coordinator.get_activation_snapshot(activation_id)
        protection_gap = (
            protection_gap or activation.protection_state is ProtectionState.GAP
        )
        if activation.lifecycle in {
            PlanLifecycle.USER_TAKEOVER,
            PlanLifecycle.COMPLETED,
        }:
            return
        actions = self._coordinator.list_execution_actions(activation_id)
        for action in actions:
            if action.state is ExecutionActionState.UNKNOWN:
                if action.action_kind is ExecutionActionKind.CANCEL:
                    target_client_order_id = (action.cancel_target or {}).get(
                        "client_order_id"
                    )
                    target = next(
                        (
                            candidate
                            for candidate in actions
                            if candidate.client_order_id == target_client_order_id
                        ),
                        None,
                    )
                    target_fact = (
                        _latest_terminal_order_fact(
                            self._coordinator.list_venue_facts_for_action(
                                target.execution_action_id
                            )
                        )
                        if target is not None
                        else None
                    )
                    if target_fact is not None:
                        self._coordinator.reconcile_cancel_from_target_fact(
                            action.execution_action_id,
                            target_fact=target_fact,
                            observed_at=facts.checked_at,
                        )
                        continue
                self._coordinator.query_unknown_action_if_due(
                    action.execution_action_id,
                    observed_at=facts.checked_at,
                )
        actions = self._coordinator.list_execution_actions(activation_id)
        protection_gap = protection_gap or self._failed_protection_seen(actions)
        unknown_protection_exit_due = self._unknown_protection_exit_due(
            actions,
            observed_at=facts.checked_at,
        )
        for action in actions:
            context = action.action_terms.get("execution_context")
            if (
                not protection_gap
                and activation.lifecycle is PlanLifecycle.RUNNING
                and action.action_kind is ExecutionActionKind.PROTECTION
                and isinstance(action.action_terms.get("quantity"), str)
                and isinstance(context, dict)
                and all(
                    isinstance(context.get(key), str)
                    for key in (
                        "entry_action_ref",
                        "fill_fact_ref",
                        "fill_source_identity",
                    )
                )
                and order_is_working(
                    self._coordinator.list_venue_facts_for_action(
                        action.execution_action_id
                    )
                )
            ):
                # The streaming WORKING callback is only a latency path.  A
                # persisted working protection must be able to recover its TP
                # responsibility in the same process after a dropped task.
                await self._create_take_profits(action.execution_action_id)
        actions = self._coordinator.list_execution_actions(activation_id)
        if not protection_gap:
            self._manage_aggregate_entry_protection(activation, facts, actions)
            actions = self._coordinator.list_execution_actions(activation_id)
            self._manage_aggregate_entry_take_profits(activation, facts, actions)
            actions = self._coordinator.list_execution_actions(activation_id)
        self._resume_ready_non_entry_actions(activation, facts, actions)
        actions = self._coordinator.list_execution_actions(activation_id)
        self._ensure_crossed_take_profit_reductions(activation, facts, actions)
        actions = self._coordinator.list_execution_actions(activation_id)
        if not protection_gap:
            self._manage_dynamic_protection(activation, facts, actions)
            actions = self._coordinator.list_execution_actions(activation_id)
        direct_time_exit_due = _direct_time_exit_due(
            activation,
            observed_at=facts.checked_at,
        )
        entry_cycle_closed = (
            activation.lifecycle is PlanLifecycle.RUNNING
            and Decimal(facts.current_abs_position) == 0
            and self._risk_reduction_fill_seen(actions)
        )
        if activation.lifecycle is PlanLifecycle.EXITING:
            # An explicit plan exit intentionally cancels working reducers
            # before the attributed market exit completes.  That bounded
            # hand-off must keep the user's PLAN_EXIT causation rather than
            # being relabelled as an unexpected protection failure.
            await self._ensure_exit(activation, facts, reason_code="PLAN_EXIT")
        elif direct_time_exit_due:
            # A configured time exit also cancels working reducers before its
            # attributed market exit completes.  The resulting bounded hand-off
            # can temporarily project a protection gap, but the position is
            # already due to close for DIRECT_TIME_EXIT.  Preserve that
            # deterministic causation; genuine pre-deadline gaps still take the
            # fail-closed branch below.
            await self._ensure_exit(
                activation,
                facts,
                reason_code="DIRECT_TIME_EXIT",
            )
        elif protection_gap:
            await self._ensure_exit(
                activation,
                facts,
                reason_code="PROTECTION_GAP",
            )
        elif unknown_protection_exit_due:
            await self._ensure_exit(
                activation,
                facts,
                reason_code="PROTECTION_RESULT_UNKNOWN",
            )
        elif entry_cycle_closed:
            await self._ensure_exit(
                activation,
                facts,
                reason_code="ENTRY_CYCLE_CLOSED",
            )
        if Decimal(facts.current_abs_position) == 0 and (
            activation.lifecycle is PlanLifecycle.EXITING
            or self._risk_reduction_fill_seen(actions)
        ):
            await self._sync_funding(
                activation,
                end_time=facts.checked_at,
                force=True,
            )
        await self._try_close_activation(activation, facts)

    async def _consistent_risk_facts(
        self,
        activation: PlanActivation,
    ) -> ProductRiskReductionFacts | None:
        """Discard one mixed-time account read without failing the runtime."""

        try:
            return await self._fact_provider(activation)
        except Exception as exc:
            if _failure_reason_code(exc) == "ACCOUNT_FACT_SUPERSEDED":
                return None
            raise

    async def _query_actions_missing_from_open_order_facts(
        self,
        actions: tuple[ExecutionAction, ...],
        facts: ProductRiskReductionFacts,
    ) -> None:
        ordinary_open = set(facts.open_order_client_ids)
        algo_open = set(facts.open_algo_client_ids)
        missing: list[ExecutionAction] = []
        for action in actions:
            client_order_id = action.client_order_id
            if (
                client_order_id is None
                or action.action_kind
                not in {
                    ExecutionActionKind.ENTRY,
                    ExecutionActionKind.PROTECTION,
                    ExecutionActionKind.TAKE_PROFIT,
                    ExecutionActionKind.RISK_REDUCTION,
                    ExecutionActionKind.EXIT,
                }
                or action.state
                not in {
                    ExecutionActionState.SUBMITTING,
                    ExecutionActionState.UNKNOWN,
                    ExecutionActionState.OPEN,
                }
            ):
                continue
            if (
                action.action_kind is ExecutionActionKind.EXIT
                and Decimal(facts.current_abs_position) > 0
            ):
                # Residual-position exit recovery is owned by _ensure_exit,
                # which queries the original identity before it may authorize
                # the single bounded successor.  Avoid a duplicate query in
                # the same pass; this missing-open path is needed when the
                # account is already flat and only terminal reconciliation
                # remains.
                continue
            expected_open = (
                algo_open
                if action.action_kind
                in {
                    ExecutionActionKind.PROTECTION,
                    ExecutionActionKind.TAKE_PROFIT,
                }
                else ordinary_open
            )
            if client_order_id not in expected_open:
                if (
                    terminal_order_status(
                        self._coordinator.list_venue_facts_for_action(
                            action.execution_action_id
                        )
                    )
                    is not None
                ):
                    continue
                missing.append(action)
        await self._query_called_actions_if_due(tuple(missing))

    async def _query_called_actions_if_due(
        self,
        actions: tuple[ExecutionAction, ...],
    ) -> int:
        now = self._loop.time()
        recovered = 0
        for action in actions:
            if (
                action.state
                not in {
                    ExecutionActionState.SUBMITTING,
                    ExecutionActionState.UNKNOWN,
                    ExecutionActionState.OPEN,
                }
                or action.call_started_at is None
            ):
                continue
            last_query = self._last_called_action_query.get(
                action.execution_action_id
            )
            if (
                last_query is not None
                and now - last_query
                < CALLED_ACTION_RECONCILIATION_INTERVAL_SECONDS
            ):
                continue
            queried = False
            action_recovered = 0
            provider = self._called_action_recovery_fact_provider
            if provider is not None:
                queried = True
                try:
                    recovered_facts = await provider(action)
                except Exception:
                    # Keep the original responsibility unresolved and fall
                    # back to Nautilus' identity query.  A failed read-only
                    # enrichment must never authorize a replacement write.
                    recovered_facts = ()
                for fact in recovered_facts:
                    self._coordinator.apply_venue_fact(
                        fact,
                        observed_at=fact.received_at,
                    )
                    action_recovered += 1
                    recovered += 1
            if not action_recovered and self._coordinator.query_called_action_identity(
                action.execution_action_id
            ):
                queried = True
            if queried:
                self._last_called_action_query[action.execution_action_id] = now
        return recovered

    async def _sync_funding(
        self,
        activation: PlanActivation,
        *,
        end_time: datetime,
        force: bool,
    ) -> None:
        provider = self._funding_provider
        # A plan which never filled cannot own funding income.  Avoid both an
        # unnecessary signed account query and a misleading accounting warning
        # for normal zero-fill outcomes such as a Post Only rejection.
        if provider is None or not activation.has_entry_fill:
            return
        now = self._loop.time()
        last_sync = self._last_funding_sync.get(activation.activation_id)
        if not force and last_sync is not None and now - last_sync < 300.0:
            return
        self._last_funding_sync[activation.activation_id] = now
        try:
            records = await provider(activation, end_time)
            self._coordinator.record_funding_income(
                activation_id=activation.activation_id,
                records=records,
                observed_at=end_time,
            )
        except Exception as exc:
            # Funding is an accounting enrichment, not a prerequisite for
            # protection, exit, or lifecycle progress.  A temporarily
            # unavailable signed read therefore remains visible as stale
            # accounting data without being reported as a responsibility
            # failure.
            if self._funding_fact_unavailable_sink is not None:
                self._funding_fact_unavailable_sink(exc)
            else:
                self._loop.call_exception_handler(
                    {
                        "message": "HALPHA_FUNDING_SYNC_FAILED",
                        "exception": exc,
                    }
                )

    async def _sync_user_takeover(
        self,
        activation: PlanActivation,
        *,
        force: bool,
    ) -> None:
        """Hand over never-called actions and retain only read-only reconciliation."""

        now = self._loop.time()
        last_sync = self._last_fallback_sync.get(activation.activation_id)
        if not force and last_sync is not None and now - last_sync < 10.0:
            return
        self._last_fallback_sync[activation.activation_id] = now
        observed_at = datetime.now(UTC)
        self._coordinator.apply_persisted_user_takeover(
            activation_id=activation.activation_id,
            observed_at=observed_at,
        )
        actions = self._coordinator.list_execution_actions(activation.activation_id)
        for action in actions:
            if (
                action.action_kind is ExecutionActionKind.CANCEL
                and action.state is ExecutionActionState.UNKNOWN
            ):
                target_client_order_id = (action.cancel_target or {}).get(
                    "client_order_id"
                )
                target = next(
                    (
                        candidate
                        for candidate in actions
                        if candidate.client_order_id == target_client_order_id
                    ),
                    None,
                )
                target_fact = (
                    _latest_terminal_order_fact(
                        self._coordinator.list_venue_facts_for_action(
                            target.execution_action_id
                        )
                    )
                    if target is not None
                    else None
                )
                if target_fact is not None:
                    self._coordinator.reconcile_cancel_from_target_fact(
                        action.execution_action_id,
                        target_fact=target_fact,
                        observed_at=observed_at,
                    )
                    continue
            self._coordinator.query_called_action_identity(action.execution_action_id)
        refreshed = self._coordinator.get_activation_snapshot(activation.activation_id)
        if refreshed.lifecycle is not PlanLifecycle.USER_TAKEOVER:
            return
        facts = await self._consistent_risk_facts(refreshed)
        if facts is None:
            return
        await self._try_close_activation(refreshed, facts)

    async def _replay_entry_fill_protections(
        self,
        activation: PlanActivation,
        actions: tuple[ExecutionAction, ...],
        facts: ProductRiskReductionFacts,
    ) -> bool:
        """Idempotently derive protection from every durable entry fill.

        The stream callback is only a latency optimization.  A callback task or
        transaction failure must be recoverable on the next ordinary sync,
        without requiring a process restart.
        """

        confirmed_gap = False
        for action in actions:
            if action.action_kind is not ExecutionActionKind.ENTRY:
                continue
            for fill in self._coordinator.list_venue_facts_for_action(
                action.execution_action_id
            ):
                if fill.kind is not VenueFactKind.FILL:
                    continue
                if (
                    getattr(fill, "action_ref", None) != action.execution_action_id
                    or getattr(fill, "activation_ref", None) != activation.activation_id
                ):
                    continue
                protection = await self._protect_fill(fill, risk_facts=facts)
                confirmed_gap = confirmed_gap or protection is None
        return confirmed_gap

    def _schedule(self, key: str, coroutine: Coroutine[Any, Any, None]) -> None:
        if key in self._completed_task_keys:
            self._completed_task_keys.move_to_end(key)
            coroutine.close()
            return
        existing = self._tasks.get(key)
        if existing is not None and (
            not existing.done()
            or (not existing.cancelled() and existing.exception() is None)
        ):
            coroutine.close()
            return
        task = self._loop.create_task(coroutine)
        self._tasks[key] = task
        task.add_done_callback(
            lambda completed, task_key=key: self._report_failure(
                task_key,
                completed,
            )
        )

    def _arm_direct_time_exit(
        self,
        activation: PlanActivation,
        *,
        skip_due_wake: bool = False,
    ) -> None:
        """Wake once at the frozen deadline without increasing normal REST polling."""

        activation_id = activation.activation_id
        if activation.lifecycle is not PlanLifecycle.RUNNING:
            self._clear_direct_time_exit_wake(activation_id)
            return
        deadline = direct_time_exit_at(activation)
        if deadline is None:
            return
        if self._direct_time_exit_woken.get(activation_id) == deadline:
            return
        current = self._direct_time_exit_handles.get(activation_id)
        if skip_due_wake and deadline <= datetime.now(UTC):
            if current is not None:
                current[1].cancel()
                self._direct_time_exit_handles.pop(activation_id, None)
            return
        if current is not None:
            current_deadline, current_handle = current
            if current_deadline == deadline and not current_handle.cancelled():
                return
            current_handle.cancel()
        delay = max(
            0.0,
            (deadline - datetime.now(UTC)).total_seconds(),
        )
        handle = self._loop.call_later(
            delay,
            self._wake_direct_time_exit,
            activation_id,
            deadline,
        )
        self._direct_time_exit_handles[activation_id] = (deadline, handle)

    def _wake_direct_time_exit(
        self,
        activation_id: str,
        deadline: datetime,
    ) -> None:
        current = self._direct_time_exit_handles.get(activation_id)
        if current is None or current[0] != deadline:
            return
        self._direct_time_exit_handles.pop(activation_id, None)
        self._direct_time_exit_woken[activation_id] = deadline
        self._schedule(
            f"DIRECT_TIME_EXIT:{activation_id}:{deadline.isoformat()}",
            self.sync(activation_id, force=True),
        )

    def _clear_direct_time_exit_wake(self, activation_id: str) -> None:
        current = self._direct_time_exit_handles.pop(activation_id, None)
        if current is not None:
            current[1].cancel()
        self._direct_time_exit_woken.pop(activation_id, None)

    def _report_failure(self, key: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is None:
            self._completed_task_keys[key] = None
            self._completed_task_keys.move_to_end(key)
            while (
                len(self._completed_task_keys)
                > COMPLETED_RESPONSIBILITY_TASK_KEY_LIMIT
            ):
                self._completed_task_keys.popitem(last=False)
        else:
            if self._failure_sink is not None:
                try:
                    self._failure_sink(exception)
                except Exception as sink_exception:
                    self._loop.call_exception_handler(
                        {
                            "message": (
                                "HALPHA_PRODUCT_RESPONSIBILITY_FAILURE_SINK_FAILED"
                            ),
                            "exception_type": type(sink_exception).__name__,
                        }
                    )
            else:
                self._loop.call_exception_handler(
                    {
                        "message": "HALPHA_PRODUCT_RESPONSIBILITY_FAILED",
                        "exception": exception,
                        "task": task,
                    }
                )

    async def _resume(self, activation_id: str) -> None:
        for action in self._coordinator.list_execution_actions(activation_id):
            if action.action_kind is ExecutionActionKind.ENTRY:
                for fact in self._coordinator.list_venue_facts_for_action(
                    action.execution_action_id
                ):
                    if fact.kind is VenueFactKind.FILL:
                        await self._protect_fill(fact)
            elif (
                action.action_kind is ExecutionActionKind.PROTECTION
                and order_is_working(
                    self._coordinator.list_venue_facts_for_action(
                        action.execution_action_id
                    )
                )
            ):
                await self._create_take_profits(action.execution_action_id)
        # Resume is also the fail-closed reconciliation boundary.  In
        # particular, a persisted fill whose protection could not be compiled
        # must immediately form an exit instead of waiting for a periodic tick.
        await self.sync(activation_id, force=True)

    async def _protect_fill_and_sync(self, fill: VenueFact) -> None:
        """Protect one callback fill, then reconcile its aggregate risk state."""

        await self._protect_fill(fill)
        activation_ref = getattr(fill, "activation_ref", None)
        if isinstance(activation_ref, str):
            await self.sync(activation_ref, force=True)

    async def _protect_fill(
        self,
        fill: VenueFact,
        *,
        risk_facts: ProductRiskReductionFacts | None = None,
    ) -> ExecutionAction | None:
        action_ref = getattr(fill, "action_ref", None)
        activation_ref = getattr(fill, "activation_ref", None)
        if action_ref is None or activation_ref is None:
            return None
        activation = self._coordinator.get_activation_snapshot(activation_ref)
        if activation.lifecycle not in {
            PlanLifecycle.RUNNING,
            PlanLifecycle.EXITING,
        }:
            return None
        existing = next(
            (
                action
                for action in self._coordinator.list_execution_actions(
                    activation.activation_id
                )
                if action.action_kind is ExecutionActionKind.PROTECTION
                and action.action_terms.get("execution_context", {}).get(
                    "fill_fact_ref"
                )
                == fill.venue_fact_id
            ),
            None,
        )
        if existing is not None and existing.state is not ExecutionActionState.READY:
            return existing
        facts = risk_facts
        if facts is None:
            facts = await self._consistent_risk_facts(activation)
        if facts is None:
            return None
        quantity = str(fill.payload["last_quantity"])
        check = facts.action_check(
            activation,
            action_profile="PROTECTIVE_STOP_REDUCE_ONLY",
            control_category=StopCategory.PROTECTION,
            quantity=quantity,
            confirmed_position_floor=quantity,
        )
        source_identity = venue_source_identity(
            activation_id=activation.activation_id,
            rule_id="PROTECTION_AFTER_FILL",
            source_class=fill.source_class.value,
            source_object_id=fill.source_object_id,
            source_sequence_or_version=fill.source_sequence,
        )
        if existing is None:
            result = self._coordinator.create_protection_for_fill(
                fill_fact=fill,
                plan_event_id=_stable_id(
                    self._environment_id,
                    "plan-event",
                    source_identity,
                ),
                execution_action_id=_stable_id(
                    self._environment_id, "execution-action", source_identity
                ),
                action_check=check,
                observed_at=facts.checked_at,
                client_order_id=_stable_client_order_id(
                    self._environment_id, source_identity
                ),
            )
            action = result.execution_action
        else:
            action = existing
        if action is not None:
            self._submit_ready(action, check, observed_at=facts.checked_at)
        return action

    async def _create_take_profits(self, protection_action_id: str) -> None:
        protection = self._coordinator.get_execution_action(protection_action_id)
        if not order_is_working(
            self._coordinator.list_venue_facts_for_action(protection_action_id)
        ):
            return
        context = protection.action_terms.get("execution_context")
        quantity = protection.action_terms.get("quantity")
        if not isinstance(context, dict) or not isinstance(quantity, str):
            raise ValueError("PROTECTION_UNKNOWN")
        if isinstance(context.get("protection_replacement"), dict):
            return
        required = ("entry_action_ref", "fill_fact_ref", "fill_source_identity")
        if any(not isinstance(context.get(key), str) for key in required):
            raise ValueError("PROTECTION_UNKNOWN")
        activation = self._coordinator.get_activation_snapshot(protection.activation_id)
        if activation.lifecycle is not PlanLifecycle.RUNNING:
            return
        if isinstance(context.get("direct_fill"), dict):
            await self._create_direct_take_profits(
                activation,
                protection,
                context,
            )
            return
        proposed = proposed_take_profits_from_fill(
            activation,
            entry_action_ref=str(context["entry_action_ref"]),
            protection_action_ref=protection.execution_action_id,
            fill_fact_ref=str(context["fill_fact_ref"]),
            fill_source_identity=str(context["fill_source_identity"]),
            fill_quantity=quantity,
        )
        facts = await self._consistent_risk_facts(activation)
        if facts is None:
            return
        checks = tuple(
            facts.action_check(
                activation,
                action_profile=item.action_profile,
                control_category=StopCategory.RISK_REDUCTION_OR_ORDER_MANAGEMENT,
                quantity=str(item.quantity),
                confirmed_position_floor=quantity,
            )
            for item in proposed
        )
        source = f"{protection.execution_action_id}:TAKE_PROFITS"
        action_ids = tuple(
            _stable_id(self._environment_id, f"execution-action-{index}", source)
            for index in (1, 2)
        )
        results = self._coordinator.create_take_profits_for_protected_fill(
            protection_action_id=protection.execution_action_id,
            fill_fact_ref=str(context["fill_fact_ref"]),
            fill_source_identity=str(context["fill_source_identity"]),
            fill_quantity=quantity,
            plan_event_ids=tuple(
                _stable_id(self._environment_id, f"plan-event-{index}", source)
                for index in (1, 2)
            ),
            execution_action_ids=action_ids,
            action_checks=checks,
            observed_at=facts.checked_at,
            client_order_ids=tuple(
                _stable_client_order_id(self._environment_id, f"{source}:{index}")
                for index in (1, 2)
            ),
        )
        for result, check in zip(results, checks, strict=True):
            action = result.execution_action
            if action is not None:
                self._submit_ready(action, check, observed_at=facts.checked_at)

    async def _create_direct_take_profits(
        self,
        activation: PlanActivation,
        protection: ExecutionAction,
        context: dict[str, object],
    ) -> None:
        entry_action_ref = context.get("entry_action_ref")
        fill_fact_ref = context.get("fill_fact_ref")
        fill_source_identity = context.get("fill_source_identity")
        if not all(
            isinstance(value, str)
            for value in (entry_action_ref, fill_fact_ref, fill_source_identity)
        ):
            raise ValueError("PROTECTION_UNKNOWN")
        proposed = proposed_direct_take_profits_from_fill(
            activation,
            entry_action_ref=str(entry_action_ref),
            protection_action_ref=protection.execution_action_id,
            fill_fact_ref=str(fill_fact_ref),
            fill_source_identity=str(fill_source_identity),
        )
        if not proposed:
            return
        protected_quantity = protection.action_terms.get("quantity")
        if not isinstance(protected_quantity, str):
            raise ValueError("PROTECTION_UNKNOWN")
        facts = await self._consistent_risk_facts(activation)
        if facts is None:
            return
        checks = tuple(
            facts.action_check(
                activation,
                action_profile=item.action_profile,
                control_category=StopCategory.RISK_REDUCTION_OR_ORDER_MANAGEMENT,
                quantity=str(item.quantity),
                confirmed_position_floor=protected_quantity,
            )
            for item in proposed
        )
        source = f"{protection.execution_action_id}:DIRECT_TAKE_PROFITS"
        ordinals = tuple(range(1, len(proposed) + 1))
        results = self._coordinator.create_direct_take_profits_for_protected_fill(
            protection_action_id=protection.execution_action_id,
            fill_fact_ref=str(fill_fact_ref),
            fill_source_identity=str(fill_source_identity),
            plan_event_ids=tuple(
                _stable_id(self._environment_id, f"plan-event-{index}", source)
                for index in ordinals
            ),
            execution_action_ids=tuple(
                _stable_id(self._environment_id, f"execution-action-{index}", source)
                for index in ordinals
            ),
            action_checks=checks,
            observed_at=facts.checked_at,
            client_order_ids=tuple(
                _stable_client_order_id(self._environment_id, f"{source}:{index}")
                for index in ordinals
            ),
        )
        for result, check in zip(results, checks, strict=True):
            action = result.execution_action
            if action is not None:
                self._submit_ready(action, check, observed_at=facts.checked_at)

    async def _ensure_exit(
        self,
        activation: PlanActivation,
        facts: ProductRiskReductionFacts,
        *,
        reason_code: str,
    ) -> None:
        actions = self._coordinator.list_execution_actions(activation.activation_id)
        unresolved_entry = False
        for target in actions:
            if target.action_kind is not ExecutionActionKind.ENTRY:
                continue
            if target.state is ExecutionActionState.READY:
                self._coordinator.reject_execution_action_before_submission(
                    target.execution_action_id,
                    reason_code=f"{reason_code}_ENTRY_NOT_SUBMITTED",
                    observed_at=facts.checked_at,
                )
                continue
            if target.state in {
                ExecutionActionState.SUBMITTING,
                ExecutionActionState.UNKNOWN,
            }:
                unresolved_entry = True
                continue
            if target.state is not ExecutionActionState.OPEN:
                continue
            target_facts = self._coordinator.list_venue_facts_for_action(
                target.execution_action_id
            )
            terminal_status = terminal_order_status(target_facts)
            if terminal_status is not None:
                if (
                    terminal_fills_complete(target, target_facts)
                    or terminal_status == "FILLED"
                    or terminal_fills_accounted_for_exit(
                        target,
                        target_facts,
                    )
                ):
                    # A FILLED order, or a CANCELLED/EXPIRED order whose
                    # terminal cumulative fill is fully persisted, cannot
                    # create another entry fill. If venue quantity exceeded
                    # the immutable action, the refreshed position and all
                    # persisted fills below remain the authority for sizing a
                    # reduce-only exit; strict action closure stays blocked.
                    continue
                unresolved_entry = True
                continue
            unresolved_entry = True
            if not order_is_working(target_facts) or target.client_order_id is None:
                continue
            self._ensure_cancel(
                activation,
                facts,
                target,
                target_endpoint="ORDINARY",
                reason_code=reason_code,
            )

        # An unresolved entry keeps the activation and all further entry risk
        # blocked.  It must not, however, strand a separately proven current
        # position: a fresh reduce-only exit cannot reverse the one-way account,
        # and any late entry fill remains attributable and is reduced again.
        if unresolved_entry and Decimal(facts.current_abs_position) == 0:
            return

        actions = self._coordinator.list_execution_actions(activation.activation_id)
        exit_actions = tuple(
            action
            for action in actions
            if action.action_kind is ExecutionActionKind.EXIT
        )
        if Decimal(facts.current_abs_position) > 0:
            position_fact = facts.position_fact
            if position_fact is None:
                raise ValueError("POSITION_FACT_REQUIRED")
            if not _position_attribution_proven(
                activation,
                facts,
                actions,
                self._coordinator,
            ):
                if _execution_state_changed_after_facts(
                    facts,
                    actions,
                    self._coordinator,
                ):
                    # A user-stream fill can land while the signed position
                    # query is in flight.  The old snapshot must authorize no
                    # successor, but this ordinary race is not corruption and
                    # must not crash the executor.  The next sync re-queries.
                    return
                raise ValueError("POSITION_ATTRIBUTION_UNKNOWN")
            if not self._prepare_reducers_for_explicit_exit(
                activation,
                facts,
                actions,
                reason_code=reason_code,
            ):
                return
            predecessor: ExecutionAction | None = None
            unresolved_predecessor = False
            exit_quantity = facts.current_abs_position
            reason_ref: str
            exit_responsibility_role = ExitResponsibilityRole.PRIMARY_EXIT
            bounded_successors = tuple(
                action for action in exit_actions if _is_exit_successor(action)
            )
            cleanup_actions = tuple(
                action for action in exit_actions if _is_post_successor_cleanup(action)
            )
            called_successors = tuple(
                action
                for action in bounded_successors
                if not _action_proven_never_called(action)
            )
            if called_successors:
                anchor = called_successors[-1]
                cleanup = _post_successor_late_entry_cleanup(
                    anchor,
                    actions=actions,
                    cleanup_actions=cleanup_actions,
                    current_abs_position=facts.current_abs_position,
                    coordinator=self._coordinator,
                )
                if cleanup is None:
                    return
                exit_quantity, claim_digest = cleanup
                reason_ref = (
                    f"{activation.activation_id}:EXIT:{reason_code}:"
                    f"POST_SUCCESSOR_LATE_ENTRY_CLEANUP:"
                    f"{anchor.execution_action_id}:{claim_digest}"
                )
                exit_responsibility_role = (
                    ExitResponsibilityRole.POST_SUCCESSOR_LATE_ENTRY_CLEANUP
                )
            elif exit_actions:
                unresolved_exits = tuple(
                    action
                    for action in exit_actions
                    if not _is_post_successor_cleanup(action)
                    and not _exit_action_resolved(action, self._coordinator)
                )
                if unresolved_exits:
                    if len(unresolved_exits) != 1 or not _unknown_exit_successor_due(
                        unresolved_exits[0],
                        position_fact=position_fact,
                        current_abs_position=facts.current_abs_position,
                        observed_at=facts.checked_at,
                    ):
                        return
                    predecessor = unresolved_exits[0]
                    unresolved_predecessor = True
                else:
                    predecessor = exit_actions[-1]
                if _is_post_successor_cleanup(predecessor):
                    return
                local_replacement = (
                    predecessor
                    if _is_exit_successor(predecessor)
                    and _action_proven_never_called(predecessor)
                    else (
                        bounded_successors[-1]
                        if bounded_successors
                        and all(
                            _action_proven_never_called(action)
                            for action in bounded_successors
                        )
                        else None
                    )
                )
                reason_ref = (
                    f"{activation.activation_id}:EXIT:{reason_code}:"
                    f"EXIT_SUCCESSOR:{predecessor.execution_action_id}"
                )
                exit_responsibility_role = ExitResponsibilityRole.EXIT_SUCCESSOR
                if local_replacement is not None:
                    reason_ref = (
                        f"{reason_ref}:LOCAL_REPLACEMENT:"
                        f"{local_replacement.execution_action_id}:"
                        f"{position_fact.venue_fact_id}"
                    )
            else:
                reason_ref = f"{activation.activation_id}:EXIT:{reason_code}"
            if unresolved_predecessor and not (
                self._coordinator.query_called_action_identity(
                    predecessor.execution_action_id
                )
            ):
                return
            self._coordinator.apply_venue_fact(
                position_fact,
                observed_at=position_fact.received_at,
            )
            source_identity = (
                f"{activation.activation_id}:EXIT:"
                f"{position_fact.venue_fact_id}:{reason_ref}"
            )
            check = facts.action_check(
                activation,
                action_profile="REDUCE_OR_CLOSE_MARKET",
                control_category=StopCategory.RISK_REDUCTION_OR_ORDER_MANAGEMENT,
                quantity=exit_quantity,
            )
            result = self._coordinator.create_position_exit(
                activation_id=activation.activation_id,
                position_quantity=exit_quantity,
                position_fact_ref=position_fact.venue_fact_id,
                exit_responsibility_role=exit_responsibility_role,
                reason_ref=reason_ref,
                plan_event_id=_stable_id(
                    self._environment_id,
                    "plan-event-exit",
                    source_identity,
                ),
                execution_action_id=_stable_id(
                    self._environment_id,
                    "execution-action-exit",
                    reason_ref,
                ),
                action_check=check,
                observed_at=facts.checked_at,
                client_order_id=_stable_client_order_id(
                    self._environment_id,
                    reason_ref,
                ),
            )
            action = result.execution_action
            if action is not None:
                self._submit_ready(
                    action,
                    check,
                    observed_at=facts.checked_at,
                )
            return
        for target in actions:
            if target.action_kind not in {
                ExecutionActionKind.ENTRY,
                ExecutionActionKind.PROTECTION,
                ExecutionActionKind.TAKE_PROFIT,
            }:
                continue
            if target.state is ExecutionActionState.READY:
                self._coordinator.reject_execution_action_before_submission(
                    target.execution_action_id,
                    reason_code=f"{reason_code}_NO_POSITION",
                    observed_at=facts.checked_at,
                )
                continue
            if (
                target.state is not ExecutionActionState.OPEN
                or not order_is_working(
                    self._coordinator.list_venue_facts_for_action(
                        target.execution_action_id
                    )
                )
                or target.client_order_id is None
            ):
                continue
            self._ensure_cancel(
                activation,
                facts,
                target,
                target_endpoint=(
                    "ORDINARY"
                    if target.action_kind is ExecutionActionKind.ENTRY
                    else "ALGO"
                ),
                reason_code=reason_code,
            )

    def _prepare_reducers_for_explicit_exit(
        self,
        activation: PlanActivation,
        facts: ProductRiskReductionFacts,
        actions: tuple[ExecutionAction, ...],
        *,
        reason_code: str,
    ) -> bool:
        """Cancel this plan's resting reducers before a market exit.

        Binance one-way mode keeps one account position. A stale stop or
        take-profit left behind after this activation exits could otherwise
        reduce another activation's virtual share.
        """

        if reason_code == "PROTECTION_RESULT_UNKNOWN":
            # Once the venue result of the only protection is unknown past its
            # bounded window, waiting on that same unknown identity cannot
            # prove safety. Submit the bounded reduce-only exit and continue
            # querying/cancelling the original identity afterward.
            return True
        waiting = False
        for target in actions:
            if target.action_kind not in {
                ExecutionActionKind.PROTECTION,
                ExecutionActionKind.TAKE_PROFIT,
            }:
                continue
            if target.state is ExecutionActionState.READY:
                self._coordinator.reject_execution_action_before_submission(
                    target.execution_action_id,
                    reason_code=f"{reason_code}_SUPERSEDED_BY_EXPLICIT_EXIT",
                    observed_at=facts.checked_at,
                )
                continue
            if target.state in {
                ExecutionActionState.NOT_SUBMITTED,
                ExecutionActionState.CLOSED,
                ExecutionActionState.HANDED_OVER,
            }:
                continue
            target_facts = self._coordinator.list_venue_facts_for_action(
                target.execution_action_id
            )
            if terminal_order_status(target_facts) is not None:
                continue
            if target.state in {
                ExecutionActionState.SUBMITTING,
                ExecutionActionState.UNKNOWN,
            }:
                waiting = True
                continue
            if (
                target.state is ExecutionActionState.OPEN
                and target.client_order_id is not None
                and order_is_working(target_facts)
            ):
                self._ensure_cancel(
                    activation,
                    facts,
                    target,
                    target_endpoint="ALGO",
                    reason_code=f"{reason_code}_BEFORE_POSITION_EXIT",
                )
            waiting = True
        return not waiting

    def _ensure_cancel(
        self,
        activation: PlanActivation,
        facts: ProductRiskReductionFacts,
        target: ExecutionAction,
        *,
        target_endpoint: str,
        reason_code: str,
    ) -> None:
        matching = tuple(
            action
            for action in self._coordinator.list_execution_actions(
                activation.activation_id
            )
            if action.action_kind is ExecutionActionKind.CANCEL
            and (action.cancel_target or {}).get("client_order_id")
            == target.client_order_id
        )
        ready = next(
            (
                action
                for action in matching
                if action.state is ExecutionActionState.READY
            ),
            None,
        )
        if ready is not None:
            self._submit_ready(
                ready,
                facts.cancel_check(activation),
                observed_at=facts.checked_at,
            )
            return
        active = tuple(
            action
            for action in matching
            if action.state
            in {
                ExecutionActionState.SUBMITTING,
                ExecutionActionState.UNKNOWN,
                ExecutionActionState.OPEN,
            }
        )
        predecessor: ExecutionAction | None = None
        if active:
            if (
                len(active) != 1
                or any(
                    ":CANCEL_SUCCESSOR:" in str(getattr(action, "source_identity", ""))
                    for action in matching
                )
                or not _unknown_cancel_successor_due(
                    active[0],
                    target_facts=self._coordinator.list_venue_facts_for_action(
                        target.execution_action_id
                    ),
                    observed_at=facts.checked_at,
                )
            ):
                return
            predecessor = active[0]
        if predecessor is not None:
            reason_ref = (
                f"{activation.activation_id}:EXIT_CANCEL:{reason_code}:"
                f"{target.execution_action_id}:CANCEL_SUCCESSOR:"
                f"{predecessor.execution_action_id}"
            )
        else:
            reason_ref = (
                f"{activation.activation_id}:EXIT_CANCEL:{reason_code}:"
                f"{target.execution_action_id}:v{target.state_version}"
            )
        result = self._coordinator.create_cancel_for_action(
            activation_id=activation.activation_id,
            target_action_id=target.execution_action_id,
            target_endpoint=target_endpoint,
            plan_event_id=_stable_id(
                self._environment_id,
                "plan-event-cancel",
                reason_ref,
            ),
            execution_action_id=_stable_id(
                self._environment_id,
                "execution-action-cancel",
                reason_ref,
            ),
            action_check=facts.cancel_check(activation),
            reason_ref=reason_ref,
            observed_at=facts.checked_at,
            client_order_id=None,
        )
        action = result.execution_action
        if action is not None:
            self._submit_ready(
                action,
                facts.cancel_check(activation),
                observed_at=facts.checked_at,
            )

    def _manage_aggregate_entry_protection(
        self,
        activation: PlanActivation,
        facts: ProductRiskReductionFacts,
        actions: tuple[ExecutionAction, ...],
    ) -> None:
        """Reprice owned fill stops to the latest average-cost, capped target."""

        snapshot = activation.order_schedule_snapshot
        if (
            snapshot is None
            or snapshot.full_fill_protection_estimate is None
            or activation.lifecycle is not PlanLifecycle.RUNNING
        ):
            return
        state = activation.rule_state.get("direct_protection")
        fills = state.get("fills") if isinstance(state, dict) else None
        if not activation.has_entry_fill and (
            state is None or (isinstance(fills, dict) and not fills)
        ):
            # Aggregate targets are derived only after the first confirmed
            # direct-entry fill.  A working entry order therefore has no
            # protection aggregate to manage yet and is not an invariant
            # violation.  Once any fill is recorded, malformed or missing
            # aggregate state must continue to fail closed below.
            return
        target_state = state.get("aggregate_target") if isinstance(state, dict) else None
        revision = state.get("aggregate_revision") if isinstance(state, dict) else None
        target_price = (
            target_state.get("initial_stop_price")
            if isinstance(target_state, dict)
            else None
        )
        if (
            not isinstance(fills, dict)
            or not isinstance(target_price, str)
            or not isinstance(revision, int)
            or revision < 1
        ):
            raise ValueError("PROTECTION_AGGREGATE_TARGET_UNKNOWN")
        protections_by_fill: dict[str, list[ExecutionAction]] = {}
        for action in actions:
            if action.action_kind is not ExecutionActionKind.PROTECTION:
                continue
            context = action.action_terms.get("execution_context")
            fill_ref = context.get("fill_fact_ref") if isinstance(context, dict) else None
            if isinstance(fill_ref, str):
                protections_by_fill.setdefault(fill_ref, []).append(action)
        for fill_ref in fills:
            protections = protections_by_fill.get(fill_ref, [])
            working = [
                action
                for action in protections
                if action.state is ExecutionActionState.OPEN
                and order_is_working(
                    self._coordinator.list_venue_facts_for_action(
                        action.execution_action_id
                    )
                )
            ]
            desired = next(
                (
                    action
                    for action in protections
                    if (
                        action.action_terms.get("execution_context", {})
                        .get("protection_replacement", {})
                        .get("kind")
                        == "ENTRY_AGGREGATE_REBALANCE"
                        and action.action_terms["execution_context"][
                            "protection_replacement"
                        ].get("step_index")
                        == revision
                    )
                ),
                None,
            )
            if desired is not None:
                if desired.state is ExecutionActionState.READY:
                    if self._replacement_waits_for_predecessor_cancel(
                        activation,
                        facts,
                        desired,
                        protections,
                    ):
                        continue
                    quantity = desired.action_terms.get("quantity")
                    if isinstance(quantity, str):
                        self._submit_ready(
                            desired,
                            facts.action_check(
                                activation,
                                action_profile="PROTECTIVE_STOP_REDUCE_ONLY",
                                control_category=StopCategory.PROTECTION,
                                quantity=quantity,
                            ),
                            observed_at=facts.checked_at,
                        )
                    continue
                if desired in working:
                    for older in working:
                        if older.execution_action_id != desired.execution_action_id:
                            self._ensure_cancel(
                                activation,
                                facts,
                                older,
                                target_endpoint="ALGO",
                                reason_code=f"ENTRY_AGGREGATE_REBALANCE_{revision}",
                            )
                continue
            if not working:
                continue
            predecessor = max(working, key=lambda item: item.created_at)
            predecessor_trigger = predecessor.action_terms.get("trigger_price")
            quantity = predecessor.action_terms.get("quantity")
            if (
                not isinstance(predecessor_trigger, str)
                or not isinstance(quantity, str)
                or Decimal(predecessor_trigger) == Decimal(target_price)
            ):
                continue
            predecessor_replacement = (
                predecessor.action_terms.get("execution_context", {})
                .get("protection_replacement")
            )
            target_is_looser = (
                Decimal(target_price) < Decimal(predecessor_trigger)
                if activation.direction is Direction.LONG
                else Decimal(target_price) > Decimal(predecessor_trigger)
            )
            if (
                target_is_looser
                and isinstance(predecessor_replacement, dict)
                and predecessor_replacement.get("kind") == "DYNAMIC_TIGHTEN"
            ):
                # A later entry cannot undo an already-earned profit lock.
                continue
            source = (
                f"{activation.activation_id}:ENTRY_AGGREGATE_REBALANCE:"
                f"{fill_ref}:{revision}"
            )
            check = facts.action_check(
                activation,
                action_profile="PROTECTIVE_STOP_REDUCE_ONLY",
                control_category=StopCategory.PROTECTION,
                quantity=quantity,
            )
            result = self._coordinator.create_direct_protection_replacement(
                activation_id=activation.activation_id,
                predecessor_action_id=predecessor.execution_action_id,
                target_trigger_price=target_price,
                step_index=revision,
                replacement_kind="ENTRY_AGGREGATE_REBALANCE",
                plan_event_id=_stable_id(
                    self._environment_id,
                    "plan-event",
                    source,
                ),
                execution_action_id=_stable_id(
                    self._environment_id,
                    "execution-action",
                    source,
                ),
                action_check=check,
                observed_at=facts.checked_at,
                client_order_id=_stable_client_order_id(
                    self._environment_id,
                    source,
                ),
            )
            if result.execution_action is None:
                continue
            if target_is_looser or _venue_position_is_shared(facts):
                # A looser stop must never coexist with the older tighter one;
                # shared venue positions use the same cancel-first hand-off.
                self._ensure_cancel(
                    activation,
                    facts,
                    predecessor,
                    target_endpoint="ALGO",
                    reason_code=f"ENTRY_AGGREGATE_REBALANCE_{revision}",
                )
            else:
                self._submit_ready(
                    result.execution_action,
                    check,
                    observed_at=facts.checked_at,
                )

    def _manage_aggregate_entry_take_profits(
        self,
        activation: PlanActivation,
        facts: ProductRiskReductionFacts,
        actions: tuple[ExecutionAction, ...],
    ) -> None:
        """Cancel stale aggregate TPs and rebuild only proven remaining quantity."""

        snapshot = activation.order_schedule_snapshot
        if (
            snapshot is None
            or snapshot.full_fill_protection_estimate is None
            or activation.lifecycle is not PlanLifecycle.RUNNING
        ):
            return
        state = activation.rule_state.get("direct_protection")
        fills = state.get("fills") if isinstance(state, dict) else None
        if not activation.has_entry_fill and (
            state is None or (isinstance(fills, dict) and not fills)
        ):
            # Take-profit aggregates have the same first-fill boundary as the
            # protective stop aggregate.  Before that fill there is nothing to
            # reconcile; after it, incomplete aggregate state remains fatal.
            return
        target_state = state.get("aggregate_target") if isinstance(state, dict) else None
        targets = (
            target_state.get("take_profit_prices")
            if isinstance(target_state, dict)
            else None
        )
        revision = state.get("aggregate_revision") if isinstance(state, dict) else None
        if (
            not isinstance(fills, dict)
            or not isinstance(targets, (list, tuple))
            or not all(isinstance(item, str) for item in targets)
            or not isinstance(revision, int)
            or revision < 1
        ):
            raise ValueError("TAKE_PROFIT_AGGREGATE_TARGET_UNKNOWN")
        if not targets:
            return

        grouped: dict[tuple[str, int], list[ExecutionAction]] = {}
        for action in actions:
            if action.action_kind is not ExecutionActionKind.TAKE_PROFIT:
                continue
            context = action.action_terms.get("execution_context")
            level = context.get("direct_take_profit") if isinstance(context, dict) else None
            fill_ref = context.get("fill_fact_ref") if isinstance(context, dict) else None
            level_index = level.get("level_index") if isinstance(level, dict) else None
            if isinstance(fill_ref, str) and isinstance(level_index, int):
                grouped.setdefault((fill_ref, level_index), []).append(action)

        for fill_ref in fills:
            for level_index, target_price in enumerate(targets):
                candidates = grouped.get((fill_ref, level_index), [])
                if not candidates:
                    # Initial TP creation consumes the same persisted aggregate
                    # target after its fill protection becomes working.
                    continue
                desired = next(
                    (
                        action
                        for action in candidates
                        if (
                            action.action_terms.get("execution_context", {})
                            .get("take_profit_replacement", {})
                            .get("aggregate_revision")
                            == revision
                        )
                    ),
                    None,
                )
                if desired is not None:
                    if desired.state is ExecutionActionState.READY:
                        quantity = desired.action_terms.get("quantity")
                        if isinstance(quantity, str):
                            self._submit_ready(
                                desired,
                                facts.action_check(
                                    activation,
                                    action_profile=str(
                                        desired.action_terms["action_profile"]
                                    ),
                                    control_category=(
                                        StopCategory.RISK_REDUCTION_OR_ORDER_MANAGEMENT
                                    ),
                                    quantity=quantity,
                                    confirmed_position_floor=quantity,
                                ),
                                observed_at=facts.checked_at,
                            )
                    continue

                unresolved = False
                for action in candidates:
                    trigger = action.action_terms.get("trigger_price")
                    if not isinstance(trigger, str) or Decimal(trigger) == Decimal(
                        target_price
                    ):
                        continue
                    if action.state is ExecutionActionState.READY:
                        self._coordinator.reject_execution_action_before_submission(
                            action.execution_action_id,
                            reason_code="ENTRY_AGGREGATE_TAKE_PROFIT_SUPERSEDED",
                            observed_at=facts.checked_at,
                        )
                        unresolved = True
                    elif action.state is ExecutionActionState.OPEN and order_is_working(
                        self._coordinator.list_venue_facts_for_action(
                            action.execution_action_id
                        )
                    ):
                        self._ensure_cancel(
                            activation,
                            facts,
                            action,
                            target_endpoint="ALGO",
                            reason_code=(
                                f"ENTRY_AGGREGATE_TAKE_PROFIT_REBALANCE_{revision}"
                            ),
                        )
                        unresolved = True
                if unresolved:
                    continue
                if any(
                    action.state is ExecutionActionState.OPEN
                    and Decimal(str(action.action_terms.get("trigger_price")))
                    == Decimal(target_price)
                    and order_is_working(
                        self._coordinator.list_venue_facts_for_action(
                            action.execution_action_id
                        )
                    )
                    for action in candidates
                    if action.action_terms.get("trigger_price") is not None
                ):
                    continue

                predecessor = max(candidates, key=lambda item: item.created_at)
                predecessor_facts = self._coordinator.list_venue_facts_for_action(
                    predecessor.execution_action_id
                )
                definitely_not_submitted = (
                    predecessor.state is ExecutionActionState.NOT_SUBMITTED
                    and predecessor.not_submitted_reason is not None
                )
                if not definitely_not_submitted and not (
                    predecessor.state is ExecutionActionState.CLOSED
                    and terminal_order_status(predecessor_facts)
                    in {"CANCELLED", "EXPIRED"}
                    and terminal_fills_complete(predecessor, predecessor_facts)
                ):
                    continue
                requested = Decimal(str(predecessor.action_terms["quantity"]))
                filled = (
                    Decimal(0)
                    if definitely_not_submitted
                    else sum(
                        (
                            Decimal(str(fact.payload["last_quantity"]))
                            for fact in collapse_synthetic_reconciliation_fills(
                                predecessor_facts
                            )
                            if fact.kind is VenueFactKind.FILL
                        ),
                        Decimal(0),
                    )
                )
                remaining = requested - filled
                if remaining <= 0:
                    continue
                quantity = canonical_decimal(remaining)
                context = predecessor.action_terms.get("execution_context")
                if not isinstance(context, dict):
                    raise ValueError("TAKE_PROFIT_UNKNOWN")
                source = (
                    f"{activation.activation_id}:ENTRY_AGGREGATE_TAKE_PROFIT_REBALANCE:"
                    f"{fill_ref}:{level_index}:{revision}"
                )
                check = facts.action_check(
                    activation,
                    action_profile=str(predecessor.action_terms["action_profile"]),
                    control_category=StopCategory.RISK_REDUCTION_OR_ORDER_MANAGEMENT,
                    quantity=quantity,
                    confirmed_position_floor=quantity,
                )
                result = self._coordinator.create_direct_take_profit_replacement(
                    activation_id=activation.activation_id,
                    predecessor_action_id=predecessor.execution_action_id,
                    target_trigger_price=target_price,
                    aggregate_revision=revision,
                    plan_event_id=_stable_id(
                        self._environment_id,
                        "plan-event",
                        source,
                    ),
                    execution_action_id=_stable_id(
                        self._environment_id,
                        "execution-action",
                        source,
                    ),
                    action_check=check,
                    observed_at=facts.checked_at,
                    client_order_id=_stable_client_order_id(
                        self._environment_id,
                        source,
                    ),
                )
                if result is not None and result.execution_action is not None:
                    self._submit_ready(
                        result.execution_action,
                        check,
                        observed_at=facts.checked_at,
                    )

    def _manage_dynamic_protection(
        self,
        activation: PlanActivation,
        facts: ProductRiskReductionFacts,
        actions: tuple[ExecutionAction, ...],
    ) -> None:
        """Place a tighter typed stop first, then cancel only superseded stops."""

        snapshot = activation.order_schedule_snapshot
        if snapshot is None or activation.lifecycle is not PlanLifecycle.RUNNING:
            return
        stepped_rule = next(
            (
                item
                for item in snapshot.schedule_spec.dynamic_rules
                if isinstance(item, SteppedProtectionRule)
            ),
            None,
        )
        profit_lock_rule = next(
            (
                item
                for item in snapshot.schedule_spec.dynamic_rules
                if isinstance(item, ProfitLockRule)
            ),
            None,
        )
        rule = stepped_rule or profit_lock_rule
        if rule is None or Decimal(facts.current_abs_position) <= 0:
            return
        state = activation.rule_state.get("direct_protection")
        fills = state.get("fills") if isinstance(state, dict) else None
        anchor_ref = state.get("anchor_fill_ref") if isinstance(state, dict) else None
        anchor = fills.get(anchor_ref) if isinstance(fills, dict) else None
        if not isinstance(anchor, dict):
            return
        try:
            anchor_price = Decimal(
                str(
                    state["anchor_price"]
                    if getattr(snapshot, "full_fill_protection_estimate", None)
                    is not None
                    else anchor["fill_price"]
                )
            )
            risk_distance = Decimal(str(state["anchor_r"]))
            price_tick = Decimal(str(anchor["price_tick_size"]))
            reference_price = Decimal(
                facts.current_reference_price or facts.conservative_price
            )
        except (InvalidOperation, KeyError, TypeError, ValueError):
            raise ValueError("DYNAMIC_PROTECTION_FACT_INVALID") from None
        if min(anchor_price, risk_distance, price_tick, reference_price) <= 0:
            raise ValueError("DYNAMIC_PROTECTION_FACT_INVALID")
        if stepped_rule is not None:
            crossed = [
                (index, step)
                for index, step in enumerate(stepped_rule.steps)
                if (
                    reference_price
                    >= anchor_price + Decimal(step.trigger_r) * risk_distance
                    if activation.direction is Direction.LONG
                    else reference_price
                    <= anchor_price - Decimal(step.trigger_r) * risk_distance
                )
            ]
            if not crossed:
                return
            desired_index, desired_step = crossed[-1]
            desired_stop_r = Decimal(desired_step.stop_r)
            reason_prefix = "DIRECT_PROTECTION_STEP"
            source_kind = "DIRECT_STEPPED_PROTECTION"
        else:
            if profit_lock_rule is None:
                return
            profit_r = (
                (reference_price - anchor_price) / risk_distance
                if activation.direction is Direction.LONG
                else (anchor_price - reference_price) / risk_distance
            )
            if profit_r < Decimal(profit_lock_rule.activation_r):
                return
            raw_stop_r = (
                profit_r * Decimal(profit_lock_rule.lock_fraction or "0")
                if profit_lock_rule.mode is ProfitLockMode.RATIO
                else profit_r - Decimal(profit_lock_rule.giveback_r or "0")
            )
            minimum_step_r = Decimal(profit_lock_rule.minimum_step_r)
            desired_units = (raw_stop_r / minimum_step_r).to_integral_value(
                rounding=ROUND_FLOOR
            )
            desired_stop_r = desired_units * minimum_step_r
            desired_index = int(desired_units)
            reason_prefix = "DIRECT_PROFIT_LOCK"
            source_kind = "DIRECT_PROFIT_LOCK"
        raw_target = (
            anchor_price + desired_stop_r * risk_distance
            if activation.direction is Direction.LONG
            else anchor_price - desired_stop_r * risk_distance
        )
        target = (raw_target / price_tick).to_integral_value(
            rounding=(
                ROUND_DOWN if activation.direction is Direction.LONG else ROUND_UP
            )
        ) * price_tick
        target_price = canonical_decimal(target)
        protections_by_fill: dict[str, list[ExecutionAction]] = {}
        for action in actions:
            if action.action_kind is not ExecutionActionKind.PROTECTION:
                continue
            context = action.action_terms.get("execution_context")
            fill_ref = (
                context.get("fill_fact_ref") if isinstance(context, dict) else None
            )
            if isinstance(fill_ref, str):
                protections_by_fill.setdefault(fill_ref, []).append(action)
        for fill_ref, protections in protections_by_fill.items():
            working = [
                action
                for action in protections
                if action.state is ExecutionActionState.OPEN
                and order_is_working(
                    self._coordinator.list_venue_facts_for_action(
                        action.execution_action_id
                    )
                )
            ]
            if not working:
                continue
            replacements = [
                action
                for action in protections
                if isinstance(
                    action.action_terms.get("execution_context", {}).get(
                        "protection_replacement"
                    ),
                    dict,
                )
                and action.action_terms["execution_context"][
                    "protection_replacement"
                ].get("kind", "DYNAMIC_TIGHTEN")
                == "DYNAMIC_TIGHTEN"
            ]
            desired = next(
                (
                    action
                    for action in replacements
                    if action.action_terms["execution_context"][
                        "protection_replacement"
                    ].get("step_index")
                    == desired_index
                ),
                None,
            )
            if desired is not None:
                if desired.state is ExecutionActionState.READY:
                    if self._replacement_waits_for_predecessor_cancel(
                        activation,
                        facts,
                        desired,
                        protections,
                    ):
                        continue
                    quantity = desired.action_terms.get("quantity")
                    if isinstance(quantity, str):
                        self._submit_ready(
                            desired,
                            facts.action_check(
                                activation,
                                action_profile="PROTECTIVE_STOP_REDUCE_ONLY",
                                control_category=StopCategory.PROTECTION,
                                quantity=quantity,
                            ),
                            observed_at=facts.checked_at,
                        )
                    continue
                if desired not in working:
                    continue
                for older in working:
                    if older.execution_action_id == desired.execution_action_id:
                        continue
                    older_trigger = older.action_terms.get("trigger_price")
                    if not isinstance(older_trigger, str):
                        continue
                    tighter = (
                        Decimal(target_price) > Decimal(older_trigger)
                        if activation.direction is Direction.LONG
                        else Decimal(target_price) < Decimal(older_trigger)
                    )
                    if tighter:
                        self._ensure_cancel(
                            activation,
                            facts,
                            older,
                            target_endpoint="ALGO",
                            reason_code=f"{reason_prefix}_{desired_index}",
                        )
                continue
            if len(replacements) >= rule.max_adjustments:
                continue
            latest_replacement = max(
                (item.created_at for item in replacements),
                default=None,
            )
            if (
                latest_replacement is not None
                and facts.checked_at - latest_replacement
                < timedelta(seconds=rule.minimum_update_interval_seconds)
            ):
                continue
            predecessor = (
                max(
                    working,
                    key=lambda item: Decimal(
                        str(item.action_terms.get("trigger_price", "0"))
                    ),
                )
                if activation.direction is Direction.LONG
                else min(
                    working,
                    key=lambda item: Decimal(
                        str(item.action_terms.get("trigger_price", "0"))
                    ),
                )
            )
            predecessor_trigger = predecessor.action_terms.get("trigger_price")
            quantity = predecessor.action_terms.get("quantity")
            if not isinstance(predecessor_trigger, str) or not isinstance(
                quantity,
                str,
            ):
                continue
            tighter = (
                Decimal(target_price) > Decimal(predecessor_trigger)
                if activation.direction is Direction.LONG
                else Decimal(target_price) < Decimal(predecessor_trigger)
            )
            if not tighter:
                continue
            source = (
                f"{activation.activation_id}:{source_kind}:"
                f"{fill_ref}:{desired_index}"
            )
            check = facts.action_check(
                activation,
                action_profile="PROTECTIVE_STOP_REDUCE_ONLY",
                control_category=StopCategory.PROTECTION,
                quantity=quantity,
            )
            result = self._coordinator.create_direct_protection_replacement(
                activation_id=activation.activation_id,
                predecessor_action_id=predecessor.execution_action_id,
                target_trigger_price=target_price,
                step_index=desired_index,
                replacement_kind="DYNAMIC_TIGHTEN",
                plan_event_id=_stable_id(
                    self._environment_id,
                    "plan-event",
                    source,
                ),
                execution_action_id=_stable_id(
                    self._environment_id,
                    "execution-action",
                    source,
                ),
                action_check=check,
                observed_at=facts.checked_at,
                client_order_id=_stable_client_order_id(
                    self._environment_id,
                    source,
                ),
            )
            if result.execution_action is not None:
                if _venue_position_is_shared(facts):
                    self._ensure_cancel(
                        activation,
                        facts,
                        predecessor,
                        target_endpoint="ALGO",
                        reason_code=(
                            f"{reason_prefix}_{desired_index}_SHARED_POSITION"
                        ),
                    )
                else:
                    self._submit_ready(
                        result.execution_action,
                        check,
                        observed_at=facts.checked_at,
                    )

    def _replacement_waits_for_predecessor_cancel(
        self,
        activation: PlanActivation,
        facts: ProductRiskReductionFacts,
        replacement: ExecutionAction,
        protections: list[ExecutionAction] | tuple[ExecutionAction, ...],
    ) -> bool:
        context = replacement.action_terms.get("execution_context", {})
        replacement_context = (
            context.get("protection_replacement") if isinstance(context, dict) else None
        )
        if not isinstance(replacement_context, dict):
            return False
        replacement_kind = replacement_context.get("kind")
        predecessor_ref = (
            replacement_context.get("predecessor_action_ref")
            if isinstance(replacement_context, dict)
            else None
        )
        predecessor = next(
            (
                action
                for action in protections
                if action.execution_action_id == predecessor_ref
            ),
            None,
        )
        if predecessor is None:
            raise ValueError("DYNAMIC_PROTECTION_PREDECESSOR_UNKNOWN")
        predecessor_facts = self._coordinator.list_venue_facts_for_action(
            predecessor.execution_action_id
        )
        if terminal_order_status(predecessor_facts) is not None:
            return False
        replacement_trigger = replacement.action_terms.get("trigger_price")
        predecessor_trigger = predecessor.action_terms.get("trigger_price")
        aggregate_loosen = (
            replacement_kind == "ENTRY_AGGREGATE_REBALANCE"
            and isinstance(replacement_trigger, str)
            and isinstance(predecessor_trigger, str)
            and (
                Decimal(replacement_trigger) < Decimal(predecessor_trigger)
                if activation.direction is Direction.LONG
                else Decimal(replacement_trigger) > Decimal(predecessor_trigger)
            )
        )
        if not aggregate_loosen and not _venue_position_is_shared(facts):
            return False
        if (
            predecessor.state is ExecutionActionState.OPEN
            and predecessor.client_order_id is not None
            and order_is_working(predecessor_facts)
        ):
            self._ensure_cancel(
                activation,
                facts,
                predecessor,
                target_endpoint="ALGO",
                reason_code="DIRECT_PROTECTION_REPLACEMENT_SHARED_POSITION",
            )
        return True

    def _resume_ready_non_entry_actions(
        self,
        activation: PlanActivation,
        facts: ProductRiskReductionFacts,
        actions: tuple[ExecutionAction, ...],
    ) -> None:
        """Retry only local responsibilities proven never to have been called."""

        if activation.lifecycle is PlanLifecycle.USER_TAKEOVER:
            return
        order_actions_by_client_id = {
            action.client_order_id: action
            for action in actions
            if action.action_kind is not ExecutionActionKind.CANCEL
            and action.client_order_id is not None
        }
        for action in actions:
            if (
                action.state is not ExecutionActionState.READY
                or action.action_kind is ExecutionActionKind.ENTRY
            ):
                continue
            if (
                action.action_kind is ExecutionActionKind.PROTECTION
                and self._replacement_waits_for_predecessor_cancel(
                    activation,
                    facts,
                    action,
                    actions,
                )
            ):
                continue
            if action.action_kind is ExecutionActionKind.CANCEL:
                cancel_target = action.cancel_target or {}
                target_client_order_id = cancel_target.get("client_order_id")
                target = order_actions_by_client_id.get(target_client_order_id)
                if (
                    target is not None
                    and terminal_order_status(
                        self._coordinator.list_venue_facts_for_action(
                            target.execution_action_id
                        )
                    )
                    is not None
                ):
                    self._coordinator.reject_execution_action_before_submission(
                        action.execution_action_id,
                        reason_code="CANCEL_TARGET_ALREADY_TERMINAL",
                        observed_at=facts.checked_at,
                    )
                    continue
                check = facts.cancel_check(activation)
            else:
                quantity = action.action_terms.get("quantity")
                profile = action.action_terms.get("action_profile")
                if not isinstance(quantity, str) or not isinstance(profile, str):
                    continue
                if action.action_kind is ExecutionActionKind.EXIT:
                    if not _position_attribution_proven(
                        activation,
                        facts,
                        actions,
                        self._coordinator,
                    ):
                        if _execution_state_changed_after_facts(
                            facts,
                            actions,
                            self._coordinator,
                        ):
                            return
                        raise ValueError("POSITION_ATTRIBUTION_UNKNOWN")
                    if Decimal(quantity) != Decimal(facts.current_abs_position):
                        self._coordinator.reject_execution_action_before_submission(
                            action.execution_action_id,
                            reason_code="EXIT_POSITION_CHANGED_BEFORE_SUBMISSION",
                            observed_at=facts.checked_at,
                        )
                        continue
                check = facts.action_check(
                    activation,
                    action_profile=profile,
                    control_category=(
                        StopCategory.PROTECTION
                        if action.action_kind is ExecutionActionKind.PROTECTION
                        else StopCategory.RISK_REDUCTION_OR_ORDER_MANAGEMENT
                    ),
                    quantity=quantity,
                )
            self._submit_ready(action, check, observed_at=facts.checked_at)

    def _ensure_crossed_take_profit_reductions(
        self,
        activation: PlanActivation,
        facts: ProductRiskReductionFacts,
        actions: tuple[ExecutionAction, ...],
    ) -> None:
        """Market-reduce a TP quantity when Binance says its trigger already crossed."""

        position_fact = facts.position_fact
        remaining = Decimal(facts.current_abs_position)
        if (
            activation.lifecycle is not PlanLifecycle.RUNNING
            or remaining <= 0
            or position_fact is None
        ):
            return
        candidates = tuple(
            (action, rejection_fact)
            for action in actions
            if action.action_kind is ExecutionActionKind.TAKE_PROFIT
            and (
                rejection_fact := _crossed_take_profit_rejection_fact(
                    action,
                    self._coordinator.list_venue_facts_for_action(
                        action.execution_action_id
                    ),
                )
            )
            is not None
        )
        if not candidates:
            return
        if not _position_attribution_proven(
            activation,
            facts,
            actions,
            self._coordinator,
        ):
            if _execution_state_changed_after_facts(
                facts,
                actions,
                self._coordinator,
            ):
                return
            raise ValueError("POSITION_ATTRIBUTION_UNKNOWN")

        fallback_by_predecessor: dict[str, ExecutionAction] = {}
        for action in actions:
            if action.action_kind is not ExecutionActionKind.RISK_REDUCTION:
                continue
            context = action.action_terms.get("execution_context")
            predecessor_ref = (
                context.get("rejected_take_profit_action_ref")
                if isinstance(context, dict)
                else None
            )
            if isinstance(predecessor_ref, str):
                fallback_by_predecessor[predecessor_ref] = action

        self._coordinator.apply_venue_fact(
            position_fact,
            observed_at=position_fact.received_at,
        )
        for predecessor, rejection_fact in sorted(
            candidates,
            key=lambda item: (item[0].created_at, item[0].execution_action_id),
        ):
            existing = fallback_by_predecessor.get(predecessor.execution_action_id)
            if existing is not None:
                if existing.state is ExecutionActionState.READY:
                    check = facts.action_check(
                        activation,
                        action_profile="REDUCE_OR_CLOSE_MARKET",
                        control_category=(
                            StopCategory.RISK_REDUCTION_OR_ORDER_MANAGEMENT
                        ),
                        quantity=str(existing.action_terms["quantity"]),
                    )
                    self._submit_ready(
                        existing,
                        check,
                        observed_at=facts.checked_at,
                    )
                if existing.state in {
                    ExecutionActionState.READY,
                    ExecutionActionState.SUBMITTING,
                    ExecutionActionState.UNKNOWN,
                    ExecutionActionState.OPEN,
                }:
                    remaining = max(
                        remaining - Decimal(str(existing.action_terms["quantity"])),
                        Decimal(0),
                    )
                continue
            if remaining <= 0:
                return
            predecessor_quantity = Decimal(
                str(predecessor.action_terms.get("quantity", "0"))
            )
            if predecessor_quantity <= 0:
                raise ValueError("TAKE_PROFIT_SUCCESSOR_QUANTITY_INVALID")
            quantity = canonical_decimal(min(predecessor_quantity, remaining))
            reason_ref = (
                f"{activation.activation_id}:TAKE_PROFIT_TRIGGER_CROSSED:"
                f"{predecessor.execution_action_id}:{rejection_fact.venue_fact_id}"
            )
            check = facts.action_check(
                activation,
                action_profile="REDUCE_OR_CLOSE_MARKET",
                control_category=StopCategory.RISK_REDUCTION_OR_ORDER_MANAGEMENT,
                quantity=quantity,
            )
            result = self._coordinator.create_take_profit_market_reduction(
                activation_id=activation.activation_id,
                rejected_take_profit_action_id=predecessor.execution_action_id,
                rejection_fact_ref=rejection_fact.venue_fact_id,
                position_quantity=quantity,
                position_fact_ref=position_fact.venue_fact_id,
                reason_ref=reason_ref,
                plan_event_id=_stable_id(
                    self._environment_id,
                    "plan-event-take-profit-trigger-crossed",
                    reason_ref,
                ),
                execution_action_id=_stable_id(
                    self._environment_id,
                    "execution-action-take-profit-trigger-crossed",
                    reason_ref,
                ),
                action_check=check,
                observed_at=facts.checked_at,
                client_order_id=_stable_client_order_id(
                    self._environment_id,
                    reason_ref,
                ),
            )
            action = result.execution_action
            if action is None:
                raise RuntimeError("TAKE_PROFIT_SUCCESSOR_CAP_REJECTED")
            fallback_by_predecessor[predecessor.execution_action_id] = action
            self._submit_ready(action, check, observed_at=facts.checked_at)
            remaining -= Decimal(quantity)

    async def _try_close_activation(
        self,
        activation: PlanActivation,
        facts: ProductRiskReductionFacts,
    ) -> None:
        if Decimal(facts.current_abs_position) != 0:
            return
        if facts.open_order_client_ids or facts.open_algo_client_ids:
            return
        position_fact = facts.position_fact
        if position_fact is None:
            return
        actions = self._coordinator.list_execution_actions(activation.activation_id)
        if not actions:
            return
        if _has_pending_retryable_entry(
            activation,
            actions,
            self._coordinator,
            observed_at=facts.checked_at,
        ):
            # A bounded entry-policy rejection is an expected attempt result. Direct schedule
            # remains responsible for re-checking conditions and creating the
            # next stable attempt while the entry window is still open.
            return
        if not _position_attribution_proven(
            activation,
            facts,
            actions,
            self._coordinator,
        ):
            return
        self._coordinator.apply_venue_fact(
            position_fact,
            observed_at=position_fact.received_at,
        )
        action_has_fill = {
            action.execution_action_id: any(
                fact.kind is VenueFactKind.FILL
                for fact in self._coordinator.list_venue_facts_for_action(
                    action.execution_action_id
                )
            )
            for action in actions
        }
        entry_fill_seen = activation.has_entry_fill or any(
            action.action_kind is ExecutionActionKind.ENTRY
            and action_has_fill[action.execution_action_id]
            for action in actions
        )
        risk_reduction_fill_seen = self._risk_reduction_fill_seen(actions)
        if (
            activation.lifecycle is PlanLifecycle.RUNNING
            and entry_fill_seen
            and not risk_reduction_fill_seen
        ):
            # A zero position snapshot can briefly lag an entry fill at Binance.
            # Do not let that transient fact close the activation before its
            # protection task can persist and submit the first reduce-only order.
            return

        closure_fact_refs = {position_fact.venue_fact_id}
        user_takeover = activation.lifecycle is PlanLifecycle.USER_TAKEOVER
        for action in actions:
            if action.state in {
                ExecutionActionState.CLOSED,
                ExecutionActionState.NOT_SUBMITTED,
                ExecutionActionState.HANDED_OVER,
            }:
                continue
            if action.state is not ExecutionActionState.OPEN:
                return
            action_facts = self._coordinator.list_venue_facts_for_action(
                action.execution_action_id
            )
            fact_refs = tuple(fact.venue_fact_id for fact in action_facts)
            terminal_status = terminal_order_status(action_facts)
            if terminal_status is None:
                return
            exact_fills_complete = terminal_fills_complete(action, action_facts)
            takeover_fills_accounted = (
                user_takeover
                and not exact_fills_complete
                and terminal_fills_accounted_for_user_takeover(action, action_facts)
            )
            fills_complete = exact_fills_complete or takeover_fills_accounted
            fees_complete = _fills_have_commissions(action_facts)
            if not fills_complete or not fees_complete:
                return
            closure_fact_refs.update(fact_refs)
            self._coordinator.reconcile_execution_action(
                action.execution_action_id,
                closure_evidence={
                    "order_terminal": True,
                    "terminal_order_status": terminal_status,
                    "fills_complete": fills_complete,
                    "fees_complete": fees_complete,
                    "position_effect_known": True,
                    "position_fact_ref": position_fact.venue_fact_id,
                    "user_takeover_downsized_terminal_accounted": (
                        takeover_fills_accounted
                    ),
                },
                venue_fact_refs=fact_refs,
                observed_at=facts.checked_at,
            )

        refreshed = self._coordinator.list_execution_actions(activation.activation_id)
        if any(
            action.state
            not in {
                ExecutionActionState.CLOSED,
                ExecutionActionState.NOT_SUBMITTED,
                ExecutionActionState.HANDED_OVER,
            }
            for action in refreshed
        ):
            return
        takeover_scope = activation.takeover_scope or {}
        handover_command_ref = takeover_scope.get("command_ref")
        if user_takeover and not isinstance(handover_command_ref, str):
            raise ValueError("HANDOVER_COMMAND_REF_REQUIRED")
        self._coordinator.close_activation(
            activation_id=activation.activation_id,
            cutoff=facts.checked_at,
            position_zero=True,
            open_order_refs=(),
            # The current activation has already passed the exact fill,
            # account-position and owned-order attribution proof above.  An
            # account-level NEW_RISK stop may remain latched for separate
            # historical/external evidence, but must not strand this proven
            # activation's bounded reduction and closure.
            external_activity_conflict=False,
            user_takeover=user_takeover,
            handover_command_ref=(handover_command_ref if user_takeover else None),
            fact_refs=tuple(sorted(closure_fact_refs)),
            observed_at=facts.checked_at,
        )

    def _risk_reduction_fill_seen(
        self,
        actions: tuple[ExecutionAction, ...],
    ) -> bool:
        return any(
            action.action_kind
            in {
                ExecutionActionKind.PROTECTION,
                ExecutionActionKind.TAKE_PROFIT,
                ExecutionActionKind.RISK_REDUCTION,
                ExecutionActionKind.EXIT,
            }
            and any(
                fact.kind is VenueFactKind.FILL
                for fact in self._coordinator.list_venue_facts_for_action(
                    action.execution_action_id
                )
            )
            for action in actions
        )

    def _failed_protection_seen(
        self,
        actions: tuple[ExecutionAction, ...],
    ) -> bool:
        protections_by_fill: dict[str, list[ExecutionAction]] = {}
        for action in actions:
            if action.action_kind is not ExecutionActionKind.PROTECTION:
                continue
            context = action.action_terms.get("execution_context")
            if not isinstance(context, dict) or not isinstance(
                context.get("fill_fact_ref"),
                str,
            ):
                continue
            protections_by_fill.setdefault(
                str(context["fill_fact_ref"]),
                [],
            ).append(action)
        for protections in protections_by_fill.values():
            failed = False
            viable = False
            for action in protections:
                terminal = terminal_order_status(
                    self._coordinator.list_venue_facts_for_action(
                        action.execution_action_id
                    )
                )
                if action.state is ExecutionActionState.NOT_SUBMITTED or terminal in {
                    "CANCELLED",
                    "REJECTED",
                    "EXPIRED",
                }:
                    failed = True
                else:
                    viable = True
            if failed and not viable:
                return True
        return False

    @staticmethod
    def _unknown_protection_exit_due(
        actions: tuple[ExecutionAction, ...],
        *,
        observed_at: datetime,
    ) -> bool:
        for action in actions:
            if (
                action.action_kind is not ExecutionActionKind.PROTECTION
                or action.state is not ExecutionActionState.UNKNOWN
            ):
                continue
            context = action.action_terms.get("execution_context")
            if not isinstance(context, dict) or not isinstance(
                context.get("fill_fact_ref"),
                str,
            ):
                continue
            started_at = action.call_started_at
            if (
                started_at is None
                or started_at.utcoffset() is None
                or observed_at.utcoffset() is None
                or observed_at < started_at
            ):
                raise ValueError("PROTECTION_CALL_EVIDENCE_INVALID")
            if observed_at - started_at >= PROTECTION_UNKNOWN_EXIT_DELAY:
                # The original stop identity remains queryable.  A bounded
                # reduce-only exit avoids indefinite naked exposure; if the
                # stop appears late, neither order can reverse the position.
                return True
        return False

    def _submit_ready(
        self,
        action: ExecutionAction,
        check: ActionCheckInput,
        *,
        observed_at: datetime,
    ) -> None:
        if action.state is not ExecutionActionState.READY:
            return
        request = {
            "profile": action.action_terms["action_profile"],
        }
        if action.action_terms.get("quantity") is not None:
            request["quantity"] = action.action_terms["quantity"]
        if action.action_terms.get("trigger_price") is not None:
            request["trigger_price"] = action.action_terms["trigger_price"]
        self._coordinator.process_execution_action(
            action.execution_action_id,
            action_check=check,
            request_payload=request,
            observed_at=observed_at,
        )

    async def wait_idle(self) -> None:
        pending = tuple(task for task in self._tasks.values() if not task.done())
        if pending:
            await asyncio.gather(*pending)

    def close(self) -> None:
        for _deadline, handle in self._direct_time_exit_handles.values():
            handle.cancel()
        self._direct_time_exit_handles.clear()
        self._direct_time_exit_woken.clear()
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        self._tasks.clear()
        self._completed_task_keys.clear()
        self._last_fallback_sync.clear()
        self._last_funding_sync.clear()
        self._last_called_action_query.clear()


def _direct_time_exit_due(
    activation: PlanActivation,
    *,
    observed_at: datetime,
) -> bool:
    deadline = direct_time_exit_at(activation)
    if deadline is None:
        return False
    if observed_at.utcoffset() is None:
        raise ValueError("DIRECT_TIME_EXIT_INVALID")
    return observed_at >= deadline


def _fills_have_commissions(facts: tuple[VenueFact, ...]) -> bool:
    fills = tuple(
        fact
        for fact in collapse_synthetic_reconciliation_fills(facts)
        if fact.kind is VenueFactKind.FILL
    )
    if any(fact.payload.get("trade_id") is None for fact in fills):
        return False
    fill_trade_ids = {str(fact.payload.get("trade_id")) for fact in fills}
    commission_trade_ids = {
        str(fact.payload.get("trade_id"))
        for fact in facts
        if fact.kind is VenueFactKind.COMMISSION
        and fact.payload.get("trade_id") is not None
    }
    return fill_trade_ids.issubset(commission_trade_ids)


def _unknown_exit_successor_due(
    action: ExecutionAction,
    *,
    position_fact: VenueFact,
    current_abs_position: str,
    observed_at: datetime,
) -> bool:
    if (
        action.state
        not in {
            ExecutionActionState.SUBMITTING,
            ExecutionActionState.UNKNOWN,
            ExecutionActionState.OPEN,
        }
        or action.action_terms.get("action_profile") != "REDUCE_OR_CLOSE_MARKET"
        or action.action_terms.get("exit_responsibility_role")
        != ExitResponsibilityRole.PRIMARY_EXIT.value
    ):
        return False
    started_at = action.call_started_at
    if started_at is None:
        return False
    if started_at.utcoffset() is None or observed_at.utcoffset() is None:
        raise ValueError("EXIT_CALL_EVIDENCE_INVALID")
    if observed_at < started_at:
        # The responsibility sync reads its account snapshot before it may
        # create and call the primary exit later in the same pass.  That
        # already-read snapshot is valid, but predates the call and therefore
        # cannot authorize a successor.  Wait for the next fresh sync instead
        # of treating the normal intra-pass ordering as corrupt evidence.
        return False
    if observed_at - started_at < UNKNOWN_RISK_CONTROL_SUCCESSOR_DELAY:
        return False
    if (
        position_fact.kind is not VenueFactKind.POSITION_STATE
        or position_fact.source_class is not VenueFactSourceClass.VENUE_QUERY
    ):
        return False
    received_at = position_fact.received_at
    cutoff = position_fact.cutoff
    if (
        received_at.utcoffset() is None
        or cutoff.utcoffset() is None
        or not (started_at <= received_at <= observed_at)
        or not (started_at <= cutoff <= observed_at)
        or observed_at - received_at > UNKNOWN_RISK_CONTROL_SUCCESSOR_DELAY
    ):
        return False
    raw_position = position_fact.payload.get("position_quantity")
    if not isinstance(raw_position, str):
        return False
    try:
        fact_abs_position = abs(Decimal(raw_position))
        expected_abs_position = Decimal(current_abs_position)
    except (ArithmeticError, ValueError):
        return False
    return (
        fact_abs_position.is_finite()
        and expected_abs_position.is_finite()
        and fact_abs_position > 0
        and fact_abs_position == expected_abs_position
    )


def _is_exit_successor(action: ExecutionAction) -> bool:
    return (
        action.action_kind is ExecutionActionKind.EXIT
        and action.action_terms.get("exit_responsibility_role")
        == ExitResponsibilityRole.EXIT_SUCCESSOR.value
    )


def _is_post_successor_cleanup(action: ExecutionAction) -> bool:
    return (
        action.action_kind is ExecutionActionKind.EXIT
        and action.action_terms.get("exit_responsibility_role")
        == ExitResponsibilityRole.POST_SUCCESSOR_LATE_ENTRY_CLEANUP.value
    )


def _action_proven_never_called(action: ExecutionAction) -> bool:
    return (
        action.state
        in {
            ExecutionActionState.READY,
            ExecutionActionState.NOT_SUBMITTED,
        }
        and getattr(action, "call_started_at", None) is None
        and getattr(action, "request_digest", None) is None
    )


def _post_successor_late_entry_cleanup(
    successor: ExecutionAction,
    *,
    actions: tuple[ExecutionAction, ...],
    cleanup_actions: tuple[ExecutionAction, ...],
    current_abs_position: str,
    coordinator: ProductResponsibilityCoordinator,
) -> tuple[str, str] | None:
    """Claim post-successor entry fills once without reopening a successor chain."""

    if (
        cleanup_actions
        or not _exit_action_resolved(successor, coordinator)
        or successor.state is not ExecutionActionState.CLOSED
    ):
        return None
    baseline = getattr(successor, "created_at", None) or getattr(
        successor, "call_started_at", None
    )
    if not isinstance(baseline, datetime) or baseline.utcoffset() is None:
        return None
    fill_quantities: dict[str, Decimal] = {}
    for action in actions:
        if action.action_kind is not ExecutionActionKind.ENTRY:
            continue
        for fact in collapse_synthetic_reconciliation_fills(
            coordinator.list_venue_facts_for_action(action.execution_action_id)
        ):
            if (
                fact.kind is not VenueFactKind.FILL
                or fact.received_at <= baseline
                or fact.venue_fact_id in fill_quantities
            ):
                continue
            try:
                quantity = Decimal(str(fact.payload["last_quantity"]))
            except (InvalidOperation, KeyError, TypeError, ValueError):
                return None
            if not quantity.is_finite() or quantity <= 0:
                return None
            fill_quantities[fact.venue_fact_id] = quantity
    if not fill_quantities:
        return None
    try:
        current = Decimal(current_abs_position)
    except (InvalidOperation, ValueError):
        return None
    if not current.is_finite() or current <= 0:
        return None
    claim_ids = tuple(sorted(fill_quantities))
    quantity = min(current, sum(fill_quantities.values(), Decimal(0)))
    if quantity <= 0:
        return None
    return canonical_decimal(quantity), content_digest(claim_ids)


def _unknown_cancel_successor_due(
    action: ExecutionAction,
    *,
    target_facts: tuple[VenueFact, ...],
    observed_at: datetime,
) -> bool:
    if action.state not in {
        ExecutionActionState.SUBMITTING,
        ExecutionActionState.UNKNOWN,
        ExecutionActionState.OPEN,
    } or ":CANCEL_SUCCESSOR:" in str(getattr(action, "source_identity", "")):
        return False
    started_at = action.call_started_at
    if started_at is None:
        return False
    if (
        started_at.utcoffset() is None
        or observed_at.utcoffset() is None
        or observed_at < started_at
    ):
        raise ValueError("CANCEL_CALL_EVIDENCE_INVALID")
    if observed_at - started_at < UNKNOWN_RISK_CONTROL_SUCCESSOR_DELAY:
        return False
    return any(
        fact.kind is VenueFactKind.ORDER_STATE
        and fact.source_class is VenueFactSourceClass.VENUE_QUERY
        and fact.payload.get("status") == "WORKING"
        and fact.received_at >= started_at
        for fact in target_facts
    )


def _exit_action_resolved(
    action: ExecutionAction,
    coordinator: ProductResponsibilityCoordinator,
) -> bool:
    if action.state in {
        ExecutionActionState.NOT_SUBMITTED,
        ExecutionActionState.CLOSED,
    }:
        return True
    if action.state is not ExecutionActionState.OPEN:
        return False
    action_facts = coordinator.list_venue_facts_for_action(action.execution_action_id)
    return (
        terminal_order_status(action_facts) is not None
        and terminal_fills_complete(action, action_facts)
        and _fills_have_commissions(action_facts)
    )


def _crossed_take_profit_rejection_fact(
    action: ExecutionAction,
    facts: tuple[VenueFact, ...],
) -> VenueFact | None:
    if (
        venue_rejection_disposition(action, facts)
        is not VenueRejectionDisposition.TAKE_PROFIT_TRIGGER_ALREADY_CROSSED
    ):
        return None
    candidates = tuple(
        fact
        for fact in facts
        if fact.kind is VenueFactKind.ORDER_STATE
        and str(fact.payload.get("status", "")).upper() == "REJECTED"
        and (
            "-2021" in str(fact.payload.get("reason", "")).casefold()
            or "order would immediately trigger"
            in str(fact.payload.get("reason", "")).casefold()
        )
    )
    if not candidates:
        return None
    return max(candidates, key=lambda fact: (fact.cutoff, fact.venue_fact_id))


def _position_attribution_proven(
    activation: PlanActivation,
    facts: ProductRiskReductionFacts,
    actions: tuple[ExecutionAction, ...],
    coordinator: ProductResponsibilityCoordinator,
) -> bool:
    position_fact = facts.position_fact
    if not _position_fact_matches_activation(activation, facts):
        return False
    payload = getattr(position_fact, "payload", None)
    assert isinstance(payload, dict)
    try:
        signed_position = Decimal(
            str(
                payload.get(
                    "activation_position_quantity",
                    payload["position_quantity"],
                )
            )
        )
        account_position = Decimal(str(payload["position_quantity"]))
        attributed_account_position = Decimal(
            str(payload.get("attributed_account_position_quantity", account_position))
        )
        reported_abs = Decimal(facts.current_abs_position)
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return False
    if (
        account_position != attributed_account_position
        or abs(signed_position) != reported_abs
    ):
        return False

    direction_sign = (
        Decimal(1) if activation.direction is Direction.LONG else Decimal(-1)
    )
    alignment = getattr(activation, "position_alignment", None)
    expected_quantity = (
        Decimal(alignment.requested_reduction_quantity)
        if alignment is not None
        else Decimal(0)
    )
    seen_fill_ids: set[str] = set()
    ordinary_client_ids: set[str] = set()
    algo_client_ids: set[str] = set()
    reduction_kinds = {
        ExecutionActionKind.PROTECTION,
        ExecutionActionKind.TAKE_PROFIT,
        ExecutionActionKind.RISK_REDUCTION,
        ExecutionActionKind.EXIT,
    }
    for action in actions:
        if action.client_order_id is not None:
            if action.action_kind in {
                ExecutionActionKind.PROTECTION,
                ExecutionActionKind.TAKE_PROFIT,
            }:
                algo_client_ids.add(action.client_order_id)
            elif action.action_kind is not ExecutionActionKind.CANCEL:
                ordinary_client_ids.add(action.client_order_id)
        if action.action_kind not in {
            ExecutionActionKind.ENTRY,
            *reduction_kinds,
        }:
            continue
        for fact in collapse_synthetic_reconciliation_fills(
            coordinator.list_venue_facts_for_action(action.execution_action_id)
        ):
            if fact.kind is not VenueFactKind.FILL:
                continue
            if fact.venue_fact_id in seen_fill_ids:
                return False
            seen_fill_ids.add(fact.venue_fact_id)
            try:
                quantity = Decimal(str(fact.payload["last_quantity"]))
            except (InvalidOperation, KeyError, TypeError, ValueError):
                return False
            if quantity <= 0:
                return False
            expected_quantity += (
                quantity
                if action.action_kind is ExecutionActionKind.ENTRY
                else -quantity
            )
    expected_signed_position = direction_sign * expected_quantity
    if expected_quantity < 0 or signed_position != expected_signed_position:
        return False
    if not set(facts.open_order_client_ids).issubset(ordinary_client_ids):
        return False
    return set(facts.open_algo_client_ids).issubset(algo_client_ids)


def _execution_state_changed_after_facts(
    facts: ProductRiskReductionFacts,
    actions: tuple[ExecutionAction, ...],
    coordinator: ProductResponsibilityCoordinator,
) -> bool:
    """Return whether the immutable action stream superseded this account read."""

    attribution_cutoff = facts.attribution_cutoff or facts.checked_at
    if (
        not isinstance(attribution_cutoff, datetime)
        or attribution_cutoff.utcoffset() is None
    ):
        return False
    for action in actions:
        updated_at = getattr(action, "updated_at", None)
        if (
            isinstance(updated_at, datetime)
            and updated_at.utcoffset() is not None
            and updated_at > attribution_cutoff
        ):
            return True
        if any(
            (
                isinstance(fact.received_at, datetime)
                and fact.received_at.utcoffset() is not None
                and fact.received_at > attribution_cutoff
            )
            or (
                isinstance(fact.cutoff, datetime)
                and fact.cutoff.utcoffset() is not None
                and fact.cutoff > attribution_cutoff
            )
            for fact in coordinator.list_venue_facts_for_action(
                action.execution_action_id
            )
        ):
            return True
    return False


def _venue_position_is_shared(facts: ProductRiskReductionFacts) -> bool:
    fact = facts.position_fact
    payload = getattr(fact, "payload", None)
    if not isinstance(payload, dict):
        return False
    try:
        account_position = Decimal(
            str(
                payload.get(
                    "attributed_account_position_quantity",
                    payload["position_quantity"],
                )
            )
        )
        activation_position = Decimal(
            str(
                payload.get(
                    "activation_position_quantity",
                    payload["position_quantity"],
                )
            )
        )
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return False
    return account_position != activation_position


def _position_fact_matches_activation(
    activation: PlanActivation,
    facts: ProductRiskReductionFacts,
) -> bool:
    fact = facts.position_fact
    payload = getattr(fact, "payload", None)
    if fact is None or not isinstance(payload, dict):
        return False
    if (
        getattr(fact, "kind", None) is not VenueFactKind.POSITION_STATE
        or getattr(fact, "source_class", None) is not VenueFactSourceClass.VENUE_QUERY
        or getattr(fact, "environment_id", None) != activation.environment_id
        or getattr(fact, "venue_ref", None) != BINANCE_USDM_VENUE_REF
        or getattr(fact, "account_ref", None) != activation.account_ref
        or getattr(fact, "instrument_ref", None) != activation.instrument_ref
        or getattr(fact, "activation_ref", None) is not None
        or getattr(fact, "action_ref", None) is not None
    ):
        return False
    alignment = getattr(activation, "position_alignment", None)
    expected_position_side = (
        alignment.position_side if alignment is not None else "BOTH"
    )
    if payload.get("position_side", "BOTH") != expected_position_side:
        return False
    received_at = getattr(fact, "received_at", None)
    cutoff = getattr(fact, "cutoff", None)
    checked_at = facts.checked_at
    if not all(
        isinstance(value, datetime) and value.utcoffset() is not None
        for value in (received_at, cutoff, checked_at)
    ):
        return False
    if (
        cutoff > received_at
        or received_at > checked_at
        or checked_at - received_at > UNKNOWN_RISK_CONTROL_SUCCESSOR_DELAY
    ):
        return False
    try:
        signed_position = Decimal(
            str(
                payload.get(
                    "activation_position_quantity",
                    payload["position_quantity"],
                )
            )
        )
        account_position = Decimal(str(payload["position_quantity"]))
        attributed_account_position = Decimal(
            str(payload.get("attributed_account_position_quantity", account_position))
        )
        reported_abs = Decimal(facts.current_abs_position)
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return False
    return (
        signed_position.is_finite()
        and account_position.is_finite()
        and account_position == attributed_account_position
        and reported_abs.is_finite()
        and reported_abs >= 0
        and abs(signed_position) == reported_abs
    )


def _latest_terminal_order_fact(
    facts: tuple[VenueFact, ...],
) -> VenueFact | None:
    terminal_facts = tuple(
        fact for fact in facts if terminal_order_status((fact,)) is not None
    )
    if not terminal_facts:
        return None
    return max(
        terminal_facts,
        key=lambda fact: (
            fact.source_time or fact.cutoff,
            fact.cutoff,
            fact.received_at,
            fact.venue_fact_id,
        ),
    )


def _has_pending_retryable_entry(
    activation: PlanActivation,
    actions: tuple[ExecutionAction, ...],
    coordinator: ProductResponsibilityCoordinator,
    *,
    observed_at: datetime,
) -> bool:
    if (
        activation.decision_basis_ref != DIRECT_EXECUTION_REF
        or activation.entry_opportunity_consumed
    ):
        return False
    deadlines = activation.rule_state.get("deadlines")
    deadline_raw = (
        deadlines.get("entry_valid_until") if isinstance(deadlines, dict) else None
    )
    if not isinstance(deadline_raw, str):
        return False
    try:
        deadline = datetime.fromisoformat(deadline_raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if (
        deadline.utcoffset() is None
        or observed_at.utcoffset() is None
        or observed_at >= deadline
    ):
        return False
    latest_by_leg: dict[int, tuple[int, ExecutionAction]] = {}
    for action in actions:
        if action.action_kind is not ExecutionActionKind.ENTRY:
            continue
        context = action.action_terms.get("execution_context")
        schedule = context.get("order_schedule") if isinstance(context, dict) else None
        if not isinstance(schedule, dict):
            continue
        leg_index = schedule.get("leg_index")
        attempt_index = schedule.get("attempt_index", 0)
        if not isinstance(leg_index, int) or not isinstance(attempt_index, int):
            continue
        current = latest_by_leg.get(leg_index)
        if current is None or attempt_index > current[0]:
            latest_by_leg[leg_index] = (attempt_index, action)
    retryable_rejection_pending = any(
        venue_rejection_disposition(
            action,
            coordinator.list_venue_facts_for_action(action.execution_action_id),
        )
        in {
            VenueRejectionDisposition.RETRYABLE_POST_ONLY,
            VenueRejectionDisposition.RETRYABLE_PRICE_MATCH,
        }
        for _attempt_index, action in latest_by_leg.values()
    )
    if retryable_rejection_pending:
        return True

    snapshot = activation.order_schedule_snapshot
    reprice_rule = next(
        (
            rule
            for rule in snapshot.schedule_spec.dynamic_rules
            if isinstance(rule, RepriceEntryRule)
        ),
        None,
    ) if snapshot is not None else None
    if reprice_rule is None:
        return False
    for _attempt_index, entry in latest_by_leg.values():
        context = entry.action_terms.get("execution_context")
        schedule = context.get("order_schedule") if isinstance(context, dict) else None
        reprice_index = (
            schedule.get("reprice_index", 0)
            if isinstance(schedule, dict)
            else None
        )
        if (
            entry.state is not ExecutionActionState.CLOSED
            or entry.client_order_id is None
            or not isinstance(reprice_index, int)
            or reprice_index >= reprice_rule.max_adjustments
        ):
            continue
        entry_facts = coordinator.list_venue_facts_for_action(
            entry.execution_action_id
        )
        if (
            terminal_order_status(entry_facts) not in {"CANCELLED", "EXPIRED"}
            or not terminal_fills_complete(entry, entry_facts)
            or any(fact.kind is VenueFactKind.FILL for fact in entry_facts)
        ):
            continue
        matching_cancels = tuple(
            action
            for action in actions
            if (
                action.action_kind is ExecutionActionKind.CANCEL
                and action.state is ExecutionActionState.CLOSED
                and isinstance(action.cancel_target, dict)
                and action.cancel_target.get("client_order_id")
                == entry.client_order_id
                and ":DIRECT_ENTRY_REPRICE:" in str(
                    action.action_terms.get("causation_ref", "")
                )
            )
        )
        if len(matching_cancels) > 1:
            raise ValueError("ORDER_SCHEDULE_REPRICE_CANCEL_CONFLICT")
        if matching_cancels:
            return True
    return False


_has_pending_retryable_post_only_entry = _has_pending_retryable_entry


def _failure_reason_code(exc: BaseException) -> str:
    reason_code = getattr(exc, "reason_code", None)
    return reason_code if isinstance(reason_code, str) else str(exc)


def _stable_id(environment_id: str, kind: str, source_identity: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"urn:halpha:{environment_id}:{kind}:{source_identity}",
        )
    )


def _stable_client_order_id(environment_id: str, source_identity: str) -> str:
    return uuid5(
        NAMESPACE_URL,
        f"urn:halpha:{environment_id}:client-order:{source_identity}",
    ).hex
