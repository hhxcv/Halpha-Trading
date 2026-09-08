"""Private PostgreSQL persistence for Review."""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from halpha.outcomes.models import (
    EvidencePurpose,
    PrimaryResult,
    Review,
    ReviewRevisionReason,
    ReviewStatus,
    StageReview,
    StageReviewCreator,
)


class OutcomeConflict(ValueError):
    """Stable OUT conflict result."""


class PostgreSQLOutcomeRepository:
    def __init__(self, connection: Connection[Any], environment_id: str) -> None:
        self._connection = connection
        self._environment_id = environment_id

    def insert_review(self, review: Review) -> None:
        if review.environment_id != self._environment_id:
            raise OutcomeConflict("REVIEW_ENVIRONMENT_MISMATCH")
        self._connection.execute(
            """
            INSERT INTO halpha.review (
              review_id, review_version, environment_id, activation_id,
              previous_version, revision_reason, status, primary_result, fact_cutoff,
              input_refs, input_digest, account_result, open_responsibilities,
              evaluations, evidence_purpose, content_digest, created_at
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s
            )
            """,
            (
                review.review_id,
                review.review_version,
                review.environment_id,
                review.activation_id,
                review.previous_version,
                review.revision_reason.value,
                review.status.value,
                review.primary_result.value,
                review.fact_cutoff,
                Jsonb(review.input_refs),
                review.input_digest,
                Jsonb(review.account_result),
                Jsonb(review.open_responsibilities),
                Jsonb(review.evaluations),
                review.evidence_purpose.value,
                review.content_digest,
                review.created_at,
            ),
        )

    def lock_activation(self, activation_id: str) -> None:
        row = self._connection.execute(
            """
            SELECT activation_id
            FROM halpha.plan_activation
            WHERE environment_id = %s AND activation_id = %s
            FOR UPDATE
            """,
            (self._environment_id, activation_id),
        ).fetchone()
        if row is None:
            raise OutcomeConflict("ACTIVATION_NOT_FOUND")

    def get_latest_for_activation(self, activation_id: str) -> Review | None:
        row = self._connection.execute(
            """
            SELECT review_id, review_version, environment_id, activation_id,
                   previous_version, revision_reason, status, primary_result, fact_cutoff,
                   input_refs, input_digest, account_result, open_responsibilities,
                   evaluations, evidence_purpose, content_digest, created_at
            FROM halpha.review
            WHERE environment_id = %s AND activation_id = %s
            ORDER BY review_version DESC
            LIMIT 1
            """,
            (self._environment_id, activation_id),
        ).fetchone()
        return _review_from_row(row) if row is not None else None

    def get_review(self, review_id: str, version: int | None = None) -> Review:
        version_filter = "AND review_version = %s" if version is not None else ""
        parameters: tuple[Any, ...] = (
            (self._environment_id, review_id, version)
            if version is not None
            else (self._environment_id, review_id)
        )
        row = self._connection.execute(
            f"""
            SELECT review_id, review_version, environment_id, activation_id,
                   previous_version, revision_reason, status, primary_result, fact_cutoff,
                   input_refs, input_digest, account_result, open_responsibilities,
                   evaluations, evidence_purpose, content_digest, created_at
            FROM halpha.review
            WHERE environment_id = %s AND review_id = %s {version_filter}
            ORDER BY review_version DESC LIMIT 1
            """,
            parameters,
        ).fetchone()
        if row is None:
            raise OutcomeConflict("REVIEW_NOT_FOUND")
        return _review_from_row(row)

    def list_review_versions(self, review_id: str) -> tuple[Review, ...]:
        rows = self._connection.execute(
            """
            SELECT review_id, review_version, environment_id, activation_id,
                   previous_version, revision_reason, status, primary_result, fact_cutoff,
                   input_refs, input_digest, account_result, open_responsibilities,
                   evaluations, evidence_purpose, content_digest, created_at
            FROM halpha.review
            WHERE environment_id = %s AND review_id = %s
            ORDER BY review_version DESC
            """,
            (self._environment_id, review_id),
        ).fetchall()
        if not rows:
            raise OutcomeConflict("REVIEW_NOT_FOUND")
        return tuple(_review_from_row(row) for row in rows)

    def list_reviews(self) -> tuple[Review, ...]:
        return self._list_reviews(include_scalp=False)

    def list_evidence_reviews(self) -> tuple[Review, ...]:
        """Include hidden scalp reviews for exact-cohort evidence projections."""

        return self._list_reviews(include_scalp=True)

    def _list_reviews(self, *, include_scalp: bool) -> tuple[Review, ...]:
        rows = self._connection.execute(
            """
            SELECT DISTINCT ON (review_id)
                   review_id, review_version, environment_id, activation_id,
                   previous_version, revision_reason, status, primary_result, fact_cutoff,
                   input_refs, input_digest, account_result, open_responsibilities,
                   evaluations, evidence_purpose, content_digest, created_at
            FROM halpha.review review
            WHERE review.environment_id = %s
              AND (%s OR NOT EXISTS (
                SELECT 1
                FROM halpha.plan_activation activation
                JOIN halpha.trade_plan_version version
                  ON version.environment_id = activation.environment_id
                 AND version.plan_version_id = activation.plan_version_ref
                WHERE activation.environment_id = review.environment_id
                  AND activation.activation_id = review.activation_id
                  AND version.terms ->> 'workflow_kind' = 'SCALP_CYCLE'
              ))
            ORDER BY review_id, review_version DESC
            """,
            (self._environment_id, include_scalp),
        ).fetchall()
        return tuple(_review_from_row(row) for row in rows)

    def get_stage_review_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> StageReview | None:
        row = self._connection.execute(
            """
            SELECT stage_review_id, environment_id, idempotency_key,
                   request_digest, title, range_start, range_end,
                   source_review_refs, metrics_snapshot, problem_analysis,
                   improvement_plan, creator_kind, content_digest, created_at
            FROM halpha.stage_review
            WHERE environment_id = %s AND idempotency_key = %s
            """,
            (self._environment_id, idempotency_key),
        ).fetchone()
        return _stage_review_from_row(row) if row is not None else None

    def insert_stage_review(self, review: StageReview) -> bool:
        if review.environment_id != self._environment_id:
            raise OutcomeConflict("STAGE_REVIEW_ENVIRONMENT_MISMATCH")
        row = self._connection.execute(
            """
            INSERT INTO halpha.stage_review (
              stage_review_id, environment_id, idempotency_key,
              request_digest, title, range_start, range_end,
              source_review_refs, metrics_snapshot, problem_analysis,
              improvement_plan, creator_kind, content_digest, created_at
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (environment_id, idempotency_key) DO NOTHING
            RETURNING stage_review_id
            """,
            (
                review.stage_review_id,
                review.environment_id,
                review.idempotency_key,
                review.request_digest,
                review.title,
                review.range_start,
                review.range_end,
                Jsonb(review.source_review_refs),
                Jsonb(review.metrics_snapshot),
                review.problem_analysis,
                review.improvement_plan,
                review.creator_kind.value,
                review.content_digest,
                review.created_at,
            ),
        ).fetchone()
        return row is not None

    def list_stage_reviews(self) -> tuple[StageReview, ...]:
        rows = self._connection.execute(
            """
            SELECT stage_review_id, environment_id, idempotency_key,
                   request_digest, title, range_start, range_end,
                   source_review_refs, metrics_snapshot, problem_analysis,
                   improvement_plan, creator_kind, content_digest, created_at
            FROM halpha.stage_review
            WHERE environment_id = %s
            ORDER BY created_at DESC, stage_review_id
            """,
            (self._environment_id,),
        ).fetchall()
        return tuple(_stage_review_from_row(row) for row in rows)


def _review_from_row(row: tuple[Any, ...]) -> Review:
    return Review(
        review_id=str(row[0]),
        review_version=int(row[1]),
        environment_id=str(row[2]),
        activation_id=str(row[3]),
        previous_version=int(row[4]) if row[4] is not None else None,
        revision_reason=ReviewRevisionReason(str(row[5])),
        status=ReviewStatus(str(row[6])),
        primary_result=PrimaryResult(str(row[7])),
        fact_cutoff=row[8],
        input_refs=dict(row[9]),
        input_digest=str(row[10]),
        account_result=dict(row[11]),
        open_responsibilities=dict(row[12]),
        evaluations=dict(row[13]),
        evidence_purpose=EvidencePurpose(str(row[14])),
        content_digest=str(row[15]),
        created_at=row[16],
    )


def _stage_review_from_row(row: tuple[Any, ...]) -> StageReview:
    return StageReview(
        stage_review_id=str(row[0]),
        environment_id=str(row[1]),
        idempotency_key=str(row[2]),
        request_digest=str(row[3]),
        title=str(row[4]),
        range_start=row[5],
        range_end=row[6],
        source_review_refs=list(row[7]),
        metrics_snapshot=dict(row[8]),
        problem_analysis=str(row[9]),
        improvement_plan=str(row[10]),
        creator_kind=StageReviewCreator(str(row[11])),
        content_digest=str(row[12]),
        created_at=row[13],
    )
