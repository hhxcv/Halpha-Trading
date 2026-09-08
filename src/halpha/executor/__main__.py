"""Entry point for the Executor process role."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
from threading import Event, Lock, Thread, Timer, current_thread
from time import monotonic
from typing import Callable, Sequence

import keyring
import psycopg

from halpha.configuration import (
    ConfigurationError,
    executor_settings,
    forward_observation_directory,
    load_settings,
    runtime_log_directory,
    settings_digest,
)
from halpha.executor.continuity import (
    ExecutorContinuityUnavailable,
    PostgreSQLExecutorContinuityGuard,
)
from halpha.executor.forward_observation import (
    ForwardObservationError,
    ForwardObservationEvidence,
    load_forward_observation_spec,
    require_forward_observation_source_identity,
)
from halpha.executor.runtime import (
    ExecutorRuntimeError,
    ProductExecutorRuntime,
    _connect_product_database,
    query_execution_hedge_mode,
)
from halpha.live_write_gate import (
    LiveWriteGateError,
    evaluate_live_write_gate,
    require_live_write_credential_binding,
    require_live_write_gate_open,
    require_live_write_gate_startup,
    require_live_write_gate_startup_precheck,
)
from halpha.operational_logging import configure_halpha_logging
from halpha.process_contract import ProcessRole, preflight
from halpha.product_build import calculate_product_build_id
from halpha.runtime_identity import RuntimeIdentityError, repository_root
from halpha.source_identity import (
    SourceIdentityError,
)
from halpha.venue_account_qualification import (
    LiveVenueAccountQualifier,
    VenueAccountQualificationError,
    require_live_venue_symbols,
)
from halpha.winvault import SecretResolutionError, executor_secret_resolver
from halpha.windows_filesystem import (
    WindowsFilesystemError,
    assert_directory_security,
    runtime_log_acl_spec,
)
from halpha.windows_runtime import (
    WindowsRuntimeError,
    acquire_executor_mutex,
    create_stop_event,
    require_process_identity,
)

_FAILURE_CLEANUP_TIMEOUT_SECONDS = 15.0
_RUNTIME_LIVENESS_TIMEOUT_SECONDS = 60.0
_RUNTIME_LIVENESS_POLL_SECONDS = 1.0


class _RuntimeLivenessWatchdog:
    """Force recovery when the trading event loop stops making progress."""

    def __init__(
        self,
        *,
        timeout_seconds: float = _RUNTIME_LIVENESS_TIMEOUT_SECONDS,
        poll_seconds: float = _RUNTIME_LIVENESS_POLL_SECONDS,
        hard_exit: Callable[[int], object] = os._exit,
        clock: Callable[[], float] = monotonic,
        on_timeout: Callable[[float], object] | None = None,
    ) -> None:
        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("RUNTIME_LIVENESS_INTERVAL_INVALID")
        self._timeout_seconds = timeout_seconds
        self._poll_seconds = min(poll_seconds, timeout_seconds)
        self._hard_exit = hard_exit
        self._clock = clock
        self._on_timeout = on_timeout
        self._lock = Lock()
        self._closed = Event()
        self._last_heartbeat = clock()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("RUNTIME_LIVENESS_WATCHDOG_ALREADY_STARTED")
        self.heartbeat()
        thread = Thread(
            target=self._watch,
            name="halpha-executor-liveness",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def heartbeat(self) -> None:
        with self._lock:
            self._last_heartbeat = self._clock()

    def _watch(self) -> None:
        while not self._closed.wait(self._poll_seconds):
            now = self._clock()
            with self._lock:
                elapsed = max(0.0, now - self._last_heartbeat)
            if elapsed < self._timeout_seconds:
                continue
            if self._on_timeout is not None:
                try:
                    self._on_timeout(elapsed)
                except Exception:
                    pass
            self._hard_exit(3)
            return

    def close(self) -> None:
        self._closed.set()
        thread = self._thread
        if thread is not None and thread is not current_thread():
            thread.join(timeout=self._poll_seconds + 1.0)


def _start_failure_cleanup_watchdog(
    *,
    timeout_seconds: float = _FAILURE_CLEANUP_TIMEOUT_SECONDS,
    hard_exit: Callable[[int], object] = os._exit,
) -> Timer:
    """Bound cleanup after an unexpected runtime failure.

    Venue mutations have already been disabled before this is armed.  A hard
    process exit lets the existing task trigger restart a failed Executor
    instead of leaving an alive process without a database-ready session.
    """

    watchdog = Timer(timeout_seconds, hard_exit, args=(2,))
    watchdog.daemon = True
    watchdog.start()
    return watchdog


def _signal_runtime_failure_stop(stop_event: object) -> bool:
    """Wake the stop waiter so its executor thread cannot outlive cleanup."""

    signal = getattr(stop_event, "signal", None)
    if not callable(signal):
        return False
    try:
        signal()
    except Exception:
        return False
    return True


def _runtime_ready_status(
    profile: str,
    *,
    risk_control_only: bool = False,
    recovery_complete: bool = True,
) -> str:
    if profile == "BINANCE_LIVE_WRITE":
        if not recovery_complete:
            return (
                "LIVE_WRITE_RISK_CONTROL_RECOVERY_PENDING"
                if risk_control_only
                else "LIVE_WRITE_RECOVERY_PENDING"
            )
        if risk_control_only:
            return "LIVE_WRITE_RISK_CONTROL_ONLY_READY"
    try:
        return {
            "BINANCE_DEMO": "DEMO_RUNTIME_READY",
            "BINANCE_LIVE_READ_ONLY": "READ_ONLY_RUNTIME_READY",
            "BINANCE_LIVE_WRITE": "LIVE_WRITE_RUNTIME_READY",
        }[profile]
    except KeyError:
        raise ExecutorRuntimeError("EXECUTION_PROFILE_MISMATCH") from None


def _runtime_ready_log_fields(
    *,
    profile: str,
    environment_id: str,
    paused_open_activations: int,
    proxy_supplied: bool,
    product_build_id: str,
    runtime_evidence: dict[str, object],
) -> dict[str, object]:
    """Merge runtime evidence into one collision-safe structured log payload."""

    return {
        "profile": profile,
        "environment_id": environment_id,
        "paused_open_activations": paused_open_activations,
        "proxy_supplied": proxy_supplied,
        "product_build_id": product_build_id,
        **runtime_evidence,
    }


def _runtime_build_failure_log_fields(exc: Exception) -> dict[str, str]:
    """Return a stable, credential-safe startup failure record.

    ``ProductExecutorRuntime.build`` deliberately sanitizes unexpected
    dependency failures before it crosses the process boundary.  Preserve that
    reason at the only durable startup log boundary so a scheduled restart is
    diagnosable without emitting exception text or configuration values.
    """

    reason_code = (
        str(exc)
        if isinstance(exc, ExecutorRuntimeError)
        else f"PRODUCT_RUNTIME_BUILD_FAILED type={type(exc).__name__}"
    )
    return {
        "exception_type": type(exc).__name__,
        "reason_code": reason_code,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=ProcessRole.EXECUTOR.value)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--forward-observation-spec", type=Path)
    parser.add_argument("--forward-observation-evidence", type=Path)
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
        repo_root = repository_root()
        report = preflight(ProcessRole.EXECUTOR, settings)
        if args.preflight_only:
            product_build_id = calculate_product_build_id(repo_root, settings)
            gate_status = evaluate_live_write_gate(
                repo_root,
                settings,
                current_product_build_id=product_build_id,
            )
            report.update(
                {
                    "product_build_id": gate_status.product_build_id,
                    "configured_runtime_real_write_gate": (
                        gate_status.configured_runtime_real_write_gate
                    ),
                    "runtime_real_write_gate": gate_status.runtime_real_write_gate,
                    "live_write_gate_violations": list(gate_status.violations),
                }
            )
            print(json.dumps(report, sort_keys=True))
            return 0
        role_settings = executor_settings(settings)
        product_build_id = calculate_product_build_id(repo_root, settings)
        read_only = settings.release.profile == "BINANCE_LIVE_READ_ONLY"
        private_account_observation = bool(
            read_only
            and role_settings.executor.binance_api_key_reference is not None
            and role_settings.executor.binance_api_secret_reference is not None
        )
        observation = None
        observation_spec = None
        observation_evidence_path = None
        if read_only and private_account_observation:
            if (
                args.forward_observation_spec is not None
                or args.forward_observation_evidence is not None
            ):
                raise ExecutorRuntimeError(
                    "READ_ONLY_OBSERVATION_MODES_CONFLICT"
                )
        elif read_only:
            if (
                args.forward_observation_spec is None
                or args.forward_observation_evidence is None
            ):
                raise ExecutorRuntimeError("READ_ONLY_OBSERVATION_ARGUMENTS_REQUIRED")
            spec_root = (
                repo_root / "build" / "evidence" / "reports"
            ).resolve()
            evidence_root = forward_observation_directory(
                repo_root,
                settings,
            ).resolve()
            spec_path = args.forward_observation_spec.resolve()
            evidence_path = args.forward_observation_evidence.resolve()
            try:
                spec_path.relative_to(spec_root)
                evidence_path.relative_to(evidence_root)
            except ValueError:
                raise ExecutorRuntimeError(
                    "READ_ONLY_OBSERVATION_PATH_OUTSIDE_ROLE_ROOT"
                ) from None
            if evidence_path.suffix.lower() != ".jsonl":
                raise ExecutorRuntimeError("READ_ONLY_OBSERVATION_EVIDENCE_FORMAT_INVALID")
            observation_spec = load_forward_observation_spec(spec_path)
            if observation_spec.configuration_digest != settings_digest(settings):
                raise ExecutorRuntimeError("READ_ONLY_OBSERVATION_CONFIGURATION_DRIFT")
            require_forward_observation_source_identity(
                repo_root,
                observation_spec,
            )
            observation_evidence_path = evidence_path
        elif (
            args.forward_observation_spec is not None
            or args.forward_observation_evidence is not None
        ):
            raise ExecutorRuntimeError("FORWARD_OBSERVATION_ARGUMENTS_PROFILE_MISMATCH")
        require_process_identity(role_settings.executor_task_sid)
        assert_directory_security(
            runtime_log_acl_spec(
                repo_root,
                settings,
                role="executor",
            )
        )
        if observation_spec is not None and observation_evidence_path is not None:
            observation = ForwardObservationEvidence(
                observation_spec,
                observation_evidence_path,
            )
        live_write = settings.release.profile == "BINANCE_LIVE_WRITE"
        gate_status = (
            require_live_write_gate_startup_precheck(
                repo_root,
                settings,
                current_product_build_id=product_build_id,
            )
            if live_write
            else evaluate_live_write_gate(
                repo_root,
                settings,
                current_product_build_id=product_build_id,
            )
        )
        with acquire_executor_mutex(
            name=role_settings.executor.mutex_name,
            task_sid=role_settings.executor_task_sid,
            maintenance_sid=role_settings.maintenance_sid,
        ), create_stop_event(
            name=role_settings.stop_event,
            task_sid=role_settings.executor_task_sid,
            maintenance_sid=role_settings.maintenance_sid,
        ) as stop_event:
            resolver = executor_secret_resolver(keyring.get_keyring(), role_settings)
            database_password = (
                None
                if read_only and not private_account_observation
                else resolver.resolve(
                    role_settings.executor.database_credential_reference
                )
            )
            live_write_submission_guard = None
            live_venue_account_qualifier = None
            if live_write:
                if database_password is None:
                    raise ExecutorRuntimeError("PRODUCT_DATABASE_CREDENTIAL_REQUIRED")
                gate_connection = _connect_product_database(
                    psycopg.connect,
                    database_name=settings.release.database_name,
                    password=database_password.get_secret_value(),
                )
                try:
                    gate_status = require_live_write_gate_startup(
                        repo_root,
                        settings,
                        gate_connection,
                        current_product_build_id=product_build_id,
                    )
                finally:
                    gate_connection.close()

                def live_write_submission_guard(activation_id: str) -> None:
                    current_connection = _connect_product_database(
                        psycopg.connect,
                        database_name=settings.release.database_name,
                        password=database_password.get_secret_value(),
                    )
                    try:
                        current_status = require_live_write_gate_open(
                            repo_root,
                            settings,
                            current_connection,
                            current_product_build_id=product_build_id,
                        )
                        require_live_write_credential_binding(
                            current_status,
                            api_key,
                        )
                        if activation_id not in current_status.authorized_activation_ids:
                            raise LiveWriteGateError(
                                "LIVE_WRITE_ACTIVATION_SCOPE_MISMATCH"
                            )
                        activation_row = current_connection.execute(
                            """
                            SELECT instrument_ref
                            FROM halpha.plan_activation
                            WHERE environment_id = %s
                              AND activation_id = %s
                              AND account_ref = %s
                            """,
                            (
                                settings.release.environment_id,
                                activation_id,
                                settings.release.account_id,
                            ),
                        ).fetchone()
                        if activation_row is None:
                            raise LiveWriteGateError(
                                "LIVE_WRITE_ACTIVATION_SCOPE_MISMATCH"
                            )
                        instrument_ref = str(activation_row[0])
                        symbol = instrument_ref.removesuffix("-PERP")
                        if not symbol or symbol == instrument_ref:
                            raise LiveWriteGateError(
                                "LIVE_WRITE_ACTIVATION_INSTRUMENT_INVALID"
                            )
                        if live_venue_account_qualifier is None:
                            raise VenueAccountQualificationError(
                                "VENUE_ACCOUNT_QUALIFICATION_UNAVAILABLE"
                            )
                        # Network refresh runs separately from the Nautilus
                        # event loop.  The final write boundary only validates
                        # the bounded cached evidence and therefore cannot
                        # stall order-event processing on SAPI latency.
                        current_account_facts = (
                            live_venue_account_qualifier.require_cached_current()
                        )
                        require_live_venue_symbols(
                            current_account_facts,
                            (symbol,),
                        )
                    finally:
                        current_connection.close()
            if read_only and not private_account_observation:
                api_key = None
                api_secret = None
            else:
                key_reference = role_settings.executor.binance_api_key_reference
                secret_reference = role_settings.executor.binance_api_secret_reference
                if key_reference is None or secret_reference is None:
                    raise ExecutorRuntimeError("BINANCE_CREDENTIAL_REFERENCE_REQUIRED")
                api_key = resolver.resolve(key_reference)
                api_secret = resolver.resolve(secret_reference)
                if live_write:
                    require_live_write_credential_binding(gate_status, api_key)
            proxy_reference = role_settings.executor.runtime_proxy_reference
            vault_proxy = (
                resolver.resolve(proxy_reference) if proxy_reference is not None else None
            )
            environment_proxy = os.environ.get("HALPHA_RUNTIME_PROXY_URL")
            if (
                vault_proxy is not None
                and environment_proxy is not None
                and vault_proxy.get_secret_value() != environment_proxy
            ):
                raise ExecutorRuntimeError("RUNTIME_PROXY_SOURCES_CONFLICT")
            proxy_url = (
                environment_proxy
                if environment_proxy is not None
                else (
                    vault_proxy.get_secret_value() if vault_proxy is not None else None
                )
            )
            if live_write and not gate_status.risk_control_only:
                if api_key is None or api_secret is None:
                    raise ExecutorRuntimeError("BINANCE_CREDENTIAL_REFERENCE_REQUIRED")
                live_venue_account_qualifier = LiveVenueAccountQualifier(
                    settings.release.venue_account_type,
                    api_key=api_key,
                    api_secret=api_secret,
                    # Startup proves the account type and caches the complete
                    # lead-symbol set. Each activation checks its exact symbol
                    # at discovery and again at final submission.
                    required_symbols=(),
                    proxy_url=proxy_url,
                )
                live_venue_account_qualifier.require_current()
            binance_hedge_mode = False
            if not read_only:
                if api_key is None or api_secret is None:
                    raise ExecutorRuntimeError(
                        "BINANCE_CREDENTIAL_REFERENCE_REQUIRED"
                    )
                binance_hedge_mode = asyncio.run(
                    query_execution_hedge_mode(
                        settings.release.profile,
                        api_key=api_key,
                        api_secret=api_secret,
                        proxy_url=proxy_url,
                    )
                )
            secret_values: list[str] = []
            if api_key is not None and api_secret is not None:
                secret_values.extend(
                    (api_key.get_secret_value(), api_secret.get_secret_value())
                )
            if database_password is not None:
                secret_values.append(database_password.get_secret_value())
            if proxy_url is not None:
                secret_values.append(proxy_url)
            logger = configure_halpha_logging(
                runtime_log_directory(
                    repo_root,
                    settings,
                    role="executor",
                ),
                role="executor",
                secret_values=tuple(secret_values),
            )
            paused_activations = 0
            if not read_only:
                if database_password is None:
                    raise ExecutorRuntimeError("PRODUCT_DATABASE_CREDENTIAL_REQUIRED")
                paused_activations = PostgreSQLExecutorContinuityGuard(
                    database_name=settings.release.database_name,
                    password=database_password,
                    environment_id=settings.release.environment_id,
                ).pause_open_activations(datetime.now(UTC))
            liveness_watchdog = _RuntimeLivenessWatchdog(
                on_timeout=lambda elapsed: logger.critical(
                    "runtime_event_loop_stalled",
                    elapsed_seconds=elapsed,
                )
            )
            runtime = ProductExecutorRuntime(
                settings=role_settings,
                database_password=database_password,
                api_key=api_key,
                api_secret=api_secret,
                log_directory=runtime_log_directory(
                    repo_root,
                    settings,
                    role="executor",
                ),
                proxy_url=proxy_url,
                runtime_real_write_gate=gate_status.runtime_real_write_gate,
                live_write_activation_ids=gate_status.authorized_activation_ids,
                live_write_submission_guard=live_write_submission_guard,
                live_write_risk_control_only=gate_status.risk_control_only,
                live_write_account_qualification_refresh=(
                    live_venue_account_qualifier.refresh
                    if live_venue_account_qualifier is not None
                    else None
                ),
                binance_hedge_mode=binance_hedge_mode,
                runtime_event_sink=lambda event, fields: logger.info(event, **fields),
                runtime_heartbeat_sink=liveness_watchdog.heartbeat,
                forward_observation_spec=observation_spec,
                observation_proposal_sink=(
                    observation.record_proposal if observation is not None else None
                ),
                observation_bar_sink=(
                    observation.record_bar if observation is not None else None
                ),
                observation_quote_sink=(
                    observation.record_quote_tick if observation is not None else None
                ),
                observation_mark_price_sink=(
                    observation.record_mark_price if observation is not None else None
                ),
            )
            failure_cleanup_watchdog: Timer | None = None
            failure_stop_signaled = False
            try:
                liveness_watchdog.start()
                try:
                    runtime.build()
                except Exception as exc:
                    logger.error(
                        "runtime_build_failed",
                        **_runtime_build_failure_log_fields(exc),
                    )
                    raise
                if observation is not None:
                    observation.record_process_started()

                def report_ready(runtime_evidence: dict[str, object]) -> None:
                    recovery_complete = (
                        runtime_evidence.get("startup_reconciliation_completed")
                        is not False
                    )
                    if recovery_complete:
                        runtime.publish_ready_product_build(product_build_id)
                    report = {
                        "status": _runtime_ready_status(
                            settings.release.profile,
                            risk_control_only=gate_status.risk_control_only,
                            recovery_complete=recovery_complete,
                        ),
                        "role": ProcessRole.EXECUTOR.value,
                        "paused_open_activations": paused_activations,
                        "product_build_id": product_build_id,
                        **runtime_evidence,
                    }
                    if observation is not None:
                        observation.record_runtime_ready(runtime_evidence)
                    print(json.dumps(report, sort_keys=True))
                    logger.info(
                        "runtime_ready",
                        **_runtime_ready_log_fields(
                            profile=settings.release.profile,
                            environment_id=settings.release.environment_id,
                            paused_open_activations=paused_activations,
                            proxy_supplied=proxy_url is not None,
                            product_build_id=product_build_id,
                            runtime_evidence=runtime_evidence,
                        ),
                    )

                try:
                    runtime.run_until_stop(stop_event.wait, on_ready=report_ready)
                except BaseException as exc:
                    failure_stop_signaled = _signal_runtime_failure_stop(
                        stop_event
                    )
                    logger.error(
                        "runtime_failed",
                        exception_type=type(exc).__name__,
                        reason_code=str(exc),
                        stop_waiter_signaled=failure_stop_signaled,
                    )
                    failure_cleanup_watchdog = (
                        _start_failure_cleanup_watchdog()
                    )
                    raise
                logger.info("runtime_stopped", reason_code="MAINTENANCE_STOP")
                if observation is not None:
                    observation.close(reason_code="MAINTENANCE_STOP")
            finally:
                try:
                    runtime.close()
                finally:
                    try:
                        if (
                            failure_cleanup_watchdog is not None
                            and failure_stop_signaled
                        ):
                            failure_cleanup_watchdog.cancel()
                        logger.info("runtime_exiting")
                        if observation is not None:
                            observation.close(reason_code="RUNTIME_EXIT")
                    finally:
                        liveness_watchdog.close()
            return 0
    except (
        ConfigurationError,
        ExecutorContinuityUnavailable,
        ExecutorRuntimeError,
        ForwardObservationError,
        LiveWriteGateError,
        RuntimeIdentityError,
        SecretResolutionError,
        SourceIdentityError,
        VenueAccountQualificationError,
        WindowsFilesystemError,
        WindowsRuntimeError,
    ) as exc:
        print(json.dumps({"status": "STARTUP_REJECTED", "reason": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
