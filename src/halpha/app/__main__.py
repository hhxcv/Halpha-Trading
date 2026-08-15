"""Entry point for the App process role."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from threading import Thread
from typing import Sequence

import keyring
import psycopg
import uvicorn
from fastapi import FastAPI

from halpha.app.secrets import resolve_app_secrets
from halpha.app.web import WebConfigurationError, create_app
from halpha.configuration import (
    AppSettingsView,
    ConfigurationError,
    app_settings,
    load_settings,
    runtime_log_directory,
)
from halpha.database.schema_version import SchemaVersionError, require_current_schema
from halpha.operational_logging import configure_halpha_logging
from halpha.process_contract import ProcessRole, preflight
from halpha.product_build import calculate_product_build_id
from halpha.runtime_identity import RuntimeIdentityError, repository_root
from halpha.source_identity import SourceIdentityError
from halpha.winvault import SecretResolutionError
from halpha.windows_filesystem import (
    WindowsFilesystemError,
    assert_directory_security,
    runtime_log_acl_spec,
)
from halpha.windows_runtime import (
    WindowsRuntimeError,
    create_stop_event,
    require_process_identity,
)


_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS = 10


def _runtime_schema_failure_log_fields(exc: Exception) -> dict[str, str]:
    """Keep scheduled App startup failures diagnosable without leaking details."""

    return {
        "exception_type": type(exc).__name__,
        "reason_code": (
            str(exc)
            if isinstance(exc, SchemaVersionError)
            else f"APP_RUNTIME_SCHEMA_CHECK_FAILED type={type(exc).__name__}"
        ),
    }


def _require_current_app_schema(
    *,
    database_name: str,
    database_role_name: str,
    database_password: str,
) -> None:
    """Use the App role's own read capability before binding the listener."""

    try:
        with psycopg.connect(
            host="127.0.0.1",
            port=5432,
            dbname=database_name,
            user=database_role_name,
            password=database_password,
            connect_timeout=2,
            autocommit=True,
        ) as connection:
            require_current_schema(connection)
    except SchemaVersionError:
        raise
    except Exception:
        raise SchemaVersionError("DATABASE_SCHEMA_VERSION_UNAVAILABLE") from None


def _uvicorn_config(
    web_app: FastAPI,
    role_settings: AppSettingsView,
) -> uvicorn.Config:
    """Bound shutdown so an open UI WebSocket cannot strand the App task."""

    return uvicorn.Config(
        web_app,
        host=role_settings.app.bind,
        port=role_settings.app.port,
        workers=role_settings.app.workers,
        reload=role_settings.app.reload,
        proxy_headers=False,
        server_header=False,
        log_level="info",
        timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=ProcessRole.APP.value)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.config)
        report = preflight(ProcessRole.APP, settings)
        if args.preflight_only:
            print(json.dumps(report, sort_keys=True))
            return 0
        role_settings = app_settings(settings)
        product_build_id = calculate_product_build_id(repository_root(), settings)
        require_process_identity(role_settings.app_task_sid)
        assert_directory_security(
            runtime_log_acl_spec(
                repository_root(),
                settings,
                role="app",
            )
        )
        secrets = resolve_app_secrets(role_settings, keyring.get_keyring())
        secret_values = [
            secrets.database_password.get_secret_value(),
            secrets.csrf_signing_secret.get_secret_value(),
        ]
        if secrets.smtp_password is not None:
            secret_values.append(secrets.smtp_password.get_secret_value())
        logger = configure_halpha_logging(
            runtime_log_directory(
                repository_root(),
                settings,
                role="app",
            ),
            role="app",
            secret_values=tuple(secret_values),
        )

        def schema_guard() -> None:
            _require_current_app_schema(
                database_name=settings.release.database_name,
                database_role_name=role_settings.app.database_role_name,
                database_password=secrets.database_password.get_secret_value(),
            )

        try:
            # Scheduled tasks may restart after a source update. Check before
            # binding the listener so a missing migration becomes a durable,
            # stable log record instead of an opaque retry loop.
            schema_guard()
        except Exception as exc:
            logger.error(
                "runtime_schema_check_failed",
                **_runtime_schema_failure_log_fields(exc),
            )
            raise
        web_app = create_app(
            settings,
            secrets,
            repo_root=repository_root(),
            product_build_id=product_build_id,
            schema_guard=schema_guard,
        )
        gate_status = web_app.state.live_write_gate_status_provider()
        logger.info(
            "runtime_starting",
            profile=settings.release.profile,
            environment_id=settings.release.environment_id,
            configured_runtime_real_write_gate=(
                gate_status.configured_runtime_real_write_gate
            ),
            runtime_real_write_gate=gate_status.runtime_real_write_gate,
            product_build_id=product_build_id,
        )
    except (
        ConfigurationError,
        RuntimeIdentityError,
        SecretResolutionError,
        SourceIdentityError,
        SchemaVersionError,
        WebConfigurationError,
        WindowsFilesystemError,
        WindowsRuntimeError,
    ) as exc:
        print(json.dumps({"status": "STARTUP_REJECTED", "reason": str(exc)}, sort_keys=True))
        return 2

    try:
        with create_stop_event(
            name=role_settings.stop_event,
            task_sid=role_settings.app_task_sid,
            maintenance_sid=role_settings.maintenance_sid,
        ) as stop_event:
            config = _uvicorn_config(web_app, role_settings)
            server = uvicorn.Server(config)

            def wait_for_stop() -> None:
                stop_event.wait()
                server.should_exit = True

            waiter = Thread(target=wait_for_stop, name="halpha-app-stop-wait", daemon=True)
            waiter.start()
            try:
                server.run()
            finally:
                stop_event.signal()
                waiter.join(timeout=5)
                logger.info("runtime_stopped", reason_code="MAINTENANCE_STOP")
    except WindowsRuntimeError as exc:
        print(json.dumps({"status": "STARTUP_REJECTED", "reason": str(exc)}, sort_keys=True))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
