from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment

import halpha.public_instrument_rules as instrument_rules_module
from halpha.public_instrument_rules import (
    BinancePublicInstrumentRules,
    InstrumentRulesUnavailable,
)


class FakeExchangeInfoApi:
    def __init__(self) -> None:
        self.calls = 0

    async def query_futures_exchange_info(self) -> object:
        self.calls += 1
        return SimpleNamespace(
            serverTime=1_800_000_100_000 + self.calls * 1_000,
            symbols=[
                SimpleNamespace(
                    symbol="BTCUSDT",
                    filters=[
                        SimpleNamespace(
                            filterType="PRICE_FILTER",
                            minPrice="0.1",
                            maxPrice="1000000",
                            tickSize="0.1",
                        ),
                        SimpleNamespace(
                            filterType="LOT_SIZE",
                            minQty="0.0001",
                            maxQty="1000",
                            stepSize="0.0001",
                        ),
                        SimpleNamespace(
                            filterType="MARKET_LOT_SIZE",
                            minQty="0.001",
                            maxQty="100",
                            stepSize="0.001",
                        ),
                        SimpleNamespace(
                            filterType="MIN_NOTIONAL",
                            notional="5",
                        ),
                    ],
                )
            ],
        )


class TimeoutThenExchangeInfoApi(FakeExchangeInfoApi):
    async def query_futures_exchange_info(self) -> object:
        if self.calls == 0:
            self.calls += 1
            raise TimeoutError
        return await super().query_futures_exchange_info()


class AlwaysTimeoutExchangeInfoApi(FakeExchangeInfoApi):
    async def query_futures_exchange_info(self) -> object:
        self.calls += 1
        raise TimeoutError


class SucceedOnceThenTimeoutExchangeInfoApi(FakeExchangeInfoApi):
    async def query_futures_exchange_info(self) -> object:
        if self.calls > 0:
            self.calls += 1
            raise TimeoutError
        return await super().query_futures_exchange_info()


@pytest.mark.parametrize(
    ("profile", "expected_environment", "expected_source"),
    (
        (
            "BINANCE_DEMO",
            BinanceEnvironment.DEMO,
            "BINANCE_DEMO_EXCHANGE_INFO",
        ),
        (
            "BINANCE_LIVE_READ_ONLY",
            BinanceEnvironment.LIVE,
            "BINANCE_LIVE_EXCHANGE_INFO",
        ),
        (
            "BINANCE_LIVE_WRITE",
            BinanceEnvironment.LIVE,
            "BINANCE_LIVE_EXCHANGE_INFO",
        ),
    ),
)
def test_instrument_rule_profile_routes_to_only_its_own_environment(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    expected_environment: BinanceEnvironment,
    expected_source: str,
) -> None:
    environments: list[BinanceEnvironment] = []

    def fake_http_client(**kwargs):
        environments.append(kwargs["environment"])
        return object()

    monkeypatch.setattr(
        instrument_rules_module,
        "get_cached_binance_http_client",
        fake_http_client,
    )
    monkeypatch.setattr(
        instrument_rules_module,
        "BinanceFuturesMarketHttpAPI",
        lambda *_args, **_kwargs: FakeExchangeInfoApi(),
    )

    provider = BinancePublicInstrumentRules(profile)
    rules = asyncio.run(provider.fetch("BTCUSDT-PERP"))

    assert environments == [expected_environment]
    assert rules.source == expected_source


def test_unknown_profile_cannot_fall_back_to_live_instrument_rules() -> None:
    with pytest.raises(ValueError, match="INSTRUMENT_RULES_PROFILE_UNSUPPORTED"):
        BinancePublicInstrumentRules(
            "UNRECOGNIZED_PROFILE",
            market_api=FakeExchangeInfoApi(),
        )


def test_public_instrument_rules_are_limit_rules_and_short_lived_cached() -> None:
    api = FakeExchangeInfoApi()
    provider = BinancePublicInstrumentRules(
        "BINANCE_DEMO",
        market_api=api,
    )

    first = asyncio.run(provider.fetch("BTCUSDT-PERP"))
    second = asyncio.run(provider.fetch("BTCUSDT-PERP"))

    assert first == second
    assert first.source == "BINANCE_DEMO_EXCHANGE_INFO"
    assert first.limit_quantity_step == "0.0001"
    assert first.max_limit_quantity == "1000"
    assert first.source_cutoff is not None
    assert api.calls == 1


def test_contract_catalog_contains_every_active_usdt_perpetual_and_is_cached() -> None:
    class CatalogApi:
        def __init__(self) -> None:
            self.calls = 0

        async def query_futures_exchange_info(self) -> object:
            self.calls += 1
            return {
                "serverTime": 1_800_000_100_000,
                "symbols": [
                    {
                        "symbol": "ETHUSDT",
                        "baseAsset": "ETH",
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                        "contractType": "PERPETUAL",
                    },
                    {
                        "symbol": "BTCUSDT",
                        "baseAsset": "BTC",
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                        "contractType": "PERPETUAL",
                    },
                    {
                        "symbol": "BTCUSDC",
                        "baseAsset": "BTC",
                        "quoteAsset": "USDC",
                        "status": "TRADING",
                        "contractType": "PERPETUAL",
                    },
                    {
                        "symbol": "XRPUSDT_260925",
                        "baseAsset": "XRP",
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                        "contractType": "CURRENT_QUARTER",
                    },
                    {
                        "symbol": "OLDUSDT",
                        "baseAsset": "OLD",
                        "quoteAsset": "USDT",
                        "status": "SETTLING",
                        "contractType": "PERPETUAL",
                    },
                ],
            }

    api = CatalogApi()
    provider = BinancePublicInstrumentRules("BINANCE_LIVE_READ_ONLY", market_api=api)

    async def exercise():
        return await provider.list_contracts(), await provider.list_contracts()

    first, second = asyncio.run(exercise())

    assert first == second
    assert first.source == "BINANCE_LIVE_EXCHANGE_INFO"
    assert [item.instrument_ref for item in first.contracts] == [
        "BTCUSDT-PERP",
        "ETHUSDT-PERP",
    ]
    assert first.contracts[0].base_asset == "BTC"
    assert api.calls == 1


def test_public_instrument_rules_reject_unknown_instrument_shape() -> None:
    provider = BinancePublicInstrumentRules(
        "BINANCE_DEMO",
        market_api=FakeExchangeInfoApi(),
    )

    with pytest.raises(
        InstrumentRulesUnavailable,
        match="INSTRUMENT_RULES_INSTRUMENT_UNSUPPORTED",
    ):
        asyncio.run(provider.fetch("btc/usdt"))


def test_public_instrument_rules_refresh_bypasses_the_live_cache() -> None:
    api = FakeExchangeInfoApi()
    provider = BinancePublicInstrumentRules(
        "BINANCE_DEMO",
        market_api=api,
    )

    async def exercise_refresh() -> tuple[object, object, object]:
        first = await provider.fetch("BTCUSDT-PERP")
        refreshed = await provider.refresh("BTCUSDT-PERP")
        cached_after_refresh = await provider.fetch("BTCUSDT-PERP")
        return first, refreshed, cached_after_refresh

    first, refreshed, cached_after_refresh = asyncio.run(exercise_refresh())

    assert api.calls == 2
    assert first.source_cutoff != refreshed.source_cutoff
    assert refreshed == cached_after_refresh


def test_public_instrument_rules_retry_one_transient_timeout() -> None:
    api = TimeoutThenExchangeInfoApi()
    provider = BinancePublicInstrumentRules(
        "BINANCE_DEMO",
        market_api=api,
        retry_delay_seconds=0,
    )

    rules = asyncio.run(provider.fetch("BTCUSDT-PERP"))

    assert rules.source == "BINANCE_DEMO_EXCHANGE_INFO"
    assert api.calls == 2


def test_public_instrument_rules_fail_closed_after_bounded_timeouts() -> None:
    api = AlwaysTimeoutExchangeInfoApi()
    provider = BinancePublicInstrumentRules(
        "BINANCE_DEMO",
        market_api=api,
        retry_delay_seconds=0,
    )

    async def exercise() -> None:
        for _attempt in range(2):
            with pytest.raises(
                InstrumentRulesUnavailable,
                match="INSTRUMENT_RULES_QUERY_FAILED_TIMEOUTERROR",
            ):
                await provider.fetch("BTCUSDT-PERP")

    asyncio.run(exercise())
    assert api.calls == 2


def test_public_instrument_rules_bound_failed_symbol_state() -> None:
    api = AlwaysTimeoutExchangeInfoApi()
    provider = BinancePublicInstrumentRules(
        profile="BINANCE_DEMO",
        market_api=api,
        max_entries=2,
        query_attempts=1,
        retry_delay_seconds=0,
    )

    async def exercise() -> None:
        for instrument in ("AAA-PERP", "BBB-PERP", "CCC-PERP"):
            with pytest.raises(InstrumentRulesUnavailable):
                await provider.fetch(instrument)
        with pytest.raises(InstrumentRulesUnavailable):
            await provider.fetch("BBB-PERP")
        with pytest.raises(InstrumentRulesUnavailable):
            await provider.fetch("AAA-PERP")

    asyncio.run(exercise())
    assert api.calls == 4


def test_public_instrument_rules_use_bounded_stale_cache_for_preview_timeout() -> None:
    api = SucceedOnceThenTimeoutExchangeInfoApi()
    provider = BinancePublicInstrumentRules(
        "BINANCE_DEMO",
        market_api=api,
        ttl_seconds=0,
        stale_on_error_seconds=900,
        retry_delay_seconds=0,
    )

    async def exercise() -> tuple[object, object, object]:
        first = await provider.fetch("BTCUSDT-PERP")
        stale, concurrent_stale = await asyncio.gather(
            provider.fetch("BTCUSDT-PERP"),
            provider.fetch("BTCUSDT-PERP"),
        )
        return first, stale, concurrent_stale

    first, stale, concurrent_stale = asyncio.run(exercise())

    assert stale == first
    assert concurrent_stale == first
    assert api.calls == 3


def test_public_instrument_rules_refresh_never_uses_stale_cache() -> None:
    api = SucceedOnceThenTimeoutExchangeInfoApi()
    provider = BinancePublicInstrumentRules(
        "BINANCE_DEMO",
        market_api=api,
        ttl_seconds=0,
        stale_on_error_seconds=900,
        retry_delay_seconds=0,
    )

    async def exercise() -> None:
        await provider.fetch("BTCUSDT-PERP")
        with pytest.raises(
            InstrumentRulesUnavailable,
            match="INSTRUMENT_RULES_QUERY_FAILED_TIMEOUTERROR",
        ):
            await provider.refresh("BTCUSDT-PERP")

    asyncio.run(exercise())
    assert api.calls == 3
