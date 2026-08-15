from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from halpha.domain_values import content_digest
from halpha.planning.models import PlanDecisionContext
from halpha.planning.order_schedule import (
    AmountDistribution,
    OrderScheduleSpec,
    SinglePrice,
    VenueOrderPolicy,
    VenueOrderType,
    VenueTimeInForce,
)
from halpha.planning.repository import load_persisted_order_schedule_spec
from halpha.planning.repository import PostgreSQLPlanningRepository


def _gtd_schedule() -> OrderScheduleSpec:
    return OrderScheduleSpec(
        price_distribution=SinglePrice(limit_price="63000"),
        amount_distribution=AmountDistribution(base_notional="70"),
        venue_policy=VenueOrderPolicy(
            order_type=VenueOrderType.LIMIT,
            time_in_force=VenueTimeInForce.GTD,
            expire_at=datetime(2026, 7, 30, 5, tzinfo=UTC),
        )
    )


def test_persisted_schedule_accepts_current_json_digest() -> None:
    schedule = _gtd_schedule()
    payload = schedule.model_dump(mode="json")

    restored = load_persisted_order_schedule_spec(payload, content_digest(payload))

    assert restored == schedule


def test_persisted_schedule_accepts_legacy_model_digest() -> None:
    schedule = _gtd_schedule()
    payload = schedule.model_dump(mode="json")

    restored = load_persisted_order_schedule_spec(payload, content_digest(schedule))

    assert restored == schedule


def test_persisted_schedule_rejects_unrelated_digest() -> None:
    schedule = _gtd_schedule()
    payload = schedule.model_dump(mode="json")

    with pytest.raises(ValueError, match="ORDER_SCHEDULE_SPEC_CORRUPT"):
        load_persisted_order_schedule_spec(payload, "0" * 64)


def test_insert_version_hashes_the_exact_json_payload_it_persists() -> None:
    class RecordingConnection:
        def __init__(self) -> None:
            self.params: tuple[object, ...] | None = None

        def execute(
            self,
            _statement: str,
            params: tuple[object, ...],
        ) -> None:
            self.params = params

    schedule = _gtd_schedule()
    connection = RecordingConnection()
    basis = SimpleNamespace(
        decision_basis_ref="DIRECT_EXECUTION@1",
        product_build_id="a" * 64,
        parameter_schema_version="1",
        normalized_parameters={},
        parameter_digest=content_digest({}),
        model_dump=lambda **_kwargs: {
            "kind": "DIRECT_EXECUTION",
            "decision_basis_ref": "DIRECT_EXECUTION@1",
            "parameter_schema_version": "1",
            "normalized_parameters": {},
            "parameter_digest": content_digest({}),
            "product_build_id": "a" * 64,
        },
    )
    version = SimpleNamespace(
        plan_version_id="version-1",
        environment_id="demo",
        plan_id="plan-1",
        fixed_at=datetime(2026, 7, 30, 4, 45, tzinfo=UTC),
        decision_basis=basis,
        account_ref="demo-account",
        venue_ref="BINANCE_USDM",
        instrument_ref="BTCUSDT-PERP",
        direction=SimpleNamespace(value="LONG"),
        requested_limits=SimpleNamespace(
            max_margin="70",
            max_notional="70",
            max_allowed_loss="70",
        ),
        terms={},
        plan_name="[测试] GTD",
        created_at=datetime(2026, 7, 30, 4, 45, tzinfo=UTC),
        creator_kind=SimpleNamespace(value="AI"),
        decision_context=PlanDecisionContext(
            rationale="bounded reason",
            evidence="bounded evidence",
            limitations="bounded limitations",
        ),
        target_exposure="70",
        valid_from=datetime(2026, 7, 30, 4, 45, tzinfo=UTC),
        valid_until=datetime(2026, 7, 30, 5, 15, tzinfo=UTC),
        allowed_actions=frozenset({"ENTRY_LIMIT"}),
        content_digest="b" * 64,
        order_schedule_spec=schedule,
    )

    PostgreSQLPlanningRepository(connection, "demo").insert_version(version)  # type: ignore[arg-type]

    assert connection.params is not None
    assert connection.params[17].obj["decision_context"] == {
        "rationale": "bounded reason",
        "evidence": "bounded evidence",
        "limitations": "bounded limitations",
    }
    assert connection.params[-3] == content_digest(schedule.model_dump(mode="json"))
    assert connection.params[-2:] == (None, None)
