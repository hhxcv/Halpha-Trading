from threading import Event

from halpha.executor.__main__ import (
    _RuntimeLivenessWatchdog,
    _runtime_build_failure_log_fields,
    _runtime_ready_log_fields,
    _signal_runtime_failure_stop,
    _start_failure_cleanup_watchdog,
)
from halpha.executor.runtime import ExecutorRuntimeError


def test_runtime_liveness_watchdog_forces_recovery_after_heartbeat_stalls() -> None:
    exited = Event()
    exit_codes: list[int] = []
    watchdog = _RuntimeLivenessWatchdog(
        timeout_seconds=0.02,
        poll_seconds=0.001,
        hard_exit=lambda code: (exit_codes.append(code), exited.set()),
    )

    watchdog.start()
    try:
        assert exited.wait(timeout=1)
    finally:
        watchdog.close()

    assert exit_codes == [3]


def test_runtime_liveness_watchdog_accepts_progress_and_closes_cleanly() -> None:
    exited = Event()
    tick = Event()
    watchdog = _RuntimeLivenessWatchdog(
        timeout_seconds=0.05,
        poll_seconds=0.001,
        hard_exit=lambda _code: exited.set(),
    )

    watchdog.start()
    for _ in range(5):
        assert not tick.wait(timeout=0.005)
        watchdog.heartbeat()
    watchdog.close()

    assert not exited.is_set()


def test_runtime_ready_log_fields_accept_private_observer_profile_evidence() -> None:
    fields = _runtime_ready_log_fields(
        profile="BINANCE_LIVE_READ_ONLY",
        environment_id="binance-usdm-copy-lead-live",
        paused_open_activations=0,
        proxy_supplied=True,
        product_build_id="build-123",
        runtime_evidence={
            "profile": "BINANCE_LIVE_READ_ONLY",
            "read_only_mode": "PRIVATE_ACCOUNT_OBSERVATION",
            "account_observer_started": True,
        },
    )

    assert fields == {
        "profile": "BINANCE_LIVE_READ_ONLY",
        "environment_id": "binance-usdm-copy-lead-live",
        "paused_open_activations": 0,
        "proxy_supplied": True,
        "product_build_id": "build-123",
        "read_only_mode": "PRIVATE_ACCOUNT_OBSERVATION",
        "account_observer_started": True,
    }


def test_runtime_build_failure_log_fields_preserve_only_stable_codes() -> None:
    assert _runtime_build_failure_log_fields(
        ExecutorRuntimeError("DATABASE_SCHEMA_NOT_CURRENT")
    ) == {
        "exception_type": "ExecutorRuntimeError",
        "reason_code": "DATABASE_SCHEMA_NOT_CURRENT",
    }
    assert _runtime_build_failure_log_fields(RuntimeError("secret=value")) == {
        "exception_type": "RuntimeError",
        "reason_code": "PRODUCT_RUNTIME_BUILD_FAILED type=RuntimeError",
    }


def test_failure_cleanup_watchdog_forces_process_exit_after_timeout() -> None:
    exited = Event()
    exit_codes: list[int] = []

    def fake_exit(code: int) -> None:
        exit_codes.append(code)
        exited.set()

    watchdog = _start_failure_cleanup_watchdog(
        timeout_seconds=0.01,
        hard_exit=fake_exit,
    )

    assert exited.wait(timeout=1)
    watchdog.join(timeout=1)
    assert exit_codes == [2]


def test_failure_cleanup_watchdog_can_be_cancelled_after_clean_shutdown() -> None:
    exited = Event()
    watchdog = _start_failure_cleanup_watchdog(
        timeout_seconds=0.05,
        hard_exit=lambda _code: exited.set(),
    )

    watchdog.cancel()
    watchdog.join(timeout=1)

    assert not exited.is_set()


def test_runtime_failure_signals_the_blocked_stop_waiter() -> None:
    signaled = Event()
    stop_event = type("StopEvent", (), {"signal": lambda _self: signaled.set()})()

    assert _signal_runtime_failure_stop(stop_event) is True
    assert signaled.is_set()


def test_runtime_failure_stop_signal_failure_keeps_watchdog_fallback_required() -> None:
    class StopEvent:
        @staticmethod
        def signal() -> None:
            raise OSError("closed handle")

    assert _signal_runtime_failure_stop(StopEvent()) is False
