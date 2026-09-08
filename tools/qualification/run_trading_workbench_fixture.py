"""Serve deterministic workbench states without exchange network access."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import re
import sys
from threading import Timer
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import keyring
import psycopg
from psycopg import sql
from pydantic import SecretStr
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, create_engine
from sqlalchemy.pool import NullPool
import uvicorn

from halpha.app.projection import PostgreSQLWorkbenchProjection
from halpha.app.secrets import AppSecrets
from halpha.app.web import create_app
from halpha.capital.models import (
    ActionCheckInput,
    AuthorityClass,
    CapDecision,
    EnvironmentKind,
    RiskClass,
    StopCategory,
)
from halpha.configuration import load_settings
from halpha.domain_values import content_digest
from halpha.executor.coordinator import HalphaCoordinator
from halpha.planning.strategies.one_shot import (
    EntryRiskContext,
    RiskDirection,
    StrategyProposal,
)
from halpha.planning.models import ProposedAction, ProposedActionKind
from halpha.planning.registry import Direction
from halpha.planning.order_schedule import InstrumentOrderRules
from halpha.public_instrument_rules import InstrumentRulesUnavailable
from halpha.public_market import (
    MARKET_INTERVAL_MILLISECONDS,
    MarketBar,
    MarketContext,
    MarketContextUnavailable,
    MarketInterval,
    MarketWindow,
)
from halpha.public_market_stream import (
    MarketStreamBar,
    MarketStreamQuote,
    MarketStreamStatus,
)
from halpha.scalping.models import BinanceContract, BinanceContractCatalog
from halpha.venue_integration.gateway import PersistedActionGate
from halpha.venue_integration.facts import build_venue_fact
from halpha.venue_integration.models import VenueFactKind, VenueFactSourceClass
from halpha.venue_integration.repository import (
    PostgreSQLExecutionActionRepository,
    PostgreSQLVenueFactRepository,
)
from halpha.venue_integration.service import ExecutionApplicationService
from halpha.winvault import require_win_vault_backend
from tools.qualification.database_fixture import (
    NoExchangeClient,
    connect_app,
    connect_executor,
    create_and_activate,
    database_secret,
)
from tools.provisioning.provision_halpha_databases import SUPERUSER_REFERENCE


FIXTURE_ENVIRONMENT_ID = "trading-workbench-fixture"
FIXTURE_ACCOUNT_ID = "trading-workbench-account"
FIXTURE_INSTRUMENT_REFS = frozenset(
    {
        "ADAUSDT-PERP",
        "BNBUSDT-PERP",
        "BTCUSDT-PERP",
        "ETHUSDT-PERP",
        "SOLUSDT-PERP",
        "XRPUSDT-PERP",
    }
)
FIXTURE_DATABASE_PATTERN = re.compile(
    r"^halpha_workbench_fixture_[1-9][0-9]*$"
)


class FixtureMarketContextProvider:
    """Return deterministic public facts without reaching an exchange endpoint."""

    async def fetch(
        self,
        instrument_ref: str,
        lookback: int,
        stop_reference_interval: MarketInterval = "15m",
    ) -> MarketContext:
        if instrument_ref not in FIXTURE_INSTRUMENT_REFS:
            raise MarketContextUnavailable("MARKET_CONTEXT_INSTRUMENT_UNSUPPORTED")
        now = datetime.now(UTC)
        latest_closed_stop_reference_at = now - timedelta(
            milliseconds=MARKET_INTERVAL_MILLISECONDS[stop_reference_interval]
        )
        return MarketContext(
            instrument_ref=instrument_ref,
            source="BINANCE_DEMO_PUBLIC",
            source_cutoff=now,
            latest_closed_1m_at=now - timedelta(minutes=1),
            latest_closed_15m_at=now - timedelta(minutes=15),
            latest_closed_stop_reference_at=latest_closed_stop_reference_at,
            channel_lookback_15m=lookback,
            stop_reference_interval=stop_reference_interval,
            bid_price="100",
            ask_price="100.2",
            reference_price="100.1",
            latest_close_1m="100.1",
            latest_volume_1m="1000",
            latest_trade_count_1m=100,
            latest_close_15m="100",
            channel_upper="101",
            channel_lower="99",
            atr_14="2",
            stop_reference_atr_14="2",
            long_breakout_gap_pct="0.8991",
            short_breakout_gap_pct="1.0989",
        )

    async def fetch_window(
        self,
        instrument_ref: str,
        interval: MarketInterval,
        start_at: datetime,
        end_at: datetime,
    ) -> MarketWindow:
        if instrument_ref not in FIXTURE_INSTRUMENT_REFS:
            raise MarketContextUnavailable("MARKET_CONTEXT_INSTRUMENT_UNSUPPORTED")
        interval_milliseconds = MARKET_INTERVAL_MILLISECONDS[interval]
        step = timedelta(milliseconds=interval_milliseconds)
        start_milliseconds = int(start_at.timestamp() * 1000)
        aligned_start_milliseconds = (
            start_milliseconds // interval_milliseconds
        ) * interval_milliseconds
        cursor = datetime.fromtimestamp(
            aligned_start_milliseconds / 1000,
            UTC,
        )
        bars: list[MarketBar] = []
        index = 0
        while cursor <= end_at and len(bars) < 300:
            base = 100 + (index % 12) * 0.18
            close = base + (0.12 if index % 3 else -0.08)
            bars.append(
                MarketBar(
                    open_at=cursor,
                    close_at=cursor + step,
                    open=str(base),
                    high=str(max(base, close) + 0.22),
                    low=str(min(base, close) - 0.2),
                    close=str(close),
                    volume=str(900 + index * 7),
                )
            )
            cursor += step
            index += 1
        return MarketWindow(
            instrument_ref=instrument_ref,
            interval=interval,
            source="BINANCE_DEMO_PUBLIC",
            source_cutoff=bars[-1].close_at,
            bars=tuple(bars),
        )


class FixtureInstrumentRulesProvider:
    """Return stable Demo rules without reaching an exchange endpoint."""

    async def list_contracts(self) -> BinanceContractCatalog:
        return BinanceContractCatalog(
            source="BINANCE_DEMO_EXCHANGE_INFO",
            source_cutoff=datetime.now(UTC),
            contracts=tuple(
                BinanceContract(
                    instrument_ref=instrument_ref,
                    symbol=instrument_ref.removesuffix("-PERP"),
                    base_asset=instrument_ref.removesuffix("USDT-PERP"),
                )
                for instrument_ref in sorted(FIXTURE_INSTRUMENT_REFS)
            ),
        )

    async def fetch(self, instrument_ref: str) -> InstrumentOrderRules:
        if instrument_ref not in FIXTURE_INSTRUMENT_REFS:
            raise InstrumentRulesUnavailable(
                "INSTRUMENT_RULES_INSTRUMENT_UNSUPPORTED"
            )
        return InstrumentOrderRules(
            source="BINANCE_DEMO_EXCHANGE_INFO",
            min_price="0.1",
            max_price="1000000",
            price_tick_size="0.1",
            limit_quantity_step="0.001",
            min_limit_quantity="0.001",
            max_limit_quantity="1000",
            market_quantity_step="0.001",
            min_market_quantity="0.001",
            max_market_quantity="1000",
            min_notional="5",
            source_cutoff=datetime.now(UTC).isoformat(),
        )


class FixtureMarketStreamProvider:
    """Keep deterministic Demo quotes and bars live for browser qualification."""

    def __init__(self) -> None:
        self._closed = False

    async def stream(self, instrument_ref: str):
        if instrument_ref not in FIXTURE_INSTRUMENT_REFS:
            raise MarketContextUnavailable("MARKET_CONTEXT_INSTRUMENT_UNSUPPORTED")
        source = "BINANCE_DEMO_PUBLIC"
        yield MarketStreamStatus(
            state="LIVE",
            source=source,
            observed_at=datetime.now(UTC),
        )
        while not self._closed:
            now = datetime.now(UTC)
            yield MarketStreamQuote(
                instrument_ref=instrument_ref,
                source=source,
                source_cutoff=now,
                received_at=now,
                bid_price="100",
                ask_price="100.2",
                reference_price="100.1",
            )
            for interval, interval_milliseconds in (
                MARKET_INTERVAL_MILLISECONDS.items()
            ):
                now_milliseconds = int(now.timestamp() * 1000)
                open_milliseconds = (
                    now_milliseconds // interval_milliseconds
                ) * interval_milliseconds
                open_at = datetime.fromtimestamp(
                    open_milliseconds / 1000,
                    UTC,
                )
                yield MarketStreamBar(
                    instrument_ref=instrument_ref,
                    interval=interval,
                    source=source,
                    source_cutoff=now,
                    received_at=now,
                    closed=False,
                    bar=MarketBar(
                        open_at=open_at,
                        close_at=open_at + timedelta(
                            milliseconds=interval_milliseconds,
                        ),
                        open="100",
                        high="100.3",
                        low="99.9",
                        close="100.1",
                        volume="1000",
                    ),
                )
            await asyncio.sleep(1)

    async def close(self) -> None:
        self._closed = True


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve deterministic workbench states without exchange-changing requests."
    )
    parser.add_argument(
        "--port",
        type=int,
        help="Override the configured local bind port for an isolated qualification run.",
    )
    parser.add_argument(
        "--max-runtime-seconds",
        type=int,
        help="Gracefully stop and clean the fixture after this many seconds.",
    )
    parser.add_argument(
        "--static-dist",
        type=Path,
        default=ROOT / "frontend" / "dist",
        help="Serve an isolated frontend build without replacing the product assets.",
    )
    return parser.parse_args()


def _fixture_database_name(process_id: int | None = None) -> str:
    return f"halpha_workbench_fixture_{process_id or os.getpid()}"


def _superuser_password() -> str:
    backend = keyring.get_keyring()
    require_win_vault_backend(backend)
    password = backend.get_password(*SUPERUSER_REFERENCE)
    if not password:
        raise RuntimeError("POSTGRESQL_SUPERUSER_REFERENCE_MISSING")
    return password


def _create_fixture_database(database_name: str) -> None:
    if FIXTURE_DATABASE_PATTERN.fullmatch(database_name) is None:
        raise RuntimeError("WORKBENCH_FIXTURE_DATABASE_NAME_INVALID")
    password = _superuser_password()
    created = False
    try:
        with psycopg.connect(
            host="127.0.0.1",
            port=5432,
            dbname="postgres",
            user="postgres",
            password=password,
            autocommit=True,
        ) as admin:
            exists = admin.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (database_name,),
            ).fetchone()
            if exists is not None:
                raise RuntimeError("WORKBENCH_FIXTURE_DATABASE_ALREADY_EXISTS")
            admin.execute(
                sql.SQL(
                    "CREATE DATABASE {} OWNER halpha_demo_migration "
                    "TEMPLATE template0 ENCODING 'UTF8' "
                    "LC_COLLATE 'C' LC_CTYPE 'C'"
                ).format(sql.Identifier(database_name))
            )
            created = True
            admin.execute(
                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(database_name)
                )
            )

        _migrate_fixture_database(database_name)

        with psycopg.connect(
            host="127.0.0.1",
            port=5432,
            dbname="postgres",
            user="postgres",
            password=password,
            autocommit=True,
        ) as admin:
            admin.execute(
                sql.SQL(
                    "GRANT CONNECT ON DATABASE {} "
                    "TO halpha_demo_app, halpha_demo_executor"
                ).format(sql.Identifier(database_name))
            )
    except Exception:
        if created:
            with psycopg.connect(
                host="127.0.0.1",
                port=5432,
                dbname="postgres",
                user="postgres",
                password=password,
                autocommit=True,
            ) as admin:
                admin.execute(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (database_name,),
                )
                admin.execute(
                    sql.SQL("DROP DATABASE {}").format(
                        sql.Identifier(database_name)
                    )
                )
        raise
    finally:
        password = ""


def _migrate_fixture_database(database_name: str) -> None:
    if FIXTURE_DATABASE_PATTERN.fullmatch(database_name) is None:
        raise RuntimeError("WORKBENCH_FIXTURE_DATABASE_NAME_INVALID")
    secret = database_secret("Migration")
    engine = create_engine(
        URL.create(
            "postgresql+psycopg",
            username="halpha_demo_migration",
            password=secret,
            host="127.0.0.1",
            port=5432,
            database=database_name,
        ),
        poolclass=NullPool,
        echo=False,
    )
    try:
        with engine.connect() as connection:
            config = Config(str(ROOT / "alembic.ini"))
            config.attributes["connection"] = connection
            config.attributes["schema_bootstrap_allowed"] = True
            command.upgrade(config, "head")
    finally:
        secret = ""
        engine.dispose()


def _drop_fixture_database(database_name: str) -> None:
    if FIXTURE_DATABASE_PATTERN.fullmatch(database_name) is None:
        raise RuntimeError("WORKBENCH_FIXTURE_DATABASE_NAME_INVALID")
    password = _superuser_password()
    try:
        with psycopg.connect(
            host="127.0.0.1",
            port=5432,
            dbname="postgres",
            user="postgres",
            password=password,
            autocommit=True,
        ) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) "
                "FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(database_name)
                )
            )
    finally:
        password = ""


def _accepted_decision() -> CapDecision:
    fields = {
        "accepted": True,
        "reason_code": "ACCEPTED_RISK_INCREASING",
        "risk_class": RiskClass.RISK_INCREASING,
        "effective_leverage": "5",
        "action_notional": "500",
        "economic_action_notional": "500",
        "activation_notional_after": "500",
        "account_notional_after": "500",
        "activation_margin_after": "100",
        "stopped_categories": (),
        "input_digest": "c" * 64,
    }
    return CapDecision(**fields, decision_digest=content_digest(fields))


def _create_unknown_entry(
    executor_connection: object,
    *,
    activation_id: str,
    observed_at: datetime,
) -> None:
    action_repository = PostgreSQLExecutionActionRepository(
        executor_connection,
        FIXTURE_ENVIRONMENT_ID,
    )
    coordinator = HalphaCoordinator(
        executor_connection,
        PersistedActionGate(
            action_repository,
            NoExchangeClient(),
            environment_id=FIXTURE_ENVIRONMENT_ID,
            execution_profile_ref="BINANCE_DEMO",
            account_ref=FIXTURE_ACCOUNT_ID,
        ),
        environment_id=FIXTURE_ENVIRONMENT_ID,
        environment_kind="DEMO",
        authority_class="DEMO_VALIDATION",
        execution_profile_ref="BINANCE_DEMO",
        account_ref=FIXTURE_ACCOUNT_ID,
        runtime_real_write_gate="CLOSED",
    )
    source_ns = int(observed_at.timestamp() * 1_000_000_000)
    proposal_fields = {
        "strategy_id": "ONE_SHOT_DONCHIAN_ATR_BREAKOUT",
        "activation_id": activation_id,
        "rule_id": "ENTRY_BREAKOUT",
        "source_identity": f"{activation_id}:FIXTURE:ENTRY:{source_ns}",
        "source_cutoff": observed_at,
        "input_digest": "7" * 64,
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "direction": "LONG",
        "action_profile": "ENTRY_MARKET",
        "risk_direction": RiskDirection.INCREASE,
        "quantity": "0.01",
        "reference_price": "50000",
        "reference_source": "TRADING_WORKBENCH_FIXTURE",
        "reason_code": "ENTRY_BREAKOUT_CONFIRMED",
        "valid_until": observed_at + timedelta(minutes=5),
        "entry_risk_context": EntryRiskContext(
            trigger_atr="500",
            initial_stop_atr_multiple="1.5",
            take_profit_1_r="1.5",
            take_profit_1_fraction="0.5",
            take_profit_2_r="3",
            max_hold_bars_15m=96,
            indicator_source_digest="8" * 64,
            indicator_source_cutoff_ns=source_ns,
            quantity_step="0.001",
            price_tick_size="0.1",
            entry_extension_boundary="51000",
            sizing_taker_fee_rate="0.0006",
            sizing_effective_leverage="5",
            instrument_rules_digest="9" * 64,
        ).model_dump(mode="python", exclude_none=True),
    }
    proposal = StrategyProposal(
        **proposal_fields,
        proposal_digest=content_digest(proposal_fields),
    )
    action_check = ActionCheckInput(
        environment_id=FIXTURE_ENVIRONMENT_ID,
        environment_kind=EnvironmentKind.DEMO,
        authority_class=AuthorityClass.DEMO_VALIDATION,
        activation_id=activation_id,
        account_ref=FIXTURE_ACCOUNT_ID,
        instrument_ref="BTCUSDT-PERP",
        action_profile="ENTRY_MARKET",
        control_category=StopCategory.NEW_RISK,
        risk_class=RiskClass.RISK_INCREASING,
        checked_at=observed_at,
        quantized_quantity="0.01",
        conservative_price="50000",
        account_dynamic_available_margin="500",
        actual_margin_mode="ISOLATED",
        actual_leverage="20",
        post_action_abs_position="0.01",
        current_abs_position="0",
    )
    result = coordinator.consume_strategy_proposal(
        plan_event_id=str(uuid4()),
        execution_action_id=str(uuid4()),
        proposal=proposal,
        action_check=action_check,
        created_at=observed_at,
        client_order_id=uuid4().hex,
    )
    if result.execution_action is None:
        raise RuntimeError(
            "WORKBENCH_UNKNOWN_ACTION_NOT_CREATED "
            f"reason={result.plan_event.capital_decision.get('reason_code')}"
        )
    execution = ExecutionApplicationService(
        action_repository,
        PostgreSQLVenueFactRepository(
            executor_connection,
            FIXTURE_ENVIRONMENT_ID,
        ),
        environment_id=FIXTURE_ENVIRONMENT_ID,
        environment_kind="DEMO",
        authority_class="DEMO_VALIDATION",
        execution_profile_ref="BINANCE_DEMO",
        account_ref=FIXTURE_ACCOUNT_ID,
    )
    execution.prepare_submission(
        result.execution_action.execution_action_id,
        capital_decision=_accepted_decision(),
        request_payload={"profile": "ENTRY_MARKET", "quantity": "0.01"},
        observed_at=observed_at + timedelta(seconds=1),
    )
    execution.record_submission_unknown(
        result.execution_action.execution_action_id,
        reason="WORKBENCH_UNKNOWN_RESULT",
        next_query_at=observed_at + timedelta(seconds=20),
        observed_at=observed_at + timedelta(seconds=2),
    )


def _seed_completed_fee_evidence(
    executor_connection: object,
    *,
    coordinator: HalphaCoordinator,
    activation_id: str,
    observed_at: datetime,
) -> tuple[str, ...]:
    """Create one closed attributed round trip for the fee-evidence projection."""

    action_repository = PostgreSQLExecutionActionRepository(
        executor_connection,
        FIXTURE_ENVIRONMENT_ID,
    )
    execution = ExecutionApplicationService(
        action_repository,
        PostgreSQLVenueFactRepository(
            executor_connection,
            FIXTURE_ENVIRONMENT_ID,
        ),
        environment_id=FIXTURE_ENVIRONMENT_ID,
        environment_kind="DEMO",
        authority_class="DEMO_VALIDATION",
        execution_profile_ref="BINANCE_DEMO",
        account_ref=FIXTURE_ACCOUNT_ID,
    )
    fact_refs: list[str] = []

    def close_filled_action(
        *,
        action_kind: ProposedActionKind,
        action_profile: str,
        risk_class: RiskClass,
        control_category: StopCategory,
        order_side: str,
        liquidity_side: str,
        price: str,
        fee: str,
        source_suffix: str,
        offset_seconds: int,
    ) -> None:
        action_time = observed_at + timedelta(seconds=offset_seconds)
        client_order_id = uuid4().hex
        execution_context = (
            {
                "exit_responsibility_role": "PRIMARY_EXIT",
                "position_side": "BOTH",
            }
            if action_kind is ProposedActionKind.EXIT
            else {}
        )
        proposed = ProposedAction(
            environment_id=FIXTURE_ENVIRONMENT_ID,
            action_kind=action_kind,
            action_profile=action_profile,
            instrument_ref="BTCUSDT-PERP",
            direction=Direction.LONG,
            quantity="1",
            close_position=False,
            order_type="MARKET",
            valid_until=action_time + timedelta(minutes=5),
            reduce_only=action_kind is not ProposedActionKind.ENTRY,
            source_responsibility=(
                "HALPHA_MONITORED"
                if action_kind is ProposedActionKind.ENTRY
                else "NONE"
            ),
            causation_ref=content_digest(
                {
                    "activation_id": activation_id,
                    "fixture": "EXECUTION_FEE_EVIDENCE",
                    "action_kind": action_kind.value,
                }
            ),
            execution_context=execution_context,
        )
        current_position = "0" if action_kind is ProposedActionKind.ENTRY else "1"
        current_notional = "0" if action_kind is ProposedActionKind.ENTRY else "100"
        post_position = "1" if action_kind is ProposedActionKind.ENTRY else "0"
        action_check = ActionCheckInput(
            environment_id=FIXTURE_ENVIRONMENT_ID,
            environment_kind=EnvironmentKind.DEMO,
            authority_class=AuthorityClass.DEMO_VALIDATION,
            activation_id=activation_id,
            account_ref=FIXTURE_ACCOUNT_ID,
            instrument_ref="BTCUSDT-PERP",
            action_profile=action_profile,
            control_category=control_category,
            risk_class=risk_class,
            checked_at=action_time,
            quantized_quantity="1",
            conservative_price=price,
            economic_action_prior_notional=current_notional,
            activation_current_notional=current_notional,
            account_current_notional=current_notional,
            activation_current_margin="20" if current_position == "1" else "0",
            account_dynamic_available_margin="500",
            actual_margin_mode="ISOLATED",
            actual_leverage="20",
            post_action_abs_position=post_position,
            current_abs_position=current_position,
        )
        result = coordinator.consume_proposed_action(
            plan_event_id=str(uuid4()),
            execution_action_id=str(uuid4()),
            activation_id=activation_id,
            rule_id=f"FIXTURE_FEE_{source_suffix}",
            source_identity=(
                f"{activation_id}:FIXTURE:FEE:{source_suffix}:{int(action_time.timestamp())}"
            ),
            source_cutoff=action_time,
            input_digest=content_digest(
                {
                    "activation_id": activation_id,
                    "source_suffix": source_suffix,
                    "action_time": action_time,
                }
            ),
            proposed_action=proposed,
            action_check=action_check,
            observed_at=action_time,
            client_order_id=client_order_id,
        )
        action = result.execution_action
        if action is None:
            raise RuntimeError(
                "WORKBENCH_FEE_EVIDENCE_ACTION_NOT_CREATED "
                f"kind={action_kind.value} "
                f"reason={result.plan_event.capital_decision.get('reason_code')}"
            )
        with executor_connection.transaction():
            execution.prepare_submission(
                action.execution_action_id,
                capital_decision=CapDecision.model_validate(
                    result.plan_event.capital_decision
                ),
                request_payload={
                    "profile": action_profile,
                    "quantity": "1",
                },
                observed_at=action_time + timedelta(milliseconds=100),
            )

        trade_id = f"fixture-fee-{source_suffix.lower()}"
        common = {
            "environment_id": FIXTURE_ENVIRONMENT_ID,
            "venue_ref": "BINANCE_USDM",
            "account_ref": FIXTURE_ACCOUNT_ID,
            "instrument_ref": "BTCUSDT-PERP",
            "source_class": VenueFactSourceClass.VENUE_STREAM,
            "source_object_id": trade_id,
            "source_time": action_time + timedelta(milliseconds=200),
            "received_at": action_time + timedelta(milliseconds=300),
            "cutoff": action_time + timedelta(milliseconds=300),
            "action": action,
        }
        fill = build_venue_fact(
            venue_fact_id=str(uuid4()),
            kind=VenueFactKind.FILL,
            source_sequence=f"{source_suffix}:FILL",
            payload={
                "event_type": "FixtureOrderFilled",
                "trade_id": trade_id,
                "client_order_id": client_order_id,
                "venue_order_ref": f"fixture-order-{source_suffix.lower()}",
                "last_price": price,
                "last_quantity": "1",
                "leaves_quantity": "0",
                "order_side": order_side,
                "liquidity_side": liquidity_side,
                "reconciliation": False,
            },
            **common,
        )
        commission = build_venue_fact(
            venue_fact_id=str(uuid4()),
            kind=VenueFactKind.COMMISSION,
            source_sequence=f"{source_suffix}:COMMISSION",
            payload={
                "event_type": "FixtureOrderFilled",
                "trade_id": trade_id,
                "client_order_id": client_order_id,
                "amount": f"{fee} USDT",
                "currency": "USDT",
            },
            **common,
        )
        coordinator.apply_venue_fact(
            fill,
            observed_at=action_time + timedelta(milliseconds=400),
        )
        coordinator.apply_venue_fact(
            commission,
            observed_at=action_time + timedelta(milliseconds=500),
        )
        refs = (fill.venue_fact_id, commission.venue_fact_id)
        with executor_connection.transaction():
            execution.reconcile_execution_action(
                action.execution_action_id,
                closure_evidence={
                    "order_terminal": True,
                    "terminal_order_status": "FILLED",
                    "fills_complete": True,
                    "fees_complete": True,
                    "position_effect_known": True,
                    "position_effect": post_position,
                },
                venue_fact_refs=refs,
                observed_at=action_time + timedelta(milliseconds=600),
            )
        fact_refs.extend(refs)

    close_filled_action(
        action_kind=ProposedActionKind.ENTRY,
        action_profile="ENTRY_MARKET",
        risk_class=RiskClass.RISK_INCREASING,
        control_category=StopCategory.NEW_RISK,
        order_side="BUY",
        liquidity_side="MAKER",
        price="100",
        fee="0.02",
        source_suffix="ENTRY",
        offset_seconds=1,
    )
    close_filled_action(
        action_kind=ProposedActionKind.EXIT,
        action_profile="REDUCE_OR_CLOSE_MARKET",
        risk_class=RiskClass.RISK_REDUCING,
        control_category=StopCategory.RISK_REDUCTION_OR_ORDER_MANAGEMENT,
        order_side="SELL",
        liquidity_side="TAKER",
        price="101",
        fee="0.0404",
        source_suffix="EXIT",
        offset_seconds=4,
    )
    return tuple(fact_refs)


def _prepare_states(app_connection: object, executor_connection: object) -> dict[str, str]:
    now = datetime.now(UTC)
    with app_connection.transaction():
        completed = create_and_activate(
            app_connection,
            environment_id=FIXTURE_ENVIRONMENT_ID,
            account_ref=FIXTURE_ACCOUNT_ID,
            now=now,
            instrument_ref="BTCUSDT-PERP",
            limits=("50", "250", "25"),
        )
    coordinator = HalphaCoordinator(
        executor_connection,
        PersistedActionGate(
            PostgreSQLExecutionActionRepository(
                executor_connection,
                FIXTURE_ENVIRONMENT_ID,
            ),
            NoExchangeClient(),
            environment_id=FIXTURE_ENVIRONMENT_ID,
            execution_profile_ref="BINANCE_DEMO",
            account_ref=FIXTURE_ACCOUNT_ID,
        ),
        environment_id=FIXTURE_ENVIRONMENT_ID,
        environment_kind="DEMO",
        authority_class="DEMO_VALIDATION",
        execution_profile_ref="BINANCE_DEMO",
        account_ref=FIXTURE_ACCOUNT_ID,
        runtime_real_write_gate="CLOSED",
    )
    completed_fact_refs = _seed_completed_fee_evidence(
        executor_connection,
        coordinator=coordinator,
        activation_id=completed["activation_id"],
        observed_at=now,
    )
    coordinator.close_activation(
        activation_id=completed["activation_id"],
        cutoff=now + timedelta(seconds=7),
        position_zero=True,
        open_order_refs=(),
        external_activity_conflict=False,
        user_takeover=False,
        handover_command_ref=None,
        fact_refs=completed_fact_refs,
        observed_at=now + timedelta(seconds=7),
    )

    with app_connection.transaction():
        gap = create_and_activate(
            app_connection,
            environment_id=FIXTURE_ENVIRONMENT_ID,
            account_ref=FIXTURE_ACCOUNT_ID,
            now=now + timedelta(seconds=10),
            instrument_ref="BTCUSDT-PERP",
            limits=("100", "500", "50"),
        )
        exiting = create_and_activate(
            app_connection,
            environment_id=FIXTURE_ENVIRONMENT_ID,
            account_ref=FIXTURE_ACCOUNT_ID,
            now=now + timedelta(seconds=20),
            instrument_ref="ETHUSDT-PERP",
            limits=("80", "400", "40"),
        )
        takeover = create_and_activate(
            app_connection,
            environment_id=FIXTURE_ENVIRONMENT_ID,
            account_ref=FIXTURE_ACCOUNT_ID,
            now=now + timedelta(seconds=30),
            instrument_ref="SOLUSDT-PERP",
            limits=("60", "300", "30"),
        )
        stale_control = create_and_activate(
            app_connection,
            environment_id=FIXTURE_ENVIRONMENT_ID,
            account_ref=FIXTURE_ACCOUNT_ID,
            now=now + timedelta(seconds=35),
            instrument_ref="XRPUSDT-PERP",
            limits=("40", "200", "20"),
        )
        recovery = create_and_activate(
            app_connection,
            environment_id=FIXTURE_ENVIRONMENT_ID,
            account_ref=FIXTURE_ACCOUNT_ID,
            now=now + timedelta(seconds=37),
            instrument_ref="BNBUSDT-PERP",
            limits=("30", "150", "15"),
        )
        recovery_narrow = create_and_activate(
            app_connection,
            environment_id=FIXTURE_ENVIRONMENT_ID,
            account_ref=FIXTURE_ACCOUNT_ID,
            now=now + timedelta(seconds=38),
            instrument_ref="ADAUSDT-PERP",
            limits=("30", "150", "15"),
        )

    with executor_connection.transaction():
        _create_unknown_entry(
            executor_connection,
            activation_id=gap["activation_id"],
            observed_at=now + timedelta(seconds=43),
        )

    with app_connection.transaction():
        app_connection.execute(
            """
            UPDATE halpha.plan_activation
            SET has_entry_fill = true, protection_state = 'GAP',
                latest_venue_cutoff = %s, state_version = state_version + 1,
                updated_at = %s
            WHERE environment_id = %s AND activation_id = %s
            """,
            (now, now + timedelta(seconds=40), FIXTURE_ENVIRONMENT_ID, gap["activation_id"]),
        )
        app_connection.execute(
            """
            UPDATE halpha.plan_activation
            SET lifecycle = 'EXITING', state_version = state_version + 1, updated_at = %s
            WHERE environment_id = %s AND activation_id = %s
            """,
            (now + timedelta(seconds=41), FIXTURE_ENVIRONMENT_ID, exiting["activation_id"]),
        )
        app_connection.execute(
            """
            UPDATE halpha.plan_activation
            SET lifecycle = 'USER_TAKEOVER', responsibility_owner = 'USER',
                entry_opportunity_consumed = true, takeover_scope = %s,
                state_version = state_version + 1, updated_at = %s
            WHERE environment_id = %s AND activation_id = %s
            """,
            (
                '{"source":"TRADING_WORKBENCH_FIXTURE","open_responsibility":"USER"}',
                now + timedelta(seconds=42),
                FIXTURE_ENVIRONMENT_ID,
                takeover["activation_id"],
            ),
        )
        app_connection.execute(
            """
            UPDATE halpha.plan_activation
            SET run_state = 'PAUSED', pause_reason = 'WRITER_CONTINUITY_LOST',
                paused_at = %s, state_version = state_version + 1, updated_at = %s
            WHERE environment_id = %s AND activation_id IN (%s, %s)
            """,
            (
                now + timedelta(seconds=44),
                now + timedelta(seconds=44),
                FIXTURE_ENVIRONMENT_ID,
                recovery["activation_id"],
                recovery_narrow["activation_id"],
            ),
        )
    return {
        "completed": completed["activation_id"],
        "gap": gap["activation_id"],
        "exiting": exiting["activation_id"],
        "takeover": takeover["activation_id"],
        "stale_control": stale_control["activation_id"],
        "recovery": recovery["activation_id"],
        "recovery_narrow": recovery_narrow["activation_id"],
    }


def main() -> int:
    args = _parse_args()
    settings = load_settings(ROOT / "config" / "halpha.example.toml")
    if args.port is not None:
        if not 1 <= args.port <= 65535:
            raise ValueError("WORKBENCH_FIXTURE_PORT_OUT_OF_RANGE")
        settings = settings.model_copy(
            update={"app": settings.app.model_copy(update={"port": args.port})}
        )
    if args.max_runtime_seconds is not None and args.max_runtime_seconds <= 0:
        raise ValueError("WORKBENCH_FIXTURE_MAX_RUNTIME_NOT_POSITIVE")
    fixture_database_name = _fixture_database_name()
    settings = settings.model_copy(
        update={
            "release": settings.release.model_copy(
                update={
                    "environment_id": FIXTURE_ENVIRONMENT_ID,
                    "account_id": FIXTURE_ACCOUNT_ID,
                    "database_name": fixture_database_name,
                }
            )
        }
    )
    backend = keyring.get_keyring()
    require_win_vault_backend(backend)
    reference = settings.app.database_credential_reference
    database_password = backend.get_password(reference.service, reference.account)
    if not database_password:
        raise RuntimeError("WORKBENCH_FIXTURE_DATABASE_CREDENTIAL_MISSING")

    fixture_database_created = False
    app_connection = None
    executor_connection = None
    try:
        _create_fixture_database(fixture_database_name)
        fixture_database_created = True
        app_connection = connect_app(fixture_database_name)
        executor_connection = connect_executor(fixture_database_name)
        _prepare_states(app_connection, executor_connection)
        app = create_app(
            settings,
            AppSecrets(
                database_password=SecretStr(database_password),
                csrf_signing_secret=SecretStr("workbench-fixture-csrf-signing-only"),
            ),
            repo_root=ROOT,
            projection=PostgreSQLWorkbenchProjection(
                database_name=settings.release.database_name,
                database_role_name=settings.app.database_role_name,
                password=SecretStr(database_password),
                environment_id=settings.release.environment_id,
                account_id=settings.release.account_id,
            ),
            market_context_provider=FixtureMarketContextProvider(),
            market_stream_provider=FixtureMarketStreamProvider(),
            instrument_rules_provider=FixtureInstrumentRulesProvider(),
            static_dist=args.static_dist,
            product_build_id="a" * 64,
        )
        server_config = {
            "host": settings.app.bind,
            "port": settings.app.port,
            "workers": 1,
            "reload": False,
            "proxy_headers": False,
            "server_header": False,
            "log_level": "warning",
            "timeout_graceful_shutdown": 10,
        }
        if args.max_runtime_seconds is None:
            uvicorn.run(app, **server_config)
        else:
            server = uvicorn.Server(uvicorn.Config(app, **server_config))
            shutdown_timer = Timer(
                args.max_runtime_seconds,
                lambda: setattr(server, "should_exit", True),
            )
            shutdown_timer.daemon = True
            shutdown_timer.start()
            try:
                server.run()
            finally:
                shutdown_timer.cancel()
    finally:
        database_password = None
        try:
            if executor_connection is not None:
                executor_connection.close()
            if app_connection is not None:
                app_connection.close()
        finally:
            if fixture_database_created:
                _drop_fixture_database(fixture_database_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
