from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Price, Quantity
from pydantic import SecretStr

import halpha.executor.runtime as runtime_module
from halpha.capital.models import EnvironmentKind
from halpha.domain_values import content_digest
from halpha.executor.account_observation import AccountObservationError
from halpha.executor.forward_observation import ForwardObservationSpec
from halpha.executor.product_entry import (
    LiveEntryFactTracker,
    ProductPreSubmitRejected,
)
from halpha.executor.runtime import (
    ExecutorRuntimeError,
    ProductExecutorRuntime,
    _activation_entry_deadline,
    _cached_filled_quantity,
    _cached_leaves_quantity,
    _cached_order_quantity,
    _binance_data_stream_state,
    _binance_execution_stream_state,
    _binance_mark_price_stream_name,
    _connect_product_database,
    _required_mark_price_event_state,
    _resolve_binance_data_client,
    _resolve_binance_execution_client,
    _require_zero_binance_mutation_retries,
    _stable_failure_reason_code,
    _venue_event_reason_code,
    build_product_node_config,
    query_execution_hedge_mode,
)
from halpha.product_build import EXECUTOR_STARTING_APPLICATION_NAME
from halpha.venue_integration.models import VenueFactKind
from halpha.planning.adapter import HalphaStrategyAdapter
from halpha.planning.bar_evaluation import NautilusBarEntryEvaluator
from halpha.planning.models import PlanLifecycle
from halpha.planning.order_policies import EntryConditionKind
from halpha.planning.registry import DIRECT_EXECUTION_REF, Direction, OneShotParameters
from halpha.planning.strategies.one_shot import OneShotDonchianAtrLogic


ROOT = Path(__file__).resolve().parents[2]


def _execution_client_with_stream_state(
    *,
    connected: bool = True,
    authenticated: bool = True,
    subscription_id: str | None = "listen-key",
    recovery_failed: bool = False,
    dispatch_paused: bool = False,
    buffered_event_count: int = 0,
    reconnecting: bool = False,
    disconnecting: bool = False,
    closed: bool = False,
    active: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        # Nautilus Component exposes this as a bool property in the qualified
        # runtime; the observer also tolerates older callable shapes.
        is_connected=connected,
        _ws_client=SimpleNamespace(
            _is_recovery_failed=recovery_failed,
        _dispatch_paused=dispatch_paused,
        _dispatch_buffer=[b"event"] * buffered_event_count,
            is_authenticated=authenticated,
            subscription_id=subscription_id,
            _stream_client=SimpleNamespace(
                is_reconnecting=lambda: reconnecting,
                is_disconnecting=lambda: disconnecting,
                is_closed=lambda: closed,
                is_active=lambda: active,
            ),
        ),
    )


def _data_client_with_stream_state(
    *,
    connected: bool = True,
    subscribed: bool = True,
    raw_client_present: bool = True,
    reconnecting: bool = False,
    disconnecting: bool = False,
    closed: bool = False,
    active: bool = True,
) -> SimpleNamespace:
    stream_name = "btcusdt@markPrice"
    raw_client = SimpleNamespace(
        is_reconnecting=lambda: reconnecting,
        is_disconnecting=lambda: disconnecting,
        is_closed=lambda: closed,
        is_active=lambda: active,
    )
    primary = SimpleNamespace(
        subscriptions=[stream_name] if subscribed else [],
        _clients={0: raw_client if raw_client_present else None},
        _client_streams={0: [stream_name] if subscribed else []},
    )
    secondary = SimpleNamespace(
        subscriptions=[],
        _clients={},
        _client_streams={},
    )
    return SimpleNamespace(
        is_connected=connected,
        _ws_client=primary,
        _ws_public_client=secondary,
    )


def test_execution_stream_health_reads_the_framework_recovery_contract() -> None:
    assert (
        _binance_execution_stream_state(
            _execution_client_with_stream_state(connected=False)
        )
        == "CLIENT_DISCONNECTED"
    )
    assert (
        _binance_execution_stream_state(
            _execution_client_with_stream_state(recovery_failed=True)
        )
        == "RECOVERY_FAILED"
    )
    assert (
        _binance_execution_stream_state(
            _execution_client_with_stream_state(authenticated=False)
        )
        == "RECOVERING"
    )
    assert (
        _binance_execution_stream_state(
            _execution_client_with_stream_state(dispatch_paused=True)
        )
        == "RECOVERING"
    )
    assert (
        _binance_execution_stream_state(
            _execution_client_with_stream_state(
                dispatch_paused=True,
                buffered_event_count=(
                    runtime_module._EXECUTION_RECOVERY_BUFFER_EVENT_LIMIT + 1
                ),
            )
        )
        == "RECOVERY_BUFFER_LIMIT_EXCEEDED"
    )
    assert (
        _binance_execution_stream_state(
            _execution_client_with_stream_state(reconnecting=True)
        )
        == "RECOVERING"
    )
    assert (
        _binance_execution_stream_state(
            _execution_client_with_stream_state(active=False)
        )
        == "RECOVERING"
    )
    assert (
        _binance_execution_stream_state(_execution_client_with_stream_state())
        == "HEALTHY"
    )


def test_market_data_stream_health_reads_every_subscribed_transport() -> None:
    assert (
        _binance_data_stream_state(
            _data_client_with_stream_state(connected=False)
        )
        == "CLIENT_DISCONNECTED"
    )
    assert (
        _binance_data_stream_state(
            _data_client_with_stream_state(subscribed=False)
        )
        == "HEALTHY"
    )
    assert (
        _binance_data_stream_state(
            _data_client_with_stream_state(subscribed=False),
            required_streams=("btcusdt@markPrice@1s",),
        )
        == "STREAM_SUBSCRIPTIONS_MISSING"
    )
    assert (
        _binance_data_stream_state(
            _data_client_with_stream_state(raw_client_present=False)
        )
        == "RECOVERING"
    )
    assert (
        _binance_data_stream_state(
            _data_client_with_stream_state(reconnecting=True)
        )
        == "RECOVERING"
    )
    assert (
        _binance_data_stream_state(_data_client_with_stream_state())
        == "HEALTHY"
    )


def test_runtime_market_health_requires_base_and_active_bar_streams() -> None:
    class Lifecycle:
        activation_ids = ("strategy-btc",)

        @staticmethod
        def adapter_for_activation(_activation_id: str):
            return SimpleNamespace(
                _bar_evaluator=SimpleNamespace(
                    subscribed_bar_types=(
                        BarType.from_str(
                            "BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL"
                        ),
                        BarType.from_str(
                            "BTCUSDT-PERP.BINANCE-15-MINUTE-LAST-EXTERNAL"
                        ),
                    )
                )
            )

    runtime = object.__new__(ProductExecutorRuntime)
    runtime._settings = SimpleNamespace(
        release=SimpleNamespace(profile="BINANCE_DEMO")
    )
    runtime._lifecycle = Lifecycle()
    runtime._market_fact_lifecycle = None

    assert runtime._required_market_data_streams() == (
        "btcusdt@bookTicker",
        "btcusdt@kline_15m",
        "btcusdt@kline_1m",
        "btcusdt@markPrice@1s",
        "ethusdt@bookTicker",
        "ethusdt@markPrice@1s",
    )


def test_mark_price_event_health_detects_silent_connected_stream() -> None:
    stream = _binance_mark_price_stream_name("BTCUSDT-PERP")
    required = ("btcusdt@bookTicker", stream)

    assert (
        _required_mark_price_event_state(required, {}, now=100.0)
        == "MARK_PRICE_EVENT_MISSING"
    )
    assert (
        _required_mark_price_event_state(
            required,
            {stream: 80.0},
            now=100.0,
        )
        == "MARK_PRICE_EVENT_STALE"
    )
    assert (
        _required_mark_price_event_state(
            required,
            {stream: 99.0},
            now=100.0,
        )
        == "HEALTHY"
    )


def test_execution_client_resolution_rejects_an_ambiguous_topology() -> None:
    client = object()
    node = SimpleNamespace(
        kernel=SimpleNamespace(
            exec_engine=SimpleNamespace(_clients={"BINANCE": client})
        )
    )

    assert _resolve_binance_execution_client(node) is client

    node.kernel.exec_engine._clients["OTHER"] = object()
    with pytest.raises(
        ExecutorRuntimeError,
        match="BINANCE_EXECUTION_CLIENT_TOPOLOGY_INVALID",
    ):
        _resolve_binance_execution_client(node)


def test_framework_mutation_retry_pool_must_be_disabled() -> None:
    _require_zero_binance_mutation_retries(
        SimpleNamespace(_retry_manager_pool=SimpleNamespace(max_retries=0))
    )

    for value in (None, 1, True):
        with pytest.raises(
            ExecutorRuntimeError,
            match="BINANCE_MUTATION_RETRY_POLICY_UNSAFE",
        ):
            _require_zero_binance_mutation_retries(
                SimpleNamespace(
                    _retry_manager_pool=SimpleNamespace(max_retries=value)
                )
            )


def test_data_client_resolution_rejects_an_ambiguous_topology() -> None:
    client = object()
    node = SimpleNamespace(
        kernel=SimpleNamespace(
            data_engine=SimpleNamespace(_clients={"BINANCE": client})
        )
    )

    assert _resolve_binance_data_client(node) is client

    node.kernel.data_engine._clients["OTHER"] = object()
    with pytest.raises(
        ExecutorRuntimeError,
        match="BINANCE_DATA_CLIENT_TOPOLOGY_INVALID",
    ):
        _resolve_binance_data_client(node)


def test_execution_stream_allows_bounded_recovery_then_requests_restart() -> None:
    now = [10.0]
    events: list[tuple[str, dict[str, object]]] = []
    runtime = object.__new__(ProductExecutorRuntime)
    runtime._loop = SimpleNamespace(time=lambda: now[0])
    runtime._framework_execution_client = _execution_client_with_stream_state(
        reconnecting=True
    )
    runtime._execution_stream_unhealthy_since = None
    runtime._execution_stream_last_state = "UNKNOWN"
    runtime._runtime_event_sink = lambda event, fields: events.append((event, fields))

    runtime._require_execution_stream_recoverable()
    now[0] += runtime_module._EXECUTION_STREAM_RECOVERY_TIMEOUT_SECONDS - 0.001
    runtime._require_execution_stream_recoverable()

    now[0] += 0.001
    with pytest.raises(
        ExecutorRuntimeError,
        match="BINANCE_EXECUTION_STREAM_RECOVERY_FAILED",
    ):
        runtime._require_execution_stream_recoverable()

    assert [event for event, _fields in events] == [
        "execution_stream_recovering",
        "execution_stream_recovery_failed",
    ]


def test_execution_stream_permanent_failure_requests_immediate_restart() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    runtime = object.__new__(ProductExecutorRuntime)
    runtime._loop = SimpleNamespace(time=lambda: 10.0)
    runtime._framework_execution_client = _execution_client_with_stream_state(
        recovery_failed=True
    )
    runtime._execution_stream_unhealthy_since = None
    runtime._execution_stream_last_state = "UNKNOWN"
    runtime._runtime_event_sink = lambda event, fields: events.append((event, fields))

    with pytest.raises(
        ExecutorRuntimeError,
        match="BINANCE_EXECUTION_STREAM_RECOVERY_FAILED",
    ):
        runtime._require_execution_stream_recoverable()

    assert [event for event, _fields in events] == [
        "execution_stream_recovering",
        "execution_stream_recovery_failed",
    ]


def test_execution_stream_recovery_clears_the_restart_deadline() -> None:
    now = [10.0]
    events: list[tuple[str, dict[str, object]]] = []
    runtime = object.__new__(ProductExecutorRuntime)
    runtime._loop = SimpleNamespace(time=lambda: now[0])
    runtime._framework_execution_client = _execution_client_with_stream_state(
        reconnecting=True
    )
    runtime._execution_stream_unhealthy_since = None
    runtime._execution_stream_last_state = "UNKNOWN"
    runtime._runtime_event_sink = lambda event, fields: events.append((event, fields))

    runtime._require_execution_stream_recoverable()
    now[0] += 5.0
    runtime._framework_execution_client = _execution_client_with_stream_state()
    runtime._require_execution_stream_recoverable()

    assert runtime._execution_stream_unhealthy_since is None
    assert events[-1] == (
        "execution_stream_recovered",
        {"previous_state": "RECOVERING", "recovery_seconds": 5.0},
    )


def test_market_data_stream_allows_bounded_recovery_then_requests_restart() -> None:
    now = [10.0]
    events: list[tuple[str, dict[str, object]]] = []
    runtime = object.__new__(ProductExecutorRuntime)
    runtime._loop = SimpleNamespace(time=lambda: now[0])
    runtime._framework_data_client = _data_client_with_stream_state(
        reconnecting=True
    )
    runtime._market_data_stream_unhealthy_since = None
    runtime._market_data_stream_last_state = "UNKNOWN"
    runtime._runtime_event_sink = lambda event, fields: events.append((event, fields))

    runtime._require_market_data_stream_recoverable()
    now[0] += runtime_module._MARKET_DATA_STREAM_RECOVERY_TIMEOUT_SECONDS
    with pytest.raises(
        ExecutorRuntimeError,
        match="BINANCE_MARKET_DATA_STREAM_RECOVERY_FAILED",
    ):
        runtime._require_market_data_stream_recoverable()

    assert [event for event, _fields in events] == [
        "market_data_stream_recovering",
        "market_data_stream_recovery_failed",
    ]


def test_market_data_stream_recovery_clears_the_restart_deadline() -> None:
    now = [10.0]
    events: list[tuple[str, dict[str, object]]] = []
    runtime = object.__new__(ProductExecutorRuntime)
    runtime._loop = SimpleNamespace(time=lambda: now[0])
    runtime._framework_data_client = _data_client_with_stream_state(
        reconnecting=True
    )
    runtime._market_data_stream_unhealthy_since = None
    runtime._market_data_stream_last_state = "UNKNOWN"
    runtime._runtime_event_sink = lambda event, fields: events.append((event, fields))

    runtime._require_market_data_stream_recoverable()
    now[0] += 5.0
    runtime._framework_data_client = _data_client_with_stream_state()
    runtime._require_market_data_stream_recoverable()

    assert runtime._market_data_stream_unhealthy_since is None
    assert events[-1] == (
        "market_data_stream_recovered",
        {"previous_state": "RECOVERING", "recovery_seconds": 5.0},
    )


def test_runtime_heartbeat_is_driven_independently_by_the_event_loop() -> None:
    loop = asyncio.new_event_loop()
    heartbeats: list[float] = []
    runtime = object.__new__(ProductExecutorRuntime)
    runtime._loop = loop
    runtime._runtime_heartbeat_sink = lambda: heartbeats.append(loop.time())
    runtime._runtime_heartbeat_handle = None

    try:
        runtime._start_runtime_heartbeat(interval_seconds=0.001)
        loop.run_until_complete(asyncio.sleep(0.005))
        runtime._stop_runtime_heartbeat()
        count_after_stop = len(heartbeats)
        loop.run_until_complete(asyncio.sleep(0.003))
    finally:
        loop.close()

    assert count_after_stop >= 2
    assert len(heartbeats) == count_after_stop


def test_runtime_submission_barrier_is_scoped_to_the_affected_activation() -> None:
    submitted: list[str] = []
    runtime = object.__new__(ProductExecutorRuntime)
    runtime._coordinator = SimpleNamespace(
        startup_recovery_allows_submission=lambda activation_id: (
            activation_id == "activation-clear"
        )
    )
    runtime._proposal_processors = {
        "activation-clear": SimpleNamespace(
            submit=lambda proposal: (
                submitted.append(proposal.activation_id) or "accepted"
            )
        )
    }

    with pytest.raises(ExecutorRuntimeError, match="STARTUP_RECOVERY_PENDING"):
        runtime.submit_strategy_proposal(
            SimpleNamespace(activation_id="activation-pending")
        )

    assert (
        runtime.submit_strategy_proposal(
            SimpleNamespace(activation_id="activation-clear")
        )
        == "accepted"
    )
    assert submitted == ["activation-clear"]


def test_live_risk_control_only_runtime_blocks_new_entry_proposals() -> None:
    runtime = ProductExecutorRuntime(
        settings=SimpleNamespace(
            release=SimpleNamespace(profile="BINANCE_LIVE_WRITE")
        ),
        database_password=SecretStr("qualification-password"),
        api_key=SecretStr("qualification-key"),
        api_secret=SecretStr("qualification-secret"),
        log_directory=Path("logs"),
        runtime_real_write_gate="CLOSED",
        live_write_activation_ids=("activation-live",),
        live_write_submission_guard=lambda _activation_id: None,
        live_write_risk_control_only=True,
    )
    runtime._coordinator = SimpleNamespace(
        startup_recovery_allows_submission=lambda _activation_id: True
    )
    runtime._proposal_processors = {
        "activation-live": SimpleNamespace(submit=lambda _proposal: "unexpected")
    }

    try:
        assert runtime._activation_submission_enabled("activation-live") is False
        with pytest.raises(ExecutorRuntimeError, match="STARTUP_RECOVERY_PENDING"):
            runtime.submit_strategy_proposal(
                SimpleNamespace(activation_id="activation-live")
            )
    finally:
        runtime.close()


def test_runtime_does_not_mark_recovery_complete_until_resolution_is_absorbed() -> None:
    action = SimpleNamespace(
        execution_action_id="startup-open",
        activation_id="activation-pending",
    )
    coordinator = SimpleNamespace(
        complete=False,
        sink=None,
    )
    coordinator.initialize_startup_recovery = lambda **values: (
        setattr(coordinator, "sink", values["resolution_sink"]) or (action,)
    )
    coordinator.startup_recovery_complete = lambda: coordinator.complete
    coordinator.startup_recovery_pending_action_ids = lambda: (
        () if coordinator.complete else (action.execution_action_id,)
    )
    coordinator.startup_recovery_allows_submission = lambda _activation_id: (
        coordinator.complete
    )
    responsibility_resumes: list[str] = []
    direct_resumes: list[str] = []
    events: list[tuple[str, dict[str, object]]] = []
    runtime = object.__new__(ProductExecutorRuntime)
    runtime._coordinator = coordinator
    runtime._recovered_action_count = 0
    runtime._recovery_complete = False
    runtime._startup_recovery_prepared = False
    runtime._startup_recovered_actions = ()
    runtime._responsibility_processors = {
        action.activation_id: SimpleNamespace(resume=responsibility_resumes.append)
    }
    runtime._direct_schedule_processors = {
        action.activation_id: SimpleNamespace(resume=direct_resumes.append)
    }
    runtime._runtime_event_sink = lambda event, fields: events.append((event, fields))

    recovered = runtime._prepare_startup_recovery_before_node_run(
        observed_at=datetime(2026, 7, 23, 7, 0, tzinfo=UTC)
    )

    assert recovered == (action,)
    assert runtime.recovery_complete is False
    assert responsibility_resumes == []
    assert direct_resumes == []

    # The callback is invoked only after the coordinator has absorbed a fact.
    coordinator.complete = True
    runtime._apply_startup_recovery_resolution(
        action.activation_id,
        action.execution_action_id,
    )

    assert runtime.recovery_complete is True
    assert responsibility_resumes == [action.activation_id]
    assert direct_resumes == [action.activation_id]
    assert events[-1][0] == "startup_recovery_identity_resolved"


def test_runtime_republishes_ready_after_last_startup_identity_is_resolved() -> None:
    ready_evidence: list[dict[str, object]] = []
    runtime = object.__new__(ProductExecutorRuntime)
    runtime._coordinator = SimpleNamespace(
        startup_recovery_complete=lambda: True,
        startup_recovery_pending_action_ids=lambda: (),
        startup_recovery_allows_submission=lambda _activation_id: True,
    )
    runtime._recovery_complete = False
    runtime._recovered_action_count = 1
    runtime._runtime_real_write_gate = "CLOSED"
    runtime._live_write_risk_control_only = False
    runtime._runtime_ready_sink = ready_evidence.append
    runtime._runtime_ready_reported = True
    runtime._responsibility_processors = {}
    runtime._direct_schedule_processors = {}
    runtime._runtime_event_sink = lambda _event, _fields: None

    runtime._apply_startup_recovery_resolution(
        "activation-pending",
        "startup-open",
    )

    assert ready_evidence == [
        {
            "product_runtime_started": True,
            "database_continuity_guard_completed": True,
            "startup_reconciliation_completed": True,
            "recovered_unresolved_actions": 1,
            "startup_reconciliation_pending_actions": 0,
            "runtime_real_write_gate": "CLOSED",
            "live_write_risk_control_only": False,
        }
    ]


def test_runtime_does_not_publish_ready_before_initial_runtime_report() -> None:
    ready_evidence: list[dict[str, object]] = []
    runtime = object.__new__(ProductExecutorRuntime)
    runtime._coordinator = SimpleNamespace(
        startup_recovery_complete=lambda: True,
        startup_recovery_pending_action_ids=lambda: (),
        startup_recovery_allows_submission=lambda _activation_id: True,
    )
    runtime._recovery_complete = False
    runtime._recovered_action_count = 1
    runtime._runtime_real_write_gate = "CLOSED"
    runtime._live_write_risk_control_only = False
    runtime._runtime_ready_sink = ready_evidence.append
    runtime._runtime_ready_reported = False
    runtime._responsibility_processors = {}
    runtime._direct_schedule_processors = {}
    runtime._runtime_event_sink = lambda _event, _fields: None

    runtime._apply_startup_recovery_resolution(
        "activation-pending",
        "startup-open",
    )

    assert ready_evidence == []


def test_partial_startup_resolution_resumes_risk_control_but_not_direct_entry() -> None:
    responsibility_resumes: list[str] = []
    direct_resumes: list[str] = []
    runtime = object.__new__(ProductExecutorRuntime)
    runtime._coordinator = SimpleNamespace(
        startup_recovery_complete=lambda: False,
        startup_recovery_pending_action_ids=lambda: ("other-pending-action",),
        startup_recovery_allows_submission=lambda _activation_id: False,
    )
    runtime._responsibility_processors = {
        "activation-pending": SimpleNamespace(
            resume=responsibility_resumes.append
        )
    }
    runtime._direct_schedule_processors = {
        "activation-pending": SimpleNamespace(resume=direct_resumes.append)
    }
    runtime._runtime_event_sink = lambda _event, _fields: None

    runtime._apply_startup_recovery_resolution(
        "activation-pending",
        "resolved-protection-action",
    )

    assert runtime.recovery_complete is False
    assert responsibility_resumes == ["activation-pending"]
    assert direct_resumes == []


def test_runtime_close_reports_each_failure_and_continues_cleanup() -> None:
    events: list[tuple[str, dict[str, object]]] = []

    class FailingCloser:
        @staticmethod
        def close() -> None:
            raise RuntimeError("private cleanup detail")

    class FailingLifecycle:
        @staticmethod
        def stop_all() -> None:
            raise RuntimeError("private lifecycle detail")

    class FailingNode:
        @staticmethod
        def is_running() -> bool:
            return True

        @staticmethod
        def stop() -> None:
            raise RuntimeError("private stop detail")

        @staticmethod
        def dispose() -> None:
            raise RuntimeError("private dispose detail")

    runtime = object.__new__(ProductExecutorRuntime)
    runtime._proposal_processors = {"proposal": FailingCloser()}
    runtime._direct_schedule_processors = {"direct": FailingCloser()}
    runtime._responsibility_processors = {"responsibility": FailingCloser()}
    runtime._lifecycle = FailingLifecycle()
    runtime._node = FailingNode()
    runtime._connection = FailingCloser()
    runtime._coordinator = object()
    runtime._capability = object()
    runtime._owns_loop = False
    runtime._loop = asyncio.new_event_loop()
    runtime._runtime_event_sink = lambda event, fields: events.append((event, fields))
    try:
        runtime.close()
    finally:
        runtime._loop.close()

    assert [fields["component"] for _event, fields in events] == [
        "proposal_processor:proposal",
        "direct_schedule_processor:direct",
        "responsibility_processor:responsibility",
        "activation_lifecycle",
        "trading_node_stop",
        "trading_node_dispose",
        "database_connection",
    ]
    assert {event for event, _fields in events} == {"runtime_cleanup_failed"}
    assert {fields["reason"] for _event, fields in events} == {"RuntimeError"}
    assert runtime._proposal_processors == {}
    assert runtime._direct_schedule_processors == {}
    assert runtime._responsibility_processors == {}
    assert runtime._lifecycle is None
    assert runtime._node is None
    assert runtime._connection is None
    assert runtime._coordinator is None
    assert runtime._capability is None


def test_activation_entry_deadline_uses_persisted_activation_window() -> None:
    activation = SimpleNamespace(
        rule_state={"deadlines": {"entry_valid_until": "2026-07-19T22:15:00+00:00"}}
    )

    assert _activation_entry_deadline(activation) == datetime(
        2026, 7, 19, 22, 15, tzinfo=UTC
    )


def test_activation_entry_deadline_rejects_missing_state() -> None:
    with pytest.raises(ExecutorRuntimeError, match="ENTRY_DEADLINE_MISSING"):
        _activation_entry_deadline(SimpleNamespace(rule_state={}))


def _config(profile: str):
    return build_product_node_config(
        profile,
        api_key=(
            None
            if profile == "BINANCE_LIVE_READ_ONLY"
            else SecretStr("qualification-key")
        ),
        api_secret=(
            None
            if profile == "BINANCE_LIVE_READ_ONLY"
            else SecretStr("qualification-secret")
        ),
        log_directory=Path("logs"),
    )


def test_demo_product_node_uses_the_accepted_single_topology() -> None:
    node, provider, data, execution = _config("BINANCE_DEMO")

    assert execution is not None
    assert data.instrument_provider is provider
    assert execution.instrument_provider is provider
    assert data.environment is BinanceEnvironment.DEMO
    assert execution.environment is BinanceEnvironment.DEMO
    assert {str(item) for item in provider.load_ids or ()} == {
        "BTCUSDT-PERP.BINANCE",
        "ETHUSDT-PERP.BINANCE",
    }
    assert provider.load_all is False
    assert provider.query_commission_rates is True
    assert execution.max_retries is None
    assert execution.use_reduce_only is True
    assert execution.use_position_ids is True
    assert execution.futures_leverages is None
    assert execution.futures_margin_types is None
    assert node.cache is None
    assert node.message_bus is None
    assert node.emulator is None
    assert node.load_state is False
    assert node.save_state is False
    assert node.exec_engine.reconciliation is True
    assert node.exec_engine.reconciliation_lookback_mins == 60
    assert node.exec_engine.purge_closed_orders_interval_mins == 60
    assert node.exec_engine.purge_closed_orders_buffer_mins == 24 * 60
    assert node.exec_engine.purge_closed_positions_interval_mins == 60
    assert node.exec_engine.purge_closed_positions_buffer_mins == 24 * 60
    assert node.exec_engine.purge_account_events_interval_mins == 60
    assert node.exec_engine.purge_account_events_lookback_mins == 24 * 60
    assert node.exec_engine.purge_from_database is False
    assert node.exec_engine.reconciliation_startup_delay_secs == 10.0
    assert node.exec_engine.inflight_check_interval_ms == 2_000
    assert node.exec_engine.open_check_interval_secs is None
    assert node.exec_engine.open_check_open_only is True
    assert node.exec_engine.generate_missing_orders is False
    assert node.exec_engine.filter_position_reports is True
    assert node.exec_engine.position_check_interval_secs is None
    assert node.controller is not None
    assert node.controller.controller_path == (
        "halpha.executor.runtime:HalphaRuntimeController"
    )


def test_hedge_mode_execution_config_omits_reduce_only_wire_parameter() -> None:
    _node, _provider, _data, execution = build_product_node_config(
        "BINANCE_LIVE_WRITE",
        api_key=SecretStr("qualification-key"),
        api_secret=SecretStr("qualification-secret"),
        log_directory=Path("logs"),
        hedge_mode=True,
    )

    assert execution is not None
    assert execution.use_reduce_only is False
    assert execution.use_position_ids is True


def test_execution_position_mode_uses_nautilus_account_api() -> None:
    calls: list[dict[str, object]] = []

    class AccountApi:
        async def query_futures_hedge_mode(self, **kwargs: object) -> object:
            calls.append(kwargs)
            return SimpleNamespace(dualSidePosition=True)

    result = asyncio.run(
        query_execution_hedge_mode(
            "BINANCE_LIVE_WRITE",
            api_key=SecretStr("qualification-key"),
            api_secret=SecretStr("qualification-secret"),
            account_api=AccountApi(),
        )
    )

    assert result is True
    assert calls == [{"recv_window": "5000"}]


def test_execution_position_mode_schema_failure_is_fail_closed() -> None:
    class AccountApi:
        async def query_futures_hedge_mode(self, **_kwargs: object) -> object:
            return SimpleNamespace(dualSidePosition="true")

    with pytest.raises(
        ExecutorRuntimeError,
        match="POSITION_MODE_RESPONSE_SCHEMA_MISMATCH",
    ):
        asyncio.run(
            query_execution_hedge_mode(
                "BINANCE_DEMO",
                api_key=SecretStr("qualification-key"),
                api_secret=SecretStr("qualification-secret"),
                account_api=AccountApi(),
            )
        )


def test_demo_and_live_write_change_only_environment_qualified_inputs() -> None:
    demo_node, demo_provider, demo_data, demo_execution = _config("BINANCE_DEMO")
    live_node, live_provider, live_data, live_execution = _config("BINANCE_LIVE_WRITE")

    assert demo_execution is not None
    assert live_execution is not None
    assert live_data.environment is BinanceEnvironment.LIVE
    assert live_execution.environment is BinanceEnvironment.LIVE
    assert {str(item) for item in live_provider.load_ids or ()} == {
        "BTCUSDT-PERP.BINANCE"
    }
    assert type(demo_node) is type(live_node)
    assert type(demo_node.exec_engine) is type(live_node.exec_engine)
    assert demo_node.controller == live_node.controller
    demo_engine = demo_node.exec_engine.dict()
    live_engine = live_node.exec_engine.dict()
    demo_engine.pop("reconciliation_instrument_ids")
    live_engine.pop("reconciliation_instrument_ids")
    assert demo_engine == live_engine

    demo_provider_contract = demo_provider.dict()
    live_provider_contract = live_provider.dict()
    demo_provider_contract.pop("load_ids")
    live_provider_contract.pop("load_ids")
    assert demo_provider_contract == live_provider_contract

    demo_data_contract = demo_data.dict()
    live_data_contract = live_data.dict()
    for contract in (demo_data_contract, live_data_contract):
        contract.pop("environment")
        contract.pop("instrument_provider")
    assert demo_data_contract == live_data_contract

    demo_execution_contract = demo_execution.dict()
    live_execution_contract = live_execution.dict()
    for contract in (demo_execution_contract, live_execution_contract):
        contract.pop("environment")
        contract.pop("instrument_provider")
    assert demo_execution_contract == live_execution_contract


def test_live_read_only_uses_data_client_and_same_controller_without_execution() -> (
    None
):
    node, provider, data, execution = _config("BINANCE_LIVE_READ_ONLY")

    assert data.instrument_provider is provider
    assert data.environment is BinanceEnvironment.LIVE
    assert data.api_key is None
    assert data.api_secret is None
    assert provider.query_commission_rates is False
    assert execution is None
    assert node.exec_clients == {}
    assert node.exec_engine.reconciliation is False
    assert node.exec_engine.generate_missing_orders is False
    assert node.exec_engine.inflight_check_interval_ms == 0
    assert node.exec_engine.open_check_interval_secs is None
    assert node.exec_engine.position_check_interval_secs is None
    assert node.controller is not None
    assert node.controller.controller_path == (
        "halpha.executor.runtime:HalphaRuntimeController"
    )
    assert {str(item) for item in provider.load_ids or ()} == {"BTCUSDT-PERP.BINANCE"}


def _forward_spec() -> ForwardObservationSpec:
    starts_at = datetime(2026, 7, 18, tzinfo=UTC)
    parameters = OneShotParameters(
        direction="LONG",
        channel_lookback_15m=96,
        confirmation_bars_1m=3,
        initial_stop_atr_multiple="1",
        max_entry_extension_atr="0.1",
        take_profit_1_r="1",
        take_profit_1_fraction="0.75",
        take_profit_2_r="2",
        max_hold_bars_15m=96,
        entry_valid_minutes=1440,
    )
    source_sha256 = {"src/halpha/example.py": "3" * 64}
    return ForwardObservationSpec(
        observation_id="read-only-check-20260718",
        activation_id="read-only-check-btcusdt",
        strategy_evidence_ref="build/evidence/reports/strategy-evidence.json",
        strategy_evidence_digest="1" * 64,
        configuration_digest="2" * 64,
        source_sha256=source_sha256,
        source_sha256_digest=content_digest(source_sha256),
        parameters=parameters,
        parameter_digest=content_digest(parameters.model_dump(mode="json")),
        starts_at=starts_at,
        max_allowed_loss="50",
        max_notional="500",
        max_margin="100",
        effective_leverage="5",
    )


def test_live_read_only_sizing_snapshot_canonicalizes_instrument_values() -> None:
    class Cache:
        @staticmethod
        def instrument(_instrument_id):
            return SimpleNamespace(
                size_increment=Decimal("0.001000"),
                price_increment=Decimal("0.10"),
                min_quantity=Decimal("0.001"),
                max_quantity=Decimal("100.000"),
                min_notional=Decimal("5.000"),
                taker_fee=Decimal("0.0004000"),
            )

        @staticmethod
        def quote_tick(_instrument_id):
            return SimpleNamespace(
                ask_price=Decimal("60000.100"),
                bid_price=Decimal("59999.900"),
            )

        @staticmethod
        def mark_price(_instrument_id):
            return Decimal("60000")

    runtime = object.__new__(ProductExecutorRuntime)
    runtime._forward_observation_spec = _forward_spec()
    runtime._node = SimpleNamespace(cache=Cache())

    snapshot = runtime._read_only_sizing_snapshot(object())

    assert snapshot is not None
    assert snapshot.reference_price == "60000.100"
    assert snapshot.taker_fee_rate == "0.0004"
    assert snapshot.rules.step_size == "0.001"
    assert snapshot.rules.price_tick_size == "0.1"
    assert snapshot.rules.min_quantity == "0.001"
    assert snapshot.rules.max_market_quantity == "100"
    assert snapshot.rules.min_notional == "5"


def test_live_read_only_runtime_build_never_loads_database_or_exec_factory(
    monkeypatch,
) -> None:
    calls: dict[str, int] = {"connector": 0, "data_factory": 0, "exec_factory": 0}
    created: list[object] = []

    class FakeController:
        def create_strategy(self, strategy, start=True):
            assert start is True
            created.append(strategy)

        def stop_strategy(self, _strategy):
            return None

        def remove_strategy(self, _strategy):
            return None

    controller = FakeController()

    class FakeTrader:
        def actors(self):
            return [controller]

    class FakeNode:
        def __init__(self, *, config, loop):
            self.config = config
            self.loop = loop
            self.trader = FakeTrader()

        def add_data_client_factory(self, _venue, _factory):
            calls["data_factory"] += 1

        def add_exec_client_factory(self, _venue, _factory):
            calls["exec_factory"] += 1

        def build(self):
            return None

        def is_running(self):
            return False

        def dispose(self):
            return None

    def connector(**_kwargs):
        calls["connector"] += 1
        raise AssertionError(
            "read-only runtime must not connect to the product database"
        )

    monkeypatch.setattr(runtime_module, "HalphaRuntimeController", FakeController)
    settings = SimpleNamespace(
        release=SimpleNamespace(
            profile="BINANCE_LIVE_READ_ONLY",
            database_name="halpha_live_copy",
            environment_id="live-read-only",
            authority_class="NO_TRADING_AUTHORITY",
            account_id="binance-live",
        )
    )
    runtime = ProductExecutorRuntime(
        settings=settings,
        database_password=None,
        api_key=None,
        api_secret=None,
        log_directory=Path("logs"),
        forward_observation_spec=_forward_spec(),
        connector=connector,
        node_factory=FakeNode,
    )
    try:
        runtime.build()
        assert runtime.node.config.exec_clients == {}
        assert calls == {"connector": 0, "data_factory": 1, "exec_factory": 0}
        with pytest.raises(ExecutorRuntimeError, match="PRODUCT_RUNTIME_NOT_BUILT"):
            runtime.coordinator
        runtime._start_read_only_adapter()
        assert len(created) == 1
        adapter = created[0]
        assert isinstance(adapter, HalphaStrategyAdapter)
        assert isinstance(adapter._logic, OneShotDonchianAtrLogic)
        assert isinstance(adapter._bar_evaluator, NautilusBarEntryEvaluator)
        assert adapter._persisted_action_capability is None
        assert adapter._execution_event_sink is None
    finally:
        runtime.close()


def test_private_read_only_account_observer_has_database_facts_but_no_execution(
    monkeypatch,
) -> None:
    calls: dict[str, int] = {
        "connector": 0,
        "data_factory": 0,
        "exec_factory": 0,
        "observer": 0,
    }

    class Connection:
        closed = False

        def execute(self, _query, _params=()):
            return SimpleNamespace(fetchone=lambda: (1,))

        def close(self):
            self.closed = True

    connection = Connection()

    class FakeController:
        pass

    class FakeTrader:
        @staticmethod
        def actors():
            return [FakeController()]

    class FakeNode:
        def __init__(self, *, config, loop):
            self.config = config
            self.loop = loop
            self.trader = FakeTrader()

        def add_data_client_factory(self, _venue, _factory):
            calls["data_factory"] += 1

        def add_exec_client_factory(self, _venue, _factory):
            calls["exec_factory"] += 1

        @staticmethod
        def build():
            return None

        @staticmethod
        def is_running():
            return False

        @staticmethod
        def dispose():
            return None

    class Observer:
        def __init__(self, **kwargs):
            calls["observer"] += 1
            assert kwargs["profile"] == "BINANCE_LIVE_READ_ONLY"
            assert kwargs["account_ref"] == "copy-account"

    def connector(**kwargs):
        calls["connector"] += 1
        assert kwargs["user"] == "halpha_live_copy_executor"
        return connection

    monkeypatch.setattr(runtime_module, "HalphaRuntimeController", FakeController)
    monkeypatch.setattr(runtime_module, "ProductAccountObserver", Observer)
    runtime = ProductExecutorRuntime(
        settings=SimpleNamespace(
            release=SimpleNamespace(
                profile="BINANCE_LIVE_READ_ONLY",
                database_name="halpha_live_copy",
                environment_id="binance-live-copy-primary",
                authority_class="NO_TRADING_AUTHORITY",
                account_id="copy-account",
            )
        ),
        database_password=SecretStr("database-password"),
        api_key=SecretStr("read-key"),
        api_secret=SecretStr("read-secret"),
        log_directory=Path("logs"),
        connector=connector,
        node_factory=FakeNode,
        schema_validator=lambda _connection: None,
    )

    try:
        runtime.build()

        assert runtime.node.config.exec_clients == {}
        assert next(iter(runtime.node.config.data_clients.values())).api_key is None
        assert calls == {
            "connector": 1,
            "data_factory": 1,
            "exec_factory": 0,
            "observer": 1,
        }
        with pytest.raises(ExecutorRuntimeError, match="PRODUCT_RUNTIME_NOT_BUILT"):
            runtime.coordinator
    finally:
        runtime.close()

    assert connection.closed is True


def test_live_read_only_runtime_rejects_database_credential() -> None:
    settings = SimpleNamespace(
        release=SimpleNamespace(profile="BINANCE_LIVE_READ_ONLY")
    )
    runtime = ProductExecutorRuntime(
        settings=settings,
        database_password=SecretStr("forbidden"),
        api_key=None,
        api_secret=None,
        log_directory=Path("logs"),
        forward_observation_spec=_forward_spec(),
    )
    try:
        with pytest.raises(
            ExecutorRuntimeError,
            match="READ_ONLY_DATABASE_CREDENTIAL_FORBIDDEN",
        ):
            runtime.build()
    finally:
        runtime.close()


def test_write_runtime_checks_schema_before_building_trading_node(monkeypatch) -> None:
    closed: list[bool] = []
    node_builds: list[bool] = []

    class Connection:
        @staticmethod
        def close():
            closed.append(True)

    provider = object()
    monkeypatch.setattr(
        runtime_module,
        "build_product_node_config",
        lambda *_args, **_kwargs: (
            object(),
            provider,
            SimpleNamespace(instrument_provider=provider),
            SimpleNamespace(instrument_provider=provider),
        ),
    )
    runtime = ProductExecutorRuntime(
        settings=SimpleNamespace(
            release=SimpleNamespace(
                profile="BINANCE_DEMO",
                database_name="halpha_demo",
                environment_id="demo-main",
                authority_class="DEMO_VALIDATION",
                account_id="demo-account",
            )
        ),
        database_password=SecretStr("qualification-password"),
        api_key=SecretStr("qualification-key"),
        api_secret=SecretStr("qualification-secret"),
        log_directory=Path("logs"),
        connector=lambda **_kwargs: Connection(),
        node_factory=lambda **_kwargs: node_builds.append(True),
        schema_validator=lambda _connection: (_ for _ in ()).throw(
            RuntimeError("stale schema")
        ),
    )
    try:
        with pytest.raises(
            ExecutorRuntimeError,
            match="DATABASE_SCHEMA_NOT_CURRENT",
        ):
            runtime.build()
    finally:
        runtime.close()

    assert node_builds == []
    assert closed == [True]


def test_live_read_only_runtime_rejects_binance_credentials() -> None:
    settings = SimpleNamespace(
        release=SimpleNamespace(profile="BINANCE_LIVE_READ_ONLY")
    )
    runtime = ProductExecutorRuntime(
        settings=settings,
        database_password=None,
        api_key=SecretStr("forbidden"),
        api_secret=SecretStr("forbidden"),
        log_directory=Path("logs"),
        forward_observation_spec=_forward_spec(),
    )
    try:
        with pytest.raises(
            ExecutorRuntimeError,
            match="READ_ONLY_BINANCE_CREDENTIAL_FORBIDDEN",
        ):
            runtime.build()
    finally:
        runtime.close()


def test_runtime_source_does_not_depend_on_qualification_fixtures() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "halpha"
        / "executor"
        / "runtime.py"
    ).read_text(encoding="utf-8")
    assert "tools.qualification" not in source
    assert "BINANCE_API_KEY" not in source
    assert "BINANCE_API_SECRET" not in source


def test_cached_leaves_quantity_uses_the_framework_order_projection() -> None:
    requested: list[str] = []

    class Cache:
        def order(self, client_order_id):
            requested.append(str(client_order_id))
            return SimpleNamespace(leaves_qty="0.000")

    assert (
        _cached_leaves_quantity(
            Cache(),
            "0123456789abcdef0123456789abcdef",
        )
        == "0.000"
    )
    assert requested == ["0123456789abcdef0123456789abcdef"]


def test_cached_leaves_quantity_preserves_unknown_when_order_is_absent() -> None:
    cache = SimpleNamespace(order=lambda _client_order_id: None)

    assert (
        _cached_leaves_quantity(
            cache,
            "0123456789abcdef0123456789abcdef",
        )
        is None
    )


def test_cached_filled_quantity_uses_the_framework_order_projection() -> None:
    requested: list[str] = []

    class Cache:
        def order(self, client_order_id):
            requested.append(str(client_order_id))
            return SimpleNamespace(filled_qty="0.0015")

    assert (
        _cached_filled_quantity(
            Cache(),
            "0123456789abcdef0123456789abcdef",
        )
        == "0.0015"
    )
    assert requested == ["0123456789abcdef0123456789abcdef"]


def test_cached_filled_quantity_preserves_unknown_when_order_is_absent() -> None:
    cache = SimpleNamespace(order=lambda _client_order_id: None)

    assert (
        _cached_filled_quantity(
            cache,
            "0123456789abcdef0123456789abcdef",
        )
        is None
    )


def test_cached_order_quantity_uses_the_framework_order_projection() -> None:
    requested: list[str] = []

    class Cache:
        def order(self, client_order_id):
            requested.append(str(client_order_id))
            return SimpleNamespace(quantity="0.0015")

    assert (
        _cached_order_quantity(
            Cache(),
            "0123456789abcdef0123456789abcdef",
        )
        == "0.0015"
    )
    assert requested == ["0123456789abcdef0123456789abcdef"]


def test_runtime_installs_external_bridge_with_framework_cache_providers() -> None:
    subscriptions: list[tuple[str, object]] = []
    unsubscriptions: list[tuple[str, object]] = []
    provider_values: dict[str, object] = {}
    normalized_events: list[object] = []
    responsibility_events: list[object] = []
    direct_resumes: list[tuple[str, bool]] = []
    action = SimpleNamespace(activation_id="activation-recovered")
    normalized = SimpleNamespace(
        action=action,
        facts=(SimpleNamespace(kind=VenueFactKind.FILL),),
    )

    class Cache:
        @staticmethod
        def order(_client_order_id):
            return SimpleNamespace(
                leaves_qty="0.0005",
                filled_qty="0.0015",
                quantity="0.002",
            )

    class Coordinator:
        @staticmethod
        def build_nautilus_event_normalizer(**providers):
            provider_values.update(providers)
            return object()

        @staticmethod
        def handle_nautilus_order_event(_normalizer, event, **_kwargs):
            normalized_events.append(event)
            return normalized

        @staticmethod
        def disable_venue_mutations():
            raise AssertionError("successful bridge event must not disable writes")

    runtime = object.__new__(ProductExecutorRuntime)
    runtime._node = SimpleNamespace(
        cache=Cache(),
        trader=SimpleNamespace(
            subscribe=lambda topic, handler: subscriptions.append((topic, handler)),
            unsubscribe=lambda topic, handler: unsubscriptions.append(
                (topic, handler)
            ),
        ),
    )
    runtime._coordinator = Coordinator()
    runtime._external_order_event_handler = None
    runtime._responsibility_processors = {
        action.activation_id: SimpleNamespace(
            submit_event=responsibility_events.append
        )
    }
    runtime._direct_schedule_processors = {
        action.activation_id: SimpleNamespace(
            resume=lambda activation_id, *, force_risk_refresh: (
                direct_resumes.append((activation_id, force_risk_refresh))
            )
        )
    }
    runtime._runtime_event_sink = None

    runtime._install_external_order_event_bridge()
    runtime._install_external_order_event_bridge()

    assert len(subscriptions) == 1
    topic, handler = subscriptions[0]
    assert topic == "events.order.EXTERNAL"
    client_order_id = "0123456789abcdef0123456789abcdef"
    assert provider_values["leaves_quantity_for_client_order_id"](
        client_order_id
    ) == "0.0005"
    assert provider_values["filled_quantity_for_client_order_id"](
        client_order_id
    ) == "0.0015"
    assert provider_values["order_quantity_for_client_order_id"](
        client_order_id
    ) == "0.002"
    event = SimpleNamespace(client_order_id=client_order_id)
    handler(event)
    assert normalized_events == [event]
    assert responsibility_events == [normalized]
    assert direct_resumes == [(action.activation_id, True)]

    runtime._remove_external_order_event_bridge()
    runtime._remove_external_order_event_bridge()
    assert unsubscriptions == [(topic, handler)]


def test_runtime_external_bridge_failure_disables_all_venue_mutations() -> None:
    disabled = 0
    events: list[tuple[str, dict[str, object]]] = []
    subscriptions: list[object] = []

    class Coordinator:
        @staticmethod
        def build_nautilus_event_normalizer(**_providers):
            return object()

        @staticmethod
        def handle_nautilus_order_event(*_args, **_kwargs):
            raise OSError("private database failure")

        @staticmethod
        def disable_venue_mutations():
            nonlocal disabled
            disabled += 1

    runtime = object.__new__(ProductExecutorRuntime)
    runtime._node = SimpleNamespace(
        cache=SimpleNamespace(order=lambda _client_order_id: None),
        trader=SimpleNamespace(
            subscribe=lambda _topic, handler: subscriptions.append(handler)
        ),
    )
    runtime._coordinator = Coordinator()
    runtime._external_order_event_handler = None
    runtime._responsibility_processors = {}
    runtime._direct_schedule_processors = {}
    runtime._runtime_event_sink = lambda event, fields: events.append(
        (event, fields)
    )
    runtime._install_external_order_event_bridge()

    with pytest.raises(ExecutorRuntimeError, match="EXTERNAL_ORDER_BRIDGE_FAILED"):
        subscriptions[0](SimpleNamespace(client_order_id="client-id"))

    assert disabled == 1
    assert events[-2:] == [
        (
            "external_order_bridge_failed",
            {"activation_id": "runtime", "reason": "OSError"},
        ),
        (
            "runtime_component_failure_latched",
            {
                "component_event": "external_order_bridge_failed",
                "activation_id": "runtime",
                "reason": "OSError",
            },
        ),
    ]
    with pytest.raises(ExecutorRuntimeError, match="RUNTIME_COMPONENT_FAILURE"):
        runtime._require_no_fatal_component_failure()


def test_runtime_prepares_recovery_and_subscribes_before_node_run() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "halpha"
        / "executor"
        / "runtime.py"
    ).read_text(encoding="utf-8")
    start = source.index("def run_until_stop(")
    end = source.index("\n    def close(", start)
    run_source = source[start:end]

    assert (
        run_source.index("_prepare_startup_recovery_before_node_run(")
        < run_source.index("_install_external_order_event_bridge()")
        < run_source.index("self._node.run(")
    )
    startup = source.index("async def _startup_and_stop(")
    startup_end = source.index("\n    def run_until_stop(", startup)
    startup_source = source[startup:startup_end]
    reattached = startup_source.index(
        "self._record_executor_runtime_reattached()"
    )
    assert reattached < startup_source.index(
        "if on_ready is not None:",
        reattached,
    )
    assert (
        startup_source.rindex("await self.node.stop_async()")
        < startup_source.index(
            "self._remove_external_order_event_bridge()",
            startup_source.rindex("await self.node.stop_async()"),
        )
    )


def test_node_run_entry_cannot_publish_external_event_before_recovery_bridge() -> None:
    trace: list[str] = []
    subscribed: dict[str, object] = {}
    loop = asyncio.new_event_loop()

    class Coordinator:
        initialized = False

        @classmethod
        def initialize_startup_recovery(cls, **_kwargs):
            trace.append("RECOVERY_PREPARED")
            cls.initialized = True
            return ()

        @staticmethod
        def startup_recovery_complete():
            return True

        @staticmethod
        def build_nautilus_event_normalizer(**_providers):
            return object()

        @classmethod
        def handle_nautilus_order_event(cls, _normalizer, _event, **_kwargs):
            assert cls.initialized is True
            trace.append("EXTERNAL_EVENT_PERSISTED")
            return SimpleNamespace(action=None, facts=())

        @staticmethod
        def disable_venue_mutations():
            trace.append("MUTATIONS_DISABLED")

    class Trader:
        @staticmethod
        def subscribe(topic, handler):
            trace.append("BRIDGE_SUBSCRIBED")
            subscribed["topic"] = topic
            subscribed["handler"] = handler

    class Node:
        cache = SimpleNamespace(order=lambda _client_order_id: None)
        trader = Trader()

        @staticmethod
        def run(*, raise_exception):
            assert raise_exception is True
            trace.append("NODE_RUN")
            subscribed["handler"](SimpleNamespace(client_order_id="client-id"))
            loop.run_until_complete(asyncio.sleep(0))

    runtime = object.__new__(ProductExecutorRuntime)
    runtime._node = Node()
    runtime._coordinator = Coordinator()
    runtime._settings = SimpleNamespace(
        release=SimpleNamespace(profile="BINANCE_DEMO")
    )
    runtime._loop = loop
    runtime._recovery_complete = False
    runtime._recovered_action_count = 0
    runtime._startup_recovery_prepared = False
    runtime._startup_recovered_actions = ()
    runtime._external_order_event_handler = None
    runtime._responsibility_processors = {}
    runtime._direct_schedule_processors = {}
    runtime._runtime_event_sink = None

    async def startup_and_stop(_stop_wait, _on_ready):
        trace.append("STARTUP_TASK")

    runtime._startup_and_stop = startup_and_stop
    try:
        runtime.run_until_stop(lambda: None)
    finally:
        loop.close()

    assert subscribed["topic"] == "events.order.EXTERNAL"
    assert trace[:5] == [
        "RECOVERY_PREPARED",
        "BRIDGE_SUBSCRIBED",
        "NODE_RUN",
        "EXTERNAL_EVENT_PERSISTED",
        "STARTUP_TASK",
    ]
    assert trace[-1] == "MUTATIONS_DISABLED"


def test_product_database_connection_uses_explicit_transactions_on_autocommit() -> None:
    captured: dict[str, object] = {}
    connection = object()

    def connector(**kwargs):
        captured.update(kwargs)
        return connection

    assert (
        _connect_product_database(
            connector,
            database_name="halpha_demo",
            password="qualification-password",
            application_name=EXECUTOR_STARTING_APPLICATION_NAME,
        )
        is connection
    )
    assert captured == {
        "host": "127.0.0.1",
        "port": 5432,
        "dbname": "halpha_demo",
        "user": "halpha_demo_executor",
        "password": "qualification-password",
        "connect_timeout": 2,
        "autocommit": True,
        "options": (
            "-c statement_timeout=5000 "
            "-c lock_timeout=5000 "
            "-c idle_in_transaction_session_timeout=15000"
        ),
        "application_name": EXECUTOR_STARTING_APPLICATION_NAME,
    }


def test_product_runtime_publishes_ready_build_on_existing_connection() -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []
    runtime = object.__new__(ProductExecutorRuntime)
    runtime._settings = SimpleNamespace(release=SimpleNamespace(profile="BINANCE_DEMO"))
    runtime._connection = SimpleNamespace(
        execute=lambda statement, parameters: calls.append((statement, parameters))
    )

    runtime.publish_ready_product_build("b" * 64)

    assert calls == [
        (
            "SELECT set_config('application_name', %s, false)",
            ("halpha-executor:ready:" + "b" * 40,),
        )
    ]


def test_live_write_product_runtime_fails_closed_without_an_open_runtime_gate() -> None:
    settings = SimpleNamespace(release=SimpleNamespace(profile="BINANCE_LIVE_WRITE"))
    runtime = ProductExecutorRuntime(
        settings=settings,
        database_password=SecretStr("qualification-password"),
        api_key=SecretStr("qualification-key"),
        api_secret=SecretStr("qualification-secret"),
        log_directory=Path("logs"),
    )
    try:
        with pytest.raises(
            ExecutorRuntimeError,
            match="RUNTIME_REAL_WRITE_GATE_CLOSED",
        ):
            runtime.build()
    finally:
        runtime.close()


def test_live_write_runtime_requires_a_current_submission_guard_even_when_open() -> (
    None
):
    settings = SimpleNamespace(release=SimpleNamespace(profile="BINANCE_LIVE_WRITE"))
    runtime = ProductExecutorRuntime(
        settings=settings,
        database_password=SecretStr("qualification-password"),
        api_key=SecretStr("qualification-key"),
        api_secret=SecretStr("qualification-secret"),
        log_directory=Path("logs"),
        runtime_real_write_gate="OPEN",
        live_write_activation_ids=("activation-live-001",),
    )
    try:
        with pytest.raises(
            ExecutorRuntimeError,
            match="RUNTIME_REAL_WRITE_GATE_CLOSED",
        ):
            runtime.build()
    finally:
        runtime.close()


def test_executor_entry_checks_live_gate_before_resolving_binance_secrets() -> None:
    source = (ROOT / "src" / "halpha" / "executor" / "__main__.py").read_text(
        encoding="utf-8"
    )
    runtime_entry = source.index("live_write =")
    secret_resolution = source.index("api_key = resolver.resolve(key_reference)")
    precheck = source.index(
        "require_live_write_gate_startup_precheck(",
        runtime_entry,
    )
    startup_check = source.index("require_live_write_gate_startup(", precheck)
    credential_binding = source.index(
        "require_live_write_credential_binding(gate_status, api_key)",
        secret_resolution,
    )
    current_credential_binding = source.index(
        "require_live_write_credential_binding(\n"
        "                            current_status,\n"
        "                            api_key,\n"
        "                        )",
        startup_check,
    )
    current_account_qualification = source.index(
        "live_venue_account_qualifier.require_cached_current()",
        current_credential_binding,
    )
    qualifier_construction = source.index(
        "live_venue_account_qualifier = LiveVenueAccountQualifier(",
        credential_binding,
    )
    startup_account_qualification = source.index(
        "live_venue_account_qualifier.require_current()",
        qualifier_construction,
    )
    runtime_build = source.index("runtime = ProductExecutorRuntime(", credential_binding)
    assert (
        runtime_entry
        < precheck
        < startup_check
        < current_credential_binding
        < current_account_qualification
        < secret_resolution
        < credential_binding
        < qualifier_construction
        < startup_account_qualification
        < runtime_build
    )
    assert (
        "current_product_build_id=product_build_id"
        in source[precheck:secret_resolution]
    )


def test_runtime_strategy_proposal_boundary_requires_the_activation_processor() -> None:
    proposal = SimpleNamespace(activation_id="activation-product-boundary")
    processor = SimpleNamespace(
        submit=lambda accepted: f"action:{accepted.activation_id}"
    )
    runtime = object.__new__(ProductExecutorRuntime)
    runtime._proposal_processors = {proposal.activation_id: processor}

    assert runtime.submit_strategy_proposal(proposal) == (
        "action:activation-product-boundary"
    )

    runtime._proposal_processors = {}
    with pytest.raises(
        ExecutorRuntimeError,
        match="PRODUCT_PROPOSAL_PROCESSOR_NOT_READY",
    ):
        runtime.submit_strategy_proposal(proposal)


def test_runtime_stops_the_node_when_startup_times_out(monkeypatch) -> None:
    stopped = False

    class FakeNode:
        trader = SimpleNamespace(is_running=False)

        @staticmethod
        def is_running() -> bool:
            return False

        async def stop_async(self) -> None:
            nonlocal stopped
            stopped = True

    async def no_wait(_seconds: float) -> None:
        return None

    runtime = object.__new__(ProductExecutorRuntime)
    runtime._node = FakeNode()
    monkeypatch.setattr("halpha.executor.runtime.asyncio.sleep", no_wait)

    with pytest.raises(ExecutorRuntimeError, match="TRADING_NODE_START_TIMEOUT"):
        asyncio.run(runtime._startup_and_stop(lambda: None, None))

    assert stopped is True


def test_product_runtime_records_reattachment_for_startup_activations(
    monkeypatch,
) -> None:
    observed_at = datetime(2026, 7, 28, 6, 44, 6, tzinfo=UTC)
    recorded: list[tuple[str, datetime]] = []

    class FrozenDateTime:
        @staticmethod
        def now(_timezone):
            return observed_at

    runtime = object.__new__(ProductExecutorRuntime)
    runtime._lifecycle = SimpleNamespace(
        activation_ids=("activation-b", "activation-a")
    )
    runtime._coordinator = SimpleNamespace(
        record_executor_runtime_reattached=lambda **values: recorded.append(
            (values["activation_id"], values["observed_at"])
        )
    )
    monkeypatch.setattr("halpha.executor.runtime.datetime", FrozenDateTime)

    runtime._record_executor_runtime_reattached()

    assert recorded == [
        ("activation-b", observed_at),
        ("activation-a", observed_at),
    ]


def test_product_runtime_waits_for_every_strategy_history_warmup() -> None:
    adapters = {
        "activation-a": SimpleNamespace(live_history_ready=True),
        "activation-b": SimpleNamespace(live_history_ready=False),
    }

    class FakeLifecycle:
        @property
        def activation_ids(self):
            return tuple(adapters)

        @staticmethod
        def adapter_for_activation(activation_id):
            return adapters[activation_id]

    runtime = object.__new__(ProductExecutorRuntime)
    runtime._lifecycle = FakeLifecycle()

    async def exercise() -> None:
        runtime._loop = asyncio.get_running_loop()
        assert runtime.strategy_history_warmup_complete is False
        waiter = asyncio.create_task(
            runtime._wait_for_strategy_history_warmup(timeout_seconds=0.5)
        )
        await asyncio.sleep(0)
        adapters["activation-b"].live_history_ready = True
        await waiter
        assert runtime.strategy_history_warmup_complete is True

    asyncio.run(exercise())


def test_demo_runtime_discovers_ui_created_activation_without_restart() -> None:
    stop = threading.Event()
    activation_ids: list[str] = []
    sync_calls = 0
    warmup_calls = 0

    class FakeLifecycle:
        @property
        def activation_ids(self):
            return tuple(activation_ids)

    runtime = object.__new__(ProductExecutorRuntime)
    runtime._settings = SimpleNamespace(release=SimpleNamespace(profile="BINANCE_DEMO"))
    runtime._lifecycle = FakeLifecycle()
    runtime._responsibility_processors = {}

    def sync(_capability: object) -> None:
        nonlocal sync_calls
        sync_calls += 1
        activation_ids.append("activation-created-from-ui")
        stop.set()

    async def wait_for_warmup(*, timeout_seconds: float = 60.0) -> None:
        del timeout_seconds
        nonlocal warmup_calls
        warmup_calls += 1

    async def exercise() -> None:
        runtime._loop = asyncio.get_running_loop()
        runtime._restore_paused_adapters = sync
        runtime._wait_for_strategy_history_warmup = wait_for_warmup
        await runtime._wait_for_stop_and_sync_activations(
            stop.wait,
            object(),
            interval_seconds=0.001,
        )

    asyncio.run(exercise())

    assert sync_calls == 1
    assert activation_ids == ["activation-created-from-ui"]
    assert warmup_calls == 1


def test_live_runtime_periodically_advances_time_based_responsibilities() -> None:
    stop = threading.Event()
    responsibility_calls: list[str] = []
    direct_calls: list[str] = []

    class Responsibility:
        @staticmethod
        async def sync(activation_id: str) -> None:
            responsibility_calls.append(activation_id)
            stop.set()

    class Direct:
        @staticmethod
        def resume(activation_id: str) -> None:
            direct_calls.append(activation_id)

    runtime = object.__new__(ProductExecutorRuntime)
    runtime._settings = SimpleNamespace(
        release=SimpleNamespace(profile="BINANCE_LIVE_READ_ONLY")
    )
    runtime._lifecycle = SimpleNamespace(activation_ids=("activation-live",))
    runtime._responsibility_processors = {"activation-live": Responsibility()}
    runtime._direct_schedule_processors = {"activation-live": Direct()}
    runtime._runtime_event_sink = lambda *_args: None
    runtime._restore_paused_adapters = lambda _capability: (_ for _ in ()).throw(
        AssertionError("Live must not discover new activations")
    )

    async def exercise() -> None:
        runtime._loop = asyncio.get_running_loop()
        await runtime._wait_for_stop_and_sync_activations(
            stop.wait,
            object(),
            interval_seconds=0.001,
        )

    asyncio.run(exercise())

    assert responsibility_calls == ["activation-live"]
    assert direct_calls == ["activation-live"]


def test_runtime_stop_disables_venue_mutations_before_shutdown() -> None:
    stop = threading.Event()
    stop.set()
    disabled = 0

    class Coordinator:
        @staticmethod
        def startup_recovery_complete() -> bool:
            return True

        @staticmethod
        def disable_venue_mutations() -> None:
            nonlocal disabled
            disabled += 1

    runtime = object.__new__(ProductExecutorRuntime)
    runtime._settings = SimpleNamespace(
        release=SimpleNamespace(profile="BINANCE_LIVE_WRITE")
    )
    runtime._lifecycle = SimpleNamespace(activation_ids=())
    runtime._coordinator = Coordinator()
    runtime._responsibility_processors = {}
    runtime._direct_schedule_processors = {}
    runtime._runtime_event_sink = lambda *_args: None

    async def exercise() -> None:
        runtime._loop = asyncio.get_running_loop()
        await runtime._wait_for_stop_and_sync_activations(
            stop.wait,
            object(),
            interval_seconds=0.001,
        )
        await asyncio.sleep(0)

    asyncio.run(exercise())

    assert disabled == 1


def test_runtime_database_loss_disables_mutations_and_fails_closed() -> None:
    disabled = 0
    events: list[tuple[str, dict[str, object]]] = []

    class Connection:
        @staticmethod
        def execute(_query: str):
            raise OSError("private database diagnostic")

    class Coordinator:
        @staticmethod
        def disable_venue_mutations() -> None:
            nonlocal disabled
            disabled += 1

        @staticmethod
        def startup_recovery_complete() -> bool:
            return True

    runtime = object.__new__(ProductExecutorRuntime)
    runtime._settings = SimpleNamespace(
        release=SimpleNamespace(profile="BINANCE_LIVE_WRITE")
    )
    runtime._lifecycle = SimpleNamespace(activation_ids=())
    runtime._connection = Connection()
    runtime._coordinator = Coordinator()
    runtime._responsibility_processors = {}
    runtime._direct_schedule_processors = {}
    runtime._runtime_event_sink = lambda event, fields: events.append((event, fields))

    async def exercise() -> None:
        runtime._loop = asyncio.get_running_loop()
        stop_future: asyncio.Future[object] = runtime._loop.create_future()
        with pytest.raises(
            ExecutorRuntimeError,
            match="PRODUCT_DATABASE_RUNTIME_UNAVAILABLE",
        ):
            await runtime._wait_for_stop_and_sync_activations(
                lambda: None,
                object(),
                interval_seconds=0.001,
                stop_future=stop_future,
            )

    asyncio.run(exercise())

    assert disabled == 1
    assert events == [("product_database_runtime_unavailable", {})]


def test_runtime_stops_when_responsibility_sync_has_an_internal_failure() -> None:
    stop = threading.Event()
    events: list[tuple[str, dict[str, object]]] = []
    disabled = 0

    class FailingProcessor:
        @staticmethod
        async def sync(activation_id: str) -> None:
            assert activation_id == "activation-demo"
            stop.set()
            raise RuntimeError("private diagnostic detail")

    runtime = object.__new__(ProductExecutorRuntime)
    runtime._settings = SimpleNamespace(release=SimpleNamespace(profile="BINANCE_DEMO"))
    runtime._lifecycle = SimpleNamespace(activation_ids=("activation-demo",))
    runtime._responsibility_processors = {"activation-demo": FailingProcessor()}
    runtime._runtime_event_sink = lambda event, fields: events.append((event, fields))
    runtime._restore_paused_adapters = lambda _capability: None
    runtime._wait_for_strategy_history_warmup = lambda: None
    runtime._fatal_component_failure = None

    class Coordinator:
        @staticmethod
        def disable_venue_mutations() -> None:
            nonlocal disabled
            disabled += 1

        @staticmethod
        def startup_recovery_complete() -> bool:
            return True

    runtime._coordinator = Coordinator()

    async def exercise() -> None:
        runtime._loop = asyncio.get_running_loop()
        with pytest.raises(ExecutorRuntimeError, match="RUNTIME_COMPONENT_FAILURE"):
            await runtime._wait_for_stop_and_sync_activations(
                stop.wait,
                object(),
                interval_seconds=0.001,
            )

    asyncio.run(exercise())

    assert events == [
        (
            "responsibility_sync_failed",
            {
                "activation_id": "activation-demo",
                "reason": "RuntimeError",
            },
        ),
        (
            "runtime_component_failure_latched",
            {
                "component_event": "responsibility_sync_failed",
                "activation_id": "activation-demo",
                "reason": "RuntimeError",
            },
        ),
        ("venue_mutations_disabled_for_maintenance_stop", {}),
    ]
    assert disabled >= 1


def test_account_observer_retries_network_failure_and_recovers_in_place() -> None:
    stop = threading.Event()
    events: list[tuple[str, dict[str, object]]] = []
    attempts = 0

    class Observer:
        @staticmethod
        async def observe():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise AccountObservationError(
                    "ACCOUNT_SNAPSHOT_QUERY_FAILED_OSERROR",
                    retryable=True,
                )
            stop.set()
            return SimpleNamespace(
                venue_fact_id="account-fact-recovered",
                cutoff=datetime(2026, 8, 3, tzinfo=UTC),
                payload={"open_position_count": 2},
            )

    runtime = object.__new__(ProductExecutorRuntime)
    runtime._account_observer = Observer()
    runtime._connection = None
    runtime._fatal_component_failure = None
    runtime._runtime_event_sink = lambda event, fields: events.append(
        (event, fields)
    )

    async def exercise() -> None:
        runtime._loop = asyncio.get_running_loop()
        await runtime._observe_account_until_stop(
            stop.wait,
            interval_seconds=0.001,
        )

    asyncio.run(exercise())

    assert attempts == 2
    assert events == [
        (
            "account_snapshot_refresh_failed",
            {
                "reason_code": "ACCOUNT_SNAPSHOT_QUERY_FAILED_OSERROR",
                "retry_after_seconds": 0.001,
            },
        ),
        (
            "account_snapshot_refreshed",
            {
                "venue_fact_id": "account-fact-recovered",
                "cutoff": "2026-08-03T00:00:00+00:00",
                "open_position_count": 2,
            },
        ),
    ]


def test_account_observation_failure_persists_a_safe_latest_reason() -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Connection:
        @staticmethod
        def execute(query: str, parameters: tuple[object, ...]) -> None:
            calls.append((query, parameters))

    runtime = object.__new__(ProductExecutorRuntime)
    runtime._connection = Connection()
    runtime._settings = SimpleNamespace(
        release=SimpleNamespace(
            environment_id="demo-main",
            account_id="demo-account",
        )
    )
    runtime._runtime_event_sink = None

    runtime._record_account_observation_failure(
        reason_code="ACCOUNT_SNAPSHOT_QUERY_FAILED_HTTPERROR",
        retry_after_seconds=30.0,
    )

    assert len(calls) == 1
    query, parameters = calls[0]
    assert "INSERT INTO halpha.account_observation_failure" in query
    assert "ON CONFLICT (environment_id, account_ref) DO UPDATE" in query
    assert parameters[:2] == ("demo-main", "demo-account")
    assert parameters[3:] == (
        "ACCOUNT_SNAPSHOT_QUERY_FAILED_HTTPERROR",
        30.0,
    )


def test_account_observation_failure_status_is_non_authoritative() -> None:
    events: list[tuple[str, dict[str, object]]] = []

    class BrokenConnection:
        @staticmethod
        def execute(_query: str, _parameters: tuple[object, ...]) -> None:
            raise RuntimeError("diagnostic table unavailable")

    runtime = object.__new__(ProductExecutorRuntime)
    runtime._connection = BrokenConnection()
    runtime._settings = SimpleNamespace(
        release=SimpleNamespace(
            environment_id="demo-main",
            account_id="demo-account",
        )
    )
    runtime._runtime_event_sink = lambda event, fields: events.append((event, fields))

    runtime._record_account_observation_failure(
        reason_code="ACCOUNT_SNAPSHOT_QUERY_FAILED_HTTPERROR",
        retry_after_seconds=None,
    )

    assert events == [
        (
            "account_snapshot_failure_status_persist_failed",
            {"reason": "RuntimeError"},
        )
    ]


def test_account_observer_stops_on_nonretryable_persistence_failure() -> None:
    stop = threading.Event()
    events: list[tuple[str, dict[str, object]]] = []

    class Observer:
        @staticmethod
        async def observe():
            raise AccountObservationError(
                "ACCOUNT_SNAPSHOT_PERSIST_FAILED_OPERATIONALERROR"
            )

    runtime = object.__new__(ProductExecutorRuntime)
    runtime._account_observer = Observer()
    runtime._connection = None
    runtime._fatal_component_failure = None
    runtime._runtime_event_sink = lambda event, fields: events.append(
        (event, fields)
    )

    async def exercise() -> None:
        runtime._loop = asyncio.get_running_loop()
        try:
            with pytest.raises(
                ExecutorRuntimeError,
                match="RUNTIME_COMPONENT_FAILURE",
            ):
                await runtime._observe_account_until_stop(
                    stop.wait,
                    interval_seconds=0.001,
                )
        finally:
            stop.set()

    asyncio.run(exercise())

    assert events == [
        (
            "account_snapshot_refresh_failed",
            {
                "activation_id": "account-observer",
                "reason": "AccountObservationError",
                "reason_code": "ACCOUNT_SNAPSHOT_PERSIST_FAILED_OPERATIONALERROR",
            },
        ),
        (
            "runtime_component_failure_latched",
            {
                "component_event": "account_snapshot_refresh_failed",
                "activation_id": "account-observer",
                "reason": "AccountObservationError",
                "reason_code": "ACCOUNT_SNAPSHOT_PERSIST_FAILED_OPERATIONALERROR",
            },
        ),
    ]


def test_expected_startup_fact_failure_is_deferred_to_periodic_reconciliation() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    resumed: list[str] = []

    class FailingProcessor:
        @staticmethod
        def resume(activation_id: str) -> None:
            resumed.append(activation_id)

        @staticmethod
        async def wait_idle() -> None:
            raise ProductPreSubmitRejected("POSITION_ATTRIBUTION_UNKNOWN")

    runtime = object.__new__(ProductExecutorRuntime)
    runtime._runtime_event_sink = lambda event, fields: events.append((event, fields))
    runtime._fatal_component_failure = None

    asyncio.run(
        runtime._resume_startup_processors(
            {"activation-demo": FailingProcessor()},
            failure_event="startup_responsibility_deferred",
        )
    )

    assert resumed == ["activation-demo"]
    assert events == [
        (
            "startup_responsibility_deferred",
            {
                "activation_id": "activation-demo",
                "reason": "ProductPreSubmitRejected",
                "reason_code": "POSITION_ATTRIBUTION_UNKNOWN",
            },
        )
    ]


def test_runtime_failure_logs_only_stable_reason_codes() -> None:
    assert _stable_failure_reason_code(
        ValueError("POSITION_ATTRIBUTION_UNKNOWN")
    ) == {"reason_code": "POSITION_ATTRIBUTION_UNKNOWN"}
    assert _stable_failure_reason_code(
        RuntimeError("private diagnostic detail")
    ) == {}


def test_venue_event_reason_code_classifies_without_exposing_raw_text() -> None:
    assert _venue_event_reason_code(
        SimpleNamespace(reason="Request timeout; Binance code -1007")
    ) == "BINANCE_ERROR_1007"
    assert _venue_event_reason_code(
        SimpleNamespace(reason="<title>502 Bad Gateway</title>")
    ) == "VENUE_HTTP_TRANSIENT"
    assert _venue_event_reason_code(
        SimpleNamespace(reason="account-specific rejection detail")
    ) == "VENUE_REJECTION_OTHER"


def test_demo_runtime_closes_an_expired_empty_activation_before_wiring(
    monkeypatch,
) -> None:
    state = {"closed": False}
    activation = SimpleNamespace(
        activation_id="activation-demo-expired",
        lifecycle=PlanLifecycle.RUNNING,
        entry_opportunity_consumed=False,
        rule_state={"deadlines": {"entry_valid_until": "2026-07-19T00:00:00+00:00"}},
    )

    class FakePlanning:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def list_open_activations():
            return () if state["closed"] else (activation,)

    class FakeCoordinator:
        @staticmethod
        def expire_empty_entry_window(**values):
            assert values["activation_id"] == activation.activation_id
            state["closed"] = True

    monkeypatch.setattr(
        runtime_module,
        "PostgreSQLPlanningRepository",
        FakePlanning,
    )
    runtime = object.__new__(ProductExecutorRuntime)
    runtime._connection = object()
    runtime._lifecycle = SimpleNamespace(activation_ids=())
    runtime._settings = SimpleNamespace(
        release=SimpleNamespace(
            profile="BINANCE_DEMO",
            environment_id="demo-main",
        )
    )
    runtime._coordinator = FakeCoordinator()
    runtime._proposal_processors = {}
    runtime._responsibility_processors = {}

    runtime._restore_paused_adapters(object())

    assert state["closed"] is True


def test_live_runtime_rejects_an_activation_outside_the_authorized_set(
    monkeypatch,
) -> None:
    activation = SimpleNamespace(
        activation_id="activation-live-unauthorized",
        lifecycle=PlanLifecycle.RUNNING,
        entry_opportunity_consumed=False,
        rule_state={"deadlines": {"entry_valid_until": "2026-07-19T00:00:00+00:00"}},
    )

    class FakePlanning:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def list_open_activations():
            return (activation,)

    monkeypatch.setattr(
        runtime_module,
        "PostgreSQLPlanningRepository",
        FakePlanning,
    )
    runtime = object.__new__(ProductExecutorRuntime)
    runtime._connection = object()
    runtime._lifecycle = SimpleNamespace(activation_ids=())
    runtime._settings = SimpleNamespace(
        release=SimpleNamespace(
            profile="BINANCE_LIVE_WRITE",
            environment_id="live-main",
        )
    )
    runtime._live_write_activation_ids = frozenset({"activation-live-authorized"})
    runtime._coordinator = SimpleNamespace(
        expire_empty_entry_window=lambda **_values: pytest.fail(
            "unauthorized activation must not reach lifecycle mutation"
        )
    )
    runtime._proposal_processors = {}
    runtime._responsibility_processors = {}

    with pytest.raises(
        ExecutorRuntimeError,
        match="LIVE_WRITE_ACTIVATION_SET_MISMATCH",
    ):
        runtime._restore_paused_adapters(object())


def test_runtime_keeps_user_takeover_identity_for_read_only_reconciliation(
    monkeypatch,
) -> None:
    activation = SimpleNamespace(
        activation_id="activation-takeover",
        lifecycle=PlanLifecycle.USER_TAKEOVER,
        entry_opportunity_consumed=True,
        rule_state={},
    )
    inventory_reads: list[str] = []
    takeover_calls: list[str] = []

    class FakePlanning:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def list_runtime_responsibility_activations():
            inventory_reads.append("runtime")
            return (activation,)

        @staticmethod
        def list_open_activations():
            raise AssertionError("takeover must use the responsibility inventory")

    monkeypatch.setattr(
        runtime_module,
        "PostgreSQLPlanningRepository",
        FakePlanning,
    )
    runtime = object.__new__(ProductExecutorRuntime)
    runtime._connection = object()
    runtime._lifecycle = SimpleNamespace(activation_ids=(activation.activation_id,))
    runtime._settings = SimpleNamespace(
        release=SimpleNamespace(
            profile="BINANCE_DEMO",
            environment_id="demo-main",
        )
    )
    runtime._coordinator = SimpleNamespace(
        apply_persisted_user_takeover=lambda **values: takeover_calls.append(
            values["activation_id"]
        )
    )
    runtime._proposal_processors = {}
    runtime._responsibility_processors = {}

    runtime._restore_paused_adapters(object())

    assert inventory_reads == ["runtime", "runtime"]
    assert takeover_calls == [activation.activation_id]


def test_product_activation_handoff_defers_warmup_until_old_adapter_is_removed(
    monkeypatch,
) -> None:
    new_activation = SimpleNamespace(
        activation_id="activation-new",
        lifecycle=PlanLifecycle.RUNNING,
        entry_opportunity_consumed=False,
        rule_state={"deadlines": {"entry_valid_until": "2099-07-20T12:30:00+00:00"}},
    )
    version_reads: list[str] = []

    class FakePlanning:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def list_open_activations():
            return (new_activation,)

        @staticmethod
        def get_version(plan_version_ref):
            version_reads.append(plan_version_ref)
            raise AssertionError("new warmup must wait for the next sync cycle")

    class FakeLifecycle:
        activation_ids = ("activation-old",)

        def __init__(self):
            self.removed: list[str] = []

        def stop_and_remove(self, activation_id):
            self.removed.append(activation_id)

    monkeypatch.setattr(
        runtime_module,
        "PostgreSQLPlanningRepository",
        FakePlanning,
    )
    lifecycle = FakeLifecycle()
    runtime = object.__new__(ProductExecutorRuntime)
    runtime._connection = object()
    runtime._lifecycle = lifecycle
    runtime._settings = SimpleNamespace(
        release=SimpleNamespace(
            profile="BINANCE_DEMO",
            environment_id="demo-main",
        )
    )
    runtime._coordinator = SimpleNamespace()
    runtime._proposal_processors = {}
    runtime._responsibility_processors = {}

    runtime._restore_paused_adapters(object())

    assert lifecycle.removed == ["activation-old"]
    assert version_reads == []


def test_position_alignment_wires_only_the_reduce_only_responsibility_adapter(
    monkeypatch,
) -> None:
    activation = SimpleNamespace(
        activation_id="activation-position-alignment",
        plan_version_ref="plan-version-position-alignment",
        instrument_ref="SOLUSDT-PERP",
        decision_basis_ref=DIRECT_EXECUTION_REF,
        position_alignment=SimpleNamespace(operation="REDUCE"),
        lifecycle=PlanLifecycle.EXITING,
        entry_opportunity_consumed=True,
        rule_state={"deadlines": {}},
    )

    class FakePlanning:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def list_runtime_responsibility_activations():
            return (activation,)

        @staticmethod
        def list_open_activations():
            raise AssertionError("runtime responsibility inventory must be used")

    monkeypatch.setattr(
        runtime_module,
        "PostgreSQLPlanningRepository",
        FakePlanning,
    )
    wired: list[tuple[object, object]] = []
    runtime = object.__new__(ProductExecutorRuntime)
    runtime._connection = object()
    runtime._lifecycle = SimpleNamespace(activation_ids=())
    runtime._settings = SimpleNamespace(
        release=SimpleNamespace(
            profile="BINANCE_DEMO",
            environment_id="demo-main",
        )
    )
    runtime._coordinator = SimpleNamespace(
        expire_empty_entry_window=lambda **_values: pytest.fail(
            "an exiting alignment must not enter the entry-expiry path"
        )
    )
    runtime._proposal_processors = {}
    runtime._responsibility_processors = {}
    runtime._direct_schedule_processors = {}
    runtime._start_position_alignment_adapter = (
        lambda item, capability: wired.append((item, capability))
    )
    runtime._start_direct_execution_adapter = lambda *_args: pytest.fail(
        "alignment must not use the new-entry direct schedule adapter"
    )

    capability = object()
    runtime._restore_paused_adapters(capability)

    assert wired == [(activation, capability)]


def test_product_open_activation_wires_warmup_proposal_and_event_path(
    monkeypatch,
) -> None:
    activation = SimpleNamespace(
        activation_id="activation-product-wiring",
        plan_version_ref="plan-version-product-wiring",
        instrument_ref="BTCUSDT-PERP",
        direction=Direction.LONG,
        entry_opportunity_consumed=False,
        lifecycle=SimpleNamespace(value="RUNNING"),
        run_state=SimpleNamespace(value="ACTIVE"),
        rule_state={"deadlines": {"entry_valid_until": "2026-07-19T00:00:00+00:00"}},
    )
    version = SimpleNamespace(
        valid_from=datetime(2026, 7, 18, tzinfo=UTC),
        valid_until=datetime(2026, 7, 19, tzinfo=UTC),
        strategy_basis=SimpleNamespace(
            normalized_parameters=OneShotParameters(
                direction=Direction.LONG
            ).model_dump(mode="json")
        ),
    )

    class FakePlanning:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def list_open_activations():
            return (activation,)

        @staticmethod
        def get_version(_plan_version_ref):
            return version

        @staticmethod
        def get_activation(_activation_id):
            return activation

    class FakeLifecycle:
        adapter = None

        @property
        def activation_ids(self):
            if self.adapter is None:
                return ()
            return (self.adapter.activation_id,)

        def start(self, spec):
            self.adapter = spec.factory()
            return self.adapter

        def stop_and_remove(self, _activation_id):
            self.adapter = None

    class FakeCoordinator:
        @staticmethod
        def account_instrument_attribution(_activation_id):
            raise AssertionError("attribution is not loaded during adapter wiring")

        @staticmethod
        def build_nautilus_event_normalizer(**_kwargs):
            return object()

        @staticmethod
        def handle_nautilus_order_event(*_args, **_kwargs):
            return None

    monkeypatch.setattr(
        runtime_module,
        "PostgreSQLPlanningRepository",
        FakePlanning,
    )
    lifecycle = FakeLifecycle()
    loop = asyncio.new_event_loop()
    runtime = object.__new__(ProductExecutorRuntime)
    runtime._connection = object()
    runtime._lifecycle = lifecycle
    runtime._settings = SimpleNamespace(
        release=SimpleNamespace(
            profile="BINANCE_DEMO",
            environment_id="demo-main",
            authority_class="DEMO_VALIDATION",
            account_id="demo-owner",
        )
    )
    runtime._api_key = SecretStr("qualification-key")
    runtime._api_secret = SecretStr("qualification-secret")
    runtime._proxy_url = None
    runtime._loop = loop
    observed_venues: list[Venue] = []

    class FakeCache:
        @staticmethod
        def instrument(_instrument_id):
            return object()

        @staticmethod
        def account_for_venue(venue):
            observed_venues.append(venue)
            return None

    runtime._node = SimpleNamespace(
        cache=FakeCache(),
        kernel=SimpleNamespace(clock=SimpleNamespace(timestamp_ns=lambda: 0)),
    )
    runtime._coordinator = FakeCoordinator()
    runtime._proposal_processors = {}
    runtime._responsibility_processors = {}
    runtime_events: list[tuple[str, dict[str, object]]] = []
    runtime._runtime_event_sink = lambda event, fields: runtime_events.append(
        (event, fields)
    )
    try:
        runtime._restore_paused_adapters(object())
        adapter = lifecycle.adapter
        assert isinstance(adapter, HalphaStrategyAdapter)
        assert isinstance(adapter._bar_evaluator, NautilusBarEntryEvaluator)
        assert adapter._bar_evaluator.sizing_provider(object()) is None
        assert observed_venues == [Venue("BINANCE")]
        assert adapter._bar_evaluator.warmup_complete is False
        assert adapter._live_history_warmup is True
        assert adapter._quote_event_sink is not None
        assert adapter._mark_price_event_sink is not None
        assert adapter._bar_event_sink is not None
        assert adapter._bar_failure_sink is not None
        adapter._bar_event_sink(
            SimpleNamespace(bar_type="BTCUSDT-PERP-1-MINUTE-LAST-EXTERNAL", ts_event=1)
        )
        adapter._bar_failure_sink(
            SimpleNamespace(bar_type="BTCUSDT-PERP-1-MINUTE-LAST-EXTERNAL"),
            ProductPreSubmitRejected("POSITION_ATTRIBUTION_UNKNOWN"),
        )
        assert [event for event, _fields in runtime_events] == [
            "strategy_adapter_started",
            "entry_sizing_requested",
            "entry_sizing_unavailable",
            "strategy_bar_observed",
            "strategy_bar_failed",
        ]
        assert tuple(runtime._proposal_processors) == ("activation-product-wiring",)
        assert tuple(runtime._responsibility_processors) == (
            "activation-product-wiring",
        )
    finally:
        for processor in runtime._proposal_processors.values():
            processor.close()
        for processor in runtime._responsibility_processors.values():
            processor.close()
        loop.close()


def test_framework_callback_failure_is_latched_before_it_can_be_swallowed() -> None:
    disabled = 0
    events: list[tuple[str, dict[str, object]]] = []

    class Coordinator:
        @staticmethod
        def disable_venue_mutations() -> None:
            nonlocal disabled
            disabled += 1

    runtime = object.__new__(ProductExecutorRuntime)
    runtime._coordinator = Coordinator()
    runtime._fatal_component_failure = None
    runtime._runtime_event_sink = lambda event, fields: events.append(
        (event, fields)
    )

    def failing_sink(_event: object) -> None:
        raise RuntimeError("private callback detail")

    guarded = runtime._guard_component_sink(
        "strategy_execution_event_processing_failed",
        "activation-demo",
        failing_sink,
    )

    with pytest.raises(RuntimeError, match="private callback detail"):
        guarded(object())

    assert disabled == 1
    assert events == [
        (
            "strategy_execution_event_processing_failed",
            {"activation_id": "activation-demo", "reason": "RuntimeError"},
        ),
        (
            "runtime_component_failure_latched",
            {
                "component_event": "strategy_execution_event_processing_failed",
                "activation_id": "activation-demo",
                "reason": "RuntimeError",
            },
        ),
    ]
    with pytest.raises(ExecutorRuntimeError, match="RUNTIME_COMPONENT_FAILURE"):
        runtime._require_no_fatal_component_failure()


def test_direct_activation_uses_execution_adapter_without_strategy_basis(
    monkeypatch,
) -> None:
    cached_quote_at = datetime(2026, 7, 23, 7, 0, 0, 500000, tzinfo=UTC)
    cached_quote_at_ns = int(cached_quote_at.timestamp() * 1_000_000_000)
    activation = SimpleNamespace(
        activation_id="activation-direct-wiring",
        plan_version_ref="plan-version-direct-wiring",
        decision_basis_ref=DIRECT_EXECUTION_REF,
        instrument_ref="BTCUSDT-PERP",
        direction=Direction.LONG,
        environment_kind=EnvironmentKind.DEMO,
        order_schedule_snapshot=SimpleNamespace(
            instrument_rules=SimpleNamespace(
                source="BINANCE_DEMO_EXCHANGE_INFO",
            ),
            schedule_spec=SimpleNamespace(
                entry_conditions=SimpleNamespace(
                    items=(
                        SimpleNamespace(
                            kind=EntryConditionKind.CLOSED_BAR_PRICE_15M
                        ),
                    )
                )
            ),
        ),
        created_at=datetime(2026, 7, 23, 7, 0, tzinfo=UTC),
        entry_opportunity_consumed=False,
        lifecycle=PlanLifecycle.RUNNING,
        run_state=SimpleNamespace(value="ACTIVE"),
        rule_state={"deadlines": {"entry_valid_until": "2099-07-19T00:00:00+00:00"}},
    )
    version_reads: list[str] = []

    class FakePlanning:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def list_open_activations():
            return (activation,)

        @staticmethod
        def get_version(plan_version_ref):
            version_reads.append(plan_version_ref)
            raise AssertionError("direct execution must not read strategy basis")

    class FakeLifecycle:
        adapter = None

        @property
        def activation_ids(self):
            return () if self.adapter is None else (self.adapter.activation_id,)

        def start(self, spec):
            self.adapter = spec.factory()
            return self.adapter

    class FakeCoordinator:
        @staticmethod
        def account_instrument_attribution(_activation_id):
            raise AssertionError("attribution is not loaded during adapter wiring")

        @staticmethod
        def startup_recovery_allows_submission(_activation_id):
            return False

        @staticmethod
        def build_nautilus_event_normalizer(**_kwargs):
            return object()

        @staticmethod
        def handle_nautilus_order_event(_normalizer, event, **_kwargs):
            return SimpleNamespace(
                action=None,
                facts=getattr(event, "facts", ()),
            )

    monkeypatch.setattr(runtime_module, "PostgreSQLPlanningRepository", FakePlanning)
    lifecycle = FakeLifecycle()
    loop = asyncio.new_event_loop()
    runtime = object.__new__(ProductExecutorRuntime)
    runtime._connection = object()
    runtime._lifecycle = lifecycle
    runtime._settings = SimpleNamespace(
        release=SimpleNamespace(
            profile="BINANCE_DEMO",
            environment_id="demo-main",
            authority_class="DEMO_VALIDATION",
            account_id="demo-owner",
        )
    )
    runtime._api_key = SecretStr("qualification-key")
    runtime._api_secret = SecretStr("qualification-secret")
    runtime._proxy_url = None
    runtime._loop = loop
    runtime._node = SimpleNamespace(
        cache=SimpleNamespace(
            quote_tick=lambda instrument_id: SimpleNamespace(
                instrument_id=str(instrument_id),
                bid_price="98",
                ask_price="102",
                ts_event=cached_quote_at_ns,
                ts_init=cached_quote_at_ns,
            )
        )
    )
    runtime._coordinator = FakeCoordinator()
    runtime._proposal_processors = {}
    runtime._direct_schedule_processors = {}
    runtime._responsibility_processors = {}
    runtime._recovery_complete = False
    runtime._runtime_event_sink = None
    closed_bar_streams: list[tuple[str, LiveEntryFactTracker]] = []
    runtime._ensure_closed_bar_fact_stream = lambda instrument_ref, tracker: (
        closed_bar_streams.append((instrument_ref, tracker))
    )
    try:
        runtime._restore_paused_adapters(object())

        adapter = lifecycle.adapter
        assert isinstance(adapter, HalphaStrategyAdapter)
        assert adapter._logic is None
        assert adapter._state_provider is None
        assert adapter._proposal_sink is None
        assert adapter._bar_evaluator is None
        assert adapter._quote_event_sink is not None
        assert adapter._mark_price_event_sink is not None
        assert len(closed_bar_streams) == 1
        assert closed_bar_streams[0][0] == "BTCUSDT-PERP"
        assert closed_bar_streams[0][1].target_bar_type == (
            runtime._market_fact_trackers["BTCUSDT-PERP"].target_bar_type
        )
        cutoff = datetime(2026, 7, 23, 7, 0, 1, tzinfo=UTC)
        cutoff_ns = int(cutoff.timestamp() * 1_000_000_000)
        cached_conditions = runtime._direct_schedule_processors[
            "activation-direct-wiring"
        ]._condition_fact_provider(activation, cutoff_ns, cutoff, {})
        assert cached_conditions.mark_price is None
        assert cached_conditions.bid_price == "98"
        assert cached_conditions.ask_price == "102"
        adapter._quote_event_sink(
            SimpleNamespace(
                instrument_id="BTCUSDT-PERP.BINANCE",
                bid_price="99",
                ask_price="101",
                ts_event=cutoff_ns,
            )
        )
        adapter._mark_price_event_sink(
            SimpleNamespace(
                instrument_id="BTCUSDT-PERP.BINANCE",
                value="100",
                ts_event=cutoff_ns,
            )
        )
        direct_conditions = runtime._direct_schedule_processors[
            "activation-direct-wiring"
        ]._condition_fact_provider(activation, cutoff_ns, cutoff, {})
        assert direct_conditions.mark_price == "100"
        assert direct_conditions.bid_price == "99"
        assert direct_conditions.ask_price == "101"
        direct_resumes: list[tuple[str, bool]] = []
        runtime._direct_schedule_processors[
            "activation-direct-wiring"
        ].resume = lambda activation_id, *, force_risk_refresh=False: (
            direct_resumes.append((activation_id, force_risk_refresh))
        )
        assert adapter._execution_event_sink is not None
        adapter._execution_event_sink(
            SimpleNamespace(
                client_order_id=None,
                facts=(SimpleNamespace(kind=VenueFactKind.FILL),),
            )
        )
        adapter._execution_event_sink(
            SimpleNamespace(
                client_order_id=None,
                facts=(SimpleNamespace(kind=VenueFactKind.ORDER_STATE),),
            )
        )
        assert direct_resumes == [
            ("activation-direct-wiring", True),
            ("activation-direct-wiring", False),
        ]
        assert adapter.live_history_ready is True
        assert version_reads == []
        assert tuple(runtime._proposal_processors) == ()
        assert tuple(runtime._direct_schedule_processors) == (
            "activation-direct-wiring",
        )
        assert tuple(runtime._responsibility_processors) == (
            "activation-direct-wiring",
        )
    finally:
        for processor in runtime._direct_schedule_processors.values():
            processor.close()
        for processor in runtime._responsibility_processors.values():
            processor.close()
        loop.close()


def test_market_fact_streams_keep_quotes_warm_and_resume_only_matching_direct() -> (
    None
):
    class FakeLifecycle:
        def __init__(self) -> None:
            self.adapters = {}

        def start(self, spec):
            adapter = spec.factory()
            self.adapters[spec.activation_id] = adapter
            return adapter

    resumes: list[tuple[str, str]] = []

    class FakeDirect:
        def __init__(self, label: str) -> None:
            self.label = label

        def resume(self, activation_id: str) -> None:
            resumes.append((self.label, activation_id))

    runtime = object.__new__(ProductExecutorRuntime)
    runtime._settings = SimpleNamespace(
        release=SimpleNamespace(profile="BINANCE_DEMO")
    )
    runtime._market_fact_lifecycle = FakeLifecycle()
    runtime._market_fact_trackers = {}
    runtime._direct_schedule_processors = {
        "btc-activation": FakeDirect("btc"),
        "eth-activation": FakeDirect("eth"),
    }
    runtime._direct_schedule_instruments = {
        "btc-activation": "BTCUSDT-PERP",
        "eth-activation": "ETHUSDT-PERP",
    }

    runtime._start_market_fact_streams()

    assert tuple(runtime._market_fact_lifecycle.adapters) == (
        "market-facts:BTCUSDT-PERP",
        "market-facts:ETHUSDT-PERP",
    )
    observed_at = datetime(2026, 7, 23, 7, 0, 1, tzinfo=UTC)
    observed_at_ns = int(observed_at.timestamp() * 1_000_000_000)
    btc_adapter = runtime._market_fact_lifecycle.adapters[
        "market-facts:BTCUSDT-PERP"
    ]
    btc_adapter._quote_event_sink(
        SimpleNamespace(
            instrument_id="BTCUSDT-PERP.BINANCE",
            bid_price="98",
            ask_price="102",
            ts_event=observed_at_ns,
            ts_init=observed_at_ns,
        )
    )
    btc_adapter._mark_price_event_sink(
        SimpleNamespace(
            instrument_id="BTCUSDT-PERP.BINANCE",
            value="100",
            ts_event=observed_at_ns,
            ts_init=observed_at_ns,
        )
    )

    facts = runtime._market_fact_trackers[
        "BTCUSDT-PERP"
    ].direct_condition_facts(
        "BTCUSDT-PERP.BINANCE",
        cutoff_ns=observed_at_ns,
        observed_at=observed_at,
        activated_at=observed_at,
        price_move_bps_by_window={},
        market_source="BINANCE_DEMO_PUBLIC",
    )
    assert facts.bid_price == "98"
    assert facts.ask_price == "102"
    assert facts.mark_price == "100"
    assert resumes == [("btc", "btc-activation")]


def test_closed_bar_fact_stream_is_shared_warmed_and_resumes_matching_direct() -> (
    None
):
    class FakeLifecycle:
        def __init__(self) -> None:
            self.adapters = {}

        @property
        def activation_ids(self):
            return tuple(self.adapters)

        def start(self, spec):
            adapter = spec.factory()
            self.adapters[spec.activation_id] = adapter
            return adapter

    resumes: list[str] = []
    runtime = object.__new__(ProductExecutorRuntime)
    runtime._market_fact_lifecycle = FakeLifecycle()
    runtime._direct_schedule_processors = {
        "btc-activation": SimpleNamespace(
            resume=lambda activation_id: resumes.append(activation_id)
        )
    }
    runtime._direct_schedule_instruments = {
        "btc-activation": "BTCUSDT-PERP",
    }
    runtime._runtime_event_sink = None
    tracker = LiveEntryFactTracker("BTCUSDT-PERP")
    target = tracker.target_bar_type

    def bar(close_at: datetime, close: str) -> Bar:
        timestamp = int(close_at.timestamp() * 1_000_000_000)
        return Bar(
            bar_type=target,
            open=Price.from_str(close),
            high=Price.from_str(close),
            low=Price.from_str(close),
            close=Price.from_str(close),
            volume=Quantity.from_str("1"),
            ts_event=timestamp,
            ts_init=timestamp + 1_000_000,
        )

    runtime._ensure_closed_bar_fact_stream("BTCUSDT-PERP", tracker)
    runtime._ensure_closed_bar_fact_stream("BTCUSDT-PERP", tracker)

    assert tuple(runtime._market_fact_lifecycle.adapters) == (
        "closed-bar-facts:BTCUSDT-PERP",
    )
    first_close_at = datetime(2026, 8, 1, 6, 0, tzinfo=UTC)
    tracker.try_warm_from_cached_bars(
        target_bars=(bar(first_close_at, "62960"),),
    )
    adapter = runtime._market_fact_lifecycle.adapters[
        "closed-bar-facts:BTCUSDT-PERP"
    ]
    next_close_at = first_close_at + timedelta(minutes=15)
    adapter.on_bar(bar(next_close_at, "62940"))

    assert resumes == ["btc-activation"]
    facts = tracker.direct_condition_facts(
        "BTCUSDT-PERP.BINANCE",
        cutoff_ns=int(
            (next_close_at + timedelta(minutes=1)).timestamp() * 1_000_000_000
        ),
        observed_at=next_close_at + timedelta(minutes=1),
        activated_at=first_close_at,
        price_move_bps_by_window={},
        market_source="BINANCE_DEMO_PUBLIC",
    )
    assert facts.closed_bar_15m_close == "62940"


def test_direct_adapter_recovery_rejects_cross_environment_rules_source() -> None:
    runtime = object.__new__(ProductExecutorRuntime)
    runtime._lifecycle = SimpleNamespace()
    runtime._settings = SimpleNamespace(release=SimpleNamespace(profile="BINANCE_DEMO"))
    activation = SimpleNamespace(
        environment_kind=EnvironmentKind.DEMO,
        order_schedule_snapshot=SimpleNamespace(
            instrument_rules=SimpleNamespace(
                source="BINANCE_LIVE_EXCHANGE_INFO",
            )
        ),
    )

    with pytest.raises(
        ExecutorRuntimeError,
        match="INSTRUMENT_RULES_SOURCE_ENVIRONMENT_MISMATCH",
    ):
        runtime._start_direct_execution_adapter(activation, object())
