from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from halpha.app.api_models import ReviewSequenceEvidenceResponse
from halpha.outcomes.sequence_evidence import (
    prepare_review_sequence,
    summarize_review_sequence,
)


_START = datetime(2026, 8, 1, tzinfo=UTC)


def _review(
    review_id: str,
    index: int,
    net_pnl: str,
    *,
    intent: str | None = "PROFIT_SEEKING",
    classification: str | None = "USABLE_SAMPLE",
    status: str = "COMPLETE",
    reliable: bool = True,
) -> dict[str, object]:
    closed_at = _START + timedelta(minutes=index)
    decision_context = {"intent": intent} if intent is not None else {}
    evaluations = (
        {"owner_conclusion": {"result": classification, "reason": ""}}
        if classification is not None
        else {}
    )
    return {
        "review_id": review_id,
        "review_version": 2,
        "status": status,
        "fact_cutoff": (closed_at + timedelta(seconds=5)).isoformat(),
        "evaluations": evaluations,
        "trade_context": {
            "instrument_ref": "BTCUSDT-PERP",
            "direction": "LONG",
            "decision_context": decision_context,
        },
        "resolved_trade_result": {
            "fill_count": 2,
            "fills": [{"trade_id": f"{review_id}-entry"}],
            "calculation_complete": reliable,
            "execution_cost_complete": reliable,
            "closed": reliable,
            "strategy_attribution_complete": reliable,
            "net_pnl": net_pnl if reliable else None,
            "commission": "0.1" if reliable else None,
            "entry_notional": "100",
            "fill_times_complete": reliable,
            "last_fill_time": closed_at.isoformat() if reliable else None,
            "result_scope": "HALPHA_ATTRIBUTED_ACTIONS",
        },
    }


def _no_trade(review_id: str, index: int) -> dict[str, object]:
    review = _review(review_id, index, "0")
    review["resolved_trade_result"] = {
        "fill_count": 0,
        "fills": [],
        "calculation_complete": False,
        "closed": False,
    }
    return review


def test_account_sequence_keeps_reliable_results_without_calling_them_strategy() -> None:
    reviews = [
        _review("validation", 1, "3", intent="VALIDATION", classification="VALIDATION_TRADE"),
        _review("tooling", 2, "-4", classification="TOOLING_ISSUE"),
        _review("pending", 3, "2", classification=None, status="DRAFT"),
        _no_trade("no-trade", 4),
    ]

    selection = prepare_review_sequence(reviews, scope="ACCOUNT_RESULTS")

    assert [trade.review_id for trade in selection.trades] == [
        "validation",
        "tooling",
        "pending",
    ]
    assert selection.exclusions == {"NO_ATTRIBUTED_TRADE": 1}
    assert summarize_review_sequence(selection)["metrics"]["net_pnl"] == "1"


def test_account_sequence_treats_empty_fill_list_as_no_trade_when_count_missing() -> None:
    review = _review("empty-fills", 1, "0")
    review["resolved_trade_result"] = {"fills": []}

    selection = prepare_review_sequence([review], scope="ACCOUNT_RESULTS")

    assert selection.trades == ()
    assert selection.exclusions == {"NO_ATTRIBUTED_TRADE": 1}


def test_profit_seeking_sequence_refuses_to_infer_missing_pretrade_intent() -> None:
    reviews = [
        _review("good", 1, "2"),
        _review("missing-intent", 2, "2", intent=None),
        _review("validation", 3, "2", intent="VALIDATION"),
        _review("pending", 4, "2", status="DRAFT"),
        _review("tooling", 5, "2", classification="TOOLING_ISSUE"),
        _review("unreliable", 6, "2", reliable=False),
    ]

    selection = prepare_review_sequence(reviews, scope="PROFIT_SEEKING")

    assert [trade.review_id for trade in selection.trades] == ["good"]
    assert selection.exclusions == {
        "MISSING_DECISION_INTENT": 1,
        "PENDING_REVIEW": 1,
        "TOOLING_ISSUE": 1,
        "UNRELIABLE_RESULT": 1,
        "VALIDATION_INTENT": 1,
    }


def test_sequence_reports_runs_drawdown_and_posthoc_tail_sensitivity() -> None:
    values = ("10", "-5", "-2", "4", "3", "-20", "0")
    selection = prepare_review_sequence(
        [_review(f"review-{index}", index, value) for index, value in enumerate(values, 1)],
        scope="ACCOUNT_RESULTS",
    )

    result = summarize_review_sequence(selection)
    metrics = result["metrics"]

    assert [segment["result_kind"] for segment in result["segments"]] == [
        "WIN",
        "LOSS",
        "WIN",
        "LOSS",
        "FLAT",
    ]
    assert [segment["trade_count"] for segment in result["segments"]] == [1, 2, 2, 1, 1]
    assert metrics["net_pnl"] == "-10"
    assert metrics["maximum_drawdown"] == "20"
    assert metrics["longest_win_segment_index"] == 2
    assert metrics["longest_loss_segment_index"] == 1
    assert metrics["best_win_segment_index"] == 0
    assert metrics["worst_loss_segment_index"] == 3
    assert metrics["worst_trade_net_pnl"] == "-20"
    assert metrics["net_pnl_without_worst_trade"] == "10"
    assert metrics["largest_loss_share_percent"] == "74.07407407407407407407407407"
    assert metrics["top_loss_count"] == 3
    assert metrics["top_loss_total"] == "27"
    assert metrics["net_pnl_without_top_losses"] == "17"
    assert metrics["top_loss_share_percent"] == "100"
    assert result["capital_scaling_authority"] is False


def test_sequence_aggregates_available_and_unknown_price_paths_without_zero_fill() -> None:
    selection = prepare_review_sequence(
        [
            _review("winner", 1, "3"),
            _review("loser", 2, "-2"),
            _review("unknown", 3, "-1"),
        ],
        scope="ACCOUNT_RESULTS",
    )
    paths = {
        ("winner", 2): {
            "evidence_status": "AVAILABLE",
            "reason_codes": [],
            "market_source_cutoff": (_START + timedelta(minutes=5)).isoformat(),
            "ever_favorable_on_complete_bars": True,
            "maximum_favorable_excursion_percent": "2",
            "maximum_adverse_excursion_percent": "0.5",
            "maximum_favorable_excursion_r": "2",
            "maximum_adverse_excursion_r": "0.5",
            "first_complete_bar_favorable": True,
            "threshold_sequence": "ONE_R_ONLY",
        },
        ("loser", 2): {
            "evidence_status": "AVAILABLE",
            "reason_codes": [],
            "market_source_cutoff": (_START + timedelta(minutes=6)).isoformat(),
            "ever_favorable_on_complete_bars": False,
            "maximum_favorable_excursion_percent": "0",
            "maximum_adverse_excursion_percent": "1.5",
            "maximum_favorable_excursion_r": "0",
            "maximum_adverse_excursion_r": "1.5",
            "first_complete_bar_favorable": False,
            "threshold_sequence": "STOP_ONLY",
        },
        ("unknown", 2): {
            "evidence_status": "UNKNOWN",
            "reason_codes": ["NO_COMPLETE_HOLDING_BAR"],
            "market_source_cutoff": None,
            "ever_favorable_on_complete_bars": None,
            "maximum_favorable_excursion_percent": None,
            "maximum_adverse_excursion_percent": None,
            "maximum_favorable_excursion_r": None,
            "maximum_adverse_excursion_r": None,
            "first_complete_bar_favorable": None,
            "threshold_sequence": "UNKNOWN",
        },
    }

    result = summarize_review_sequence(
        selection,
        price_paths=paths,
        price_path_requested=True,
        price_path_interval="1m",
    )
    path = result["price_path"]

    assert path["trade_count"] == 3
    assert path["available_count"] == 2
    assert path["unknown_count"] == 1
    assert path["ever_favorable_count"] == 1
    assert path["never_favorable_count"] == 1
    assert path["median_mfe_percent"] == "1"
    assert path["median_mae_percent"] == "1"
    assert path["reason_counts"] == {"NO_COMPLETE_HOLDING_BAR": 1}
    assert path["by_result"]["WIN"]["ever_favorable_count"] == 1
    assert path["by_result"]["LOSS"]["never_favorable_count"] == 1
    assert result["trades"][2]["price_path"]["evidence_status"] == "UNKNOWN"
    assert ReviewSequenceEvidenceResponse.model_validate(result).price_path is not None


def test_sequence_range_is_inclusive_and_requires_timezone() -> None:
    reviews = [_review("one", 1, "1"), _review("two", 2, "1")]
    selection = prepare_review_sequence(
        reviews,
        scope="ACCOUNT_RESULTS",
        range_start=_START + timedelta(minutes=2),
        range_end=_START + timedelta(minutes=2),
    )

    assert [trade.review_id for trade in selection.trades] == ["two"]
    assert selection.exclusions == {"OUTSIDE_RANGE": 1}
    with pytest.raises(ValueError, match="REVIEW_SEQUENCE_RANGE_TIMEZONE_REQUIRED"):
        prepare_review_sequence(
            reviews,
            scope="ACCOUNT_RESULTS",
            range_start=datetime(2026, 8, 1),
        )


def test_sequence_without_losses_keeps_tail_sensitivity_unknown() -> None:
    selection = prepare_review_sequence(
        [_review("win", 1, "2"), _review("flat", 2, "0")],
        scope="ACCOUNT_RESULTS",
    )

    metrics = summarize_review_sequence(selection)["metrics"]

    assert metrics["gross_loss"] == "0"
    assert metrics["profit_factor"] is None
    assert metrics["worst_trade_net_pnl"] is None
    assert metrics["net_pnl_without_worst_trade"] is None
    assert metrics["largest_loss_share_percent"] is None
    assert metrics["top_loss_count"] == 0
