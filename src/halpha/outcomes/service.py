"""OUT application service built only from accepted authoritative references."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from psycopg import Connection

from halpha.domain_values import content_digest
from halpha.outcomes.models import (
    EvidencePurpose,
    OWNER_CONCLUSION_KEY,
    PrimaryResult,
    Review,
    ReviewClassification,
    REVIEW_CLASSIFICATIONS_REQUIRING_REASON,
    ReviewRevisionReason,
    ReviewStatus,
    StageReview,
    StageReviewCreator,
)
from halpha.outcomes.account_reconciliation import account_result_role
from halpha.outcomes.repository import OutcomeConflict, PostgreSQLOutcomeRepository
from halpha.outcomes.trade_result import summarize_trade_result


TERMINAL_ACTION_STATES = frozenset(
    {"NOT_SUBMITTED", "CLOSED", "HANDED_OVER"}
)
UNKNOWN_ACTION_STATES = frozenset({"SUBMITTING", "UNKNOWN"})


def review_id_for_activation(environment_id: str, activation_id: str) -> str:
    return str(
        uuid5(NAMESPACE_URL, f"urn:halpha:{environment_id}:review:{activation_id}")
    )


def stage_review_id_for_request(environment_id: str, idempotency_key: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"urn:halpha:{environment_id}:stage-review:{idempotency_key}",
        )
    )


class OutcomeApplicationService:
    def __init__(self, connection: Connection[Any], environment_id: str) -> None:
        self._connection = connection
        self._environment_id = environment_id
        self._repository = PostgreSQLOutcomeRepository(connection, environment_id)

    def update_activation_review(
        self,
        activation_id: str,
        *,
        fact_cutoff: datetime,
        observed_at: datetime,
        expected_version: int | None = None,
    ) -> Review:
        self._repository.lock_activation(activation_id)
        basis = self._collect_basis(activation_id, fact_cutoff=fact_cutoff)
        input_refs = basis["input_refs"]
        input_digest = content_digest(input_refs)
        latest = self._repository.get_latest_for_activation(activation_id)
        if latest is not None and _review_matches_basis(
            latest,
            basis=basis,
            input_digest=input_digest,
        ):
            if (
                expected_version is None
                or latest.review_version == expected_version
                or (
                    latest.previous_version == expected_version
                    and latest.revision_reason
                    is ReviewRevisionReason.AUTHORITATIVE_FACTS_CHANGED
                )
            ):
                return latest
            raise OutcomeConflict("REVIEW_VERSION_CONFLICT")
        if expected_version is not None and (
            latest is None or latest.review_version != expected_version
        ):
            raise OutcomeConflict("REVIEW_VERSION_CONFLICT")
        version = 1 if latest is None else latest.review_version + 1
        review_id = review_id_for_activation(self._environment_id, activation_id)
        fields: dict[str, Any] = {
            "review_id": review_id,
            "review_version": version,
            "environment_id": self._environment_id,
            "activation_id": activation_id,
            "previous_version": latest.review_version if latest else None,
            "revision_reason": (
                ReviewRevisionReason.INITIAL_DERIVATION
                if latest is None
                else ReviewRevisionReason.AUTHORITATIVE_FACTS_CHANGED
            ),
            "status": ReviewStatus.DRAFT,
            "primary_result": basis["primary_result"],
            "fact_cutoff": fact_cutoff,
            "input_refs": input_refs,
            "input_digest": input_digest,
            "account_result": basis["account_result"],
            "open_responsibilities": basis["open_responsibilities"],
            "evaluations": basis["evaluations"],
            "evidence_purpose": basis["evidence_purpose"],
            "created_at": observed_at,
        }
        review = Review(**fields, content_digest=content_digest(fields))
        self._repository.insert_review(review)
        return review

    def complete_activation_review(
        self,
        review_id: str,
        *,
        expected_version: int,
        conclusion: ReviewClassification,
        note: str,
        observed_at: datetime,
    ) -> Review:
        current = self._repository.get_review(review_id, expected_version)
        if not isinstance(conclusion, ReviewClassification):
            raise ValueError("REVIEW_CLASSIFICATION_NOT_SUBMITTABLE")
        if (
            conclusion in REVIEW_CLASSIFICATIONS_REQUIRING_REASON
            and not note.strip()
        ):
            raise ValueError("REVIEW_CLASSIFICATION_REASON_REQUIRED")
        if (
            conclusion is ReviewClassification.NO_TRADE
            and current.primary_result is not PrimaryResult.NO_ACTION
        ):
            raise ValueError("REVIEW_NO_TRADE_REQUIRES_NO_ACTION")
        if (
            conclusion
            in {
                ReviewClassification.USABLE_SAMPLE,
                ReviewClassification.VALIDATION_TRADE,
            }
            and current.primary_result
            not in {PrimaryResult.COMPLETED, PrimaryResult.PARTIAL}
        ):
            raise ValueError(
                "REVIEW_VALIDATION_TRADE_REQUIRES_COMPLETED_TRADE"
                if conclusion is ReviewClassification.VALIDATION_TRADE
                else "REVIEW_USABLE_SAMPLE_REQUIRES_COMPLETED_TRADE"
            )
        normalized = _owner_conclusion(conclusion, note)
        self._repository.lock_activation(current.activation_id)
        latest = self._repository.get_latest_for_activation(current.activation_id)
        if (
            latest is not None
            and latest.review_id == review_id
            and latest.previous_version == expected_version
            and latest.revision_reason
            is ReviewRevisionReason.OWNER_EVALUATION_CHANGED
            and latest.evaluations == normalized
            and latest.input_digest == current.input_digest
        ):
            return latest
        if (
            latest is None
            or latest.review_id != review_id
            or latest.review_version != expected_version
        ):
            raise OutcomeConflict("REVIEW_VERSION_CONFLICT")
        if (
            latest.status is ReviewStatus.COMPLETE
            and latest.evaluations == normalized
        ):
            return latest
        completed = _append_review_version(
            latest,
            status=ReviewStatus.COMPLETE,
            revision_reason=ReviewRevisionReason.OWNER_EVALUATION_CHANGED,
            evaluations=normalized,
            created_at=observed_at,
        )
        self._repository.insert_review(completed)
        return completed

    def read_review(self, review_id: str) -> dict[str, Any]:
        versions = self._repository.list_review_versions(review_id)
        return {
            "review": versions[0].model_dump(mode="json"),
            "versions": [
                review.model_dump(mode="json")
                for review in versions
            ],
        }

    def list_reviews(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in self._repository.list_reviews()]

    def list_evidence_reviews(self) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in self._repository.list_evidence_reviews()
        ]

    def create_stage_review(
        self,
        *,
        idempotency_key: str,
        title: str,
        range_start: datetime,
        range_end: datetime,
        problem_analysis: str,
        improvement_plan: str,
        creator_kind: StageReviewCreator,
        observed_at: datetime,
    ) -> StageReview:
        normalized_request = {
            "environment_id": self._environment_id,
            "title": title.strip(),
            "range_start": range_start,
            "range_end": range_end,
            "problem_analysis": problem_analysis.strip(),
            "improvement_plan": improvement_plan.strip(),
            "creator_kind": creator_kind,
        }
        if range_start > range_end:
            raise ValueError("STAGE_REVIEW_RANGE_INVALID")
        if not normalized_request["title"]:
            raise ValueError("STAGE_REVIEW_TITLE_REQUIRED")
        if not normalized_request["problem_analysis"]:
            raise ValueError("STAGE_REVIEW_PROBLEM_ANALYSIS_REQUIRED")
        if not normalized_request["improvement_plan"]:
            raise ValueError("STAGE_REVIEW_IMPROVEMENT_PLAN_REQUIRED")
        request_digest = content_digest(normalized_request)
        existing = self._repository.get_stage_review_by_idempotency_key(
            idempotency_key
        )
        if existing is not None:
            if existing.request_digest == request_digest:
                return existing
            raise OutcomeConflict("STAGE_REVIEW_IDEMPOTENCY_CONFLICT")

        source_reviews = sorted(
            (
                review
                for review in self._repository.list_reviews()
                if range_start <= review.fact_cutoff <= range_end
            ),
            key=lambda review: (
                review.fact_cutoff,
                review.review_id,
                review.review_version,
            ),
        )
        if not source_reviews:
            raise ValueError("STAGE_REVIEW_SOURCE_EMPTY")
        source_review_refs = [
            {
                "review_id": review.review_id,
                "review_version": review.review_version,
                "activation_id": review.activation_id,
                "input_digest": review.input_digest,
                "fact_cutoff": review.fact_cutoff.isoformat(),
            }
            for review in source_reviews
        ]
        fields: dict[str, Any] = {
            "stage_review_id": stage_review_id_for_request(
                self._environment_id,
                idempotency_key,
            ),
            "environment_id": self._environment_id,
            "idempotency_key": idempotency_key,
            "request_digest": request_digest,
            "title": normalized_request["title"],
            "range_start": range_start,
            "range_end": range_end,
            "source_review_refs": source_review_refs,
            "metrics_snapshot": _stage_review_metrics(source_reviews),
            "problem_analysis": normalized_request["problem_analysis"],
            "improvement_plan": normalized_request["improvement_plan"],
            "creator_kind": creator_kind,
            "created_at": observed_at,
        }
        review = StageReview(**fields, content_digest=content_digest(fields))
        if self._repository.insert_stage_review(review):
            return review
        replay = self._repository.get_stage_review_by_idempotency_key(
            idempotency_key
        )
        if replay is not None and replay.request_digest == request_digest:
            return replay
        raise OutcomeConflict("STAGE_REVIEW_IDEMPOTENCY_CONFLICT")

    def list_stage_reviews(self) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in self._repository.list_stage_reviews()
        ]

    def _collect_basis(
        self, activation_id: str, *, fact_cutoff: datetime
    ) -> dict[str, Any]:
        activation = self._connection.execute(
            """
            SELECT activation_id, plan_version_ref, environment_kind, authority_class,
                   account_ref, instrument_ref, decision_basis_ref, lifecycle, run_state,
                   protection_state,
                   responsibility_owner, takeover_scope, closure_digest, result_ref,
                   created_at, updated_at, state_version, direction,
                   position_alignment
            FROM halpha.plan_activation
            WHERE environment_id = %s AND activation_id = %s
            """,
            (self._environment_id, activation_id),
        ).fetchone()
        if activation is None:
            raise OutcomeConflict("ACTIVATION_NOT_FOUND")
        if str(activation[7]) != "COMPLETED":
            raise OutcomeConflict("REVIEW_ACTIVATION_NOT_COMPLETE")
        plan = self._connection.execute(
            """
            SELECT plan_version_id, content_digest, fixed_at
            FROM halpha.trade_plan_version
            WHERE environment_id = %s AND plan_version_id = %s
            """,
            (self._environment_id, activation[1]),
        ).fetchone()
        events = self._connection.execute(
            """
            SELECT plan_event_id, content_digest, created_at
            FROM halpha.plan_event
            WHERE environment_id = %s AND activation_id = %s
            ORDER BY created_at, plan_event_id
            """,
            (self._environment_id, activation_id),
        ).fetchall()
        actions = self._connection.execute(
            """
            SELECT execution_action_id, state_version, state, state_digest,
                   closure_evidence_digest, updated_at, action_kind,
                   call_started_at
            FROM halpha.execution_action
            WHERE environment_id = %s AND activation_id = %s
            ORDER BY created_at, execution_action_id
            """,
            (self._environment_id, activation_id),
        ).fetchall()
        facts = self._connection.execute(
            """
            SELECT venue_fact_id, schema_version, kind, content_digest, cutoff,
                   source_time, payload, action_ref, impact_scope,
                   attribution_class
            FROM halpha.venue_fact
            WHERE environment_id = %s AND cutoff <= %s
              AND (
                activation_ref = %s
                OR (
                  attribution_class IS NULL
                  AND impact_scope ->> 'account_episode_activation_id' = %s
                )
              )
            ORDER BY cutoff, venue_fact_id
            """,
            (self._environment_id, fact_cutoff, activation_id, activation_id),
        ).fetchall()
        commands = self._connection.execute(
            """
            SELECT c.command_id, c.content_digest, r.receipt_id, r.state_version,
                   r.content_digest, r.updated_at
            FROM halpha.command AS c
            JOIN halpha.receipt AS r
              ON r.environment_id = c.environment_id AND r.command_id = c.command_id
            WHERE c.environment_id = %s AND c.target_kind = 'PLAN_ACTIVATION'
              AND c.target_ref = %s
            ORDER BY c.submitted_at, c.command_id
            """,
            (self._environment_id, activation_id),
        ).fetchall()
        input_refs = {
            "activation": {
                "activation_id": str(activation[0]),
                "state_version": int(activation[16]),
                "closure_digest": str(activation[12]),
            },
            "plan_version": (
                {
                    "plan_version_id": str(plan[0]),
                    "content_digest": str(plan[1]),
                    "fixed_at": plan[2].isoformat(),
                }
                if plan is not None
                else {"plan_version_id": str(activation[1]), "missing": True}
            ),
            "plan_events": [
                {"plan_event_id": str(row[0]), "content_digest": str(row[1]), "at": row[2].isoformat()}
                for row in events
            ],
            "execution_actions": [
                {
                    "execution_action_id": str(row[0]),
                    "state_version": int(row[1]),
                    "state_digest": str(row[3]),
                    "closure_evidence_digest": str(row[4]) if row[4] else None,
                    "at": row[5].isoformat(),
                }
                for row in actions
            ],
            "venue_facts": [
                {
                    "venue_fact_id": str(row[0]),
                    "schema_version": int(row[1]),
                    "kind": str(row[2]),
                    "content_digest": str(row[3]),
                    "cutoff": row[4].isoformat(),
                }
                for row in facts
            ],
            "commands_and_receipts": [
                {
                    "command_id": str(row[0]),
                    "command_digest": str(row[1]),
                    "receipt_id": str(row[2]),
                    "receipt_version": int(row[3]),
                    "receipt_digest": str(row[4]),
                    "at": row[5].isoformat(),
                }
                for row in commands
            ],
        }
        states = {str(row[2]) for row in actions}
        open_action_refs = [
            str(row[0]) for row in actions if str(row[2]) not in TERMINAL_ACTION_STATES
        ]
        unknown_action_refs = _unknown_result_action_refs(actions)
        takeover = str(activation[10]) == "USER" or activation[11] is not None
        has_fill = any(str(row[2]) == "FILL" for row in facts)
        has_external_closure = any(
            account_result_role(row[8]) is not None for row in facts
        )
        position_alignment = (
            activation[18]
            if len(activation) > 18 and isinstance(activation[18], dict)
            else None
        )
        if takeover:
            primary_result = PrimaryResult.HANDED_OVER
        elif unknown_action_refs:
            primary_result = PrimaryResult.RESULT_UNKNOWN
        elif open_action_refs:
            primary_result = PrimaryResult.PARTIAL
        elif not actions or (states <= {"NOT_SUBMITTED", "CLOSED"} and not has_fill):
            primary_result = PrimaryResult.NO_ACTION
        else:
            primary_result = PrimaryResult.COMPLETED
        missing_refs = ["plan_version"] if plan is None else []
        evaluations = _draft_evaluations()
        trade_result = summarize_trade_result(
            direction=str(activation[17]),
            action_kinds={str(row[0]): str(row[6]) for row in actions},
            facts=(
                {
                    "kind": str(row[2]),
                    "payload": dict(row[6]),
                    "action_ref": str(row[7]) if row[7] is not None else None,
                    "source_time": row[5].isoformat() if row[5] is not None else None,
                    "result_role": account_result_role(row[8]),
                }
                for row in facts
            ),
            opening_position_quantity=(
                str(position_alignment["requested_reduction_quantity"])
                if position_alignment is not None
                and position_alignment.get("requested_reduction_quantity") is not None
                else None
            ),
        )
        return {
            "input_refs": input_refs,
            "primary_result": primary_result,
            "account_result": {
                "classification": (
                    "UNKNOWN"
                    if (
                        missing_refs
                        or unknown_action_refs
                        or primary_result is PrimaryResult.RESULT_UNKNOWN
                    )
                    else (
                        "NO_EXTERNAL_CHANGE"
                        if not has_fill
                        else (
                            "EXTERNAL_POSITION_DISPOSITION"
                            if position_alignment is not None
                            else (
                                "ACCOUNT_FACTS_WITH_EXTERNAL_CLOSURE"
                                if has_external_closure
                                else "ATTRIBUTED_FACTS_AVAILABLE"
                            )
                        )
                    )
                ),
                "venue_fact_refs": [str(row[0]) for row in facts],
                "missing_refs": missing_refs,
                "trade_result": trade_result,
            },
            "open_responsibilities": {
                "execution_action_refs": open_action_refs,
                "unknown_action_refs": unknown_action_refs,
                "responsibility_owner": str(activation[10]),
                "takeover_scope": dict(activation[11]) if activation[11] else None,
            },
            "evaluations": evaluations,
            "evidence_purpose": (
                EvidencePurpose.SYSTEM_MECHANISM_EVIDENCE
                if str(activation[2]) == "DEMO"
                else EvidencePurpose.LIVE_ACTIVATION_REVIEW
            ),
        }


def _unknown_result_action_refs(actions: list[Any]) -> list[str]:
    """Return actions whose venue result is still unknown at the review cutoff.

    Handover closes Halpha's mutation responsibility, not a venue call that had
    already started. Those called actions remain result-unknown until venue
    facts prove their terminal outcome.
    """

    return [
        str(row[0])
        for row in actions
        if (
            str(row[2]) in UNKNOWN_ACTION_STATES
            or (
                str(row[2]) == "HANDED_OVER"
                and len(row) > 7
                and row[7] is not None
            )
        )
    ]


def _draft_evaluations() -> dict[str, dict[str, Any]]:
    return {}


def _decimal_value(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _stage_review_metrics(reviews: list[Review]) -> dict[str, Any]:
    reliable: list[tuple[Review, Decimal, Decimal, Decimal | None]] = []
    pending_evaluation_count = 0
    for review in reviews:
        if review.status is ReviewStatus.DRAFT:
            pending_evaluation_count += 1
        result = review.account_result.get("trade_result")
        if not isinstance(result, dict):
            continue
        net_pnl = _decimal_value(result.get("net_pnl"))
        commission = _decimal_value(result.get("commission"))
        entry_notional = _decimal_value(result.get("entry_notional"))
        if (
            result.get("calculation_complete") is not True
            or result.get("closed") is not True
            or net_pnl is None
            or commission is None
            or commission < 0
        ):
            continue
        reliable.append((review, net_pnl, commission, entry_notional))

    net_pnl = sum((item[1] for item in reliable), Decimal("0"))
    commissions = sum((item[2] for item in reliable), Decimal("0"))
    wins = sum(1 for item in reliable if item[1] > 0)
    entry_values = [item[3] for item in reliable]
    complete_entry_notional = (
        bool(reliable)
        and all(value is not None and value > 0 for value in entry_values)
    )
    total_entry_notional = (
        sum((value for value in entry_values if value is not None), Decimal("0"))
        if complete_entry_notional
        else None
    )
    notional_return_percent = (
        net_pnl / total_entry_notional * Decimal("100")
        if total_entry_notional is not None and total_entry_notional > 0
        else None
    )
    streak_kind = "NONE"
    streak_count = 0
    for _review, result, _commission, _entry in reversed(reliable):
        item_kind = "WIN" if result > 0 else "LOSS" if result < 0 else "NONE"
        if item_kind == "NONE":
            break
        if streak_kind == "NONE":
            streak_kind = item_kind
        if item_kind != streak_kind:
            break
        streak_count += 1
    return {
        "review_count": len(reviews),
        "reliable_trade_count": len(reliable),
        "pending_evaluation_count": pending_evaluation_count,
        "net_pnl": str(net_pnl),
        "commission": str(commissions),
        "total_entry_notional": (
            str(total_entry_notional)
            if total_entry_notional is not None
            else None
        ),
        "notional_return_percent": (
            str(notional_return_percent)
            if notional_return_percent is not None
            else None
        ),
        "wins": wins,
        "win_rate_percent": (
            str(Decimal(wins) / Decimal(len(reliable)) * Decimal("100"))
            if reliable
            else None
        ),
        "current_streak_kind": streak_kind,
        "current_streak_count": streak_count,
    }


def _review_matches_basis(
    review: Review,
    *,
    basis: dict[str, Any],
    input_digest: str,
) -> bool:
    """Reuse a review only when its facts and current derived result still match."""

    return (
        review.input_digest == input_digest
        and review.primary_result == basis["primary_result"]
        and review.account_result == basis["account_result"]
        and review.open_responsibilities == basis["open_responsibilities"]
        and review.evidence_purpose == basis["evidence_purpose"]
    )


def _owner_conclusion(
    conclusion: ReviewClassification,
    note: str,
) -> dict[str, dict[str, Any]]:
    return {
        OWNER_CONCLUSION_KEY: {
            "result": conclusion.value,
            "reason": note.strip(),
            "evidence_refs": [],
        }
    }


def _append_review_version(
    review: Review,
    *,
    status: ReviewStatus,
    revision_reason: ReviewRevisionReason,
    created_at: datetime,
    evaluations: dict[str, dict[str, Any]] | None = None,
) -> Review:
    fields = review.model_dump(mode="python", exclude={"content_digest"})
    fields["previous_version"] = review.review_version
    fields["review_version"] = review.review_version + 1
    fields["revision_reason"] = revision_reason
    fields["status"] = status
    fields["created_at"] = created_at
    if evaluations is not None:
        fields["evaluations"] = evaluations
    return Review(**fields, content_digest=content_digest(fields))
