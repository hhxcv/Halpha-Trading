"""Preserve running plans and correct one proven stale Demo reconciliation stop.

Revision ID: 20260726_0002
Revises: 20260726_0001
"""

from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
import json
from uuid import NAMESPACE_URL, uuid5

from alembic import op
import sqlalchemy as sa


revision = "20260726_0002"
down_revision = "20260726_0001"
branch_labels = None
depends_on = None


_DEMO_ENVIRONMENT_ID = "binance-demo-primary"
_STALE_STOP_ID = "00000000-0000-4000-8000-000000000002"


def _json_value(value: object) -> object:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_json_value(item) for item in value]
        return sorted(items) if isinstance(value, (set, frozenset)) else items
    return value


def _content_digest(value: object) -> str:
    payload = json.dumps(
        _json_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _correct_proven_stale_demo_stop() -> None:
    connection = op.get_bind()
    stop = connection.execute(
        sa.text(
            """
            SELECT stop_state_version_id, environment_id, environment_kind,
                   authority_class, account_ref, version, stopped_categories,
                   source, started_at, loss_latch_digest, content_digest,
                   release_rules
            FROM halpha.stop_state_version AS candidate
            WHERE environment_id = :environment_id
              AND stop_state_version_id = CAST(:stop_id AS uuid)
              AND activation_id IS NULL
              AND source = 'SYSTEM_EXTERNAL_ACTIVITY'
              AND 'NEW_RISK' = ANY(stopped_categories)
              AND NOT EXISTS (
                  SELECT 1
                  FROM halpha.stop_state_version AS later
                  WHERE later.environment_id = candidate.environment_id
                    AND later.account_ref = candidate.account_ref
                    AND later.activation_id IS NULL
                    AND later.version > candidate.version
              )
            """
        ),
        {
            "environment_id": _DEMO_ENVIRONMENT_ID,
            "stop_id": _STALE_STOP_ID,
        },
    ).mappings().one_or_none()
    if stop is None:
        return

    evidence_facts = connection.execute(
        sa.text(
            """
            SELECT venue_fact_id, source_object_id, source_time, received_at,
                   content_digest, payload
            FROM halpha.venue_fact
            WHERE environment_id = :environment_id
              AND source_class = 'EXTERNAL_UNCLAIMED'
              AND kind = 'ORDER_STATE'
              AND received_at = :started_at
            ORDER BY venue_fact_id
            """
        ),
        {
            "environment_id": _DEMO_ENVIRONMENT_ID,
            "started_at": stop["started_at"],
        },
    ).mappings().all()
    if len(evidence_facts) != 1:
        return
    evidence = evidence_facts[0]
    payload = dict(evidence["payload"])
    if (
        payload.get("reconciliation") is not True
        or payload.get("status") != "WORKING"
        or evidence["source_time"] is None
        or evidence["source_time"] > stop["started_at"] - timedelta(minutes=60)
        or stop["release_rules"].get("evidence_digest")
        != _content_digest((str(evidence["content_digest"]),))
    ):
        return

    client_order_id = str(evidence["source_object_id"])
    terminal = connection.execute(
        sa.text(
            """
            SELECT venue_fact_id, received_at, content_digest, payload
            FROM halpha.venue_fact
            WHERE environment_id = :environment_id
              AND source_class = 'EXTERNAL_UNCLAIMED'
              AND kind = 'ORDER_STATE'
              AND source_object_id = :client_order_id
              AND received_at >= :started_at
              AND payload ->> 'status' IN ('CANCELLED', 'REJECTED', 'EXPIRED')
              AND payload ->> 'reconciliation' = 'true'
            ORDER BY received_at DESC, venue_fact_id DESC
            LIMIT 1
            """
        ),
        {
            "environment_id": _DEMO_ENVIRONMENT_ID,
            "client_order_id": client_order_id,
            "started_at": stop["started_at"],
        },
    ).mappings().one_or_none()
    if terminal is None:
        return
    has_fill = connection.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM halpha.venue_fact
                WHERE environment_id = :environment_id
                  AND source_class = 'EXTERNAL_UNCLAIMED'
                  AND kind IN ('FILL', 'COMMISSION')
                  AND payload ->> 'client_order_id' = :client_order_id
            )
            """
        ),
        {
            "environment_id": _DEMO_ENVIRONMENT_ID,
            "client_order_id": client_order_id,
        },
    ).scalar_one()
    if has_fill:
        return

    categories = sorted(
        category
        for category in stop["stopped_categories"]
        if category != "NEW_RISK"
    )
    correction_id = str(
        uuid5(
            NAMESPACE_URL,
            f"urn:halpha:stale-reconciliation-correction:{_STALE_STOP_ID}",
        )
    )
    release_rules = {
        category: stop["release_rules"][category]
        for category in categories
        if category in stop["release_rules"]
    }
    release_rules["system_reconciliation_correction"] = {
        "corrected_stop_state_version_id": str(stop["stop_state_version_id"]),
        "corrected_stop_version": int(stop["version"]),
        "corrected_stop_content_digest": str(stop["content_digest"]),
        "historical_order_fact_id": str(evidence["venue_fact_id"]),
        "terminal_order_fact_id": str(terminal["venue_fact_id"]),
        "client_order_id": client_order_id,
        "reason": "STALE_TERMINAL_NO_FILL_RECONCILIATION_REPLAY",
    }
    fields = {
        "stop_state_version_id": correction_id,
        "environment_id": str(stop["environment_id"]),
        "environment_kind": str(stop["environment_kind"]),
        "authority_class": str(stop["authority_class"]),
        "account_ref": str(stop["account_ref"]),
        "activation_id": None,
        "version": int(stop["version"]) + 1,
        "stopped_categories": frozenset(categories),
        "reason": "STALE_RECONCILIATION_STOP_CORRECTED",
        "source": "SYSTEM_RECONCILIATION_CORRECTION",
        "started_at": terminal["received_at"],
        "loss_latch_digest": (
            str(stop["loss_latch_digest"])
            if stop["loss_latch_digest"] is not None
            else None
        ),
        "release_rules": release_rules,
    }
    content_digest = _content_digest(fields)
    connection.execute(
        sa.text(
            """
            INSERT INTO halpha.stop_state_version (
                stop_state_version_id, environment_id, environment_kind,
                authority_class, account_ref, activation_id, version,
                stopped_categories, reason, source, started_at,
                loss_latch_digest, release_rules, content_digest
            ) VALUES (
                CAST(:stop_state_version_id AS uuid), :environment_id,
                :environment_kind, :authority_class, :account_ref, NULL,
                :version, CAST(:stopped_categories AS text[]), :reason,
                :source, :started_at, :loss_latch_digest,
                CAST(:release_rules AS jsonb), :content_digest
            )
            ON CONFLICT (stop_state_version_id) DO NOTHING
            """
        ),
        {
            **fields,
            "stopped_categories": categories,
            "release_rules": json.dumps(
                release_rules,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            "content_digest": content_digest,
        },
    )


def upgrade() -> None:
    # The schema and every plan/action/fact row remain untouched. This appends
    # one correction only when the exact previously audited Demo evidence is
    # still current and proves a terminal, no-fill historical replay.
    _correct_proven_stale_demo_stop()


def downgrade() -> None:
    raise RuntimeError("DATABASE_DOWNGRADE_FORBIDDEN")
