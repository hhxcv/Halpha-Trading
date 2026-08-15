from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from halpha.planning.order_policies import (
    CancelOnShockRule,
    ClosedBarPrice15mCondition,
    ConditionFacts,
    ConditionGroup,
    ConditionOperator,
    ConditionResult,
    DecisionBasisReadyCondition,
    InitialStopSpec,
    MarkPriceCondition,
    NumericComparator,
    PriceMoveBpsCondition,
    ProfitLockMode,
    ProfitLockRule,
    ProtectionPolicy,
    ProtectionStep,
    RepriceEntryRule,
    SpreadBpsCondition,
    SteppedProtectionRule,
    TakeProfitLadderSpec,
    TakeProfitLevel,
    compile_protection_targets,
    evaluate_condition_group,
    validate_dynamic_rules,
)


def test_condition_group_uses_kleene_all_and_any_semantics() -> None:
    items = (
        DecisionBasisReadyCondition(),
        SpreadBpsCondition(maximum_bps="10"),
    )
    incomplete = ConditionFacts(basis_ready=True)

    all_result = evaluate_condition_group(
        ConditionGroup(operator=ConditionOperator.ALL, items=items),
        incomplete,
    )
    any_result = evaluate_condition_group(
        ConditionGroup(operator=ConditionOperator.ANY, items=items),
        incomplete,
    )

    assert all_result.result is ConditionResult.UNKNOWN
    assert any_result.result is ConditionResult.TRUE
    assert all_result.item_results == (
        ConditionResult.TRUE,
        ConditionResult.UNKNOWN,
    )


def test_initial_stop_uses_the_discipline_budget_not_a_static_half_price_cap() -> None:
    assert InitialStopSpec(distance_bps="7500").distance_bps == "7500"
    with pytest.raises(ValidationError, match="PROTECTION_POLICY_INVALID"):
        InitialStopSpec(distance_bps="10000")


def test_price_spread_and_window_move_conditions_are_exact() -> None:
    facts = ConditionFacts(
        mark_price="100",
        bid_price="99.95",
        ask_price="100.05",
        price_move_bps_by_window={5: "-25"},
    )
    group = ConditionGroup(
        items=(
            MarkPriceCondition(comparator=NumericComparator.LTE, price="100"),
            SpreadBpsCondition(maximum_bps="10"),
            PriceMoveBpsCondition(
                comparator=NumericComparator.ABS_GTE,
                threshold_bps="20",
                window_seconds=5,
            ),
        )
    )

    evaluated = evaluate_condition_group(group, facts)

    assert evaluated.result is ConditionResult.TRUE


def test_closed_15m_bar_condition_uses_only_its_explicit_close_fact() -> None:
    condition = ClosedBarPrice15mCondition(
        comparator=NumericComparator.LTE,
        price="62970.5",
    )

    assert evaluate_condition_group(
        ConditionGroup(items=(condition,)),
        ConditionFacts(
            mark_price="62900",
            closed_bar_15m_close="62980",
            closed_bar_15m_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        ),
    ).result is ConditionResult.FALSE
    assert evaluate_condition_group(
        ConditionGroup(items=(condition,)),
        ConditionFacts(
            mark_price="63000",
            closed_bar_15m_close="62970.5",
            closed_bar_15m_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        ),
    ).result is ConditionResult.TRUE
    assert evaluate_condition_group(
        ConditionGroup(items=(condition,)),
        ConditionFacts(mark_price="62900"),
    ).result is ConditionResult.UNKNOWN


def test_downward_move_condition_uses_a_positive_user_facing_magnitude() -> None:
    condition = PriceMoveBpsCondition(
        comparator=NumericComparator.DROP_GTE,
        threshold_bps="20",
        window_seconds=5,
    )

    assert evaluate_condition_group(
        ConditionGroup(items=(condition,)),
        ConditionFacts(price_move_bps_by_window={5: "-20"}),
    ).result is ConditionResult.TRUE
    assert evaluate_condition_group(
        ConditionGroup(items=(condition,)),
        ConditionFacts(price_move_bps_by_window={5: "-19.999"}),
    ).result is ConditionResult.FALSE
    assert evaluate_condition_group(
        ConditionGroup(items=(condition,)),
        ConditionFacts(price_move_bps_by_window={5: "25"}),
    ).result is ConditionResult.FALSE


def test_condition_group_rejects_empty_or_more_than_eight_items() -> None:
    with pytest.raises(ValidationError, match="ENTRY_CONDITION_COUNT_INVALID"):
        ConditionGroup(items=())
    with pytest.raises(ValidationError, match="ENTRY_CONDITION_COUNT_INVALID"):
        ConditionGroup(items=tuple(DecisionBasisReadyCondition() for _ in range(9)))


def test_take_profit_and_stepped_stop_sequences_are_monotonic_and_bounded() -> None:
    with pytest.raises(ValidationError, match="TAKE_PROFIT_TRIGGER_ORDER_INVALID"):
        TakeProfitLadderSpec(
            levels=(
                TakeProfitLevel(trigger_r="2", quantity_fraction="0.5"),
                TakeProfitLevel(trigger_r="1", quantity_fraction="0.5"),
            )
        )
    with pytest.raises(ValidationError, match="TAKE_PROFIT_FRACTION_EXCEEDED"):
        TakeProfitLadderSpec(
            levels=(
                TakeProfitLevel(trigger_r="1", quantity_fraction="0.6"),
                TakeProfitLevel(trigger_r="2", quantity_fraction="0.5"),
            )
        )
    with pytest.raises(ValidationError, match="DYNAMIC_RULE_STOP_NOT_MONOTONIC"):
        SteppedProtectionRule(
            steps=(
                ProtectionStep(trigger_r="1", stop_r="0"),
                ProtectionStep(trigger_r="2", stop_r="-0.5"),
            )
        )


def test_fill_relative_protection_rounds_toward_tighter_prices() -> None:
    policy = ProtectionPolicy(
        initial_stop=InitialStopSpec(distance_bps="105"),
        take_profit_ladder=TakeProfitLadderSpec(
            levels=(
                TakeProfitLevel(trigger_r="1", quantity_fraction="0.5"),
                TakeProfitLevel(trigger_r="2", quantity_fraction="0.5"),
            )
        ),
    )

    long_targets = compile_protection_targets(
        policy,
        direction="LONG",
        fill_price="100",
        price_tick_size="0.1",
    )
    short_targets = compile_protection_targets(
        policy,
        direction="SHORT",
        fill_price="100",
        price_tick_size="0.1",
    )

    assert Decimal(long_targets.risk_distance) == Decimal("1.05")
    assert long_targets.initial_stop_price == "99"
    assert long_targets.take_profit_prices == ("101", "102.1")
    assert short_targets.initial_stop_price == "101"
    assert short_targets.take_profit_prices == ("99", "97.9")


@pytest.mark.parametrize("direction", ("LONG", "SHORT"))
def test_fill_relative_protection_rejects_prices_collapsed_to_fill_tick(
    direction: str,
) -> None:
    policy = ProtectionPolicy(
        initial_stop=InitialStopSpec(distance_bps="1"),
        take_profit_ladder=TakeProfitLadderSpec(
            levels=(TakeProfitLevel(trigger_r="1", quantity_fraction="1"),)
        ),
    )

    with pytest.raises(ValueError, match="PROTECTION_PRICE_INVALID"):
        compile_protection_targets(
            policy,
            direction=direction,  # type: ignore[arg-type]
            fill_price="100",
            price_tick_size="1",
        )


@pytest.mark.parametrize("direction", ("LONG", "SHORT"))
def test_fill_relative_protection_rejects_take_profits_collapsed_to_same_tick(
    direction: str,
) -> None:
    policy = ProtectionPolicy(
        initial_stop=InitialStopSpec(distance_bps="100"),
        take_profit_ladder=TakeProfitLadderSpec(
            levels=(
                TakeProfitLevel(trigger_r="0.01", quantity_fraction="0.5"),
                TakeProfitLevel(trigger_r="0.02", quantity_fraction="0.5"),
            )
        ),
    )

    with pytest.raises(ValueError, match="PROTECTION_PRICE_INVALID"):
        compile_protection_targets(
            policy,
            direction=direction,  # type: ignore[arg-type]
            fill_price="100",
            price_tick_size="0.1",
        )


def test_dynamic_rule_thresholds_reject_non_positive_values() -> None:
    with pytest.raises(ValidationError, match="DYNAMIC_RULE_INVALID"):
        CancelOnShockRule(window_seconds=5, adverse_move_bps="0")


def test_market_invalidation_rule_accepts_price_or_complete_shock_pair() -> None:
    assert CancelOnShockRule(invalidation_price="101").invalidation_price == "101"
    assert (
        CancelOnShockRule(opportunity_missed_price="99")
        .opportunity_missed_price
        == "99"
    )
    assert (
        CancelOnShockRule(window_seconds=5, adverse_move_bps="25").window_seconds
        == 5
    )

    with pytest.raises(ValidationError, match="DYNAMIC_RULE_INVALID"):
        CancelOnShockRule()
    with pytest.raises(ValidationError, match="DYNAMIC_RULE_INVALID"):
        CancelOnShockRule(window_seconds=5)
    with pytest.raises(ValidationError, match="DYNAMIC_RULE_INVALID"):
        CancelOnShockRule(adverse_move_bps="25")


def test_entry_reprice_rule_is_bounded_and_coherent() -> None:
    rule = RepriceEntryRule(
        trigger_distance_bps="5",
        book_offset_bps="0",
        maximum_total_move_bps="30",
        minimum_update_interval_seconds=10,
        max_adjustments=3,
    )

    assert rule.book_offset_bps == "0"
    assert rule.max_adjustments == 3
    with pytest.raises(ValidationError, match="DYNAMIC_RULE_INVALID"):
        RepriceEntryRule(
            trigger_distance_bps="31",
            maximum_total_move_bps="30",
        )
    with pytest.raises(ValidationError, match="DYNAMIC_RULE_INVALID"):
        RepriceEntryRule(book_offset_bps="501")


def test_profit_lock_modes_are_explicit_and_mutually_exclusive() -> None:
    ratio = ProfitLockRule(
        mode=ProfitLockMode.RATIO,
        activation_r="1",
        lock_fraction="0.5",
        minimum_step_r="0.25",
    )
    giveback = ProfitLockRule(
        mode=ProfitLockMode.FIXED_GIVEBACK,
        activation_r="1",
        lock_fraction=None,
        giveback_r="0.75",
    )

    assert ratio.lock_fraction == "0.5"
    assert giveback.giveback_r == "0.75"
    with pytest.raises(ValidationError, match="DYNAMIC_RULE_INVALID"):
        ProfitLockRule(
            mode=ProfitLockMode.RATIO,
            lock_fraction="0.5",
            giveback_r="0.5",
        )
    with pytest.raises(ValidationError, match="DYNAMIC_RULE_INVALID"):
        ProfitLockRule(
            mode=ProfitLockMode.FIXED_GIVEBACK,
            lock_fraction=None,
            giveback_r=None,
        )


def test_continuous_and_stepped_profit_protection_cannot_run_together() -> None:
    policy = ProtectionPolicy(
        initial_stop=InitialStopSpec(distance_bps="100"),
    )

    with pytest.raises(ValueError, match="DYNAMIC_PROTECTION_MODE_CONFLICT"):
        validate_dynamic_rules(
            (
                SteppedProtectionRule(
                    steps=(ProtectionStep(trigger_r="1", stop_r="0.5"),)
                ),
                ProfitLockRule(),
            ),
            protection_policy=policy,
        )
