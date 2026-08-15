"""Run Alembic without putting a database secret in config, argv, or environment."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from alembic import command
from alembic.config import Config
import keyring
from sqlalchemy import URL, create_engine, text
from sqlalchemy.pool import NullPool

from halpha.configuration import (
    DatabaseMaintenanceTarget,
    HalphaSettings,
    load_settings,
    maintenance_settings,
)
from halpha.database.schema_version import require_current_schema
from halpha.runtime_identity import repository_root, require_repository_runtime
from halpha.windows_runtime import (
    acquire_executor_maintenance_mutex,
    require_process_identity,
)
from halpha.winvault import maintenance_secret_resolver


@dataclass(frozen=True, slots=True)
class MigrationMutexScope:
    name: str
    executor_task_sid: str
    maintenance_sid: str


def _environment_for_settings(settings: HalphaSettings) -> str:
    """Return the one maintenance target allowed by this product context."""

    return {
        "USDM_DEMO": "demo",
        "USDM_COPY_LEAD": "live_copy",
        "USDM_PERSONAL": "live_personal",
    }[settings.release.venue_account_type.value]


def _alembic_config(
    root: Path,
    connection: object,
    *,
    mutating: bool,
) -> Config:
    config = Config(str(root / "alembic.ini"))
    config.attributes["connection"] = connection
    config.attributes["schema_bootstrap_allowed"] = mutating
    return config


def _migration_target(
    config_path: Path,
    environment: str,
    *,
    mutating: bool = False,
) -> tuple[DatabaseMaintenanceTarget, str, MigrationMutexScope]:
    settings = load_settings(config_path)
    expected_environment = _environment_for_settings(settings)
    if environment != expected_environment:
        raise ValueError("MIGRATION_ENVIRONMENT_PROFILE_MISMATCH")
    if mutating:
        mutation_profile = {
            "demo": "BINANCE_DEMO",
            "live_copy": "BINANCE_LIVE_WRITE",
            "live_personal": "BINANCE_LIVE_WRITE",
        }[environment]
        if settings.release.profile != mutation_profile:
            raise ValueError("MIGRATION_MUTATION_PROFILE_MISMATCH")
    role_settings = maintenance_settings(settings)
    target = getattr(role_settings.maintenance, environment)
    if target.database_name != settings.release.database_name:
        raise ValueError("MIGRATION_DATABASE_PROFILE_MISMATCH")
    require_process_identity(role_settings.maintenance_sid)
    resolver = maintenance_secret_resolver(keyring.get_keyring(), role_settings)
    secret = resolver.resolve(
        target.migration_credential_reference
    ).get_secret_value()
    return (
        target,
        secret,
        MigrationMutexScope(
            name=settings.executor.mutex_name,
            executor_task_sid=settings.windows.executor_task_sid,
            maintenance_sid=role_settings.maintenance_sid,
        ),
    )


def _run_alembic(
    *,
    selected: DatabaseMaintenanceTarget,
    secret: str,
    operation: str,
    target: str,
) -> None:
    url = URL.create(
        "postgresql+psycopg",
        username=selected.migration_role_name,
        password=secret,
        host="127.0.0.1",
        port=5432,
        database=selected.database_name,
    )
    engine = create_engine(url, poolclass=NullPool, echo=False)
    try:
        with engine.connect() as connection:
            if operation == "downgrade":
                raise ValueError("DATABASE_DOWNGRADE_FORBIDDEN")
            mutating = operation == "upgrade"
            if not mutating:
                connection.execute(text("SET TRANSACTION READ ONLY"))
            if operation == "verify":
                require_current_schema(connection)
                return
            config = _alembic_config(
                repository_root(),
                connection,
                mutating=mutating,
            )
            if operation == "upgrade":
                command.upgrade(config, target)
            else:
                command.current(config, verbose=True)
    finally:
        engine.dispose()


def verify_current_schema_for_config(config_path: Path) -> None:
    """Read the selected database and require the current product schema.

    This is deliberately read-only and does not acquire the Executor maintenance
    mutex: it is a lifecycle preflight, not a migration.  Applying a migration
    remains an explicit, serialized maintenance operation.
    """

    settings = load_settings(config_path)
    selected, secret, _ = _migration_target(
        config_path,
        _environment_for_settings(settings),
    )
    try:
        _run_alembic(
            selected=selected,
            secret=secret,
            operation="verify",
            target="head",
        )
    finally:
        secret = None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m halpha.database.migrate")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "environment",
        choices=("demo", "live_copy", "live_personal"),
    )
    parser.add_argument(
        "operation",
        choices=("upgrade", "downgrade", "current", "verify"),
    )
    parser.add_argument("target", nargs="?", default="head")
    args = parser.parse_args(argv)

    require_repository_runtime()
    if args.operation == "downgrade":
        raise ValueError("DATABASE_DOWNGRADE_FORBIDDEN")
    if args.operation == "verify" and args.target != "head":
        raise ValueError("DATABASE_SCHEMA_VERIFY_TARGET_INVALID")
    selected, secret, mutex_scope = _migration_target(
        args.config,
        args.environment,
        mutating=args.operation in {"upgrade", "downgrade"},
    )
    try:
        if args.operation in {"current", "verify"}:
            _run_alembic(
                selected=selected,
                secret=secret,
                operation=args.operation,
                target=args.target,
            )
        else:
            with acquire_executor_maintenance_mutex(
                name=mutex_scope.name,
                executor_task_sid=mutex_scope.executor_task_sid,
                maintenance_sid=mutex_scope.maintenance_sid,
            ):
                _run_alembic(
                    selected=selected,
                    secret=secret,
                    operation=args.operation,
                    target=args.target,
                )
    finally:
        secret = None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
