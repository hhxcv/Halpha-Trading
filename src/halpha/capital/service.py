"""CAP public application boundary over the one exact-Decimal checker."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import Connection

from halpha.capital.checks import check_action
from halpha.capital.discipline import read_new_risk_discipline
from halpha.capital.models import (
    AccountSystemStopReleaseRequest,
    AccountSystemStopSource,
    ActivationCapitalBoundary,
    ActionCheckInput,
    AuthorityClass,
    CapDecision,
    EnvironmentKind,
    NewRiskDisciplinePolicy,
    NewRiskDisciplineStatus,
    StopCategory,
    StopStateVersion,
)
from halpha.domain_values import content_digest
from halpha.capital.repository import CapitalConflict, PostgreSQLCapitalRepository
from halpha.planning.repository import PostgreSQLPlanningRepository


class CapitalApplicationService:
    def __init__(self, connection: Connection[Any], environment_id: str) -> None:
        self._connection = connection
        self._capital = PostgreSQLCapitalRepository(connection, environment_id)
        self._planning = PostgreSQLPlanningRepository(connection, environment_id)
        self._environment_id = environment_id

    def get_plan_boundary(
        self,
        activation_id: str,
        *,
        for_update: bool = False,
    ) -> ActivationCapitalBoundary:
        activation = self._planning.get_activation(activation_id, for_update=for_update)
        version = self._planning.get_version(activation.plan_version_ref)
        return ActivationCapitalBoundary(
            activation_id=activation.activation_id,
            environment_id=activation.environment_id,
            environment_kind=activation.environment_kind,
            authority_class=activation.authority_class,
            account_ref=activation.account_ref,
            instrument_ref=activation.instrument_ref,
            valid_from=version.valid_from,
            valid_until=version.valid_until,
            allowed_actions=version.allowed_actions,
            max_margin=version.requested_limits.max_margin,
            max_notional=version.requested_limits.max_notional,
            max_allowed_loss=version.requested_limits.max_allowed_loss,
            lifecycle=activation.lifecycle.value,
            responsibility_owner=activation.responsibility_owner,
        )

    def check_current_action(self, action: ActionCheckInput) -> CapDecision:
        boundary = self.get_plan_boundary(action.activation_id, for_update=True)
        stop_states = self._capital.lock_current_stop_states(
            account_ref=action.account_ref,
            activation_id=action.activation_id,
        )
        return check_action(
            action,
            boundary=boundary,
            stop_states=stop_states,
        )

    def new_risk_discipline_status(
        self,
        *,
        account_ref: str,
        policy: NewRiskDisciplinePolicy,
        observed_at: datetime,
        entry_instrument_ref: str | None = None,
        entry_direction: str | None = None,
        is_frozen_scale_in: bool = False,
        lock: bool = False,
    ) -> NewRiskDisciplineStatus:
        """Read the same account-wide admission contract used at activation."""

        return read_new_risk_discipline(
            self._connection,
            environment_id=self._environment_id,
            account_ref=account_ref,
            policy=policy,
            observed_at=observed_at,
            entry_instrument_ref=entry_instrument_ref,
            entry_direction=entry_direction,
            is_frozen_scale_in=is_frozen_scale_in,
            lock=lock,
        )

    def new_risk_allowed(self, activation_id: str) -> bool:
        activation = self._planning.get_activation(activation_id)
        stop_states = self._capital.lock_current_stop_states(
            account_ref=activation.account_ref,
            activation_id=activation.activation_id,
        )
        stopped = {
            category
            for state in stop_states
            for category in state.stopped_categories
        }
        return not bool(
            stopped
            & {StopCategory.NEW_RISK, StopCategory.ALL_EXCHANGE_CHANGES}
        )

    def external_activity_conflict(self, activation_id: str) -> bool:
        activation = self._planning.get_activation(activation_id)
        stop_states = self._capital.lock_current_stop_states(
            account_ref=activation.account_ref,
            activation_id=activation.activation_id,
        )
        return any(
            state.source == "SYSTEM_EXTERNAL_ACTIVITY"
            and StopCategory.NEW_RISK in state.stopped_categories
            for state in stop_states
        )

    def stop_new_risk_for_external_activity(
        self,
        *,
        stop_state_version_id: str,
        environment_kind: EnvironmentKind,
        authority_class: AuthorityClass,
        account_ref: str,
        evidence_digest: str,
        observed_at: datetime,
    ) -> StopStateVersion:
        return self._stop_new_risk_for_account_signal(
            stop_state_version_id=stop_state_version_id,
            environment_kind=environment_kind,
            authority_class=authority_class,
            account_ref=account_ref,
            evidence_digest=evidence_digest,
            reason="EXTERNAL_ACTIVITY_DETECTED",
            source="SYSTEM_EXTERNAL_ACTIVITY",
            resolution_requirement=(
                "EXTERNAL_ACTIVITY_RESOLUTION_OR_USER_TAKEOVER"
            ),
            observed_at=observed_at,
        )

    def stop_new_risk_for_attributed_action_anomaly(
        self,
        *,
        stop_state_version_id: str,
        environment_kind: EnvironmentKind,
        authority_class: AuthorityClass,
        account_ref: str,
        evidence_digest: str,
        observed_at: datetime,
    ) -> StopStateVersion:
        return self._stop_new_risk_for_account_signal(
            stop_state_version_id=stop_state_version_id,
            environment_kind=environment_kind,
            authority_class=authority_class,
            account_ref=account_ref,
            evidence_digest=evidence_digest,
            reason="ATTRIBUTED_ACTION_ANOMALY_DETECTED",
            source="SYSTEM_ATTRIBUTED_ACTION_ANOMALY",
            resolution_requirement=(
                "ATTRIBUTED_ACTION_RECONCILIATION_OR_USER_TAKEOVER"
            ),
            observed_at=observed_at,
        )

    def _stop_new_risk_for_account_signal(
        self,
        *,
        stop_state_version_id: str,
        environment_kind: EnvironmentKind,
        authority_class: AuthorityClass,
        account_ref: str,
        evidence_digest: str,
        reason: str,
        source: str,
        resolution_requirement: str,
        observed_at: datetime,
    ) -> StopStateVersion:
        current = self._capital.lock_current_account_stop_state(
            account_ref=account_ref,
        )
        # One unresolved period needs at most one attributed-anomaly version
        # and one upgrade to an external-activity conflict. Canonical venue
        # facts retain every individual evidence item without recursively
        # expanding stop-state JSON.
        if current is not None and current.source == "SYSTEM_EXTERNAL_ACTIVITY":
            return current
        if current is not None and current.source == source:
            return current
        stopped_categories = frozenset(
            set(current.stopped_categories if current is not None else ())
            | {StopCategory.NEW_RISK}
        )
        retained_rules = {
            category.value: current.release_rules[category.value]
            for category in stopped_categories - {StopCategory.NEW_RISK}
            if current is not None and category.value in current.release_rules
        }
        fields = {
            "stop_state_version_id": stop_state_version_id,
            "environment_id": self._environment_id,
            "environment_kind": environment_kind,
            "authority_class": authority_class,
            "account_ref": account_ref,
            "activation_id": None,
            "version": 1 if current is None else current.version + 1,
            "stopped_categories": stopped_categories,
            "reason": reason,
            "source": source,
            "started_at": observed_at,
            "loss_latch_digest": (
                current.loss_latch_digest if current is not None else None
            ),
            "release_rules": {
                **retained_rules,
                "NEW_RISK": {
                    "user_releasable": False,
                    "requires": resolution_requirement,
                },
                "evidence_digest": evidence_digest,
            },
        }
        state = StopStateVersion(**fields, content_digest=content_digest(fields))
        self._capital.insert_stop_state(state)
        return state

    def release_account_system_stop(
        self,
        request: AccountSystemStopReleaseRequest,
    ) -> StopStateVersion:
        """Append an explicit release only after fresh, resolved account evidence."""

        if request.environment_id != self._environment_id:
            raise CapitalConflict("SYSTEM_STOP_RELEASE_ENVIRONMENT_MISMATCH")
        with self._connection.transaction():
            current = self._capital.lock_current_account_stop_state(
                account_ref=request.account_ref,
            )
            if current is None:
                raise CapitalConflict("SYSTEM_STOP_NOT_ACTIVE")
            if current.version != request.expected_version:
                raise CapitalConflict("SYSTEM_STOP_VERSION_CONFLICT")
            if current.content_digest != request.expected_stop_content_digest:
                raise CapitalConflict("SYSTEM_STOP_CONTENT_CONFLICT")
            if StopCategory.NEW_RISK not in current.stopped_categories:
                raise CapitalConflict("SYSTEM_STOP_NOT_ACTIVE")
            if (
                current.environment_id != request.environment_id
                or current.environment_kind is not request.environment_kind
                or current.authority_class is not request.authority_class
                or current.account_ref != request.account_ref
            ):
                raise CapitalConflict("SYSTEM_STOP_RELEASE_IDENTITY_MISMATCH")
            if (
                current.source
                not in {
                    AccountSystemStopSource.EXTERNAL_ACTIVITY.value,
                    AccountSystemStopSource.ATTRIBUTED_ACTION_ANOMALY.value,
                }
                or current.source != request.expected_source.value
            ):
                raise CapitalConflict("SYSTEM_STOP_SOURCE_CONFLICT")
            if (
                current.started_at.utcoffset() is None
                or request.reconciliation_observed_at < current.started_at
            ):
                raise CapitalConflict("SYSTEM_STOP_RELEASE_EVIDENCE_PREDATES_STOP")
            if request.new_stop_state_version_id == current.stop_state_version_id:
                raise CapitalConflict("SYSTEM_STOP_RELEASE_IDENTITY_CONFLICT")

            stopped_categories = frozenset(
                current.stopped_categories - {StopCategory.NEW_RISK}
            )
            retained_rules = {
                category.value: current.release_rules[category.value]
                for category in stopped_categories
                if category.value in current.release_rules
            }
            fields = {
                "stop_state_version_id": request.new_stop_state_version_id,
                "environment_id": current.environment_id,
                "environment_kind": current.environment_kind,
                "authority_class": current.authority_class,
                "account_ref": current.account_ref,
                "activation_id": None,
                "version": current.version + 1,
                "stopped_categories": stopped_categories,
                "reason": "SYSTEM_STOP_RELEASED_AFTER_RECONCILIATION",
                "source": "USER_SYSTEM_STOP_RELEASE",
                "started_at": request.submitted_at,
                "loss_latch_digest": current.loss_latch_digest,
                "release_rules": {
                    **retained_rules,
                    "system_stop_release": {
                        "released_stop_state_version_id": (
                            current.stop_state_version_id
                        ),
                        "released_stop_version": current.version,
                        "released_stop_content_digest": current.content_digest,
                        "released_source": current.source,
                        "resolution_activation_id": (
                            request.resolution_activation_id
                        ),
                        "reconciliation_digest": request.reconciliation_digest,
                        "resolution_evidence_digest": (
                            request.resolution_evidence_digest
                        ),
                        "reconciliation_observed_at": (
                            request.reconciliation_observed_at.isoformat()
                        ),
                        "confirmation": request.confirmation,
                    },
                },
            }
            state = StopStateVersion(
                **fields,
                content_digest=content_digest(fields),
            )
            self._capital.insert_stop_state(state)
            return state
