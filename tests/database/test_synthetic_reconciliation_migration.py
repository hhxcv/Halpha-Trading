from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    ROOT
    / "migrations"
    / "versions"
    / "20260726_0003_correct_synthetic_position_reconciliation.py"
)
MISSING_FILL_MIGRATION = (
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


def _revision_module():
    spec = importlib.util.spec_from_file_location(
        "halpha_synthetic_reconciliation_migration",
        MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _missing_fill_revision_module():
    spec = importlib.util.spec_from_file_location(
        "halpha_missing_fill_reconciliation_migration",
        MISSING_FILL_MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _owned_fill_replay_revision_module():
    spec = importlib.util.spec_from_file_location(
        "halpha_owned_fill_replay_migration",
        OWNED_FILL_REPLAY_MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _owned_order_replay_revision_module():
    spec = importlib.util.spec_from_file_location(
        "halpha_owned_order_replay_migration",
        OWNED_ORDER_REPLAY_MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, *, one=None, all_rows=()):
        self._one = one
        self._all = all_rows

    def mappings(self):
        return self

    def one_or_none(self):
        return self._one

    def all(self):
        return self._all


def test_synthetic_correction_is_append_only() -> None:
    for path in (
        MIGRATION,
        MISSING_FILL_MIGRATION,
        OWNED_FILL_REPLAY_MIGRATION,
        OWNED_ORDER_REPLAY_MIGRATION,
    ):
        source = path.read_text(encoding="utf-8").upper()
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


def test_exact_synthetic_order_and_matching_position_append_correction(
    monkeypatch,
) -> None:
    revision = _revision_module()
    started_at = datetime(2030, 1, 15, 0, 0, tzinfo=UTC)
    client_order_id = "00000000-0000-4000-8000-000000000101"
    venue_order_ref = "00000000-0000-4000-8000-000000000201"
    fact_digest = "1" * 64
    stop = {
        "stop_state_version_id": revision._SYNTHETIC_STOP_ID,
        "environment_id": revision._DEMO_ENVIRONMENT_ID,
        "environment_kind": "DEMO",
        "authority_class": "DEMO_VALIDATION",
        "account_ref": "binance-usdm-demo-owner-primary",
        "version": 3,
        "stopped_categories": ["NEW_RISK"],
        "source": "SYSTEM_EXTERNAL_ACTIVITY",
        "started_at": started_at,
        "loss_latch_digest": None,
        "content_digest": "2" * 64,
        "release_rules": {
            "NEW_RISK": {"user_releasable": False},
            "evidence_digest": revision._content_digest((fact_digest,)),
        },
    }
    accepted = {
        "venue_fact_id": "11111111-1111-1111-1111-111111111111",
        "source_object_id": client_order_id,
        "content_digest": fact_digest,
        "payload": {
            "event_type": "OrderAccepted",
            "status": "WORKING",
            "reconciliation": True,
            "venue_order_ref": venue_order_ref,
        },
    }
    fill = {
        "venue_fact_id": "22222222-2222-2222-2222-222222222222",
        "received_at": started_at + timedelta(milliseconds=1),
        "payload": {"last_quantity": "0.0015"},
    }
    position = {
        "venue_fact_id": "33333333-3333-3333-3333-333333333333",
        "received_at": started_at + timedelta(milliseconds=2),
        "payload": {
            "position_quantity": "0.0015",
            "attributed_account_position_quantity": "0.0015",
            "account_open_order_client_ids": [],
            "account_open_algo_client_ids": ["known-halpha-protection"],
        },
    }
    calls: list[tuple[str, dict[str, object]]] = []
    results = iter(
        (
            _Result(one=stop),
            _Result(all_rows=(accepted,)),
            _Result(one=fill),
            _Result(one=position),
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
    inserted = calls[-1][1]
    assert inserted["version"] == 4
    assert inserted["stopped_categories"] == []
    assert inserted["source"] == "SYSTEM_FRAMEWORK_SYNTHETIC_CORRECTION"


def test_non_uuid_venue_order_is_not_treated_as_framework_synthetic(
    monkeypatch,
) -> None:
    revision = _revision_module()
    started_at = datetime(2030, 1, 15, 0, 0, tzinfo=UTC)
    fact_digest = "1" * 64
    stop = {
        "stop_state_version_id": revision._SYNTHETIC_STOP_ID,
        "environment_id": revision._DEMO_ENVIRONMENT_ID,
        "environment_kind": "DEMO",
        "authority_class": "DEMO_VALIDATION",
        "account_ref": "binance-usdm-demo-owner-primary",
        "version": 3,
        "stopped_categories": ["NEW_RISK"],
        "source": "SYSTEM_EXTERNAL_ACTIVITY",
        "started_at": started_at,
        "loss_latch_digest": None,
        "content_digest": "2" * 64,
        "release_rules": {
            "evidence_digest": revision._content_digest((fact_digest,))
        },
    }
    accepted = {
        "venue_fact_id": "11111111-1111-1111-1111-111111111111",
        "source_object_id": "00000000-0000-4000-8000-000000000101",
        "content_digest": fact_digest,
        "payload": {
            "event_type": "OrderAccepted",
            "status": "WORKING",
            "reconciliation": True,
            "venue_order_ref": "synthetic-non-uuid-order",
        },
    }
    results = iter((_Result(one=stop), _Result(all_rows=(accepted,))))

    class _Connection:
        def execute(self, _statement, _parameters):
            return next(results)

    monkeypatch.setattr(revision.op, "get_bind", lambda: _Connection())

    revision.upgrade()


def test_periodic_missing_fill_projection_appends_exact_correction(
    monkeypatch,
) -> None:
    revision = _missing_fill_revision_module()
    started_at = datetime(2030, 1, 15, 0, 5, tzinfo=UTC)
    evidence = {
        "stop_state_version_id": revision._SYNTHETIC_STOP_ID,
        "environment_id": revision._DEMO_ENVIRONMENT_ID,
        "environment_kind": "DEMO",
        "authority_class": "DEMO_VALIDATION",
        "account_ref": "binance-usdm-demo-owner-primary",
        "version": 5,
        "stopped_categories": ["NEW_RISK"],
        "started_at": started_at,
        "loss_latch_digest": None,
        "content_digest": "3" * 64,
        "release_rules": {"NEW_RISK": {"user_releasable": False}},
        "accepted_fact_id": "11111111-1111-1111-1111-111111111111",
        "client_order_id": "00000000-0000-4000-8000-000000000102",
        "venue_order_ref": "22222222-2222-2222-2222-222222222222",
        "fill_fact_id": "33333333-3333-3333-3333-333333333333",
        "position_fact_id": "44444444-4444-4444-4444-444444444444",
        "position_received_at": started_at + timedelta(milliseconds=1),
        "position_payload": {
            "position_quantity": "0.0015",
            "attributed_account_position_quantity": "0.0015",
            "account_open_order_client_ids": [],
            "account_open_algo_client_ids": ["known-halpha-protection"],
        },
    }
    calls: list[tuple[str, dict[str, object]]] = []
    results = iter((_Result(one=evidence), _Result()))

    class _Connection:
        def execute(self, statement, parameters):
            calls.append((str(statement), parameters))
            return next(results)

    monkeypatch.setattr(revision.op, "get_bind", lambda: _Connection())

    revision.upgrade()

    assert len(calls) == 2
    inserted = calls[-1][1]
    assert inserted["version"] == 6
    assert inserted["stopped_categories"] == []
    assert inserted["source"] == "SYSTEM_FRAMEWORK_SYNTHETIC_CORRECTION"


def test_periodic_missing_fill_projection_keeps_stop_when_position_differs(
    monkeypatch,
) -> None:
    revision = _missing_fill_revision_module()
    evidence = {
        "stop_state_version_id": revision._SYNTHETIC_STOP_ID,
        "environment_id": revision._DEMO_ENVIRONMENT_ID,
        "environment_kind": "DEMO",
        "authority_class": "DEMO_VALIDATION",
        "account_ref": "binance-usdm-demo-owner-primary",
        "version": 5,
        "stopped_categories": ["NEW_RISK"],
        "started_at": datetime(2030, 1, 15, 0, 5, tzinfo=UTC),
        "loss_latch_digest": None,
        "content_digest": "3" * 64,
        "release_rules": {"NEW_RISK": {"user_releasable": False}},
        "accepted_fact_id": "11111111-1111-1111-1111-111111111111",
        "client_order_id": "00000000-0000-4000-8000-000000000102",
        "venue_order_ref": "22222222-2222-2222-2222-222222222222",
        "fill_fact_id": "33333333-3333-3333-3333-333333333333",
        "position_fact_id": "44444444-4444-4444-4444-444444444444",
        "position_received_at": datetime(2030, 1, 15, 0, 5, 1, tzinfo=UTC),
        "position_payload": {
            "position_quantity": "0.0020",
            "attributed_account_position_quantity": "0.0015",
            "account_open_order_client_ids": [],
            "account_open_algo_client_ids": [],
        },
    }
    calls: list[tuple[str, dict[str, object]]] = []

    class _Connection:
        def execute(self, statement, parameters):
            calls.append((str(statement), parameters))
            return _Result(one=evidence)

    monkeypatch.setattr(revision.op, "get_bind", lambda: _Connection())

    revision.upgrade()

    assert len(calls) == 1


def test_owned_fill_replay_appends_correction_only_after_exact_attribution(
    monkeypatch,
) -> None:
    revision = _owned_fill_replay_revision_module()
    started_at = datetime(2030, 1, 15, 0, 5, tzinfo=UTC)
    evidence = {
        "stop_state_version_id": revision._SYNTHETIC_STOP_ID,
        "environment_id": revision._DEMO_ENVIRONMENT_ID,
        "environment_kind": "DEMO",
        "authority_class": "DEMO_VALIDATION",
        "account_ref": "binance-usdm-demo-owner-primary",
        "version": 5,
        "stopped_categories": ["NEW_RISK"],
        "started_at": started_at,
        "loss_latch_digest": None,
        "content_digest": "5" * 64,
        "release_rules": {"NEW_RISK": {"user_releasable": False}},
        "synthetic_order_fact_id": (
            "11111111-1111-1111-1111-111111111111"
        ),
        "synthetic_client_order_id": (
            "00000000-0000-4000-8000-000000000102"
        ),
        "venue_order_ref": "00000000-0000-4000-8000-000000000201",
        "synthetic_fill_fact_id": (
            "22222222-2222-2222-2222-222222222222"
        ),
        "owned_order_fact_id": (
            "33333333-3333-3333-3333-333333333333"
        ),
        "owned_action_ref": "00000000-0000-4000-8000-000000000104",
        "owned_fill_fact_id": (
            "44444444-4444-4444-4444-444444444444"
        ),
        "position_fact_id": "55555555-5555-5555-5555-555555555555",
        "position_received_at": started_at + timedelta(seconds=3),
        "position_payload": {
            "position_quantity": "0.0015",
            "attributed_account_position_quantity": "0.0015",
            "account_open_order_client_ids": [],
            "account_open_algo_client_ids": ["known-halpha-protection"],
        },
    }
    calls: list[tuple[str, dict[str, object]]] = []
    results = iter((_Result(one=evidence), _Result()))

    class _Connection:
        def execute(self, statement, parameters):
            calls.append((str(statement), parameters))
            return next(results)

    monkeypatch.setattr(revision.op, "get_bind", lambda: _Connection())

    revision.upgrade()

    assert len(calls) == 2
    inserted = calls[-1][1]
    assert inserted["version"] == 6
    assert inserted["stopped_categories"] == []
    assert inserted["source"] == "SYSTEM_FRAMEWORK_SYNTHETIC_CORRECTION"
    release = json.loads(inserted["release_rules"])
    evidence_record = release["system_reconciliation_correction"]
    assert evidence_record["owned_action_ref"] == evidence[
        "owned_action_ref"
    ]
    assert (
        evidence_record["reason"]
        == "NAUTILUS_MISSING_FILL_REPLAY_OF_PERSISTED_HALPHA_FILL"
    )


def test_owned_order_replay_appends_correction_only_after_exact_attribution(
    monkeypatch,
) -> None:
    revision = _owned_order_replay_revision_module()
    started_at = datetime(2030, 1, 15, 0, 35, tzinfo=UTC)
    evidence = {
        "stop_state_version_id": revision._SYNTHETIC_STOP_ID,
        "environment_id": revision._DEMO_ENVIRONMENT_ID,
        "environment_kind": "DEMO",
        "authority_class": "DEMO_VALIDATION",
        "account_ref": "binance-usdm-demo-owner-primary",
        "version": 7,
        "stopped_categories": ["NEW_RISK"],
        "loss_latch_digest": None,
        "content_digest": "7" * 64,
        "release_rules": {"NEW_RISK": {"user_releasable": False}},
        "synthetic_order_fact_id": (
            "11111111-1111-1111-1111-111111111111"
        ),
        "synthetic_client_order_id": (
            "00000000-0000-4000-8000-000000000103"
        ),
        "venue_order_ref": "00000000-0000-4000-8000-000000000201",
        "owned_order_fact_id": (
            "22222222-2222-2222-2222-222222222222"
        ),
        "owned_action_ref": "00000000-0000-4000-8000-000000000104",
        "owned_fill_fact_id": (
            "33333333-3333-3333-3333-333333333333"
        ),
        "position_fact_id": "44444444-4444-4444-4444-444444444444",
        "position_received_at": started_at + timedelta(seconds=3),
        "position_payload": {
            "position_quantity": "0.0015",
            "attributed_account_position_quantity": "0.0015",
            "account_open_order_client_ids": [],
            "account_open_algo_client_ids": ["known-halpha-protection"],
        },
    }
    calls: list[tuple[str, dict[str, object]]] = []
    results = iter((_Result(one=evidence), _Result()))

    class _Connection:
        def execute(self, statement, parameters):
            calls.append((str(statement), parameters))
            return next(results)

    monkeypatch.setattr(revision.op, "get_bind", lambda: _Connection())

    revision.upgrade()

    assert len(calls) == 2
    inserted = calls[-1][1]
    assert inserted["version"] == 8
    assert inserted["stopped_categories"] == []
    assert inserted["source"] == "SYSTEM_FRAMEWORK_SYNTHETIC_CORRECTION"
    release = json.loads(inserted["release_rules"])
    evidence_record = release["system_reconciliation_correction"]
    assert evidence_record["owned_action_ref"] == evidence[
        "owned_action_ref"
    ]
    assert (
        evidence_record["reason"]
        == "NAUTILUS_OWNED_ORDER_REPLAY_WITH_GENERATED_CLIENT_ID"
    )
