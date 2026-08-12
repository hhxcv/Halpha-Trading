from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from halpha.domain_values import content_digest
from halpha.planning.indicators import IndicatorBar, native_donchian_atr_snapshot
from halpha.planning.registry import (
    FixedDirectExecutionBasis,
    ONE_SHOT_STRATEGY_ID,
    OneShotParameters,
    build_fixed_plan_basis,
    describe_strategy,
    fixed_decision_basis_runtime_incompatibility,
    strategy_parameter_schema,
    render_strategy_registry,
    strategy_allowed_plan_intents,
    strategy_decision_intent_incompatibility,
    validate_parameters,
)
from halpha.planning.strategies.one_shot import (
    ActivationStrategyState,
    EntryEvaluationInput,
    InstrumentQuantityRules,
    OneShotDonchianAtrLogic,
)
from halpha.source_identity import source_file_sha256


ROOT = Path(__file__).resolve().parents[2]


def _parameters(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {"direction": "LONG"}
    values.update(updates)
    return values


def _bars(count: int = 20) -> tuple[IndicatorBar, ...]:
    start = 1_767_225_600_000_000_000
    bars = []
    for index in range(count):
        close = 100 + index
        bars.append(
            IndicatorBar(
                open=str(close - 1),
                high=str(close + 2),
                low=str(close - 2),
                close=str(close),
                volume="1",
                ts_event_ns=start + (index + 1) * 900_000_000_000,
            )
        )
    return tuple(bars)


def test_static_registry_and_schema_are_build_bound() -> None:
    definition = describe_strategy(ONE_SHOT_STRATEGY_ID)
    schema = strategy_parameter_schema(ONE_SHOT_STRATEGY_ID)
    assert definition.strategy_version == "1.0.1"
    assert "Donchian 通道" in definition.value_logic
    assert "15 分钟通道突破" in definition.applicable_scenarios
    assert "ATR 止损和两档止盈" in definition.execution_behavior
    assert (
        definition.economic_scope["profitability_evidence"]
        == "NO_POSITIVE_EXPECTANCY_EVIDENCE"
    )
    assert (
        definition.economic_scope["recommended_use"]
        == "EXECUTION_CHAIN_VALIDATION_ONLY"
    )
    assert definition.economic_scope["allowed_plan_intents"] == ["VALIDATION"]
    assert strategy_allowed_plan_intents(definition.economic_scope) == frozenset(
        {"VALIDATION"}
    )
    assert strategy_decision_intent_incompatibility(
        definition.economic_scope,
        "VALIDATION",
    ) is None
    assert strategy_decision_intent_incompatibility(
        definition.economic_scope,
        "PROFIT_SEEKING",
    ) == "STRATEGY_DECISION_INTENT_NOT_QUALIFIED"
    assert definition.economic_scope["evidence_limit"] == (
        "固定短周期规则在费用后的历史开发样本中未取得正期望；"
        "仅用于验证计划、成交、保护和退出链路，不应用于盈利目标。"
    )
    assert definition.implementation_digest == source_file_sha256(
        ROOT / "src/halpha/planning/strategies/one_shot.py"
    )
    assert schema["additionalProperties"] is False
    assert schema["properties"]["initial_stop_atr_multiple"]["type"] == "string"
    assert definition.native_indicators == (
        "nautilus_trader.indicators.DonchianChannel",
        "nautilus_trader.indicators.AverageTrueRange",
    )
    assert [
        item.parameter_key for item in definition.plan_key_parameters
    ] == [
        "demo_immediate_entry",
        "channel_lookback_15m",
        "confirmation_bars_1m",
        "entry_valid_minutes",
        "initial_stop_atr_multiple",
        "max_entry_extension_atr",
        "take_profit_1_fraction",
        "take_profit_1_r",
        "take_profit_2_r",
        "max_hold_bars_15m",
    ]
    assert (ROOT / "src/halpha/planning/strategy_registry.json").read_text(
        encoding="utf-8"
    ) == render_strategy_registry()


def test_strategy_definition_cannot_claim_profit_intent_without_positive_evidence() -> None:
    definition = describe_strategy(ONE_SHOT_STRATEGY_ID)
    values = definition.model_dump(mode="python")
    values["economic_scope"] = {
        **definition.economic_scope,
        "allowed_plan_intents": ["PROFIT_SEEKING"],
    }

    with pytest.raises(
        ValidationError,
        match="STRATEGY_PROFIT_INTENT_NOT_EVIDENCE_QUALIFIED",
    ):
        type(definition).model_validate(values)


def test_parameter_validation_is_authoritative_and_exact() -> None:
    normalized = validate_parameters(ONE_SHOT_STRATEGY_ID, _parameters())
    assert normalized["initial_stop_atr_multiple"] == "1.5"
    assert normalized["take_profit_1_fraction"] == "0.5"
    assert normalized["entry_valid_minutes"] == 60
    assert normalized["max_hold_bars_15m"] == 4
    assert validate_parameters(
        ONE_SHOT_STRATEGY_ID,
        _parameters(channel_lookback_15m=4),
    )["channel_lookback_15m"] == 4
    with pytest.raises(ValidationError):
        validate_parameters(
            ONE_SHOT_STRATEGY_ID,
            _parameters(channel_lookback_15m=3),
        )
    with pytest.raises(ValidationError):
        validate_parameters(ONE_SHOT_STRATEGY_ID, _parameters(extra="not allowed"))
    with pytest.raises(ValidationError):
        validate_parameters(
            ONE_SHOT_STRATEGY_ID,
            _parameters(take_profit_1_r="3.0", take_profit_2_r="3.0"),
        )
    with pytest.raises(ValidationError):
        validate_parameters(
            ONE_SHOT_STRATEGY_ID,
            _parameters(initial_stop_atr_multiple=float("nan")),
        )


def test_fixed_basis_binds_parameters_and_product_build() -> None:
    basis = build_fixed_plan_basis(
        ONE_SHOT_STRATEGY_ID,
        _parameters(direction="SHORT"),
        product_build_id="a" * 64,
    )
    assert basis.normalized_parameters["direction"] == "SHORT"
    assert basis.product_build_id == "a" * 64
    assert basis.parameter_digest == content_digest(basis.normalized_parameters)


def test_runtime_compatibility_uses_frozen_semantics_not_product_build() -> None:
    direct = FixedDirectExecutionBasis(
        parameter_digest=content_digest({}),
        product_build_id="b" * 64,
    )
    strategy = build_fixed_plan_basis(
        ONE_SHOT_STRATEGY_ID,
        _parameters(),
        product_build_id="b" * 64,
    )

    assert fixed_decision_basis_runtime_incompatibility(direct) is None
    assert fixed_decision_basis_runtime_incompatibility(strategy) is None
    assert fixed_decision_basis_runtime_incompatibility(
        strategy.model_copy(update={"implementation_digest": "0" * 64})
    ) == "PLAN_STRATEGY_RUNTIME_INCOMPATIBLE"


def test_native_indicator_boundary_uses_qualified_classes() -> None:
    snapshot = native_donchian_atr_snapshot(
        instrument_id="BTCUSDT-PERP.BINANCE",
        lookback=20,
        bars=_bars(),
    )
    assert snapshot.initialized is True
    assert snapshot.upper == "121"
    assert snapshot.lower == "98"
    assert float(snapshot.atr) > 0
    with pytest.raises(ValueError, match="INDICATOR_WINDOW_ORDER_INVALID"):
        native_donchian_atr_snapshot(
            instrument_id="BTCUSDT-PERP.BINANCE",
            lookback=20,
            bars=tuple(reversed(_bars())),
        )


def test_native_indicator_boundary_supports_short_donchian_with_atr_warmup() -> None:
    snapshot = native_donchian_atr_snapshot(
        instrument_id="BTCUSDT-PERP.BINANCE",
        lookback=4,
        bars=_bars(15),
    )

    assert snapshot.initialized is True
    assert snapshot.upper == "116"
    assert snapshot.lower == "109"
    assert float(snapshot.atr) > 0


def test_one_shot_logic_is_deterministic_and_consumes_only_explicit_state() -> None:
    now = datetime(2026, 7, 17, 8, tzinfo=UTC)
    snapshot = native_donchian_atr_snapshot(
        instrument_id="BTCUSDT-PERP.BINANCE",
        lookback=20,
        bars=_bars(),
    )
    evaluation = EntryEvaluationInput(
        activation_id="activation-1",
        instrument_id="BTCUSDT-PERP.BINANCE",
        source_identity=(
            f"activation-1:BAR:{snapshot.source_cutoff_ns}:"
            f"{snapshot.source_cutoff_ns + 1}"
        ),
        source_cutoff=now - timedelta(minutes=1),
        input_digest="c" * 64,
        decision_at=now,
        valid_until=now + timedelta(seconds=30),
        confirmation_closes=("121.5", "121.6"),
        confirmation_close_times=(
            now - timedelta(minutes=2),
            now - timedelta(minutes=1),
        ),
        indicators=snapshot,
        reference_price="121.7",
        reference_source="BACKTEST_LAST_BAR_PROXY",
        max_allowed_loss="100",
        max_notional="1000",
        max_margin="200",
        effective_leverage="5",
        taker_fee_rate="0.0006",
        rules=InstrumentQuantityRules(
            step_size="0.001",
            price_tick_size="0.1",
            min_quantity="0.001",
            max_market_quantity="100",
            min_notional="5",
        ),
    )
    logic = OneShotDonchianAtrLogic(OneShotParameters(direction="LONG"))
    first = logic.evaluate_entry(evaluation, ActivationStrategyState())
    replay = logic.evaluate_entry(evaluation, ActivationStrategyState())
    consumed = logic.evaluate_entry(
        evaluation,
        ActivationStrategyState(entry_opportunity_consumed=True),
    )
    paused = logic.evaluate_entry(
        evaluation,
        ActivationStrategyState(run_state="PAUSED"),
    )
    assert first == replay
    assert first.proposal is not None
    assert first.proposal.entry_risk_context is not None
    setup = first.proposal.entry_risk_context.setup_evidence
    assert setup is not None
    assert setup.mode == "DONCHIAN_BREAKOUT"
    assert setup.trigger_boundary == snapshot.upper
    assert setup.confirmation_closes == ("121.5", "121.6")
    assert setup.confirmation_close_times[-1] == evaluation.source_cutoff
    assert setup.confirmation_all_beyond_boundary is True
    assert setup.last_confirmation_within_max_extension is True
    assert first.proposal.proposal_digest == replay.proposal.proposal_digest
    assert consumed.proposal is None
    assert consumed.reason_code == "ENTRY_OPPORTUNITY_CONSUMED"
    assert paused.proposal is None
    assert paused.reason_code == "NEW_RISK_NOT_ALLOWED"


def test_demo_immediate_entry_uses_the_same_one_shot_proposal_path() -> None:
    now = datetime(2026, 7, 17, 8, tzinfo=UTC)
    snapshot = native_donchian_atr_snapshot(
        instrument_id="BTCUSDT-PERP.BINANCE",
        lookback=20,
        bars=_bars(),
    )
    evaluation = EntryEvaluationInput(
        activation_id="activation-demo-check",
        instrument_id="BTCUSDT-PERP.BINANCE",
        source_identity="activation-demo-check:bar:1",
        source_cutoff=now,
        input_digest="c" * 64,
        decision_at=now,
        valid_until=now + timedelta(seconds=30),
        confirmation_closes=("110",),
        confirmation_close_times=(now,),
        indicators=snapshot,
        reference_price="110",
        reference_source="BACKTEST_LAST_BAR_PROXY",
        max_allowed_loss="100",
        max_notional="1000",
        max_margin="200",
        effective_leverage="5",
        taker_fee_rate="0.0006",
        rules=InstrumentQuantityRules(
            step_size="0.001",
            price_tick_size="0.1",
            min_quantity="0.001",
            max_market_quantity="100",
            min_notional="5",
        ),
    )

    result = OneShotDonchianAtrLogic(
        OneShotParameters(
            direction="LONG",
            demo_immediate_entry=True,
        )
    ).evaluate_entry(evaluation, ActivationStrategyState())

    assert result.proposal is not None
    assert result.proposal.rule_id == "DEMO_ORDER_FLOW_CHECK"
    assert result.proposal.entry_risk_context is not None
    assert result.proposal.entry_risk_context.setup_evidence is not None
    assert (
        result.proposal.entry_risk_context.setup_evidence.mode
        == "DEMO_ORDER_FLOW_CHECK"
    )
    assert result.proposal.reason_code == "DEMO_ORDER_FLOW_CHECK_REQUESTED"
    assert result.proposal.action_profile == "ENTRY_MARKET"


def test_pure_strategy_source_has_no_framework_or_venue_write_dependency() -> None:
    path = ROOT / "src/halpha/planning/strategies/one_shot.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not any(name.startswith("nautilus_trader") for name in imports)
    forbidden = (
        "submit_order",
        "cancel_order",
        "modify_order",
        "close_position",
        "market_exit",
        "ExecutionClient",
        "OrderFactory",
        "TradingNode",
    )
    assert all(token not in source for token in forbidden)
