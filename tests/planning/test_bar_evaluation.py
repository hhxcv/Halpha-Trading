from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.objects import Price, Quantity

from halpha.planning.adapter import HalphaStrategyAdapter
from halpha.planning.bar_evaluation import (
    BarEvaluationError,
    EntrySizingSnapshot,
    NautilusBarEntryEvaluator,
)
from halpha.planning.registry import Direction, OneShotParameters
from halpha.planning.strategies.one_shot import (
    ActivationStrategyState,
    InstrumentQuantityRules,
    OneShotDonchianAtrLogic,
)


SOURCE = BarType.from_str("BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL")
TARGET = BarType.from_str("BTCUSDT-PERP.BINANCE-15-MINUTE-LAST-EXTERNAL")


def _bar(bar_type: BarType, *, minute: int, close: str, high: str, low: str) -> Bar:
    timestamp = int(
        (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minute)).timestamp()
        * 1_000_000_000
    )
    return Bar(
        bar_type=bar_type,
        open=Price.from_str(close),
        high=Price.from_str(high),
        low=Price.from_str(low),
        close=Price.from_str(close),
        volume=Quantity.from_str("100"),
        ts_event=timestamp,
        ts_init=timestamp,
    )


def _evaluator(
    *,
    requires_live_warmup: bool = False,
    channel_lookback_15m: int = 20,
    demo_immediate_entry: bool = False,
) -> NautilusBarEntryEvaluator:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return NautilusBarEntryEvaluator(
        activation_id="activation-bar-evaluation",
        instrument_ref="BTCUSDT-PERP",
        parameters=OneShotParameters(
            direction=Direction.LONG,
            channel_lookback_15m=channel_lookback_15m,
            demo_immediate_entry=demo_immediate_entry,
        ),
        decision_not_before=start,
        valid_until=start + timedelta(days=1),
        sizing_provider=lambda bar: EntrySizingSnapshot(
            reference_price=str(bar.close),
            reference_source="BACKTEST_LAST_BAR_PROXY",
            max_allowed_loss="50",
            max_notional="500",
            max_margin="100",
            effective_leverage="5",
            taker_fee_rate="0.0006",
            rules=InstrumentQuantityRules(
                step_size="0.0001",
                price_tick_size="0.1",
                min_quantity="0.0001",
                max_market_quantity="100",
                min_notional="5",
            ),
        ),
        requires_live_warmup=requires_live_warmup,
    )


def test_shared_adapter_builds_one_normalized_proposal_from_native_bars() -> None:
    evaluator = _evaluator()
    proposals = []
    adapter = HalphaStrategyAdapter(
        activation_id=evaluator.activation_id,
        logic=OneShotDonchianAtrLogic(evaluator.parameters),
        state_provider=lambda: ActivationStrategyState(),
        proposal_sink=proposals.append,
        bar_evaluator=evaluator,
    )
    for index in range(20):
        adapter.on_bar(
            _bar(
                TARGET,
                minute=(index + 1) * 15,
                close=str(100 + index),
                high=str(101 + index),
                low=str(99 + index),
            )
        )
    adapter.on_bar(_bar(SOURCE, minute=301, close="120.5", high="121", low="120"))
    adapter.on_bar(_bar(SOURCE, minute=302, close="120.8", high="121", low="120"))

    assert len(proposals) == 1
    assert proposals[0].activation_id == evaluator.activation_id
    assert proposals[0].reference_source == "BACKTEST_LAST_BAR_PROXY"
    assert proposals[0].action_profile == "ENTRY_MARKET"
    expected_target_ts = _bar(
        TARGET,
        minute=300,
        close="119",
        high="120",
        low="118",
    ).ts_event
    expected_source_ts = _bar(
        SOURCE,
        minute=302,
        close="120.8",
        high="121",
        low="120",
    ).ts_event
    assert proposals[0].source_identity == (
        f"{evaluator.activation_id}:BAR:{expected_target_ts}:{expected_source_ts}"
    )


def test_adapter_accepts_observation_only_bar_evaluator() -> None:
    observed: list[Bar] = []

    class ObservationEvaluator:
        subscribed_bar_types = (TARGET,)
        target_bar_type = TARGET
        target_history_count = 1
        warmup_complete = True

        @staticmethod
        def try_warm_from_cached_bars(*, target_bars) -> bool:
            return bool(tuple(target_bars))

        @staticmethod
        def accept_historical(_bar: Bar) -> None:
            return None

        @staticmethod
        def complete_live_warmup() -> None:
            return None

        @staticmethod
        def accept(bar: Bar) -> None:
            observed.append(bar)

    adapter = HalphaStrategyAdapter(
        activation_id="observation-only-bars",
        bar_evaluator=ObservationEvaluator(),
    )
    bar = _bar(TARGET, minute=15, close="100", high="101", low="99")

    adapter.on_bar(bar)

    assert observed == [bar]


def test_adapter_skips_live_sizing_after_entry_opportunity_is_consumed() -> None:
    evaluator = _evaluator()
    sizing_reads = 0

    def sizing_provider(bar: Bar) -> EntrySizingSnapshot:
        nonlocal sizing_reads
        sizing_reads += 1
        return EntrySizingSnapshot(
            reference_price=str(bar.close),
            reference_source="LIVE_TOP_OF_BOOK_ASK",
            max_allowed_loss="50",
            max_notional="500",
            max_margin="100",
            effective_leverage="5",
            taker_fee_rate="0.0006",
            rules=InstrumentQuantityRules(
                step_size="0.0001",
                price_tick_size="0.1",
                min_quantity="0.0001",
                max_market_quantity="100",
                min_notional="5",
            ),
        )

    evaluator.sizing_provider = sizing_provider
    state = {"consumed": False}
    adapter = HalphaStrategyAdapter(
        activation_id=evaluator.activation_id,
        logic=OneShotDonchianAtrLogic(evaluator.parameters),
        state_provider=lambda: ActivationStrategyState(
            entry_opportunity_consumed=state["consumed"],
        ),
        proposal_sink=lambda _proposal: None,
        bar_evaluator=evaluator,
    )
    for index in range(20):
        adapter.on_bar(
            _bar(
                TARGET,
                minute=(index + 1) * 15,
                close=str(100 + index),
                high=str(101 + index),
                low=str(99 + index),
            )
        )
    adapter.on_bar(_bar(SOURCE, minute=301, close="120.5", high="121", low="120"))
    state["consumed"] = True
    adapter.on_bar(_bar(SOURCE, minute=302, close="120.8", high="121", low="120"))

    assert sizing_reads == 0


def test_demo_flow_check_uses_the_next_closed_minute_with_default_parameters() -> None:
    evaluator = _evaluator(demo_immediate_entry=True)
    for index in range(20):
        evaluator.accept(
            _bar(
                TARGET,
                minute=(index + 1) * 15,
                close=str(100 + index),
                high=str(101 + index),
                low=str(99 + index),
            )
        )

    evaluation = evaluator.accept(
        _bar(SOURCE, minute=301, close="120", high="121", low="119")
    )

    assert evaluation is not None
    assert evaluation.confirmation_closes == ("120",)


def test_duplicate_bar_is_idempotent_and_conflicting_identity_fails_closed() -> None:
    evaluator = _evaluator()
    first = _bar(TARGET, minute=15, close="100", high="101", low="99")
    evaluator.accept(first)
    evaluator.accept(first)

    with pytest.raises(BarEvaluationError, match="TARGET_BAR_IDENTITY_CONFLICT"):
        evaluator.accept(_bar(TARGET, minute=15, close="101", high="102", low="100"))


def test_live_warmup_primes_indicators_without_historical_proposals() -> None:
    evaluator = _evaluator(requires_live_warmup=True)
    with pytest.raises(BarEvaluationError, match="LIVE_WARMUP_NOT_COMPLETE"):
        evaluator.accept(_bar(SOURCE, minute=1, close="100", high="101", low="99"))

    for index in range(20):
        evaluator.accept_historical(
            _bar(
                TARGET,
                minute=(index + 1) * 15,
                close=str(100 + index),
                high=str(101 + index),
                low=str(99 + index),
            )
        )
    evaluator.complete_live_warmup()
    assert evaluator.warmup_complete is True
    assert evaluator.accept(
        _bar(SOURCE, minute=300, close="120", high="121", low="119")
    ) is None
    proposal_input = evaluator.accept(
        _bar(SOURCE, minute=301, close="120.5", high="121", low="120")
    )
    assert proposal_input is not None


def test_short_channel_keeps_full_atr_warmup_and_uses_recent_donchian() -> None:
    evaluator = _evaluator(
        requires_live_warmup=True,
        channel_lookback_15m=4,
    )
    for index in range(15):
        evaluator.accept_historical(
            _bar(
                TARGET,
                minute=(index + 1) * 15,
                close=str(100 + index),
                high=str(101 + index),
                low=str(99 + index),
            )
        )
    evaluator.complete_live_warmup()
    assert evaluator.accept(
        _bar(SOURCE, minute=226, close="115.5", high="116", low="115")
    ) is None
    proposal_input = evaluator.accept(
        _bar(SOURCE, minute=227, close="115.6", high="116", low="115")
    )

    assert proposal_input is not None
    assert proposal_input.indicators.upper == "115"
    assert proposal_input.indicators.lower == "110"
    assert Decimal(proposal_input.indicators.atr) > 0


def test_live_warmup_rejects_a_gapped_target_tail() -> None:
    evaluator = _evaluator(requires_live_warmup=True)
    for index in (*range(9), *range(10, 21)):
        evaluator.accept_historical(
            _bar(
                TARGET,
                minute=(index + 1) * 15,
                close=str(100 + index),
                high=str(101 + index),
                low=str(99 + index),
            )
        )
    with pytest.raises(BarEvaluationError, match="TARGET_WARMUP_INCOMPLETE"):
        evaluator.complete_live_warmup()


def test_live_source_gap_restarts_confirmation_window() -> None:
    evaluator = _evaluator()
    for index in range(20):
        evaluator.accept(
            _bar(
                TARGET,
                minute=(index + 1) * 15,
                close=str(100 + index),
                high=str(101 + index),
                low=str(99 + index),
            )
        )

    assert evaluator.accept(
        _bar(SOURCE, minute=301, close="120.5", high="121", low="120")
    ) is None
    assert evaluator.accept(
        _bar(SOURCE, minute=303, close="120.8", high="121", low="120")
    ) is None
    assert evaluator.accept(
        _bar(SOURCE, minute=304, close="120.9", high="121", low="120")
    ) is not None


def test_live_target_gap_requires_history_rewarm_before_new_risk() -> None:
    evaluator = _evaluator(requires_live_warmup=True)
    for index in range(evaluator.target_history_count):
        evaluator.accept_historical(
            _bar(
                TARGET,
                minute=15 * (index + 1),
                close=str(100 + index),
                high=str(101 + index),
                low=str(99 + index),
            )
        )
    evaluator.complete_live_warmup()
    next_minute = 15 * (evaluator.target_history_count + 2)

    with pytest.raises(BarEvaluationError, match="TARGET_BAR_GAP"):
        evaluator.accept(
            _bar(
                TARGET,
                minute=next_minute,
                close="150",
                high="151",
                low="149",
            )
        )


def test_next_external_target_bar_replaces_warmup_channel_after_mid_interval_start() -> None:
    evaluator = _evaluator(requires_live_warmup=True, channel_lookback_15m=4)
    for index in range(15):
        evaluator.accept_historical(
            _bar(
                TARGET,
                minute=(index + 1) * 15,
                close=str(100 + index),
                high=str(101 + index),
                low=str(99 + index),
            )
        )
    evaluator.complete_live_warmup()

    evaluator.accept(
        _bar(TARGET, minute=240, close="149", high="150", low="148")
    )
    assert evaluator.accept(
        _bar(SOURCE, minute=241, close="150.5", high="151", low="150")
    ) is None
    evaluation = evaluator.accept(
        _bar(SOURCE, minute=242, close="150.6", high="151", low="150")
    )

    assert evaluation is not None
    assert evaluation.indicators.upper == "150"
    assert evaluation.indicators.source_cutoff_ns == _bar(
        TARGET, minute=240, close="149", high="150", low="148"
    ).ts_event


def test_live_adapter_warms_history_before_subscribing_to_bars() -> None:
    evaluator = _evaluator(requires_live_warmup=True)
    adapter = HalphaStrategyAdapter(
        activation_id=evaluator.activation_id,
        logic=OneShotDonchianAtrLogic(evaluator.parameters),
        state_provider=lambda: ActivationStrategyState(),
        proposal_sink=lambda _proposal: None,
        instrument_ref="BTCUSDT-PERP",
        bar_evaluator=evaluator,
        live_history_warmup=True,
        current_time_provider=lambda: datetime(2026, 1, 2, 6, 7, 30, tzinfo=UTC),
        history_cache_provider=lambda _bar_type: (),
    )
    request_id = uuid4()
    events: list[tuple[str, object]] = []

    adapter.subscribe_mark_prices = lambda instrument_id: events.append(
        ("mark", instrument_id)
    )
    adapter.subscribe_quote_ticks = lambda instrument_id: events.append(
        ("quote", instrument_id)
    )
    adapter.subscribe_bars = lambda bar_type: events.append(("bar", bar_type))

    def request_bars(bar_type, **kwargs):
        events.append(("request", (bar_type, kwargs)))
        return request_id

    adapter.request_bars = request_bars
    adapter.on_start()

    assert [event[0] for event in events] == ["mark", "quote", "request"]
    bar_type, request = events[-1][1]
    assert bar_type == evaluator.target_bar_type
    assert bar_type.is_externally_aggregated()
    assert request["update_catalog"] is False
    assert request["limit"] == evaluator.target_history_count + 2
    assert request["end"] - request["start"] < timedelta(hours=6)

    for index in range(20):
        adapter.on_historical_data(
            _bar(
                TARGET,
                minute=(index + 1) * 15,
                close=str(100 + index),
                high=str(101 + index),
                low=str(99 + index),
            )
        )
    request["callback"](request_id)

    assert evaluator.warmup_complete is True
    assert adapter.live_history_ready is True
    assert events[-2:] == [
        ("bar", SOURCE),
        ("bar", evaluator.target_bar_type),
    ]


def test_live_adapter_reuses_framework_cache_before_requesting_history() -> None:
    evaluator = _evaluator(
        requires_live_warmup=True,
        channel_lookback_15m=4,
    )
    target_history = [
        _bar(
            TARGET,
            minute=(index + 1) * 15,
            close=str(100 + index),
            high=str(101 + index),
            low=str(99 + index),
        )
        for index in range(15)
    ]
    cache_reads: list[BarType] = []
    events: list[tuple[str, object]] = []

    def cached_bars(bar_type: BarType) -> list[Bar]:
        cache_reads.append(bar_type)
        if str(bar_type) == str(TARGET):
            return list(reversed(target_history))
        return []

    adapter = HalphaStrategyAdapter(
        activation_id=evaluator.activation_id,
        logic=OneShotDonchianAtrLogic(evaluator.parameters),
        state_provider=lambda: ActivationStrategyState(),
        proposal_sink=lambda _proposal: None,
        instrument_ref="BTCUSDT-PERP",
        bar_evaluator=evaluator,
        live_history_warmup=True,
        history_cache_provider=cached_bars,
    )
    adapter.subscribe_mark_prices = lambda instrument_id: events.append(
        ("mark", instrument_id)
    )
    adapter.subscribe_quote_ticks = lambda instrument_id: events.append(
        ("quote", instrument_id)
    )
    adapter.subscribe_bars = lambda bar_type: events.append(("bar", bar_type))
    adapter.request_bars = lambda *_args, **_kwargs: pytest.fail(
        "cached history must avoid a duplicate framework history request"
    )

    adapter.on_start()

    assert cache_reads == [TARGET]
    assert evaluator.warmup_complete is True
    assert adapter.live_history_ready is True
    assert events[-2:] == [
        ("bar", SOURCE),
        ("bar", evaluator.target_bar_type),
    ]


def test_adapter_releases_supported_feeds_and_leaves_mark_teardown_to_node() -> None:
    evaluator = _evaluator()
    adapter = HalphaStrategyAdapter(
        activation_id="market-feed-duty",
        instrument_ref="BTCUSDT-PERP",
        bar_evaluator=evaluator,
        quote_event_sink=lambda _tick: None,
        mark_price_event_sink=lambda _update: None,
    )
    events: list[tuple[str, object]] = []
    adapter.subscribe_mark_prices = lambda instrument_id: events.append(
        ("subscribe-mark", instrument_id)
    )
    adapter.subscribe_quote_ticks = lambda instrument_id: events.append(
        ("subscribe-quote", instrument_id)
    )
    adapter.unsubscribe_mark_prices = lambda _instrument_id: pytest.fail(
        "the fixed Binance data client has no mark-price unsubscribe implementation"
    )
    adapter.unsubscribe_quote_ticks = lambda instrument_id: events.append(
        ("unsubscribe-quote", instrument_id)
    )
    adapter.subscribe_bars = lambda bar_type: events.append(("subscribe-bar", bar_type))
    adapter.unsubscribe_bars = lambda bar_type: events.append(("unsubscribe-bar", bar_type))

    adapter.on_start()
    adapter.on_stop()

    assert [event[0] for event in events] == [
        "subscribe-bar",
        "subscribe-bar",
        "subscribe-mark",
        "subscribe-quote",
        "unsubscribe-bar",
        "unsubscribe-bar",
        "unsubscribe-quote",
    ]
    assert [value for kind, value in events if kind == "unsubscribe-bar"] == list(
        reversed(evaluator.subscribed_bar_types)
    )


def test_adapter_does_not_subscribe_market_feeds_without_a_consumer() -> None:
    adapter = HalphaStrategyAdapter(
        activation_id="risk-reduction-only-duty",
        instrument_ref="BTCUSDT-PERP",
        market_data_required=False,
        execution_event_sink=lambda _event: None,
    )
    events: list[tuple[str, object]] = []
    adapter.subscribe_mark_prices = lambda instrument_id: events.append(
        ("subscribe-mark", instrument_id)
    )
    adapter.subscribe_quote_ticks = lambda instrument_id: events.append(
        ("subscribe-quote", instrument_id)
    )

    adapter.on_start()

    assert events == []
    assert adapter.market_data_instrument_id is None


def test_adapter_reports_bar_evaluation_failure_before_reraising() -> None:
    evaluator = _evaluator()
    failures: list[tuple[object, Exception]] = []
    adapter = HalphaStrategyAdapter(
        activation_id=evaluator.activation_id,
        logic=OneShotDonchianAtrLogic(evaluator.parameters),
        state_provider=lambda: ActivationStrategyState(),
        proposal_sink=lambda _proposal: None,
        bar_evaluator=evaluator,
        bar_failure_sink=lambda bar, exception: failures.append((bar, exception)),
    )
    bar = _bar(SOURCE, minute=1, close="100", high="101", low="99")

    with pytest.raises(BarEvaluationError, match="SOURCE_BAR_OUT_OF_ORDER"):
        adapter.on_bar(bar)
        adapter.on_bar(
            _bar(SOURCE, minute=0, close="100", high="101", low="99")
        )

    assert len(failures) == 1
    assert str(failures[0][1]) == "SOURCE_BAR_OUT_OF_ORDER"


def test_adapter_reports_history_warmup_failure_before_reraising() -> None:
    evaluator = _evaluator(requires_live_warmup=True)
    failures: list[tuple[str, object | None, Exception]] = []
    adapter = HalphaStrategyAdapter(
        activation_id=evaluator.activation_id,
        logic=OneShotDonchianAtrLogic(evaluator.parameters),
        state_provider=lambda: ActivationStrategyState(),
        proposal_sink=lambda _proposal: None,
        bar_evaluator=evaluator,
        live_history_warmup=True,
        history_warmup_failure_sink=lambda stage, item, exception: failures.append(
            (stage, item, exception)
        ),
    )
    newer = _bar(TARGET, minute=30, close="100", high="101", low="99")
    older = _bar(TARGET, minute=15, close="100", high="101", low="99")

    adapter.on_historical_data(newer)
    with pytest.raises(BarEvaluationError, match="TARGET_BAR_OUT_OF_ORDER"):
        adapter.on_historical_data(older)

    assert len(failures) == 1
    assert failures[0][0] == "DATA"
    assert failures[0][1] is older
    assert str(failures[0][2]) == "TARGET_BAR_OUT_OF_ORDER"
