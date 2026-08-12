from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

import pytest

from halpha.app import planning_api as planning_api_module
from halpha.app.api_models import ReceiptResponse
from halpha.app.planning_api import (
    ControlPayload,
    PostgreSQLPlanningApi,
    _continuity_resume_evidence,
)
from halpha.planning.transitions import ControlIntent
from halpha.user_workbench.commands import (
    ReceiptState,
    advance_receipt,
    initial_receipt,
)


NOW = datetime(2026, 7, 20, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "idempotency_key",
    ("", " ", "contains whitespace", "x" * 161),
)
def test_stable_identity_rejects_invalid_idempotency_key(
    idempotency_key: str,
) -> None:
    with pytest.raises(ValueError, match="IDEMPOTENCY_KEY_INVALID"):
        planning_api_module._stable_id(
            "demo-main",
            "command",
            idempotency_key,
        )


def test_stable_identity_accepts_the_persisted_key_limit() -> None:
    assert planning_api_module._stable_id(
        "demo-main",
        "command",
        "x" * 160,
    )


def _continuity_current() -> dict[str, object]:
    cutoff = NOW - timedelta(seconds=5)
    return {
        "activation": {
            "activation_id": "activation-1",
            "environment_id": "demo-main",
            "state_version": 7,
            "run_state": "PAUSED",
            "pause_reason": "WRITER_CONTINUITY_LOST",
            "paused_at": (NOW - timedelta(seconds=60)).isoformat(),
            "entry_opportunity_consumed": False,
            "rule_state": {
                "deadlines": {
                    "entry_valid_until": (NOW + timedelta(minutes=10)).isoformat()
                }
            },
        },
        "position_attribution": {
            "reconciliation_status": "MATCH",
            "fact_activation_id": "activation-1",
            "fact_ref": "position-fact-1",
            "fact_digest": "a" * 64,
            "fact_cutoff": cutoff.isoformat(),
            "activation_signed_position": "0.01",
            "attributed_account_signed_position": "0.03",
            "venue_account_signed_position": "0.03",
        },
        "execution_actions": [
            {
                "execution_action_id": "entry-1",
                "state": "CLOSED",
                "state_version": 4,
                "updated_at": (cutoff - timedelta(seconds=1)).isoformat(),
            },
            {
                "execution_action_id": "protection-1",
                "state": "OPEN",
                "state_version": 2,
                "updated_at": cutoff.isoformat(),
            },
        ],
    }


def test_continuity_resume_accepts_only_fresh_target_scoped_exe_evidence() -> None:
    evidence = _continuity_resume_evidence(
        _continuity_current(),
        observed_at=NOW,
    )

    assert evidence["eligible"] is True
    assert evidence["denial_reasons"] == []
    assert len(str(evidence["reconciliation_digest"])) == 64


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    (
        (
            lambda current: current["position_attribution"].update(
                {"fact_activation_id": "activation-2"}
            ),
            "POSITION_FACT_ACTIVATION_MISMATCH",
        ),
        (
            lambda current: current["position_attribution"].update(
                {
                    "fact_cutoff": (
                        NOW - timedelta(seconds=31)
                    ).isoformat()
                }
            ),
            "POSITION_FACT_STALE",
        ),
        (
            lambda current: current["execution_actions"][0].update(
                {"state": "UNKNOWN"}
            ),
            "ACTION_RESULT_UNRESOLVED",
        ),
        (
            lambda current: current["activation"].update(
                {"entry_opportunity_consumed": True}
            ),
            "ENTRY_OPPORTUNITY_CONSUMED",
        ),
        (
            lambda current: current["activation"]["rule_state"]["deadlines"].update(
                {"entry_valid_until": (NOW - timedelta(seconds=1)).isoformat()}
            ),
            "ENTRY_WINDOW_EXPIRED",
        ),
    ),
)
def test_continuity_resume_rejects_untrusted_or_unresolved_evidence(
    mutation,
    expected_reason: str,
) -> None:
    current = _continuity_current()
    mutation(current)

    evidence = _continuity_resume_evidence(current, observed_at=NOW)

    assert evidence["eligible"] is False
    assert expected_reason in evidence["denial_reasons"]
    assert evidence["reconciliation_digest"] is None


def test_receipt_poll_is_pure_read() -> None:
    row = (
        "receipt-1",
        "command-1",
        "TRADEPLAN",
        "PROCESSING",
        2,
        "EXIT_RESPONSIBILITY_ACCEPTED",
        {"activation_id": "activation-1"},
        ["EXIT_CLOSURE_DIGEST"],
        "a" * 64,
        NOW,
        NOW,
        "activation-1",
    )
    query_count = 0

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def transaction():
            return nullcontext()

        @staticmethod
        def execute(_query, _parameters):
            nonlocal query_count
            query_count += 1
            return type("Cursor", (), {"fetchone": staticmethod(lambda: row)})()

    api = object.__new__(PostgreSQLPlanningApi)
    api._environment_id = "demo-main"
    api._connect = Connection

    receipt = api.receipt("receipt-1")

    assert query_count == 1
    assert receipt["state"] == "PROCESSING"
    assert receipt["reason_code"] == "EXIT_RESPONSIBILITY_ACCEPTED"
    assert receipt["pending_responsibility_refs"] == ["EXIT_CLOSURE_DIGEST"]


def test_control_submission_returns_the_public_receipt_shape(monkeypatch) -> None:
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def transaction():
            return nullcontext()

    class ControlService:
        def __init__(self, _connection, _environment_id):
            pass

        @staticmethod
        def submit(command, **_kwargs):
            received = initial_receipt(
                command,
                receipt_id="receipt-public-1",
                processing_owner="TRADEPLAN",
            )
            return advance_receipt(
                received,
                state=ReceiptState.EFFECTIVE,
                reason_code="CONTROL_EFFECTIVE",
                result={"activation_id": command.target_ref},
                pending_responsibility_refs=(),
                observed_at=command.submitted_at,
            )

    monkeypatch.setattr(
        planning_api_module,
        "ActivationControlService",
        ControlService,
    )
    api = object.__new__(PostgreSQLPlanningApi)
    api._environment_id = "demo-main"
    api._profile = "BINANCE_DEMO"
    api._connect = Connection

    result = api.submit_control(
        "activation-1",
        ControlIntent.STOP_NEW_RISK,
        ControlPayload(expected_version=7),
        idempotency_key="public-receipt-1",
        observed_at=NOW,
    )

    assert "environment_id" not in result
    assert ReceiptResponse.model_validate(result).receipt_id == "receipt-public-1"


def test_activation_timeline_includes_user_control_command_receipt() -> None:
    command_updated_at = NOW + timedelta(seconds=2)

    class Cursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def execute(query, _parameters):
            if "FROM halpha.command c" in query:
                return Cursor(
                    [
                        (
                            "command-exit-1",
                            "EXIT_STRATEGY",
                            NOW,
                            "receipt-exit-1",
                            "EFFECTIVE",
                            "EXIT_COMPLETED",
                            command_updated_at,
                        )
                    ]
                )
            return Cursor([])

    api = object.__new__(PostgreSQLPlanningApi)
    api._environment_id = "demo-main"
    api._connect = Connection

    timeline = api.activation_timeline("activation-1")

    assert timeline == [
        {
            "source": "CONTROL_COMMAND",
            "source_ref": "command-exit-1",
            "stage_order": 4,
            "at": command_updated_at.isoformat(),
            "status": "EFFECTIVE",
            "detail": {
                "intent": "EXIT_STRATEGY",
                "submitted_at": NOW.isoformat(),
                "receipt_id": "receipt-exit-1",
                "reason_code": "EXIT_COMPLETED",
            },
        }
    ]


def test_activation_timeline_includes_activation_start() -> None:
    class Cursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def execute(query, _parameters):
            if "FROM halpha.plan_activation" in query:
                return Cursor([(NOW, "plan-version-1")])
            return Cursor([])

    api = object.__new__(PostgreSQLPlanningApi)
    api._environment_id = "demo-main"
    api._connect = Connection

    timeline = api.activation_timeline("activation-1")

    assert timeline == [
        {
            "source": "ACTIVATION",
            "source_ref": "activation-1",
            "stage_order": 0,
            "at": NOW.isoformat(),
            "status": "STARTED",
            "detail": {
                "plan_version_ref": "plan-version-1",
            },
        }
    ]


def test_activation_timeline_exposes_frozen_strategy_entry_evidence() -> None:
    setup_evidence = {
        "mode": "DONCHIAN_BREAKOUT",
        "trigger_boundary": "65000",
        "confirmation_closes": ["65010", "65020"],
    }

    class Cursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def execute(query, _parameters):
            if "FROM halpha.plan_event" in query:
                return Cursor(
                    [
                        (
                            "plan-event-1",
                            "ENTRY_BREAKOUT",
                            "activation-1:BAR:1:2",
                            NOW,
                            "PROPOSED_ACTION_CAP_ACCEPTED",
                            None,
                            {"accepted": True},
                            {
                                "execution_context": {
                                    "entry_risk_context": {
                                        "trigger_atr": "500",
                                        "setup_evidence": setup_evidence,
                                    }
                                }
                            },
                            NOW,
                        )
                    ]
                )
            return Cursor([])

    api = object.__new__(PostgreSQLPlanningApi)
    api._environment_id = "demo-main"
    api._connect = Connection

    timeline = api.activation_timeline("activation-1")

    assert timeline[0]["detail"]["entry_risk_context"] == {
        "trigger_atr": "500",
        "setup_evidence": setup_evidence,
    }


def test_activation_list_uses_effective_exit_command_as_close_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cursor:
        @staticmethod
        def fetchall():
            return [(
                "activation-1",
                "No-fill exit",
                NOW.isoformat(),
                "AI",
                None,
                None,
                None,
                None,
                "EXIT_STRATEGY",
                "NO_ACTION",
                {"fills": [], "calculation_complete": False},
            )]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def execute(query, _parameters):
            assert "c.intent IN ('EXIT_STRATEGY', 'USER_TAKEOVER')" in query
            assert "r.state = 'EFFECTIVE'" in query
            return Cursor()

    class Repository:
        def __init__(self, _connection, _environment_id):
            pass

        @staticmethod
        def get_activation(_activation_id):
            return type(
                "Activation",
                (),
                {
                    "model_dump": staticmethod(
                        lambda **_kwargs: {
                            "activation_id": "activation-1",
                            "lifecycle": "COMPLETED",
                        }
                    )
                },
            )()

    monkeypatch.setattr(
        planning_api_module,
        "PostgreSQLPlanningRepository",
        Repository,
    )
    api = object.__new__(PostgreSQLPlanningApi)
    api._environment_id = "demo-main"
    api._connect = Connection

    activations = api.list_activations()

    assert activations[0]["closure_reason_code"] == "EXIT_STRATEGY"
    assert activations[0]["primary_result"] == "NO_ACTION"
