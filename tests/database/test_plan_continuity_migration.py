from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib.util
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    ROOT
    / "migrations"
    / "versions"
    / "20260726_0002_preserve_running_plan_intent.py"
)


def _revision_module():
    spec = importlib.util.spec_from_file_location(
        "halpha_plan_continuity_migration",
        MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, *, one=None, all_rows=(), scalar=None):
        self._one = one
        self._all = all_rows
        self._scalar = scalar

    def mappings(self):
        return self

    def one_or_none(self):
        return self._one

    def all(self):
        return self._all

    def scalar_one(self):
        return self._scalar


def test_upgrade_is_append_only_and_never_mutates_plan_or_action_history() -> None:
    source = MIGRATION.read_text(encoding="utf-8").upper()

    assert "INSERT INTO HALPHA.STOP_STATE_VERSION" in source
    for forbidden in (
        "UPDATE HALPHA.",
        "DELETE FROM HALPHA.",
        "TRUNCATE ",
        "DROP TABLE",
        "DROP COLUMN",
        "OP.DROP_",
    ):
        assert forbidden not in source


def test_exact_stale_terminal_no_fill_replay_appends_one_correction(
    monkeypatch,
) -> None:
    revision = _revision_module()
    # Synthetic time: local database incident timestamps are not test data.
    started_at = datetime(2030, 1, 15, 0, 0, tzinfo=UTC)
    source_time = started_at - timedelta(days=3)
    fact_digest = "5" * 64
    stop = {
        "stop_state_version_id": revision._STALE_STOP_ID,
        "environment_id": revision._DEMO_ENVIRONMENT_ID,
        "environment_kind": "DEMO",
        "authority_class": "DEMO_VALIDATION",
        "account_ref": "binance-usdm-demo-owner-primary",
        "version": 1,
        "stopped_categories": ["NEW_RISK"],
        "source": "SYSTEM_EXTERNAL_ACTIVITY",
        "started_at": started_at,
        "loss_latch_digest": None,
        "content_digest": "6" * 64,
        "release_rules": {
            "NEW_RISK": {"user_releasable": False},
            "evidence_digest": revision._content_digest((fact_digest,)),
        },
    }
    evidence = {
        "venue_fact_id": "11111111-1111-1111-1111-111111111111",
        "source_object_id": "a" * 32,
        "source_time": source_time,
        "received_at": started_at,
        "content_digest": fact_digest,
        "payload": {"reconciliation": True, "status": "WORKING"},
    }
    terminal = {
        "venue_fact_id": "22222222-2222-2222-2222-222222222222",
        "received_at": started_at + timedelta(milliseconds=1),
        "content_digest": "7" * 64,
        "payload": {"reconciliation": True, "status": "CANCELLED"},
    }
    calls: list[tuple[str, dict[str, object]]] = []
    results = iter(
        (
            _Result(one=stop),
            _Result(all_rows=(evidence,)),
            _Result(one=terminal),
            _Result(scalar=False),
            _Result(),
        )
    )

    class _Connection:
        def execute(self, statement, parameters):
            calls.append((str(statement), parameters))
            return next(results)

    monkeypatch.setattr(revision.op, "get_bind", lambda: _Connection())

    revision.upgrade()

    assert len(calls) == 5
    insert_parameters = calls[-1][1]
    assert insert_parameters["version"] == 2
    assert insert_parameters["stopped_categories"] == []
    assert insert_parameters["source"] == "SYSTEM_RECONCILIATION_CORRECTION"
    assert len(str(insert_parameters["content_digest"])) == 64


def test_unproven_replay_leaves_the_existing_stop_unchanged(monkeypatch) -> None:
    revision = _revision_module()
    connection = SimpleNamespace(
        execute=lambda _statement, _parameters: _Result(one=None)
    )
    monkeypatch.setattr(revision.op, "get_bind", lambda: connection)

    revision.upgrade()
