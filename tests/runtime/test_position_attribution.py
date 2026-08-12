from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from halpha.position_attribution import (
    account_instrument_attribution,
    account_instrument_attribution_from_rows,
    activation_position_attribution,
    allocate_funding_income,
)
from halpha.planning.registry import Direction
from halpha.venue_integration.models import (
    ExecutionActionKind,
    ExecutionActionState,
    VenueFactKind,
    VenueFactSourceClass,
)


NOW = datetime(2026, 7, 26, tzinfo=UTC)


def _activation(activation_id: str, direction: Direction = Direction.LONG):
    return SimpleNamespace(
        activation_id=activation_id,
        environment_id="demo-main",
        account_ref="demo-account",
        instrument_ref="BTCUSDT-PERP",
        direction=direction,
        created_at=NOW,
    )


def _action(
    activation_id: str,
    action_id: str,
    kind: ExecutionActionKind,
    *,
    quantity: str,
    created_at: datetime = NOW,
):
    return SimpleNamespace(
        activation_id=activation_id,
        account_ref="demo-account",
        execution_action_id=action_id,
        action_kind=kind,
        state=ExecutionActionState.CLOSED,
        client_order_id=("a" * 31 + action_id[-1]),
        action_terms={"quantity": quantity, "price": "100"},
        created_at=created_at,
    )


def _fill(
    activation_id: str,
    action_id: str,
    fact_id: str,
    quantity: str,
    *,
    source_time: datetime = NOW,
):
    return SimpleNamespace(
        kind=VenueFactKind.FILL,
        action_ref=action_id,
        activation_ref=activation_id,
        venue_fact_id=fact_id,
        payload={"last_quantity": quantity},
        source_time=source_time,
    )


def test_two_same_direction_activations_reconcile_without_losing_ownership() -> None:
    first = _activation("activation-1")
    second = _activation("activation-2")
    actions = {
        first.activation_id: (
            _action(first.activation_id, "entry-1", ExecutionActionKind.ENTRY, quantity="0.01"),
        ),
        second.activation_id: (
            _action(second.activation_id, "entry-2", ExecutionActionKind.ENTRY, quantity="0.02"),
        ),
    }
    facts = {
        "entry-1": (_fill(first.activation_id, "entry-1", "fill-1", "0.01"),),
        "entry-2": (_fill(second.activation_id, "entry-2", "fill-2", "0.02"),),
    }

    result = account_instrument_attribution(
        first,
        (first, second),
        lambda activation_id: actions[activation_id],
        lambda action_id: facts[action_id],
    )

    assert result.activation_signed_position == "0.01"
    assert result.account_signed_position == "0.03"
    assert result.activation_fill_fact_refs == ("fill-1",)
    assert result.account_fill_fact_refs == ("fill-1", "fill-2")


def test_one_plan_exit_reduces_only_its_own_virtual_position() -> None:
    activation = _activation("activation-1")
    actions = (
        _action(activation.activation_id, "entry-1", ExecutionActionKind.ENTRY, quantity="0.01"),
        _action(activation.activation_id, "exit-1", ExecutionActionKind.EXIT, quantity="0.01"),
    )
    facts = {
        "entry-1": (_fill(activation.activation_id, "entry-1", "fill-entry", "0.01"),),
        "exit-1": (_fill(activation.activation_id, "exit-1", "fill-exit", "0.01"),),
    }

    result = activation_position_attribution(
        activation,
        actions,
        lambda action_id: facts[action_id],
    )

    assert result.signed_position == "0"


def test_external_position_alignment_starts_from_only_its_reduction_scope() -> None:
    activation = _activation("alignment-1", Direction.SHORT)
    activation.position_alignment = SimpleNamespace(
        requested_reduction_quantity="0.5",
    )
    exit_action = _action(
        activation.activation_id,
        "exit-alignment",
        ExecutionActionKind.EXIT,
        quantity="0.5",
    )

    before = activation_position_attribution(activation, (), lambda _action_id: ())
    after = activation_position_attribution(
        activation,
        (exit_action,),
        lambda _action_id: (
            _fill(
                activation.activation_id,
                exit_action.execution_action_id,
                "alignment-fill",
                "0.5",
            ),
        ),
    )

    assert before.signed_position == "-0.5"
    assert after.signed_position == "0"
    assert after.fill_fact_refs == ("alignment-fill",)


@pytest.mark.parametrize(
    "placeholder_trade_id",
    (
        "c3dbbc0b-8835-5ed6-a7bd-a93fc9be7912",
        "S-18c897945fc371ff-d9fd8ca4",
    ),
)
def test_real_binance_trade_replaces_framework_reconciliation_placeholder(
    placeholder_trade_id: str,
) -> None:
    activation = _activation("activation-1")
    entry = _action(
        activation.activation_id,
        "entry-1",
        ExecutionActionKind.ENTRY,
        quantity="0.001",
    )
    take_profit = _action(
        activation.activation_id,
        "take-profit-1",
        ExecutionActionKind.TAKE_PROFIT,
        quantity="0.001",
    )
    common = {
        "last_price": "100",
        "last_quantity": "0.001",
        "client_order_id": take_profit.client_order_id,
        "venue_order_ref": "987",
        "order_side": "SELL",
        "reconciliation": True,
    }
    placeholder = SimpleNamespace(
        kind=VenueFactKind.FILL,
        action_ref=take_profit.execution_action_id,
        activation_ref=activation.activation_id,
        venue_fact_id="placeholder",
        environment_id=activation.environment_id,
        account_ref=activation.account_ref,
        instrument_ref=activation.instrument_ref,
        source_class=VenueFactSourceClass.VENUE_QUERY,
        source_time=NOW,
        payload={
            **common,
            "trade_id": placeholder_trade_id,
            "event_type": "OrderFilled",
        },
    )
    real_trade = SimpleNamespace(
        kind=VenueFactKind.FILL,
        action_ref=take_profit.execution_action_id,
        activation_ref=activation.activation_id,
        venue_fact_id="real-trade",
        environment_id=activation.environment_id,
        account_ref=activation.account_ref,
        instrument_ref=activation.instrument_ref,
        source_class=VenueFactSourceClass.VENUE_QUERY,
        source_time=NOW,
        payload={
            **common,
            "trade_id": "synthetic-authoritative-trade",
            "event_type": "BinanceUserTradeQuery",
        },
    )
    facts = {
        entry.execution_action_id: (
            _fill(
                activation.activation_id,
                entry.execution_action_id,
                "entry-fill",
                "0.001",
            ),
        ),
        take_profit.execution_action_id: (placeholder, real_trade),
    }

    result = activation_position_attribution(
        activation,
        (entry, take_profit),
        lambda action_id: facts[action_id],
    )

    assert result.signed_position == "0"
    assert result.fill_fact_refs == ("entry-fill", "real-trade")

    row_result = account_instrument_attribution_from_rows(
        activation,
        (activation,),
        (
            (
                entry.activation_id,
                entry.execution_action_id,
                entry.account_ref,
                entry.action_kind.value,
                entry.action_terms,
                entry.client_order_id,
                entry.state.value,
                entry.created_at,
            ),
            (
                take_profit.activation_id,
                take_profit.execution_action_id,
                take_profit.account_ref,
                take_profit.action_kind.value,
                take_profit.action_terms,
                take_profit.client_order_id,
                take_profit.state.value,
                take_profit.created_at,
            ),
        ),
        (
            (
                "entry-fill",
                entry.execution_action_id,
                activation.activation_id,
                VenueFactKind.FILL.value,
                {"last_quantity": "0.001"},
                NOW,
            ),
            (
                placeholder.venue_fact_id,
                take_profit.execution_action_id,
                activation.activation_id,
                VenueFactKind.FILL.value,
                placeholder.payload,
                NOW,
            ),
            (
                real_trade.venue_fact_id,
                take_profit.execution_action_id,
                activation.activation_id,
                VenueFactKind.FILL.value,
                real_trade.payload,
                NOW,
            ),
        ),
    )

    assert row_result.activation_signed_position == "0"
    assert row_result.activation_fill_fact_refs == ("entry-fill", "real-trade")


def test_over_reduction_and_opposite_active_positions_fail_closed() -> None:
    activation = _activation("activation-1")
    actions = (
        _action(activation.activation_id, "entry-1", ExecutionActionKind.ENTRY, quantity="0.01"),
        _action(activation.activation_id, "exit-1", ExecutionActionKind.EXIT, quantity="0.02"),
    )
    facts = {
        "entry-1": (_fill(activation.activation_id, "entry-1", "fill-entry", "0.01"),),
        "exit-1": (_fill(activation.activation_id, "exit-1", "fill-exit", "0.02"),),
    }
    with pytest.raises(ValueError, match="ACTIVATION_POSITION_OVER_REDUCED"):
        activation_position_attribution(
            activation,
            actions,
            lambda action_id: facts[action_id],
        )

    short = _activation("activation-short", Direction.SHORT)
    short_actions = (
        _action(short.activation_id, "entry-short", ExecutionActionKind.ENTRY, quantity="0.01"),
    )
    with pytest.raises(ValueError, match="ACCOUNT_INSTRUMENT_DIRECTION_CONFLICT"):
        account_instrument_attribution(
            activation,
            (activation, short),
            lambda activation_id: (
                actions[:1] if activation_id == activation.activation_id else short_actions
            ),
            lambda action_id: (
                facts["entry-1"]
                if action_id == "entry-1"
                else (_fill(short.activation_id, action_id, "fill-short", "0.01"),)
            ),
        )


def test_funding_allocation_is_exact_and_uses_venue_precision() -> None:
    allocations = allocate_funding_income(
        "-0.00000005",
        {
            "activation-a": "0.01",
            "activation-b": "0.02",
        },
    )

    assert sum((Decimal(item.income) for item in allocations), Decimal(0)) == Decimal(
        "-0.00000005"
    )
    assert {item.activation_id: item.income for item in allocations} == {
        "activation-a": "-0.00000002",
        "activation-b": "-0.00000003",
    }


def test_funding_uses_position_at_event_time_not_later_fill() -> None:
    activation = _activation("activation-1")
    actions = (
        _action(activation.activation_id, "entry-1", ExecutionActionKind.ENTRY, quantity="0.03"),
    )
    facts = {
        "entry-1": (
            _fill(activation.activation_id, "entry-1", "fill-before", "0.01"),
            _fill(
                activation.activation_id,
                "entry-1",
                "fill-after",
                "0.02",
                source_time=NOW + timedelta(minutes=1),
            ),
        )
    }

    at_event = activation_position_attribution(
        activation,
        actions,
        lambda action_id: facts[action_id],
        as_of=NOW,
    )

    assert at_event.signed_position == "0.01"
