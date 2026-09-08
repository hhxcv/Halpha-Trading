from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from halpha.capital.discipline import (
    evaluate_new_risk_discipline,
    read_new_risk_discipline,
)
from halpha.capital.models import (
    AccountEquitySnapshot,
    AccountPositionRisk,
    NewRiskAttempt,
    NewRiskDisciplinePolicy,
    NewRiskResult,
)


NOW = datetime(2030, 1, 15, 4, 0, tzinfo=UTC)
POLICY = NewRiskDisciplinePolicy()


def _equity(
    wallet: str = "1000",
    margin: str = "975",
    *,
    cutoff: datetime = NOW,
    positions: tuple[AccountPositionRisk, ...] = (),
) -> AccountEquitySnapshot:
    return AccountEquitySnapshot(
        fact_ref=f"snapshot-{wallet}-{margin}",
        cutoff=cutoff,
        can_trade=True,
        wallet_balance=wallet,
        unrealized_pnl=str(float(margin) - float(wallet)),
        margin_balance=margin,
        available_balance="800",
        positions=positions,
    )


def _attempt(
    activation_id: str,
    *,
    loss: str = "1",
    notional: str = "100",
    instrument_ref: str = "BTCUSDT-PERP",
    lifecycle: str = "RUNNING",
) -> NewRiskAttempt:
    return NewRiskAttempt(
        activation_id=activation_id,
        instrument_ref=instrument_ref,
        max_notional=notional,
        max_allowed_loss=loss,
        lifecycle=lifecycle,
        has_entry_fill=True,
        created_at=NOW - timedelta(hours=1),
    )


def _proposal(**updates: str) -> dict[str, str]:
    result = {
        "proposed_max_allowed_loss": "7.2",
        "proposed_max_notional": "100",
        "proposed_instrument_ref": "BTCUSDT-PERP",
        "proposed_direction": "LONG",
    }
    result.update(updates)
    return result


def test_small_plan_is_allowed_against_current_equity() -> None:
    status = evaluate_new_risk_discipline(
        policy=POLICY, observed_at=NOW, account_equity=_equity(), attempts=(), **_proposal()
    )

    assert status.status == "ALLOWED"
    assert status.risk_equity == "975"
    assert status.max_plan_loss == "7.3125"
    assert status.minimum_reward_risk_ratio == "1"
    assert status.open_risk_limit == "24.375"
    assert status.gross_exposure_limit == "1950"


def test_plan_above_single_plan_loss_is_blocked() -> None:
    status = evaluate_new_risk_discipline(
        policy=POLICY,
        observed_at=NOW,
        account_equity=_equity(),
        attempts=(),
        **_proposal(proposed_max_allowed_loss="7.4"),
    )

    assert status.status == "BLOCKED"
    assert "NEW_RISK_PLAN_LOSS_LIMIT_EXCEEDED" in status.blocker_codes


def test_multiple_open_plans_are_allowed_until_total_risk_capacity_is_used() -> None:
    status = evaluate_new_risk_discipline(
        policy=POLICY,
        observed_at=NOW,
        account_equity=_equity(),
        attempts=tuple(_attempt(str(index), loss="5", notional="100") for index in range(4)),
        **_proposal(proposed_max_allowed_loss="4.5"),
    )

    assert status.open_new_risk_activation_count == 4
    assert status.open_risk_committed == "20"
    assert "NEW_RISK_OPEN_RISK_LIMIT_EXCEEDED" in status.blocker_codes
    assert "NEW_RISK_CONCURRENT_ACTIVATION_LIMIT_REACHED" not in status.blocker_codes


def test_actual_position_and_plan_notional_use_the_larger_scope_not_the_sum() -> None:
    status = evaluate_new_risk_discipline(
        policy=POLICY,
        observed_at=NOW,
        account_equity=_equity(
            positions=(
                AccountPositionRisk(
                    instrument_ref="BTCUSDT-PERP",
                    direction="LONG",
                    notional="120",
                    unrealized_pnl="0",
                ),
            )
        ),
        attempts=(_attempt("open", notional="100"),),
        **_proposal(proposed_max_allowed_loss="1", proposed_max_notional="30"),
    )

    assert status.gross_exposure == "120"
    assert status.gross_exposure_after_proposal == "130"


def test_targeted_planning_projection_exposes_remaining_nominal_capacity() -> None:
    status = evaluate_new_risk_discipline(
        policy=POLICY,
        observed_at=NOW,
        account_equity=_equity(
            positions=(
                AccountPositionRisk(
                    instrument_ref="BTCUSDT-PERP",
                    direction="LONG",
                    notional="120",
                    unrealized_pnl="0",
                ),
            )
        ),
        attempts=(_attempt("open", notional="100"),),
        entry_instrument_ref="BTCUSDT-PERP",
        entry_direction="LONG",
    )

    assert status.status == "ALLOWED"
    assert status.instrument_ref == "BTCUSDT-PERP"
    assert status.instrument_exposure == "120"
    assert status.available_notional_capacity == "875"


def test_targeted_planning_capacity_is_hidden_when_new_risk_is_blocked() -> None:
    status = evaluate_new_risk_discipline(
        policy=POLICY,
        observed_at=NOW,
        account_equity=_equity(),
        attempts=(),
        **_proposal(proposed_max_allowed_loss="7.4"),
    )

    assert status.status == "BLOCKED"
    assert status.available_notional_capacity is None


def test_distinct_actual_and_planned_instruments_remain_additive() -> None:
    equity = _equity(
        positions=(
            AccountPositionRisk(
                instrument_ref="BTCUSDT-PERP",
                direction="LONG",
                notional="1000",
                unrealized_pnl="0",
            ),
        )
    )
    plan = evaluate_new_risk_discipline(
        policy=POLICY,
        observed_at=NOW,
        account_equity=equity,
        attempts=(),
        **_proposal(
            proposed_max_allowed_loss="1",
            proposed_max_notional="1000",
            proposed_instrument_ref="ETHUSDT-PERP",
        ),
    )
    entry = evaluate_new_risk_discipline(
        policy=POLICY,
        observed_at=NOW,
        account_equity=equity,
        attempts=(
            _attempt(
                "eth-open",
                loss="1",
                notional="1000",
                instrument_ref="ETHUSDT-PERP",
            ),
        ),
    )

    assert plan.status == "BLOCKED"
    assert plan.gross_exposure_after_proposal == "2000"
    assert plan.correlated_exposure_after_proposal == "2000"
    assert "NEW_RISK_GROSS_EXPOSURE_LIMIT_EXCEEDED" in plan.blocker_codes
    assert entry.status == "BLOCKED"
    assert entry.gross_exposure == "2000"
    assert entry.correlated_exposure == "2000"


def test_concentration_and_correlated_cluster_each_block_new_risk() -> None:
    status = evaluate_new_risk_discipline(
        policy=NewRiskDisciplinePolicy(
            max_plan_loss_fraction="0.0025",
            max_open_risk_fraction="0.01",
            max_gross_exposure_fraction="2",
            max_instrument_exposure_fraction="0.1",
            max_correlated_exposure_fraction="0.2",
            daily_loss_stop_fraction="0.0075",
            weekly_loss_stop_fraction="0.02",
            rolling_drawdown_stop_fraction="0.04",
        ),
        observed_at=NOW,
        account_equity=_equity(),
        attempts=(_attempt("eth", instrument_ref="ETHUSDT-PERP", notional="150"),),
        **_proposal(proposed_max_allowed_loss="1", proposed_max_notional="100"),
    )

    assert "NEW_RISK_INSTRUMENT_EXPOSURE_LIMIT_EXCEEDED" in status.blocker_codes
    assert "NEW_RISK_CORRELATED_EXPOSURE_LIMIT_EXCEEDED" in status.blocker_codes


def test_loss_and_drawdown_stops_do_not_depend_on_order_count() -> None:
    status = evaluate_new_risk_discipline(
        policy=POLICY,
        observed_at=NOW,
        account_equity=_equity("900", "900"),
        attempts=(),
        results=(
            NewRiskResult(
                activation_id="loss", net_pnl="-15", closed_at=NOW - timedelta(hours=1)
            ),
        ),
        rolling_peak_equity="1000",
        **_proposal(proposed_max_allowed_loss="1"),
    )

    assert status.daily_loss_measure == "15"
    assert "NEW_RISK_DAILY_LOSS_STOP_REACHED" in status.blocker_codes
    assert "NEW_RISK_ROLLING_DRAWDOWN_STOP_REACHED" in status.blocker_codes


def test_current_entry_rechecks_existing_portfolio_capacity_without_double_counting() -> None:
    status = evaluate_new_risk_discipline(
        policy=POLICY,
        observed_at=NOW,
        account_equity=_equity(
            positions=(
                AccountPositionRisk(
                    instrument_ref="BTCUSDT-PERP",
                    direction="LONG",
                    notional="3000",
                    unrealized_pnl="0",
                ),
            )
        ),
        attempts=(_attempt("open", loss="1", notional="3000"),),
    )

    assert status.status == "BLOCKED"
    assert status.gross_exposure == "3000"
    assert "NEW_RISK_GROSS_EXPOSURE_LIMIT_EXCEEDED" in status.blocker_codes


def test_all_usdt_perpetuals_share_the_conservative_correlation_cluster() -> None:
    inputs = dict(
        policy=POLICY,
        observed_at=NOW,
        account_equity=_equity(
            positions=(
                AccountPositionRisk(
                    instrument_ref="HEMIUSDT-PERP",
                    direction="LONG",
                    notional="1",
                    unrealized_pnl="0",
                ),
            )
        ),
        attempts=(),
    )

    plan = evaluate_new_risk_discipline(
        **inputs,
        **_proposal(proposed_instrument_ref="XRPUSDT-PERP"),
    )
    entry = evaluate_new_risk_discipline(
        **inputs,
        entry_instrument_ref="HEMIUSDT-PERP",
        entry_direction="LONG",
    )

    assert plan.status == "ALLOWED"
    assert plan.correlation_cluster == "CRYPTO_USDM_BETA"
    assert plan.correlated_exposure_after_proposal == "101"
    assert entry.status == "ALLOWED"
    assert entry.correlation_cluster == "CRYPTO_USDM_BETA"


def test_out_of_scope_existing_instrument_still_fails_closed() -> None:
    inputs = dict(
        policy=POLICY,
        observed_at=NOW,
        account_equity=_equity(
            positions=(
                AccountPositionRisk(
                    instrument_ref="XRPUSDC-PERP",
                    direction="LONG",
                    notional="1",
                    unrealized_pnl="0",
                ),
            )
        ),
        attempts=(),
    )

    plan = evaluate_new_risk_discipline(**inputs, **_proposal())
    entry = evaluate_new_risk_discipline(**inputs)

    assert plan.status == "UNKNOWN"
    assert entry.status == "UNKNOWN"
    assert "NEW_RISK_CORRELATION_CLUSTER_UNKNOWN" in plan.blocker_codes
    assert entry.blocker_codes == ("NEW_RISK_CORRELATION_CLUSTER_UNKNOWN",)


def test_demo_and_live_use_the_same_evaluator_and_policy() -> None:
    inputs = dict(
        observed_at=NOW,
        account_equity=_equity(),
        attempts=(_attempt("open", loss="4"),),
        **_proposal(proposed_max_allowed_loss="2"),
    )
    demo = evaluate_new_risk_discipline(policy=POLICY, **inputs)
    live = evaluate_new_risk_discipline(policy=POLICY, **inputs)

    assert demo == live


def test_losing_same_direction_position_blocks_a_new_plan() -> None:
    status = evaluate_new_risk_discipline(
        policy=POLICY,
        observed_at=NOW,
        account_equity=_equity(
            positions=(
                AccountPositionRisk(
                    instrument_ref="BTCUSDT-PERP",
                    direction="LONG",
                    notional="100",
                    unrealized_pnl="-2",
                ),
            )
        ),
        attempts=(),
        **_proposal(),
    )

    assert status.status == "BLOCKED"
    assert "NEW_RISK_LOSING_POSITION_ADD_PROHIBITED" in status.blocker_codes


def test_losing_same_direction_position_blocks_an_unplanned_entry_but_allows_frozen_scale_in() -> None:
    inputs = dict(
        policy=POLICY,
        observed_at=NOW,
        account_equity=_equity(
            positions=(
                AccountPositionRisk(
                    instrument_ref="BTCUSDT-PERP",
                    direction="LONG",
                    notional="100",
                    unrealized_pnl="-2",
                ),
            )
        ),
        attempts=(_attempt("open"),),
        entry_instrument_ref="BTCUSDT-PERP",
        entry_direction="LONG",
    )

    ordinary = evaluate_new_risk_discipline(**inputs)
    planned = evaluate_new_risk_discipline(**inputs, is_frozen_scale_in=True)

    assert ordinary.status == "BLOCKED"
    assert "NEW_RISK_LOSING_POSITION_ADD_PROHIBITED" in ordinary.blocker_codes
    assert planned.status == "ALLOWED"


def test_losing_opposite_side_or_profitable_position_does_not_block_an_entry() -> None:
    opposite = evaluate_new_risk_discipline(
        policy=POLICY,
        observed_at=NOW,
        account_equity=_equity(
            positions=(
                AccountPositionRisk(
                    instrument_ref="BTCUSDT-PERP",
                    direction="SHORT",
                    notional="100",
                    unrealized_pnl="-2",
                ),
            )
        ),
        attempts=(),
        entry_instrument_ref="BTCUSDT-PERP",
        entry_direction="LONG",
    )
    profitable = evaluate_new_risk_discipline(
        policy=POLICY,
        observed_at=NOW,
        account_equity=_equity(
            positions=(
                AccountPositionRisk(
                    instrument_ref="BTCUSDT-PERP",
                    direction="LONG",
                    notional="100",
                    unrealized_pnl="2",
                ),
            )
        ),
        attempts=(),
        entry_instrument_ref="BTCUSDT-PERP",
        entry_direction="LONG",
    )

    assert opposite.status == "ALLOWED"
    assert profitable.status == "ALLOWED"


def test_stale_equity_is_unknown_and_fails_closed() -> None:
    status = evaluate_new_risk_discipline(
        policy=POLICY,
        observed_at=NOW,
        account_equity=_equity(cutoff=NOW - timedelta(seconds=66)),
        attempts=(),
        **_proposal(),
    )

    assert status.status == "UNKNOWN"
    assert status.new_risk_allowed is False
    assert status.blocker_codes == ("ACCOUNT_EQUITY_SNAPSHOT_STALE",)


def test_database_reader_serializes_and_reads_the_same_v3_account_fact() -> None:
    payload = {
        "schema": "HALPHA_BINANCE_USDM_ACCOUNT_SNAPSHOT_V3",
        "snapshot_complete": True,
        "read_only": True,
        "management_authority": "NONE",
        "account_summary": {
            "can_trade": True, "wallet_balance": "1000", "unrealized_pnl": "0",
            "margin_balance": "1000", "available_balance": "1000",
        },
        "positions": [
            {
                "instrument_ref": "BTCUSDT-PERP",
                "direction": "SHORT",
                "notional": "-100",
                "unrealized_pnl": "0",
            },
        ],
        "open_position_count": 1,
        "ordinary_open_orders": [],
        "ordinary_open_order_count": 0,
        "algo_open_orders": [],
        "algo_open_order_count": 0,
    }

    class Result:
        def __init__(self, *, one=None, many=()): self.one, self.many = one, many
        def fetchone(self): return self.one
        def fetchall(self): return self.many

    class Connection:
        def __init__(self):
            self.queries: list[str] = []
            self.results = iter((Result(), Result(one=("current", NOW, payload)), Result(many=()), Result(many=()), Result(many=[])))
        def execute(self, query, _parameters):
            self.queries.append(query)
            return next(self.results)

    connection = Connection()
    status = read_new_risk_discipline(  # type: ignore[arg-type]
        connection, environment_id="demo", account_ref="demo-account", policy=POLICY,
        observed_at=NOW, lock=True, **_proposal(),
    )

    assert status.status == "ALLOWED"
    assert status.gross_exposure == "100"
    assert "pg_advisory_xact_lock" in connection.queries[0]
    assert "payload ->> 'schema'" in connection.queries[-1]


def _review_row(
    activation_id: str,
    *,
    net_pnl: str | None = "-20",
    review_status: str = "DRAFT",
    workflow_kind: str | None = "SCALP_CYCLE",
    closed_at: datetime = NOW - timedelta(hours=1),
    **result_changes: object,
) -> tuple[object, ...]:
    return (
        activation_id,
        review_status,
        workflow_kind,
        {
            "classification": "ATTRIBUTED_FACTS_AVAILABLE",
            "missing_refs": [],
            "trade_result": {
                "calculation_complete": True,
                "execution_cost_complete": True,
                "strategy_attribution_complete": True,
                "closed": True,
                "gross_pnl": (
                    str(Decimal(net_pnl) + Decimal("1"))
                    if net_pnl is not None else "0"
                ),
                "commission": "1",
                "funding": "0",
                "net_pnl": net_pnl,
                **result_changes,
            },
        },
        {"execution_action_refs": [], "unknown_action_refs": []},
        closed_at,
    )


def _read_with_latest_reviews(
    latest_reviews: tuple[tuple[object, ...], ...],
    **risk_inputs: str,
):
    payload = {
        "schema": "HALPHA_BINANCE_USDM_ACCOUNT_SNAPSHOT_V3",
        "snapshot_complete": True,
        "read_only": True,
        "management_authority": "NONE",
        "account_summary": {
            "can_trade": True,
            "wallet_balance": "1000",
            "unrealized_pnl": "0",
            "margin_balance": "1000",
            "available_balance": "1000",
        },
        "positions": [],
        "open_position_count": 0,
        "ordinary_open_orders": [],
        "ordinary_open_order_count": 0,
        "algo_open_orders": [],
        "algo_open_order_count": 0,
    }

    class Result:
        def __init__(self, *, one=None, many=()):
            self.one, self.many = one, many

        def fetchone(self):
            return self.one

        def fetchall(self):
            return self.many

    class Connection:
        def __init__(self):
            self.results = iter(
                (
                    Result(one=("current", NOW, payload)),
                    Result(
                        many=tuple(
                            (
                                row[0], "BTCUSDT-PERP", "100", "1",
                                "COMPLETED", True, NOW - timedelta(days=1),
                            )
                            for row in latest_reviews
                        )
                    ),
                    Result(many=latest_reviews),
                    Result(),
                )
            )

        def execute(self, _query, _parameters):
            return next(self.results)

    return read_new_risk_discipline(  # type: ignore[arg-type]
        Connection(),
        environment_id="demo",
        account_ref="demo-account",
        policy=POLICY,
        observed_at=NOW,
        **risk_inputs,
    )


@pytest.mark.parametrize("check_boundary", ("activation", "entry"))
def test_scalp_losses_stop_new_risk_without_an_owner_review(check_boundary: str) -> None:
    risk_inputs = (
        _proposal()
        if check_boundary == "activation"
        else {
            "entry_instrument_ref": "BTCUSDT-PERP",
            "entry_direction": "LONG",
        }
    )
    status = _read_with_latest_reviews(
        (
            _review_row("scalp-1", net_pnl="-10"),
            _review_row("scalp-2", net_pnl="-10"),
            _review_row(
                "scalp-yesterday",
                net_pnl="-25",
                closed_at=NOW - timedelta(days=1),
            ),
        ),
        **risk_inputs,
    )

    assert status.daily_loss_measure == "20"
    assert status.weekly_loss_measure == "45"
    assert status.new_risk_allowed is False
    assert "NEW_RISK_DAILY_LOSS_STOP_REACHED" in status.blocker_codes
    assert "NEW_RISK_WEEKLY_LOSS_STOP_REACHED" in status.blocker_codes


@pytest.mark.parametrize(
    "result_changes",
    (
        {"calculation_complete": False},
        {"execution_cost_complete": False},
        {"closed": False},
        {"net_pnl": "NaN"},
        {"net_pnl": None},
        {"commission": None},
        {"commission": "-1"},
        {"funding": None},
    ),
)
def test_latest_unreliable_scalp_result_is_excluded_from_loss_totals(
    result_changes: dict[str, object],
) -> None:
    status = _read_with_latest_reviews(
        (_review_row("corrected-scalp", **result_changes),),
        **_proposal(),
    )

    assert status.new_risk_allowed is True
    assert status.daily_loss_measure == "0"
    assert status.weekly_loss_measure == "0"


@pytest.mark.parametrize(
    "unknown_evidence",
    ("classification", "missing_refs", "execution_action_refs", "unknown_action_refs"),
)
def test_unknown_latest_review_is_not_a_reliable_scalp_loss(
    unknown_evidence: str,
) -> None:
    row = list(_review_row("corrected-scalp"))
    if unknown_evidence == "classification":
        row[3]["classification"] = "UNKNOWN"
    elif unknown_evidence == "missing_refs":
        row[3]["missing_refs"] = ["plan_version"]
    else:
        row[4][unknown_evidence] = ["action-unknown"]

    status = _read_with_latest_reviews((tuple(row),), **_proposal())

    assert status.daily_loss_measure == "0"
    assert status.weekly_loss_measure == "0"


def test_reliable_account_loss_from_external_closure_still_counts() -> None:
    row = list(
        _review_row(
            "ordinary-external-close", workflow_kind=None,
            review_status="COMPLETE", strategy_attribution_complete=False,
        )
    )
    row[3]["classification"] = "ACCOUNT_FACTS_WITH_EXTERNAL_CLOSURE"

    status = _read_with_latest_reviews((tuple(row),), **_proposal())

    assert status.daily_loss_measure == "20"
    assert status.new_risk_allowed is False


def test_scalp_exception_does_not_change_ordinary_review_completion() -> None:
    status = _read_with_latest_reviews(
        (
            _review_row("ordinary-draft", workflow_kind=None, net_pnl="-50"),
            _review_row(
                "ordinary-complete",
                workflow_kind=None,
                review_status="COMPLETE",
                net_pnl="-3",
            ),
            _review_row("scalp-draft", net_pnl="-4"),
        ),
        **_proposal(),
    )

    assert status.new_risk_allowed is True
    assert status.daily_loss_measure == "7"
    assert status.weekly_loss_measure == "7"
