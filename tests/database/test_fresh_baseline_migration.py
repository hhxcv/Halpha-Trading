from __future__ import annotations

import importlib.util
from io import StringIO
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest

from halpha.database.schema_version import CURRENT_SCHEMA_REVISION


ROOT = Path(__file__).resolve().parents[2]
BASELINE = (
    ROOT
    / "migrations"
    / "versions"
    / "20260724_0001_fresh_product_schema.py"
)
BASE_REVISION = "20260724_0001"
HEAD_REVISION = CURRENT_SCHEMA_REVISION
MULTI_ACTIVATION_MIGRATION = (
    ROOT
    / "migrations"
    / "versions"
    / "20260726_0001_multi_activation_virtual_positions.py"
)
PLAN_CONTINUITY_MIGRATION = (
    ROOT
    / "migrations"
    / "versions"
    / "20260726_0002_preserve_running_plan_intent.py"
)
SYNTHETIC_RECONCILIATION_MIGRATION = (
    ROOT
    / "migrations"
    / "versions"
    / "20260726_0003_correct_synthetic_position_reconciliation.py"
)
MISSING_FILL_RECONCILIATION_MIGRATION = (
    ROOT
    / "migrations"
    / "versions"
    / "20260726_0004_correct_missing_fill_projection.py"
)
OWNED_FILL_REPLAY_MIGRATION = (
    ROOT
    / "migrations"
    / "versions"
    / "20260726_0005_correct_owned_fill_replay.py"
)
OWNED_ORDER_REPLAY_MIGRATION = (
    ROOT
    / "migrations"
    / "versions"
    / "20260726_0006_correct_owned_order_replay.py"
)
ATTRIBUTED_RECONCILIATION_STOP_MIGRATION = (
    ROOT
    / "migrations"
    / "versions"
    / "20260727_0007_correct_attributed_reconciliation_stop.py"
)
STAGE_REVIEW_MIGRATION = (
    ROOT
    / "migrations"
    / "versions"
    / "20260728_0008_stage_reviews.py"
)
HANDOVER_CALLED_ACTION_MIGRATION = (
    ROOT
    / "migrations"
    / "versions"
    / "20260729_0009_handover_called_action_evidence.py"
)
RUNTIME_SCHEMA_GUARD_MIGRATION = (
    ROOT
    / "migrations"
    / "versions"
    / "20260731_0010_runtime_schema_guard.py"
)
POSITION_ALIGNMENT_MIGRATION = (
    ROOT
    / "migrations"
    / "versions"
    / "20260802_0011_position_alignment.py"
)
VENUE_FACT_HOT_PATH_INDEX_MIGRATION = (
    ROOT
    / "migrations"
    / "versions"
    / "20260803_0012_venue_fact_hot_path_indexes.py"
)
PLAN_AI_REVIEW_MIGRATION = (
    ROOT
    / "migrations"
    / "versions"
    / "20260813_0013_plan_ai_reviews.py"
)
PLAN_AI_REVIEW_CONFIGURATION_MIGRATION = (
    ROOT
    / "migrations"
    / "versions"
    / "20260813_0014_plan_ai_review_configuration.py"
)
ACCOUNT_OBSERVATION_FAILURE_MIGRATION = (
    ROOT
    / "migrations"
    / "versions"
    / "20260815_0015_account_observation_failures.py"
)


def _revision_module():
    spec = importlib.util.spec_from_file_location(
        "halpha_fresh_product_schema",
        BASELINE,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_history_is_one_unambiguous_fresh_root() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)

    assert script.get_bases() == [BASE_REVISION]
    assert script.get_heads() == [HEAD_REVISION]
    assert [
        (item.revision, item.down_revision)
        for item in script.walk_revisions()
    ] == [
        (HEAD_REVISION, "20260813_0014"),
        ("20260813_0014", "20260813_0013"),
        ("20260813_0013", "20260803_0012"),
        ("20260803_0012", "20260802_0011"),
        ("20260802_0011", "20260731_0010"),
        ("20260731_0010", "20260729_0009"),
        ("20260729_0009", "20260728_0008"),
        ("20260728_0008", "20260727_0007"),
        ("20260727_0007", "20260726_0006"),
        ("20260726_0006", "20260726_0005"),
        ("20260726_0005", "20260726_0004"),
        ("20260726_0004", "20260726_0003"),
        ("20260726_0003", "20260726_0002"),
        ("20260726_0002", "20260726_0001"),
        ("20260726_0001", BASE_REVISION),
        (BASE_REVISION, None),
    ]
    assert sorted(
        path.name
        for path in (ROOT / "migrations" / "versions").glob("*.py")
    ) == sorted(
        [
            BASELINE.name,
            MULTI_ACTIVATION_MIGRATION.name,
            PLAN_CONTINUITY_MIGRATION.name,
            SYNTHETIC_RECONCILIATION_MIGRATION.name,
            MISSING_FILL_RECONCILIATION_MIGRATION.name,
            OWNED_FILL_REPLAY_MIGRATION.name,
            OWNED_ORDER_REPLAY_MIGRATION.name,
            ATTRIBUTED_RECONCILIATION_STOP_MIGRATION.name,
            STAGE_REVIEW_MIGRATION.name,
            HANDOVER_CALLED_ACTION_MIGRATION.name,
            RUNTIME_SCHEMA_GUARD_MIGRATION.name,
            POSITION_ALIGNMENT_MIGRATION.name,
            VENUE_FACT_HOT_PATH_INDEX_MIGRATION.name,
            PLAN_AI_REVIEW_MIGRATION.name,
            PLAN_AI_REVIEW_CONFIGURATION_MIGRATION.name,
            ACCOUNT_OBSERVATION_FAILURE_MIGRATION.name,
        ]
    )


@pytest.mark.parametrize(
    ("database_name", "expected"),
    (
        ("halpha_demo", ("halpha_demo", "DEMO")),
        ("halpha_live_copy", ("halpha_live_copy", "LIVE")),
        ("halpha_live_personal", ("halpha_live_personal", "LIVE")),
        (
            "halpha_workbench_fixture_1234",
            ("halpha_demo", "DEMO"),
        ),
    ),
)
def test_baseline_binds_database_specific_roles_and_environment(
    database_name: str,
    expected: tuple[str, str],
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
def test_baseline_rejects_any_other_database_name(
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
    ("role_prefix", "environment_kind"),
    (
        ("halpha_demo", "DEMO"),
        ("halpha_live_copy", "LIVE"),
        ("halpha_live_personal", "LIVE"),
    ),
)
def test_baseline_compiles_as_postgresql_for_each_environment(
    role_prefix: str,
    environment_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = _revision_module()
    monkeypatch.setattr(
        revision,
        "_role_prefix",
        lambda: (role_prefix, environment_kind),
    )
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )

    with Operations.context(context):
        revision.upgrade()

    sql = output.getvalue()
    assert sql.count("CREATE TABLE halpha.") == 10
    assert (
        f"ck_plan_activation_database_environment "
        f"CHECK (environment_kind = '{environment_kind}')"
    ) in sql
    assert f'GRANT USAGE ON SCHEMA halpha TO "{role_prefix}_app"' in sql
    assert f'GRANT USAGE ON SCHEMA halpha TO "{role_prefix}_executor"' in sql
    assert f'GRANT USAGE ON SCHEMA halpha TO "{role_prefix}_backup"' in sql
    if environment_kind == "LIVE":
        assert (
            f'GRANT USAGE ON SCHEMA halpha TO "{role_prefix}_app_reader"'
            in sql
        )
        assert (
            'GRANT SELECT ON ALL TABLES IN SCHEMA halpha '
            f'TO "{role_prefix}_app_reader"'
        ) in sql
        assert not any(
            f"GRANT {privilege} ON TABLE halpha.{table} "
            f'TO "{role_prefix}_app_reader"' in sql
            for table in revision.PRODUCT_TABLES
            for privilege in ("INSERT", "UPDATE", "DELETE")
        )
    else:
        assert "halpha_demo_app_reader" not in sql
    assert (
        f"GRANT INSERT, UPDATE, DELETE ON TABLE halpha.trade_plan_draft "
        f'TO "{role_prefix}_app"'
    ) in sql
    assert (
        f"GRANT INSERT, UPDATE ON TABLE halpha.execution_action "
        f'TO "{role_prefix}_executor"'
    ) in sql
    assert (
        f"GRANT INSERT ON TABLE halpha.venue_fact "
        f'TO "{role_prefix}_executor"'
    ) in sql
    assert not any(
        f"GRANT {privilege} ON TABLE halpha.execution_action "
        f'TO "{role_prefix}_executor"' in sql
        for privilege in ("DELETE", "INSERT, UPDATE, DELETE")
    )
    assert "CREATE TRIGGER trg_execution_action_identity_immutable" in sql
    assert "CREATE TRIGGER trg_venue_fact_append_only" in sql
    assert "CREATE TRIGGER trg_review_append_only" in sql
    assert "CREATE TRIGGER trg_plan_activation_identity_immutable" in sql
    assert (
        "BEFORE UPDATE OR DELETE ON halpha.plan_activation"
        in sql
    )


def test_baseline_contains_only_the_current_product_schema() -> None:
    revision = _revision_module()
    source = BASELINE.read_text(encoding="utf-8")

    assert source.count("op.create_table(") == 10
    assert set(revision.PRODUCT_TABLES) == {
        "trade_plan_draft",
        "trade_plan_version",
        "plan_activation",
        "plan_event",
        "venue_fact",
        "stop_state_version",
        "execution_action",
        "review",
        "command",
        "receipt",
    }
    for current_column in (
        "decision_basis_ref",
        "product_build_id",
        "fixed_decision_basis",
        "order_schedule_spec",
        "order_schedule_snapshot",
        "framework_strategy_id",
        "entry_opportunity_consumed",
        "not_submitted_reason",
        "revision_reason",
    ):
        assert f'"{current_column}"' in source


def test_baseline_has_no_data_upgrade_or_removed_schema_compatibility() -> None:
    source = BASELINE.read_text(encoding="utf-8")

    for obsolete in (
        "op.add_column(",
        "op.alter_column(",
        "RENAME COLUMN",
        "UPDATE halpha.",
        "DROP TABLE IF EXISTS",
        "DROP COLUMN IF EXISTS",
        "strategy_definition_ref",
        "fixed_strategy_basis",
        "build_digest",
        "LEGACY_",
        "SUBMITTED_UNKNOWN",
        "PARTIALLY_FILLED",
        "account_capital_limit_version",
        "machine_authorization_version",
        "plan_allocation",
        "improvement_handoff",
        "notification",
        "task",
    ):
        assert obsolete not in source
    assert "CREATE SCHEMA IF NOT EXISTS halpha" not in source
    assert "CREATE SCHEMA halpha AUTHORIZATION CURRENT_USER" in source


def test_current_plan_and_execution_guards_are_created_directly() -> None:
    source = BASELINE.read_text(encoding="utf-8")

    for required_object in (
        "ck_trade_plan_version_decision_basis_strict",
        "ck_trade_plan_version_direct_schedule_strict",
        "uq_trade_plan_version_basis_identity",
        "fk_plan_activation_version_basis",
        "fk_plan_activation_resume_command",
        "ck_plan_activation_pause_state",
        "ck_plan_activation_direct_schedule_strict",
        "uq_plan_activation_open_scope",
        "uq_plan_activation_live_open_account_scope",
        "uq_stop_state_account_scope_version",
        "uq_stop_state_activation_scope_version",
        "guard_plan_activation_identity_immutable",
        "trg_plan_activation_identity_immutable",
        "ck_execution_action_not_submitted_reason",
        "ck_execution_action_order_identity",
        "ck_execution_action_unknown_evidence",
        "ck_execution_action_closure_evidence",
        "ck_execution_action_time_order",
        "uq_execution_action_client_order_identity",
        "guard_execution_action_identity_immutable",
        "trg_execution_action_identity_immutable",
        "guard_venue_fact_append_only",
        "trg_venue_fact_append_only",
        "guard_review_append_only",
        "trg_review_append_only",
        "ck_venue_fact_source_identity",
        "ck_venue_fact_time_order",
    ):
        assert required_object in source
    for responsibility_state in (
        "'READY'",
        "'NOT_SUBMITTED'",
        "'SUBMITTING'",
        "'UNKNOWN'",
        "'OPEN'",
        "'CLOSED'",
        "'HANDED_OVER'",
    ):
        assert responsibility_state in source
    assert "(state IN ('NOT_SUBMITTED','HANDED_OVER') " in source
    assert (
        "request_digest IS NOT NULL AND call_started_at IS NOT NULL"
        in source
    )


def test_handover_called_action_migration_replaces_only_call_evidence_check() -> None:
    source = HANDOVER_CALLED_ACTION_MIGRATION.read_text(encoding="utf-8")

    assert "Revises: 20260728_0008" in source
    assert "ck_execution_action_call_evidence" in source
    assert "(state IN ('NOT_SUBMITTED','HANDED_OVER') " in source
    assert "op.drop_constraint(" in source
    assert "op.create_check_constraint(" in source


def test_position_alignment_migration_is_digest_paired_and_mutually_exclusive() -> None:
    source = POSITION_ALIGNMENT_MIGRATION.read_text(encoding="utf-8")

    assert "Revises: 20260731_0010" in source
    assert source.count('sa.Column("position_alignment", JSONB)') == 2
    assert source.count('position_alignment_digest') >= 6
    assert "HALPHA_POSITION_ALIGNMENT_V1" in source
    assert "(order_schedule_spec IS NOT NULL) <> (position_alignment IS NOT NULL)" in source
    assert "(order_schedule_snapshot IS NOT NULL) <> (position_alignment IS NOT NULL)" in source
    assert "position_alignment IS NULL) = (position_alignment_digest IS NULL" in source


def test_final_write_grants_never_give_app_execution_writes_or_executor_fact_mutation() -> None:
    source = BASELINE.read_text(encoding="utf-8")
    app_block = source.split("app_write = {", 1)[1].split(
        "executor_write = {",
        1,
    )[0]
    executor_block = source.split("executor_write = {", 1)[1].split(
        "}\n    for table, privileges in app_write.items()",
        1,
    )[0]

    assert '"execution_action"' not in app_block
    assert '"venue_fact"' not in app_block
    assert '"execution_action"' in executor_block
    assert '"venue_fact": ("INSERT",)' in executor_block
    assert '"trade_plan_draft": ("INSERT", "UPDATE", "DELETE")' in app_block
    assert '"execution_action": ("INSERT", "UPDATE")' in executor_block
    assert '"review": ("INSERT",)' in app_block
    assert '"review": ("INSERT",)' in executor_block
    assert '"review": ("INSERT", "UPDATE")' not in app_block
    assert '"review": ("INSERT", "UPDATE")' not in executor_block
    assert '"plan_event"' not in app_block
    assert '"receipt"' not in executor_block
    assert '"DELETE"' not in executor_block
    assert "BEFORE UPDATE OR DELETE ON halpha.execution_action" in source
    assert "BEFORE UPDATE OR DELETE ON halpha.venue_fact" in source
    assert "BEFORE UPDATE OR DELETE ON halpha.review" in source


def test_review_versions_are_append_only_and_allow_same_fact_basis() -> None:
    source = BASELINE.read_text(encoding="utf-8")

    assert "status IN ('DRAFT','COMPLETE')" in source
    assert "SUPERSEDED" not in source
    assert "uq_review_activation_input" not in source
    assert "uq_review_activation_version" in source
    assert "ck_review_version_chain" in source
    assert "fk_review_previous_version" in source
    assert '("environment_id", "review_id", "previous_version")' in source
    assert "'INITIAL_DERIVATION'" in source
    assert "'AUTHORITATIVE_FACTS_CHANGED'" in source
    assert "'OWNER_EVALUATION_CHANGED'" in source


def test_critical_trigger_functions_are_not_publicly_executable() -> None:
    source = BASELINE.read_text(encoding="utf-8")

    for function_name in (
        "guard_plan_activation_identity_immutable",
        "guard_execution_action_identity_immutable",
        "guard_venue_fact_append_only",
        "guard_review_append_only",
    ):
        assert f'"{function_name}"' in source
    assert (
        'f"REVOKE ALL ON FUNCTION halpha.{function_name}() FROM PUBLIC"'
        in source
    )


def test_plan_activation_trigger_freezes_terms_and_deadlines_but_not_runtime_projection() -> None:
    source = BASELINE.read_text(encoding="utf-8")
    guard = source.split(
        "CREATE FUNCTION halpha.guard_plan_activation_identity_immutable()",
        1,
    )[1].split(
        "CREATE FUNCTION halpha.guard_execution_action_identity_immutable()",
        1,
    )[0]

    for field in (
        "activation_id",
        "environment_id",
        "environment_kind",
        "authority_class",
        "plan_version_ref",
        "account_ref",
        "instrument_ref",
        "direction",
        "decision_basis_ref",
        "framework_strategy_id",
        "target_exposure",
        "order_schedule_snapshot",
        "order_schedule_snapshot_digest",
        "created_at",
    ):
        assert f"NEW.{field}" in guard
        assert f"OLD.{field}" in guard
    assert "NEW.rule_state -> 'deadlines'" in guard
    assert "OLD.rule_state -> 'deadlines'" in guard
    for mutable_projection in (
        "NEW.lifecycle",
        "NEW.run_state",
        "NEW.has_entry_fill",
        "NEW.responsibility_owner",
        "NEW.state_version",
        "NEW.protection_state",
        "NEW.latest_venue_cutoff",
        "NEW.updated_at",
    ):
        assert mutable_projection not in guard


def test_fresh_root_has_no_destructive_downgrade_path() -> None:
    revision = _revision_module()

    with pytest.raises(RuntimeError, match="DATABASE_DOWNGRADE_FORBIDDEN"):
        revision.downgrade()
