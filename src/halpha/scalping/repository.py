"""PostgreSQL identity boundary for scalping cycles."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from halpha.scalping.models import ScalpCycleRecord, ScalpTemplate


class ScalpConflict(RuntimeError):
    pass


class PostgreSQLScalpRepository:
    def __init__(self, connection: Connection[Any], environment_id: str) -> None:
        self._connection = connection
        self._environment_id = environment_id

    def get_by_idempotency(self, idempotency_key: str) -> ScalpCycleRecord | None:
        row = self._connection.execute(
            """
            SELECT cycle_id, environment_id, account_ref, instrument_ref,
                   direction, template_snapshot, template_digest,
                   request_digest, idempotency_key, plan_id, plan_version_id,
                   activation_id, triggered_at
            FROM halpha.scalp_cycle
            WHERE environment_id = %s AND idempotency_key = %s
            """,
            (self._environment_id, idempotency_key),
        ).fetchone()
        return _cycle_from_row(row) if row is not None else None

    def insert(self, cycle: ScalpCycleRecord) -> None:
        if cycle.environment_id != self._environment_id:
            raise ScalpConflict("SCALP_CYCLE_ENVIRONMENT_MISMATCH")
        self._connection.execute(
            """
            INSERT INTO halpha.scalp_cycle (
                cycle_id, environment_id, account_ref, instrument_ref,
                direction, template_snapshot, template_digest,
                request_digest, idempotency_key, plan_id, plan_version_id,
                activation_id, triggered_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                cycle.cycle_id,
                cycle.environment_id,
                cycle.account_ref,
                cycle.instrument_ref,
                cycle.direction.value,
                Jsonb(cycle.template.model_dump(mode="json")),
                cycle.template_digest,
                cycle.request_digest,
                cycle.idempotency_key,
                cycle.plan_id,
                cycle.plan_version_id,
                cycle.activation_id,
                cycle.triggered_at,
            ),
        )

    def lock_open_cycle_scope(self, *, account_ref: str) -> None:
        """Serialize the account-wide one-open-cycle invariant across tabs."""

        self._connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (
                f"halpha:scalp-cycle:{self._environment_id}:{account_ref}",
            ),
        )

    def has_open_cycle(self, *, account_ref: str) -> bool:
        row = self._connection.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM halpha.scalp_cycle cycle
                JOIN halpha.plan_activation activation
                  ON activation.environment_id = cycle.environment_id
                 AND activation.activation_id = cycle.activation_id
                WHERE cycle.environment_id = %s
                  AND cycle.account_ref = %s
                  AND activation.lifecycle <> 'COMPLETED'
            )
            """,
            (self._environment_id, account_ref),
        ).fetchone()
        return bool(row and row[0])

    def result_rows(
        self,
        *,
        account_ref: str,
        range_start: datetime | None,
        range_end: datetime,
    ) -> tuple[tuple[Any, ...], ...]:
        start_clause = "AND cycle.triggered_at >= %s" if range_start is not None else ""
        parameters: tuple[Any, ...] = (
            (self._environment_id, account_ref, range_end, range_start)
            if range_start is not None
            else (self._environment_id, account_ref, range_end)
        )
        rows = self._connection.execute(
            f"""
            SELECT cycle.cycle_id, cycle.activation_id, cycle.instrument_ref,
                   cycle.direction, cycle.triggered_at,
                   activation.lifecycle, activation.state_version,
                   latest_review.account_result,
                   COALESCE(latest_review.fact_cutoff, activation.updated_at),
                   latest_review.open_responsibilities
            FROM halpha.scalp_cycle cycle
            JOIN halpha.plan_activation activation
              ON activation.environment_id = cycle.environment_id
             AND activation.activation_id = cycle.activation_id
            LEFT JOIN LATERAL (
                SELECT review.account_result, review.fact_cutoff,
                       review.open_responsibilities
                FROM halpha.review review
                WHERE review.environment_id = cycle.environment_id
                  AND review.activation_id = cycle.activation_id
                ORDER BY review.review_version DESC
                LIMIT 1
            ) latest_review ON true
            WHERE cycle.environment_id = %s
              AND cycle.account_ref = %s
              AND cycle.triggered_at <= %s
              {start_clause}
            ORDER BY cycle.triggered_at DESC, cycle.cycle_id DESC
            """,
            parameters,
        ).fetchall()
        return tuple(rows)

    def latest_result_row(
        self, *, account_ref: str, range_end: datetime
    ) -> tuple[Any, ...] | None:
        row = self._connection.execute(
            """
            SELECT cycle.cycle_id, cycle.activation_id, cycle.instrument_ref,
                   cycle.direction, cycle.triggered_at,
                   activation.lifecycle, activation.state_version,
                   latest_review.account_result,
                   COALESCE(latest_review.fact_cutoff, activation.updated_at),
                   latest_review.open_responsibilities
            FROM halpha.scalp_cycle cycle
            JOIN halpha.plan_activation activation
              ON activation.environment_id = cycle.environment_id
             AND activation.activation_id = cycle.activation_id
            LEFT JOIN LATERAL (
                SELECT review.account_result, review.fact_cutoff,
                       review.open_responsibilities
                FROM halpha.review review
                WHERE review.environment_id = cycle.environment_id
                  AND review.activation_id = cycle.activation_id
                ORDER BY review.review_version DESC
                LIMIT 1
            ) latest_review ON true
            WHERE cycle.environment_id = %s AND cycle.account_ref = %s
              AND cycle.triggered_at <= %s
            ORDER BY cycle.triggered_at DESC, cycle.cycle_id DESC
            LIMIT 1
            """,
            (self._environment_id, account_ref, range_end),
        ).fetchone()
        return tuple(row) if row is not None else None


def _cycle_from_row(row: tuple[Any, ...]) -> ScalpCycleRecord:
    return ScalpCycleRecord(
        cycle_id=str(row[0]),
        environment_id=str(row[1]),
        account_ref=str(row[2]),
        instrument_ref=str(row[3]),
        direction=str(row[4]),
        template=ScalpTemplate.model_validate(row[5]),
        template_digest=str(row[6]),
        request_digest=str(row[7]),
        idempotency_key=str(row[8]),
        plan_id=str(row[9]),
        plan_version_id=str(row[10]),
        activation_id=str(row[11]),
        triggered_at=row[12],
    )
