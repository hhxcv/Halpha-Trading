"""One product TradingNode composition shared by Demo and Live profiles."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
import re
from time import monotonic
from typing import Any

import psycopg
from nautilus_trader.adapters.binance import (
    BINANCE,
    BinanceAccountType,
    BinanceDataClientConfig,
    BinanceExecClientConfig,
    BinanceInstrumentProviderConfig,
    BinanceLiveDataClientFactory,
    BinanceLiveExecClientFactory,
    get_cached_binance_http_client,
)
from nautilus_trader.adapters.binance.common.enums import (
    BinanceEnvironment,
    BinanceKeyType,
)
from nautilus_trader.adapters.binance.futures.http.account import (
    BinanceFuturesAccountHttpAPI,
)
from nautilus_trader.common import Environment
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.config import LoggingConfig
from nautilus_trader.live.config import (
    ControllerConfig,
    LiveDataEngineConfig,
    LiveExecEngineConfig,
    RoutingConfig,
    TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, TraderId
from nautilus_trader.trading.config import ImportableControllerConfig
from nautilus_trader.trading.controller import Controller
from pydantic import SecretStr

from halpha.capital.models import AuthorityClass, EnvironmentKind
from halpha.configuration import ExecutorSettingsView
from halpha.database.schema_version import require_current_schema
from halpha.domain_values import canonical_decimal
from halpha.planning.bar_evaluation import (
    EntrySizingSnapshot,
    NautilusBarEntryEvaluator,
)
from halpha.planning.adapter import (
    ActivationAdapterLifecycle,
    ActivationAdapterSpec,
    HalphaStrategyAdapter,
)
from halpha.planning.models import PlanLifecycle, RunState
from halpha.planning.order_policies import EntryConditionKind
from halpha.planning.registry import DIRECT_EXECUTION_REF, OneShotParameters
from halpha.planning.repository import PostgreSQLPlanningRepository
from halpha.planning.service import PlanningApplicationService
from halpha.planning.strategies.one_shot import (
    ActivationStrategyState,
    InstrumentQuantityRules,
    OneShotDonchianAtrLogic,
    StrategyProposal,
)
from halpha.product_build import (
    EXECUTOR_STARTING_APPLICATION_NAME,
    executor_ready_application_name,
)
from halpha.public_market import binance_public_market_identity
from halpha.venue_integration.gateway import PersistedActionGate
from halpha.venue_integration.nautilus_client import NautilusVenueExecutionClient
from halpha.venue_integration.nautilus_account import (
    BinanceAccountContractError,
    query_hedge_mode,
)
from halpha.venue_integration.repository import (
    PostgreSQLExecutionActionRepository,
    PostgreSQLVenueFactRepository,
)
from halpha.venue_integration.models import BINANCE_USDM_VENUE_REF, VenueFactKind

from .coordinator import HalphaCoordinator
from .account_observation import (
    AccountObservationError,
    ProductAccountObserver,
)
from .direct_schedule import DirectScheduleBoundary
from .forward_observation import ForwardObservationSpec
from .product_entry import (
    LiveEntryFactTracker,
    ProductPreSubmitFactProvider,
    ProductPreSubmitRejected,
    ProductProposalBoundary,
    build_live_entry_sizing_snapshot,
    require_direct_activation_profile_consistency,
)
from .responsibilities import ProductResponsibilityBoundary


_PROFILE_SPEC = {
    "BINANCE_DEMO": (
        BinanceEnvironment.DEMO,
        ("BTCUSDT-PERP.BINANCE", "ETHUSDT-PERP.BINANCE"),
    ),
    "BINANCE_LIVE_READ_ONLY": (
        BinanceEnvironment.LIVE,
        ("BTCUSDT-PERP.BINANCE",),
    ),
    "BINANCE_LIVE_WRITE": (
        BinanceEnvironment.LIVE,
        ("BTCUSDT-PERP.BINANCE",),
    ),
}

_EXTERNAL_ORDER_EVENT_TOPIC = "events.order.EXTERNAL"
_EXECUTION_CACHE_PURGE_INTERVAL_MINS = 60
_EXECUTION_CACHE_RETENTION_MINS = 24 * 60
_RECONCILIATION_HISTORY_LOOKBACK_MINS = 60
_EXECUTION_STREAM_RECOVERY_TIMEOUT_SECONDS = 120.0
_EXECUTION_RECOVERY_BUFFER_EVENT_LIMIT = 10_000
_MARKET_DATA_STREAM_RECOVERY_TIMEOUT_SECONDS = 120.0
_MARK_PRICE_EVENT_MAX_AGE_SECONDS = 15.0
_RUNTIME_HEARTBEAT_INTERVAL_SECONDS = 1.0
_PRODUCT_DATABASE_SESSION_OPTIONS = (
    "-c statement_timeout=5000 "
    "-c lock_timeout=5000 "
    "-c idle_in_transaction_session_timeout=15000"
)


class ExecutorRuntimeError(RuntimeError):
    """Sanitized fail-closed product runtime failure."""


def _resolve_binance_execution_client(node: TradingNode) -> object:
    """Resolve the single framework client for health observation only."""

    kernel = getattr(node, "kernel", None)
    engine = getattr(kernel, "exec_engine", None)
    clients = getattr(engine, "_clients", None)
    if not isinstance(clients, dict) or len(clients) != 1:
        raise ExecutorRuntimeError("BINANCE_EXECUTION_CLIENT_TOPOLOGY_INVALID")
    return next(iter(clients.values()))


def _resolve_binance_data_client(node: TradingNode) -> object:
    """Resolve the single framework market-data client for health observation."""

    kernel = getattr(node, "kernel", None)
    engine = getattr(kernel, "data_engine", None)
    clients = getattr(engine, "_clients", None)
    if not isinstance(clients, dict) or len(clients) != 1:
        raise ExecutorRuntimeError("BINANCE_DATA_CLIENT_TOPOLOGY_INVALID")
    return next(iter(clients.values()))


def _require_zero_binance_mutation_retries(client: object) -> None:
    """Assert the configured framework pool cannot repeat uncertain writes."""

    pool = getattr(client, "_retry_manager_pool", None)
    max_retries = getattr(pool, "max_retries", None)
    if type(max_retries) is not int or max_retries != 0:
        raise ExecutorRuntimeError("BINANCE_MUTATION_RETRY_POLICY_UNSAFE")


def _boolean_state(owner: object, name: str) -> bool | None:
    """Read a framework boolean exposed as either a property or method."""

    value = getattr(owner, name, None)
    try:
        value = value() if callable(value) else value
    except Exception:
        return None
    return value if type(value) is bool else None


def _websocket_transport_state(stream: object) -> str:
    """Return the narrow transport state shared by Binance WS implementations."""

    for method_name in ("is_reconnecting", "is_disconnecting", "is_closed"):
        state = _boolean_state(stream, method_name)
        if state is None:
            return "STREAM_STATE_UNAVAILABLE"
        if state:
            return "RECOVERING"
    active = _boolean_state(stream, "is_active")
    if active is None:
        return "STREAM_STATE_UNAVAILABLE"
    return "HEALTHY" if active else "RECOVERING"


def _binance_execution_stream_state(client: object) -> str:
    """Read framework recovery state without taking over reconnect ownership."""

    connected = _boolean_state(client, "is_connected")
    if connected is None:
        return "CLIENT_STATE_UNAVAILABLE"
    if not connected:
        return "CLIENT_DISCONNECTED"
    user_stream = getattr(client, "_ws_client", None)
    if user_stream is None:
        return "USER_STREAM_UNAVAILABLE"
    if bool(getattr(user_stream, "_is_recovery_failed", False)):
        return "RECOVERY_FAILED"
    if bool(getattr(user_stream, "_dispatch_paused", False)):
        # The replacement stream is connected, but the framework is still
        # reconciling the disconnect gap and buffering fresh account events.
        buffered = getattr(user_stream, "_dispatch_buffer", ())
        try:
            buffered_count = len(buffered)
        except TypeError:
            return "RECOVERY_BUFFER_STATE_UNAVAILABLE"
        if buffered_count > _EXECUTION_RECOVERY_BUFFER_EVENT_LIMIT:
            return "RECOVERY_BUFFER_LIMIT_EXCEEDED"
        return "RECOVERING"
    if (
        not bool(getattr(user_stream, "is_authenticated", False))
        or getattr(user_stream, "subscription_id", None) is None
    ):
        return "RECOVERING"
    stream = getattr(user_stream, "_stream_client", None)
    if stream is None:
        return "RECOVERING"
    return _websocket_transport_state(stream)


def _binance_data_stream_state(
    client: object,
    *,
    required_streams: tuple[str, ...] = (),
) -> str:
    """Read subscribed public stream health while Nautilus owns reconnection."""

    connected = _boolean_state(client, "is_connected")
    if connected is None:
        return "CLIENT_STATE_UNAVAILABLE"
    if not connected:
        return "CLIENT_DISCONNECTED"
    wrappers = (
        getattr(client, "_ws_client", None),
        getattr(client, "_ws_public_client", None),
    )
    if any(wrapper is None for wrapper in wrappers):
        return "DATA_STREAM_UNAVAILABLE"
    observed_subscriptions: set[str] = set()
    for wrapper in wrappers:
        try:
            subscriptions = getattr(wrapper, "subscriptions", None)
            subscriptions = (
                subscriptions() if callable(subscriptions) else subscriptions
            )
            subscribed_streams = tuple(subscriptions)
        except Exception:
            return "STREAM_TOPOLOGY_UNAVAILABLE"
        if not all(isinstance(stream, str) for stream in subscribed_streams):
            return "STREAM_TOPOLOGY_UNAVAILABLE"
        observed_subscriptions.update(subscribed_streams)
        if not subscribed_streams:
            continue
        clients = getattr(wrapper, "_clients", None)
        client_streams = getattr(wrapper, "_client_streams", None)
        if not isinstance(clients, dict) or not isinstance(client_streams, dict):
            return "STREAM_TOPOLOGY_UNAVAILABLE"
        for subscribed_stream in subscribed_streams:
            owners = tuple(
                client_id
                for client_id, streams in client_streams.items()
                if isinstance(streams, list) and subscribed_stream in streams
            )
            if len(owners) != 1:
                return "STREAM_TOPOLOGY_UNAVAILABLE"
            stream = clients.get(owners[0])
            if stream is None:
                return "RECOVERING"
            state = _websocket_transport_state(stream)
            if state != "HEALTHY":
                return state
    if not set(required_streams).issubset(observed_subscriptions):
        return "STREAM_SUBSCRIPTIONS_MISSING"
    return "HEALTHY"


def _binance_bar_stream_name(bar_type: object) -> str:
    """Map the currently qualified external Binance bar topology to its stream."""

    prefix, separator, suffix = str(bar_type).partition(".BINANCE-")
    parts = suffix.split("-")
    if (
        not separator
        or not prefix.endswith("-PERP")
        or len(parts) < 3
        or not parts[0].isdigit()
    ):
        raise ExecutorRuntimeError("BINANCE_BAR_STREAM_IDENTITY_INVALID")
    unit = {
        "SECOND": "s",
        "MINUTE": "m",
        "HOUR": "h",
        "DAY": "d",
        "WEEK": "w",
        "MONTH": "M",
    }.get(parts[1])
    if unit is None:
        raise ExecutorRuntimeError("BINANCE_BAR_STREAM_IDENTITY_INVALID")
    symbol = prefix.removesuffix("-PERP").lower()
    return f"{symbol}@kline_{parts[0]}{unit}"


def _binance_mark_price_stream_name(instrument_ref: str) -> str:
    symbol, separator, suffix = instrument_ref.partition("-PERP")
    if not separator or suffix or not symbol:
        raise ExecutorRuntimeError("BINANCE_INSTRUMENT_ID_INVALID")
    return f"{symbol.lower()}@markPrice@1s"


def _required_mark_price_event_state(
    required_streams: tuple[str, ...],
    observed_at: dict[str, float],
    *,
    now: float,
) -> str:
    """Detect a connected transport which stopped delivering mark events."""

    for stream in required_streams:
        if not stream.endswith("@markPrice@1s"):
            continue
        last_event_at = observed_at.get(stream)
        if last_event_at is None:
            return "MARK_PRICE_EVENT_MISSING"
        age = now - last_event_at
        if age < 0 or age > _MARK_PRICE_EVENT_MAX_AGE_SECONDS:
            return "MARK_PRICE_EVENT_STALE"
    return "HEALTHY"


def product_profile_symbols(profile: str) -> tuple[str, ...]:
    """Return Binance symbols owned by one shared product-runtime profile."""

    try:
        instrument_ids = _PROFILE_SPEC[profile][1]
    except KeyError:
        raise ExecutorRuntimeError("EXECUTION_PROFILE_MISMATCH") from None
    symbols: list[str] = []
    for instrument_id in instrument_ids:
        symbol, separator, venue = instrument_id.partition("-PERP.")
        if not separator or venue != "BINANCE" or not symbol:
            raise ExecutorRuntimeError("BINANCE_INSTRUMENT_ID_INVALID")
        symbols.append(symbol)
    return tuple(symbols)


async def query_execution_hedge_mode(
    profile: str,
    *,
    api_key: SecretStr,
    api_secret: SecretStr,
    proxy_url: str | None = None,
    account_api: Any | None = None,
    clock: Any | None = None,
) -> bool:
    """Read the account mode which the Nautilus execution client must match."""

    try:
        venue_environment, _instrument_ids = _PROFILE_SPEC[profile]
    except KeyError:
        raise ExecutorRuntimeError("EXECUTION_PROFILE_MISMATCH") from None
    if profile == "BINANCE_LIVE_READ_ONLY":
        raise ExecutorRuntimeError("READ_ONLY_EXECUTION_CLIENT_FORBIDDEN")
    runtime_clock = clock or LiveClock()
    if account_api is None:
        client = get_cached_binance_http_client(
            clock=runtime_clock,
            account_type=BinanceAccountType.USDT_FUTURES,
            api_key=api_key.get_secret_value(),
            api_secret=api_secret.get_secret_value(),
            key_type=BinanceKeyType.HMAC,
            base_url=None,
            environment=venue_environment,
            is_us=False,
            proxy_url=proxy_url,
        )
        account_api = BinanceFuturesAccountHttpAPI(
            client,
            runtime_clock,
            BinanceAccountType.USDT_FUTURES,
        )
    try:
        return await asyncio.wait_for(
            query_hedge_mode(account_api, recv_window="5000"),
            timeout=10,
        )
    except BinanceAccountContractError as exc:
        raise ExecutorRuntimeError(str(exc)) from None
    except Exception as exc:
        raise ExecutorRuntimeError(
            f"BINANCE_POSITION_MODE_QUERY_FAILED_{type(exc).__name__.upper()}"
        ) from None


def _stable_failure_reason_code(exception: BaseException) -> dict[str, str]:
    """Expose only the typed rejection code, never arbitrary exception text."""

    if isinstance(exception, ProductPreSubmitRejected):
        return {"reason_code": exception.reason_code}
    if isinstance(exception, (ExecutorRuntimeError, RuntimeError, ValueError)):
        reason_code = str(exception)
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,95}", reason_code):
            return {"reason_code": reason_code}
    return {}


def _venue_event_reason_code(event: object) -> str | None:
    """Classify exchange event reasons without logging arbitrary response text."""

    reason = str(getattr(event, "reason", "")).strip()
    if not reason:
        return None
    binance_code = re.search(r"(?<!\d)-(\d{3,5})(?!\d)", reason)
    if binance_code is not None:
        return f"BINANCE_ERROR_{binance_code.group(1)}"
    normalized = reason.casefold()
    if re.search(r"\b(?:408|5\d\d)\b", normalized):
        return "VENUE_HTTP_TRANSIENT"
    if any(
        marker in normalized
        for marker in (
            "timeout",
            "timed out",
            "connection reset",
            "connection aborted",
            "connection closed",
            "unexpected eof",
        )
    ):
        return "VENUE_TRANSPORT_UNKNOWN"
    if normalized in {"unknown", "order_not_found_at_venue"}:
        return "VENUE_RESULT_UNKNOWN"
    return "VENUE_REJECTION_OTHER"


def _activation_entry_deadline(activation: object) -> datetime:
    rule_state = getattr(activation, "rule_state", {})
    deadlines = rule_state.get("deadlines", {}) if isinstance(rule_state, dict) else {}
    value = deadlines.get("entry_valid_until") if isinstance(deadlines, dict) else None
    if not isinstance(value, str):
        raise ExecutorRuntimeError("ENTRY_DEADLINE_MISSING")
    try:
        deadline = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ExecutorRuntimeError("ENTRY_DEADLINE_INVALID") from None
    if deadline.tzinfo is None:
        raise ExecutorRuntimeError("ENTRY_DEADLINE_INVALID")
    return deadline.astimezone(UTC)


class HalphaRuntimeController(Controller):
    """The unique product Controller used for activation adapter lifecycle."""

    def __init__(self, trader: Any, config: ControllerConfig | None = None) -> None:
        super().__init__(trader=trader, config=config)


def _connect_product_database(
    connector: Callable[..., Any],
    *,
    database_name: str,
    password: str,
    application_name: str | None = None,
) -> Any:
    """Keep reads outside explicit units from opening a hidden outer transaction."""

    values: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 5432,
        "dbname": database_name,
        "user": f"{database_name}_executor",
        "password": password,
        "connect_timeout": 2,
        "autocommit": True,
        "options": _PRODUCT_DATABASE_SESSION_OPTIONS,
    }
    if application_name is not None:
        values["application_name"] = application_name
    return connector(
        **values,
    )


def _cached_leaves_quantity(cache: Any, client_order_id: str) -> str | None:
    """Read the framework-owned order projection without inventing a terminal state."""

    try:
        order = cache.order(ClientOrderId(client_order_id))
    except (TypeError, ValueError):
        return None
    if order is None:
        return None
    leaves_quantity = getattr(order, "leaves_qty", None)
    return str(leaves_quantity) if leaves_quantity is not None else None


def _cached_filled_quantity(cache: Any, client_order_id: str) -> str | None:
    """Read the framework-owned cumulative fill without deriving it from intent."""

    try:
        order = cache.order(ClientOrderId(client_order_id))
    except (TypeError, ValueError):
        return None
    if order is None:
        return None
    filled_quantity = getattr(order, "filled_qty", None)
    return str(filled_quantity) if filled_quantity is not None else None


def _cached_order_quantity(cache: Any, client_order_id: str) -> str | None:
    """Read the venue-reported order quantity from Nautilus' cache."""

    try:
        order = cache.order(ClientOrderId(client_order_id))
    except (TypeError, ValueError):
        return None
    if order is None:
        return None
    quantity = getattr(order, "quantity", None)
    return str(quantity) if quantity is not None else None


def build_binance_client_configs(
    profile: str,
    *,
    api_key: SecretStr | None,
    api_secret: SecretStr | None,
    proxy_url: str | None = None,
    hedge_mode: bool = False,
) -> tuple[
    BinanceInstrumentProviderConfig,
    BinanceDataClientConfig,
    BinanceExecClientConfig | None,
]:
    """Build the sole environment-qualified Binance client topology."""

    try:
        venue_environment, raw_ids = _PROFILE_SPEC[profile]
    except KeyError:
        raise ExecutorRuntimeError("EXECUTION_PROFILE_MISMATCH") from None
    if type(hedge_mode) is not bool:
        raise ExecutorRuntimeError("BINANCE_POSITION_MODE_INVALID")
    instrument_ids = frozenset(InstrumentId.from_str(value) for value in raw_ids)
    read_only = profile == "BINANCE_LIVE_READ_ONLY"
    if (api_key is None) != (api_secret is None):
        raise ExecutorRuntimeError("BINANCE_CREDENTIAL_PAIR_INCOMPLETE")
    if not read_only and (api_key is None or api_secret is None):
        raise ExecutorRuntimeError("BINANCE_CREDENTIAL_REQUIRED")
    provider = BinanceInstrumentProviderConfig(
        load_all=False,
        load_ids=instrument_ids,
        query_commission_rates=not read_only,
    )
    # The read-only data client remains public even when the separate account
    # observer has private credentials. This prevents an authenticated client
    # from becoming an accidental second account-state or execution path.
    key = (
        api_key.get_secret_value()
        if api_key is not None and not read_only
        else None
    )
    secret = (
        api_secret.get_secret_value()
        if api_secret is not None and not read_only
        else None
    )
    routing = RoutingConfig(default=True, venues=frozenset({BINANCE}))
    data = BinanceDataClientConfig(
        api_key=key,
        api_secret=secret,
        account_type=BinanceAccountType.USDT_FUTURES,
        environment=venue_environment,
        instrument_provider=provider,
        routing=routing,
        proxy_url=proxy_url,
    )
    execution = None
    if not read_only:
        execution = BinanceExecClientConfig(
            api_key=key,
            api_secret=secret,
            account_type=BinanceAccountType.USDT_FUTURES,
            environment=venue_environment,
            instrument_provider=provider,
            routing=routing,
            proxy_url=proxy_url,
            # Binance forbids the reduceOnly parameter in Hedge Mode.  In
            # One-way Mode this remains enabled so every Halpha reduction is
            # represented explicitly on the wire.
            use_reduce_only=not hedge_mode,
            use_position_ids=True,
            use_trade_lite=False,
            treat_expired_as_canceled=False,
            recv_window_ms=5000,
            max_retries=None,
            futures_leverages=None,
            futures_margin_types=None,
        )
    return provider, data, execution


def build_product_node_config(
    profile: str,
    *,
    api_key: SecretStr | None,
    api_secret: SecretStr | None,
    log_directory: Path,
    proxy_url: str | None = None,
    hedge_mode: bool = False,
) -> tuple[
    TradingNodeConfig,
    BinanceInstrumentProviderConfig,
    BinanceDataClientConfig,
    BinanceExecClientConfig | None,
]:
    provider, data, execution = build_binance_client_configs(
        profile,
        api_key=api_key,
        api_secret=api_secret,
        proxy_url=proxy_url,
        hedge_mode=hedge_mode,
    )
    instrument_ids = list(provider.load_ids or ())
    read_only = profile == "BINANCE_LIVE_READ_ONLY"
    exec_engine = (
        LiveExecEngineConfig(
            purge_closed_orders_interval_mins=_EXECUTION_CACHE_PURGE_INTERVAL_MINS,
            purge_closed_orders_buffer_mins=_EXECUTION_CACHE_RETENTION_MINS,
            purge_closed_positions_interval_mins=(
                _EXECUTION_CACHE_PURGE_INTERVAL_MINS
            ),
            purge_closed_positions_buffer_mins=_EXECUTION_CACHE_RETENTION_MINS,
            purge_account_events_interval_mins=_EXECUTION_CACHE_PURGE_INTERVAL_MINS,
            purge_account_events_lookback_mins=_EXECUTION_CACHE_RETENTION_MINS,
            purge_from_database=False,
            reconciliation=False,
            reconciliation_instrument_ids=None,
            inflight_check_interval_ms=0,
            open_check_interval_secs=None,
            position_check_interval_secs=None,
            generate_missing_orders=False,
            filter_unclaimed_external_orders=True,
            filter_position_reports=True,
        )
        if read_only
        else LiveExecEngineConfig(
            # PostgreSQL remains the recovery authority. Keep the framework's
            # hot cache bounded while retaining a full day for diagnostics and
            # local event correlation; purging never removes product facts.
            purge_closed_orders_interval_mins=_EXECUTION_CACHE_PURGE_INTERVAL_MINS,
            purge_closed_orders_buffer_mins=_EXECUTION_CACHE_RETENTION_MINS,
            purge_closed_positions_interval_mins=(
                _EXECUTION_CACHE_PURGE_INTERVAL_MINS
            ),
            purge_closed_positions_buffer_mins=_EXECUTION_CACHE_RETENTION_MINS,
            purge_account_events_interval_mins=_EXECUTION_CACHE_PURGE_INTERVAL_MINS,
            purge_account_events_lookback_mins=_EXECUTION_CACHE_RETENTION_MINS,
            purge_from_database=False,
            reconciliation=True,
            # Startup reconciliation is bounded to recent venue evidence.
            # Halpha's older unresolved actions are queried separately by their
            # persisted client identities.
            reconciliation_lookback_mins=_RECONCILIATION_HISTORY_LOOKBACK_MINS,
            reconciliation_instrument_ids=instrument_ids,
            reconciliation_startup_delay_secs=10.0,
            inflight_check_interval_ms=2_000,
            inflight_check_threshold_ms=5000,
            inflight_check_retries=5,
            # ProductResponsibilityBoundary queries current account orders and
            # Halpha queries unresolved actions by their persisted identities.
            # Nautilus' periodic bulk check has no persistent cache here and can
            # replay an already-owned fill as a synthetic EXTERNAL UUID order.
            open_check_interval_secs=None,
            open_check_open_only=True,
            open_check_lookback_mins=60,
            open_check_threshold_ms=5000,
            open_check_missing_retries=5,
            # Halpha reconciles venue position against persisted per-plan fills
            # in ProductResponsibilityBoundary. Nautilus has no persisted
            # position cache in this topology; its periodic "missing fill"
            # repair would therefore create synthetic EXTERNAL orders for
            # already-attributed historical fills.
            position_check_interval_secs=None,
            position_check_lookback_mins=60,
            position_check_retries=3,
            # PostgreSQL actions/facts are Halpha's recovery authority. Asking
            # Nautilus to synthesize a MARKET order when its empty process
            # cache sees an existing venue position creates a technical fill
            # with UUID identities which is not a Binance trade.
            generate_missing_orders=False,
            filter_unclaimed_external_orders=False,
            # Framework position reports are not a second persistence path;
            # ProductResponsibilityBoundary records the authoritative venue
            # query and per-plan attribution in PostgreSQL.
            filter_position_reports=True,
        )
    )
    config = TradingNodeConfig(
        environment=Environment.LIVE,
        trader_id=TraderId("HALPHA-OWNER-001"),
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
            log_directory=str(log_directory),
            log_file_name=f"halpha-{profile.lower().replace('_', '-')}",
            log_file_format="JSON",
            log_file_max_size=104857600,
            log_file_max_backup_count=5,
            log_colors=False,
            print_config=False,
            clear_log_file=False,
        ),
        data_engine=LiveDataEngineConfig(
            time_bars_interval_type="left-open",
            time_bars_timestamp_on_close=True,
            time_bars_skip_first_non_full_bar=True,
            time_bars_build_with_no_updates=False,
            validate_data_sequence=True,
        ),
        exec_engine=exec_engine,
        data_clients={BINANCE: data},
        exec_clients={} if execution is None else {BINANCE: execution},
        controller=ImportableControllerConfig(
            controller_path="halpha.executor.runtime:HalphaRuntimeController",
            config_path="nautilus_trader.live.config:ControllerConfig",
            config={},
        ),
    )
    return config, provider, data, execution


class ProductExecutorRuntime:
    """Own the one DB connection, TradingNode, controller, gate and coordinator."""

    def __init__(
        self,
        *,
        settings: ExecutorSettingsView,
        database_password: SecretStr | None,
        api_key: SecretStr | None,
        api_secret: SecretStr | None,
        log_directory: Path,
        proxy_url: str | None = None,
        forward_observation_spec: ForwardObservationSpec | None = None,
        observation_proposal_sink: Callable[[StrategyProposal], None] | None = None,
        observation_bar_sink: Callable[[object], None] | None = None,
        observation_quote_sink: Callable[[object], None] | None = None,
        observation_mark_price_sink: Callable[[object], None] | None = None,
        connector: Callable[..., Any] = psycopg.connect,
        node_factory: Callable[..., TradingNode] = TradingNode,
        loop: asyncio.AbstractEventLoop | None = None,
        runtime_real_write_gate: str = "CLOSED",
        live_write_activation_ids: tuple[str, ...] = (),
        live_write_submission_guard: Callable[[str], None] | None = None,
        live_write_risk_control_only: bool = False,
        live_write_account_qualification_refresh: Callable[[], object] | None = None,
        binance_hedge_mode: bool = False,
        runtime_event_sink: Callable[[str, dict[str, object]], None] | None = None,
        runtime_heartbeat_sink: Callable[[], object] | None = None,
        schema_validator: Callable[[Any], None] = require_current_schema,
    ) -> None:
        self._settings = settings
        self._database_password = database_password
        self._api_key = api_key
        self._api_secret = api_secret
        self._log_directory = log_directory
        self._proxy_url = proxy_url
        self._forward_observation_spec = forward_observation_spec
        self._observation_proposal_sink = observation_proposal_sink
        self._observation_bar_sink = observation_bar_sink
        self._observation_quote_sink = observation_quote_sink
        self._observation_mark_price_sink = observation_mark_price_sink
        self._connector = connector
        self._node_factory = node_factory
        self._loop = loop or asyncio.new_event_loop()
        self._owns_loop = loop is None
        self._runtime_real_write_gate = runtime_real_write_gate
        self._live_write_activation_ids = frozenset(live_write_activation_ids)
        self._live_write_submission_guard = live_write_submission_guard
        self._live_write_risk_control_only = live_write_risk_control_only
        self._live_write_account_qualification_refresh = (
            live_write_account_qualification_refresh
        )
        if type(binance_hedge_mode) is not bool:
            raise ExecutorRuntimeError("BINANCE_POSITION_MODE_INVALID")
        self._binance_hedge_mode = binance_hedge_mode
        self._runtime_event_sink = runtime_event_sink
        self._runtime_heartbeat_sink = runtime_heartbeat_sink
        self._runtime_heartbeat_handle: asyncio.Handle | None = None
        self._schema_validator = schema_validator
        self._connection: Any | None = None
        self._node: TradingNode | None = None
        self._lifecycle: ActivationAdapterLifecycle | None = None
        self._market_fact_lifecycle: ActivationAdapterLifecycle | None = None
        self._market_fact_trackers: dict[str, LiveEntryFactTracker] = {}
        self._coordinator: HalphaCoordinator | None = None
        self._capability: object | None = None
        self._proposal_processors: dict[str, ProductProposalBoundary] = {}
        self._direct_schedule_processors: dict[str, DirectScheduleBoundary] = {}
        self._direct_schedule_instruments: dict[str, str] = {}
        self._responsibility_processors: dict[str, ProductResponsibilityBoundary] = {}
        self._account_observer: ProductAccountObserver | None = None
        self._pre_submit_fact_provider: ProductPreSubmitFactProvider | None = None
        self._venue_execution_client: NautilusVenueExecutionClient | None = None
        self._framework_data_client: object | None = None
        self._framework_execution_client: object | None = None
        self._market_mark_event_at: dict[str, float] = {}
        self._market_data_stream_unhealthy_since: float | None = None
        self._market_data_stream_last_state = "UNKNOWN"
        self._execution_stream_unhealthy_since: float | None = None
        self._execution_stream_last_state = "UNKNOWN"
        self._fatal_component_failure: tuple[str, str, str] | None = None
        self._recovery_complete = False
        self._recovered_action_count = 0
        self._startup_recovery_prepared = False
        self._startup_recovered_actions: tuple[object, ...] = ()
        self._external_order_event_handler: Callable[[object], None] | None = None
        self._runtime_ready_sink: Callable[[dict[str, object]], None] | None = None
        self._runtime_ready_reported = False

    def _record_runtime_event(self, event: str, **fields: object) -> None:
        sink = getattr(self, "_runtime_event_sink", None)
        if sink is not None:
            sink(event, fields)

    def _start_runtime_heartbeat(
        self,
        *,
        interval_seconds: float = _RUNTIME_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        """Schedule an event-loop heartbeat observed by the process watchdog."""

        sink = getattr(self, "_runtime_heartbeat_sink", None)
        if sink is None:
            return
        if getattr(self, "_runtime_heartbeat_handle", None) is not None:
            raise ExecutorRuntimeError("RUNTIME_HEARTBEAT_ALREADY_STARTED")

        def heartbeat() -> None:
            self._runtime_heartbeat_handle = None
            try:
                sink()
            except Exception:
                # Leaving the independent watchdog armed is fail-closed: the
                # process will be replaced instead of running unobserved.
                return
            self._runtime_heartbeat_handle = self._loop.call_later(
                interval_seconds,
                heartbeat,
            )

        self._runtime_heartbeat_handle = self._loop.call_soon(heartbeat)

    def _stop_runtime_heartbeat(self) -> None:
        handle = getattr(self, "_runtime_heartbeat_handle", None)
        self._runtime_heartbeat_handle = None
        if handle is not None:
            handle.cancel()

    def _query_was_recently_dispatched(self, client_order_id: str) -> bool:
        client = getattr(self, "_venue_execution_client", None)
        return (
            client.query_was_recently_dispatched(client_order_id)
            if client is not None
            else False
        )

    def _arm_maintenance_stop_latch(
        self,
        stop_future: asyncio.Future[object],
    ) -> None:
        coordinator = self.__dict__.get("_coordinator")
        if coordinator is None:
            return

        def disable_venue_mutations(_completed: asyncio.Future[object]) -> None:
            coordinator.disable_venue_mutations()
            self._record_runtime_event("venue_mutations_disabled_for_maintenance_stop")

        stop_future.add_done_callback(disable_venue_mutations)

    def _require_product_database_available(self) -> None:
        """Stop all exchange mutation if the durable control plane is lost."""

        connection = getattr(self, "_connection", None)
        if connection is None:
            return
        try:
            row = connection.execute("SELECT 1").fetchone()
            if row is None or row[0] != 1:
                raise RuntimeError("DATABASE_PROBE_INVALID")
        except Exception:
            coordinator = self.__dict__.get("_coordinator")
            if coordinator is not None:
                coordinator.disable_venue_mutations()
            self._record_runtime_event("product_database_runtime_unavailable")
            raise ExecutorRuntimeError(
                "PRODUCT_DATABASE_RUNTIME_UNAVAILABLE"
            ) from None

    @property
    def node(self) -> TradingNode:
        if self._node is None:
            raise ExecutorRuntimeError("PRODUCT_RUNTIME_NOT_BUILT")
        return self._node

    @property
    def coordinator(self) -> HalphaCoordinator:
        if self._coordinator is None:
            raise ExecutorRuntimeError("PRODUCT_RUNTIME_NOT_BUILT")
        return self._coordinator

    def submit_strategy_proposal(self, proposal: StrategyProposal) -> str:
        """Enter the production EXE boundary used by the adapter proposal sink."""

        if not self._activation_submission_enabled(proposal.activation_id):
            raise ExecutorRuntimeError("STARTUP_RECOVERY_PENDING")
        processor = self._proposal_processors.get(proposal.activation_id)
        if processor is None:
            raise ExecutorRuntimeError("PRODUCT_PROPOSAL_PROCESSOR_NOT_READY")
        return processor.submit(proposal)

    def _activation_submission_enabled(self, activation_id: str) -> bool:
        settings = self.__dict__.get("_settings")
        profile = getattr(getattr(settings, "release", None), "profile", None)
        if (
            profile == "BINANCE_LIVE_WRITE"
            and getattr(self, "_live_write_risk_control_only", False)
        ):
            return False
        coordinator = self.__dict__.get("_coordinator")
        if coordinator is None:
            # Narrow construction tests bypass __init__; production write
            # runtimes always install and arm a coordinator before adapters.
            return bool(getattr(self, "_recovery_complete", True))
        return coordinator.startup_recovery_allows_submission(activation_id)

    def _prepare_startup_recovery_before_node_run(
        self,
        *,
        observed_at: datetime,
    ) -> tuple[object, ...]:
        """Persist the recovery pending set before Nautilus can emit callbacks."""

        if self._startup_recovery_prepared:
            raise ExecutorRuntimeError("STARTUP_RECOVERY_ALREADY_PREPARED")
        recovered = self.coordinator.initialize_startup_recovery(
            observed_at=observed_at,
            resolution_sink=self._queue_startup_recovery_resolution,
        )
        self._startup_recovered_actions = recovered
        self._recovered_action_count = len(recovered)
        self._recovery_complete = self.coordinator.startup_recovery_complete()
        self._startup_recovery_prepared = True
        return recovered

    def _install_external_order_event_bridge(self) -> None:
        """Capture Nautilus-generated EXTERNAL recovery events before startup."""

        if self._external_order_event_handler is not None:
            return
        normalizer = self.coordinator.build_nautilus_event_normalizer(
            leaves_quantity_for_client_order_id=lambda client_order_id: (
                _cached_leaves_quantity(self.node.cache, client_order_id)
            ),
            filled_quantity_for_client_order_id=lambda client_order_id: (
                _cached_filled_quantity(self.node.cache, client_order_id)
            ),
            order_quantity_for_client_order_id=lambda client_order_id: (
                _cached_order_quantity(self.node.cache, client_order_id)
            ),
            query_was_recently_dispatched=self._query_was_recently_dispatched,
        )

        def handle_external_order_event(event: object) -> None:
            try:
                client_order_id = getattr(event, "client_order_id", None)
                self._record_runtime_event(
                    "external_execution_event_observed",
                    event_type=type(event).__name__,
                    client_order_id=(
                        str(client_order_id)
                        if client_order_id is not None
                        else None
                    ),
                )
                normalized = self.coordinator.handle_nautilus_order_event(
                    normalizer,
                    event,
                    observed_at=datetime.now(UTC),
                )
                action = normalized.action
                if action is None:
                    return
                responsibility = self._responsibility_processors.get(
                    action.activation_id
                )
                if responsibility is not None:
                    responsibility.submit_event(normalized)
                direct = self._direct_schedule_processors.get(action.activation_id)
                if direct is not None:
                    direct.resume(
                        action.activation_id,
                        force_risk_refresh=any(
                            fact.kind is VenueFactKind.FILL
                            for fact in normalized.facts
                        ),
                    )
            except Exception as exc:
                self._handle_component_failure(
                    "external_order_bridge_failed",
                    "runtime",
                    exc,
                )
                raise ExecutorRuntimeError(
                    "EXTERNAL_ORDER_BRIDGE_FAILED"
                ) from None

        self.node.trader.subscribe(
            _EXTERNAL_ORDER_EVENT_TOPIC,
            handle_external_order_event,
        )
        self._external_order_event_handler = handle_external_order_event

    def _remove_external_order_event_bridge(self) -> None:
        handler = getattr(self, "_external_order_event_handler", None)
        node = getattr(self, "_node", None)
        if handler is None or node is None:
            return
        node.trader.unsubscribe(_EXTERNAL_ORDER_EVENT_TOPIC, handler)
        self._external_order_event_handler = None

    def _queue_startup_recovery_resolution(
        self,
        activation_id: str,
        execution_action_id: str,
    ) -> None:
        self._loop.call_soon_threadsafe(
            self._apply_startup_recovery_resolution,
            activation_id,
            execution_action_id,
        )

    def _apply_startup_recovery_resolution(
        self,
        activation_id: str,
        execution_action_id: str,
    ) -> None:
        was_complete = getattr(self, "_recovery_complete", False)
        self._recovery_complete = self.coordinator.startup_recovery_complete()
        pending = self.coordinator.startup_recovery_pending_action_ids()
        self._record_runtime_event(
            "startup_recovery_identity_resolved",
            activation_id=activation_id,
            execution_action_id=execution_action_id,
            pending_action_count=len(pending),
        )
        responsibility = self._responsibility_processors.get(activation_id)
        if responsibility is not None:
            responsibility.resume(activation_id)
        direct = self._direct_schedule_processors.get(activation_id)
        if direct is not None and self._activation_submission_enabled(activation_id):
            direct.resume(activation_id)
        if (
            self._recovery_complete
            and not was_complete
            and getattr(self, "_runtime_ready_reported", False)
        ):
            sink = getattr(self, "_runtime_ready_sink", None)
            if sink is not None:
                sink(self._runtime_ready_evidence())

    def _retry_startup_recovery(self, *, observed_at: datetime) -> tuple[str, ...]:
        coordinator = self._coordinator
        if coordinator is None or coordinator.startup_recovery_complete():
            return ()
        attempted = coordinator.retry_startup_recovery_queries(
            observed_at=observed_at,
        )
        self._recovery_complete = coordinator.startup_recovery_complete()
        if attempted:
            self._record_runtime_event(
                "startup_recovery_queries_retried",
                execution_action_ids=attempted,
                pending_action_count=len(
                    coordinator.startup_recovery_pending_action_ids()
                ),
            )
        return attempted

    @property
    def recovery_complete(self) -> bool:
        return self._recovery_complete

    @property
    def recovered_action_count(self) -> int:
        return self._recovered_action_count

    def _runtime_ready_evidence(self) -> dict[str, object]:
        coordinator = self.__dict__.get("_coordinator")
        pending_action_count = (
            len(coordinator.startup_recovery_pending_action_ids())
            if coordinator is not None
            else 0
        )
        return {
            "product_runtime_started": True,
            "database_continuity_guard_completed": True,
            "startup_reconciliation_completed": self._recovery_complete,
            "recovered_unresolved_actions": self._recovered_action_count,
            "startup_reconciliation_pending_actions": pending_action_count,
            "runtime_real_write_gate": self._runtime_real_write_gate,
            "live_write_risk_control_only": self._live_write_risk_control_only,
        }

    @property
    def strategy_history_warmup_complete(self) -> bool:
        lifecycle = self._lifecycle
        if lifecycle is None:
            return False
        return all(
            lifecycle.adapter_for_activation(activation_id).live_history_ready
            for activation_id in lifecycle.activation_ids
        )

    async def _wait_for_strategy_history_warmup(
        self,
        *,
        timeout_seconds: float = 60.0,
    ) -> None:
        deadline = self._loop.time() + timeout_seconds
        while not self.strategy_history_warmup_complete:
            self._require_no_fatal_component_failure()
            if self._loop.time() >= deadline:
                raise ExecutorRuntimeError("LIVE_HISTORY_WARMUP_TIMEOUT")
            await asyncio.sleep(0.05)

    def build(self) -> None:
        if self._node is not None:
            raise ExecutorRuntimeError("PRODUCT_RUNTIME_ALREADY_BUILT")
        release = self._settings.release
        read_only = release.profile == "BINANCE_LIVE_READ_ONLY"
        private_account_observation = False
        if release.profile == "BINANCE_LIVE_WRITE":
            full_write = (
                self._runtime_real_write_gate == "OPEN"
                and not self._live_write_risk_control_only
            )
            risk_control_only = (
                self._runtime_real_write_gate == "CLOSED"
                and self._live_write_risk_control_only
            )
            if (
                not (full_write or risk_control_only)
                or not self._live_write_activation_ids
                or self._live_write_submission_guard is None
            ):
                raise ExecutorRuntimeError("RUNTIME_REAL_WRITE_GATE_CLOSED")
        database_password = self._database_password
        if read_only:
            private_account_observation = self._forward_observation_spec is None
            if private_account_observation:
                if (
                    database_password is None
                    or self._api_key is None
                    or self._api_secret is None
                ):
                    raise ExecutorRuntimeError(
                        "READ_ONLY_ACCOUNT_OBSERVATION_REQUIREMENTS_INCOMPLETE"
                    )
            else:
                if database_password is not None:
                    raise ExecutorRuntimeError(
                        "READ_ONLY_DATABASE_CREDENTIAL_FORBIDDEN"
                    )
                if self._api_key is not None or self._api_secret is not None:
                    raise ExecutorRuntimeError(
                        "READ_ONLY_BINANCE_CREDENTIAL_FORBIDDEN"
                    )
                if self._forward_observation_spec.profile != release.profile:
                    raise ExecutorRuntimeError(
                        "READ_ONLY_OBSERVATION_PROFILE_MISMATCH"
                    )
        elif database_password is None:
            raise ExecutorRuntimeError("PRODUCT_DATABASE_CREDENTIAL_REQUIRED")
        try:
            config, provider, data, execution = build_product_node_config(
                release.profile,
                api_key=self._api_key,
                api_secret=self._api_secret,
                log_directory=self._log_directory,
                proxy_url=self._proxy_url,
                hedge_mode=self._binance_hedge_mode,
            )
            if data.instrument_provider is not provider:
                raise ExecutorRuntimeError("BINANCE_PROVIDER_IDENTITY_MISMATCH")
            if read_only and execution is not None:
                raise ExecutorRuntimeError("READ_ONLY_EXECUTION_CLIENT_FORBIDDEN")
            if not read_only and (
                execution is None or execution.instrument_provider is not provider
            ):
                raise ExecutorRuntimeError("BINANCE_PROVIDER_IDENTITY_MISMATCH")
            connection = None
            if not read_only or private_account_observation:
                if database_password is None:
                    raise ExecutorRuntimeError("PRODUCT_DATABASE_CREDENTIAL_REQUIRED")
                connection = _connect_product_database(
                    self._connector,
                    database_name=release.database_name,
                    password=database_password.get_secret_value(),
                    application_name=EXECUTOR_STARTING_APPLICATION_NAME,
                )
                self._connection = connection
                try:
                    self._schema_validator(connection)
                except Exception:
                    raise ExecutorRuntimeError(
                        "DATABASE_SCHEMA_NOT_CURRENT"
                    ) from None
                if not read_only:
                    with connection.transaction():
                        PlanningApplicationService(
                            connection,
                            release.environment_id,
                        ).recover_completed_command_receipts(
                            observed_at=datetime.now(UTC),
                        )
            node = self._node_factory(config=config, loop=self._loop)
            node.add_data_client_factory(BINANCE, BinanceLiveDataClientFactory)
            if execution is not None:
                node.add_exec_client_factory(BINANCE, BinanceLiveExecClientFactory)
            node.build()
            if isinstance(node, TradingNode):
                self._framework_data_client = _resolve_binance_data_client(node)
                if execution is not None:
                    self._framework_execution_client = (
                        _resolve_binance_execution_client(node)
                    )
                    _require_zero_binance_mutation_retries(
                        self._framework_execution_client
                    )
            controllers = [
                actor
                for actor in node.trader.actors()
                if isinstance(actor, HalphaRuntimeController)
            ]
            if len(controllers) != 1:
                raise ExecutorRuntimeError("CONTROLLER_COUNT_MISMATCH")
            lifecycle = ActivationAdapterLifecycle(controllers[0])
            market_fact_lifecycle = ActivationAdapterLifecycle(controllers[0])
            self._node = node
            self._lifecycle = lifecycle
            self._market_fact_lifecycle = market_fact_lifecycle
            if read_only:
                if private_account_observation:
                    if connection is None:
                        raise ExecutorRuntimeError(
                            "PRODUCT_DATABASE_CREDENTIAL_REQUIRED"
                        )
                    if self._api_key is None or self._api_secret is None:
                        raise ExecutorRuntimeError(
                            "BINANCE_CREDENTIAL_REQUIRED"
                        )
                    self._account_observer = ProductAccountObserver(
                        profile=release.profile,
                        environment_id=release.environment_id,
                        account_ref=release.account_id,
                        api_key=self._api_key,
                        api_secret=self._api_secret,
                        proxy_url=self._proxy_url,
                        repository=PostgreSQLVenueFactRepository(
                            connection,
                            release.environment_id,
                        ),
                    )
                return
            if connection is None:
                raise ExecutorRuntimeError("PRODUCT_DATABASE_CREDENTIAL_REQUIRED")
            capability = object()
            self._capability = capability
            action_repository = PostgreSQLExecutionActionRepository(
                connection,
                release.environment_id,
            )
            client = NautilusVenueExecutionClient(
                lifecycle.adapter_for_activation,
                capability,
            )
            self._venue_execution_client = client
            gate = PersistedActionGate(
                action_repository,
                client,
                environment_id=release.environment_id,
                execution_profile_ref=release.profile,
                account_ref=release.account_id,
            )
            environment_kind = "DEMO" if release.profile == "BINANCE_DEMO" else "LIVE"
            coordinator = HalphaCoordinator(
                connection,
                gate,
                environment_id=release.environment_id,
                environment_kind=environment_kind,
                authority_class=release.authority_class,
                execution_profile_ref=release.profile,
                account_ref=release.account_id,
                venue_ref=BINANCE_USDM_VENUE_REF,
                runtime_real_write_gate=self._runtime_real_write_gate,
                live_write_activation_ids=tuple(sorted(self._live_write_activation_ids)),
                live_write_submission_guard=self._live_write_submission_guard,
                live_write_risk_control_only=self._live_write_risk_control_only,
                new_risk_discipline_policy=self._settings.new_risk_discipline,
                unattributed_reconciliation_not_before=(
                    datetime.now(UTC)
                    - timedelta(minutes=_RECONCILIATION_HISTORY_LOOKBACK_MINS)
                ),
            )
            coordinator.arm_startup_recovery_barrier()
            self._coordinator = coordinator
            if self._api_key is None or self._api_secret is None:
                raise ExecutorRuntimeError("BINANCE_CREDENTIAL_REQUIRED")
            # One account-scoped provider shares the authenticated Nautilus
            # client and any venue cooldown across every activation.
            self._pre_submit_fact_provider = ProductPreSubmitFactProvider(
                node=self.node,
                profile=release.profile,
                api_key=self._api_key,
                api_secret=self._api_secret,
                proxy_url=self._proxy_url,
                attribution_provider=coordinator.account_instrument_attribution,
            )
            if release.profile in {"BINANCE_DEMO", "BINANCE_LIVE_WRITE"}:
                self._account_observer = ProductAccountObserver(
                    profile=release.profile,
                    environment_id=release.environment_id,
                    account_ref=release.account_id,
                    api_key=self._api_key,
                    api_secret=self._api_secret,
                    proxy_url=self._proxy_url,
                    repository=PostgreSQLVenueFactRepository(
                        connection,
                        release.environment_id,
                    ),
                )
        except ExecutorRuntimeError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise ExecutorRuntimeError(
                f"PRODUCT_RUNTIME_BUILD_FAILED type={type(exc).__name__}"
            ) from None

    def publish_ready_product_build(self, product_build_id: str) -> None:
        """Publish framework readiness through the Executor's existing DB session."""

        if (
            self._settings.release.profile == "BINANCE_LIVE_READ_ONLY"
            and self._account_observer is None
        ):
            return
        if self._connection is None:
            raise ExecutorRuntimeError("PRODUCT_DATABASE_CONNECTION_MISSING")
        try:
            self._connection.execute(
                "SELECT set_config('application_name', %s, false)",
                (executor_ready_application_name(product_build_id),),
            )
        except Exception:
            raise ExecutorRuntimeError("EXECUTOR_READINESS_PUBLISH_FAILED") from None

    @staticmethod
    def _decimal_text(value: object | None) -> str | None:
        if value is None:
            return None
        if hasattr(value, "as_decimal"):
            value = value.as_decimal()
        try:
            return canonical_decimal(value)
        except Exception:
            return None

    def _read_only_sizing_snapshot(self, _bar: object) -> EntrySizingSnapshot | None:
        spec = self._forward_observation_spec
        if spec is None:
            raise ExecutorRuntimeError("READ_ONLY_OBSERVATION_SPEC_REQUIRED")
        instrument_id = InstrumentId.from_str(f"{spec.instrument_ref}.BINANCE")
        instrument = self.node.cache.instrument(instrument_id)
        quote = self.node.cache.quote_tick(instrument_id)
        mark = self.node.cache.mark_price(instrument_id)
        if instrument is None or quote is None or mark is None:
            return None
        direction = spec.parameters.direction.value
        reference_price = quote.ask_price if direction == "LONG" else quote.bid_price
        values = {
            "step_size": self._decimal_text(instrument.size_increment),
            "price_tick_size": self._decimal_text(instrument.price_increment),
            "min_quantity": self._decimal_text(instrument.min_quantity),
            "max_market_quantity": self._decimal_text(instrument.max_quantity),
            "min_notional": self._decimal_text(instrument.min_notional),
            "taker_fee_rate": self._decimal_text(instrument.taker_fee),
        }
        if any(value is None for value in values.values()):
            return None
        return EntrySizingSnapshot(
            reference_price=str(reference_price),
            reference_source=f"LIVE_TOP_OF_BOOK_{'ASK' if direction == 'LONG' else 'BID'}",
            max_allowed_loss=spec.max_allowed_loss,
            max_notional=spec.max_notional,
            max_margin=spec.max_margin,
            effective_leverage=spec.effective_leverage,
            taker_fee_rate=str(values["taker_fee_rate"]),
            rules=InstrumentQuantityRules(
                step_size=str(values["step_size"]),
                price_tick_size=str(values["price_tick_size"]),
                min_quantity=str(values["min_quantity"]),
                max_market_quantity=str(values["max_market_quantity"]),
                min_notional=str(values["min_notional"]),
            ),
        )

    def _start_read_only_adapter(self) -> None:
        if self._lifecycle is None or self._forward_observation_spec is None:
            raise ExecutorRuntimeError("PRODUCT_RUNTIME_NOT_BUILT")
        spec = self._forward_observation_spec
        proposal_sink = self._observation_proposal_sink or (lambda _proposal: None)

        def quote_sink(tick: object) -> None:
            if self._observation_quote_sink is not None:
                self._observation_quote_sink(tick)

        def mark_price_sink(mark_price: object) -> None:
            if self._observation_mark_price_sink is not None:
                self._observation_mark_price_sink(mark_price)
            self._record_market_mark_event(spec.instrument_ref)

        def bar_failure_sink(bar: object, exception: Exception) -> None:
            self._handle_component_failure(
                "read_only_strategy_bar_failed",
                spec.activation_id,
                exception,
                bar_type=str(getattr(bar, "bar_type", "UNKNOWN")),
            )

        def history_warmup_failure_sink(
            stage: str,
            item: object | None,
            exception: Exception,
        ) -> None:
            self._handle_component_failure(
                "read_only_history_warmup_failed",
                spec.activation_id,
                exception,
                stage=stage,
                item_type=type(item).__name__ if item is not None else "NONE",
            )

        evaluator = NautilusBarEntryEvaluator(
            activation_id=spec.activation_id,
            instrument_ref=spec.instrument_ref,
            parameters=spec.parameters,
            decision_not_before=spec.starts_at,
            valid_until=spec.entry_valid_until,
            sizing_provider=self._read_only_sizing_snapshot,
            requires_live_warmup=True,
        )
        self._lifecycle.start(
            ActivationAdapterSpec(
                activation_id=spec.activation_id,
                factory=lambda: HalphaStrategyAdapter(
                    activation_id=spec.activation_id,
                    logic=OneShotDonchianAtrLogic(spec.parameters),
                    state_provider=lambda: ActivationStrategyState(),
                    proposal_sink=proposal_sink,
                    instrument_ref=spec.instrument_ref,
                    persisted_action_capability=None,
                    execution_event_sink=None,
                    bar_evaluator=evaluator,
                    bar_event_sink=self._observation_bar_sink,
                    bar_failure_sink=bar_failure_sink,
                    history_warmup_failure_sink=history_warmup_failure_sink,
                    quote_event_sink=self._guard_component_sink(
                        "read_only_quote_processing_failed",
                        spec.activation_id,
                        quote_sink,
                    ),
                    mark_price_event_sink=self._guard_component_sink(
                        "read_only_mark_price_processing_failed",
                        spec.activation_id,
                        mark_price_sink,
                    ),
                    live_history_warmup=True,
                ),
            )
        )

    def _require_pre_submit_fact_provider(self) -> ProductPreSubmitFactProvider:
        provider = getattr(self, "_pre_submit_fact_provider", None)
        if provider is not None:
            return provider
        api_key = self._api_key
        api_secret = self._api_secret
        if api_key is None or api_secret is None:
            raise ExecutorRuntimeError("BINANCE_CREDENTIAL_REQUIRED")
        provider = ProductPreSubmitFactProvider(
            node=self.node,
            profile=self._settings.release.profile,
            api_key=api_key,
            api_secret=api_secret,
            proxy_url=self._proxy_url,
            attribution_provider=self.coordinator.account_instrument_attribution,
        )
        self._pre_submit_fact_provider = provider
        return provider

    def _start_direct_execution_adapter(
        self,
        activation: object,
        capability: object,
    ) -> None:
        if self._lifecycle is None:
            raise ExecutorRuntimeError("PRODUCT_RUNTIME_NOT_BUILT")
        try:
            environment_kind = require_direct_activation_profile_consistency(
                activation,
                profile=self._settings.release.profile,
            )
        except ProductPreSubmitRejected as exc:
            raise ExecutorRuntimeError(exc.reason_code) from None
        fact_provider = self._require_pre_submit_fact_provider()
        instrument_ref = str(getattr(activation, "instrument_ref"))
        schedule_snapshot = getattr(activation, "order_schedule_snapshot", None)
        schedule_spec = getattr(schedule_snapshot, "schedule_spec", None)
        entry_conditions = getattr(schedule_spec, "entry_conditions", None)
        uses_closed_bar_15m = any(
            getattr(condition, "kind", None)
            == EntryConditionKind.CLOSED_BAR_PRICE_15M
            for condition in getattr(entry_conditions, "items", ())
        )
        if not hasattr(self, "_market_fact_trackers"):
            self._market_fact_trackers = {}
        tracker = getattr(self, "_market_fact_trackers", {}).get(instrument_ref)
        if tracker is None:
            tracker = LiveEntryFactTracker(instrument_ref)
            self._market_fact_trackers[instrument_ref] = tracker
        else:
            tracker.configure_closed_bar_15m(instrument_ref)
        cached_quote = self.node.cache.quote_tick(
            InstrumentId.from_str(f"{instrument_ref}.BINANCE")
        )
        if cached_quote is not None:
            tracker.record_quote(cached_quote)
        _, market_source = binance_public_market_identity(
            self._settings.release.profile
        )
        activation_id = str(getattr(activation, "activation_id"))

        async def direct_facts(
            used_activation,
            leg,
            owned_order_client_ids,
            owned_algo_client_ids,
            expected_signed_position,
            outstanding_entry_quantity,
            outstanding_entry_notional,
        ):
            return await fact_provider.direct_pre_submit_facts(
                used_activation,
                leg,
                owned_order_client_ids=owned_order_client_ids,
                owned_algo_client_ids=owned_algo_client_ids,
                expected_signed_position=expected_signed_position,
                outstanding_entry_quantity=outstanding_entry_quantity,
                outstanding_entry_notional=outstanding_entry_notional,
            )

        def direct_condition_facts(
            used_activation,
            cutoff_ns,
            observed_at,
            price_move_bps_by_window,
        ):
            return tracker.direct_condition_facts(
                f"{instrument_ref}.BINANCE",
                cutoff_ns=cutoff_ns,
                observed_at=observed_at,
                activated_at=used_activation.created_at,
                price_move_bps_by_window=price_move_bps_by_window,
                market_source=market_source,
            )

        direct = DirectScheduleBoundary(
            loop=self._loop,
            coordinator=self.coordinator,
            pre_submit_fact_provider=direct_facts,
            condition_fact_provider=direct_condition_facts,
            risk_reduction_fact_provider=fact_provider.risk_reduction_facts,
            environment_id=self._settings.release.environment_id,
            environment_kind=environment_kind,
            authority_class=AuthorityClass(self._settings.release.authority_class),
            account_ref=self._settings.release.account_id,
            submission_enabled=lambda: self._activation_submission_enabled(
                activation_id
            ),
            failure_sink=lambda activation_id, exception: (
                self._handle_component_failure(
                    "direct_schedule_failed",
                    activation_id,
                    exception,
                )
            ),
        )
        responsibility = ProductResponsibilityBoundary(
            loop=self._loop,
            coordinator=self.coordinator,
            fact_provider=fact_provider.risk_reduction_facts,
            funding_provider=lambda used_activation, end_time: (
                fact_provider.funding_income(
                    used_activation,
                    end_time=end_time,
                )
            ),
            called_action_recovery_fact_provider=(
                fact_provider.called_action_recovery_facts
            ),
            environment_id=self._settings.release.environment_id,
            failure_sink=lambda exception: self._handle_component_failure(
                "product_responsibility_failed",
                activation_id,
                exception,
            ),
            funding_fact_unavailable_sink=lambda exception: (
                self._record_runtime_event(
                    "funding_fact_unavailable",
                    activation_id=activation_id,
                    reason=type(exception).__name__,
                    **_stable_failure_reason_code(exception),
                )
            ),
        )
        normalizer = self.coordinator.build_nautilus_event_normalizer(
            leaves_quantity_for_client_order_id=lambda client_order_id: (
                _cached_leaves_quantity(self.node.cache, client_order_id)
            ),
            filled_quantity_for_client_order_id=lambda client_order_id: (
                _cached_filled_quantity(self.node.cache, client_order_id)
            ),
            order_quantity_for_client_order_id=lambda client_order_id: (
                _cached_order_quantity(self.node.cache, client_order_id)
            ),
            query_was_recently_dispatched=self._query_was_recently_dispatched,
        )

        def event_sink(event: object) -> None:
            client_order_id = getattr(event, "client_order_id", None)
            self._record_runtime_event(
                "execution_event_observed",
                activation_id=activation_id,
                event_type=type(event).__name__,
                venue_reason_code=_venue_event_reason_code(event),
                client_order_id=(
                    str(client_order_id) if client_order_id is not None else None
                ),
            )
            normalized = self.coordinator.handle_nautilus_order_event(
                normalizer,
                event,
                observed_at=datetime.now(UTC),
            )
            responsibility.submit_event(normalized)
            direct.resume(
                activation_id,
                force_risk_refresh=any(
                    fact.kind is VenueFactKind.FILL for fact in normalized.facts
                ),
            )

        def quote_sink(tick: object) -> None:
            tracker.record_quote(tick)
            direct.resume(activation_id)

        def mark_price_sink(update: object) -> None:
            tracker.record_mark(update)
            self._record_market_mark_event(instrument_ref)
            direct.record_mark(activation_id, update)

        self._direct_schedule_processors[activation_id] = direct
        if not hasattr(self, "_direct_schedule_instruments"):
            self._direct_schedule_instruments = {}
        self._direct_schedule_instruments[activation_id] = instrument_ref
        self._responsibility_processors[activation_id] = responsibility
        if uses_closed_bar_15m:
            self._ensure_closed_bar_fact_stream(instrument_ref, tracker)
        self._lifecycle.start(
            ActivationAdapterSpec(
                activation_id=activation_id,
                factory=lambda: HalphaStrategyAdapter(
                    activation_id=activation_id,
                    instrument_ref=instrument_ref,
                    persisted_action_capability=capability,
                    execution_event_sink=self._guard_component_sink(
                        "direct_execution_event_processing_failed",
                        activation_id,
                        event_sink,
                    ),
                    quote_event_sink=self._guard_component_sink(
                        "direct_quote_processing_failed",
                        activation_id,
                        quote_sink,
                    ),
                    mark_price_event_sink=self._guard_component_sink(
                        "direct_mark_price_processing_failed",
                        activation_id,
                        mark_price_sink,
                    ),
                    live_history_warmup=False,
                ),
            )
        )
        self._record_runtime_event(
            "direct_execution_adapter_started",
            activation_id=activation_id,
            entry_valid_until=_activation_entry_deadline(activation).isoformat(),
        )

    def _start_position_alignment_adapter(
        self,
        activation: object,
        capability: object,
    ) -> None:
        """Attach only the persisted reduce-only responsibility for a baseline."""

        if self._lifecycle is None:
            raise ExecutorRuntimeError("PRODUCT_RUNTIME_NOT_BUILT")
        if getattr(activation, "position_alignment", None) is None:
            raise ExecutorRuntimeError("POSITION_ALIGNMENT_REQUIRED")
        profile = self._settings.release.profile
        if profile == "BINANCE_LIVE_READ_ONLY":
            raise ExecutorRuntimeError("POSITION_ALIGNMENT_WRITE_RUNTIME_REQUIRED")
        expected_kind = (
            EnvironmentKind.DEMO
            if profile == "BINANCE_DEMO"
            else EnvironmentKind.LIVE
        )
        if getattr(activation, "environment_kind", None) is not expected_kind:
            raise ExecutorRuntimeError("ACTIVATION_ENVIRONMENT_PROFILE_MISMATCH")
        fact_provider = self._require_pre_submit_fact_provider()
        activation_id = str(getattr(activation, "activation_id"))
        instrument_ref = str(getattr(activation, "instrument_ref"))
        responsibility = ProductResponsibilityBoundary(
            loop=self._loop,
            coordinator=self.coordinator,
            fact_provider=fact_provider.risk_reduction_facts,
            # The pre-existing entry remains external history.  This bounded
            # disposition does not claim funding or strategy PnL before it.
            funding_provider=None,
            called_action_recovery_fact_provider=(
                fact_provider.called_action_recovery_facts
            ),
            environment_id=self._settings.release.environment_id,
            failure_sink=lambda exception: self._handle_component_failure(
                "position_alignment_responsibility_failed",
                activation_id,
                exception,
            ),
        )
        normalizer = self.coordinator.build_nautilus_event_normalizer(
            leaves_quantity_for_client_order_id=lambda client_order_id: (
                _cached_leaves_quantity(self.node.cache, client_order_id)
            ),
            filled_quantity_for_client_order_id=lambda client_order_id: (
                _cached_filled_quantity(self.node.cache, client_order_id)
            ),
            order_quantity_for_client_order_id=lambda client_order_id: (
                _cached_order_quantity(self.node.cache, client_order_id)
            ),
            query_was_recently_dispatched=self._query_was_recently_dispatched,
        )

        def event_sink(event: object) -> None:
            normalized = self.coordinator.handle_nautilus_order_event(
                normalizer,
                event,
                observed_at=datetime.now(UTC),
            )
            responsibility.submit_event(normalized)

        self._responsibility_processors[activation_id] = responsibility
        self._lifecycle.start(
            ActivationAdapterSpec(
                activation_id=activation_id,
                factory=lambda: HalphaStrategyAdapter(
                    activation_id=activation_id,
                    instrument_ref=instrument_ref,
                    persisted_action_capability=capability,
                    execution_event_sink=self._guard_component_sink(
                        "position_alignment_execution_event_processing_failed",
                        activation_id,
                        event_sink,
                    ),
                ),
            )
        )
        self._record_runtime_event(
            "position_alignment_adapter_started",
            activation_id=activation_id,
            operation=str(getattr(activation.position_alignment, "operation")),
        )

    def _ensure_closed_bar_fact_stream(
        self,
        instrument_ref: str,
        tracker: LiveEntryFactTracker,
    ) -> None:
        """Start one shared, history-warmed 15m fact stream when a plan needs it."""

        lifecycle = getattr(self, "_market_fact_lifecycle", None)
        if lifecycle is None:
            raise ExecutorRuntimeError("PRODUCT_RUNTIME_NOT_BUILT")
        stream_id = f"closed-bar-facts:{instrument_ref}"
        if stream_id in lifecycle.activation_ids:
            return

        def bar_sink(bar: object) -> None:
            self._record_runtime_event(
                "direct_condition_bar_observed",
                instrument_ref=instrument_ref,
                bar_type=str(getattr(bar, "bar_type", "UNKNOWN")),
                ts_event=int(getattr(bar, "ts_event", 0)),
            )
            instruments = getattr(self, "_direct_schedule_instruments", {})
            for activation_id, processor in tuple(
                getattr(self, "_direct_schedule_processors", {}).items()
            ):
                if instruments.get(activation_id) == instrument_ref:
                    processor.resume(activation_id)

        def bar_failure_sink(bar: object, exception: Exception) -> None:
            self._handle_component_failure(
                "direct_condition_bar_failed",
                stream_id,
                exception,
                instrument_ref=instrument_ref,
                bar_type=str(getattr(bar, "bar_type", "UNKNOWN")),
            )

        def history_warmup_failure_sink(
            stage: str,
            item: object | None,
            exception: Exception,
        ) -> None:
            self._handle_component_failure(
                "direct_condition_history_warmup_failed",
                stream_id,
                exception,
                instrument_ref=instrument_ref,
                stage=stage,
                item_type=type(item).__name__ if item is not None else "NONE",
                bar_type=str(getattr(item, "bar_type", "UNKNOWN")),
            )

        lifecycle.start(
            ActivationAdapterSpec(
                activation_id=stream_id,
                factory=lambda: HalphaStrategyAdapter(
                    activation_id=stream_id,
                    bar_evaluator=tracker,
                    bar_event_sink=bar_sink,
                    bar_failure_sink=bar_failure_sink,
                    history_warmup_failure_sink=history_warmup_failure_sink,
                    live_history_warmup=True,
                ),
            )
        )
        self._record_runtime_event(
            "direct_condition_bar_stream_started",
            instrument_ref=instrument_ref,
        )

    def _start_market_fact_streams(self) -> None:
        """Keep one framework-owned quote/mark stream warm per product instrument."""

        lifecycle = getattr(self, "_market_fact_lifecycle", None)
        if lifecycle is None:
            raise ExecutorRuntimeError("PRODUCT_RUNTIME_NOT_BUILT")
        profile = self._settings.release.profile
        if profile == "BINANCE_LIVE_READ_ONLY":
            return
        try:
            instrument_ids = _PROFILE_SPEC[profile][1]
        except KeyError:
            raise ExecutorRuntimeError("PRODUCT_PROFILE_UNSUPPORTED") from None
        for instrument_id in instrument_ids:
            instrument_ref = instrument_id.removesuffix(".BINANCE")
            tracker = self._market_fact_trackers.setdefault(
                instrument_ref,
                LiveEntryFactTracker(instrument_ref),
            )
            tracker.configure_closed_bar_15m(instrument_ref)

            def quote_sink(
                tick: object,
                *,
                used_tracker: LiveEntryFactTracker = tracker,
                used_instrument_ref: str = instrument_ref,
            ) -> None:
                used_tracker.record_quote(tick)
                instruments = getattr(self, "_direct_schedule_instruments", {})
                for activation_id, processor in tuple(
                    getattr(self, "_direct_schedule_processors", {}).items()
                ):
                    if instruments.get(activation_id) == used_instrument_ref:
                        processor.resume(activation_id)

            def mark_price_sink(
                update: object,
                *,
                used_tracker: LiveEntryFactTracker = tracker,
                used_instrument_ref: str = instrument_ref,
            ) -> None:
                used_tracker.record_mark(update)
                self._record_market_mark_event(used_instrument_ref)

            lifecycle.start(
                ActivationAdapterSpec(
                    activation_id=f"market-facts:{instrument_ref}",
                    factory=lambda instrument_ref=instrument_ref,
                    tracker=tracker,
                    quote_sink=quote_sink: HalphaStrategyAdapter(
                        activation_id=f"market-facts:{instrument_ref}",
                        instrument_ref=instrument_ref,
                        quote_event_sink=self._guard_component_sink(
                            "market_fact_quote_processing_failed",
                            f"market-facts:{instrument_ref}",
                            quote_sink,
                        ),
                        mark_price_event_sink=self._guard_component_sink(
                            "market_fact_mark_price_processing_failed",
                            f"market-facts:{instrument_ref}",
                            mark_price_sink,
                        ),
                    ),
                )
            )

    def _restore_paused_adapters(self, capability: object) -> None:
        if self._connection is None or self._lifecycle is None:
            raise ExecutorRuntimeError("PRODUCT_RUNTIME_NOT_BUILT")
        # A few narrow construction tests instantiate the runtime without its
        # initializer; keep the restore boundary self-contained as well.
        if not hasattr(self, "_direct_schedule_processors"):
            self._direct_schedule_processors = {}
        planning = PostgreSQLPlanningRepository(
            self._connection,
            self._settings.release.environment_id,
        )
        list_runtime_activations = getattr(
            planning,
            "list_runtime_responsibility_activations",
            planning.list_open_activations,
        )
        activations = list_runtime_activations()
        if self._settings.release.profile == "BINANCE_DEMO":
            observed_at = datetime.now(UTC)
            for activation in activations:
                rule_state = getattr(activation, "rule_state", {})
                deadlines = (
                    rule_state.get("deadlines", {})
                    if isinstance(rule_state, dict)
                    else {}
                )
                deadline_value = (
                    deadlines.get("entry_valid_until")
                    if isinstance(deadlines, dict)
                    else None
                )
                if (
                    activation.lifecycle is not PlanLifecycle.RUNNING
                    or activation.entry_opportunity_consumed
                    or not isinstance(deadline_value, str)
                ):
                    continue
                try:
                    deadline = datetime.fromisoformat(
                        deadline_value.replace("Z", "+00:00")
                    )
                except ValueError:
                    raise ExecutorRuntimeError("ENTRY_DEADLINE_INVALID") from None
                if observed_at >= deadline:
                    self.coordinator.expire_empty_entry_window(
                        activation_id=activation.activation_id,
                        observed_at=observed_at,
                    )
            activations = list_runtime_activations()
        if self._settings.release.profile == "BINANCE_LIVE_WRITE":
            authorized_activation_ids = self._live_write_activation_ids
            if (
                not authorized_activation_ids
                or {item.activation_id for item in activations}
                != authorized_activation_ids
            ):
                raise ExecutorRuntimeError("LIVE_WRITE_ACTIVATION_SET_MISMATCH")
        open_activation_ids = {activation.activation_id for activation in activations}
        removed_stale_adapter = False
        for activation_id in set(self._lifecycle.activation_ids) - open_activation_ids:
            self._lifecycle.stop_and_remove(activation_id)
            removed_stale_adapter = True
            proposal_processor = self._proposal_processors.pop(activation_id, None)
            if proposal_processor is not None:
                proposal_processor.close()
            direct_processor = self._direct_schedule_processors.pop(
                activation_id,
                None,
            )
            if direct_processor is not None:
                direct_processor.close()
            getattr(self, "_direct_schedule_instruments", {}).pop(
                activation_id,
                None,
            )
            responsibility_processor = self._responsibility_processors.pop(
                activation_id,
                None,
            )
            if responsibility_processor is not None:
                responsibility_processor.close()
        if removed_stale_adapter:
            # Nautilus requires no active subscription for the same underlying
            # market while aggregated history is requested. Give the controller
            # one sync cycle to finish the old adapter's unsubscriptions before
            # starting the replacement activation and its warmup request.
            return
        for activation in activations:
            if activation.lifecycle is PlanLifecycle.USER_TAKEOVER:
                self.coordinator.apply_persisted_user_takeover(
                    activation_id=activation.activation_id,
                    observed_at=datetime.now(UTC),
                )
            if activation.activation_id in self._lifecycle.activation_ids:
                continue
            if getattr(activation, "position_alignment", None) is not None:
                self._start_position_alignment_adapter(activation, capability)
                continue
            if getattr(activation, "decision_basis_ref", None) == DIRECT_EXECUTION_REF:
                self._start_direct_execution_adapter(activation, capability)
                continue
            version = planning.get_version(activation.plan_version_ref)
            parameters = OneShotParameters.model_validate(
                version.strategy_basis.normalized_parameters
            )
            if (
                parameters.demo_immediate_entry
                and self._settings.release.profile != "BINANCE_DEMO"
            ):
                raise ExecutorRuntimeError("DEMO_IMMEDIATE_ENTRY_REQUIRES_DEMO")
            entry_valid_until = _activation_entry_deadline(activation)
            tracker = LiveEntryFactTracker()
            fact_provider = self._require_pre_submit_fact_provider()
            environment_kind = (
                EnvironmentKind.DEMO
                if self._settings.release.profile == "BINANCE_DEMO"
                else EnvironmentKind.LIVE
            )
            processor = ProductProposalBoundary(
                loop=self._loop,
                coordinator=self.coordinator,
                fact_provider=fact_provider,
                environment_id=self._settings.release.environment_id,
                environment_kind=environment_kind,
                authority_class=AuthorityClass(self._settings.release.authority_class),
                account_ref=self._settings.release.account_id,
                failure_sink=lambda exception,
                activation_id=activation.activation_id: self._handle_component_failure(
                    "product_proposal_failed",
                    activation_id,
                    exception,
                ),
            )
            self._proposal_processors[activation.activation_id] = processor
            responsibility = ProductResponsibilityBoundary(
                loop=self._loop,
                coordinator=self.coordinator,
                fact_provider=fact_provider.risk_reduction_facts,
                funding_provider=lambda used_activation, end_time: (
                    fact_provider.funding_income(
                        used_activation,
                        end_time=end_time,
                    )
                ),
                called_action_recovery_fact_provider=(
                    fact_provider.called_action_recovery_facts
                ),
                environment_id=self._settings.release.environment_id,
                failure_sink=lambda exception,
                activation_id=activation.activation_id: self._handle_component_failure(
                    "product_responsibility_failed",
                    activation_id,
                    exception,
                ),
                funding_fact_unavailable_sink=lambda exception,
                activation_id=activation.activation_id: self._record_runtime_event(
                    "funding_fact_unavailable",
                    activation_id=activation_id,
                    reason=type(exception).__name__,
                    **_stable_failure_reason_code(exception),
                ),
            )
            self._responsibility_processors[activation.activation_id] = responsibility

            def state_provider(
                activation_id: str = activation.activation_id,
            ) -> ActivationStrategyState:
                current = planning.get_activation(activation_id)
                entry_responsibility_open = (
                    self.coordinator.has_open_entry_responsibility(activation_id)
                )
                new_risk_allowed = self.coordinator.new_risk_allowed(activation_id)
                return ActivationStrategyState(
                    entry_opportunity_consumed=(
                        current.entry_opportunity_consumed or entry_responsibility_open
                    ),
                    lifecycle=current.lifecycle.value,
                    run_state=current.run_state.value,
                    new_risk_allowed=(
                        current.lifecycle is PlanLifecycle.RUNNING
                        and current.run_state is RunState.ACTIVE
                        and new_risk_allowed
                        and self._activation_submission_enabled(activation_id)
                    ),
                )

            def proposal_sink(
                proposal: StrategyProposal,
            ) -> None:
                self._record_runtime_event(
                    "strategy_proposal_observed",
                    activation_id=proposal.activation_id,
                    source_identity=proposal.source_identity,
                )
                self.submit_strategy_proposal(proposal)

            def sizing_provider(
                _bar: object,
                *,
                activation_id: str = activation.activation_id,
                instrument_ref: str = activation.instrument_ref,
                direction=activation.direction,
                used_tracker: LiveEntryFactTracker = tracker,
            ) -> EntrySizingSnapshot | None:
                self._record_runtime_event(
                    "entry_sizing_requested",
                    activation_id=activation_id,
                )
                instrument_id = f"{instrument_ref}.BINANCE"
                nautilus_instrument_id = InstrumentId.from_str(instrument_id)
                instrument = self.node.cache.instrument(nautilus_instrument_id)
                account = self.node.cache.account_for_venue(
                    nautilus_instrument_id.venue
                )
                if instrument is None:
                    self._record_runtime_event(
                        "entry_sizing_unavailable",
                        activation_id=activation_id,
                        reason_code="INSTRUMENT_CACHE_MISSING",
                    )
                    return None
                if account is None:
                    self._record_runtime_event(
                        "entry_sizing_unavailable",
                        activation_id=activation_id,
                        reason_code="ACCOUNT_CACHE_MISSING",
                    )
                    return None
                try:
                    boundary = self.coordinator.get_entry_sizing_boundary(activation_id)
                    snapshot = build_live_entry_sizing_snapshot(
                        instrument_id=instrument_id,
                        direction=direction,
                        cutoff_ns=self.node.kernel.clock.timestamp_ns(),
                        tracker=used_tracker,
                        instrument=instrument,
                        account=account,
                        boundary=boundary,
                    )
                    self._record_runtime_event(
                        "entry_sizing_ready",
                        activation_id=activation_id,
                    )
                    return snapshot
                except ProductPreSubmitRejected as exc:
                    self._record_runtime_event(
                        "entry_sizing_unavailable",
                        activation_id=activation_id,
                        reason_code=exc.reason_code,
                    )
                    return None

            evaluator = NautilusBarEntryEvaluator(
                activation_id=activation.activation_id,
                instrument_ref=activation.instrument_ref,
                parameters=parameters,
                decision_not_before=version.valid_from,
                valid_until=entry_valid_until,
                sizing_provider=sizing_provider,
                requires_live_warmup=True,
            )

            normalizer = self.coordinator.build_nautilus_event_normalizer(
                leaves_quantity_for_client_order_id=lambda client_order_id: (
                    _cached_leaves_quantity(self.node.cache, client_order_id)
                ),
                filled_quantity_for_client_order_id=lambda client_order_id: (
                    _cached_filled_quantity(self.node.cache, client_order_id)
                ),
                order_quantity_for_client_order_id=lambda client_order_id: (
                    _cached_order_quantity(self.node.cache, client_order_id)
                ),
                query_was_recently_dispatched=self._query_was_recently_dispatched,
            )

            def event_sink(
                event: object,
                used_normalizer=normalizer,
                used_responsibility: ProductResponsibilityBoundary = responsibility,
                activation_id: str = activation.activation_id,
            ) -> None:
                client_order_id = getattr(event, "client_order_id", None)
                self._record_runtime_event(
                    "execution_event_observed",
                    activation_id=activation_id,
                    event_type=type(event).__name__,
                    venue_reason_code=_venue_event_reason_code(event),
                    client_order_id=(
                        str(client_order_id) if client_order_id is not None else None
                    ),
                )
                normalized = self.coordinator.handle_nautilus_order_event(
                    used_normalizer,
                    event,
                    observed_at=datetime.now(UTC),
                )
                used_responsibility.submit_event(normalized)

            def bar_event_sink(
                bar: object,
                *,
                activation_id: str = activation.activation_id,
            ) -> None:
                self._record_runtime_event(
                    "strategy_bar_observed",
                    activation_id=activation_id,
                    bar_type=str(getattr(bar, "bar_type", "UNKNOWN")),
                    ts_event=int(getattr(bar, "ts_event", 0)),
                )

            def bar_failure_sink(
                bar: object,
                exception: Exception,
                *,
                activation_id: str = activation.activation_id,
            ) -> None:
                self._handle_component_failure(
                    "strategy_bar_failed",
                    activation_id,
                    exception,
                    bar_type=str(getattr(bar, "bar_type", "UNKNOWN")),
                )

            def history_warmup_failure_sink(
                stage: str,
                item: object | None,
                exception: Exception,
                *,
                activation_id: str = activation.activation_id,
            ) -> None:
                self._handle_component_failure(
                    "strategy_history_warmup_failed",
                    activation_id,
                    exception,
                    stage=stage,
                    item_type=type(item).__name__ if item is not None else "NONE",
                    bar_type=str(getattr(item, "bar_type", "UNKNOWN")),
                )

            def adapter_factory(
                activation_id: str = activation.activation_id,
                instrument_ref: str = activation.instrument_ref,
                logic: OneShotDonchianAtrLogic = OneShotDonchianAtrLogic(parameters),
                used_state_provider=state_provider,
                used_proposal_sink=proposal_sink,
                used_event_sink=event_sink,
                used_evaluator: NautilusBarEntryEvaluator = evaluator,
                used_tracker: LiveEntryFactTracker = tracker,
            ) -> HalphaStrategyAdapter:
                def mark_price_sink(update: object) -> None:
                    used_tracker.record_mark(update)
                    self._record_market_mark_event(instrument_ref)

                return HalphaStrategyAdapter(
                    activation_id=activation_id,
                    logic=logic,
                    state_provider=used_state_provider,
                    proposal_sink=used_proposal_sink,
                    instrument_ref=instrument_ref,
                    persisted_action_capability=capability,
                    execution_event_sink=self._guard_component_sink(
                        "strategy_execution_event_processing_failed",
                        activation_id,
                        used_event_sink,
                    ),
                    bar_evaluator=used_evaluator,
                    bar_event_sink=bar_event_sink,
                    bar_failure_sink=bar_failure_sink,
                    history_warmup_failure_sink=history_warmup_failure_sink,
                    quote_event_sink=self._guard_component_sink(
                        "strategy_quote_processing_failed",
                        activation_id,
                        used_tracker.record_quote,
                    ),
                    mark_price_event_sink=self._guard_component_sink(
                        "strategy_mark_price_processing_failed",
                        activation_id,
                        mark_price_sink,
                    ),
                    live_history_warmup=True,
                )

            self._lifecycle.start(
                ActivationAdapterSpec(
                    activation_id=activation.activation_id,
                    factory=adapter_factory,
                )
            )
            self._record_runtime_event(
                "strategy_adapter_started",
                activation_id=activation.activation_id,
                entry_valid_until=entry_valid_until.isoformat(),
            )

    async def _wait_for_stop_and_sync_activations(
        self,
        stop_wait: Callable[[], object],
        capability: object,
        *,
        interval_seconds: float = 1.0,
        stop_future: asyncio.Future[object] | None = None,
    ) -> None:
        """Periodically advance time-based duties; Demo also discovers activations."""

        discover_activations = self._settings.release.profile == "BINANCE_DEMO"
        lifecycle = self._lifecycle
        if lifecycle is None:
            raise ExecutorRuntimeError("PRODUCT_RUNTIME_NOT_BUILT")

        if stop_future is None:
            stop_future = self._loop.run_in_executor(None, stop_wait)
            self._arm_maintenance_stop_latch(stop_future)
        while not stop_future.done():
            await asyncio.sleep(interval_seconds)
            if stop_future.done():
                break
            self._require_no_fatal_component_failure()
            self._require_execution_stream_recoverable()
            self._require_market_data_stream_recoverable()
            self._require_product_database_available()
            previous_ids = set(lifecycle.activation_ids)
            if discover_activations:
                self._restore_paused_adapters(capability)
            if getattr(self, "_coordinator", None) is not None:
                self._retry_startup_recovery(observed_at=datetime.now(UTC))
            for activation_id, processor in tuple(
                self._responsibility_processors.items()
            ):
                try:
                    await processor.sync(activation_id)
                except Exception as exc:
                    self._handle_component_failure(
                        "responsibility_sync_failed",
                        activation_id,
                        exc,
                    )
                    self._require_no_fatal_component_failure()
            for activation_id, processor in tuple(
                getattr(self, "_direct_schedule_processors", {}).items()
            ):
                processor.resume(activation_id)
            current_ids = set(lifecycle.activation_ids)
            if discover_activations and current_ids - previous_ids:
                await self._wait_for_strategy_history_warmup()
            self._require_no_fatal_component_failure()
        await stop_future

    def _guard_component_sink(
        self,
        failure_event: str,
        activation_id: str,
        sink: Callable[..., object],
    ) -> Callable[..., object]:
        """Latch any exception which an external framework callback may swallow."""

        def guarded(*args: object, **kwargs: object) -> object:
            try:
                return sink(*args, **kwargs)
            except Exception as exc:
                self._handle_component_failure(
                    failure_event,
                    activation_id,
                    exc,
                )
                raise

        return guarded

    def _handle_component_failure(
        self,
        event: str,
        activation_id: str,
        exception: BaseException,
        **context: object,
    ) -> None:
        """Retry expected fact rejection, but latch unexpected component failure."""

        unexpected = not isinstance(exception, ProductPreSubmitRejected)
        latched_now = False
        if unexpected and getattr(self, "_fatal_component_failure", None) is None:
            self._fatal_component_failure = (
                event,
                activation_id,
                type(exception).__name__,
            )
            latched_now = True
            coordinator = self.__dict__.get("_coordinator")
            if coordinator is not None:
                coordinator.disable_venue_mutations()
        failure_fields = dict(context)
        failure_fields.update(
            activation_id=activation_id,
            reason=type(exception).__name__,
            **_stable_failure_reason_code(exception),
        )
        self._record_runtime_event(event, **failure_fields)
        if not latched_now:
            return
        self._record_runtime_event(
            "runtime_component_failure_latched",
            component_event=event,
            activation_id=activation_id,
            reason=type(exception).__name__,
            **_stable_failure_reason_code(exception),
        )

    def _require_no_fatal_component_failure(self) -> None:
        if getattr(self, "_fatal_component_failure", None) is not None:
            raise ExecutorRuntimeError("RUNTIME_COMPONENT_FAILURE")

    def _require_execution_stream_recoverable(self) -> None:
        """Let Nautilus reconnect briefly, then restart a degraded process."""

        client = getattr(self, "_framework_execution_client", None)
        if client is None:
            return
        state = _binance_execution_stream_state(client)
        now = self._loop.time()
        unhealthy_since = getattr(
            self,
            "_execution_stream_unhealthy_since",
            None,
        )
        previous_state = getattr(
            self,
            "_execution_stream_last_state",
            "UNKNOWN",
        )
        self._execution_stream_last_state = state
        if state == "HEALTHY":
            if unhealthy_since is not None:
                self._record_runtime_event(
                    "execution_stream_recovered",
                    previous_state=previous_state,
                    recovery_seconds=max(0.0, now - unhealthy_since),
                )
            self._execution_stream_unhealthy_since = None
            return
        if unhealthy_since is None:
            unhealthy_since = now
            self._execution_stream_unhealthy_since = now
            self._record_runtime_event(
                "execution_stream_recovering",
                stream_state=state,
                recovery_timeout_seconds=(
                    _EXECUTION_STREAM_RECOVERY_TIMEOUT_SECONDS
                ),
            )
        elapsed = max(0.0, now - unhealthy_since)
        if (
            state
            not in {
                "RECOVERY_FAILED",
                "RECOVERY_BUFFER_LIMIT_EXCEEDED",
            }
            and elapsed < _EXECUTION_STREAM_RECOVERY_TIMEOUT_SECONDS
        ):
            return
        self._record_runtime_event(
            "execution_stream_recovery_failed",
            stream_state=state,
            recovery_seconds=elapsed,
        )
        raise ExecutorRuntimeError("BINANCE_EXECUTION_STREAM_RECOVERY_FAILED")

    def _required_market_data_streams(self) -> tuple[str, ...]:
        """Return every stream which current Halpha responsibilities require."""

        settings = getattr(self, "_settings", None)
        profile = getattr(getattr(settings, "release", None), "profile", None)
        if profile is None:
            return ()
        try:
            instrument_ids = _PROFILE_SPEC[profile][1]
        except KeyError:
            raise ExecutorRuntimeError("EXECUTION_PROFILE_MISMATCH") from None
        required: set[str] = set()
        for instrument_id in instrument_ids:
            symbol = instrument_id.removesuffix("-PERP.BINANCE").lower()
            if not symbol or symbol == instrument_id.lower():
                raise ExecutorRuntimeError("BINANCE_INSTRUMENT_ID_INVALID")
            required.add(f"{symbol}@bookTicker")
            required.add(f"{symbol}@markPrice@1s")
        for lifecycle in (
            getattr(self, "_lifecycle", None),
            getattr(self, "_market_fact_lifecycle", None),
        ):
            if lifecycle is None:
                continue
            for activation_id in tuple(lifecycle.activation_ids):
                adapter = lifecycle.adapter_for_activation(activation_id)
                evaluator = getattr(adapter, "_bar_evaluator", None)
                if evaluator is None:
                    continue
                for bar_type in tuple(evaluator.subscribed_bar_types):
                    required.add(_binance_bar_stream_name(bar_type))
        return tuple(sorted(required))

    def _record_market_mark_event(self, instrument_ref: str) -> None:
        observed = getattr(self, "_market_mark_event_at", None)
        if observed is None:
            observed = {}
            self._market_mark_event_at = observed
        loop = getattr(self, "_loop", None)
        observed[
            _binance_mark_price_stream_name(instrument_ref)
        ] = loop.time() if loop is not None else monotonic()

    def _require_market_data_stream_recoverable(self) -> None:
        """Let Nautilus reconnect public streams briefly, then restart safely."""

        client = getattr(self, "_framework_data_client", None)
        if client is None:
            return
        required_streams = self._required_market_data_streams()
        state = _binance_data_stream_state(
            client,
            required_streams=required_streams,
        )
        now = self._loop.time()
        observed_mark_events = getattr(self, "_market_mark_event_at", None)
        if state == "HEALTHY" and observed_mark_events is not None:
            state = _required_mark_price_event_state(
                required_streams,
                observed_mark_events,
                now=now,
            )
        unhealthy_since = getattr(
            self,
            "_market_data_stream_unhealthy_since",
            None,
        )
        previous_state = getattr(
            self,
            "_market_data_stream_last_state",
            "UNKNOWN",
        )
        self._market_data_stream_last_state = state
        if state == "HEALTHY":
            if unhealthy_since is not None:
                self._record_runtime_event(
                    "market_data_stream_recovered",
                    previous_state=previous_state,
                    recovery_seconds=max(0.0, now - unhealthy_since),
                )
            self._market_data_stream_unhealthy_since = None
            return
        if unhealthy_since is None:
            unhealthy_since = now
            self._market_data_stream_unhealthy_since = now
            self._record_runtime_event(
                "market_data_stream_recovering",
                stream_state=state,
                recovery_timeout_seconds=(
                    _MARKET_DATA_STREAM_RECOVERY_TIMEOUT_SECONDS
                ),
            )
        elapsed = max(0.0, now - unhealthy_since)
        if elapsed < _MARKET_DATA_STREAM_RECOVERY_TIMEOUT_SECONDS:
            return
        self._record_runtime_event(
            "market_data_stream_recovery_failed",
            stream_state=state,
            recovery_seconds=elapsed,
        )
        raise ExecutorRuntimeError("BINANCE_MARKET_DATA_STREAM_RECOVERY_FAILED")

    async def _resume_startup_processors(
        self,
        processors: dict[str, object],
        *,
        failure_event: str,
    ) -> None:
        """Resume every activation without letting one stale snapshot stop the runtime."""

        resumed: list[tuple[str, object]] = []
        for activation_id, processor in processors.items():
            try:
                processor.resume(activation_id)
            except Exception as exc:
                self._handle_component_failure(
                    failure_event,
                    activation_id,
                    exc,
                )
                continue
            resumed.append((activation_id, processor))
        for activation_id, processor in resumed:
            try:
                await processor.wait_idle()
            except Exception as exc:
                # Expected stale or rate-limited facts remain deferred. An
                # unexpected invariant or persistence failure is latched so
                # the process cannot advertise readiness while duties stopped.
                self._handle_component_failure(
                    failure_event,
                    activation_id,
                    exc,
                )

    async def _wait_for_stop_and_monitor_market_data(
        self,
        stop_wait: Callable[[], object],
        *,
        interval_seconds: float = 1.0,
    ) -> None:
        """Keep public observation restartable when its market stream stalls."""

        stop_future = self._loop.run_in_executor(None, stop_wait)
        while not stop_future.done():
            self._require_no_fatal_component_failure()
            try:
                await asyncio.wait_for(
                    asyncio.shield(stop_future),
                    timeout=interval_seconds,
                )
                break
            except TimeoutError:
                self._require_market_data_stream_recoverable()
                self._require_no_fatal_component_failure()
        await stop_future

    async def _observe_account_until_stop(
        self,
        stop_wait: Callable[[], object],
        *,
        interval_seconds: float = 30.0,
        stop_future: asyncio.Future[object] | None = None,
    ) -> None:
        """Refresh complete account facts without creating venue authority."""

        observer = self._account_observer
        if observer is None:
            raise ExecutorRuntimeError("ACCOUNT_OBSERVER_NOT_BUILT")
        if stop_future is None:
            stop_future = self._loop.run_in_executor(None, stop_wait)
        next_interval_seconds = interval_seconds
        while not stop_future.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(stop_future),
                    timeout=next_interval_seconds,
                )
                break
            except TimeoutError:
                pass
            self._require_product_database_available()
            try:
                fact = await observer.observe()
            except AccountObservationError as exc:
                if not exc.retryable:
                    self._handle_component_failure(
                        "account_snapshot_refresh_failed",
                        "account-observer",
                        exc,
                    )
                    self._require_no_fatal_component_failure()
                next_interval_seconds = max(
                    interval_seconds,
                    exc.retry_after_seconds or 0.0,
                )
                self._record_runtime_event(
                    "account_snapshot_refresh_failed",
                    reason_code=exc.reason_code,
                    retry_after_seconds=next_interval_seconds,
                )
            except Exception as exc:
                self._handle_component_failure(
                    "account_snapshot_refresh_failed",
                    "account-observer",
                    exc,
                )
                self._require_no_fatal_component_failure()
            else:
                next_interval_seconds = interval_seconds
                self._record_runtime_event(
                    "account_snapshot_refreshed",
                    venue_fact_id=fact.venue_fact_id,
                    cutoff=fact.cutoff.isoformat(),
                    open_position_count=fact.payload.get(
                        "open_position_count",
                        0,
                    ),
                )
        await stop_future

    async def _refresh_live_account_qualification_until_stop(
        self,
        stop_future: asyncio.Future[object],
        *,
        interval_seconds: float = 30.0,
    ) -> None:
        """Refresh Live account-type evidence off the trading event loop."""

        refresh = self._live_write_account_qualification_refresh
        if refresh is None:
            return
        while not stop_future.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(stop_future),
                    timeout=interval_seconds,
                )
                break
            except TimeoutError:
                pass
            try:
                await self._loop.run_in_executor(None, refresh)
            except Exception as exc:
                self._record_runtime_event(
                    "live_account_qualification_refresh_failed",
                    reason_code=getattr(
                        exc,
                        "reason_code",
                        type(exc).__name__.upper(),
                    ),
                    retry_after_seconds=getattr(
                        exc,
                        "retry_after_seconds",
                        None,
                    ),
                )

    async def _startup_and_stop(
        self,
        stop_wait: Callable[[], object],
        on_ready: Callable[[dict[str, object]], None] | None,
    ) -> None:
        for _ in range(3000):
            if self.node.is_running() and self.node.trader.is_running:
                break
            await asyncio.sleep(0.01)
        else:
            try:
                await self.node.stop_async()
            finally:
                raise ExecutorRuntimeError("TRADING_NODE_START_TIMEOUT")
        read_only = self._settings.release.profile == "BINANCE_LIVE_READ_ONLY"
        if read_only:
            private_account_observation = self._account_observer is not None
            try:
                if private_account_observation:
                    try:
                        initial_fact = await self._account_observer.observe()
                    except AccountObservationError as exc:
                        self._record_runtime_event(
                            "account_snapshot_startup_failed",
                            reason_code=str(exc),
                        )
                        raise ExecutorRuntimeError(
                            "ACCOUNT_OBSERVATION_STARTUP_FAILED"
                        ) from None
                    self._record_runtime_event(
                        "account_snapshot_started",
                        venue_fact_id=initial_fact.venue_fact_id,
                        cutoff=initial_fact.cutoff.isoformat(),
                        open_position_count=initial_fact.payload.get(
                            "open_position_count",
                            0,
                        ),
                    )
                else:
                    self._start_read_only_adapter()
                    await self._wait_for_strategy_history_warmup()
                if on_ready is not None:
                    on_ready(
                        {
                            "product_runtime_started": True,
                            "profile": "BINANCE_LIVE_READ_ONLY",
                            "read_only_mode": (
                                "PRIVATE_ACCOUNT_OBSERVATION"
                                if private_account_observation
                                else "PUBLIC_FORWARD_OBSERVATION"
                            ),
                            "strategy_adapter_started": (
                                not private_account_observation
                            ),
                            "account_observer_started": private_account_observation,
                            "data_client_loaded": True,
                            "binance_credentials_loaded": (
                                private_account_observation
                            ),
                            "instrument_commission_query_enabled": False,
                            "execution_client_loaded": False,
                            "database_connection_loaded": (
                                private_account_observation
                            ),
                            "execution_action_repository_loaded": False,
                            "persisted_action_capability_loaded": False,
                            "venue_fact_append_capability_loaded": (
                                private_account_observation
                            ),
                            "startup_execution_reconciliation": "NOT_APPLICABLE",
                            "runtime_real_write_gate": self._runtime_real_write_gate,
                            "live_write_risk_control_only": False,
                        }
                    )
                if private_account_observation:
                    await self._observe_account_until_stop(stop_wait)
                else:
                    await self._wait_for_stop_and_monitor_market_data(stop_wait)
            finally:
                if self._lifecycle is not None:
                    self._lifecycle.stop_all()
                await self.node.stop_async()
            return
        if self._capability is None:
            raise ExecutorRuntimeError("PRODUCT_RUNTIME_NOT_BUILT")
        stop_future = self._loop.run_in_executor(None, stop_wait)
        self._arm_maintenance_stop_latch(stop_future)
        qualification_refresh_task = (
            self._loop.create_task(
                self._refresh_live_account_qualification_until_stop(
                    stop_future
                )
            )
            if self._live_write_account_qualification_refresh is not None
            else None
        )
        account_observation_task = (
            self._loop.create_task(
                self._observe_account_until_stop(
                    stop_wait,
                    stop_future=stop_future,
                )
            )
            if self._account_observer is not None
            else None
        )
        try:
            self._start_market_fact_streams()
            self._restore_paused_adapters(self._capability)
            if not self._startup_recovery_prepared:
                raise ExecutorRuntimeError("STARTUP_RECOVERY_NOT_PREPARED")
            queried_recovery_ids = (
                self.coordinator.query_prepared_startup_recovery(
                    observed_at=datetime.now(UTC),
                )
            )
            self._recovery_complete = (
                self.coordinator.startup_recovery_complete()
            )
            if queried_recovery_ids:
                self._record_runtime_event(
                    "startup_recovery_queries_dispatched",
                    execution_action_ids=queried_recovery_ids,
                    pending_action_count=len(
                        self.coordinator.startup_recovery_pending_action_ids()
                    ),
                )
            pending_recovery_ids = (
                self.coordinator.startup_recovery_pending_action_ids()
            )
            if pending_recovery_ids:
                self._record_runtime_event(
                    "startup_recovery_pending",
                    execution_action_ids=pending_recovery_ids,
                    pending_action_count=len(pending_recovery_ids),
                )
            await self._resume_startup_processors(
                self._responsibility_processors,
                failure_event="startup_responsibility_deferred",
            )
            await self._resume_startup_processors(
                getattr(self, "_direct_schedule_processors", {}),
                failure_event="startup_direct_schedule_deferred",
            )
            self._require_no_fatal_component_failure()
            await self._wait_for_strategy_history_warmup()
            self._require_product_database_available()
            self._record_executor_runtime_reattached()
            if on_ready is not None:
                on_ready(self._runtime_ready_evidence())
                self._runtime_ready_reported = True
            await self._wait_for_stop_and_sync_activations(
                stop_wait,
                self._capability,
                stop_future=stop_future,
            )
        finally:
            background_tasks = tuple(
                task
                for task in (
                    qualification_refresh_task,
                    account_observation_task,
                )
                if task is not None
            )
            for task in background_tasks:
                if not task.done():
                    task.cancel()
            if background_tasks:
                await asyncio.gather(
                    *background_tasks,
                    return_exceptions=True,
                )
            self.coordinator.disable_venue_mutations()
            if self._lifecycle is not None:
                self._lifecycle.stop_all()
            if self._market_fact_lifecycle is not None:
                self._market_fact_lifecycle.stop_all()
            await self.node.stop_async()
            try:
                self._remove_external_order_event_bridge()
            except Exception as exc:
                self._record_cleanup_failure(
                    "external_order_event_bridge",
                    exc,
                )

    def _record_executor_runtime_reattached(self) -> None:
        lifecycle = self._lifecycle
        if lifecycle is None:
            return
        observed_at = datetime.now(UTC)
        for activation_id in lifecycle.activation_ids:
            self.coordinator.record_executor_runtime_reattached(
                activation_id=activation_id,
                observed_at=observed_at,
            )

    def run_until_stop(
        self,
        stop_wait: Callable[[], object],
        *,
        on_ready: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        if self._node is None:
            raise ExecutorRuntimeError("PRODUCT_RUNTIME_NOT_BUILT")
        asyncio.set_event_loop(self._loop)
        write_runtime = (
            self._settings.release.profile != "BINANCE_LIVE_READ_ONLY"
        )
        if write_runtime:
            try:
                self._prepare_startup_recovery_before_node_run(
                    observed_at=datetime.now(UTC),
                )
                self._install_external_order_event_bridge()
            except Exception:
                self.coordinator.disable_venue_mutations()
                raise
        self._runtime_ready_sink = on_ready
        self._runtime_ready_reported = False
        task = self._loop.create_task(self._startup_and_stop(stop_wait, on_ready))
        try:
            self._start_runtime_heartbeat()
            self._node.run(raise_exception=True)
            task.result()
        finally:
            self._stop_runtime_heartbeat()
            self._runtime_ready_sink = None
            self._runtime_ready_reported = False
            if write_runtime:
                self.coordinator.disable_venue_mutations()
            if not task.done():
                task.cancel()

    def close(self) -> None:
        self._stop_runtime_heartbeat()
        for activation_id, processor in self._proposal_processors.items():
            try:
                processor.close()
            except Exception as exc:
                self._record_cleanup_failure(
                    f"proposal_processor:{activation_id}",
                    exc,
                )
        self._proposal_processors.clear()
        for activation_id, processor in getattr(
            self,
            "_direct_schedule_processors",
            {},
        ).items():
            try:
                processor.close()
            except Exception as exc:
                self._record_cleanup_failure(
                    f"direct_schedule_processor:{activation_id}",
                    exc,
                )
        if hasattr(self, "_direct_schedule_processors"):
            self._direct_schedule_processors.clear()
        if hasattr(self, "_direct_schedule_instruments"):
            self._direct_schedule_instruments.clear()
        for activation_id, processor in self._responsibility_processors.items():
            try:
                processor.close()
            except Exception as exc:
                self._record_cleanup_failure(
                    f"responsibility_processor:{activation_id}",
                    exc,
                )
        self._responsibility_processors.clear()
        if self._lifecycle is not None:
            try:
                self._lifecycle.stop_all()
            except Exception as exc:
                self._record_cleanup_failure("activation_lifecycle", exc)
        market_fact_lifecycle = getattr(self, "_market_fact_lifecycle", None)
        if market_fact_lifecycle is not None:
            try:
                market_fact_lifecycle.stop_all()
            except Exception as exc:
                self._record_cleanup_failure("market_fact_lifecycle", exc)
        if self._node is not None:
            try:
                if self._node.is_running():
                    self._node.stop()
            except Exception as exc:
                self._record_cleanup_failure("trading_node_stop", exc)
            try:
                self._remove_external_order_event_bridge()
            except Exception as exc:
                self._record_cleanup_failure(
                    "external_order_event_bridge",
                    exc,
                )
            try:
                self._node.dispose()
            except Exception as exc:
                self._record_cleanup_failure("trading_node_dispose", exc)
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception as exc:
                self._record_cleanup_failure("database_connection", exc)
        if self._owns_loop and not self._loop.is_closed():
            try:
                self._loop.close()
            except Exception as exc:
                self._record_cleanup_failure("event_loop", exc)
        self._lifecycle = None
        self._market_fact_lifecycle = None
        getattr(self, "_market_fact_trackers", {}).clear()
        self._coordinator = None
        self._capability = None
        self._account_observer = None
        self._pre_submit_fact_provider = None
        self._venue_execution_client = None
        self._framework_data_client = None
        self._framework_execution_client = None
        getattr(self, "_market_mark_event_at", {}).clear()
        self._market_data_stream_unhealthy_since = None
        self._market_data_stream_last_state = "UNKNOWN"
        self._execution_stream_unhealthy_since = None
        self._execution_stream_last_state = "UNKNOWN"
        self._fatal_component_failure = None
        self._node = None
        self._connection = None
        self._startup_recovery_prepared = False
        self._startup_recovered_actions = ()
        self._external_order_event_handler = None
        self._runtime_ready_sink = None
        self._runtime_ready_reported = False
        self._runtime_heartbeat_sink = None

    def _record_cleanup_failure(
        self,
        component: str,
        exception: BaseException,
    ) -> None:
        try:
            self._record_runtime_event(
                "runtime_cleanup_failed",
                component=component,
                reason=type(exception).__name__,
            )
        except Exception as sink_exception:
            self._loop.call_exception_handler(
                {
                    "message": "HALPHA_RUNTIME_CLEANUP_EVENT_FAILED",
                    "exception": sink_exception,
                }
            )
