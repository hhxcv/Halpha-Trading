from decimal import Decimal

from halpha.outcomes.repeatability import assess_playbook_repeatability


def _assessment(
    r_values: list[str],
    *,
    intent: str = "PROFIT_SEEKING",
    missing_risk_at: int | None = None,
) -> dict[str, object]:
    values = [Decimal(value) for value in r_values]
    max_losses: list[Decimal | None] = [Decimal("10")] * len(values)
    if missing_risk_at is not None:
        max_losses[missing_risk_at] = None
    pnl_values = [value * Decimal("10") for value in values]
    return assess_playbook_repeatability(
        intent=intent,
        net_pnl_values=pnl_values,
        max_allowed_losses=max_losses,
        gross_profit=sum((max(value, Decimal(0)) for value in pnl_values), Decimal(0)),
        gross_loss=sum((max(-value, Decimal(0)) for value in pnl_values), Decimal(0)),
    )


def test_repeatability_screen_is_not_applicable_to_validation_intent() -> None:
    result = _assessment(["0.2"] * 30, intent="VALIDATION")

    assert result["status"] == "NOT_APPLICABLE_VALIDATION"
    assert result["reason_codes"] == []
    assert result["live_promotion_authority"] is False
    assert result["capital_scaling_authority"] is False


def test_repeatability_screen_requires_a_fixed_risk_basis_and_minimum_sample() -> None:
    result = _assessment(["0.2"] * 12, missing_risk_at=3)

    assert result["status"] == "NOT_READY"
    assert result["reason_codes"] == [
        "MINIMUM_SAMPLE_NOT_MET",
        "RISK_BASIS_INCOMPLETE",
    ]
    assert result["risk_basis_trade_count"] == 11
    assert result["mean_r_lower_confidence_bound"] is None


def test_repeatability_screen_rejects_best_trade_dependence() -> None:
    result = _assessment(["-0.1"] * 29 + ["4"])

    assert result["status"] == "NOT_READY"
    assert "BEST_TRADE_DEPENDENCE" in result["reason_codes"]
    assert "EARLY_SEGMENT_NOT_POSITIVE" in result["reason_codes"]
    assert result["net_r_multiple"] == "1.1"
    assert result["net_r_without_best_trade"] == "-2.9"


def test_repeatability_screen_rejects_recent_regime_failure() -> None:
    result = _assessment(["0.3"] * 20 + ["-0.2"] * 10)

    assert result["status"] == "NOT_READY"
    assert "RECENT_SEGMENT_NOT_POSITIVE" in result["reason_codes"]
    assert result["early_segment_net_r"] == "6"
    assert result["recent_segment_net_r"] == "-2"


def test_repeatability_screen_accepts_only_as_a_non_authoritative_candidate() -> None:
    result = _assessment(["0.2"] * 30)

    assert result["status"] == "EVIDENCE_CANDIDATE"
    assert result["reason_codes"] == []
    assert result["risk_basis_trade_count"] == 30
    assert result["net_r_multiple"] == "6"
    assert result["net_r_without_best_trade"] == "5.8"
    assert result["early_segment_net_r"] == "4"
    assert result["recent_segment_net_r"] == "2"
    assert Decimal(str(result["mean_r_lower_confidence_bound"])) > 0
    assert result["live_promotion_authority"] is False
    assert result["capital_scaling_authority"] is False


def test_repeatability_bootstrap_is_deterministic() -> None:
    values = ["0.4", "-0.1", "0.3", "0.2", "-0.05"] * 6

    first = _assessment(values)
    second = _assessment(values)

    assert first == second
