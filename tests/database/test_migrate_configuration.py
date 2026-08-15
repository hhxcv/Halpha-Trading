from __future__ import annotations

from pathlib import Path
import runpy
from types import SimpleNamespace

from alembic import context as alembic_context
import pytest
from pydantic import SecretStr

from halpha.configuration import load_settings
from halpha.database import migrate


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "halpha.example.toml"
LIVE_READ_ONLY_EXAMPLE = ROOT / "config" / "halpha.live-copy-read-only.example.toml"
LIVE_WRITE_CONFIG = ROOT / "config" / "halpha.live-copy-write.example.toml"
PERSONAL_READ_ONLY_EXAMPLE = (
    ROOT / "config" / "halpha.live-personal-read-only.example.toml"
)
PERSONAL_WRITE_CONFIG = ROOT / "config" / "halpha.live-personal-write.example.toml"
ALEMBIC_ENV = ROOT / "migrations" / "env.py"


@pytest.mark.parametrize(
    ("config_path", "environment"),
    (
        (EXAMPLE, "demo"),
        (LIVE_READ_ONLY_EXAMPLE, "live_copy"),
        (LIVE_WRITE_CONFIG, "live_copy"),
        (PERSONAL_READ_ONLY_EXAMPLE, "live_personal"),
        (PERSONAL_WRITE_CONFIG, "live_personal"),
    ),
)
def test_migration_target_and_secret_reference_come_from_selected_config(
    config_path: Path,
    environment: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = load_settings(config_path)
    expected = getattr(configured.maintenance, environment)
    backend = object()
    observed: dict[str, object] = {}
    events: list[str] = []

    class _Resolver:
        def resolve(self, reference: object) -> SecretStr:
            observed["reference"] = reference
            return SecretStr("migration-test-secret")

    def resolver_factory(candidate_backend: object, role_settings: object) -> _Resolver:
        events.append("resolver")
        observed["backend"] = candidate_backend
        observed["role_settings"] = role_settings
        return _Resolver()

    def require_identity(sid: str) -> None:
        events.append("identity")
        observed["identity_sid"] = sid

    def get_keyring() -> object:
        events.append("keyring")
        return backend

    monkeypatch.setattr(migrate, "require_process_identity", require_identity)
    monkeypatch.setattr(migrate.keyring, "get_keyring", get_keyring)
    monkeypatch.setattr(migrate, "maintenance_secret_resolver", resolver_factory)

    selected, secret, mutex_scope = migrate._migration_target(
        config_path,
        environment,
    )

    assert selected == expected
    assert observed["reference"] == expected.migration_credential_reference
    assert observed["backend"] is backend
    assert observed["identity_sid"] == configured.windows.maintenance_sid
    assert events[:3] == ["identity", "keyring", "resolver"]
    assert secret == "migration-test-secret"
    assert mutex_scope == migrate.MigrationMutexScope(
        name=configured.executor.mutex_name,
        executor_task_sid=configured.windows.executor_task_sid,
        maintenance_sid=configured.windows.maintenance_sid,
    )


@pytest.mark.parametrize(
    ("config_path", "environment"),
    (
        (EXAMPLE, "live_copy"),
        (LIVE_READ_ONLY_EXAMPLE, "demo"),
        (LIVE_WRITE_CONFIG, "demo"),
    ),
)
def test_migration_rejects_a_target_from_another_profile_before_secret_access(
    config_path: Path,
    environment: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        migrate,
        "require_process_identity",
        lambda _sid: pytest.fail("identity must not be checked for a mismatched target"),
    )
    monkeypatch.setattr(
        migrate.keyring,
        "get_keyring",
        lambda: pytest.fail("secret backend must not be opened for a mismatched target"),
    )

    with pytest.raises(ValueError, match="MIGRATION_ENVIRONMENT_PROFILE_MISMATCH"):
        migrate._migration_target(config_path, environment)


def test_live_read_only_config_cannot_mutate_live_database_before_secret_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        migrate,
        "require_process_identity",
        lambda _sid: pytest.fail("identity must not be checked for a mutation mismatch"),
    )
    monkeypatch.setattr(
        migrate.keyring,
        "get_keyring",
        lambda: pytest.fail("secret backend must not be opened for a mutation mismatch"),
    )

    with pytest.raises(ValueError, match="MIGRATION_MUTATION_PROFILE_MISMATCH"):
        migrate._migration_target(
            LIVE_READ_ONLY_EXAMPLE,
            "live_copy",
            mutating=True,
        )


@pytest.mark.parametrize(
    ("environment", "config"),
    (
        ("demo", EXAMPLE),
        ("live_copy", LIVE_WRITE_CONFIG),
        ("live_personal", PERSONAL_WRITE_CONFIG),
    ),
)
def test_database_downgrade_is_rejected_before_target_or_secret_access(
    environment: str,
    config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(migrate, "require_repository_runtime", lambda: None)
    monkeypatch.setattr(
        migrate,
        "_migration_target",
        lambda *_args, **_kwargs: pytest.fail(
            "downgrade must fail before target and secret resolution"
        ),
    )

    with pytest.raises(ValueError, match="DATABASE_DOWNGRADE_FORBIDDEN"):
        migrate.main(
            [
                "--config",
                str(config),
                environment,
                "downgrade",
                "base",
            ]
        )


class _Connection:
    def __init__(self, events: list[object]) -> None:
        self._events = events

    def execute(self, statement: object) -> None:
        self._events.append(("execute", str(statement)))


class _ConnectionContext:
    def __init__(self, events: list[object]) -> None:
        self._events = events
        self.connection = _Connection(events)

    def __enter__(self) -> _Connection:
        self._events.append("connection_enter")
        return self.connection

    def __exit__(self, *_args: object) -> None:
        self._events.append("connection_exit")


class _Engine:
    def __init__(self, events: list[object]) -> None:
        self._events = events

    def connect(self) -> _ConnectionContext:
        self._events.append("connect")
        return _ConnectionContext(self._events)

    def dispose(self) -> None:
        self._events.append("dispose")


class _Mutex:
    def __init__(self, events: list[object]) -> None:
        self._events = events

    def __enter__(self) -> "_Mutex":
        self._events.append("mutex_enter")
        return self

    def __exit__(self, *_args: object) -> None:
        self._events.append("mutex_exit")


def _patch_migration_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    events: list[object],
) -> None:
    configured = load_settings(EXAMPLE)
    selected = configured.maintenance.demo
    mutex_scope = migrate.MigrationMutexScope(
        name=configured.executor.mutex_name,
        executor_task_sid=configured.windows.executor_task_sid,
        maintenance_sid=configured.windows.maintenance_sid,
    )
    monkeypatch.setattr(
        migrate,
        "_migration_target",
        lambda _config, _environment, *, mutating: (
            events.append(("migration_target", mutating))
            or (
                selected,
                "migration-test-secret",
                mutex_scope,
            )
        ),
    )
    monkeypatch.setattr(
        migrate,
        "require_repository_runtime",
        lambda: events.append("runtime"),
    )
    monkeypatch.setattr(
        migrate,
        "create_engine",
        lambda *_args, **_kwargs: (
            events.append("create_engine") or _Engine(events)
        ),
    )
    monkeypatch.setattr(
        migrate,
        "_alembic_config",
        lambda _root, _connection, *, mutating: (
            events.append(("alembic_config", mutating)) or SimpleNamespace()
        ),
    )


def test_upgrade_holds_environment_executor_mutex_across_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    _patch_migration_runtime(monkeypatch, events=events)

    def acquire_mutex(**kwargs: object) -> _Mutex:
        events.append(("mutex", kwargs))
        return _Mutex(events)

    monkeypatch.setattr(
        migrate,
        "acquire_executor_maintenance_mutex",
        acquire_mutex,
    )
    monkeypatch.setattr(
        migrate.command,
        "upgrade",
        lambda _config, target: events.append(("upgrade", target)),
    )

    assert (
        migrate.main(
            [
                "--config",
                str(EXAMPLE),
                "demo",
                "upgrade",
                "test-target",
            ]
        )
        == 0
    )

    assert events[1] == ("migration_target", True)
    mutex_event = events[2]
    assert isinstance(mutex_event, tuple)
    assert mutex_event[0] == "mutex"
    assert mutex_event[1] == {
        "name": r"Global\Halpha.Executor.BINANCE_DEMO.WriteOwner",
        "executor_task_sid": load_settings(EXAMPLE).windows.executor_task_sid,
        "maintenance_sid": load_settings(EXAMPLE).windows.maintenance_sid,
    }
    assert events[3:] == [
        "mutex_enter",
        "create_engine",
        "connect",
        "connection_enter",
        ("alembic_config", True),
        ("upgrade", "test-target"),
        "connection_exit",
        "dispose",
        "mutex_exit",
    ]


def test_current_is_read_only_and_does_not_acquire_executor_mutex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    _patch_migration_runtime(monkeypatch, events=events)
    monkeypatch.setattr(
        migrate,
        "acquire_executor_maintenance_mutex",
        lambda **_kwargs: pytest.fail("current must not acquire the Executor mutex"),
    )
    monkeypatch.setattr(
        migrate.command,
        "current",
        lambda _config, *, verbose: events.append(("current", verbose)),
    )

    assert (
        migrate.main(
            [
                "--config",
                str(EXAMPLE),
                "demo",
                "current",
            ]
        )
        == 0
    )
    assert events == [
        "runtime",
        ("migration_target", False),
        "create_engine",
        "connect",
        "connection_enter",
        ("execute", "SET TRANSACTION READ ONLY"),
        ("alembic_config", False),
        ("current", True),
        "connection_exit",
        "dispose",
    ]


def test_verify_is_read_only_and_checks_the_runtime_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    _patch_migration_runtime(monkeypatch, events=events)
    monkeypatch.setattr(
        migrate,
        "acquire_executor_maintenance_mutex",
        lambda **_kwargs: pytest.fail("verify must not acquire the Executor mutex"),
    )
    monkeypatch.setattr(
        migrate,
        "require_current_schema",
        lambda _connection: events.append("verify_schema"),
    )

    assert (
        migrate.main(
            [
                "--config",
                str(EXAMPLE),
                "demo",
                "verify",
            ]
        )
        == 0
    )

    assert events == [
        "runtime",
        ("migration_target", False),
        "create_engine",
        "connect",
        "connection_enter",
        ("execute", "SET TRANSACTION READ ONLY"),
        "verify_schema",
        "connection_exit",
        "dispose",
    ]


def test_mutating_migration_does_not_connect_when_executor_mutex_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    _patch_migration_runtime(monkeypatch, events=events)

    def reject_mutex(**_kwargs: object) -> _Mutex:
        raise RuntimeError("EXECUTOR_MUST_BE_STOPPED_FOR_MAINTENANCE")

    monkeypatch.setattr(
        migrate,
        "acquire_executor_maintenance_mutex",
        reject_mutex,
    )

    with pytest.raises(
        RuntimeError,
        match="EXECUTOR_MUST_BE_STOPPED_FOR_MAINTENANCE",
    ):
        migrate.main(
            [
                "--config",
                str(EXAMPLE),
                "demo",
                "upgrade",
                "head",
            ]
        )

    assert events == ["runtime", ("migration_target", True)]


@pytest.mark.parametrize("schema_bootstrap_allowed", (False, True))
def test_alembic_environment_bootstraps_schema_only_for_mutations(
    schema_bootstrap_allowed: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    class _EnvironmentConnection:
        def execute(self, statement: object) -> None:
            events.append(("execute", str(statement)))

        def commit(self) -> None:
            events.append("commit")

    class _EnvironmentTransaction:
        def __enter__(self) -> "_EnvironmentTransaction":
            events.append("transaction_enter")
            return self

        def __exit__(self, *_args: object) -> None:
            events.append("transaction_exit")

    connection = _EnvironmentConnection()
    monkeypatch.setattr(
        alembic_context,
        "config",
        SimpleNamespace(
            attributes={
                "connection": connection,
                "schema_bootstrap_allowed": schema_bootstrap_allowed,
            }
        ),
        raising=False,
    )
    monkeypatch.setattr(
        alembic_context,
        "configure",
        lambda **_kwargs: events.append("configure"),
    )
    monkeypatch.setattr(
        alembic_context,
        "begin_transaction",
        lambda: _EnvironmentTransaction(),
    )
    monkeypatch.setattr(
        alembic_context,
        "run_migrations",
        lambda: events.append("run_migrations"),
    )

    runpy.run_path(str(ALEMBIC_ENV), run_name="halpha_test_alembic_env")

    expected_prefix = (
        [
            (
                "execute",
                "CREATE SCHEMA IF NOT EXISTS halpha_meta AUTHORIZATION CURRENT_USER",
            ),
            "commit",
        ]
        if schema_bootstrap_allowed
        else []
    )
    assert events == [
        *expected_prefix,
        "configure",
        "transaction_enter",
        "run_migrations",
        "transaction_exit",
    ]


def test_alembic_environment_rejects_an_unclassified_direct_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        alembic_context,
        "config",
        SimpleNamespace(attributes={"connection": object()}),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="MIGRATION_OPERATION_ATTRIBUTE_REQUIRED"):
        runpy.run_path(str(ALEMBIC_ENV), run_name="halpha_test_alembic_env")
