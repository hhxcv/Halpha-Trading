from __future__ import annotations

import importlib.util
from io import StringIO
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest


MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "20260816_0016_scalp_cycles.py"
)


def _revision_module():
    spec = importlib.util.spec_from_file_location(
        "halpha_scalp_cycle_migration",
        MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("database_name", "expected"),
    (
        ("halpha_demo", ("halpha_demo", False)),
        ("halpha_live_copy", ("halpha_live_copy", True)),
        ("halpha_live_personal", ("halpha_live_personal", True)),
        ("halpha_workbench_fixture_42", ("halpha_demo", False)),
    ),
)
def test_scalp_cycle_binds_only_supported_database_roles(
    database_name: str,
    expected: tuple[str, bool],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = _revision_module()

    class _Result:
        def scalar_one(self) -> str:
            return database_name

    class _Connection:
        def execute(self, _statement: object) -> _Result:
            return _Result()

    monkeypatch.setattr(revision.op, "get_bind", lambda: _Connection())

    assert revision._role_prefix() == expected


@pytest.mark.parametrize("database_name", ("halpha_live", "halpha_restore_check"))
def test_scalp_cycle_rejects_unrelated_database_name(
    database_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = _revision_module()

    class _Result:
        def scalar_one(self) -> str:
            return database_name

    class _Connection:
        def execute(self, _statement: object) -> _Result:
            return _Result()

    monkeypatch.setattr(revision.op, "get_bind", lambda: _Connection())

    with pytest.raises(RuntimeError, match="UNSUPPORTED_HALPHA_DATABASE"):
        revision._role_prefix()


@pytest.mark.parametrize(
    ("role_prefix", "is_live"),
    (
        ("halpha_demo", False),
        ("halpha_live_copy", True),
        ("halpha_live_personal", True),
    ),
)
def test_scalp_cycle_is_immutable_app_owned_identity(
    role_prefix: str,
    is_live: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = _revision_module()
    monkeypatch.setattr(revision, "_role_prefix", lambda: (role_prefix, is_live))
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )

    with Operations.context(context):
        revision.upgrade()

    sql = output.getvalue()
    assert "CREATE TABLE halpha.scalp_cycle" in sql
    assert "fk_scalp_cycle_activation" in sql
    assert "uq_scalp_cycle_idempotency" in sql
    assert f'GRANT SELECT ON TABLE halpha.scalp_cycle TO "{role_prefix}_app"' in sql
    assert f'GRANT INSERT ON TABLE halpha.scalp_cycle TO "{role_prefix}_app"' in sql
    assert "GRANT UPDATE" not in sql
    assert "GRANT DELETE" not in sql
    if is_live:
        assert (
            "GRANT SELECT ON TABLE halpha.scalp_cycle "
            f'TO "{role_prefix}_app_reader"'
        ) in sql
    else:
        assert "halpha_demo_app_reader" not in sql


def test_scalp_cycle_migration_has_no_destructive_downgrade() -> None:
    revision = _revision_module()

    with pytest.raises(RuntimeError, match="DATABASE_DOWNGRADE_FORBIDDEN"):
        revision.downgrade()
