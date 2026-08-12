from __future__ import annotations

import pytest

from halpha.app.api_models import AccountPositionOperationPreviewPayload
from halpha.app.position_operations import preview_account_position_operation


def _summary(**updates: object) -> dict[str, object]:
    result: dict[str, object] = {
        "account_snapshot_status": "CURRENT",
        "account_snapshot_ref": "snapshot-1",
        "account_snapshot_cutoff": "2030-01-15T04:00:00Z",
        "account_ordinary_open_order_count": 0,
        "account_algo_open_order_count": 0,
        "account_positions": [
            {
                "snapshot_ref": "snapshot-1",
                "fact_cutoff": "2030-01-15T04:00:00Z",
                "instrument_ref": "TESTUSDT-PERP",
                "position_side": "BOTH",
                "direction": "SHORT",
                "absolute_quantity": "2.5",
                "entry_price": "100",
                "mark_price": "105",
                "origin": "EXTERNAL_UNMANAGED",
            }
        ],
    }
    result.update(updates)
    return result


def _payload(operation: str, **updates: object) -> AccountPositionOperationPreviewPayload:
    values: dict[str, object] = {
        "operation": operation,
        "snapshot_ref": "snapshot-1",
        "fact_cutoff": "2030-01-15T04:00:00Z",
        "instrument_ref": "TESTUSDT-PERP",
        "position_side": "BOTH",
        "expected_absolute_quantity": "2.5",
    }
    values.update(updates)
    return AccountPositionOperationPreviewPayload.model_validate(values)


def test_reduce_preview_binds_exact_external_baseline_without_entry_claim() -> None:
    preview = preview_account_position_operation(
        _summary(),
        _payload("REDUCE", requested_quantity="0.5"),
        profile="BINANCE_DEMO",
        account_ref="demo-account",
    )

    assert preview.activation_allowed is True
    assert preview.venue_action_created is False
    assert preview.plan_prefill.kind == "POSITION_DISPOSITION"
    assert preview.plan_prefill.trade_amount == "52.5"
    assert preview.plan_prefill.target_quantity_after == "2"
    alignment = preview.plan_prefill.position_alignment
    assert alignment is not None
    assert alignment.snapshot_ref == "snapshot-1"
    assert alignment.requested_reduction_quantity == "0.5"
    assert alignment.target_quantity_after == "2"


def test_close_preview_uses_the_full_snapshot_quantity() -> None:
    preview = preview_account_position_operation(
        _summary(),
        _payload("CLOSE"),
        profile="BINANCE_DEMO",
        account_ref="demo-account",
    )

    alignment = preview.plan_prefill.position_alignment
    assert alignment is not None
    assert alignment.operation.value == "CLOSE"
    assert alignment.requested_reduction_quantity == "2.5"
    assert alignment.target_quantity_after == "0"


def test_add_preview_remains_a_separate_new_risk_plan() -> None:
    preview = preview_account_position_operation(
        _summary(),
        _payload("ADD", requested_notional="125"),
        profile="BINANCE_DEMO",
        account_ref="demo-account",
    )

    assert preview.plan_prefill.kind == "NEW_EXPOSURE"
    assert preview.plan_prefill.position_alignment is None
    assert preview.plan_prefill.trade_amount == "125"
    assert preview.activation_allowed is False
    assert preview.blockers == ["EXTERNAL_POSITION_REQUIRES_ALIGNMENT"]


def test_read_only_hedge_disposition_keeps_side_and_exposes_real_blockers() -> None:
    position = dict(_summary()["account_positions"][0])  # type: ignore[index]
    position["position_side"] = "LONG"
    position["direction"] = "LONG"
    preview = preview_account_position_operation(
        _summary(
            account_ordinary_open_order_count=2,
            account_positions=[position],
        ),
        _payload("CLOSE", position_side="LONG"),
        profile="BINANCE_LIVE_READ_ONLY",
        account_ref="copy-lead-account",
    )

    assert preview.preparation_allowed is True
    assert preview.activation_allowed is False
    assert preview.blockers == [
        "READ_ONLY_CREDENTIAL",
        "OPEN_ORDERS_REQUIRE_RECONCILIATION",
    ]
    alignment = preview.plan_prefill.position_alignment
    assert alignment is not None
    assert alignment.position_side == "LONG"


def test_hedge_side_add_remains_an_independent_unsupported_new_risk_path() -> None:
    position = dict(_summary()["account_positions"][0])  # type: ignore[index]
    position["position_side"] = "LONG"
    preview = preview_account_position_operation(
        _summary(account_positions=[position]),
        _payload("ADD", position_side="LONG", requested_notional="125"),
        profile="BINANCE_DEMO",
        account_ref="demo-account",
    )

    assert preview.plan_prefill.kind == "NEW_EXPOSURE"
    assert preview.blockers == [
        "HEDGE_MODE_POSITION_OPERATIONS_UNSUPPORTED",
        "EXTERNAL_POSITION_REQUIRES_ALIGNMENT",
    ]


@pytest.mark.parametrize(
    ("summary", "payload", "reason"),
    (
        (
            _summary(account_snapshot_status="STALE"),
            _payload("CLOSE"),
            "ACCOUNT_POSITION_SNAPSHOT_NOT_CURRENT",
        ),
        (
            _summary(account_snapshot_ref="snapshot-2"),
            _payload("CLOSE"),
            "ACCOUNT_POSITION_SNAPSHOT_CHANGED",
        ),
        (
            _summary(),
            _payload("REDUCE", requested_quantity="2.5"),
            "POSITION_REDUCTION_MUST_LEAVE_POSITION",
        ),
    ),
)
def test_preview_fails_closed_for_stale_changed_or_ambiguous_requests(
    summary: dict[str, object],
    payload: AccountPositionOperationPreviewPayload,
    reason: str,
) -> None:
    with pytest.raises(ValueError, match=reason):
        preview_account_position_operation(
            summary,
            payload,
            profile="BINANCE_DEMO",
            account_ref="demo-account",
        )
