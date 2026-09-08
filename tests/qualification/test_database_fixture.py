import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from halpha.planning.registry import ONE_SHOT_STRATEGY_ID, describe_strategy
from halpha.public_market import (
    MARKET_INTERVAL_MILLISECONDS,
    MarketContextUnavailable,
    MarketInterval,
)
from tools.qualification.database_fixture import plan_content
from tools.qualification.run_trading_workbench_fixture import (
    FixtureInstrumentRulesProvider,
    FixtureMarketContextProvider,
    FixtureMarketStreamProvider,
)


def test_database_fixture_uses_current_strategy_contract() -> None:
    content = plan_content(
        environment_id="demo-fixture",
        account_ref="demo-account",
        instrument_ref="BTCUSDT-PERP",
        now=datetime(2026, 7, 24, tzinfo=UTC),
        limits=("10", "100", "5"),
    )
    strategy = describe_strategy(ONE_SHOT_STRATEGY_ID)

    assert content.decision_basis.decision_basis_ref == strategy.strategy_id
    assert content.decision_basis.parameters == {"direction": "LONG"}
    assert content.allowed_actions == frozenset(strategy.allowed_action_profiles)


@pytest.mark.parametrize(
    "interval",
    ("1m", "5m", "15m", "1h", "4h", "1d"),
)
def test_workbench_fixture_returns_selected_stop_reference_interval(
    interval: MarketInterval,
) -> None:
    context = asyncio.run(
        FixtureMarketContextProvider().fetch(
            "BTCUSDT-PERP",
            20,
            interval,
        )
    )

    assert context.stop_reference_interval == interval
    assert context.stop_reference_atr_14 == "2"
    assert (
        context.source_cutoff - context.latest_closed_stop_reference_at
        == timedelta(milliseconds=MARKET_INTERVAL_MILLISECONDS[interval])
    )


@pytest.mark.parametrize(
    "interval",
    ("1m", "5m", "15m", "1h", "4h", "1d"),
)
def test_workbench_fixture_returns_valid_aligned_market_windows(
    interval: MarketInterval,
) -> None:
    start = datetime(2026, 7, 31, 17, 57, 59, 123000, tzinfo=UTC)
    window = asyncio.run(
        FixtureMarketContextProvider().fetch_window(
            "BTCUSDT-PERP",
            interval,
            start,
            start + timedelta(days=2),
        )
    )
    interval_milliseconds = MARKET_INTERVAL_MILLISECONDS[interval]

    assert window.interval == interval
    assert 1 <= len(window.bars) <= 300
    assert int(window.bars[0].open_at.timestamp() * 1000) % interval_milliseconds == 0
    assert all(
        bar.close_at - bar.open_at
        == timedelta(milliseconds=interval_milliseconds)
        for bar in window.bars
    )


def test_workbench_fixture_returns_demo_instrument_rules() -> None:
    rules = asyncio.run(
        FixtureInstrumentRulesProvider().fetch("BTCUSDT-PERP")
    )

    assert rules.source == "BINANCE_DEMO_EXCHANGE_INFO"
    assert rules.price_tick_size == "0.1"
    assert rules.limit_quantity_step == "0.001"


def test_workbench_fixture_returns_searchable_scalping_contract_catalog() -> None:
    provider = FixtureInstrumentRulesProvider()

    catalog = asyncio.run(provider.list_contracts())

    assert catalog.source == "BINANCE_DEMO_EXCHANGE_INFO"
    assert [contract.instrument_ref for contract in catalog.contracts] == [
        "ADAUSDT-PERP",
        "BNBUSDT-PERP",
        "BTCUSDT-PERP",
        "ETHUSDT-PERP",
        "SOLUSDT-PERP",
        "XRPUSDT-PERP",
    ]
    assert catalog.contracts[2].base_asset == "BTC"


def test_workbench_fixture_market_stream_stays_live_and_environment_scoped() -> None:
    async def first_events() -> list[object]:
        provider = FixtureMarketStreamProvider()
        events: list[object] = []
        async for event in provider.stream("BTCUSDT-PERP"):
            events.append(event)
            if len(events) == 8:
                await provider.close()
                break
        return events

    events = asyncio.run(first_events())

    assert events[0].state == "LIVE"
    assert all(event.source == "BINANCE_DEMO_PUBLIC" for event in events)
    assert {event.interval for event in events[2:]} == {
        "1m",
        "5m",
        "15m",
        "1h",
        "4h",
        "1d",
    }


def test_workbench_fixture_stream_supports_every_seeded_activation_instrument() -> None:
    async def first_xrp_event() -> object:
        provider = FixtureMarketStreamProvider()
        return await anext(provider.stream("XRPUSDT-PERP"))

    event = asyncio.run(first_xrp_event())

    assert event.state == "LIVE"


def test_workbench_fixture_stream_rejects_unknown_instrument_with_domain_error() -> None:
    async def first_unknown_event() -> object:
        provider = FixtureMarketStreamProvider()
        return await anext(provider.stream("UNKNOWN-PERP"))

    with pytest.raises(
        MarketContextUnavailable,
        match="MARKET_CONTEXT_INSTRUMENT_UNSUPPORTED",
    ):
        asyncio.run(first_unknown_event())
