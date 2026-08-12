"""Correct a proven Nautilus synthetic-position reconciliation stop.

Revision ID: 20260726_0003
Revises: 20260726_0002
"""

from __future__ import annotations

from hashlib import sha256
import json
import re
from uuid import NAMESPACE_URL, uuid5

from alembic import op
import sqlalchemy as sa


revision = "20260726_0003"
down_revision = "20260726_0002"
branch_labels = None
depends_on = None


_DEMO_ENVIRONMENT_ID = "binance-demo-primary"
_SYNTHETIC_STOP_ID = "00000000-0000-4000-8000-000000000003"
_UUID_TEXT = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


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
            "stop_id": _SYNTHETIC_STOP_ID,
        },
    ).mappings().one_or_none()
    if stop is None:
        return

    accepted_rows = connection.execute(
        sa.text(
            """
            SELECT venue_fact_id, source_object_id, content_digest, payload
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
    if len(accepted_rows) != 1:
        return
    accepted = accepted_rows[0]
    accepted_payload = dict(accepted["payload"])
    client_order_id = str(accepted["source_object_id"])
    venue_order_ref = str(accepted_payload.get("venue_order_ref", ""))
    if (
        accepted_payload.get("reconciliation") is not True
        or accepted_payload.get("event_type") != "OrderAccepted"
        or accepted_payload.get("status") != "WORKING"
        or _UUID_TEXT.fullmatch(client_order_id) is None
        or _UUID_TEXT.fullmatch(venue_order_ref) is None
        or stop["release_rules"].get("evidence_digest")
        != _content_digest((str(accepted["content_digest"]),))
    ):
        return

    fill = connection.execute(
        sa.text(
            """
            SELECT venue_fact_id, received_at, payload
            FROM halpha.venue_fact
            WHERE environment_id = :environment_id
              AND source_class = 'EXTERNAL_UNCLAIMED'
              AND kind = 'FILL'
              AND payload ->> 'client_order_id' = :client_order_id
              AND payload ->> 'venue_order_ref' = :venue_order_ref
              AND payload ->> 'reconciliation' = 'true'
            ORDER BY received_at, venue_fact_id
            LIMIT 1
            """
        ),
        {
            "environment_id": _DEMO_ENVIRONMENT_ID,
            "client_order_id": client_order_id,
            "venue_order_ref": venue_order_ref,
        },
    ).mappings().one_or_none()
    if fill is None:
        return

    position = connection.execute(
        sa.text(
            """
            SELECT venue_fact_id, received_at, payload
            FROM halpha.venue_fact
            WHERE environment_id = :environment_id
              AND kind = 'POSITION_STATE'
              AND source_class = 'VENUE_QUERY'
              AND received_at > :fill_received_at
            ORDER BY received_at, venue_fact_id
            LIMIT 1
            """
        ),
        {
            "environment_id": _DEMO_ENVIRONMENT_ID,
            "fill_received_at": fill["received_at"],
        },
    ).mappings().one_or_none()
    if position is None:
        return
    position_payload = dict(position["payload"])
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
        for category in stop["stopped_categories"]
        if category != "NEW_RISK"
    )
    correction_id = str(
        uuid5(
            NAMESPACE_URL,
            f"urn:halpha:synthetic-reconciliation-correction:{_SYNTHETIC_STOP_ID}",
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
        "synthetic_order_fact_id": str(accepted["venue_fact_id"]),
        "synthetic_fill_fact_id": str(fill["venue_fact_id"]),
        "confirming_position_fact_id": str(position["venue_fact_id"]),
        "client_order_id": client_order_id,
        "venue_order_ref": venue_order_ref,
        "reason": "NAUTILUS_GENERATED_MISSING_ORDER_NOT_A_VENUE_TRADE",
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
        "reason": "SYNTHETIC_RECONCILIATION_STOP_CORRECTED",
        "source": "SYSTEM_FRAMEWORK_SYNTHETIC_CORRECTION",
        "started_at": position["received_at"],
        "loss_latch_digest": (
            str(stop["loss_latch_digest"])
            if stop["loss_latch_digest"] is not None
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
