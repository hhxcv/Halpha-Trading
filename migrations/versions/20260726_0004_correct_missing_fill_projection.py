"""Correct the proven periodic missing-fill synthetic reconciliation stop.

Revision ID: 20260726_0004
Revises: 20260726_0003
"""

from __future__ import annotations

from hashlib import sha256
import json
from uuid import NAMESPACE_URL, uuid5

from alembic import op
import sqlalchemy as sa


revision = "20260726_0004"
down_revision = "20260726_0003"
branch_labels = None
depends_on = None


_DEMO_ENVIRONMENT_ID = "binance-demo-primary"
_SYNTHETIC_STOP_ID = "00000000-0000-4000-8000-000000000004"


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


def upgrade() -> None:
    connection = op.get_bind()
    evidence = connection.execute(
        sa.text(
            """
            SELECT stop.stop_state_version_id, stop.environment_id,
                   stop.environment_kind, stop.authority_class,
                   stop.account_ref, stop.version, stop.stopped_categories,
                   stop.started_at, stop.loss_latch_digest,
                   stop.content_digest, stop.release_rules,
                   accepted.venue_fact_id AS accepted_fact_id,
                   accepted.source_object_id AS client_order_id,
                   accepted.payload ->> 'venue_order_ref' AS venue_order_ref,
                   fill.venue_fact_id AS fill_fact_id,
                   position.venue_fact_id AS position_fact_id,
                   position.received_at AS position_received_at,
                   position.payload AS position_payload
            FROM halpha.stop_state_version AS stop
            JOIN halpha.venue_fact AS accepted
              ON accepted.environment_id = stop.environment_id
             AND accepted.received_at = stop.started_at
             AND accepted.source_class = 'EXTERNAL_UNCLAIMED'
             AND accepted.kind = 'ORDER_STATE'
             AND accepted.payload ->> 'event_type' = 'OrderAccepted'
             AND accepted.payload ->> 'reconciliation' = 'true'
             AND accepted.source_object_id ~
                 '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
             AND accepted.payload ->> 'venue_order_ref' ~
                 '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            JOIN LATERAL (
                SELECT fact.venue_fact_id, fact.received_at
                FROM halpha.venue_fact AS fact
                WHERE fact.environment_id = stop.environment_id
                  AND fact.source_class = 'EXTERNAL_UNCLAIMED'
                  AND fact.kind = 'FILL'
                  AND fact.payload ->> 'client_order_id'
                      = accepted.source_object_id
                  AND fact.payload ->> 'venue_order_ref'
                      = accepted.payload ->> 'venue_order_ref'
                  AND fact.payload ->> 'reconciliation' = 'true'
                ORDER BY fact.received_at, fact.venue_fact_id
                LIMIT 1
            ) AS fill ON TRUE
            JOIN LATERAL (
                SELECT fact.venue_fact_id, fact.received_at, fact.payload
                FROM halpha.venue_fact AS fact
                WHERE fact.environment_id = stop.environment_id
                  AND fact.source_class = 'VENUE_QUERY'
                  AND fact.kind = 'POSITION_STATE'
                  AND fact.received_at > fill.received_at
                ORDER BY fact.received_at, fact.venue_fact_id
                LIMIT 1
            ) AS position ON TRUE
            WHERE stop.environment_id = :environment_id
              AND stop.stop_state_version_id = CAST(:stop_id AS uuid)
              AND stop.activation_id IS NULL
              AND stop.source = 'SYSTEM_EXTERNAL_ACTIVITY'
              AND 'NEW_RISK' = ANY(stop.stopped_categories)
              AND NOT EXISTS (
                  SELECT 1
                  FROM halpha.stop_state_version AS later
                  WHERE later.environment_id = stop.environment_id
                    AND later.account_ref = stop.account_ref
                    AND later.activation_id IS NULL
                    AND later.version > stop.version
              )
            """
        ),
        {
            "environment_id": _DEMO_ENVIRONMENT_ID,
            "stop_id": _SYNTHETIC_STOP_ID,
        },
    ).mappings().one_or_none()
    if evidence is None:
        return
    position_payload = dict(evidence["position_payload"])
    client_order_id = str(evidence["client_order_id"])
    open_ids = {
        *position_payload.get("account_open_order_client_ids", []),
        *position_payload.get("account_open_algo_client_ids", []),
    }
    if (
        position_payload.get("position_quantity")
        != position_payload.get("attributed_account_position_quantity")
        or client_order_id in open_ids
    ):
        return

    categories = sorted(
        category
        for category in evidence["stopped_categories"]
        if category != "NEW_RISK"
    )
    correction_id = str(
        uuid5(
            NAMESPACE_URL,
            f"urn:halpha:missing-fill-correction:{_SYNTHETIC_STOP_ID}",
        )
    )
    release_rules = {
        category: evidence["release_rules"][category]
        for category in categories
        if category in evidence["release_rules"]
    }
    release_rules["system_reconciliation_correction"] = {
        "corrected_stop_state_version_id": str(
            evidence["stop_state_version_id"]
        ),
        "corrected_stop_version": int(evidence["version"]),
        "corrected_stop_content_digest": str(evidence["content_digest"]),
        "synthetic_order_fact_id": str(evidence["accepted_fact_id"]),
        "synthetic_fill_fact_id": str(evidence["fill_fact_id"]),
        "confirming_position_fact_id": str(evidence["position_fact_id"]),
        "client_order_id": client_order_id,
        "venue_order_ref": str(evidence["venue_order_ref"]),
        "reason": "NAUTILUS_MISSING_FILL_PROJECTION_NOT_A_NEW_VENUE_TRADE",
    }
    fields = {
        "stop_state_version_id": correction_id,
        "environment_id": str(evidence["environment_id"]),
        "environment_kind": str(evidence["environment_kind"]),
        "authority_class": str(evidence["authority_class"]),
        "account_ref": str(evidence["account_ref"]),
        "activation_id": None,
        "version": int(evidence["version"]) + 1,
        "stopped_categories": frozenset(categories),
        "reason": "SYNTHETIC_RECONCILIATION_STOP_CORRECTED",
        "source": "SYSTEM_FRAMEWORK_SYNTHETIC_CORRECTION",
        "started_at": evidence["position_received_at"],
        "loss_latch_digest": (
            str(evidence["loss_latch_digest"])
            if evidence["loss_latch_digest"] is not None
            else None
        ),
        "release_rules": release_rules,
    }
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
            "content_digest": _content_digest(fields),
        },
    )


def downgrade() -> None:
    raise RuntimeError("DATABASE_DOWNGRADE_FORBIDDEN")
