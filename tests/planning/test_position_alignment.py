from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from halpha.capital.models import AuthorityClass, EnvironmentKind
from halpha.domain_values import content_digest
from halpha.planning.models import (
    POSITION_ALIGNMENT_ALLOWED_ACTIONS,
    PlanActivation,
    PlanLifecycle,
    PositionAlignmentSpec,
    RequestedLimits,
    TradePlanVersion,
)
from halpha.planning.registry import Direction, FixedDirectExecutionBasis
from halpha.planning.service import PlanningApplicationService


NOW = datetime(2030, 1, 15, 15, tzinfo=UTC)


def _alignment(
    *,
    operation: str = "REDUCE",
    position_side: str = "BOTH",
) -> PositionAlignmentSpec:
    return PositionAlignmentSpec(
        operation=operation,
        snapshot_ref="snapshot-1",
        fact_cutoff=NOW,
        account_ref="copy-lead-account",
        venue_ref="BINANCE_USDM",
        instrument_ref="TESTUSDT-PERP",
        direction="LONG",
        position_side=position_side,
        baseline_quantity="10",
        requested_reduction_quantity=("10" if operation == "CLOSE" else "4"),
        target_quantity_after=("0" if operation == "CLOSE" else "6"),
        baseline_entry_price="100",
        baseline_mark_price="105",
    )


def _version(alignment: PositionAlignmentSpec | None = None) -> TradePlanVersion:
    return TradePlanVersion(
        plan_version_id="position-plan-version",
        plan_id="position-plan",
        environment_id="demo",
        fixed_at=NOW,
        plan_name="TESTUSDT synthetic external reduction",
        created_at=NOW,
        creator_kind="AI",
        decision_basis=FixedDirectExecutionBasis(
            parameter_digest=content_digest({}),
            product_build_id="a" * 64,
        ),
        position_alignment=alignment or _alignment(),
        account_ref="copy-lead-account",
        venue_ref="BINANCE_USDM",
        instrument_ref="TESTUSDT-PERP",
        direction=Direction.LONG,
        target_exposure="50",
        requested_limits=RequestedLimits(
            max_margin="50",
            max_notional="50",
            max_allowed_loss="50",
        ),
        valid_from=NOW,
        valid_until=NOW + timedelta(hours=1),
        allowed_actions=POSITION_ALIGNMENT_ALLOWED_ACTIONS,
        terms={},
        content_digest="b" * 64,
    )


def _snapshot(
    *,
    quantity: str = "10",
    entry_price: str = "100",
    ordinary_open_orders: int = 0,
    position_side: str = "BOTH",
) -> dict[str, object]:
    return {
        "schema": "HALPHA_BINANCE_USDM_ACCOUNT_SNAPSHOT_V2",
        "snapshot_complete": True,
        "read_only": True,
        "management_authority": "NONE",
        "open_position_count": 1,
        "ordinary_open_order_count": ordinary_open_orders,
        "algo_open_order_count": 0,
        "ordinary_open_orders": [{} for _ in range(ordinary_open_orders)],
        "algo_open_orders": [],
        "positions": [
            {
                "instrument_ref": "TESTUSDT-PERP",
                "position_side": position_side,
                "direction": "LONG",
                "absolute_quantity": quantity,
                "entry_price": entry_price,
            }
        ],
    }


class _Result:
    def __init__(self, row: object) -> None:
        self._row = row

    def fetchone(self) -> object:
        return self._row


class _Connection:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, parameters: tuple[object, ...]) -> _Result:
        self.calls.append((query, parameters))
        return _Result(self.rows.pop(0))


def _service(
    version: TradePlanVersion,
    rows: list[object],
) -> tuple[PlanningApplicationService, list[object], _Connection]:
    inserted: list[object] = []
    connection = _Connection(rows)
    service = object.__new__(PlanningApplicationService)
    service._environment_id = "demo"
    service._connection = connection
    service._planning = SimpleNamespace(
        get_version=lambda _plan_version_id: version,
        lock_and_list_open_instrument_activations=lambda **_scope: (),
        insert_activation=inserted.append,
    )
    return service, inserted, connection


def test_position_alignment_model_requires_an_exact_bounded_reduction() -> None:
    assert _alignment(operation="CLOSE").target_quantity_after == "0"

    payload = _alignment().model_dump(mode="python")
    payload["target_quantity_after"] = "7"
    with pytest.raises(ValidationError, match="POSITION_ALIGNMENT_QUANTITY_MISMATCH"):
        PositionAlignmentSpec.model_validate(payload)

    payload = _alignment().model_dump(mode="python")
    payload["position_side"] = "SHORT"
    with pytest.raises(
        ValidationError,
        match="POSITION_ALIGNMENT_SIDE_DIRECTION_MISMATCH",
    ):
        PositionAlignmentSpec.model_validate(payload)


def test_activation_revalidates_original_and_latest_snapshot_then_starts_exiting() -> None:
    version = _version()
    service, inserted, connection = _service(
        version,
        [
            (NOW, _snapshot()),
            (NOW + timedelta(seconds=20), _snapshot()),
        ],
    )

    activation = service.activate_version(
        plan_version_id=version.plan_version_id,
        activation_id="position-activation",
        environment_kind=EnvironmentKind.DEMO,
        authority_class=AuthorityClass.DEMO_VALIDATION,
        observed_at=NOW + timedelta(seconds=30),
        order_schedule_snapshot=None,
    )

    assert activation.lifecycle is PlanLifecycle.EXITING
    assert activation.entry_opportunity_consumed is True
    assert activation.position_alignment == version.position_alignment
    assert activation.order_schedule_snapshot is None
    assert inserted == [activation]
    assert len(connection.calls) == 2


@pytest.mark.parametrize(
    ("lifecycle", "state_updates"),
    (
        (
            PlanLifecycle.USER_TAKEOVER,
            {"takeover_scope": {"scope": "POSITION_ALIGNMENT"}},
        ),
        (
            PlanLifecycle.COMPLETED,
            {"closure_digest": "c" * 64, "result_ref": "result-1"},
        ),
    ),
)
def test_position_alignment_activation_remains_readable_after_terminal_transition(
    lifecycle: PlanLifecycle,
    state_updates: dict[str, object],
) -> None:
    version = _version()
    service, _inserted, _connection = _service(
        version,
        [(NOW, _snapshot()), (NOW + timedelta(seconds=20), _snapshot())],
    )
    activation = service.activate_version(
        plan_version_id=version.plan_version_id,
        activation_id="position-activation",
        environment_kind=EnvironmentKind.DEMO,
        authority_class=AuthorityClass.DEMO_VALIDATION,
        observed_at=NOW + timedelta(seconds=30),
        order_schedule_snapshot=None,
    )
    payload = activation.model_dump(mode="python")
    payload.update({"lifecycle": lifecycle, **state_updates})

    restored = PlanActivation.model_validate(payload)

    assert restored.lifecycle is lifecycle
    assert restored.position_alignment == activation.position_alignment


@pytest.mark.parametrize(
    ("latest_cutoff", "latest_snapshot", "reason"),
    (
        (
            NOW + timedelta(seconds=20),
            _snapshot(quantity="12"),
            "POSITION_ALIGNMENT_FACT_CHANGED",
        ),
        (
            NOW + timedelta(seconds=20),
            _snapshot(ordinary_open_orders=1),
            "POSITION_ALIGNMENT_FACT_CHANGED",
        ),
        (
            NOW - timedelta(minutes=3),
            _snapshot(),
            "POSITION_ALIGNMENT_FACT_NOT_CURRENT",
        ),
    ),
)
def test_activation_fails_closed_when_latest_account_fact_is_not_the_baseline(
    latest_cutoff: datetime,
    latest_snapshot: dict[str, object],
    reason: str,
) -> None:
    version = _version()
    service, inserted, _connection = _service(
        version,
        [(NOW, _snapshot()), (latest_cutoff, latest_snapshot)],
    )

    with pytest.raises(ValueError, match=reason):
        service.activate_version(
            plan_version_id=version.plan_version_id,
            activation_id="position-activation",
            environment_kind=EnvironmentKind.DEMO,
            authority_class=AuthorityClass.DEMO_VALIDATION,
            observed_at=NOW + timedelta(seconds=30),
            order_schedule_snapshot=None,
        )

    assert inserted == []


def test_hedge_side_alignment_revalidates_the_exact_side_and_can_activate() -> None:
    version = _version(_alignment(position_side="LONG"))
    service, inserted, connection = _service(
        version,
        [
            (NOW, _snapshot(position_side="LONG")),
            (NOW + timedelta(seconds=20), _snapshot(position_side="LONG")),
        ],
    )

    activation = service.activate_version(
        plan_version_id=version.plan_version_id,
        activation_id="position-activation",
        environment_kind=EnvironmentKind.DEMO,
        authority_class=AuthorityClass.DEMO_VALIDATION,
        observed_at=NOW + timedelta(seconds=30),
        order_schedule_snapshot=None,
    )

    assert activation.position_alignment is not None
    assert activation.position_alignment.position_side == "LONG"
    assert inserted == [activation]
    assert len(connection.calls) == 2
