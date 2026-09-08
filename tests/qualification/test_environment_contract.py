from __future__ import annotations

import asyncio
import inspect
import json
import subprocess
import sys
from datetime import datetime
from datetime import timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from nautilus_trader.adapters.binance.http.error import BinanceClientError
from nautilus_trader.adapters.binance.http.error import BinanceServerError
from nautilus_trader.core.nautilus_pyo3 import HttpMethod

from tools.qualification.nautilus_fixtures import DemoReduceOnlyTopologyStrategy
from tools.qualification.probe_binance_demo_clients import _deduplicate_funding_records
from tools.qualification.probe_binance_demo_clients import _effective_leverage
from tools.qualification.probe_binance_demo_clients import _funding_read_failure_disposition
from tools.qualification.probe_binance_demo_clients import _margin_leverage_compatibility_vector
from tools.qualification.probe_binance_demo_clients import _query_funding_income_window
from tools.qualification.probe_binance_demo_clients import _three_calendar_months_ago_ms
from tools.qualification.probe_binance_demo_clients import _validate_funding_income_page
from tools.qualification.probe_binance_demo_clients import QualificationError
from tools.qualification.probe_binance_demo_15m_crosscheck import _compare_15m_with_1m
from tools.qualification.probe_binance_demo_order_roundtrip import (
    _query_algo_order_until_visible,
)
from tools.qualification.probe_binance_demo_reduce_only_topology import (
    _wait_for_terminal_algo_status,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class _SequencedAlgoOrderAPI:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.client_order_ids: list[str] = []

    async def query_algo_order(
        self,
        *,
        client_algo_id: str,
        recv_window: str,
    ) -> object | None:
        assert recv_window == "5000"
        self.client_order_ids.append(client_algo_id)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _FundingClock:
    def timestamp_ms(self) -> int:
        return 1_700_000_000_000


class _FundingHttpClient:
    def __init__(self, pages: dict[int, list[dict[str, object]] | Exception]) -> None:
        self.pages = pages
        self.calls: list[dict[str, object]] = []

    async def sign_request(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        payload = kwargs["payload"]
        assert isinstance(payload, dict)
        page = int(payload["page"])
        result = self.pages[page]
        if isinstance(result, Exception):
            raise result
        return json.dumps(result)


def _run_json_probe(relative_path: str) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, str(REPOSITORY_ROOT / relative_path)],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_repository_venv_is_exact_and_isolated() -> None:
    evidence = _run_json_probe("tools/qualification/verify_venv.py")
    assert evidence["status"] == "QUALIFIED"
    assert evidence["errors"] == []


def test_components_and_winvault_backend_are_exact() -> None:
    evidence = _run_json_probe("tools/qualification/verify_components.py")
    assert evidence["status"] == "QUALIFIED"
    assert evidence["errors"] == []
    assert evidence["keyring_backend"] == "keyring.backends.Windows.WinVaultKeyring"


def test_development_lock_requires_hashes_and_official_index() -> None:
    lock_text = (REPOSITORY_ROOT / "requirements/dev.txt").read_text(encoding="utf-8")
    assert (
        "--index-url https://pypi.org/simple" in lock_text
        or "--index-url=https://pypi.org/simple" in lock_text
    )
    assert "--trusted-host" not in lock_text
    assert "--extra-index-url" not in lock_text
    assert "--hash=sha256:" in lock_text
    assert "pandas==2.3.3" in lock_text
    assert "pandas==3." not in lock_text


def test_repository_venv_is_git_ignored() -> None:
    completed = subprocess.run(
        ["git", "check-ignore", ".venv"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0


def test_windows_primitives_match_runtime_contract() -> None:
    evidence = _run_json_probe("tools/qualification/probe_windows_primitives.py")
    assert evidence["status"] == "QUALIFIED"
    assert evidence["errors"] == []


def test_nautilus_node_and_controller_public_lifecycle() -> None:
    evidence = _run_json_probe("tools/qualification/probe_nautilus_lifecycle.py")
    assert evidence["status"] == "QUALIFIED"
    assert evidence["errors"] == []


def test_binance_demo_configuration_is_explicit_and_nonsecret() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(REPOSITORY_ROOT / "tools/qualification/probe_binance_demo_clients.py"),
            "--config-only",
        ],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    evidence = json.loads(completed.stdout)
    assert evidence["status"] == "QUALIFIED"
    assert evidence["errors"] == []
    assert evidence["configuration"]["profile"] == "BINANCE_DEMO"
    assert evidence["configuration"]["topology"] == {
        "trading_nodes": 1,
        "data_clients": 1,
        "execution_clients": 1,
        "event_store": None,
        "persistent_cache": None,
        "redis": None,
        "order_emulator": None,
    }
    assert evidence["configuration"]["execution_client"]["max_retries"] is None
    assert evidence["profile_isolation"]["providers_distinct_across_profiles"] is True
    assert evidence["profile_isolation"]["real_credentials_loaded"] is False
    assert evidence["profile_isolation"]["network_connection_attempted"] is False
    assert evidence["profile_isolation"]["profiles"] == {
        "BINANCE_DEMO": {
            "base_url_override": False,
            "data_execution_environment_match": True,
            "environment": "DEMO",
            "load_all": True,
            "load_ids": [
                "BTCUSDT-PERP.BINANCE",
                "ETHUSDT-PERP.BINANCE",
            ],
            "provider_shared_within_profile": True,
            "query_commission_rates": False,
            "exchange_change_mode": "DEMO_CHECK_ENABLED",
        },
        "BINANCE_LIVE_READ_ONLY": {
            "base_url_override": False,
            "data_execution_environment_match": True,
            "environment": "LIVE",
            "load_all": True,
            "load_ids": ["BTCUSDT-PERP.BINANCE"],
            "provider_shared_within_profile": True,
            "query_commission_rates": False,
            "exchange_change_mode": "PUBLIC_DATA_ONLY",
        },
        "BINANCE_LIVE_WRITE": {
            "base_url_override": False,
            "data_execution_environment_match": True,
            "environment": "LIVE",
            "load_all": True,
            "load_ids": ["BTCUSDT-PERP.BINANCE"],
            "provider_shared_within_profile": True,
            "query_commission_rates": False,
            "exchange_change_mode": "RUNTIME_GATE_REQUIRED",
        },
    }
    assert "K" * 64 not in completed.stdout
    assert "S" * 64 not in completed.stdout


def test_bar_types_native_aggregation_and_cython_indicators() -> None:
    evidence = _run_json_probe("tools/qualification/probe_bar_semantics.py")
    assert evidence["status"] == "QUALIFIED"
    assert evidence["errors"] == []
    assert evidence["native_aggregation"]["emitted_target_bars"] == 1
    assert evidence["continuity_gate_fixture"]["complete"]["usable"] is True
    assert evidence["continuity_gate_fixture"]["missing_one"]["usable"] is False
    assert evidence["continuity_gate_fixture"]["conflicting_duplicate"]["usable"] is False
    assert evidence["indicators"]["20"]["input_bars"] == 20
    assert evidence["indicators"]["96"]["input_bars"] == 96


def test_official_15m_crosscheck_uses_exact_decimal_aggregation() -> None:
    start_ms = 1_700_000_100_000
    rows = []
    for index in range(15):
        rows.append(
            SimpleNamespace(
                open_time=start_ms + index * 60_000,
                close_time=start_ms + (index + 1) * 60_000 - 1,
                open=str(100 + index),
                high=str(102 + index),
                low=str(99 + index),
                close=str(101 + index),
                volume="0.1",
                asset_volume="10.01",
                trades_count=2,
                taker_base_volume="0.04",
                taker_quote_volume="4.004",
            ),
        )
    aggregate = SimpleNamespace(
        open_time=start_ms,
        close_time=start_ms + 15 * 60_000 - 1,
        open="100",
        high="116",
        low="99",
        close="115",
        volume="1.5",
        asset_volume="150.15",
        trades_count=30,
        taker_base_volume="0.60",
        taker_quote_volume="60.060",
    )
    checks = _compare_15m_with_1m(rows, aggregate)
    assert checks
    assert all(checks.values())


def test_funding_income_validation_cursor_dedup_and_failure_policy() -> None:
    cutoff = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    start_ms = _three_calendar_months_ago_ms(cutoff_ms)
    assert datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc) == datetime(
        2026,
        4,
        30,
        12,
        0,
        tzinfo=timezone.utc,
    )

    page = _validate_funding_income_page(
        [
            {
                "symbol": "BTCUSDT",
                "incomeType": "FUNDING_FEE",
                "income": "-0.00000001",
                "asset": "USDT",
                "info": "",
                "time": cutoff_ms,
                "tranId": 1001,
                "tradeId": "",
            },
        ],
        symbol="BTCUSDT",
        start_time_ms=start_ms,
        end_time_ms=cutoff_ms,
    )
    unique, duplicate_count = _deduplicate_funding_records([*page, *page])
    assert len(unique) == 1
    assert duplicate_count == 1
    assert unique[0]["decimal_places"] == 8
    assert unique[0]["income"] == "-0.00000001"
    assert unique[0]["asset"] == "USDT"

    assert (
        _funding_read_failure_disposition(BinanceClientError(429, {}, {}))
        == "UNKNOWN_TRANSIENT_READ"
    )
    assert (
        _funding_read_failure_disposition(BinanceServerError(503, {}, {}))
        == "UNKNOWN_TRANSIENT_READ"
    )
    assert _funding_read_failure_disposition(TimeoutError()) == "UNKNOWN_TRANSIENT_READ"
    assert _funding_read_failure_disposition(ValueError()) == "REJECTED_NON_TRANSIENT_READ"

    malformed = {
        "symbol": "BTCUSDT",
        "incomeType": "FUNDING_FEE",
        "income": 0.1,
        "asset": "USDT",
        "info": "",
        "time": cutoff_ms,
        "tranId": 1002,
        "tradeId": "",
    }
    with pytest.raises(QualificationError, match="FUNDING_INCOME_NOT_DECIMAL_TEXT"):
        _validate_funding_income_page(
            [malformed],
            symbol="BTCUSDT",
            start_time_ms=start_ms,
            end_time_ms=cutoff_ms,
        )


def test_funding_income_pagination_is_get_only_and_transient_read_is_not_retried() -> None:
    start_ms = 1_699_999_000_000
    end_ms = 1_700_000_000_000

    def record(transaction_id: int) -> dict[str, object]:
        return {
            "symbol": "BTCUSDT",
            "incomeType": "FUNDING_FEE",
            "income": "-0.00000001",
            "asset": "USDT",
            "info": "",
            "time": end_ms,
            "tranId": transaction_id,
            "tradeId": "",
        }

    client = _FundingHttpClient(
        {
            1: [record(transaction_id) for transaction_id in range(100)],
            2: [record(99), record(100)],
        },
    )
    records, evidence = asyncio.run(
        _query_funding_income_window(
            client,
            _FundingClock(),
            symbol="BTCUSDT",
            start_time_ms=start_ms,
            end_time_ms=end_ms,
        ),
    )
    assert len(records) == 101
    assert evidence["page_sizes"] == [100, 2]
    assert evidence["duplicate_identity_count"] == 1
    assert [call["payload"]["page"] for call in client.calls] == ["1", "2"]
    assert all(call["http_method"] == HttpMethod.GET for call in client.calls)
    assert all(call["url_path"] == "/fapi/v1/income" for call in client.calls)

    throttled = _FundingHttpClient({1: BinanceClientError(429, {}, {})})
    with pytest.raises(BinanceClientError):
        asyncio.run(
            _query_funding_income_window(
                throttled,
                _FundingClock(),
                symbol="BTCUSDT",
                start_time_ms=start_ms,
                end_time_ms=end_ms,
            ),
        )
    assert len(throttled.calls) == 1


def test_actual_margin_mode_and_effective_leverage_goldens() -> None:
    crossed_high = _margin_leverage_compatibility_vector(
        "CROSSED",
        20,
        Decimal("100"),
    )
    isolated_low = _margin_leverage_compatibility_vector(
        "ISOLATED",
        3,
        Decimal("100"),
    )
    assert crossed_high == {
        "actual_margin_mode": "CROSSED",
        "actual_leverage": 20,
        "effective_leverage": 5,
        "max_margin": "100",
        "max_notional_from_margin_axis": "500",
        "account_setting_mutation": False,
        "crossed_is_not_represented_as_venue_isolation": True,
    }
    assert isolated_low == {
        "actual_margin_mode": "ISOLATED",
        "actual_leverage": 3,
        "effective_leverage": 3,
        "max_margin": "100",
        "max_notional_from_margin_axis": "300",
        "account_setting_mutation": False,
        "crossed_is_not_represented_as_venue_isolation": False,
    }
    assert _effective_leverage(1) == 1
    assert _effective_leverage(5) == 5
    assert _effective_leverage(20) == 5


def test_algo_order_visibility_wait_reuses_identity_without_write_retry() -> None:
    expected_order = object()
    api = _SequencedAlgoOrderAPI(
        [
            None,
            BinanceClientError(429, {}, {}),
            expected_order,
        ],
    )
    order, attempts, transient_errors = asyncio.run(
        _query_algo_order_until_visible(
            api,  # type: ignore[arg-type]
            "0123456789abcdef0123456789abcdef",
            max_attempts=3,
            delay_seconds=0,
        ),
    )
    assert order is expected_order
    assert attempts == 3
    assert transient_errors == 1
    assert api.client_order_ids == ["0123456789abcdef0123456789abcdef"] * 3


def test_algo_order_terminal_wait_ignores_stale_visible_status() -> None:
    identity = "0123456789abcdef0123456789abcdef"
    expected_order = SimpleNamespace(algoStatus="CANCELED")
    api = _SequencedAlgoOrderAPI(
        [
            SimpleNamespace(algoStatus="NEW"),
            expected_order,
        ],
    )
    order, attempts, transient_errors = asyncio.run(
        _query_algo_order_until_visible(
            api,  # type: ignore[arg-type]
            identity,
            max_attempts=2,
            delay_seconds=0,
            expected_statuses=frozenset({"CANCELED"}),
        ),
    )
    assert order is expected_order
    assert attempts == 2
    assert transient_errors == 0
    assert api.client_order_ids == [identity, identity]


def test_algo_terminal_wait_does_not_treat_visible_new_as_terminal() -> None:
    identity = "0123456789abcdef0123456789abcdef"
    api = _SequencedAlgoOrderAPI(
        [
            SimpleNamespace(algoStatus="NEW"),
            SimpleNamespace(algoStatus="FINISHED"),
        ],
    )
    strategy = SimpleNamespace(
        orders={
            "TP_PARTIAL": SimpleNamespace(
                client_order_id=SimpleNamespace(value=identity),
            ),
        },
    )
    status, attempts, transient_errors = asyncio.run(
        _wait_for_terminal_algo_status(
            api,  # type: ignore[arg-type]
            strategy,  # type: ignore[arg-type]
            "TP_PARTIAL",
            max_attempts=2,
            delay_seconds=0,
        ),
    )
    assert status == "FINISHED"
    assert attempts == 2
    assert transient_errors == 0
    assert api.client_order_ids == [identity, identity]


def test_backtest_stack_uses_public_components_and_one_shot_fixture() -> None:
    evidence = _run_json_probe("tools/qualification/probe_backtest_stack.py")
    assert evidence["status"] == "QUALIFIED"
    assert evidence["errors"] == []
    assert evidence["configuration"]["pandas"] == "2.3.3"
    assert evidence["catalog"]["bar_timestamps_round_trip"] is True
    assert evidence["engine"]["proposal_count"] == 1
    assert evidence["engine"]["fill_count"] == 2
    assert evidence["engine"]["closed_positions"] == 1
    assert evidence["engine"]["open_positions_after_run"] == 0
    assert evidence["engine"]["exit_order_reduce_only"] is True
    assert evidence["engine"]["commission_matches_decimal_price_quantity_rate"] is True
    assert evidence["funding_model"] == "NOT_MODELED"
    assert evidence["funding_data_injected"] is False
    assert evidence["engine_disposed"] is True


def test_live_and_backtest_proposal_adapters_have_parity_without_live_write() -> None:
    evidence = _run_json_probe("tools/qualification/probe_strategy_proposal_parity.py")
    assert evidence["status"] == "QUALIFIED"
    assert evidence["errors"] == []
    assert all(evidence["checks"].values())


def test_eight_order_profiles_and_fixed_binance_mapping() -> None:
    evidence = _run_json_probe("tools/qualification/probe_order_profiles.py")
    assert evidence["status"] == "QUALIFIED"
    assert evidence["errors"] == []
    assert evidence["profile_count"] == 8
    assert [profile["id"] for profile in evidence["profiles"]] == [
        "ENTRY_MARKET",
        "ENTRY_LIMIT",
        "ENTRY_STOP_MARKET",
        "CANCEL_ORDER",
        "PROTECTIVE_STOP_REDUCE_ONLY",
        "TAKE_PROFIT_1",
        "TAKE_PROFIT_2",
        "REDUCE_OR_CLOSE_MARKET",
    ]
    assert evidence["adapter_mapping"] == {
        "ENTRY_MARKET": "MARKET",
        "ENTRY_LIMIT": "LIMIT",
        "ENTRY_STOP_MARKET": "STOP_MARKET",
        "PROTECTIVE_STOP_REDUCE_ONLY": "STOP_MARKET",
        "TAKE_PROFIT_1": "TAKE_PROFIT_MARKET",
        "TAKE_PROFIT_2": "TAKE_PROFIT_MARKET",
        "REDUCE_OR_CLOSE_MARKET": "MARKET",
    }
    assert all(evidence["fixed_adapter_contract"].values())
    assert evidence["conditional_trigger"] == {
        "internal": "LAST_PRICE",
        "binance_working_type": "CONTRACT_PRICE",
        "all_profiles_match": True,
    }
    assert evidence["automatic_write_retry"] == "DISABLED"
    assert not any(evidence["forbidden_profile_use"].values())


def test_reduce_only_demo_fixture_has_one_public_strategy_write_path() -> None:
    source = inspect.getsource(DemoReduceOnlyTopologyStrategy)
    assert source.count("self.submit_order(order)") == 1
    assert "self.cancel_order(order)" in source
    assert "self.query_order(order)" in source
    assert "close_position" not in source
    assert "market_exit" not in source
    assert "modify_order" not in source


def test_fixed_binance_reconnect_and_resubscribe_contract() -> None:
    evidence = _run_json_probe("tools/qualification/probe_reconnect_contract.py")
    assert evidence["status"] == "QUALIFIED_FIXED_SOURCE_CONTRACT"
    assert evidence["errors"] == []
    assert all(evidence["contracts"].values())
    assert evidence["controlled_active_stream_fault"]["status"] == "UNKNOWN_NOT_RUN"


def test_write_retry_and_order_unknown_failure_semantics() -> None:
    evidence = _run_json_probe("tools/qualification/probe_order_failure_semantics.py")
    assert evidence["status"] == "QUALIFIED_COMPONENT_SEMANTICS"
    assert evidence["errors"] == []
    assert evidence["zero_write_retry_black_box"]["call_count"] == 1
    assert evidence["zero_write_retry_black_box"]["retry_count"] == 0
    assert evidence["order_status_read_black_box"] == {
        "cache_open_query_missing": {
            "all_results_none": True,
            "cache_lookup_count": 0,
            "query_call_count": 1,
            "retry_counter_cleared": True,
            "technical_rejection_count": 0,
        },
        "continuous_transient_error": {
            "all_results_none": True,
            "cache_lookup_count": 3,
            "query_call_count": 3,
            "retry_counter_cleared": True,
            "technical_rejection_count": 1,
        },
        "explicit_minus_2013_not_found": {
            "all_results_none": True,
            "cache_lookup_count": 0,
            "query_call_count": 1,
            "retry_counter_cleared": True,
            "technical_rejection_count": 0,
        },
    }
    assert all(evidence["fixed_source_contract"].values())
    assert evidence["required_halpha_interpretation"] == {
        "write_timeout_or_crash": "UNKNOWN",
        "automatic_resubmit_same_identity": False,
        "next_action": "QUERY_ORIGINAL_UUID32_ONLY",
        "single_not_found_proves_not_submitted": False,
        "technical_synthetic_rejected_is_product_terminal": False,
        "cancel_unknown_identity": False,
        "cancel_only_after_open_is_proven": True,
    }


def test_public_websocket_reconnect_resubscribes_all_streams_black_box() -> None:
    evidence = _run_json_probe(
        "tools/qualification/probe_websocket_reconnect_blackbox.py",
    )
    assert evidence["status"] == "QUALIFIED"
    assert evidence["errors"] == []
    black_box = evidence["black_box"]
    assert black_box["connection_count"] >= 2
    assert black_box["reconnect_callback_seen"] is True
    assert black_box["all_streams_present_after_reconnect"] is True
    assert black_box["client_subscriptions_after_reconnect"] == [
        "btcusdt@kline_1m",
        "btcusdt@bookTicker",
        "btcusdt@markPrice@1s",
    ]
    assert black_box["system_proxy_modified"] is False
    assert black_box["private_client_runtime_fields_accessed"] is False


def test_qualification_pure_logic_has_no_nautilus_or_write_api() -> None:
    source = (
        REPOSITORY_ROOT / "tools/qualification/strategy_logic_fixture.py"
    ).read_text(encoding="utf-8")
    forbidden = (
        "nautilus_trader",
        "submit_order",
        "cancel_order",
        "modify_order",
        "close_position",
        "market_exit",
    )
    assert not any(token in source for token in forbidden)
