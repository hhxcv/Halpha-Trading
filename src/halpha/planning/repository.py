"""TRADEPLAN's private PostgreSQL writer and read boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from halpha.domain_values import content_digest

from halpha.planning.models import (
    PERSISTED_HISTORY_CONTEXT_KEY,
    PlanActivation,
    PlanDecisionContext,
    PlanEvent,
    PositionAlignmentSpec,
    TradePlanContent,
    TradePlanDraft,
    TradePlanVersion,
)
from halpha.planning.order_schedule import (
    OrderSchedulePreview,
    OrderScheduleSpec,
    validate_order_schedule_snapshot,
)
from halpha.planning.registry import FixedDecisionBasis


_FIXED_DECISION_BASIS_ADAPTER = TypeAdapter(FixedDecisionBasis)


def _fixed_decision_basis(value: object) -> FixedDecisionBasis:
    return _FIXED_DECISION_BASIS_ADAPTER.validate_python(value)


def load_persisted_order_schedule_spec(
    raw_spec: object | None,
    persisted_digest: object | None,
) -> OrderScheduleSpec | None:
    """Validate current and legacy persisted schedule encodings."""

    if (raw_spec is None) != (persisted_digest is None):
        raise ValueError("ORDER_SCHEDULE_SPEC_CORRUPT")
    if raw_spec is None:
        return None
    try:
        schedule_spec = OrderScheduleSpec.model_validate(raw_spec)
        accepted_digests = {
            content_digest(raw_spec),
            # Versions written before the JSON-payload digest fix hashed the
            # model's Python datetime representation instead of its JSON form.
            content_digest(schedule_spec),
        }
    except (TypeError, ValueError):
        raise ValueError("ORDER_SCHEDULE_SPEC_CORRUPT") from None
    if str(persisted_digest) not in accepted_digests:
        raise ValueError("ORDER_SCHEDULE_SPEC_CORRUPT")
    return schedule_spec


class PlanningConflict(RuntimeError):
    pass


class PostgreSQLPlanningRepository:
    """Only this repository writes the four TRADEPLAN record families."""

    def __init__(self, connection: Connection[Any], environment_id: str) -> None:
        self._connection = connection
        self._environment_id = environment_id

    def _require_environment(self, environment_id: str) -> None:
        if environment_id != self._environment_id:
            raise PlanningConflict("PLAN_ENVIRONMENT_MISMATCH")

    def save_draft(self, draft: TradePlanDraft, *, expected_version: int | None) -> None:
        self._require_environment(draft.environment_id)
        if expected_version is None:
            cursor = self._connection.execute(
                """
                INSERT INTO halpha.trade_plan_draft (
                    plan_id, environment_id, draft_version, content_digest, content, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    draft.plan_id,
                    draft.environment_id,
                    draft.draft_version,
                    draft.content_digest,
                    Jsonb(draft.content.model_dump(mode="json")),
                    draft.updated_at,
                ),
            )
        else:
            if draft.draft_version != expected_version + 1:
                raise PlanningConflict("PLAN_VERSION_CONFLICT")
            cursor = self._connection.execute(
                """
                UPDATE halpha.trade_plan_draft
                SET draft_version = %s, content_digest = %s, content = %s, updated_at = %s
                WHERE environment_id = %s AND plan_id = %s AND draft_version = %s
                """,
                (
                    draft.draft_version,
                    draft.content_digest,
                    Jsonb(draft.content.model_dump(mode="json")),
                    draft.updated_at,
                    draft.environment_id,
                    draft.plan_id,
                    expected_version,
                ),
            )
        if cursor.rowcount != 1:
            raise PlanningConflict("PLAN_VERSION_CONFLICT")

    def get_draft(self, plan_id: str, *, for_update: bool = False) -> TradePlanDraft:
        suffix = " FOR UPDATE" if for_update else ""
        row = self._connection.execute(
            """
            SELECT plan_id, environment_id, draft_version, content_digest, content, updated_at
            FROM halpha.trade_plan_draft
            WHERE environment_id = %s AND plan_id = %s
            """ + suffix,
            (self._environment_id, plan_id),
        ).fetchone()
        if row is None:
            raise PlanningConflict("PLAN_NOT_FOUND")
        return TradePlanDraft(
            plan_id=str(row[0]),
            environment_id=str(row[1]),
            draft_version=int(row[2]),
            content_digest=str(row[3]),
            content=TradePlanContent.model_validate(
                row[4],
                context={PERSISTED_HISTORY_CONTEXT_KEY: True},
            ),
            updated_at=row[5],
        )

    def has_fixed_version(self, plan_id: str) -> bool:
        row = self._connection.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM halpha.trade_plan_version
                WHERE environment_id = %s AND plan_id = %s
            )
            """,
            (self._environment_id, plan_id),
        ).fetchone()
        return bool(row and row[0])

    def delete_draft(self, plan_id: str, *, expected_version: int) -> None:
        cursor = self._connection.execute(
            """
            DELETE FROM halpha.trade_plan_draft
            WHERE environment_id = %s AND plan_id = %s AND draft_version = %s
            """,
            (self._environment_id, plan_id, expected_version),
        )
        if cursor.rowcount != 1:
            raise PlanningConflict("PLAN_VERSION_CONFLICT")

    def insert_version(self, version: TradePlanVersion) -> None:
        self._require_environment(version.environment_id)
        basis = version.decision_basis
        schedule_spec = version.order_schedule_spec
        persisted_schedule_spec = (
            schedule_spec.model_dump(mode="json")
            if schedule_spec is not None
            else None
        )
        position_alignment = getattr(version, "position_alignment", None)
        decision_context = getattr(version, "decision_context", None)
        self._connection.execute(
            """
            INSERT INTO halpha.trade_plan_version (
                plan_version_id, environment_id, plan_id, fixed_at,
                decision_basis_ref, product_build_id, parameter_schema_version,
                parameters, parameter_digest, account_ref, venue_ref, instrument_ref,
                direction, max_margin, max_notional, max_allowed_loss, terms,
                content_digest, fixed_decision_basis,
                order_schedule_spec, order_schedule_spec_digest,
                position_alignment, position_alignment_digest
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                version.plan_version_id,
                version.environment_id,
                version.plan_id,
                version.fixed_at,
                basis.decision_basis_ref,
                basis.product_build_id,
                basis.parameter_schema_version,
                Jsonb(basis.normalized_parameters),
                basis.parameter_digest,
                version.account_ref,
                version.venue_ref,
                version.instrument_ref,
                version.direction.value,
                version.requested_limits.max_margin,
                version.requested_limits.max_notional,
                version.requested_limits.max_allowed_loss,
                Jsonb(
                    {
                        **version.terms,
                        **(
                            {"plan_name": version.plan_name}
                            if version.plan_name is not None
                            else {}
                        ),
                        **(
                            {"created_at": version.created_at.isoformat()}
                            if version.created_at is not None
                            else {}
                        ),
                        **(
                            {"creator_kind": version.creator_kind.value}
                            if version.creator_kind is not None
                            else {}
                        ),
                        **(
                            {
                                "decision_context": (
                                    decision_context.model_dump(
                                        mode="json",
                                        exclude_none=True,
                                    )
                                )
                            }
                            if decision_context is not None
                            else {}
                        ),
                        "target_exposure": version.target_exposure,
                        "valid_from": version.valid_from.isoformat(),
                        "valid_until": version.valid_until.isoformat(),
                        "allowed_actions": sorted(version.allowed_actions),
                    }
                ),
                version.content_digest,
                Jsonb(basis.model_dump(mode="json")),
                (
                    Jsonb(persisted_schedule_spec)
                    if persisted_schedule_spec is not None
                    else None
                ),
                (
                    content_digest(persisted_schedule_spec)
                    if persisted_schedule_spec is not None
                    else None
                ),
                (
                    Jsonb(position_alignment.model_dump(mode="json"))
                    if position_alignment is not None
                    else None
                ),
                (
                    content_digest(position_alignment)
                    if position_alignment is not None
                    else None
                ),
            ),
        )

    def get_version(self, plan_version_id: str) -> TradePlanVersion:
        row = self._connection.execute(
            """
            SELECT plan_version_id, plan_id, environment_id, fixed_at,
                   fixed_decision_basis, account_ref, venue_ref, instrument_ref,
                   direction, max_margin, max_notional, max_allowed_loss, terms,
                   content_digest, order_schedule_spec, order_schedule_spec_digest,
                   position_alignment, position_alignment_digest
            FROM halpha.trade_plan_version
            WHERE environment_id = %s AND plan_version_id = %s
            """,
            (self._environment_id, plan_version_id),
        ).fetchone()
        if row is None:
            raise PlanningConflict("PLAN_VERSION_NOT_FOUND")
        terms = dict(row[12])
        created_at = terms.pop("created_at", None)
        decision_context_payload = terms.pop("decision_context", None)
        try:
            decision_context = (
                PlanDecisionContext.model_validate(decision_context_payload)
                if decision_context_payload is not None
                else None
            )
        except ValueError:
            raise PlanningConflict("PLAN_DECISION_CONTEXT_CORRUPT") from None
        try:
            schedule_spec = load_persisted_order_schedule_spec(row[14], row[15])
        except ValueError:
            raise PlanningConflict("ORDER_SCHEDULE_SPEC_CORRUPT") from None
        alignment = (
            PositionAlignmentSpec.model_validate(row[16])
            if row[16] is not None
            else None
        )
        if (alignment is None) != (row[17] is None) or (
            alignment is not None
            and content_digest(alignment) != str(row[17])
        ):
            raise PlanningConflict("POSITION_ALIGNMENT_CORRUPT")
        return TradePlanVersion.model_validate(
            {
                "plan_version_id": str(row[0]),
                "plan_id": str(row[1]),
                "environment_id": str(row[2]),
                "fixed_at": row[3],
                "plan_name": terms.pop("plan_name", None),
                "created_at": (
                    datetime.fromisoformat(str(created_at)) if created_at else None
                ),
                "creator_kind": terms.pop("creator_kind", None),
                "decision_context": decision_context,
                "decision_basis": _fixed_decision_basis(row[4]),
                "order_schedule_spec": schedule_spec,
                "position_alignment": alignment,
                "account_ref": str(row[5]),
                "venue_ref": str(row[6]),
                "instrument_ref": str(row[7]),
                "direction": str(row[8]),
                "target_exposure": str(terms.pop("target_exposure")),
                "requested_limits": {
                    "max_margin": str(row[9]),
                    "max_notional": str(row[10]),
                    "max_allowed_loss": str(row[11]),
                },
                "valid_from": datetime.fromisoformat(str(terms.pop("valid_from"))),
                "valid_until": datetime.fromisoformat(str(terms.pop("valid_until"))),
                "allowed_actions": frozenset(terms.pop("allowed_actions")),
                "terms": terms,
                "content_digest": str(row[13]),
            },
            context={PERSISTED_HISTORY_CONTEXT_KEY: True},
        )

    def insert_activation(self, activation: PlanActivation) -> None:
        self._require_environment(activation.environment_id)
        self._connection.execute(
            """
            INSERT INTO halpha.plan_activation (
                activation_id, environment_id, environment_kind, authority_class,
                plan_version_ref, account_ref, instrument_ref, direction,
                decision_basis_ref, framework_strategy_id,
                target_exposure, order_schedule_snapshot,
                order_schedule_snapshot_digest, position_alignment,
                position_alignment_digest,
                lifecycle, run_state, pause_reason, paused_at, reconciliation_digest,
                current_resume_command_ref, has_entry_fill, entry_opportunity_consumed,
                responsibility_owner, state_version, rule_state, pending_action_digest,
                protection_state, takeover_scope, latest_venue_cutoff, closure_digest,
                result_ref, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s
            )
            """,
            (
                activation.activation_id,
                activation.environment_id,
                activation.environment_kind.value,
                activation.authority_class.value,
                activation.plan_version_ref,
                activation.account_ref,
                activation.instrument_ref,
                activation.direction.value,
                activation.decision_basis_ref,
                activation.framework_strategy_id,
                activation.target_exposure,
                (
                    Jsonb(activation.order_schedule_snapshot.model_dump(mode="json"))
                    if activation.order_schedule_snapshot is not None
                    else None
                ),
                (
                    activation.order_schedule_snapshot.schedule_digest
                    if activation.order_schedule_snapshot is not None
                    else None
                ),
                (
                    Jsonb(activation.position_alignment.model_dump(mode="json"))
                    if activation.position_alignment is not None
                    else None
                ),
                (
                    content_digest(activation.position_alignment)
                    if activation.position_alignment is not None
                    else None
                ),
                activation.lifecycle.value,
                activation.run_state.value,
                activation.pause_reason,
                activation.paused_at,
                activation.reconciliation_digest,
                activation.current_resume_command_ref,
                activation.has_entry_fill,
                activation.entry_opportunity_consumed,
                activation.responsibility_owner,
                activation.state_version,
                Jsonb(activation.rule_state),
                activation.pending_action_digest,
                activation.protection_state.value,
                Jsonb(activation.takeover_scope) if activation.takeover_scope is not None else None,
                activation.latest_venue_cutoff,
                activation.closure_digest,
                activation.result_ref,
                activation.created_at,
                activation.updated_at,
            ),
        )

    def lock_and_list_open_instrument_activations(
        self,
        *,
        account_ref: str,
        instrument_ref: str,
    ) -> tuple[PlanActivation, ...]:
        """Serialize activation admission for one venue net-position scope."""

        lock_scope = (
            f"{self._environment_id}\x1f{account_ref}\x1f{instrument_ref}"
        )
        self._connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (lock_scope,),
        )
        rows = self._connection.execute(
            """
            SELECT activation_id
            FROM halpha.plan_activation
            WHERE environment_id = %s
              AND account_ref = %s
              AND instrument_ref = %s
              AND lifecycle <> 'COMPLETED'
            ORDER BY created_at, activation_id
            """,
            (self._environment_id, account_ref, instrument_ref),
        ).fetchall()
        return tuple(self.get_activation(str(row[0])) for row in rows)

    def get_activation(
        self, activation_id: str, *, for_update: bool = False
    ) -> PlanActivation:
        suffix = " FOR UPDATE" if for_update else ""
        row = self._connection.execute(
            """
            SELECT activation_id, environment_id, environment_kind, authority_class,
                   plan_version_ref, account_ref, instrument_ref, direction,
                   decision_basis_ref, framework_strategy_id, target_exposure,
                   order_schedule_snapshot, order_schedule_snapshot_digest,
                   position_alignment, position_alignment_digest,
                   lifecycle, run_state,
                   pause_reason, paused_at, reconciliation_digest,
                   current_resume_command_ref, has_entry_fill,
                   entry_opportunity_consumed, responsibility_owner, state_version,
                   rule_state, pending_action_digest, protection_state,
                   takeover_scope, latest_venue_cutoff, closure_digest, result_ref,
                   created_at, updated_at
            FROM halpha.plan_activation
            WHERE environment_id = %s AND activation_id = %s
            """ + suffix,
            (self._environment_id, activation_id),
        ).fetchone()
        if row is None:
            raise PlanningConflict("ACTIVATION_NOT_FOUND")
        schedule_snapshot = (
            OrderSchedulePreview.model_validate(row[11])
            if row[11] is not None
            else None
        )
        if (schedule_snapshot is None) != (row[12] is None):
            raise PlanningConflict("ORDER_SCHEDULE_SNAPSHOT_CORRUPT")
        if schedule_snapshot is not None:
            try:
                validate_order_schedule_snapshot(schedule_snapshot)
            except ValueError:
                raise PlanningConflict("ORDER_SCHEDULE_SNAPSHOT_CORRUPT") from None
            if schedule_snapshot.schedule_digest != str(row[12]):
                raise PlanningConflict("ORDER_SCHEDULE_SNAPSHOT_CORRUPT")
        alignment = (
            PositionAlignmentSpec.model_validate(row[13])
            if row[13] is not None
            else None
        )
        if (alignment is None) != (row[14] is None) or (
            alignment is not None
            and content_digest(alignment) != str(row[14])
        ):
            raise PlanningConflict("POSITION_ALIGNMENT_CORRUPT")
        return PlanActivation(
            activation_id=str(row[0]),
            environment_id=str(row[1]),
            environment_kind=str(row[2]),
            authority_class=str(row[3]),
            plan_version_ref=str(row[4]),
            account_ref=str(row[5]),
            instrument_ref=str(row[6]),
            direction=str(row[7]),
            decision_basis_ref=str(row[8]),
            framework_strategy_id=str(row[9]),
            target_exposure=str(row[10]),
            order_schedule_snapshot=schedule_snapshot,
            position_alignment=alignment,
            lifecycle=str(row[15]),
            run_state=str(row[16]),
            pause_reason=str(row[17]) if row[17] is not None else None,
            paused_at=row[18],
            reconciliation_digest=str(row[19]) if row[19] is not None else None,
            current_resume_command_ref=str(row[20]) if row[20] is not None else None,
            has_entry_fill=bool(row[21]),
            entry_opportunity_consumed=bool(row[22]),
            responsibility_owner=str(row[23]),
            state_version=int(row[24]),
            rule_state=dict(row[25]),
            pending_action_digest=str(row[26]) if row[26] is not None else None,
            protection_state=str(row[27]),
            takeover_scope=dict(row[28]) if row[28] is not None else None,
            latest_venue_cutoff=row[29],
            closure_digest=str(row[30]) if row[30] is not None else None,
            result_ref=str(row[31]) if row[31] is not None else None,
            created_at=row[32],
            updated_at=row[33],
        )

    def list_open_activations(self) -> tuple[PlanActivation, ...]:
        rows = self._connection.execute(
            """
            SELECT activation_id
            FROM halpha.plan_activation
            WHERE environment_id = %s
              AND lifecycle NOT IN ('COMPLETED', 'USER_TAKEOVER')
            ORDER BY created_at, activation_id
            """,
            (self._environment_id,),
        ).fetchall()
        return tuple(self.get_activation(str(row[0])) for row in rows)

    def list_runtime_responsibility_activations(self) -> tuple[PlanActivation, ...]:
        """Return active trading plus takeover identities needing read-only closure."""

        rows = self._connection.execute(
            """
            SELECT activation_id
            FROM halpha.plan_activation
            WHERE environment_id = %s
              AND lifecycle <> 'COMPLETED'
            ORDER BY created_at, activation_id
            """,
            (self._environment_id,),
        ).fetchall()
        return tuple(self.get_activation(str(row[0])) for row in rows)

    def list_account_instrument_activations(
        self,
        *,
        account_ref: str,
        instrument_ref: str,
    ) -> tuple[PlanActivation, ...]:
        """Return attribution history for one venue net-position scope."""

        rows = self._connection.execute(
            """
            SELECT activation_id
            FROM halpha.plan_activation
            WHERE environment_id = %s
              AND account_ref = %s
              AND instrument_ref = %s
            ORDER BY created_at, activation_id
            """,
            (self._environment_id, account_ref, instrument_ref),
        ).fetchall()
        return tuple(self.get_activation(str(row[0])) for row in rows)

    def update_activation(self, activation: PlanActivation, *, expected_version: int) -> None:
        self._require_environment(activation.environment_id)
        cursor = self._connection.execute(
            """
            UPDATE halpha.plan_activation
            SET lifecycle = %s, run_state = %s, pause_reason = %s, paused_at = %s,
                reconciliation_digest = %s, current_resume_command_ref = %s,
                has_entry_fill = %s, entry_opportunity_consumed = %s,
                responsibility_owner = %s, state_version = %s, rule_state = %s,
                pending_action_digest = %s, protection_state = %s, takeover_scope = %s,
                latest_venue_cutoff = %s, closure_digest = %s, result_ref = %s,
                updated_at = %s
            WHERE environment_id = %s AND activation_id = %s AND state_version = %s
            """,
            (
                activation.lifecycle.value,
                activation.run_state.value,
                activation.pause_reason,
                activation.paused_at,
                activation.reconciliation_digest,
                activation.current_resume_command_ref,
                activation.has_entry_fill,
                activation.entry_opportunity_consumed,
                activation.responsibility_owner,
                activation.state_version,
                Jsonb(activation.rule_state),
                activation.pending_action_digest,
                activation.protection_state.value,
                Jsonb(activation.takeover_scope) if activation.takeover_scope is not None else None,
                activation.latest_venue_cutoff,
                activation.closure_digest,
                activation.result_ref,
                activation.updated_at,
                activation.environment_id,
                activation.activation_id,
                expected_version,
            ),
        )
        if cursor.rowcount != 1:
            raise PlanningConflict("PLAN_VERSION_CONFLICT")

    def pause_all_open_for_writer_continuity_loss(self, observed_at: datetime) -> int:
        cursor = self._connection.execute(
            """
            UPDATE halpha.plan_activation
            SET run_state = 'PAUSED', pause_reason = 'WRITER_CONTINUITY_LOST',
                paused_at = %s, reconciliation_digest = NULL,
                current_resume_command_ref = NULL, state_version = state_version + 1,
                updated_at = %s
            WHERE environment_id = %s
              AND lifecycle NOT IN ('COMPLETED', 'USER_TAKEOVER')
              AND run_state <> 'PAUSED'
              AND entry_opportunity_consumed = FALSE
              AND (
                NULLIF(rule_state #>> '{deadlines,entry_valid_until}', '') IS NULL
                OR NULLIF(rule_state #>> '{deadlines,entry_valid_until}', '')::timestamptz > %s
              )
              AND (
                has_entry_fill
                OR pending_action_digest IS NOT NULL
                OR EXISTS (
                  SELECT 1 FROM halpha.execution_action action
                  WHERE action.environment_id = plan_activation.environment_id
                    AND action.activation_id = plan_activation.activation_id
                )
              )
            """,
            (observed_at, observed_at, self._environment_id, observed_at),
        )
        paused_count = int(cursor.rowcount)
        self._connection.execute(
            """
            UPDATE halpha.plan_activation
            SET run_state = 'ACTIVE', pause_reason = NULL, paused_at = NULL,
                reconciliation_digest = NULL, current_resume_command_ref = NULL,
                state_version = state_version + 1, updated_at = %s
            WHERE environment_id = %s
              AND lifecycle = 'RUNNING'
              AND run_state = 'PAUSED'
              AND pause_reason = 'WRITER_CONTINUITY_LOST'
              AND (
                entry_opportunity_consumed = TRUE
                OR (
                  NULLIF(rule_state #>> '{deadlines,entry_valid_until}', '') IS NOT NULL
                  AND NULLIF(rule_state #>> '{deadlines,entry_valid_until}', '')::timestamptz <= %s
                )
                OR (
                  has_entry_fill = FALSE
                  AND pending_action_digest IS NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM halpha.execution_action action
                    WHERE action.environment_id = plan_activation.environment_id
                      AND action.activation_id = plan_activation.activation_id
                  )
                )
              )
            """,
            (observed_at, self._environment_id, observed_at),
        )
        return paused_count

    def find_event_by_source(
        self, activation_id: str, source_identity: str
    ) -> PlanEvent | None:
        row = self._connection.execute(
            """
            SELECT plan_event_id, environment_id, activation_id, rule_id,
                   source_identity, source_cutoff, input_digest, reason_code,
                   condition_judgement, proposed_action, no_action_reason,
                   capital_decision, capital_decision_digest, created_at, content_digest
            FROM halpha.plan_event
            WHERE environment_id = %s AND activation_id = %s AND source_identity = %s
            """,
            (self._environment_id, activation_id, source_identity),
        ).fetchone()
        if row is None:
            return None
        return PlanEvent(
            plan_event_id=str(row[0]),
            environment_id=str(row[1]),
            activation_id=str(row[2]),
            rule_id=str(row[3]),
            source_identity=str(row[4]),
            source_cutoff=row[5],
            input_digest=str(row[6]),
            reason_code=str(row[7]),
            condition_judgement=row[8],
            proposed_action=row[9],
            no_action_reason=row[10],
            capital_decision=dict(row[11]),
            capital_decision_digest=str(row[12]),
            created_at=row[13],
            content_digest=str(row[14]),
        )

    def insert_event(self, event: PlanEvent) -> None:
        self._require_environment(event.environment_id)
        self._connection.execute(
            """
            INSERT INTO halpha.plan_event (
                plan_event_id, environment_id, activation_id, rule_id,
                source_identity, source_cutoff, input_digest, reason_code,
                condition_judgement, proposed_action, no_action_reason,
                capital_decision, capital_decision_digest, created_at, content_digest
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event.plan_event_id,
                event.environment_id,
                event.activation_id,
                event.rule_id,
                event.source_identity,
                event.source_cutoff,
                event.input_digest,
                event.reason_code,
                Jsonb(event.condition_judgement.model_dump(mode="json")) if event.condition_judgement else None,
                Jsonb(event.proposed_action.model_dump(mode="json")) if event.proposed_action else None,
                event.no_action_reason,
                Jsonb(event.capital_decision),
                event.capital_decision_digest,
                event.created_at,
                event.content_digest,
            ),
        )
