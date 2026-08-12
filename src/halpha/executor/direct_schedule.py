"""Stateless runtime boundary for one persisted direct order schedule."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import (
    Decimal,
    InvalidOperation,
    ROUND_CEILING,
    ROUND_DOWN,
    localcontext,
)
from typing import Any, Literal, Protocol
from uuid import NAMESPACE_URL, uuid5

from halpha.capital.checks import effective_leverage
from halpha.capital.models import ActionCheckInput, AuthorityClass, EnvironmentKind
from halpha.domain_values import canonical_decimal, content_digest
from halpha.executor.product_entry import (
    ProductAccountFacts,
    ProductPreSubmitRejected,
)
from halpha.executor.responsibilities import ProductRiskReductionFacts
from halpha.planning.models import PlanActivation, PlanLifecycle, RunState
from halpha.planning.order_policies import (
    CancelOnShockRule,
    ConditionFacts,
    ConditionResult,
    ExpireRemainingRule,
    MarkPriceCondition,
    PriceMoveBpsCondition,
    RepriceEntryRule,
    RuntimeConditionState,
    SpreadBpsCondition,
    evaluate_condition_group,
)
from halpha.planning.order_schedule import (
    BINANCE_GTD_MIN_LEAD_SECONDS,
    EntryProgramKind,
    ScheduleSubmissionMode,
    VenueOrderType,
)
from halpha.planning.order_schedule_actions import (
    MaterializedOrderLeg,
    materialize_direct_schedule,
    materialize_direct_schedule_reprice,
    materialize_direct_schedule_retry,
)
from halpha.planning.registry import DIRECT_EXECUTION_REF, Direction
from halpha.venue_integration.facts import (
    build_venue_fact,
    order_is_working,
    terminal_fills_complete,
    terminal_order_status,
)
from halpha.venue_integration.models import (
    BINANCE_USDM_VENUE_REF,
    ExecutionAction,
    ExecutionActionKind,
    ExecutionActionState,
    VenueFact,
    VenueFactKind,
    VenueFactSourceClass,
)
from halpha.venue_integration.rejections import (
    VenueRejectionDisposition,
    venue_rejection_disposition,
)

from .coordinator import OrderScheduleCapRejected
from .direct_protection import direct_time_exit_at


class DirectScheduleCoordinator(Protocol):
    def get_activation_snapshot(self, activation_id: str) -> PlanActivation: ...

    def record_runtime_condition_state(
        self,
        *,
        activation_id: str,
        state_key: str,
        state: RuntimeConditionState,
    ) -> PlanActivation: ...

    def expire_empty_entry_window(
        self,
        *,
        activation_id: str,
        observed_at: datetime,
    ) -> tuple[PlanActivation, Any]: ...

    def expire_remaining_entry_opportunity(
        self,
        *,
        activation_id: str,
        source_cutoff: datetime,
        observed_at: datetime,
    ) -> tuple[PlanActivation, Any]: ...

    def invalidate_empty_entry_opportunity(
        self,
        *,
        activation_id: str,
        source_cutoff: datetime,
        evidence: dict[str, object],
        observed_at: datetime,
    ) -> tuple[PlanActivation, Any]: ...

    def list_execution_actions(
        self,
        activation_id: str,
    ) -> tuple[ExecutionAction, ...]: ...

    def list_venue_facts_for_action(
        self,
        execution_action_id: str,
    ) -> tuple[VenueFact, ...]: ...

    def consume_order_schedule_atomic(self, **kwargs: Any) -> tuple[Any, ...]: ...

    def consume_order_schedule_retry(self, **kwargs: Any) -> Any: ...

    def consume_order_schedule_reprice(self, **kwargs: Any) -> Any: ...

    def process_execution_action(
        self,
        execution_action_id: str,
        **kwargs: Any,
    ) -> Any: ...

    def reconcile_execution_action(
        self,
        execution_action_id: str,
        **kwargs: Any,
    ) -> ExecutionAction: ...

    def reconcile_retryable_entry_rejection(
        self,
        execution_action_id: str,
        *,
        observed_at: datetime,
    ) -> ExecutionAction: ...

    def reject_execution_action_before_submission(
        self,
        execution_action_id: str,
        **kwargs: Any,
    ) -> ExecutionAction: ...

    def record_direct_pre_submit_rejection(self, **kwargs: Any) -> Any: ...

    def create_cancel_for_action(self, **kwargs: Any) -> Any: ...

    def apply_venue_fact(
        self,
        fact: VenueFact,
        **kwargs: Any,
    ) -> ExecutionAction | None: ...


DirectPreSubmitFactProvider = Callable[
    [
        PlanActivation,
        MaterializedOrderLeg,
        frozenset[str],
        frozenset[str],
        str,
        str,
        str,
    ],
    Awaitable[ProductAccountFacts],
]
DirectConditionFactProvider = Callable[
    [PlanActivation, int, datetime, dict[int, str]],
    ConditionFacts,
]
RiskReductionFactProvider = Callable[
    [PlanActivation], Awaitable[ProductRiskReductionFacts]
]
FailureSink = Callable[[str, BaseException], None]


MAX_PRICE_MOVE_LATEST_AGE_NS = 3_000_000_000
MAX_MANAGEMENT_PRICE_MOVE_LATEST_AGE_NS = 10_000_000_000
FACT_RETRY_BASE_SECONDS = 5
FACT_RETRY_MAX_SECONDS = 60
RISK_FACT_REFRESH_SECONDS = 10
RUNTIME_CONDITION_STATE_KEY = "DIRECT_ENTRY"
ENTRY_POLICY_RETRY_MAX_DELAY_SECONDS = 30
ENTRY_POLICY_RETRY_MAX_ATTEMPTS = 5
DIRECT_PRICE_MOVE_MAX_SAMPLES = 4096
# Backwards-compatible names for existing callers and evidence.
POST_ONLY_RETRY_MAX_DELAY_SECONDS = ENTRY_POLICY_RETRY_MAX_DELAY_SECONDS
POST_ONLY_RETRY_MAX_ATTEMPTS = ENTRY_POLICY_RETRY_MAX_ATTEMPTS


@dataclass(frozen=True, slots=True)
class _FactRetryState:
    failure_count: int
    retry_at_monotonic: float


@dataclass(frozen=True, slots=True)
class _RiskFactCache:
    facts: ProductRiskReductionFacts
    refresh_at_monotonic: float


@dataclass(frozen=True, slots=True)
class DirectPriceMoveObservation:
    window_seconds: int
    start_source_time_ns: int
    start_value: str
    end_source_time_ns: int
    end_value: str
    move_bps: str


class DirectPriceMoveTracker:
    """Keep only a bounded in-memory mark window; restart begins UNKNOWN."""

    def __init__(self) -> None:
        self._marks: deque[tuple[int, Decimal]] = deque(
            maxlen=DIRECT_PRICE_MOVE_MAX_SAMPLES
        )

    def record_mark(self, update: object) -> None:
        try:
            timestamp = int(getattr(update, "ts_event"))
            value = Decimal(str(getattr(update, "value")))
        except (AttributeError, TypeError, ValueError):
            return
        if timestamp <= 0 or value <= 0:
            return
        if self._marks and timestamp <= self._marks[-1][0]:
            return
        self._marks.append((timestamp, value))
        cutoff = timestamp - 310_000_000_000
        while self._marks and self._marks[0][0] < cutoff:
            self._marks.popleft()

    def moves(
        self,
        windows: frozenset[int],
        *,
        cutoff_ns: int,
    ) -> dict[int, str]:
        return {
            window: observation.move_bps
            for window, observation in self.observations(
                windows,
                cutoff_ns=cutoff_ns,
            ).items()
        }

    def observations(
        self,
        windows: frozenset[int],
        *,
        cutoff_ns: int,
        max_latest_age_ns: int = MAX_PRICE_MOVE_LATEST_AGE_NS,
    ) -> dict[int, DirectPriceMoveObservation]:
        if not self._marks:
            return {}
        latest_ts, latest = self._marks[-1]
        latest_age = cutoff_ns - latest_ts
        if latest_age < 0 or latest_age > max_latest_age_ns:
            return {}
        results: dict[int, DirectPriceMoveObservation] = {}
        samples = tuple(self._marks)
        for window in windows:
            target = latest_ts - window * 1_000_000_000
            candidates = tuple(item for item in samples if item[0] <= target)
            if not candidates:
                continue
            start_ts, start = candidates[-1]
            relevant = tuple(item for item in samples if item[0] >= start_ts)
            maximum_gap = max(
                (right[0] - left[0] for left, right in zip(relevant, relevant[1:])),
                default=0,
            )
            allowed_gap = max(3, min(window, 15)) * 1_000_000_000
            if target - start_ts > allowed_gap or maximum_gap > allowed_gap:
                continue
            results[window] = DirectPriceMoveObservation(
                window_seconds=window,
                start_source_time_ns=start_ts,
                start_value=canonical_decimal(start),
                end_source_time_ns=latest_ts,
                end_value=canonical_decimal(latest),
                move_bps=canonical_decimal((latest - start) / start * Decimal(10_000)),
            )
        return results

    def latest_source_time_ns(self, *, cutoff_ns: int) -> int | None:
        if not self._marks:
            return None
        latest_ts = self._marks[-1][0]
        age = cutoff_ns - latest_ts
        if age < 0 or age > MAX_PRICE_MOVE_LATEST_AGE_NS:
            return None
        return latest_ts


def _condition_venue_facts(
    activation: PlanActivation,
    condition_facts: ConditionFacts,
    price_move_observations: dict[int, DirectPriceMoveObservation],
    *,
    environment_id: str,
) -> tuple[VenueFact, ...]:
    snapshot = activation.order_schedule_snapshot
    if snapshot is None:
        raise ValueError("DIRECT_CONDITION_PROVENANCE_REQUIRED")
    if snapshot.venue_ref != BINANCE_USDM_VENUE_REF:
        raise ValueError("DIRECT_CONDITION_VENUE_REF_MISMATCH")
    condition_items = snapshot.schedule_spec.entry_conditions.items
    quote_required = any(
        isinstance(item, SpreadBpsCondition) for item in condition_items
    )
    mark_required = any(
        isinstance(item, MarkPriceCondition) for item in condition_items
    )
    price_move_windows = frozenset(
        item.window_seconds
        for item in condition_items
        if isinstance(item, PriceMoveBpsCondition)
    )
    used_price_move_observations = {
        window: price_move_observations[window]
        for window in price_move_windows
        if window in condition_facts.price_move_bps_by_window
        and window in price_move_observations
    }
    if any(
        window in condition_facts.price_move_bps_by_window
        and window not in used_price_move_observations
        for window in price_move_windows
    ):
        raise ValueError("DIRECT_CONDITION_PROVENANCE_REQUIRED")
    quote_available = (
        quote_required
        and condition_facts.bid_price is not None
        and condition_facts.ask_price is not None
    )
    mark_available = (mark_required and condition_facts.mark_price is not None) or bool(
        used_price_move_observations
    )
    if not quote_available and not mark_available:
        return ()

    provenance = condition_facts.provenance
    if provenance is None:
        raise ValueError("DIRECT_CONDITION_PROVENANCE_REQUIRED")
    expected_source = (
        "BINANCE_DEMO_PUBLIC"
        if activation.environment_kind is EnvironmentKind.DEMO
        else "BINANCE_LIVE_PUBLIC"
    )
    if (
        activation.environment_id != environment_id
        or provenance.source != expected_source
    ):
        raise ValueError("DIRECT_CONDITION_SOURCE_MISMATCH")

    facts: list[VenueFact] = []
    if (
        quote_available
        and provenance.quote_source_time is not None
        and provenance.quote_received_at is not None
    ):
        payload = {
            "bid_price": condition_facts.bid_price,
            "ask_price": condition_facts.ask_price,
            "unit": "USDT",
            "source": provenance.source,
        }
        facts.append(
            _condition_venue_fact(
                activation,
                venue_ref=snapshot.venue_ref,
                kind=VenueFactKind.TOP_OF_BOOK,
                source=provenance.source,
                source_time=provenance.quote_source_time,
                received_at=provenance.quote_received_at,
                payload=payload,
                environment_id=environment_id,
            )
        )
    elif quote_available:
        raise ValueError("DIRECT_CONDITION_PROVENANCE_REQUIRED")
    if mark_available:
        if (
            condition_facts.mark_price is None
            or provenance.mark_source_time is None
            or provenance.mark_received_at is None
        ):
            raise ValueError("DIRECT_CONDITION_PROVENANCE_REQUIRED")
        derived_moves = [
            {
                "window_seconds": observation.window_seconds,
                "start_source_time": datetime.fromtimestamp(
                    observation.start_source_time_ns / 1_000_000_000,
                    tz=UTC,
                ).isoformat(),
                "start_value": observation.start_value,
                "end_source_time": datetime.fromtimestamp(
                    observation.end_source_time_ns / 1_000_000_000,
                    tz=UTC,
                ).isoformat(),
                "end_value": observation.end_value,
                "move_bps": observation.move_bps,
                "method": "BOUNDED_CONTIGUOUS_MARK_WINDOW",
            }
            for _window, observation in sorted(used_price_move_observations.items())
        ]
        payload = {
            "mark_price": condition_facts.mark_price,
            "unit": "USDT",
            "source": provenance.source,
            "derived_price_moves": derived_moves,
        }
        facts.append(
            _condition_venue_fact(
                activation,
                venue_ref=snapshot.venue_ref,
                kind=VenueFactKind.MARK_PRICE,
                source=provenance.source,
                source_time=provenance.mark_source_time,
                received_at=provenance.mark_received_at,
                payload=payload,
                environment_id=environment_id,
            )
        )
    return tuple(facts)


def _condition_venue_fact(
    activation: PlanActivation,
    *,
    venue_ref: str,
    kind: VenueFactKind,
    source: str,
    source_time: datetime,
    received_at: datetime,
    payload: dict[str, Any],
    environment_id: str,
) -> VenueFact:
    source_object_id = f"{source}:{activation.instrument_ref}:{kind.value}"
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
                    environment_id,
                    source_object_id,
                    source_sequence,
                )
            ),
        )
    )
    return build_venue_fact(
        venue_fact_id=venue_fact_id,
        environment_id=environment_id,
        venue_ref=venue_ref,
        account_ref=None,
        instrument_ref=activation.instrument_ref,
        kind=kind,
        source_class=VenueFactSourceClass.VENUE_STREAM,
        source_object_id=source_object_id,
        source_sequence=source_sequence,
        source_time=source_time,
        received_at=received_at,
        cutoff=received_at,
        payload=payload,
    )


def _runtime_condition_state(
    activation: PlanActivation,
    state_key: str,
) -> RuntimeConditionState | None:
    raw_judgements = activation.rule_state.get("condition_judgements")
    if not isinstance(raw_judgements, dict):
        return None
    raw_state = raw_judgements.get(state_key)
    if not isinstance(raw_state, dict):
        return None
    try:
        return RuntimeConditionState.model_validate(raw_state)
    except ValueError:
        return None


class DirectScheduleBoundary:
    """Reconstruct progress from snapshot, actions, and facts without a second FSM."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        coordinator: DirectScheduleCoordinator,
        pre_submit_fact_provider: DirectPreSubmitFactProvider,
        condition_fact_provider: DirectConditionFactProvider,
        risk_reduction_fact_provider: RiskReductionFactProvider,
        environment_id: str,
        environment_kind: EnvironmentKind,
        authority_class: AuthorityClass,
        account_ref: str,
        submission_enabled: Callable[[], bool] | None = None,
        current_time_provider: Callable[[], datetime] | None = None,
        monotonic_time_provider: Callable[[], float] | None = None,
        failure_sink: FailureSink | None = None,
    ) -> None:
        self._loop = loop
        self._coordinator = coordinator
        self._pre_submit_fact_provider = pre_submit_fact_provider
        self._condition_fact_provider = condition_fact_provider
        self._risk_reduction_fact_provider = risk_reduction_fact_provider
        self._environment_id = environment_id
        self._environment_kind = environment_kind
        self._authority_class = authority_class
        self._account_ref = account_ref
        self._submission_enabled = submission_enabled or (lambda: True)
        self._current_time_provider = current_time_provider or (
            lambda: datetime.now(UTC)
        )
        self._monotonic_time_provider = monotonic_time_provider or loop.time
        self._failure_sink = failure_sink
        self._tracker = DirectPriceMoveTracker()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._fact_retries: dict[str, _FactRetryState] = {}
        self._fact_retry_reasons: dict[str, str] = {}
        self._risk_fact_cache: dict[str, _RiskFactCache] = {}
        self._forced_risk_refreshes: set[str] = set()

    def resume(
        self,
        activation_id: str,
        *,
        force_risk_refresh: bool = False,
    ) -> None:
        if not self._submission_enabled():
            return
        if force_risk_refresh:
            self._risk_fact_cache.pop(activation_id, None)
            self._forced_risk_refreshes.add(activation_id)
        self._schedule(activation_id, self._advance(activation_id))

    def record_mark(self, activation_id: str, update: object) -> None:
        self._tracker.record_mark(update)
        if not self._submission_enabled():
            return
        self._schedule(activation_id, self._advance(activation_id))

    def _schedule(self, key: str, coroutine: Coroutine[Any, Any, None]) -> None:
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            coroutine.close()
            return
        task = self._loop.create_task(coroutine)
        self._tasks[key] = task
        task.add_done_callback(
            lambda completed, activation_id=key: self._report_failure(
                activation_id,
                completed,
            )
        )

    def _report_failure(
        self,
        activation_id: str,
        task: asyncio.Task[None],
    ) -> None:
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            if self._failure_sink is not None:
                try:
                    self._failure_sink(activation_id, exception)
                except Exception as sink_exception:
                    self._loop.call_exception_handler(
                        {
                            "message": "HALPHA_DIRECT_SCHEDULE_FAILURE_SINK_FAILED",
                            "exception_type": type(sink_exception).__name__,
                            "activation_id": activation_id,
                        }
                    )
            else:
                self._loop.call_exception_handler(
                    {
                        "message": "HALPHA_DIRECT_SCHEDULE_FAILED",
                        "exception": exception,
                        "task": task,
                    }
                )
        if activation_id in self._forced_risk_refreshes and self._submission_enabled():
            self._schedule(activation_id, self._advance(activation_id))

    def _record_condition_state(
        self,
        activation: PlanActivation,
        *,
        facts: ConditionFacts,
        evaluation: object,
        phase: Literal["INITIAL", "PRE_SUBMIT_RECHECK", "LATER_LEG_RECHECK"],
        observed_at: datetime,
        submission_ready: bool | None = None,
        blocking_reason: str | None = None,
    ) -> None:
        result = getattr(evaluation, "result", None)
        item_results = getattr(evaluation, "item_results", None)
        if not isinstance(result, ConditionResult) or not isinstance(
            item_results,
            tuple,
        ):
            raise ValueError("DIRECT_CONDITION_EVALUATION_INVALID")
        source_cutoff = (
            facts.provenance.source_cutoff
            if facts.provenance is not None
            else observed_at
        )
        self._coordinator.record_runtime_condition_state(
            activation_id=activation.activation_id,
            state_key=RUNTIME_CONDITION_STATE_KEY,
            state=RuntimeConditionState(
                result=result,
                item_results=item_results,
                phase=phase,
                source_cutoff=source_cutoff,
                evaluated_at=observed_at,
                facts=facts,
                submission_ready=submission_ready,
                blocking_reason=blocking_reason,
            ),
        )

    async def _advance(self, activation_id: str) -> None:
        if not self._submission_enabled():
            return
        force_risk_refresh = activation_id in self._forced_risk_refreshes
        self._forced_risk_refreshes.discard(activation_id)
        activation = self._coordinator.get_activation_snapshot(activation_id)
        if (
            activation.decision_basis_ref != DIRECT_EXECUTION_REF
            or activation.lifecycle is not PlanLifecycle.RUNNING
            or activation.run_state is not RunState.ACTIVE
        ):
            return
        entry_valid_until = _entry_valid_until(activation)
        legs = materialize_direct_schedule(
            activation,
            entry_valid_until=entry_valid_until,
        )
        snapshot = activation.order_schedule_snapshot
        if snapshot is None:
            raise ValueError("ORDER_SCHEDULE_SNAPSHOT_REQUIRED")
        if (
            snapshot.schedule_spec.submission_mode
            is ScheduleSubmissionMode.PREPROTECTED_PARALLEL
        ):
            # A persisted capability bit is not venue evidence. This runtime
            # path remains closed until a Demo-qualified pre-protection
            # implementation exists.
            raise ValueError("PREPROTECTED_PARALLEL_RUNTIME_NOT_QUALIFIED")
        actions = self._coordinator.list_execution_actions(activation_id)
        if any(
            action.action_kind is ExecutionActionKind.EXIT
            and action.state
            in {
                ExecutionActionState.READY,
                ExecutionActionState.SUBMITTING,
                ExecutionActionState.UNKNOWN,
                ExecutionActionState.OPEN,
            }
            for action in actions
        ):
            # While an exit responsibility is still unresolved, responsibility
            # sync owns its query, terminal facts and position reconciliation.
            # The entry scheduler must not re-enter account pre-submit checks
            # or form more risk during that hand-off.  A resolved reducer is
            # handled below because a late entry fill can still leave a
            # separately attributable residual position.
            return
        schedule_actions = _schedule_actions(actions, activation, legs)
        if activation.entry_opportunity_consumed and not schedule_actions:
            return
        pre_submit_account: ProductAccountFacts | None = None
        windows = _price_move_windows(activation)
        now = self._current_time_provider()
        if now.utcoffset() is None:
            raise ValueError("DIRECT_SCHEDULE_TIMEZONE_REQUIRED")
        now = now.astimezone(UTC)
        cutoff_ns = int(now.timestamp() * 1_000_000_000)
        if now >= entry_valid_until and not schedule_actions:
            self._coordinator.expire_empty_entry_window(
                activation_id=activation_id,
                observed_at=now,
            )
            return
        if not schedule_actions and now < _leg_release_at(activation, legs[0]):
            return
        if not schedule_actions:
            condition_facts = self._condition_facts(
                activation,
                windows,
                observed_at=now,
                cutoff_ns=cutoff_ns,
            )
            invalidation = _entry_invalidation_evaluation(
                activation,
                condition_facts,
            )
            if invalidation.result is ConditionResult.TRUE:
                source_cutoff = (
                    condition_facts.provenance.source_cutoff
                    if condition_facts.provenance is not None
                    else now
                )
                self._coordinator.invalidate_empty_entry_opportunity(
                    activation_id=activation_id,
                    source_cutoff=source_cutoff,
                    evidence=invalidation.evidence,
                    observed_at=now,
                )
                return
            if invalidation.result is ConditionResult.UNKNOWN:
                return
            evaluation = evaluate_condition_group(
                snapshot.schedule_spec.entry_conditions,
                condition_facts,
            )
            if evaluation.result is not ConditionResult.TRUE:
                self._record_condition_state(
                    activation,
                    facts=condition_facts,
                    evaluation=evaluation,
                    phase="INITIAL",
                    observed_at=now,
                )
                return
            current_condition_state = _runtime_condition_state(
                activation,
                RUNTIME_CONDITION_STATE_KEY,
            )
            if (
                current_condition_state is None
                or current_condition_state.submission_ready is not False
            ):
                self._record_condition_state(
                    activation,
                    facts=condition_facts,
                    evaluation=evaluation,
                    phase="PRE_SUBMIT_RECHECK",
                    observed_at=now,
                    submission_ready=False,
                    blocking_reason="DIRECT_ACCOUNT_FACT_CHECKING",
                )
            pre_submit_account, account_block_reason = (
                await self._pre_submit_account_for_leg(
                    activation,
                    legs[0],
                    actions,
                )
            )
            if pre_submit_account is None:
                self._record_condition_state(
                    activation,
                    facts=condition_facts,
                    evaluation=evaluation,
                    phase="PRE_SUBMIT_RECHECK",
                    observed_at=self._current_time_provider().astimezone(UTC),
                    submission_ready=False,
                    blocking_reason=(
                        account_block_reason or "DIRECT_ACCOUNT_FACT_UNAVAILABLE"
                    ),
                )
                return
            refreshed_now = self._current_time_provider()
            if (
                refreshed_now.utcoffset() is None
                or pre_submit_account.checked_at.utcoffset() is None
            ):
                raise ValueError("DIRECT_SCHEDULE_TIMEZONE_REQUIRED")
            now = max(
                refreshed_now.astimezone(UTC),
                pre_submit_account.checked_at.astimezone(UTC),
            )
            cutoff_ns = int(now.timestamp() * 1_000_000_000)
            price_move_observations = self._tracker.observations(
                windows,
                cutoff_ns=cutoff_ns,
            )
            condition_facts = self._condition_facts(
                activation,
                windows,
                observed_at=now,
                cutoff_ns=cutoff_ns,
                price_move_observations=price_move_observations,
            )
            evaluation = evaluate_condition_group(
                snapshot.schedule_spec.entry_conditions,
                condition_facts,
            )
            if evaluation.result is not ConditionResult.TRUE:
                self._record_condition_state(
                    activation,
                    facts=condition_facts,
                    evaluation=evaluation,
                    phase="PRE_SUBMIT_RECHECK",
                    observed_at=now,
                )
                return
            post_only_block = _post_only_submission_block_reason(
                legs[0],
                condition_facts,
                direction=activation.direction,
            )
            self._record_condition_state(
                activation,
                facts=condition_facts,
                evaluation=evaluation,
                phase="PRE_SUBMIT_RECHECK",
                observed_at=now,
                submission_ready=post_only_block is None,
                blocking_reason=post_only_block,
            )
            if post_only_block is not None:
                return
            try:
                checks = tuple(
                    _action_check_for_leg(
                        pre_submit_account,
                        activation,
                        item,
                        economic_action_prior_notional=(
                            item.economic_action_prior_notional
                        ),
                        include_economic_prior_margin=True,
                        environment_id=self._environment_id,
                        environment_kind=self._environment_kind,
                        authority_class=self._authority_class,
                        account_ref=self._account_ref,
                    )
                    for item in legs
                )
            except ProductPreSubmitRejected as exc:
                failed_at = self._current_monotonic()
                for item in legs:
                    self._coordinator.record_direct_pre_submit_rejection(
                        activation_id=activation_id,
                        execution_action_id=item.execution_action_id,
                        reason_code=exc.reason_code,
                        observed_at=now,
                    )
                    self._record_fact_failure(
                        f"PRE_SUBMIT:{item.execution_action_id}",
                        failed_at_monotonic=failed_at,
                        minimum_delay_seconds=exc.retry_after_seconds,
                    )
                return
            try:
                self._coordinator.consume_order_schedule_atomic(
                    activation_id=activation_id,
                    legs=legs,
                    action_checks=checks,
                    observed_at=now,
                    condition_source_cutoff=now,
                    condition_facts=_condition_venue_facts(
                        activation,
                        condition_facts,
                        price_move_observations,
                        environment_id=self._environment_id,
                    ),
                    condition_evidence={
                        "environment": {
                            "environment_id": self._environment_id,
                            "environment_kind": self._environment_kind.value,
                        },
                        "condition_group": (
                            snapshot.schedule_spec.entry_conditions.model_dump(
                                mode="json"
                            )
                        ),
                        "facts": condition_facts.model_dump(mode="json"),
                        "evaluation": evaluation.model_dump(mode="json"),
                        "price_move_source_time_ns": (
                            self._tracker.latest_source_time_ns(cutoff_ns=cutoff_ns)
                        ),
                    },
                )
            except OrderScheduleCapRejected as exc:
                failed_at = self._current_monotonic()
                for execution_action_id, reason_code in exc.rejections:
                    self._coordinator.record_direct_pre_submit_rejection(
                        activation_id=activation_id,
                        execution_action_id=execution_action_id,
                        reason_code=reason_code,
                        observed_at=now,
                    )
                    self._record_fact_failure(
                        f"PRE_SUBMIT:{execution_action_id}",
                        failed_at_monotonic=failed_at,
                    )
                return
            actions = self._coordinator.list_execution_actions(activation_id)
            schedule_actions = _schedule_actions(actions, activation, legs)

        risk_facts: ProductRiskReductionFacts | None = None
        retryable_rejection_closed = False
        for action in schedule_actions:
            if (
                action.state is ExecutionActionState.OPEN
                and _retryable_entry_rejection(self._coordinator, action) is not None
            ):
                self._coordinator.reconcile_retryable_entry_rejection(
                    action.execution_action_id,
                    observed_at=now,
                )
                retryable_rejection_closed = True
        if retryable_rejection_closed:
            actions = self._coordinator.list_execution_actions(activation_id)
            schedule_actions = _schedule_actions(actions, activation, legs)
        if _has_terminal_open_entry(self._coordinator, actions):
            risk_facts = await self._risk_facts_for_activation(
                activation,
                force_refresh=force_risk_refresh,
            )
            if risk_facts is None:
                return
            _close_proven_entry_actions(
                self._coordinator,
                actions,
                risk_facts=risk_facts,
                risk_summary=_risk_summary(self._coordinator, activation, actions),
                observed_at=risk_facts.checked_at,
            )
        actions = self._coordinator.list_execution_actions(activation_id)
        schedule_actions = _schedule_actions(actions, activation, legs)
        cycle_closed = False
        cycle_status_unknown = False
        risk_reduction_seen = activation.has_entry_fill and _has_risk_reduction_fill(
            self._coordinator,
            actions,
        )
        if risk_reduction_seen:
            if risk_facts is None:
                risk_facts = await self._risk_facts_for_activation(
                    activation,
                    force_refresh=force_risk_refresh,
                )
            cycle_status_unknown = (
                risk_facts is None or risk_facts.position_fact is None
            )
            cycle_closed = (
                not cycle_status_unknown
                and risk_facts is not None
                and Decimal(risk_facts.current_abs_position) == 0
            )
        protection_unproven = _has_unprotected_open_entry_fill(
            self._coordinator,
            actions,
        )
        management_condition_facts = self._condition_facts(
            activation,
            windows,
            observed_at=now,
            cutoff_ns=cutoff_ns,
            price_move_latest_age_ns=MAX_MANAGEMENT_PRICE_MOVE_LATEST_AGE_NS,
        )
        remaining_valid_until = _remaining_valid_until(
            legs,
            schedule_actions,
            default=entry_valid_until,
        )
        remaining_expiry_at = _entry_remaining_expiry_at(
            activation,
            schedule_actions,
        )
        management_expiry_at = _entry_management_expiry_at(
            activation,
            remaining_valid_until=remaining_valid_until,
            remaining_expiry_at=remaining_expiry_at,
        )
        management = _entry_management_decision(
            activation,
            actions,
            schedule_actions,
            condition_facts=management_condition_facts,
            entry_actions_with_fills=_entry_actions_with_fills(
                self._coordinator,
                schedule_actions,
            ),
            observed_at=now,
            entry_valid_until=remaining_valid_until,
            expire_anchor=_entry_expire_anchor(activation, schedule_actions),
            cycle_closed=cycle_closed,
            cycle_status_unknown=cycle_status_unknown,
            protection_unproven=protection_unproven,
        )
        if (
            not activation.entry_opportunity_consumed
            and not activation.has_entry_fill
            and management.expire_ready
        ):
            if management.reason_code == "DIRECT_ENTRY_REMAINING_EXPIRED":
                if (
                    management_expiry_at < entry_valid_until
                    and now >= management_expiry_at
                ):
                    self._coordinator.expire_remaining_entry_opportunity(
                        activation_id=activation_id,
                        source_cutoff=management_expiry_at,
                        observed_at=now,
                    )
                else:
                    self._coordinator.expire_empty_entry_window(
                        activation_id=activation_id,
                        observed_at=now,
                    )
            else:
                invalidation = _entry_invalidation_evaluation(
                    activation,
                    management_condition_facts,
                )
                if invalidation.result is ConditionResult.TRUE:
                    source_cutoff = (
                        management_condition_facts.provenance.source_cutoff
                        if management_condition_facts.provenance is not None
                        else now
                    )
                    self._coordinator.invalidate_empty_entry_opportunity(
                        activation_id=activation_id,
                        source_cutoff=source_cutoff,
                        evidence=invalidation.evidence,
                        observed_at=now,
                    )
        handled, risk_facts = await self._apply_entry_management(
            activation,
            schedule_actions,
            management,
            observed_at=now,
            risk_facts=risk_facts,
            force_risk_refresh=force_risk_refresh,
        )
        if handled:
            return
        if activation.entry_opportunity_consumed:
            return
        if any(
            action.state
            in {
                ExecutionActionState.SUBMITTING,
                ExecutionActionState.UNKNOWN,
                ExecutionActionState.OPEN,
            }
            for action in schedule_actions
        ):
            return
        latest_by_leg = _latest_schedule_actions_by_leg(schedule_actions)
        target: MaterializedOrderLeg | None = None
        retry_predecessor: ExecutionAction | None = None
        reprice_predecessor: ExecutionAction | None = None
        reprice_cancel: ExecutionAction | None = None
        for item in legs:
            action = latest_by_leg.get(item.leg.leg_index)
            if action is None:
                raise ValueError("ORDER_SCHEDULE_LOCAL_RESPONSIBILITY_CONFLICT")
            attempt_index = _schedule_attempt_index(action)
            if action.state is ExecutionActionState.READY:
                if now < _leg_release_at(activation, item):
                    return
                schedule = (
                    action.action_terms.get("execution_context", {}).get(
                        "order_schedule",
                        {},
                    )
                )
                target = _materialized_schedule_attempt(
                    activation,
                    item,
                    schedule,
                )
                break
            if action.state not in {
                ExecutionActionState.CLOSED,
                ExecutionActionState.NOT_SUBMITTED,
            }:
                return
            retry_disposition = _retryable_entry_rejection(
                self._coordinator,
                action,
            )
            if retry_disposition is not None:
                if (
                    _entry_policy_retry_count(schedule_actions)
                    >= ENTRY_POLICY_RETRY_MAX_ATTEMPTS
                ):
                    return
                if now < _entry_policy_retry_ready_at(action):
                    return
                replacement_price, reprice_index = _replacement_context(action)
                target = materialize_direct_schedule_retry(
                    activation,
                    item,
                    attempt_index=attempt_index + 1,
                    retry_reason=_retry_reason_for_disposition(retry_disposition),
                    replacement_price=replacement_price,
                    reprice_index=reprice_index,
                )
                retry_predecessor = action
                break
            cancel = _closed_reprice_cancel_for_entry(
                self._coordinator,
                actions,
                action,
            )
            rule = _reprice_rule(activation)
            if cancel is not None and rule is not None:
                reprice_count = sum(
                    1
                    for candidate in schedule_actions
                    if (
                        candidate.action_terms.get("execution_context", {})
                        .get("order_schedule", {})
                        .get("retry_reason")
                        == "ENTRY_REPRICE"
                    )
                )
                if reprice_count >= rule.max_adjustments:
                    return
                replacement_price = _aligned_reprice_target(
                    activation,
                    item,
                    action,
                    management_condition_facts,
                )
                if replacement_price is None:
                    return
                target = materialize_direct_schedule_reprice(
                    activation,
                    item,
                    attempt_index=attempt_index + 1,
                    replacement_price=replacement_price,
                    reprice_index=reprice_count + 1,
                )
                reprice_predecessor = action
                reprice_cancel = cancel
                break
        if target is None:
            completed_slice_expiry = _completed_time_slice_expiry_at(
                activation,
                actions,
                latest_by_leg,
            )
            if (
                not activation.entry_opportunity_consumed
                and completed_slice_expiry is not None
                and now >= completed_slice_expiry
            ):
                self._coordinator.expire_remaining_entry_opportunity(
                    activation_id=activation_id,
                    source_cutoff=completed_slice_expiry,
                    observed_at=now,
                )
            return
        if pre_submit_account is None:
            pre_submit_conditions = self._condition_facts(
                activation,
                windows,
                observed_at=now,
                cutoff_ns=cutoff_ns,
            )
            pre_submit_evaluation = evaluate_condition_group(
                snapshot.schedule_spec.entry_conditions,
                pre_submit_conditions,
            )
            if pre_submit_evaluation.result is not ConditionResult.TRUE:
                self._record_condition_state(
                    activation,
                    facts=pre_submit_conditions,
                    evaluation=pre_submit_evaluation,
                    phase="LATER_LEG_RECHECK",
                    observed_at=now,
                )
                return
            current_condition_state = _runtime_condition_state(
                activation,
                RUNTIME_CONDITION_STATE_KEY,
            )
            if (
                current_condition_state is None
                or current_condition_state.submission_ready is not False
            ):
                self._record_condition_state(
                    activation,
                    facts=pre_submit_conditions,
                    evaluation=pre_submit_evaluation,
                    phase="LATER_LEG_RECHECK",
                    observed_at=now,
                    submission_ready=False,
                    blocking_reason="DIRECT_ACCOUNT_FACT_CHECKING",
                )
            pre_submit_account, account_block_reason = (
                await self._pre_submit_account_for_leg(
                    activation,
                    target,
                    actions,
                )
            )
            if pre_submit_account is None:
                self._record_condition_state(
                    activation,
                    facts=pre_submit_conditions,
                    evaluation=pre_submit_evaluation,
                    phase="LATER_LEG_RECHECK",
                    observed_at=self._current_time_provider().astimezone(UTC),
                    submission_ready=False,
                    blocking_reason=(
                        account_block_reason or "DIRECT_ACCOUNT_FACT_UNAVAILABLE"
                    ),
                )
                return
        refreshed_now = self._current_time_provider()
        if (
            refreshed_now.utcoffset() is None
            or pre_submit_account.checked_at.utcoffset() is None
        ):
            raise ValueError("DIRECT_SCHEDULE_TIMEZONE_REQUIRED")
        refreshed_now = max(
            refreshed_now.astimezone(UTC),
            pre_submit_account.checked_at.astimezone(UTC),
        )
        refreshed_cutoff_ns = int(refreshed_now.timestamp() * 1_000_000_000)
        actions = self._coordinator.list_execution_actions(activation_id)
        schedule_actions = _schedule_actions(actions, activation, legs)
        refreshed_by_id = {
            action.execution_action_id: action for action in schedule_actions
        }
        refreshed_target = refreshed_by_id.get(target.execution_action_id)
        active_schedule_action = any(
            action.state
            in {
                ExecutionActionState.SUBMITTING,
                ExecutionActionState.UNKNOWN,
                ExecutionActionState.OPEN,
            }
            for action in schedule_actions
        )
        if active_schedule_action:
            return
        if refreshed_now < _leg_release_at(activation, target):
            return
        if retry_predecessor is None and reprice_predecessor is None:
            if (
                refreshed_target is None
                or refreshed_target.state is not ExecutionActionState.READY
            ):
                return
        elif retry_predecessor is not None:
            refreshed_latest = _latest_schedule_actions_by_leg(schedule_actions).get(
                target.leg.leg_index
            )
            if (
                refreshed_target is not None
                or refreshed_latest is None
                or refreshed_latest.execution_action_id
                != retry_predecessor.execution_action_id
                or refreshed_latest.state is not ExecutionActionState.CLOSED
                or _retryable_entry_rejection(
                    self._coordinator,
                    refreshed_latest,
                )
                is None
                or refreshed_now < _entry_policy_retry_ready_at(refreshed_latest)
            ):
                return
        else:
            refreshed_latest = _latest_schedule_actions_by_leg(schedule_actions).get(
                target.leg.leg_index
            )
            if (
                reprice_predecessor is None
                or reprice_cancel is None
                or refreshed_target is not None
                or refreshed_latest is None
                or refreshed_latest.execution_action_id
                != reprice_predecessor.execution_action_id
                or _closed_reprice_cancel_for_entry(
                    self._coordinator,
                    actions,
                    refreshed_latest,
                )
                is None
            ):
                return
        if not risk_reduction_seen and _has_risk_reduction_fill(
            self._coordinator,
            actions,
        ):
            # A new reduction fill arrived while account/condition facts were
            # awaited.  Its current position must be re-queried next cycle.
            return
        refreshed_management_facts = self._condition_facts(
            activation,
            windows,
            observed_at=refreshed_now,
            cutoff_ns=refreshed_cutoff_ns,
            price_move_latest_age_ns=MAX_MANAGEMENT_PRICE_MOVE_LATEST_AGE_NS,
        )
        refreshed_remaining_valid_until = _remaining_valid_until(
            legs,
            schedule_actions,
            default=entry_valid_until,
        )
        refreshed_remaining_expiry_at = _entry_remaining_expiry_at(
            activation,
            schedule_actions,
        )
        refreshed_management_expiry_at = _entry_management_expiry_at(
            activation,
            remaining_valid_until=refreshed_remaining_valid_until,
            remaining_expiry_at=refreshed_remaining_expiry_at,
        )
        refreshed_management = _entry_management_decision(
            activation,
            actions,
            schedule_actions,
            condition_facts=refreshed_management_facts,
            entry_actions_with_fills=_entry_actions_with_fills(
                self._coordinator,
                schedule_actions,
            ),
            observed_at=refreshed_now,
            entry_valid_until=refreshed_remaining_valid_until,
            expire_anchor=_entry_expire_anchor(activation, schedule_actions),
            cycle_closed=cycle_closed,
            cycle_status_unknown=cycle_status_unknown,
            protection_unproven=_has_unprotected_open_entry_fill(
                self._coordinator,
                actions,
            ),
        )
        if (
            not activation.entry_opportunity_consumed
            and not activation.has_entry_fill
            and refreshed_management.expire_ready
        ):
            if refreshed_management.reason_code == "DIRECT_ENTRY_REMAINING_EXPIRED":
                if (
                    refreshed_management_expiry_at < entry_valid_until
                    and refreshed_now >= refreshed_management_expiry_at
                ):
                    self._coordinator.expire_remaining_entry_opportunity(
                        activation_id=activation_id,
                        source_cutoff=refreshed_management_expiry_at,
                        observed_at=refreshed_now,
                    )
                else:
                    self._coordinator.expire_empty_entry_window(
                        activation_id=activation_id,
                        observed_at=refreshed_now,
                    )
            else:
                invalidation = _entry_invalidation_evaluation(
                    activation,
                    refreshed_management_facts,
                )
                if invalidation.result is ConditionResult.TRUE:
                    source_cutoff = (
                        refreshed_management_facts.provenance.source_cutoff
                        if refreshed_management_facts.provenance is not None
                        else refreshed_now
                    )
                    self._coordinator.invalidate_empty_entry_opportunity(
                        activation_id=activation_id,
                        source_cutoff=source_cutoff,
                        evidence=invalidation.evidence,
                        observed_at=refreshed_now,
                    )
        handled, risk_facts = await self._apply_entry_management(
            activation,
            schedule_actions,
            refreshed_management,
            observed_at=refreshed_now,
            risk_facts=risk_facts,
            force_risk_refresh=force_risk_refresh,
        )
        if handled:
            return
        current_price_move_observations = self._tracker.observations(
            windows,
            cutoff_ns=refreshed_cutoff_ns,
        )
        current_condition_facts = self._condition_facts(
            activation,
            windows,
            observed_at=refreshed_now,
            cutoff_ns=refreshed_cutoff_ns,
            price_move_observations=current_price_move_observations,
        )
        current_evaluation = evaluate_condition_group(
            snapshot.schedule_spec.entry_conditions,
            current_condition_facts,
        )
        if current_evaluation.result is not ConditionResult.TRUE:
            self._record_condition_state(
                activation,
                facts=current_condition_facts,
                evaluation=current_evaluation,
                phase="LATER_LEG_RECHECK",
                observed_at=refreshed_now,
            )
            # Materializing all local legs records the original entry
            # opportunity.  Each later external risk request still needs the
            # current direct-execution conditions to be true; stale or unknown
            # market facts keep the READY responsibility dormant.
            return
        if reprice_predecessor is not None:
            base = next(
                (
                    item
                    for item in legs
                    if item.leg.leg_index == target.leg.leg_index
                ),
                None,
            )
            if (
                base is None
                or _aligned_reprice_target(
                    activation,
                    base,
                    reprice_predecessor,
                    current_condition_facts,
                )
                != target.proposed_action.price
            ):
                # The book changed while account facts were refreshed. No
                # replacement has been persisted yet, so the next cycle can
                # derive a fresh stable attempt from the new facts.
                return
        post_only_block = _post_only_submission_block_reason(
            target,
            current_condition_facts,
            direction=activation.direction,
        )
        self._record_condition_state(
            activation,
            facts=current_condition_facts,
            evaluation=current_evaluation,
            phase="LATER_LEG_RECHECK",
            observed_at=refreshed_now,
            submission_ready=post_only_block is None,
            blocking_reason=post_only_block,
        )
        if post_only_block is not None:
            return
        policy = target.proposed_action.execution_context["venue_policy"]
        if _gtd_expiry_too_soon(policy, observed_at=refreshed_now):
            for action in schedule_actions:
                if action.state is ExecutionActionState.READY:
                    self._coordinator.reject_execution_action_before_submission(
                        action.execution_action_id,
                        reason_code="DIRECT_GTD_EXPIRY_TOO_SOON",
                        observed_at=refreshed_now,
                    )
            return
        submitted_quantity = (
            refreshed_target.action_terms.get("quantity")
            if refreshed_target is not None
            else None
        )
        if refreshed_target is not None and not isinstance(
            submitted_quantity,
            str,
        ):
            raise ValueError("ORDER_SCHEDULE_ACTION_CONFLICT")
        check = _action_check_for_leg(
            pre_submit_account,
            activation,
            target,
            economic_action_prior_notional=target.economic_action_prior_notional,
            quantity_override=(
                submitted_quantity if isinstance(submitted_quantity, str) else None
            ),
            environment_id=self._environment_id,
            environment_kind=self._environment_kind,
            authority_class=self._authority_class,
            account_ref=self._account_ref,
        )
        condition_evidence = {
            "environment": {
                "environment_id": self._environment_id,
                "environment_kind": self._environment_kind.value,
            },
            "condition_group": (
                snapshot.schedule_spec.entry_conditions.model_dump(mode="json")
            ),
            "facts": current_condition_facts.model_dump(mode="json"),
            "evaluation": current_evaluation.model_dump(mode="json"),
            "price_move_source_time_ns": (
                self._tracker.latest_source_time_ns(
                    cutoff_ns=refreshed_cutoff_ns
                )
            ),
        }
        condition_venue_facts = _condition_venue_facts(
            activation,
            current_condition_facts,
            current_price_move_observations,
            environment_id=self._environment_id,
        )
        if retry_predecessor is not None:
            try:
                retry_result = self._coordinator.consume_order_schedule_retry(
                    activation_id=activation_id,
                    retry_leg=target,
                    previous_action_id=retry_predecessor.execution_action_id,
                    action_check=check,
                    observed_at=refreshed_now,
                    condition_source_cutoff=refreshed_now,
                    condition_facts=condition_venue_facts,
                    condition_evidence=condition_evidence,
                )
            except OrderScheduleCapRejected as exc:
                failed_at = self._current_monotonic()
                for execution_action_id, reason_code in exc.rejections:
                    self._coordinator.record_direct_pre_submit_rejection(
                        activation_id=activation_id,
                        execution_action_id=execution_action_id,
                        reason_code=reason_code,
                        observed_at=refreshed_now,
                    )
                    self._record_fact_failure(
                        f"PRE_SUBMIT:{execution_action_id}",
                        failed_at_monotonic=failed_at,
                    )
                return
            refreshed_target = retry_result.execution_action
            submitted_quantity = refreshed_target.action_terms.get("quantity")
            if not isinstance(submitted_quantity, str):
                raise ValueError("ORDER_SCHEDULE_ACTION_CONFLICT")
        elif reprice_predecessor is not None and reprice_cancel is not None:
            try:
                reprice_result = self._coordinator.consume_order_schedule_reprice(
                    activation_id=activation_id,
                    replacement_leg=target,
                    previous_action_id=reprice_predecessor.execution_action_id,
                    cancel_action_id=reprice_cancel.execution_action_id,
                    action_check=check,
                    observed_at=refreshed_now,
                    condition_source_cutoff=refreshed_now,
                    condition_facts=condition_venue_facts,
                    condition_evidence=condition_evidence,
                )
            except OrderScheduleCapRejected as exc:
                failed_at = self._current_monotonic()
                for execution_action_id, reason_code in exc.rejections:
                    self._coordinator.record_direct_pre_submit_rejection(
                        activation_id=activation_id,
                        execution_action_id=execution_action_id,
                        reason_code=reason_code,
                        observed_at=refreshed_now,
                    )
                    self._record_fact_failure(
                        f"PRE_SUBMIT:{execution_action_id}",
                        failed_at_monotonic=failed_at,
                    )
                return
            refreshed_target = reprice_result.execution_action
            submitted_quantity = refreshed_target.action_terms.get("quantity")
            if not isinstance(submitted_quantity, str):
                raise ValueError("ORDER_SCHEDULE_ACTION_CONFLICT")
        if refreshed_target is None:
            raise ValueError("ORDER_SCHEDULE_ACTION_CONFLICT")
        self._coordinator.process_execution_action(
            target.execution_action_id,
            action_check=check,
            request_payload={
                "profile": target.proposed_action.action_profile,
                "quantity": submitted_quantity,
                "venue_policy": policy,
                "pre_submit_cutoff": pre_submit_account.checked_at.isoformat(),
            },
            # The account fact can legitimately predate the atomic creation of
            # the persisted READY action.  It remains the evidence cutoff in
            # the request payload/action check, while the state transition
            # itself must use a non-regressing runtime timestamp.
            observed_at=max(refreshed_now, refreshed_target.created_at),
        )

    async def _apply_entry_management(
        self,
        activation: PlanActivation,
        schedule_actions: tuple[ExecutionAction, ...],
        management: _EntryManagementDecision,
        *,
        observed_at: datetime,
        risk_facts: ProductRiskReductionFacts | None,
        force_risk_refresh: bool,
    ) -> tuple[bool, ProductRiskReductionFacts | None]:
        if management.reason_code is None:
            return False, risk_facts
        if management.expire_ready:
            for action in schedule_actions:
                if action.state is ExecutionActionState.READY:
                    self._coordinator.reject_execution_action_before_submission(
                        action.execution_action_id,
                        reason_code=management.reason_code,
                        observed_at=observed_at,
                    )
        if management.cancel_target is not None:
            target = management.cancel_target
            if risk_facts is None:
                risk_facts = await self._risk_facts_for_activation(
                    activation,
                    force_refresh=force_risk_refresh,
                )
            if risk_facts is None:
                return True, None
            reason_ref = (
                f"{activation.activation_id}:DIRECT_DYNAMIC:"
                f"{management.reason_code}:{target.execution_action_id}:"
                f"v{target.state_version}"
            )
            check = risk_facts.cancel_check(activation)
            result = self._coordinator.create_cancel_for_action(
                activation_id=activation.activation_id,
                target_action_id=target.execution_action_id,
                target_endpoint="ORDINARY",
                plan_event_id=_management_uuid(
                    self._environment_id,
                    "plan-event",
                    reason_ref,
                ),
                execution_action_id=_management_uuid(
                    self._environment_id,
                    "execution-action",
                    reason_ref,
                ),
                action_check=check,
                reason_ref=reason_ref,
                observed_at=risk_facts.checked_at,
                client_order_id=None,
            )
            cancel = result.execution_action
            if cancel is not None and cancel.state is ExecutionActionState.READY:
                self._coordinator.process_execution_action(
                    cancel.execution_action_id,
                    action_check=check,
                    request_payload={"profile": "CANCEL_ORDER"},
                    observed_at=risk_facts.checked_at,
                )
        return True, risk_facts

    async def _pre_submit_account_for_leg(
        self,
        activation: PlanActivation,
        leg: MaterializedOrderLeg,
        actions: tuple[ExecutionAction, ...],
    ) -> tuple[ProductAccountFacts | None, str | None]:
        retry_key = f"PRE_SUBMIT:{leg.execution_action_id}"
        if not self._fact_retry_ready(
            retry_key,
            observed_at_monotonic=self._current_monotonic(),
        ):
            return (
                None,
                self._fact_retry_reasons.get(
                    retry_key,
                    "DIRECT_ACCOUNT_FACT_RETRY_BACKOFF",
                ),
            )
        summary = _risk_summary(
            self._coordinator,
            activation,
            actions,
        )
        try:
            facts = await self._pre_submit_fact_provider(
                activation,
                leg,
                summary.ordinary_client_ids,
                summary.algo_client_ids,
                summary.expected_signed_position,
                summary.outstanding_entry_quantity,
                summary.outstanding_entry_notional,
            )
        except ProductPreSubmitRejected as exc:
            observed_at = self._current_time_provider()
            if observed_at.utcoffset() is None:
                raise ValueError("DIRECT_SCHEDULE_TIMEZONE_REQUIRED") from exc
            self._coordinator.record_direct_pre_submit_rejection(
                activation_id=activation.activation_id,
                execution_action_id=leg.execution_action_id,
                reason_code=exc.reason_code,
                observed_at=observed_at.astimezone(UTC),
            )
            self._record_fact_failure(
                retry_key,
                failed_at_monotonic=self._current_monotonic(),
                minimum_delay_seconds=exc.retry_after_seconds,
            )
            self._fact_retry_reasons[retry_key] = exc.reason_code
            return None, exc.reason_code
        except Exception:
            self._record_fact_failure(
                retry_key,
                failed_at_monotonic=self._current_monotonic(),
            )
            raise
        self._fact_retries.pop(retry_key, None)
        self._fact_retry_reasons.pop(retry_key, None)
        return facts, None

    async def _risk_facts_for_activation(
        self,
        activation: PlanActivation,
        *,
        force_refresh: bool,
    ) -> ProductRiskReductionFacts | None:
        activation_id = activation.activation_id
        observed_at_monotonic = self._current_monotonic()
        cached = self._risk_fact_cache.get(activation_id)
        if (
            not force_refresh
            and cached is not None
            and observed_at_monotonic < cached.refresh_at_monotonic
        ):
            return cached.facts
        retry_key = f"RISK_REDUCTION:{activation_id}"
        if not self._fact_retry_ready(
            retry_key,
            observed_at_monotonic=observed_at_monotonic,
            force=force_refresh,
        ):
            return None
        if force_refresh:
            self._risk_fact_cache.pop(activation_id, None)
        try:
            facts = await self._risk_reduction_fact_provider(activation)
            if facts.checked_at.utcoffset() is None:
                raise ValueError("DIRECT_SCHEDULE_TIMEZONE_REQUIRED")
            if isinstance(facts.position_fact, VenueFact):
                self._coordinator.apply_venue_fact(
                    facts.position_fact,
                    observed_at=facts.checked_at,
                )
        except ProductPreSubmitRejected as exc:
            self._record_fact_failure(
                retry_key,
                failed_at_monotonic=self._current_monotonic(),
                minimum_delay_seconds=exc.retry_after_seconds,
            )
            if exc.reason_code == "ACCOUNT_FACT_SUPERSEDED":
                # A stream fill changed the local attribution ledger while the
                # signed account reads were in flight.  The mixed-time result
                # is intentionally discarded; this is a transient consistency
                # miss, not a scheduler failure and never authorizes a write.
                return None
            raise
        except Exception:
            self._record_fact_failure(
                retry_key,
                failed_at_monotonic=self._current_monotonic(),
            )
            raise
        self._fact_retries.pop(retry_key, None)
        self._risk_fact_cache[activation_id] = _RiskFactCache(
            facts=facts,
            refresh_at_monotonic=(
                self._current_monotonic() + RISK_FACT_REFRESH_SECONDS
            ),
        )
        return facts

    def _fact_retry_ready(
        self,
        key: str,
        *,
        observed_at_monotonic: float,
        force: bool = False,
    ) -> bool:
        if force:
            return True
        state = self._fact_retries.get(key)
        return state is None or observed_at_monotonic >= state.retry_at_monotonic

    def _record_fact_failure(
        self,
        key: str,
        *,
        failed_at_monotonic: float,
        minimum_delay_seconds: float | None = None,
    ) -> None:
        previous = self._fact_retries.get(key)
        failure_count = 1 if previous is None else previous.failure_count + 1
        exponent = min(failure_count - 1, 10)
        delay_seconds = min(
            FACT_RETRY_BASE_SECONDS * (2**exponent),
            FACT_RETRY_MAX_SECONDS,
        )
        if minimum_delay_seconds is not None:
            delay_seconds = max(delay_seconds, minimum_delay_seconds)
        self._fact_retries[key] = _FactRetryState(
            failure_count=failure_count,
            retry_at_monotonic=failed_at_monotonic + delay_seconds,
        )

    def _current_utc(self) -> datetime:
        observed_at = self._current_time_provider()
        if observed_at.utcoffset() is None:
            raise ValueError("DIRECT_SCHEDULE_TIMEZONE_REQUIRED")
        return observed_at.astimezone(UTC)

    def _current_monotonic(self) -> float:
        return float(self._monotonic_time_provider())

    def _condition_facts(
        self,
        activation: PlanActivation,
        windows: frozenset[int],
        *,
        observed_at: datetime,
        cutoff_ns: int,
        price_move_observations: (dict[int, DirectPriceMoveObservation] | None) = None,
        price_move_latest_age_ns: int = MAX_PRICE_MOVE_LATEST_AGE_NS,
    ) -> ConditionFacts:
        observations = (
            self._tracker.observations(
                windows,
                cutoff_ns=cutoff_ns,
                max_latest_age_ns=price_move_latest_age_ns,
            )
            if price_move_observations is None
            else price_move_observations
        )
        facts = self._condition_fact_provider(
            activation,
            cutoff_ns,
            observed_at,
            {
                window: observation.move_bps
                for window, observation in observations.items()
            },
        )
        tracked_moves = {
            window: observation.move_bps for window, observation in observations.items()
        }
        if facts.price_move_bps_by_window == tracked_moves:
            return facts
        return facts.model_copy(update={"price_move_bps_by_window": tracked_moves})

    async def wait_idle(self) -> None:
        pending = tuple(task for task in self._tasks.values() if not task.done())
        if pending:
            await asyncio.gather(*pending)

    def close(self) -> None:
        self._forced_risk_refreshes.clear()
        self._fact_retries.clear()
        self._fact_retry_reasons.clear()
        self._risk_fact_cache.clear()
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        self._tasks.clear()


@dataclass(frozen=True, slots=True)
class _RiskSummary:
    expected_signed_position: str
    outstanding_entry_quantity: str
    outstanding_entry_notional: str
    ordinary_client_ids: frozenset[str]
    algo_client_ids: frozenset[str]


def _entry_valid_until(activation: PlanActivation) -> datetime:
    deadlines = activation.rule_state.get("deadlines")
    value = deadlines.get("entry_valid_until") if isinstance(deadlines, dict) else None
    if not isinstance(value, str):
        raise ValueError("ENTRY_DEADLINE_INVALID")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("ENTRY_DEADLINE_INVALID") from None
    if parsed.utcoffset() is None:
        raise ValueError("ENTRY_DEADLINE_INVALID")
    return parsed


def _leg_release_at(
    activation: PlanActivation,
    item: MaterializedOrderLeg,
) -> datetime:
    if activation.created_at.utcoffset() is None:
        raise ValueError("DIRECT_SCHEDULE_TIMEZONE_REQUIRED")
    return activation.created_at.astimezone(UTC) + timedelta(
        seconds=item.leg.release_after_seconds
    )


def _remaining_valid_until(
    legs: tuple[MaterializedOrderLeg, ...],
    schedule_actions: tuple[ExecutionAction, ...],
    *,
    default: datetime,
) -> datetime:
    """Use the earliest immutable venue/plan deadline of any unfinished leg."""

    states = {action.execution_action_id: action.state for action in schedule_actions}
    deadlines = tuple(
        item.proposed_action.valid_until
        for item in legs
        if states.get(item.execution_action_id)
        not in {ExecutionActionState.CLOSED, ExecutionActionState.NOT_SUBMITTED}
        and item.proposed_action.valid_until is not None
    )
    return min((default, *deadlines))


def _price_move_windows(activation: PlanActivation) -> frozenset[int]:
    snapshot = activation.order_schedule_snapshot
    if snapshot is None:
        return frozenset()
    condition_windows = {
        item.window_seconds
        for item in snapshot.schedule_spec.entry_conditions.items
        if isinstance(item, PriceMoveBpsCondition)
    }
    dynamic_windows = {
        item.window_seconds
        for item in snapshot.schedule_spec.dynamic_rules
        if isinstance(item, CancelOnShockRule) and item.window_seconds is not None
    }
    return frozenset(condition_windows | dynamic_windows)


@dataclass(frozen=True, slots=True)
class _EntryManagementDecision:
    reason_code: str | None = None
    expire_ready: bool = False
    cancel_target: ExecutionAction | None = None


@dataclass(frozen=True, slots=True)
class _EntryInvalidationEvaluation:
    result: ConditionResult
    reason_code: str | None = None
    evidence: dict[str, object] = field(default_factory=dict)


def _entry_invalidation_evaluation(
    activation: PlanActivation,
    facts: ConditionFacts,
) -> _EntryInvalidationEvaluation:
    snapshot = activation.order_schedule_snapshot
    if snapshot is None:
        raise ValueError("ORDER_SCHEDULE_SNAPSHOT_REQUIRED")
    rule = next(
        (
            item
            for item in snapshot.schedule_spec.dynamic_rules
            if isinstance(item, CancelOnShockRule)
        ),
        None,
    )
    if rule is None:
        return _EntryInvalidationEvaluation(ConditionResult.FALSE)

    checks: list[tuple[str, ConditionResult, dict[str, object]]] = []
    if rule.invalidation_price is not None:
        threshold = Decimal(rule.invalidation_price)
        if facts.mark_price is None:
            fixed_result = ConditionResult.UNKNOWN
            observed_mark: str | None = None
        else:
            mark = Decimal(facts.mark_price)
            invalidated = (
                mark <= threshold
                if activation.direction is Direction.LONG
                else mark >= threshold
            )
            fixed_result = (
                ConditionResult.TRUE if invalidated else ConditionResult.FALSE
            )
            observed_mark = facts.mark_price
        checks.append(
            (
                "INVALIDATION_PRICE",
                fixed_result,
                {
                    "kind": "INVALIDATION_PRICE",
                    "direction": activation.direction.value,
                    "configured_price": rule.invalidation_price,
                    "observed_mark_price": observed_mark,
                    "result": fixed_result.value,
                },
            )
        )
    if rule.opportunity_missed_price is not None:
        threshold = Decimal(rule.opportunity_missed_price)
        if facts.mark_price is None:
            missed_result = ConditionResult.UNKNOWN
            observed_mark = None
        else:
            mark = Decimal(facts.mark_price)
            missed = (
                mark >= threshold
                if activation.direction is Direction.LONG
                else mark <= threshold
            )
            missed_result = ConditionResult.TRUE if missed else ConditionResult.FALSE
            observed_mark = facts.mark_price
        checks.append(
            (
                "OPPORTUNITY_MISSED_PRICE",
                missed_result,
                {
                    "kind": "OPPORTUNITY_MISSED_PRICE",
                    "direction": activation.direction.value,
                    "configured_price": rule.opportunity_missed_price,
                    "observed_mark_price": observed_mark,
                    "result": missed_result.value,
                },
            )
        )
    if rule.window_seconds is not None and rule.adverse_move_bps is not None:
        move_value = facts.price_move_bps_by_window.get(rule.window_seconds)
        if move_value is None:
            shock_result = ConditionResult.UNKNOWN
        else:
            move = Decimal(move_value)
            adverse = (
                move <= -Decimal(rule.adverse_move_bps)
                if activation.direction is Direction.LONG
                else move >= Decimal(rule.adverse_move_bps)
            )
            shock_result = ConditionResult.TRUE if adverse else ConditionResult.FALSE
        checks.append(
            (
                "ADVERSE_MOVE",
                shock_result,
                {
                    "kind": "ADVERSE_MOVE",
                    "direction": activation.direction.value,
                    "window_seconds": rule.window_seconds,
                    "configured_adverse_move_bps": rule.adverse_move_bps,
                    "observed_move_bps": move_value,
                    "result": shock_result.value,
                },
            )
        )

    triggered = next(
        (
            (kind, evidence)
            for kind, result, evidence in checks
            if result is ConditionResult.TRUE
        ),
        None,
    )
    all_evidence = {
        "rule": rule.model_dump(mode="json", exclude_none=True),
        "checks": [evidence for _kind, _result, evidence in checks],
        "fact_source": (
            facts.provenance.model_dump(mode="json")
            if facts.provenance is not None
            else None
        ),
    }
    if triggered is not None:
        kind, _evidence = triggered
        return _EntryInvalidationEvaluation(
            ConditionResult.TRUE,
            (
                "DIRECT_ENTRY_INVALIDATION_PRICE"
                if kind == "INVALIDATION_PRICE"
                else (
                    "DIRECT_ENTRY_OPPORTUNITY_MISSED_PRICE"
                    if kind == "OPPORTUNITY_MISSED_PRICE"
                    else "DIRECT_ENTRY_SHOCK"
                )
            ),
            all_evidence,
        )
    if any(result is ConditionResult.UNKNOWN for _kind, result, _evidence in checks):
        return _EntryInvalidationEvaluation(
            ConditionResult.UNKNOWN,
            (
                "DIRECT_ENTRY_SHOCK_STATUS_UNKNOWN"
                if (
                    rule.invalidation_price is None
                    and rule.opportunity_missed_price is None
                )
                else "DIRECT_ENTRY_INVALIDATION_STATUS_UNKNOWN"
            ),
            all_evidence,
        )
    return _EntryInvalidationEvaluation(
        ConditionResult.FALSE,
        evidence=all_evidence,
    )


def _entry_cancel_target(
    snapshot: object,
    active: ExecutionAction | None,
    *,
    cancel_already_recorded: bool,
) -> ExecutionAction | None:
    schedule = getattr(snapshot, "schedule_spec", None)
    venue_policy = getattr(schedule, "venue_policy", None)
    if (
        getattr(venue_policy, "order_type", None) is not VenueOrderType.LIMIT
        or active is None
        or active.state is not ExecutionActionState.OPEN
        or cancel_already_recorded
    ):
        return None
    return active


def _entry_actions_with_fills(
    coordinator: DirectScheduleCoordinator,
    schedule_actions: tuple[ExecutionAction, ...],
) -> frozenset[str]:
    return frozenset(
        action.execution_action_id
        for action in schedule_actions
        if any(
            fact.kind is VenueFactKind.FILL
            for fact in coordinator.list_venue_facts_for_action(
                action.execution_action_id
            )
        )
    )


def _reprice_rule(activation: PlanActivation) -> RepriceEntryRule | None:
    snapshot = activation.order_schedule_snapshot
    if snapshot is None:
        raise ValueError("ORDER_SCHEDULE_SNAPSHOT_REQUIRED")
    return next(
        (
            rule
            for rule in snapshot.schedule_spec.dynamic_rules
            if isinstance(rule, RepriceEntryRule)
        ),
        None,
    )


def _aligned_reprice_target(
    activation: PlanActivation,
    base: MaterializedOrderLeg,
    current_action: ExecutionAction,
    facts: ConditionFacts,
) -> str | None:
    rule = _reprice_rule(activation)
    snapshot = activation.order_schedule_snapshot
    if rule is None or snapshot is None:
        return None
    book_raw = (
        facts.bid_price
        if activation.direction is Direction.LONG
        else facts.ask_price
    )
    current_raw = current_action.action_terms.get("price")
    original_raw = base.proposed_action.price
    if book_raw is None or current_raw is None or original_raw is None:
        return None
    try:
        book = Decimal(book_raw)
        current = Decimal(str(current_raw))
        original = Decimal(original_raw)
        tick = Decimal(snapshot.instrument_rules.price_tick_size)
        offset = Decimal(rule.book_offset_bps) / Decimal(10_000)
        maximum_move = Decimal(rule.maximum_total_move_bps) / Decimal(10_000)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if min(book, current, original, tick) <= 0:
        return None
    raw_target = (
        book * (Decimal(1) - offset)
        if activation.direction is Direction.LONG
        else book * (Decimal(1) + offset)
    )
    target = (
        (raw_target / tick).to_integral_value(rounding=ROUND_DOWN) * tick
        if activation.direction is Direction.LONG
        else (
            (raw_target / tick).to_integral_value(rounding=ROUND_CEILING)
            * tick
        )
    )
    lower = (
        (original * (Decimal(1) - maximum_move) / tick).to_integral_value(
            rounding=ROUND_CEILING
        )
        * tick
    )
    upper = (
        (original * (Decimal(1) + maximum_move) / tick).to_integral_value(
            rounding=ROUND_DOWN
        )
        * tick
    )
    target = max(lower, min(upper, target))
    if target <= 0 or target == current:
        return None
    distance_bps = abs(target - current) * Decimal(10_000) / current
    if distance_bps < Decimal(rule.trigger_distance_bps):
        return None
    return canonical_decimal(target)


def _entry_reprice_management_decision(
    activation: PlanActivation,
    schedule_actions: tuple[ExecutionAction, ...],
    *,
    active: ExecutionAction | None,
    cancel_already_recorded: bool,
    entry_actions_with_fills: frozenset[str],
    condition_facts: ConditionFacts,
    observed_at: datetime,
) -> _EntryManagementDecision:
    rule = _reprice_rule(activation)
    if (
        rule is None
        or active is None
        or active.state is not ExecutionActionState.OPEN
        or cancel_already_recorded
        or active.execution_action_id in entry_actions_with_fills
    ):
        return _EntryManagementDecision()
    reprice_count = sum(
        1
        for action in schedule_actions
        if (
            action.action_terms.get("execution_context", {})
            .get("order_schedule", {})
            .get("retry_reason")
            == "ENTRY_REPRICE"
        )
    )
    if reprice_count >= rule.max_adjustments:
        return _EntryManagementDecision()
    anchor = (
        getattr(active, "call_started_at", None)
        or getattr(active, "created_at", None)
    )
    if (
        anchor is None
        or anchor.utcoffset() is None
        or observed_at.utcoffset() is None
        or observed_at.astimezone(UTC)
        < anchor.astimezone(UTC)
        + timedelta(seconds=rule.minimum_update_interval_seconds)
    ):
        return _EntryManagementDecision()
    snapshot = activation.order_schedule_snapshot
    if snapshot is None:
        raise ValueError("ORDER_SCHEDULE_SNAPSHOT_REQUIRED")
    legs = materialize_direct_schedule(
        activation,
        entry_valid_until=_entry_valid_until(activation),
    )
    if len(legs) != 1:
        raise ValueError("REPRICE_ENTRY_POLICY_CONFLICT")
    if (
        _aligned_reprice_target(
            activation,
            legs[0],
            active,
            condition_facts,
        )
        is None
    ):
        return _EntryManagementDecision()
    return _EntryManagementDecision(
        reason_code="DIRECT_ENTRY_REPRICE",
        cancel_target=active,
    )


def _entry_management_decision(
    activation: PlanActivation,
    all_actions: tuple[ExecutionAction, ...],
    schedule_actions: tuple[ExecutionAction, ...],
    *,
    condition_facts: ConditionFacts,
    entry_actions_with_fills: frozenset[str],
    observed_at: datetime,
    entry_valid_until: datetime,
    expire_anchor: datetime | None,
    cycle_closed: bool,
    cycle_status_unknown: bool,
    protection_unproven: bool,
) -> _EntryManagementDecision:
    snapshot = activation.order_schedule_snapshot
    if snapshot is None:
        raise ValueError("ORDER_SCHEDULE_SNAPSHOT_REQUIRED")
    expire_rule = next(
        (
            item
            for item in snapshot.schedule_spec.dynamic_rules
            if isinstance(item, ExpireRemainingRule)
        ),
        None,
    )
    direct_time_exit = direct_time_exit_at(activation)
    expire_rule_reached = (
        expire_rule is not None
        and expire_anchor is not None
        and observed_at
        >= expire_anchor + timedelta(seconds=expire_rule.after_seconds)
    )
    time_sliced = (
        snapshot.schedule_spec.resolved_entry_program.kind
        is EntryProgramKind.TIME_SLICED
    )
    expired = (
        observed_at >= entry_valid_until
        or (expire_rule_reached and not time_sliced)
        or (direct_time_exit is not None and observed_at >= direct_time_exit)
    )
    active = next(
        (
            action
            for action in schedule_actions
            if action.state
            in {
                ExecutionActionState.SUBMITTING,
                ExecutionActionState.UNKNOWN,
                ExecutionActionState.OPEN,
            }
        ),
        None,
    )
    cancel_already_recorded = (
        active is not None
        and active.client_order_id is not None
        and any(
            action.action_kind is ExecutionActionKind.CANCEL
            and isinstance(action.cancel_target, dict)
            and action.cancel_target.get("client_order_id") == active.client_order_id
            for action in all_actions
        )
    )
    if cycle_closed:
        return _EntryManagementDecision(
            reason_code="DIRECT_ENTRY_CYCLE_CLOSED",
            expire_ready=True,
            cancel_target=(
                active
                if active is not None
                and active.state is ExecutionActionState.OPEN
                and not cancel_already_recorded
                else None
            ),
        )
    if time_sliced and expire_rule_reached:
        return _EntryManagementDecision(
            reason_code="DIRECT_TIME_SLICE_EXPIRED",
            expire_ready=False,
            cancel_target=(
                active
                if active is not None
                and active.state is ExecutionActionState.OPEN
                and not cancel_already_recorded
                else None
            ),
        )
    if expired:
        return _EntryManagementDecision(
            reason_code="DIRECT_ENTRY_REMAINING_EXPIRED",
            expire_ready=True,
            cancel_target=(
                active
                if active is not None
                and active.state is ExecutionActionState.OPEN
                and not cancel_already_recorded
                else None
            ),
        )
    if cycle_status_unknown:
        return _EntryManagementDecision(
            reason_code="DIRECT_ENTRY_CYCLE_STATUS_UNKNOWN",
            cancel_target=(
                active
                if active is not None
                and active.state is ExecutionActionState.OPEN
                and not cancel_already_recorded
                else None
            ),
        )
    if protection_unproven:
        return _EntryManagementDecision(
            reason_code="DIRECT_ENTRY_PROTECTION_UNPROVEN",
            cancel_target=(
                active
                if active is not None
                and active.state is ExecutionActionState.OPEN
                and not cancel_already_recorded
                else None
            ),
        )
    invalidation = _entry_invalidation_evaluation(activation, condition_facts)
    if invalidation.result is ConditionResult.FALSE:
        return _entry_reprice_management_decision(
            activation,
            schedule_actions,
            active=active,
            cancel_already_recorded=cancel_already_recorded,
            entry_actions_with_fills=entry_actions_with_fills,
            condition_facts=condition_facts,
            observed_at=observed_at,
        )
    trigger_count = sum(
        1
        for action in all_actions
        if action.action_kind is ExecutionActionKind.CANCEL
        and (
            ":DIRECT_DYNAMIC:DIRECT_ENTRY_SHOCK:" in action.source_identity
            or ":DIRECT_DYNAMIC:DIRECT_ENTRY_INVALIDATION_PRICE:"
            in action.source_identity
            or ":DIRECT_DYNAMIC:DIRECT_ENTRY_OPPORTUNITY_MISSED_PRICE:"
            in action.source_identity
        )
    )
    if trigger_count:
        return _EntryManagementDecision(
            reason_code="DIRECT_ENTRY_INVALIDATION_TRIGGERED",
            expire_ready=True,
            cancel_target=_entry_cancel_target(
                snapshot,
                active,
                cancel_already_recorded=cancel_already_recorded,
            ),
        )
    if invalidation.result is ConditionResult.UNKNOWN:
        return _EntryManagementDecision(
            reason_code=invalidation.reason_code,
            cancel_target=_entry_cancel_target(
                snapshot,
                active,
                cancel_already_recorded=cancel_already_recorded,
            ),
        )
    return _EntryManagementDecision(
        reason_code=invalidation.reason_code,
        expire_ready=True,
        cancel_target=_entry_cancel_target(
            snapshot,
            active,
            cancel_already_recorded=cancel_already_recorded,
        ),
    )


def _gtd_expiry_too_soon(policy: object, *, observed_at: datetime) -> bool:
    if not isinstance(policy, dict) or policy.get("time_in_force") != "GTD":
        return False
    raw_expire_at = policy.get("expire_at")
    if not isinstance(raw_expire_at, str):
        raise ValueError("DIRECT_GTD_EXPIRY_REQUIRED")
    try:
        expire_at = datetime.fromisoformat(raw_expire_at.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("DIRECT_GTD_EXPIRY_INVALID") from None
    if expire_at.utcoffset() is None or observed_at.utcoffset() is None:
        raise ValueError("DIRECT_GTD_TIMEZONE_REQUIRED")
    return expire_at <= observed_at + timedelta(seconds=BINANCE_GTD_MIN_LEAD_SECONDS)


def _post_only_submission_block_reason(
    item: MaterializedOrderLeg,
    facts: ConditionFacts,
    *,
    direction: Direction,
) -> str | None:
    policy = item.proposed_action.execution_context.get("venue_policy")
    if not isinstance(policy, dict) or policy.get("post_only") is not True:
        return None
    price_raw = item.proposed_action.price
    if price_raw is None:
        raise ValueError("DIRECT_POST_ONLY_PRICE_REQUIRED")
    if facts.bid_price is None or facts.ask_price is None:
        return "DIRECT_POST_ONLY_BOOK_UNKNOWN"
    price = Decimal(price_raw)
    bid = Decimal(facts.bid_price)
    ask = Decimal(facts.ask_price)
    if bid <= 0 or ask <= 0 or bid > ask:
        return "DIRECT_POST_ONLY_BOOK_UNKNOWN"
    if direction is Direction.LONG and price >= ask:
        return "DIRECT_POST_ONLY_WOULD_TAKE"
    if direction is Direction.SHORT and price <= bid:
        return "DIRECT_POST_ONLY_WOULD_TAKE"
    return None


def _schedule_submission_started_at(
    schedule_actions: tuple[ExecutionAction, ...],
) -> datetime | None:
    started = tuple(
        value
        for action in schedule_actions
        if (value := getattr(action, "call_started_at", None)) is not None
    )
    if not started:
        return None
    if any(value.utcoffset() is None for value in started):
        raise ValueError("DIRECT_SCHEDULE_TIMEZONE_REQUIRED")
    return min(value.astimezone(UTC) for value in started)


def _entry_expire_anchor(
    activation: PlanActivation,
    schedule_actions: tuple[ExecutionAction, ...],
) -> datetime | None:
    snapshot = activation.order_schedule_snapshot
    if snapshot is None:
        raise ValueError("ORDER_SCHEDULE_SNAPSHOT_REQUIRED")
    if (
        snapshot.schedule_spec.resolved_entry_program.kind
        is not EntryProgramKind.TIME_SLICED
    ):
        return _schedule_submission_started_at(schedule_actions)
    active_started = tuple(
        action.call_started_at
        for action in schedule_actions
        if action.state
        in {
            ExecutionActionState.SUBMITTING,
            ExecutionActionState.UNKNOWN,
            ExecutionActionState.OPEN,
        }
        and action.call_started_at is not None
    )
    if not active_started:
        return None
    if any(value.utcoffset() is None for value in active_started):
        raise ValueError("DIRECT_SCHEDULE_TIMEZONE_REQUIRED")
    return max(value.astimezone(UTC) for value in active_started)


def _entry_remaining_expiry_at(
    activation: PlanActivation,
    schedule_actions: tuple[ExecutionAction, ...],
) -> datetime | None:
    snapshot = activation.order_schedule_snapshot
    if snapshot is None:
        raise ValueError("ORDER_SCHEDULE_SNAPSHOT_REQUIRED")
    rule = next(
        (
            item
            for item in snapshot.schedule_spec.dynamic_rules
            if isinstance(item, ExpireRemainingRule)
        ),
        None,
    )
    started_at = _entry_expire_anchor(activation, schedule_actions)
    if rule is None or started_at is None:
        return None
    return started_at + timedelta(seconds=rule.after_seconds)


def _entry_management_expiry_at(
    activation: PlanActivation,
    *,
    remaining_valid_until: datetime,
    remaining_expiry_at: datetime | None,
) -> datetime:
    """Return the earliest frozen deadline which ends the entry opportunity."""

    candidates = [remaining_valid_until]
    if remaining_expiry_at is not None:
        candidates.append(remaining_expiry_at)
    direct_time_exit = direct_time_exit_at(activation)
    if direct_time_exit is not None:
        candidates.append(direct_time_exit)
    if any(value.utcoffset() is None for value in candidates):
        raise ValueError("DIRECT_SCHEDULE_TIMEZONE_REQUIRED")
    return min(value.astimezone(UTC) for value in candidates)


def _completed_time_slice_expiry_at(
    activation: PlanActivation,
    actions: tuple[ExecutionAction, ...],
    latest_by_leg: dict[int, ExecutionAction],
) -> datetime | None:
    snapshot = activation.order_schedule_snapshot
    if snapshot is None:
        raise ValueError("ORDER_SCHEDULE_SNAPSHOT_REQUIRED")
    if (
        snapshot.schedule_spec.resolved_entry_program.kind
        is not EntryProgramKind.TIME_SLICED
        or len(latest_by_leg) != len(snapshot.normalized_legs)
        or any(
            action.state
            not in {
                ExecutionActionState.CLOSED,
                ExecutionActionState.NOT_SUBMITTED,
            }
            for action in latest_by_leg.values()
        )
    ):
        return None
    rule = next(
        (
            item
            for item in snapshot.schedule_spec.dynamic_rules
            if isinstance(item, ExpireRemainingRule)
        ),
        None,
    )
    if rule is None:
        return None
    final_entry = latest_by_leg[max(latest_by_leg)]
    if final_entry.call_started_at is None:
        return None
    if final_entry.call_started_at.utcoffset() is None:
        raise ValueError("DIRECT_SCHEDULE_TIMEZONE_REQUIRED")
    final_client_order_id = final_entry.client_order_id
    if final_client_order_id is None or not any(
        action.action_kind is ExecutionActionKind.CANCEL
        and action.state is ExecutionActionState.CLOSED
        and isinstance(action.cancel_target, dict)
        and action.cancel_target.get("client_order_id") == final_client_order_id
        and ":DIRECT_TIME_SLICE_EXPIRED:" in str(
            action.action_terms.get("causation_ref", "")
        )
        for action in actions
    ):
        return None
    return final_entry.call_started_at.astimezone(UTC) + timedelta(
        seconds=rule.after_seconds
    )


def _management_uuid(environment_id: str, kind: str, identity: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"urn:halpha:{environment_id}:{kind}:{identity}"))


def _materialized_schedule_attempt(
    activation: PlanActivation,
    base: MaterializedOrderLeg,
    schedule: dict[str, Any],
) -> MaterializedOrderLeg:
    attempt_index = schedule.get("attempt_index", 0)
    if not isinstance(attempt_index, int) or attempt_index < 0:
        raise ValueError("ORDER_SCHEDULE_RETRY_INVALID")
    if attempt_index == 0:
        if any(
            key in schedule
            for key in ("retry_reason", "replacement_price", "reprice_index")
        ):
            raise ValueError("ORDER_SCHEDULE_ACTION_CONFLICT")
        return base
    retry_reason = schedule.get("retry_reason")
    replacement_price = schedule.get("replacement_price")
    reprice_index = schedule.get("reprice_index", 0)
    if replacement_price is not None and not isinstance(replacement_price, str):
        raise ValueError("ORDER_SCHEDULE_REPRICE_INVALID")
    if not isinstance(reprice_index, int) or reprice_index < 0:
        raise ValueError("ORDER_SCHEDULE_REPRICE_INVALID")
    if retry_reason == "ENTRY_REPRICE":
        if replacement_price is None:
            raise ValueError("ORDER_SCHEDULE_REPRICE_INVALID")
        return materialize_direct_schedule_reprice(
            activation,
            base,
            attempt_index=attempt_index,
            replacement_price=replacement_price,
            reprice_index=reprice_index,
        )
    if retry_reason in {
        "POST_ONLY_WOULD_TAKE_RACE",
        "PRICE_MATCH_TEMPORARILY_UNAVAILABLE",
    }:
        return materialize_direct_schedule_retry(
            activation,
            base,
            attempt_index=attempt_index,
            retry_reason=retry_reason,
            replacement_price=replacement_price,
            reprice_index=reprice_index,
        )
    raise ValueError("ORDER_SCHEDULE_RETRY_INVALID")


def _schedule_actions(
    actions: tuple[ExecutionAction, ...],
    activation: PlanActivation,
    legs: tuple[MaterializedOrderLeg, ...],
) -> tuple[ExecutionAction, ...]:
    snapshot = activation.order_schedule_snapshot
    if snapshot is None:
        raise ValueError("ORDER_SCHEDULE_SNAPSHOT_REQUIRED")
    expected_by_leg = {item.leg.leg_index: item for item in legs}
    selected = tuple(
        action
        for action in actions
        if isinstance(action.action_terms.get("execution_context"), dict)
        and action.action_terms["execution_context"]
        .get("order_schedule", {})
        .get("schedule_digest")
        == snapshot.schedule_digest
    )
    if not selected:
        return ()
    observed_attempts: dict[int, set[int]] = {}
    for action in selected:
        context = action.action_terms.get("execution_context", {})
        schedule = context.get("order_schedule", {})
        leg_index = schedule.get("leg_index")
        attempt_index = schedule.get("attempt_index", 0)
        if (
            not isinstance(leg_index, int)
            or not isinstance(attempt_index, int)
            or attempt_index < 0
            or leg_index not in expected_by_leg
        ):
            raise ValueError("ORDER_SCHEDULE_LOCAL_RESPONSIBILITY_CONFLICT")
        observed_attempts.setdefault(leg_index, set())
        if attempt_index in observed_attempts[leg_index]:
            raise ValueError("ORDER_SCHEDULE_LOCAL_RESPONSIBILITY_CONFLICT")
        observed_attempts[leg_index].add(attempt_index)
        base = expected_by_leg[leg_index]
        item = _materialized_schedule_attempt(activation, base, schedule)
        if (
            action.execution_action_id != item.execution_action_id
            or action.source_identity != item.source_identity
            or action.client_order_id != item.client_order_id
            or schedule.get("leg_index") != item.leg.leg_index
            or schedule.get("submission_index") != item.submission_index
            or action.action_terms.get("causation_ref")
            != item.proposed_action.causation_ref
            or action.action_terms.get("price") != item.proposed_action.price
        ):
            raise ValueError("ORDER_SCHEDULE_ACTION_CONFLICT")
    for leg_index, attempts in observed_attempts.items():
        if attempts != set(range(max(attempts) + 1)):
            raise ValueError("ORDER_SCHEDULE_LOCAL_RESPONSIBILITY_CONFLICT")
    return tuple(
        sorted(
            selected,
            key=lambda action: (
                int(
                    action.action_terms["execution_context"]["order_schedule"][
                        "submission_index"
                    ]
                ),
                int(
                    action.action_terms["execution_context"]["order_schedule"].get(
                        "attempt_index",
                        0,
                    )
                ),
            ),
        )
    )


def _schedule_attempt_index(action: ExecutionAction) -> int:
    context = action.action_terms.get("execution_context")
    schedule = context.get("order_schedule") if isinstance(context, dict) else None
    value = schedule.get("attempt_index", 0) if isinstance(schedule, dict) else None
    if not isinstance(value, int) or value < 0:
        raise ValueError("ORDER_SCHEDULE_RETRY_INVALID")
    return value


def _latest_schedule_actions_by_leg(
    actions: tuple[ExecutionAction, ...],
) -> dict[int, ExecutionAction]:
    latest: dict[int, ExecutionAction] = {}
    for action in actions:
        context = action.action_terms.get("execution_context")
        schedule = context.get("order_schedule") if isinstance(context, dict) else None
        leg_index = schedule.get("leg_index") if isinstance(schedule, dict) else None
        if not isinstance(leg_index, int):
            raise ValueError("ORDER_SCHEDULE_ACTION_CONFLICT")
        current = latest.get(leg_index)
        if current is None or _schedule_attempt_index(action) > _schedule_attempt_index(
            current
        ):
            latest[leg_index] = action
    return latest


def _retryable_entry_rejection(
    coordinator: DirectScheduleCoordinator,
    action: ExecutionAction,
) -> VenueRejectionDisposition | None:
    facts = coordinator.list_venue_facts_for_action(action.execution_action_id)
    return venue_rejection_disposition(action, facts)


def _entry_policy_retry_count(actions: tuple[ExecutionAction, ...]) -> int:
    return sum(
        1
        for action in actions
        if (
            action.action_terms.get("execution_context", {})
            .get("order_schedule", {})
            .get("retry_reason")
            in {
                "POST_ONLY_WOULD_TAKE_RACE",
                "PRICE_MATCH_TEMPORARILY_UNAVAILABLE",
            }
        )
    )


def _retry_reason_for_disposition(
    disposition: VenueRejectionDisposition,
) -> str:
    if disposition is VenueRejectionDisposition.RETRYABLE_POST_ONLY:
        return "POST_ONLY_WOULD_TAKE_RACE"
    if disposition is VenueRejectionDisposition.RETRYABLE_PRICE_MATCH:
        return "PRICE_MATCH_TEMPORARILY_UNAVAILABLE"
    raise ValueError("ENTRY_POLICY_RETRY_DISPOSITION_INVALID")


def _replacement_context(
    action: ExecutionAction,
) -> tuple[str | None, int]:
    schedule = (
        action.action_terms.get("execution_context", {}).get("order_schedule", {})
    )
    replacement_price = schedule.get("replacement_price")
    reprice_index = schedule.get("reprice_index", 0)
    if replacement_price is not None and not isinstance(replacement_price, str):
        raise ValueError("ORDER_SCHEDULE_REPRICE_INVALID")
    if not isinstance(reprice_index, int) or reprice_index < 0:
        raise ValueError("ORDER_SCHEDULE_REPRICE_INVALID")
    return replacement_price, reprice_index


def _closed_reprice_cancel_for_entry(
    coordinator: DirectScheduleCoordinator,
    actions: tuple[ExecutionAction, ...],
    entry: ExecutionAction,
) -> ExecutionAction | None:
    if (
        entry.state is not ExecutionActionState.CLOSED
        or entry.client_order_id is None
    ):
        return None
    entry_facts = coordinator.list_venue_facts_for_action(
        entry.execution_action_id
    )
    if (
        terminal_order_status(entry_facts) not in {"CANCELLED", "EXPIRED"}
        or not terminal_fills_complete(entry, entry_facts)
        or any(fact.kind is VenueFactKind.FILL for fact in entry_facts)
    ):
        return None
    matches = tuple(
        action
        for action in actions
        if (
            action.action_kind is ExecutionActionKind.CANCEL
            and action.state is ExecutionActionState.CLOSED
            and isinstance(action.cancel_target, dict)
            and action.cancel_target.get("client_order_id") == entry.client_order_id
            and ":DIRECT_ENTRY_REPRICE:" in str(
                action.action_terms.get("causation_ref", "")
            )
        )
    )
    if len(matches) > 1:
        raise ValueError("ORDER_SCHEDULE_REPRICE_CANCEL_CONFLICT")
    return matches[0] if matches else None


def _entry_policy_retry_ready_at(action: ExecutionAction) -> datetime:
    attempt_index = _schedule_attempt_index(action)
    delay_seconds = min(
        ENTRY_POLICY_RETRY_MAX_DELAY_SECONDS,
        2 ** min(attempt_index + 1, 5),
    )
    updated_at = (
        getattr(action, "updated_at", None)
        or getattr(
            action,
            "call_completed_at",
            None,
        )
        or action.created_at
    )
    if updated_at.utcoffset() is None:
        raise ValueError("DIRECT_SCHEDULE_TIMEZONE_REQUIRED")
    return updated_at.astimezone(UTC) + timedelta(seconds=delay_seconds)


def _risk_summary(
    coordinator: DirectScheduleCoordinator,
    activation: PlanActivation,
    actions: tuple[ExecutionAction, ...],
) -> _RiskSummary:
    sign = Decimal(1) if activation.direction is Direction.LONG else Decimal(-1)
    position = Decimal(0)
    outstanding_quantity = Decimal(0)
    outstanding_notional = Decimal(0)
    ordinary: set[str] = set()
    algo: set[str] = set()
    for action in actions:
        if action.client_order_id is not None:
            if action.action_kind in {
                ExecutionActionKind.PROTECTION,
                ExecutionActionKind.TAKE_PROFIT,
            }:
                algo.add(action.client_order_id)
            else:
                ordinary.add(action.client_order_id)
        facts = coordinator.list_venue_facts_for_action(action.execution_action_id)
        filled = sum(
            (
                Decimal(str(fact.payload["last_quantity"]))
                for fact in facts
                if fact.kind is VenueFactKind.FILL
                and fact.payload.get("last_quantity") is not None
            ),
            Decimal(0),
        )
        if action.action_kind is ExecutionActionKind.ENTRY:
            position += sign * filled
            if action.state in {
                ExecutionActionState.SUBMITTING,
                ExecutionActionState.UNKNOWN,
                ExecutionActionState.OPEN,
            }:
                quantity = Decimal(str(action.action_terms.get("quantity", "0")))
                remaining = max(Decimal(0), quantity - filled)
                outstanding_quantity += remaining
                price = action.action_terms.get("price")
                if price is None:
                    schedule = action.action_terms.get("execution_context", {}).get(
                        "order_schedule",
                        {},
                    )
                    price = schedule.get("sizing_price")
                if price is not None:
                    outstanding_notional += remaining * Decimal(str(price))
        elif action.action_kind in {
            ExecutionActionKind.PROTECTION,
            ExecutionActionKind.TAKE_PROFIT,
            ExecutionActionKind.RISK_REDUCTION,
            ExecutionActionKind.EXIT,
        }:
            position -= sign * filled
    return _RiskSummary(
        canonical_decimal(position),
        canonical_decimal(outstanding_quantity),
        canonical_decimal(outstanding_notional),
        frozenset(ordinary),
        frozenset(algo),
    )


def _fills_have_commissions(facts: tuple[VenueFact, ...]) -> bool:
    fill_trade_ids = {
        str(fact.payload.get("trade_id"))
        for fact in facts
        if fact.kind is VenueFactKind.FILL and fact.payload.get("trade_id") is not None
    }
    commission_trade_ids = {
        str(fact.payload.get("trade_id"))
        for fact in facts
        if fact.kind is VenueFactKind.COMMISSION
        and fact.payload.get("trade_id") is not None
    }
    return bool(fill_trade_ids) and fill_trade_ids.issubset(commission_trade_ids)


def _has_terminal_open_entry(
    coordinator: DirectScheduleCoordinator,
    actions: tuple[ExecutionAction, ...],
) -> bool:
    return any(
        action.action_kind is ExecutionActionKind.ENTRY
        and action.state is ExecutionActionState.OPEN
        and terminal_order_status(
            coordinator.list_venue_facts_for_action(action.execution_action_id)
        )
        is not None
        for action in actions
    )


def _has_risk_reduction_fill(
    coordinator: DirectScheduleCoordinator,
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
            for fact in coordinator.list_venue_facts_for_action(
                action.execution_action_id
            )
        )
        for action in actions
    )


def _has_unprotected_open_entry_fill(
    coordinator: DirectScheduleCoordinator,
    actions: tuple[ExecutionAction, ...],
) -> bool:
    protections = tuple(
        action
        for action in actions
        if action.action_kind is ExecutionActionKind.PROTECTION
    )
    for entry in actions:
        if entry.action_kind is not ExecutionActionKind.ENTRY:
            continue
        fills = tuple(
            fact
            for fact in coordinator.list_venue_facts_for_action(
                entry.execution_action_id
            )
            if fact.kind is VenueFactKind.FILL
        )
        for fill in fills:
            matching: list[ExecutionAction] = []
            for protection in protections:
                protection_facts = coordinator.list_venue_facts_for_action(
                    protection.execution_action_id
                )
                protective = order_is_working(protection_facts) or (
                    terminal_order_status(protection_facts) == "FILLED"
                    and terminal_fills_complete(protection, protection_facts)
                    and _fills_have_commissions(protection_facts)
                )
                if (
                    protection.action_terms.get("execution_context", {}).get(
                        "fill_fact_ref"
                    )
                    == fill.venue_fact_id
                    and _same_positive_quantity(
                        protection.action_terms.get("quantity"),
                        fill.payload.get("last_quantity"),
                    )
                    and protective
                ):
                    matching.append(protection)
            if len(matching) != 1:
                return True
    return False


def _close_proven_entry_actions(
    coordinator: DirectScheduleCoordinator,
    actions: tuple[ExecutionAction, ...],
    *,
    risk_facts: ProductRiskReductionFacts,
    risk_summary: _RiskSummary,
    observed_at: datetime,
) -> None:
    position_fact = risk_facts.position_fact
    if position_fact is None or Decimal(risk_facts.current_abs_position) != abs(
        Decimal(risk_summary.expected_signed_position)
    ):
        return
    protections = tuple(
        action
        for action in actions
        if action.action_kind is ExecutionActionKind.PROTECTION
    )
    for entry in actions:
        if (
            entry.action_kind is not ExecutionActionKind.ENTRY
            or entry.state is not ExecutionActionState.OPEN
        ):
            continue
        facts = coordinator.list_venue_facts_for_action(entry.execution_action_id)
        terminal = terminal_order_status(facts)
        if terminal is None:
            continue
        if not terminal_fills_complete(entry, facts):
            continue
        fills = tuple(fact for fact in facts if fact.kind is VenueFactKind.FILL)
        if fills and not _fills_have_commissions(facts):
            continue
        covering_refs: list[str] = []
        protection_fact_refs: list[str] = []
        protected = True
        for fill in fills:
            matching: list[tuple[ExecutionAction, tuple[VenueFact, ...]]] = []
            for protection in protections:
                protection_facts = coordinator.list_venue_facts_for_action(
                    protection.execution_action_id
                )
                terminal_protection = terminal_order_status(protection_facts)
                is_protective = order_is_working(protection_facts) or (
                    terminal_protection == "FILLED"
                    and terminal_fills_complete(protection, protection_facts)
                    and _fills_have_commissions(protection_facts)
                )
                if (
                    protection.action_terms.get("execution_context", {}).get(
                        "fill_fact_ref"
                    )
                    == fill.venue_fact_id
                    and _same_positive_quantity(
                        protection.action_terms.get("quantity"),
                        fill.payload.get("last_quantity"),
                    )
                    and is_protective
                ):
                    matching.append((protection, protection_facts))
            if len(matching) != 1:
                protected = False
                break
            protection, matched_facts = matching[0]
            covering_refs.append(protection.execution_action_id)
            protection_fact_refs.extend(fact.venue_fact_id for fact in matched_facts)
        if not protected:
            continue
        fact_refs = tuple(
            dict.fromkeys(
                (
                    *(fact.venue_fact_id for fact in facts),
                    *protection_fact_refs,
                    position_fact.venue_fact_id,
                )
            )
        )
        coordinator.reconcile_execution_action(
            entry.execution_action_id,
            closure_evidence={
                "order_terminal": True,
                "terminal_order_status": terminal,
                "fills_complete": True,
                "fees_complete": not fills or _fills_have_commissions(facts),
                "position_effect_known": True,
                "position_fact_ref": position_fact.venue_fact_id,
                "protection_action_refs": tuple(covering_refs),
            },
            venue_fact_refs=fact_refs,
            observed_at=observed_at,
        )


def _same_positive_quantity(left: object, right: object) -> bool:
    """Compare venue and persisted quantities by value, not text formatting."""

    try:
        left_quantity = Decimal(str(left))
        right_quantity = Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return left_quantity > 0 and left_quantity == right_quantity


def _action_check_for_leg(
    account: ProductAccountFacts,
    activation: PlanActivation,
    leg: MaterializedOrderLeg,
    *,
    economic_action_prior_notional: str,
    include_economic_prior_margin: bool = False,
    quantity_override: str | None = None,
    environment_id: str,
    environment_kind: EnvironmentKind,
    authority_class: AuthorityClass,
    account_ref: str,
) -> ActionCheckInput:
    action_price_value = max(
        Decimal(account.conservative_price),
        Decimal(leg.leg.sizing_price),
    )
    action_price = canonical_decimal(action_price_value)
    quantity = (
        quantity_override
        if quantity_override is not None
        else _runtime_entry_quantity(
            account,
            activation,
            leg,
            action_price=action_price_value,
        )
    )
    prior_margin = Decimal(0)
    if include_economic_prior_margin:
        prior_margin = Decimal(economic_action_prior_notional) / effective_leverage(
            account.actual_margin_mode,
            account.actual_leverage,
        )
    adjusted = replace(
        account,
        conservative_price=action_price,
        activation_current_margin=canonical_decimal(
            Decimal(account.activation_current_margin) + prior_margin
        ),
        post_action_abs_position=canonical_decimal(
            Decimal(account.current_abs_position) + Decimal(quantity)
        ),
    )
    proposed = leg.proposed_action.model_copy(update={"quantity": quantity})
    return adjusted.direct_action_check(
        proposed,
        activation_id=activation.activation_id,
        economic_action_prior_notional=economic_action_prior_notional,
        environment_id=environment_id,
        environment_kind=environment_kind,
        authority_class=authority_class,
        account_ref=account_ref,
    )


def _runtime_entry_quantity(
    account: ProductAccountFacts,
    activation: PlanActivation,
    leg: MaterializedOrderLeg,
    *,
    action_price: Decimal,
) -> str:
    """Keep a market leg within its requested notional at the latest safe price."""

    snapshot = activation.order_schedule_snapshot
    if snapshot is None:
        raise ValueError("ORDER_SCHEDULE_SNAPSHOT_REQUIRED")
    if snapshot.schedule_spec.venue_policy.order_type is not VenueOrderType.MARKET:
        return leg.leg.quantity
    rules = snapshot.instrument_rules
    step = Decimal(rules.market_quantity_step)
    planned = Decimal(leg.leg.quantity)
    requested_notional = Decimal(leg.leg.requested_notional)
    with localcontext() as context:
        context.prec = 128
        affordable = (requested_notional / action_price / step).to_integral_value(
            rounding=ROUND_DOWN
        ) * step
    quantity = min(planned, affordable)
    if quantity < Decimal(rules.min_market_quantity):
        raise ProductPreSubmitRejected("ORDER_SCHEDULE_RUNTIME_QUANTITY_BELOW_MINIMUM")
    if quantity * action_price < Decimal(rules.min_notional):
        raise ProductPreSubmitRejected("ORDER_SCHEDULE_RUNTIME_NOTIONAL_BELOW_MINIMUM")
    return canonical_decimal(quantity)
