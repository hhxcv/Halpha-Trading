from __future__ import annotations

from datetime import UTC, datetime, timedelta

from halpha.outcomes.price_path import (
    ReviewPricePathBasis,
    prepare_review_price_path,
    summarize_review_price_path,
)
from halpha.public_market import MarketBar, MarketInterval, MarketWindow


_START = datetime(2026, 8, 1, tzinfo=UTC)


def _review(
    *,
    direction: str = "LONG",
    entry_at: datetime = _START + timedelta(seconds=30),
    exit_at: datetime = _START + timedelta(minutes=4, seconds=30),
    stop_bps: str | None = "100",
) -> dict[str, object]:
    snapshot: dict[str, object] | None = None
    if stop_bps is not None:
        snapshot = {
            "schedule_spec": {
                "protection_policy": {
                    "initial_stop": {"distance_bps": stop_bps},
                }
            }
        }
    return {
        "review_id": "review-1",
        "review_version": 3,
        "primary_result": "COMPLETED",
        "trade_context": {
            "instrument_ref": "BTCUSDT-PERP",
            "direction": direction,
            "order_schedule_snapshot": snapshot,
            "position_alignment": None,
        },
        "resolved_trade_result": {
            "fill_count": 2,
            "fills": [
                {
                    "trade_id": "entry",
                    "action_kind": "ENTRY",
                    "price": "100",
                    "quantity": "1",
                    "fill_time": entry_at.isoformat(),
                },
                {
                    "trade_id": "exit",
                    "action_kind": "EXIT",
                    "price": "100",
                    "quantity": "1",
                    "fill_time": exit_at.isoformat(),
                },
            ],
            "average_entry_price": "100",
            "closed": True,
            "fill_times_complete": True,
            "strategy_attribution_complete": True,
            "result_scope": "HALPHA_ATTRIBUTED_ACTIONS",
        },
    }


def _window(
    rows: tuple[tuple[str, str, str, str], ...],
    *,
    interval: str = "1m",
    start: datetime = _START + timedelta(minutes=1),
) -> MarketWindow:
    delta = timedelta(minutes=1 if interval == "1m" else 15)
    bars = tuple(
        MarketBar(
            open_at=start + index * delta,
            close_at=start + (index + 1) * delta,
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume="10",
        )
        for index, (open_price, high, low, close) in enumerate(rows)
    )
    return MarketWindow(
        instrument_ref="BTCUSDT-PERP",
        interval=interval,
        source="BINANCE_DEMO_PUBLIC",
        source_cutoff=bars[-1].close_at,
        bars=bars,
    )


def _basis(review: dict[str, object]) -> ReviewPricePathBasis:
    result = prepare_review_price_path(review, interval="1m")
    assert isinstance(result, ReviewPricePathBasis)
    return result


def test_price_path_accepts_every_current_workbench_interval() -> None:
    holding_windows: dict[MarketInterval, timedelta] = {
        "1m": timedelta(minutes=4, seconds=30),
        "5m": timedelta(minutes=16, seconds=30),
        "15m": timedelta(minutes=46, seconds=30),
        "1h": timedelta(hours=3, seconds=30),
        "4h": timedelta(hours=12, seconds=30),
        "1d": timedelta(days=3, seconds=30),
    }

    for interval, holding_window in holding_windows.items():
        result = prepare_review_price_path(
            _review(exit_at=_START + holding_window),
            interval=interval,
        )

        assert isinstance(result, ReviewPricePathBasis)
        assert result.interval == interval


def test_price_path_keeps_complete_bar_scope_and_threshold_order() -> None:
    basis = _basis(_review())
    window = _window(
        (
            ("100", "100.5", "99.5", "99.8"),
            ("99.8", "101.2", "99.6", "100.8"),
            ("100.8", "100.9", "98.8", "99.1"),
        )
    )

    result = summarize_review_price_path(basis, window)

    assert result["evidence_status"] == "AVAILABLE"
    assert result["complete_bar_count"] == 3
    assert result["analysis_start_at"] == "2026-08-01T00:01:00+00:00"
    assert result["analysis_end_at"] == "2026-08-01T00:04:00+00:00"
    assert result["best_directional_move_percent"] == "1.2"
    assert result["worst_directional_move_percent"] == "-1.2"
    assert result["maximum_favorable_excursion_r"] == "1.2"
    assert result["maximum_adverse_excursion_r"] == "1.2"
    assert result["ever_favorable_on_complete_bars"] is True
    assert result["first_complete_bar_directional_return_percent"] == "-0.2"
    assert result["one_r_touched"] is True
    assert result["two_r_touched"] is False
    assert result["planned_stop_touched"] is True
    assert result["threshold_sequence"] == "ONE_R_BEFORE_STOP"
    assert result["capital_scaling_authority"] is False


def test_price_path_is_directionally_symmetric_for_short_trades() -> None:
    basis = _basis(_review(direction="SHORT"))
    window = _window(
        (
            ("100", "100.4", "99.7", "99.8"),
            ("99.8", "100.2", "98.6", "99"),
            ("99", "101.1", "98.9", "100.8"),
        )
    )

    result = summarize_review_price_path(basis, window)

    assert result["best_favorable_price"] == "98.6"
    assert result["worst_adverse_price"] == "101.1"
    assert result["best_directional_move_percent"] == "1.4"
    assert result["worst_directional_move_percent"] == "-1.1"
    assert result["first_complete_bar_favorable"] is True
    assert result["threshold_sequence"] == "ONE_R_BEFORE_STOP"


def test_price_path_reports_no_favorable_complete_bar_without_calling_it_zero_risk() -> None:
    basis = _basis(_review())
    result = summarize_review_price_path(
        basis,
        _window(
            (
                ("99.8", "99.9", "99.2", "99.4"),
                ("99.4", "99.7", "98.7", "99"),
                ("99", "99.5", "98.4", "98.8"),
            )
        ),
    )

    assert result["ever_favorable_on_complete_bars"] is False
    assert result["best_directional_move_percent"] == "-0.1"
    assert result["maximum_favorable_excursion_percent"] == "0"
    assert result["maximum_adverse_excursion_percent"] == "1.6"
    assert result["threshold_sequence"] == "STOP_ONLY"


def test_price_path_keeps_same_bar_threshold_order_ambiguous() -> None:
    basis = _basis(_review())
    result = summarize_review_price_path(
        basis,
        _window(
            (
                ("100", "101.1", "98.9", "100"),
                ("100", "100.2", "99.8", "100"),
                ("100", "100.2", "99.8", "100"),
            )
        ),
    )

    assert result["one_r_touched"] is True
    assert result["planned_stop_touched"] is True
    assert result["threshold_sequence"] == "SAME_BAR_AMBIGUOUS"


def test_price_path_preserves_raw_excursion_when_initial_risk_is_unknown() -> None:
    basis = _basis(_review(stop_bps=None))
    result = summarize_review_price_path(
        basis,
        _window(
            (
                ("100", "100.5", "99.5", "100.2"),
                ("100.2", "100.8", "99.8", "100.6"),
                ("100.6", "100.9", "100.1", "100.4"),
            )
        ),
    )

    assert result["evidence_status"] == "AVAILABLE"
    assert result["reason_codes"] == ["INITIAL_RISK_UNKNOWN"]
    assert result["best_directional_move_percent"] == "0.9"
    assert result["maximum_favorable_excursion_r"] is None
    assert result["one_r_touched"] is None
    assert result["threshold_sequence"] == "UNKNOWN"


def test_price_path_distinguishes_no_trade_from_missing_complete_bars() -> None:
    no_trade = _review()
    no_trade["primary_result"] = "NO_ACTION"
    no_trade["resolved_trade_result"] = {
        "fill_count": 0,
        "fills": [],
        "closed": False,
    }

    no_trade_result = prepare_review_price_path(no_trade, interval="1m")
    short_trade_result = prepare_review_price_path(
        _review(exit_at=_START + timedelta(seconds=50)),
        interval="1m",
    )

    assert isinstance(no_trade_result, dict)
    assert no_trade_result["evidence_status"] == "NOT_APPLICABLE"
    assert no_trade_result["reason_codes"] == ["NO_ATTRIBUTED_ENTRY"]
    assert isinstance(short_trade_result, dict)
    assert short_trade_result["evidence_status"] == "UNKNOWN"
    assert short_trade_result["reason_codes"] == ["NO_COMPLETE_HOLDING_BAR"]


def test_price_path_rejects_a_window_that_does_not_cover_the_exact_basis() -> None:
    basis = _basis(_review())
    incomplete = _window(
        (
            ("100", "100.5", "99.5", "100.2"),
            ("100.2", "100.8", "99.8", "100.6"),
        )
    )

    result = summarize_review_price_path(basis, incomplete)

    assert result["evidence_status"] == "UNKNOWN"
    assert result["reason_codes"] == ["MARKET_WINDOW_COVERAGE_MISMATCH"]
    assert result["market_source"] is None
