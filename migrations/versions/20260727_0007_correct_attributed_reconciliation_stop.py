"""Correct a stop raised after every reconciled order was already attributed.

Revision ID: 20260727_0007
Revises: 20260726_0006
"""

from __future__ import annotations

from hashlib import sha256
import json
from uuid import NAMESPACE_URL, uuid5

from alembic import op
import sqlalchemy as sa


revision = "20260727_0007"
down_revision = "20260726_0006"
branch_labels = None
depends_on = None


_DEMO_ENVIRONMENT_ID = "binance-demo-primary"
_ATTRIBUTED_BATCH_STOP_ID = "00000000-0000-4000-8000-000000000007"
_EVIDENCE_DIGEST = "0" * 64


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


def _all_references_are_owned(
    connection: sa.engine.Connection,
    *,
    environment_id: str,
    account_ref: str,
    position_payload: dict[str, object],
) -> bool:
    fill_refs = {
        str(value)
        for value in position_payload.get("account_fill_fact_refs", [])
    }
    open_client_ids = {
        str(value)
        for key in (
            "account_open_order_client_ids",
            "account_open_algo_client_ids",
        )
        for value in position_payload.get(key, [])
    }
    owned_fill_refs = {
        str(row[0])
        for row in connection.execute(
            sa.text(
                """
                SELECT venue_fact_id
                FROM halpha.venue_fact
                WHERE environment_id = :environment_id
                  AND account_ref = :account_ref
                  AND kind = 'FILL'
                  AND action_ref IS NOT NULL
                """
            ),
            {
                "environment_id": environment_id,
                "account_ref": account_ref,
            },
        )
    }
    owned_client_ids = {
        str(row[0])
        for row in connection.execute(
            sa.text(
                """
                SELECT client_order_id
                FROM halpha.execution_action
                WHERE environment_id = :environment_id
                  AND account_ref = :account_ref
                  AND client_order_id IS NOT NULL
                """
            ),
            {
                "environment_id": environment_id,
                "account_ref": account_ref,
            },
        )
    }
    return (
        fill_refs <= owned_fill_refs
        and open_client_ids <= owned_client_ids
    )


def upgrade() -> None:
    connection = op.get_bind()
    evidence = connection.execute(
        sa.text(
            """
            SELECT stop.stop_state_version_id, stop.environment_id,
                   stop.environment_kind, stop.authority_class,
                   stop.account_ref, stop.version, stop.stopped_categories,
                   stop.loss_latch_digest, stop.content_digest,
                   stop.release_rules,
                   batch.fact_refs,
                   batch.action_refs,
                   position.venue_fact_id AS position_fact_id,
                   position.received_at AS position_received_at,
                   position.payload AS position_payload
            FROM halpha.stop_state_version AS stop
            JOIN LATERAL (
                SELECT
                    array_agg(fact.venue_fact_id ORDER BY fact.received_at)
                        AS fact_refs,
                    array_agg(
                        DISTINCT fact.action_ref
                        ORDER BY fact.action_ref
                    ) AS action_refs,
                    bool_and(
                        fact.action_ref IS NOT NULL
                        AND fact.activation_ref IS NOT NULL
                        AND fact.attribution_class = 'HALPHA_EXECUTION'
                        AND fact.payload ->> 'client_order_id'
                            = action.client_order_id
                    ) AS all_owned
                FROM halpha.venue_fact AS fact
                LEFT JOIN halpha.execution_action AS action
                  ON action.environment_id = fact.environment_id
                 AND action.execution_action_id = fact.action_ref
                WHERE fact.environment_id = stop.environment_id
                  AND fact.account_ref = stop.account_ref
                  AND fact.kind = 'ORDER_STATE'
                  AND fact.source_class = 'VENUE_QUERY'
                  AND fact.received_at >= stop.started_at - interval '1 second'
                  AND fact.received_at <= stop.started_at
            ) AS batch ON batch.all_owned
                           AND cardinality(batch.fact_refs) > 0
            JOIN LATERAL (
                SELECT fact.venue_fact_id, fact.received_at, fact.payload
                FROM halpha.venue_fact AS fact
                WHERE fact.environment_id = stop.environment_id
                  AND fact.account_ref = stop.account_ref
                  AND fact.source_class = 'VENUE_QUERY'
                  AND fact.kind = 'POSITION_STATE'
                  AND fact.received_at > stop.started_at
                ORDER BY fact.received_at DESC, fact.venue_fact_id DESC
                LIMIT 1
            ) AS position ON TRUE
            WHERE stop.environment_id = :environment_id
              AND stop.stop_state_version_id = CAST(:stop_id AS uuid)
              AND stop.activation_id IS NULL
              AND stop.source = 'SYSTEM_EXTERNAL_ACTIVITY'
              AND 'NEW_RISK' = ANY(stop.stopped_categories)
              AND stop.release_rules ->> 'evidence_digest'
                  = :evidence_digest
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
            "stop_id": _ATTRIBUTED_BATCH_STOP_ID,
            "evidence_digest": _EVIDENCE_DIGEST,
        },
    ).mappings().one_or_none()
    if evidence is None:
        return

    position_payload = dict(evidence["position_payload"])
    if (
        position_payload.get("position_quantity")
        != position_payload.get("attributed_account_position_quantity")
        or not _all_references_are_owned(
            connection,
            environment_id=str(evidence["environment_id"]),
            account_ref=str(evidence["account_ref"]),
            position_payload=position_payload,
        )
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
            "urn:halpha:attributed-reconciliation-stop-correction:"
            f"{_ATTRIBUTED_BATCH_STOP_ID}",
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
        "reconciled_fact_refs": [
            str(value) for value in evidence["fact_refs"]
        ],
        "owned_action_refs": [
            str(value) for value in evidence["action_refs"]
        ],
        "confirming_position_fact_id": str(evidence["position_fact_id"]),
        "reason": "ACTIONLESS_BATCH_CONTAINED_ONLY_ATTRIBUTED_ORDERS",
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
        "reason": "ATTRIBUTED_RECONCILIATION_STOP_CORRECTED",
        "source": "SYSTEM_ATTRIBUTED_RECONCILIATION_CORRECTION",
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
