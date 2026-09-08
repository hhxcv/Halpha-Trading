from __future__ import annotations

# ruff: noqa: E402

import argparse
import asyncio
import calendar
import hashlib
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from decimal import Decimal
from decimal import InvalidOperation
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

import keyring
import pywintypes
import win32api
import win32con
import win32event
import win32security


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from nautilus_trader.adapters.binance import BINANCE
from nautilus_trader.adapters.binance import BINANCE_VENUE
from nautilus_trader.adapters.binance import BinanceAccountType
from nautilus_trader.adapters.binance import BinanceDataClientConfig
from nautilus_trader.adapters.binance import BinanceExecClientConfig
from nautilus_trader.adapters.binance import BinanceInstrumentProviderConfig
from nautilus_trader.adapters.binance import BinanceLiveDataClientFactory
from nautilus_trader.adapters.binance import BinanceLiveExecClientFactory
from nautilus_trader.adapters.binance import get_cached_binance_http_client
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.adapters.binance.common.enums import BinanceKeyType
from nautilus_trader.adapters.binance.factories import (
    get_cached_binance_futures_instrument_provider,
)
from nautilus_trader.adapters.binance.futures.http.account import (
    BinanceFuturesAccountHttpAPI,
)
from nautilus_trader.adapters.binance.futures.http.wallet import (
    BinanceFuturesWalletHttpAPI,
)
from nautilus_trader.adapters.binance.http.error import BinanceClientError
from nautilus_trader.adapters.binance.http.error import BinanceServerError
from nautilus_trader.common import Environment
from nautilus_trader.common.config import LoggingConfig
from nautilus_trader.core.nautilus_pyo3 import HttpMethod
from nautilus_trader.live.config import LiveDataEngineConfig
from nautilus_trader.live.config import LiveExecEngineConfig
from nautilus_trader.live.config import RoutingConfig
from nautilus_trader.live.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.trading.config import StrategyConfig

from halpha.venue_integration.nautilus_account import (
    MULTI_ASSET_MARGIN_PATH,
    query_single_asset_mode,
)

from tools.qualification.nautilus_fixtures import (
    MarketDataQualificationStrategy,
)


EXPECTED_BACKEND = "keyring.backends.Windows.WinVaultKeyring"
SERVICE = "Halpha/Binance/BINANCE_DEMO"
KEY_ACCOUNT = "api_key"
SECRET_ACCOUNT = "api_secret"
MUTEX_NAME = r"Global\Halpha.Executor.BINANCE_DEMO.WriteOwner"
MUTEX_ALL_ACCESS = 0x001F0001
DEMO_INSTRUMENT_IDS = (
    InstrumentId.from_str("BTCUSDT-PERP.BINANCE"),
    InstrumentId.from_str("ETHUSDT-PERP.BINANCE"),
)
LIVE_INSTRUMENT_IDS = (InstrumentId.from_str("BTCUSDT-PERP.BINANCE"),)
INSTRUMENT_IDS = DEMO_INSTRUMENT_IDS
INSTRUMENT_SYMBOLS = ("BTCUSDT", "ETHUSDT")
BINANCE_CREDENTIAL_ENVIRONMENT_NAMES = (
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "BINANCE_DEMO_API_KEY",
    "BINANCE_DEMO_API_SECRET",
    "BINANCE_FUTURES_TESTNET_API_KEY",
    "BINANCE_FUTURES_TESTNET_API_SECRET",
    "BINANCE_TESTNET_API_KEY",
    "BINANCE_TESTNET_API_SECRET",
)
LOG_DIRECTORY = REPOSITORY_ROOT / "build" / "qualification" / "binance-demo"
FUNDING_INCOME_PATH = "/fapi/v1/income"
FUNDING_INCOME_TYPE = "FUNDING_FEE"
FUNDING_PAGE_LIMIT = 100
FUNDING_POLL_INTERVAL_MS = 60_000
FUNDING_MAX_PAGES = 100
BINANCE_USDM_DEMO_BASE_URL = "https://demo-fapi.binance.com"
BINANCE_USDM_ACCOUNT_DOCUMENTATION = (
    "https://developers.binance.com/en/docs/catalog/"
    "core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account"
)
FUNDING_INCOME_FIELDS = frozenset(
    {"symbol", "incomeType", "income", "asset", "info", "time", "tranId", "tradeId"},
)
READ_ONLY_GET_WHITELIST = frozenset(
    {FUNDING_INCOME_PATH},
)


class QualificationError(Exception):
    """A qualification failure whose message never contains credential values."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _configuration_digest(configuration: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json(configuration).encode("utf-8")).hexdigest()


def _credential_shape_is_valid(value: str | None) -> bool:
    return bool(
        value
        and value.isascii()
        and 32 <= len(value) <= 256
        and not any(character.isspace() for character in value)
    )


def _load_credentials() -> tuple[str, str, str]:
    backend = keyring.get_keyring()
    backend_name = f"{type(backend).__module__}.{type(backend).__qualname__}"
    if backend_name != EXPECTED_BACKEND:
        raise QualificationError("KEYRING_BACKEND_MISMATCH")

    try:
        api_key = keyring.get_password(SERVICE, KEY_ACCOUNT)
        api_secret = keyring.get_password(SERVICE, SECRET_ACCOUNT)
    except Exception as exc:
        raise QualificationError(f"WINVAULT_READ_FAILED:{type(exc).__name__}") from None

    if not _credential_shape_is_valid(api_key):
        raise QualificationError("WINVAULT_API_KEY_MISSING_OR_INVALID")
    if not _credential_shape_is_valid(api_secret):
        raise QualificationError("WINVAULT_API_SECRET_MISSING_OR_INVALID")
    return api_key, api_secret, backend_name


@contextmanager
def _without_binance_credential_environment() -> Iterator[bool]:
    saved = {
        name: os.environ.pop(name)
        for name in BINANCE_CREDENTIAL_ENVIRONMENT_NAMES
        if name in os.environ
    }
    try:
        yield bool(saved)
    finally:
        os.environ.update(saved)


def _current_user_sid():
    process = win32api.GetCurrentProcess()
    token = win32security.OpenProcessToken(process, win32con.TOKEN_QUERY)
    try:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()


def _protected_mutex_security_attributes(user_sid):
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAceEx(
        win32security.ACL_REVISION,
        0,
        MUTEX_ALL_ACCESS,
        user_sid,
    )
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorOwner(user_sid, False)
    descriptor.SetSecurityDescriptorDacl(True, dacl, False)
    descriptor.SetSecurityDescriptorControl(
        win32security.SE_DACL_PROTECTED,
        win32security.SE_DACL_PROTECTED,
    )
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.bInheritHandle = False
    attributes.SECURITY_DESCRIPTOR = descriptor
    return attributes


def _mutex_security_is_exact(handle, user_sid) -> bool:
    descriptor = win32security.GetSecurityInfo(
        handle,
        win32security.SE_KERNEL_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION,
    )
    owner_sid = descriptor.GetSecurityDescriptorOwner()
    dacl = descriptor.GetSecurityDescriptorDacl()
    control, _revision = descriptor.GetSecurityDescriptorControl()
    user_sid_text = win32security.ConvertSidToStringSid(user_sid)
    return bool(
        win32security.ConvertSidToStringSid(owner_sid) == user_sid_text
        and control & win32security.SE_DACL_PROTECTED
        and dacl is not None
        and dacl.GetAceCount() == 1
        and win32security.ConvertSidToStringSid(dacl.GetAce(0)[2]) == user_sid_text
    )


def _acquire_executor_mutex():
    user_sid = _current_user_sid()
    attributes = _protected_mutex_security_attributes(user_sid)
    handle = win32event.CreateMutex(attributes, False, MUTEX_NAME)
    wait_result = win32event.WaitForSingleObject(handle, 0)
    if wait_result == win32con.WAIT_TIMEOUT:
        handle.Close()
        raise QualificationError("EXECUTOR_MUTEX_ALREADY_OWNED")
    if wait_result == win32con.WAIT_ABANDONED:
        win32event.ReleaseMutex(handle)
        handle.Close()
        raise QualificationError("EXECUTOR_MUTEX_ABANDONED")
    if wait_result != win32con.WAIT_OBJECT_0:
        handle.Close()
        raise QualificationError("EXECUTOR_MUTEX_WAIT_FAILED")
    if not _mutex_security_is_exact(handle, user_sid):
        win32event.ReleaseMutex(handle)
        handle.Close()
        raise QualificationError("EXECUTOR_MUTEX_DACL_MISMATCH")
    return handle


def _release_executor_mutex(handle) -> None:
    try:
        win32event.ReleaseMutex(handle)
    finally:
        handle.Close()


def _nonsecret_configuration(proxy_enabled: bool = False) -> dict[str, object]:
    instrument_ids = sorted(str(instrument_id) for instrument_id in INSTRUMENT_IDS)
    return {
        "profile": "BINANCE_DEMO",
        "topology": {
            "trading_nodes": 1,
            "data_clients": 1,
            "execution_clients": 1,
            "event_store": None,
            "persistent_cache": None,
            "redis": None,
            "order_emulator": None,
        },
        "trading_node": {
            "kernel_environment": "LIVE",
            "timeout_connection_secs": 30.0,
            "timeout_disconnection_secs": 15.0,
            "cache": None,
            "message_bus": None,
            "emulator": None,
            "load_state": False,
            "save_state": False,
            "catalogs": [],
            "streaming": None,
        },
        "data_engine": {
            "time_bars_interval_type": "left-open",
            "time_bars_timestamp_on_close": True,
            "time_bars_skip_first_non_full_bar": True,
            "time_bars_build_with_no_updates": False,
            "validate_data_sequence": True,
        },
        "execution_engine": {
            "reconciliation": True,
            "reconciliation_lookback_mins": None,
            "reconciliation_instrument_ids": instrument_ids,
            "reconciliation_startup_delay_secs": 10.0,
            "inflight_check_interval_ms": 0,
            "inflight_check_threshold_ms": 5000,
            "inflight_check_retries": 5,
            "open_check_interval_secs": 10.0,
            "open_check_open_only": True,
            "open_check_lookback_mins": 60,
            "open_check_threshold_ms": 5000,
            "open_check_missing_retries": 5,
            "position_check_interval_secs": 60.0,
            "position_check_lookback_mins": 60,
            "position_check_retries": 3,
            "generate_missing_orders": True,
            "filter_unclaimed_external_orders": False,
            "filter_position_reports": False,
        },
        "instrument_provider": {
            "load_all": True,
            "load_ids": instrument_ids,
            "query_commission_rates": False,
            "shared_by_data_and_execution": True,
        },
        "data_client": {
            "account_type": "USDT_FUTURES",
            "environment": "DEMO",
            "credential_injection": "EXPLICIT_WINVAULT_VALUES",
        },
        "execution_client": {
            "account_type": "USDT_FUTURES",
            "environment": "DEMO",
            "credential_injection": "EXPLICIT_SAME_VALUES_AS_DATA_CLIENT",
            "use_reduce_only": True,
            "use_position_ids": True,
            "use_trade_lite": False,
            "treat_expired_as_canceled": False,
            "recv_window_ms": 5000,
            "max_retries": None,
            "futures_leverages": None,
            "futures_margin_types": None,
        },
        "transport": {
            "proxy": "RUNTIME_LOOPBACK_ARGUMENT" if proxy_enabled else "DISABLED",
        },
        "logging": {
            "log_level": "INFO",
            "log_level_file": "INFO",
            "log_file_format": "JSON",
            "log_file_max_size": 104857600,
            "log_file_max_backup_count": 5,
            "print_config": False,
        },
    }


def _build_profile_client_configs(
    profile: str,
    api_key: str,
    api_secret: str,
    proxy_url: str | None = None,
) -> tuple[
    BinanceInstrumentProviderConfig,
    BinanceDataClientConfig,
    BinanceExecClientConfig,
]:
    profile_specs = {
        "BINANCE_DEMO": (BinanceEnvironment.DEMO, DEMO_INSTRUMENT_IDS),
        "BINANCE_LIVE_READ_ONLY": (BinanceEnvironment.LIVE, LIVE_INSTRUMENT_IDS),
        "BINANCE_LIVE_WRITE": (BinanceEnvironment.LIVE, LIVE_INSTRUMENT_IDS),
    }
    if profile not in profile_specs:
        raise QualificationError("UNKNOWN_BINANCE_PROFILE")
    environment, instrument_ids = profile_specs[profile]
    provider = BinanceInstrumentProviderConfig(
        load_all=True,
        load_ids=frozenset(instrument_ids),
        query_commission_rates=False,
    )
    routing = RoutingConfig(default=True, venues=frozenset({BINANCE}))
    data_client = BinanceDataClientConfig(
        api_key=api_key,
        api_secret=api_secret,
        account_type=BinanceAccountType.USDT_FUTURES,
        environment=environment,
        instrument_provider=provider,
        routing=routing,
        proxy_url=proxy_url,
    )
    exec_client = BinanceExecClientConfig(
        api_key=api_key,
        api_secret=api_secret,
        account_type=BinanceAccountType.USDT_FUTURES,
        environment=environment,
        instrument_provider=provider,
        routing=routing,
        proxy_url=proxy_url,
        use_reduce_only=True,
        use_position_ids=True,
        use_trade_lite=False,
        treat_expired_as_canceled=False,
        recv_window_ms=5000,
        max_retries=None,
        futures_leverages=None,
        futures_margin_types=None,
    )
    return provider, data_client, exec_client


def _build_configuration(
    api_key: str,
    api_secret: str,
    proxy_url: str | None = None,
) -> tuple[
    TradingNodeConfig,
    BinanceInstrumentProviderConfig,
    BinanceDataClientConfig,
    BinanceExecClientConfig,
]:
    provider, data_client, exec_client = _build_profile_client_configs(
        "BINANCE_DEMO",
        api_key=api_key,
        api_secret=api_secret,
        proxy_url=proxy_url,
    )
    node = TradingNodeConfig(
        environment=Environment.LIVE,
        trader_id=TraderId("DIRECT-DEMO-001"),
        cache=None,
        message_bus=None,
        emulator=None,
        streaming=None,
        catalogs=[],
        load_state=False,
        save_state=False,
        timeout_connection=30.0,
        timeout_disconnection=15.0,
        logging=LoggingConfig(
            log_level="INFO",
            log_level_file="INFO",
            log_directory=str(LOG_DIRECTORY),
            log_file_name="direct-binance-demo",
            log_file_format="JSON",
            log_file_max_size=104857600,
            log_file_max_backup_count=5,
            log_colors=False,
            print_config=False,
            clear_log_file=True,
        ),
        data_engine=LiveDataEngineConfig(
            time_bars_interval_type="left-open",
            time_bars_timestamp_on_close=True,
            time_bars_skip_first_non_full_bar=True,
            time_bars_build_with_no_updates=False,
            validate_data_sequence=True,
        ),
        exec_engine=LiveExecEngineConfig(
            reconciliation=True,
            reconciliation_lookback_mins=None,
            reconciliation_instrument_ids=list(INSTRUMENT_IDS),
            reconciliation_startup_delay_secs=10.0,
            inflight_check_interval_ms=0,
            inflight_check_threshold_ms=5000,
            inflight_check_retries=5,
            open_check_interval_secs=10.0,
            open_check_open_only=True,
            open_check_lookback_mins=60,
            open_check_threshold_ms=5000,
            open_check_missing_retries=5,
            position_check_interval_secs=60.0,
            position_check_lookback_mins=60,
            position_check_retries=3,
            generate_missing_orders=True,
            filter_unclaimed_external_orders=False,
            filter_position_reports=False,
        ),
        data_clients={BINANCE: data_client},
        exec_clients={BINANCE: exec_client},
    )
    return node, provider, data_client, exec_client


def _parse_finite_decimal_text(value: object, field: str) -> Decimal:
    if not isinstance(value, str) or not value:
        raise QualificationError(f"{field}_NOT_DECIMAL_TEXT")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise QualificationError(f"{field}_NOT_DECIMAL_TEXT") from None
    if not parsed.is_finite():
        raise QualificationError(f"{field}_NOT_FINITE")
    return parsed


def _effective_leverage(actual_leverage: int) -> int:
    if isinstance(actual_leverage, bool) or not isinstance(actual_leverage, int):
        raise QualificationError("ACTUAL_LEVERAGE_NOT_INTEGER")
    if actual_leverage <= 0:
        raise QualificationError("ACTUAL_LEVERAGE_NOT_POSITIVE")
    return min(actual_leverage, 5)


def _margin_leverage_compatibility_vector(
    margin_mode: str,
    actual_leverage: int,
    max_margin: Decimal,
) -> dict[str, object]:
    normalized_mode = margin_mode.upper()
    if normalized_mode not in {"CROSSED", "ISOLATED"}:
        raise QualificationError("MARGIN_MODE_UNSUPPORTED")
    if not isinstance(max_margin, Decimal) or not max_margin.is_finite() or max_margin <= 0:
        raise QualificationError("MAX_MARGIN_NOT_POSITIVE_FINITE_DECIMAL")
    effective_leverage = _effective_leverage(actual_leverage)
    return {
        "actual_margin_mode": normalized_mode,
        "actual_leverage": actual_leverage,
        "effective_leverage": effective_leverage,
        "max_margin": str(max_margin),
        "max_notional_from_margin_axis": str(max_margin * effective_leverage),
        "account_setting_mutation": False,
        "crossed_is_not_represented_as_venue_isolation": normalized_mode == "CROSSED",
    }


def _three_calendar_months_ago_ms(cutoff_ms: int) -> int:
    cutoff = datetime.fromtimestamp(cutoff_ms / 1000, tz=timezone.utc)
    month_index = cutoff.year * 12 + cutoff.month - 1 - 3
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(cutoff.day, calendar.monthrange(year, month)[1])
    return int(cutoff.replace(year=year, month=month, day=day).timestamp() * 1000)


def _validate_funding_income_page(
    decoded: object,
    *,
    symbol: str,
    start_time_ms: int,
    end_time_ms: int,
) -> list[dict[str, object]]:
    if not isinstance(decoded, list):
        raise QualificationError("FUNDING_INCOME_RESPONSE_NOT_LIST")

    sanitized: list[dict[str, object]] = []
    for item in decoded:
        if not isinstance(item, dict) or not FUNDING_INCOME_FIELDS.issubset(item):
            raise QualificationError("FUNDING_INCOME_RESPONSE_SCHEMA_MISMATCH")
        if item["symbol"] != symbol:
            raise QualificationError("FUNDING_INCOME_SYMBOL_MISMATCH")
        if item["incomeType"] != FUNDING_INCOME_TYPE:
            raise QualificationError("FUNDING_INCOME_TYPE_MISMATCH")
        if item["asset"] != "USDT":
            raise QualificationError("FUNDING_INCOME_ASSET_NOT_USDT")
        if not isinstance(item["info"], str) or not isinstance(item["tradeId"], str):
            raise QualificationError("FUNDING_INCOME_TEXT_FIELD_TYPE_MISMATCH")
        if type(item["time"]) is not int or not start_time_ms <= item["time"] <= end_time_ms:
            raise QualificationError("FUNDING_INCOME_TIME_OUTSIDE_QUERY")
        if type(item["tranId"]) is not int:
            raise QualificationError("FUNDING_INCOME_TRANSACTION_ID_TYPE_MISMATCH")

        income = _parse_finite_decimal_text(item["income"], "FUNDING_INCOME")
        sanitized.append(
            {
                "identity": (FUNDING_INCOME_TYPE, item["tranId"]),
                "symbol": symbol,
                "time_ms": item["time"],
                "income": item["income"],
                "asset": item["asset"],
                "decimal_places": max(0, -income.as_tuple().exponent),
            },
        )
    return sanitized


def _deduplicate_funding_records(
    records: list[dict[str, object]],
) -> tuple[list[dict[str, object]], int]:
    seen: set[tuple[str, int]] = set()
    unique: list[dict[str, object]] = []
    duplicate_count = 0
    for record in records:
        identity = record["identity"]
        if not isinstance(identity, tuple) or len(identity) != 2:
            raise QualificationError("FUNDING_INCOME_IDENTITY_INVALID")
        if identity in seen:
            duplicate_count += 1
            continue
        seen.add(identity)
        unique.append(record)
    return unique, duplicate_count


async def _query_funding_income_page(
    shared_http_client,
    clock,
    *,
    symbol: str,
    start_time_ms: int,
    end_time_ms: int,
    page: int,
    limit: int,
) -> list[dict[str, object]]:
    if FUNDING_INCOME_PATH not in READ_ONLY_GET_WHITELIST:
        raise QualificationError("READ_ONLY_GET_NOT_WHITELISTED")
    if symbol not in INSTRUMENT_SYMBOLS:
        raise QualificationError("FUNDING_INCOME_SYMBOL_NOT_ALLOWED")
    if start_time_ms < 0 or end_time_ms < start_time_ms:
        raise QualificationError("FUNDING_INCOME_TIME_RANGE_INVALID")
    if page < 1 or not 1 <= limit <= 1000:
        raise QualificationError("FUNDING_INCOME_PAGINATION_INVALID")

    raw = await shared_http_client.sign_request(
        http_method=HttpMethod.GET,
        url_path=FUNDING_INCOME_PATH,
        payload={
            "symbol": symbol,
            "incomeType": FUNDING_INCOME_TYPE,
            "startTime": str(start_time_ms),
            "endTime": str(end_time_ms),
            "page": str(page),
            "limit": str(limit),
            "timestamp": str(clock.timestamp_ms()),
            "recvWindow": "5000",
        },
        ratelimiter_keys=[f"binance:{FUNDING_INCOME_PATH}", "binance:global"],
    )
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        raise QualificationError("FUNDING_INCOME_RESPONSE_NOT_JSON") from None
    return _validate_funding_income_page(
        decoded,
        symbol=symbol,
        start_time_ms=start_time_ms,
        end_time_ms=end_time_ms,
    )


async def _query_funding_income_window(
    shared_http_client,
    clock,
    *,
    symbol: str,
    start_time_ms: int,
    end_time_ms: int,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    records: list[dict[str, object]] = []
    page_sizes: list[int] = []
    previous_page_identities: list[object] | None = None
    for page in range(1, FUNDING_MAX_PAGES + 1):
        current = await _query_funding_income_page(
            shared_http_client,
            clock,
            symbol=symbol,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            page=page,
            limit=FUNDING_PAGE_LIMIT,
        )
        current_identities = [record["identity"] for record in current]
        if current and current_identities == previous_page_identities:
            raise QualificationError("FUNDING_INCOME_PAGINATION_NOT_ADVANCING")
        page_sizes.append(len(current))
        records.extend(current)
        previous_page_identities = current_identities
        if len(current) < FUNDING_PAGE_LIMIT and (current or page > 1):
            break
    else:
        raise QualificationError("FUNDING_INCOME_PAGINATION_LIMIT_EXCEEDED")

    unique, duplicate_count = _deduplicate_funding_records(records)
    return unique, {
        "page_numbers_are_one_based": True,
        "limit": FUNDING_PAGE_LIMIT,
        "page_sizes": page_sizes,
        "raw_record_count": len(records),
        "unique_record_count": len(unique),
        "duplicate_identity_count": duplicate_count,
        "empty_second_page_probed": page_sizes == [0, 0],
        "decimal_text_parsed_without_float": True,
        "max_decimal_places": max(
            (int(record["decimal_places"]) for record in unique),
            default=None,
        ),
    }


def _funding_read_failure_disposition(error: BaseException) -> str:
    if isinstance(error, BinanceClientError) and error.status in {408, 418, 429}:
        return "UNKNOWN_TRANSIENT_READ"
    if isinstance(error, BinanceServerError):
        return "UNKNOWN_TRANSIENT_READ"
    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return "UNKNOWN_TRANSIENT_READ"
    return "REJECTED_NON_TRANSIENT_READ"


def _config_only_evidence() -> dict[str, object]:
    api_key = "K" * 64
    api_secret = "S" * 64
    node, provider, data_client, exec_client = _build_configuration(api_key, api_secret)
    configuration = _nonsecret_configuration()
    rendered = _canonical_json(configuration)
    errors: list[str] = []

    if provider.load_ids != frozenset(INSTRUMENT_IDS):
        errors.append("PROVIDER_LOAD_IDS_MISMATCH")
    if data_client.instrument_provider is not provider:
        errors.append("DATA_PROVIDER_OBJECT_NOT_SHARED")
    if exec_client.instrument_provider is not provider:
        errors.append("EXEC_PROVIDER_OBJECT_NOT_SHARED")
    if data_client.environment != BinanceEnvironment.DEMO:
        errors.append("DATA_ENVIRONMENT_NOT_DEMO")
    if exec_client.environment != BinanceEnvironment.DEMO:
        errors.append("EXEC_ENVIRONMENT_NOT_DEMO")
    if data_client.api_key is None or data_client.api_secret is None:
        errors.append("DATA_CREDENTIALS_NOT_EXPLICIT")
    if exec_client.api_key is None or exec_client.api_secret is None:
        errors.append("EXEC_CREDENTIALS_NOT_EXPLICIT")
    if exec_client.max_retries is not None:
        errors.append("EXEC_WRITE_RETRIES_NOT_DISABLED")
    if api_key in rendered or api_secret in rendered:
        errors.append("NONSECRET_CONFIG_CONTAINS_CREDENTIAL")
    if set(node.data_clients) != {BINANCE} or set(node.exec_clients) != {BINANCE}:
        errors.append("CLIENT_CONFIG_COUNT_MISMATCH")

    profile_specs = {
        "BINANCE_DEMO": (BinanceEnvironment.DEMO, DEMO_INSTRUMENT_IDS),
        "BINANCE_LIVE_READ_ONLY": (BinanceEnvironment.LIVE, LIVE_INSTRUMENT_IDS),
        "BINANCE_LIVE_WRITE": (BinanceEnvironment.LIVE, LIVE_INSTRUMENT_IDS),
    }
    providers: dict[str, BinanceInstrumentProviderConfig] = {}
    profile_matrix: dict[str, object] = {}
    for profile, (environment, instrument_ids) in profile_specs.items():
        profile_provider, profile_data, profile_exec = _build_profile_client_configs(
            profile,
            api_key,
            api_secret,
        )
        providers[profile] = profile_provider
        expected_ids = frozenset(instrument_ids)
        profile_matrix[profile] = {
            "environment": environment.value,
            "load_ids": sorted(str(value) for value in expected_ids),
            "load_all": profile_provider.load_all,
            "query_commission_rates": profile_provider.query_commission_rates,
            "provider_shared_within_profile": (
                profile_data.instrument_provider is profile_provider
                and profile_exec.instrument_provider is profile_provider
            ),
            "data_execution_environment_match": (
                profile_data.environment == environment
                and profile_exec.environment == environment
            ),
            "base_url_override": False,
            "exchange_change_mode": (
                "DEMO_CHECK_ENABLED"
                if profile == "BINANCE_DEMO"
                else "PUBLIC_DATA_ONLY"
                if profile == "BINANCE_LIVE_READ_ONLY"
                else "RUNTIME_GATE_REQUIRED"
            ),
        }
        if profile_provider.load_ids != expected_ids:
            errors.append(f"PROFILE_LOAD_IDS_MISMATCH:{profile}")
        if profile_data.instrument_provider is not profile_provider:
            errors.append(f"PROFILE_DATA_PROVIDER_NOT_SHARED:{profile}")
        if profile_exec.instrument_provider is not profile_provider:
            errors.append(f"PROFILE_EXEC_PROVIDER_NOT_SHARED:{profile}")
        if profile_data.environment != environment or profile_exec.environment != environment:
            errors.append(f"PROFILE_ENVIRONMENT_MISMATCH:{profile}")
    if len({id(provider) for provider in providers.values()}) != len(providers):
        errors.append("PROVIDER_SHARED_ACROSS_PROFILES")

    return {
        "config_digest_sha256": _configuration_digest(configuration),
        "configuration": configuration,
        "profile_isolation": {
            "profiles": profile_matrix,
            "providers_distinct_across_profiles": len(
                {id(provider) for provider in providers.values()},
            )
            == len(providers),
            "real_credentials_loaded": False,
            "network_connection_attempted": False,
        },
        "errors": errors,
        "status": "QUALIFIED" if not errors else "REJECTED",
    }


async def _observe_running_node(
    node: TradingNode,
    provider_config: BinanceInstrumentProviderConfig,
    api_key: str,
    api_secret: str,
    proxy_url: str | None,
    evidence: dict[str, object],
    errors: list[str],
    blockers: list[str],
    unknowns: list[str],
    market_strategy: MarketDataQualificationStrategy | None,
    run_funding_history: bool,
) -> None:
    snapshot: dict[str, object] = {}
    try:
        expected_ids = sorted(str(instrument_id) for instrument_id in INSTRUMENT_IDS)
        for _ in range(700):
            cache_ids = sorted(
                str(instrument_id) for instrument_id in node.cache.instrument_ids(BINANCE_VENUE)
            )
            snapshot = {
                "node_running": node.is_running(),
                "data_engine_connected": bool(node.kernel.data_engine.check_connected()),
                "execution_engine_connected": bool(node.kernel.exec_engine.check_connected()),
                "instrument_ids": cache_ids,
                "account_count": len(node.cache.accounts()),
            }
            ready = bool(
                snapshot["node_running"]
                and snapshot["data_engine_connected"]
                and snapshot["execution_engine_connected"]
                and set(expected_ids).issubset(cache_ids)
                and snapshot["account_count"] == 1
            )
            if ready:
                break
            await asyncio.sleep(0.05)
        evidence["initialization_snapshot"] = snapshot
        if not ready:
            raise TimeoutError("NODE_CLIENT_INITIALIZATION_TIMEOUT")

        evidence["node_running"] = True
        evidence["data_engine_connected"] = bool(node.kernel.data_engine.check_connected())
        evidence["execution_engine_connected"] = bool(node.kernel.exec_engine.check_connected())
        evidence["registered_data_clients"] = [
            str(client_id) for client_id in node.kernel.data_engine.registered_clients
        ]
        evidence["registered_execution_clients"] = [
            str(client_id) for client_id in node.kernel.exec_engine.registered_clients
        ]

        shared_http_client = get_cached_binance_http_client(
            clock=node.kernel.clock,
            account_type=BinanceAccountType.USDT_FUTURES,
            api_key=api_key,
            api_secret=api_secret,
            key_type=BinanceKeyType.HMAC,
            base_url=None,
            environment=BinanceEnvironment.DEMO,
            is_us=False,
            proxy_url=proxy_url,
        )
        demo_base_url_matches = shared_http_client.base_url == BINANCE_USDM_DEMO_BASE_URL
        if not demo_base_url_matches:
            errors.append("HTTP_CLIENT_NOT_BOUND_TO_USDM_DEMO_BASE_URL")
        provider = get_cached_binance_futures_instrument_provider(
            client=shared_http_client,
            clock=node.kernel.clock,
            account_type=BinanceAccountType.USDT_FUTURES,
            config=provider_config,
            venue=BINANCE_VENUE,
        )
        provider_cache_info = get_cached_binance_futures_instrument_provider.cache_info()
        http_cache_info = get_cached_binance_http_client.cache_info()
        evidence["factory_singletons"] = {
            "http_cache_currsize": http_cache_info.currsize,
            "http_cache_hits": http_cache_info.hits,
            "provider_cache_currsize": provider_cache_info.currsize,
            "provider_cache_hits": provider_cache_info.hits,
        }

        provider_ids = sorted(str(instrument_id) for instrument_id in provider.get_all())
        node_cache_ids = sorted(
            str(instrument_id) for instrument_id in node.cache.instrument_ids(BINANCE_VENUE)
        )
        evidence["instrument_ids"] = {
            "required_baseline": expected_ids,
            "provider": provider_ids,
            "node_cache": node_cache_ids,
        }
        if not set(expected_ids).issubset(provider_ids):
            errors.append("PROVIDER_INSTRUMENT_IDS_MISMATCH")
        if not set(expected_ids).issubset(node_cache_ids):
            errors.append("NODE_CACHE_INSTRUMENT_IDS_MISMATCH")
        if http_cache_info.currsize != 1 or http_cache_info.hits < 2:
            errors.append("HTTP_CLIENT_NOT_SINGLETON")
        if provider_cache_info.currsize != 1 or provider_cache_info.hits < 2:
            errors.append("INSTRUMENT_PROVIDER_NOT_SINGLETON")

        instrument_shapes: dict[str, dict[str, object]] = {}
        for instrument_id in INSTRUMENT_IDS:
            instrument = node.cache.instrument(instrument_id)
            if instrument is None:
                errors.append(f"INSTRUMENT_MISSING:{instrument_id}")
                continue
            instrument_shapes[str(instrument_id)] = {
                "price_precision": instrument.price_precision,
                "size_precision": instrument.size_precision,
                "price_increment": str(instrument.price_increment),
                "size_increment": str(instrument.size_increment),
            }
        evidence["instrument_shapes"] = instrument_shapes

        wallet_api = BinanceFuturesWalletHttpAPI(
            shared_http_client,
            node.kernel.clock,
            BinanceAccountType.USDT_FUTURES,
        )
        commission_rates: dict[str, dict[str, object]] = {}
        for symbol, instrument_id in zip(INSTRUMENT_SYMBOLS, INSTRUMENT_IDS, strict=True):
            direct_rate = await wallet_api.query_futures_commission_rate(
                symbol=symbol,
                recv_window="5000",
            )
            if direct_rate.symbol != symbol:
                errors.append(f"COMMISSION_RATE_SYMBOL_MISMATCH:{symbol}")
                continue
            maker_rate = _parse_finite_decimal_text(
                direct_rate.makerCommissionRate,
                "MAKER_COMMISSION_RATE",
            )
            taker_rate = _parse_finite_decimal_text(
                direct_rate.takerCommissionRate,
                "TAKER_COMMISSION_RATE",
            )
            if maker_rate < 0 or taker_rate < 0:
                errors.append(f"COMMISSION_RATE_NEGATIVE:{symbol}")
            instrument = node.cache.instrument(instrument_id)
            provider_matches_direct = bool(
                instrument is not None
                and instrument.maker_fee == maker_rate
                and instrument.taker_fee == taker_rate
            )
            if not provider_matches_direct:
                errors.append(f"PROVIDER_COMMISSION_RATE_MISMATCH:{symbol}")
            commission_rates[symbol] = {
                "maker_rate": str(maker_rate),
                "taker_rate": str(taker_rate),
                "provider_matches_direct_get": provider_matches_direct,
            }
        evidence["commission_rate_read"] = {
            "client": "SHARED_CACHED_NAUTILUS_BINANCE_HTTP_CLIENT",
            "fixed_package_wrapper": "BinanceFuturesWalletHttpAPI",
            "method": "GET",
            "path": "/fapi/v1/commissionRate",
            "provider_fallback_is_not_used_as_actual_rate_evidence": True,
            "write_methods_exposed": False,
            "rates": commission_rates,
        }

        account_api = BinanceFuturesAccountHttpAPI(
            shared_http_client,
            node.kernel.clock,
            BinanceAccountType.USDT_FUTURES,
        )
        account_info = await account_api.query_futures_account_info(recv_window="5000")
        hedge_mode = await account_api.query_futures_hedge_mode(recv_window="5000")
        symbol_configs = await account_api.query_futures_symbol_config(recv_window="5000")
        configs_by_symbol = {config.symbol: config for config in symbol_configs}

        account_flags = {
            "trading_enabled": account_info.canTrade,
            "transfer_in_enabled": account_info.canDeposit,
            "transfer_out_enabled": account_info.canWithdraw,
        }
        evidence["account_flags"] = account_flags
        evidence["api_permissions"] = {
            "authenticated_user_data": True,
            "trading_enabled": account_info.canTrade,
            "account_can_withdraw_field_meaning": "FUTURES_TRANSFER_OUT_ENABLED",
            "api_key_withdrawal_permission_readback": "NOT_EXPOSED_BY_USDM_API",
            "withdrawal_capability_in_selected_profile": (
                "OUTSIDE_BINANCE_USDM_DEMO_PROFILE"
            ),
            "demo_http_base_url_matches_fixed_adapter": demo_base_url_matches,
            "direct_get_whitelist_exposes_withdrawal_route": False,
            "live_write_gate": "MANUAL_BINANCE_UI_PERMISSION_EVIDENCE_REQUIRED",
            "documentation": BINANCE_USDM_ACCOUNT_DOCUMENTATION,
        }
        if not account_info.canTrade:
            blockers.append("ACCOUNT_TRADING_NOT_ENABLED")

        evidence["position_mode"] = "ONE_WAY" if not hedge_mode.dualSidePosition else "HEDGE"
        if hedge_mode.dualSidePosition:
            blockers.append("ACCOUNT_NOT_ONE_WAY")

        symbol_facts: dict[str, dict[str, object]] = {}
        for symbol in INSTRUMENT_SYMBOLS:
            symbol_config = configs_by_symbol.get(symbol)
            if symbol_config is None:
                errors.append(f"SYMBOL_CONFIG_MISSING:{symbol}")
                continue
            symbol_facts[symbol] = {
                "margin_type": symbol_config.marginType,
                "actual_leverage": symbol_config.leverage,
                "effective_leverage": _effective_leverage(symbol_config.leverage),
                "auto_add_margin": symbol_config.isAutoAddMargin,
            }
            if symbol_config.marginType.upper() not in {"ISOLATED", "CROSSED"}:
                blockers.append(f"SYMBOL_MARGIN_MODE_UNSUPPORTED:{symbol}")
            if symbol_config.leverage <= 0:
                blockers.append(f"SYMBOL_LEVERAGE_NOT_POSITIVE:{symbol}")
        evidence["symbol_configuration"] = symbol_facts
        evidence["margin_leverage_policy"] = {
            "accepted_actual_margin_modes": ["CROSSED", "ISOLATED"],
            "actual_margin_mode_is_observed_not_modified": True,
            "actual_leverage_is_observed_not_modified": True,
            "effective_leverage_formula": "min(actual_leverage, 5)",
            "crossed_or_actual_leverage_above_5_is_not_a_blocker": True,
            "golden_vectors": [
                _margin_leverage_compatibility_vector("CROSSED", 20, Decimal("100")),
                _margin_leverage_compatibility_vector("ISOLATED", 3, Decimal("100")),
            ],
        }

        write_preflight: dict[str, dict[str, object]] = {}
        for symbol in INSTRUMENT_SYMBOLS:
            position_risks = await account_api.query_futures_position_risk(
                symbol=symbol,
                recv_window="5000",
            )
            ordinary_orders = await account_api.query_open_orders(
                symbol=symbol,
                recv_window="5000",
            )
            algo_orders = await account_api.query_open_algo_orders(
                symbol=symbol,
                recv_window="5000",
            )
            nonzero_positions = [
                position
                for position in position_risks
                if _parse_finite_decimal_text(position.positionAmt, "POSITION_AMOUNT") != 0
            ]
            write_preflight[symbol] = {
                "nonzero_position_count": len(nonzero_positions),
                "open_ordinary_order_count": len(ordinary_orders),
                "open_algo_order_count": len(algo_orders),
                "external_or_unknown_responsibility_absent": not (
                    nonzero_positions or ordinary_orders or algo_orders
                ),
            }
            if nonzero_positions:
                blockers.append(f"DEMO_WRITE_PREFLIGHT_NONZERO_POSITION:{symbol}")
            if ordinary_orders:
                blockers.append(f"DEMO_WRITE_PREFLIGHT_OPEN_ORDINARY_ORDER:{symbol}")
            if algo_orders:
                blockers.append(f"DEMO_WRITE_PREFLIGHT_OPEN_ALGO_ORDER:{symbol}")
        evidence["demo_write_preflight"] = {
            "read_only": True,
            "queried_position_risk": True,
            "queried_open_ordinary_orders": True,
            "queried_open_algo_orders": True,
            "identities_or_amounts_persisted": False,
            "symbols": write_preflight,
        }

        accounts = node.cache.accounts()
        evidence["account_cache"] = {
            "account_count": len(accounts),
            "account_types": sorted({str(account.type) for account in accounts}),
        }
        if len(accounts) != 1:
            errors.append("ACCOUNT_CACHE_COUNT_MISMATCH")
        else:
            account = accounts[0]
            cache_leverages: dict[str, str | None] = {}
            for instrument_id in INSTRUMENT_IDS:
                leverage = account.leverage(instrument_id)
                cache_leverages[str(instrument_id)] = None if leverage is None else str(leverage)
                if leverage is None:
                    errors.append(f"ACCOUNT_CACHE_LEVERAGE_MISSING:{instrument_id}")
            evidence["account_cache"]["leverages"] = cache_leverages

        single_asset_mode = await query_single_asset_mode(
            shared_http_client,
            node.kernel.clock,
        )
        evidence["read_only_get_supplement"] = {
            "client": "SHARED_CACHED_NAUTILUS_BINANCE_HTTP_CLIENT",
            "method": "GET",
            "path": MULTI_ASSET_MARGIN_PATH,
            "reason": "NAUTILUS_ACCOUNT_SCHEMA_OMITS_MULTI_ASSETS_MARGIN",
            "write_methods_exposed": False,
        }
        evidence["single_asset_mode"] = "SINGLE_ASSET" if single_asset_mode else "MULTI_ASSET"
        if not single_asset_mode:
            blockers.append("ACCOUNT_NOT_SINGLE_ASSET")

        if run_funding_history:
            try:
                initial_cutoff_ms = node.kernel.clock.timestamp_ms()
                retention_start_ms = _three_calendar_months_ago_ms(initial_cutoff_ms)
                initial_records: list[dict[str, object]] = []
                initial_windows: dict[str, object] = {}
                for symbol in INSTRUMENT_SYMBOLS:
                    records, window_evidence = await _query_funding_income_window(
                        shared_http_client,
                        node.kernel.clock,
                        symbol=symbol,
                        start_time_ms=retention_start_ms,
                        end_time_ms=initial_cutoff_ms,
                    )
                    initial_records.extend(records)
                    initial_windows[symbol] = window_evidence

                initial_unique, initial_duplicate_count = _deduplicate_funding_records(
                    initial_records,
                )
                initial_identities = {record["identity"] for record in initial_unique}
                retention_violations = sum(
                    int(record["time_ms"] < retention_start_ms)
                    for record in initial_unique
                )
                if retention_violations:
                    errors.append("FUNDING_INCOME_RETENTION_BOUNDARY_VIOLATED")

                restart_cutoff_ms = max(
                    initial_cutoff_ms,
                    node.kernel.clock.timestamp_ms(),
                )
                restart_records: list[dict[str, object]] = []
                restart_windows: dict[str, object] = {}
                for symbol in INSTRUMENT_SYMBOLS:
                    records, window_evidence = await _query_funding_income_window(
                        shared_http_client,
                        node.kernel.clock,
                        symbol=symbol,
                        start_time_ms=initial_cutoff_ms,
                        end_time_ms=restart_cutoff_ms,
                    )
                    restart_records.extend(records)
                    restart_windows[symbol] = window_evidence
                restart_unique, restart_duplicate_count = _deduplicate_funding_records(
                    restart_records,
                )
                overlap_count = sum(
                    int(record["identity"] in initial_identities)
                    for record in restart_unique
                )
                new_unique_count = len(restart_unique) - overlap_count
                nonempty_venue_sample = bool(initial_unique or restart_unique)
                evidence["funding_income_read"] = {
                    "status": (
                        "QUALIFIED_NONEMPTY_READ_PATH"
                        if nonempty_venue_sample
                        else "QUALIFIED_EMPTY_ACCOUNT_WINDOW_WITH_CONTRACT_VECTOR"
                    ),
                    "client": "SHARED_CACHED_NAUTILUS_BINANCE_HTTP_CLIENT",
                    "method": "GET",
                    "path": FUNDING_INCOME_PATH,
                    "fixed_parameters": {
                        "incomeType": FUNDING_INCOME_TYPE,
                        "symbols": list(INSTRUMENT_SYMBOLS),
                        "page": "ONE_BASED",
                        "limit": FUNDING_PAGE_LIMIT,
                    },
                    "documented_retention": "LAST_THREE_CALENDAR_MONTHS",
                    "retention_start_ms": retention_start_ms,
                    "initial_cutoff_ms": initial_cutoff_ms,
                    "initial": {
                        "windows": initial_windows,
                        "combined_unique_record_count": len(initial_unique),
                        "combined_duplicate_identity_count": initial_duplicate_count,
                        "retention_boundary_violations": retention_violations,
                    },
                    "cursor_restart": {
                        "poll_interval_ms": FUNDING_POLL_INTERVAL_MS,
                        "start_from_inclusive_last_funding_query_cutoff": True,
                        "restart_cutoff_ms": restart_cutoff_ms,
                        "windows": restart_windows,
                        "combined_unique_record_count": len(restart_unique),
                        "combined_duplicate_identity_count": restart_duplicate_count,
                        "overlap_deduplicated_count": overlap_count,
                        "new_unique_record_count": new_unique_count,
                    },
                    "identity": "(incomeType,tranId)",
                    "runtime_nonempty_sample_present": nonempty_venue_sample,
                    "empty_runtime_window_is_not_a_negative_venue_fact": True,
                    "field_contract_qualification": {
                        "source": "BINANCE_OFFICIAL_USDM_ACCOUNT_API_SCHEMA",
                        "documentation": BINANCE_USDM_ACCOUNT_DOCUMENTATION,
                        "required_fields": sorted(FUNDING_INCOME_FIELDS),
                        "nonempty_decimal_golden_vector": True,
                        "malformed_payload_rejected": True,
                        "synthetic_record_emitted_as_venue_fact": False,
                    },
                    "raw_transaction_ids_or_income_amounts_persisted": False,
                    "write_methods_exposed": False,
                    "failure_policy": {
                        "http_429": "UNKNOWN_NO_NEGATIVE_FACT",
                        "http_5xx_or_network_timeout": "UNKNOWN_NO_NEGATIVE_FACT",
                        "automatic_retry_in_probe": False,
                    },
                }
            except Exception as exc:
                disposition = _funding_read_failure_disposition(exc)
                evidence["funding_income_read"] = {
                    "status": disposition,
                    "error_type": type(exc).__name__,
                    "http_status": getattr(exc, "status", None),
                    "raw_error_message_persisted": False,
                    "automatic_retry_in_probe": False,
                }
                if disposition == "UNKNOWN_TRANSIENT_READ":
                    unknowns.append("FUNDING_INCOME_TRANSIENT_READ_UNKNOWN")
                elif isinstance(exc, QualificationError):
                    errors.append(f"FUNDING_INCOME_QUALIFICATION_FAILED:{exc}")
                else:
                    errors.append(f"FUNDING_INCOME_QUALIFICATION_FAILED:{type(exc).__name__}")

        if market_strategy is not None:
            for _ in range(2400):
                if market_strategy.smoke_ready or market_strategy.errors:
                    break
                await asyncio.sleep(0.05)
            market_evidence, market_errors = market_strategy.qualification_evidence()
            data_engine = node.kernel.data_engine
            data_client = data_engine.routing_map[BINANCE_VENUE]
            engine_bars = sorted(str(bar_type) for bar_type in data_engine.subscribed_bars())
            client_bars = sorted(str(bar_type) for bar_type in data_client.subscribed_bars())
            engine_marks = sorted(str(value) for value in data_engine.subscribed_mark_prices())
            client_marks = sorted(str(value) for value in data_client.subscribed_mark_prices())
            engine_quotes = sorted(str(value) for value in data_engine.subscribed_quote_ticks())
            client_quotes = sorted(str(value) for value in data_client.subscribed_quote_ticks())
            expected_source_bars = sorted(
                str(market_strategy.source_types[instrument_id])
                for instrument_id in market_strategy.instrument_ids
            )
            expected_engine_bars = sorted(
                [
                    *expected_source_bars,
                    *(
                        str(market_strategy.target_types[instrument_id].standard())
                        for instrument_id in market_strategy.instrument_ids
                    ),
                ],
            )
            expected_instruments = sorted(str(value) for value in market_strategy.instrument_ids)
            market_evidence["subscriptions"] = {
                "data_engine_bars": engine_bars,
                "data_client_bars": client_bars,
                "data_engine_mark_prices": engine_marks,
                "data_client_mark_prices": client_marks,
                "data_engine_quote_ticks": engine_quotes,
                "data_client_quote_ticks": client_quotes,
                "one_underlying_source_bar_per_instrument": client_bars == expected_source_bars,
                "mark_price_individual_unsubscribe": "UNSUPPORTED_BY_BINANCE_DATA_CLIENT",
                "cleanup": "TRADING_NODE_STOP_DISCONNECTS_CLIENT",
            }
            if not market_strategy.smoke_ready:
                market_errors.append("MARKET_DATA_SMOKE_TIMEOUT")
            if engine_bars != expected_engine_bars:
                market_errors.append("DATA_ENGINE_BAR_SUBSCRIPTIONS_MISMATCH")
            if client_bars != expected_source_bars:
                market_errors.append("DATA_CLIENT_SOURCE_SUBSCRIPTIONS_MISMATCH")
            if engine_marks != expected_instruments or client_marks != expected_instruments:
                market_errors.append("MARK_PRICE_SUBSCRIPTIONS_MISMATCH")
            if engine_quotes != expected_instruments or client_quotes != expected_instruments:
                market_errors.append("QUOTE_TICK_SUBSCRIPTIONS_MISMATCH")
            evidence["market_data"] = market_evidence
            errors.extend(market_errors)
    finally:
        await node.stop_async()
        evidence["node_stopped"] = not node.is_running()


def _scan_paths_for_credentials(
    paths: list[Path],
    api_key: str,
    api_secret: str,
) -> tuple[int, bool]:
    scanned = 0
    found = False
    encoded_values = (api_key.encode("utf-8"), api_secret.encode("utf-8"))
    for path in paths:
        if not path.is_file():
            continue
        scanned += 1
        try:
            content = path.read_bytes()
        except OSError:
            found = True
            continue
        if any(value in content for value in encoded_values):
            found = True
    return scanned, found


def _write_evidence(path: Path | None, evidence: dict[str, object]) -> None:
    rendered = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True)
    if path is None:
        print(rendered)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered + "\n", encoding="utf-8")


def _validate_proxy_url(proxy_url: str | None) -> str | None:
    if proxy_url is None:
        return None
    parsed = urlsplit(proxy_url)
    if parsed.scheme not in {"http", "https"}:
        raise QualificationError("PROXY_SCHEME_NOT_ALLOWED")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise QualificationError("PROXY_NOT_LOOPBACK")
    if parsed.username is not None or parsed.password is not None:
        raise QualificationError("PROXY_CREDENTIALS_NOT_ALLOWED")
    if parsed.port is None:
        raise QualificationError("PROXY_PORT_REQUIRED")
    return proxy_url


def _online_probe(
    evidence_path: Path | None,
    captured_log: Path | None,
    proxy_url: str | None,
    run_market_data: bool,
    run_funding_history: bool,
) -> int:
    configuration = _nonsecret_configuration(proxy_enabled=proxy_url is not None)
    evidence: dict[str, object] = {
        "operation": "INITIALIZING",
        "profile": "BINANCE_DEMO",
        "config_digest_sha256": _configuration_digest(configuration),
        "configuration": configuration,
        "credential_reference": {
            "service": SERVICE,
            "accounts": [KEY_ACCOUNT, SECRET_ACCOUNT],
        },
        "node_built": False,
        "node_running": False,
        "node_stopped": False,
        "node_disposed": False,
    }
    errors: list[str] = []
    blockers: list[str] = []
    unknowns: list[str] = []
    mutex = None
    loop: asyncio.AbstractEventLoop | None = None
    node: TradingNode | None = None
    api_key: str | None = None
    api_secret: str | None = None
    market_strategy: MarketDataQualificationStrategy | None = None

    try:
        proxy_url = _validate_proxy_url(proxy_url)
        evidence["operation"] = "ACQUIRING_MUTEX"
        mutex = _acquire_executor_mutex()
        evidence["executor_mutex"] = "ACQUIRED_CURRENT_USER_PROTECTED_DACL"

        evidence["operation"] = "LOADING_WINVAULT"
        api_key, api_secret, backend_name = _load_credentials()
        evidence["credential_backend"] = backend_name

        with _without_binance_credential_environment() as environment_was_populated:
            evidence["credential_environment_sanitized"] = True
            evidence["credential_environment_had_values"] = environment_was_populated
            evidence["operation"] = "CONFIGURING_NODE"
            node_config, provider_config, data_config, exec_config = _build_configuration(
                api_key,
                api_secret,
                proxy_url,
            )
            if data_config.instrument_provider is not provider_config:
                raise QualificationError("DATA_PROVIDER_OBJECT_NOT_SHARED")
            if exec_config.instrument_provider is not provider_config:
                raise QualificationError("EXEC_PROVIDER_OBJECT_NOT_SHARED")
            if data_config.proxy_url != proxy_url or exec_config.proxy_url != proxy_url:
                raise QualificationError("CLIENT_PROXY_CONFIGURATION_MISMATCH")

            LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            evidence["operation"] = "BUILDING_NODE"
            node = TradingNode(config=node_config, loop=loop)
            node.add_data_client_factory(BINANCE, BinanceLiveDataClientFactory)
            node.add_exec_client_factory(BINANCE, BinanceLiveExecClientFactory)
            node.build()
            evidence["node_built"] = node.is_built()
            if run_market_data:
                market_strategy = MarketDataQualificationStrategy(
                    config=StrategyConfig(
                        strategy_id="DIRECTMARKET",
                        order_id_tag="001",
                        external_order_claims=None,
                        manage_contingent_orders=False,
                        manage_gtd_expiry=False,
                        manage_stop=False,
                    ),
                )
                node.trader.add_strategy(market_strategy)

            evidence["operation"] = "RUNNING_READ_ONLY_NODE"
            observer_task = loop.create_task(
                _observe_running_node(
                    node,
                    provider_config,
                    api_key,
                    api_secret,
                    proxy_url,
                    evidence,
                    errors,
                    blockers,
                    unknowns,
                    market_strategy,
                    run_funding_history,
                ),
            )
            try:
                node.run(raise_exception=True)
                if observer_task.done():
                    observer_task.result()
                else:
                    errors.append("OBSERVER_TASK_NOT_COMPLETED")
                    observer_task.cancel()
            finally:
                if not observer_task.done():
                    observer_task.cancel()

            evidence["node_stopped"] = not node.is_running()
            node.dispose()
            evidence["node_disposed"] = True
            evidence["operation"] = "COMPLETED"
    except Exception as exc:
        if isinstance(exc, QualificationError):
            errors.append(f"BINANCE_DEMO_PROBE_FAILED:{exc}")
        else:
            errors.append(f"BINANCE_DEMO_PROBE_FAILED:{type(exc).__name__}")
        if node is not None:
            try:
                node.stop()
                evidence["node_stopped"] = not node.is_running()
            except Exception as cleanup_exc:
                errors.append(f"NODE_STOP_FAILED:{type(cleanup_exc).__name__}")
            try:
                node.dispose()
                evidence["node_disposed"] = True
            except Exception as cleanup_exc:
                errors.append(f"NODE_DISPOSE_FAILED:{type(cleanup_exc).__name__}")
    finally:
        asyncio.set_event_loop(None)
        if loop is not None and not loop.is_closed():
            loop.close()
        get_cached_binance_futures_instrument_provider.cache_clear()
        get_cached_binance_http_client.cache_clear()
        if mutex is not None:
            try:
                _release_executor_mutex(mutex)
            except Exception as exc:
                errors.append(f"EXECUTOR_MUTEX_RELEASE_FAILED:{type(exc).__name__}")

    if api_key is not None and api_secret is not None:
        scan_paths = [path for path in LOG_DIRECTORY.rglob("*") if path.is_file()]
        if captured_log is not None:
            scan_paths.append(captured_log)
        scanned, secret_found = _scan_paths_for_credentials(scan_paths, api_key, api_secret)
        evidence["secret_scan"] = {
            "files_scanned": scanned,
            "raw_credential_found": secret_found,
        }
        if secret_found:
            errors.append("RAW_CREDENTIAL_FOUND_IN_LOG")

    if evidence["node_built"]:
        if not evidence["node_stopped"]:
            errors.append("NODE_NOT_STOPPED")
        if not evidence["node_disposed"]:
            errors.append("NODE_NOT_DISPOSED")

    evidence["errors"] = sorted(set(errors))
    evidence["blockers"] = sorted(set(blockers))
    evidence["unknowns"] = sorted(set(unknowns))
    if errors:
        evidence["status"] = "REJECTED"
    elif blockers:
        evidence["status"] = "BLOCKED_ACCOUNT_CONFIGURATION"
    elif unknowns:
        evidence["status"] = "PARTIAL_QUALIFIED"
    else:
        evidence["status"] = "QUALIFIED"

    rendered = _canonical_json(evidence)
    if api_key is not None and api_key in rendered:
        evidence = {"status": "REJECTED", "errors": ["EVIDENCE_SECRET_LEAK_GUARD"]}
    elif api_secret is not None and api_secret in rendered:
        evidence = {"status": "REJECTED", "errors": ["EVIDENCE_SECRET_LEAK_GUARD"]}
    _write_evidence(evidence_path, evidence)
    if evidence.get("status") in {"QUALIFIED", "PARTIAL_QUALIFIED"}:
        return 0
    if evidence.get("status") == "BLOCKED_ACCOUNT_CONFIGURATION":
        return 2
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Qualify one read-only Binance Demo data+execution node without exposing secrets.",
    )
    parser.add_argument("--config-only", action="store_true")
    parser.add_argument("--evidence-path", type=Path)
    parser.add_argument("--captured-log", type=Path)
    parser.add_argument("--proxy-url")
    parser.add_argument("--market-data", action="store_true")
    parser.add_argument("--funding-history", action="store_true")
    args = parser.parse_args()

    if args.config_only:
        evidence = _config_only_evidence()
        _write_evidence(args.evidence_path, evidence)
        return 0 if evidence["status"] == "QUALIFIED" else 1
    return _online_probe(
        args.evidence_path,
        args.captured_log,
        args.proxy_url,
        args.market_data,
        args.funding_history,
    )


if __name__ == "__main__":
    raise SystemExit(main())
