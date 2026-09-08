from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from halpha.app.planning_api import PlanCreatePayload, PostgreSQLPlanningApi
from halpha.public_market import (
    MarketBar,
    MarketContext,
    MarketContextProvider,
    MarketFundingRateHistory,
    MarketFundingRateSample,
    MarketInterval,
    MarketWindow,
)
from halpha.public_market_stream import (
    MarketStreamBar,
    MarketStreamQuote,
    MarketStreamStatus,
    PublicMarketStreamProvider,
)
from halpha.public_instrument_rules import InstrumentRulesProvider
from halpha.app.projection import ProjectionUnavailable
from halpha.app.security import csrf_cookie_name
from halpha.app.secrets import AppSecrets
from halpha.app.web import create_app, _live_read_only_request_is_non_mutating
from halpha.capital.repository import CapitalConflict
from halpha.configuration import load_settings
from halpha.domain_values import content_digest
from halpha.planning.registry import Direction, OneShotParameters
from halpha.planning.order_schedule import InstrumentOrderRules
from halpha.scalping.models import (
    BinanceContract,
    BinanceContractCatalog,
    ScalpCycleCreateResponse,
    ScalpCycleRecord,
    ScalpTemplate,
)
from halpha.user_workbench.repository import CommandConflict


ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "http://127.0.0.1:8765"


def _mock_activation(activation_id: str) -> dict[str, Any]:
    observed_at = datetime(2026, 7, 20, tzinfo=UTC).isoformat()
    return {
        "activation_id": activation_id,
        "environment_id": "demo-main",
        "environment_kind": "DEMO",
        "authority_class": "DEMO_VALIDATION",
        "plan_version_ref": "plan-version-001",
        "account_ref": "demo-account",
        "instrument_ref": "BTCUSDT-PERP",
        "direction": "LONG",
        "decision_basis_ref": "ONE_SHOT_DONCHIAN_ATR@1",
        "framework_strategy_id": "strategy-1",
        "order_schedule_snapshot": None,
        "target_exposure": "100",
        "lifecycle": "RUNNING",
        "run_state": "ACTIVE",
        "rule_state": {},
        "created_at": observed_at,
        "updated_at": observed_at,
    }


class FakeProjection:
    def __init__(
        self,
        *,
        available: bool = True,
        activations: list[dict[str, Any]] | None = None,
        executor_status: str = "READY",
        overview_overrides: dict[str, Any] | None = None,
    ) -> None:
        self.available = available
        self.activations = activations or []
        self.projected_executor_status = executor_status
        self.overview_overrides = overview_overrides or {}
        self.overview_targets: list[tuple[str | None, str | None]] = []

    def overview(
        self,
        *,
        entry_instrument_ref: str | None = None,
        entry_direction: str | None = None,
    ) -> dict[str, Any]:
        if not self.available:
            raise ProjectionUnavailable("DATABASE_UNAVAILABLE")
        self.overview_targets.append((entry_instrument_ref, entry_direction))
        return {
            "database_available": True,
            "server_fact_cutoff": "2026-07-17T00:00:00Z",
            "open_activation_count": 0,
            "database_name": "halpha_demo",
            **self.overview_overrides,
        }

    def availability(self) -> dict[str, Any]:
        return {
            "database_available": self.available,
            "reason_code": None if self.available else "DATABASE_UNAVAILABLE",
            "server_fact_cutoff": (
                "2026-07-17T00:00:00Z" if self.available else None
            ),
        }

    def operations(self) -> dict[str, Any]:
        if not self.available:
            raise ProjectionUnavailable("DATABASE_UNAVAILABLE")
        return {
            "database_available": True,
            "server_fact_cutoff": "2026-07-17T00:00:00Z",
            "activations": self.activations,
        }

    def executor_status(self, product_build_id: str) -> dict[str, Any]:
        return {
            "status": self.projected_executor_status,
            "checked_at": "2026-07-17T00:00:01Z",
            "product_build_consistent": (
                True
                if self.projected_executor_status == "READY"
                else False
                if self.projected_executor_status == "BUILD_MISMATCH"
                else None
            ),
            "product_build_id": product_build_id,
        }


class FakeMarketContext:
    async def fetch(
        self,
        instrument_ref: str,
        lookback: int,
        stop_reference_interval: MarketInterval = "15m",
    ) -> MarketContext:
        return MarketContext(
            instrument_ref=instrument_ref,
            source="BINANCE_DEMO_PUBLIC",
            source_cutoff=datetime(2026, 7, 20, tzinfo=UTC),
            latest_closed_1m_at=datetime(2026, 7, 20, tzinfo=UTC),
            latest_closed_15m_at=datetime(2026, 7, 20, tzinfo=UTC),
            latest_closed_stop_reference_at=datetime(2026, 7, 20, tzinfo=UTC),
            channel_lookback_15m=lookback,
            stop_reference_interval=stop_reference_interval,
            bid_price="100",
            ask_price="101",
            reference_price="100.5",
            latest_close_1m="100.25",
            latest_volume_1m="12.5",
            latest_trade_count_1m=8,
            latest_close_15m="100",
            channel_upper="102",
            channel_lower="98",
            atr_14="2",
            stop_reference_atr_14="2",
            long_breakout_gap_pct="1.492537313432835820895522388",
            short_breakout_gap_pct="2.48756218905472636815920398",
        )

    async def fetch_window(
        self,
        instrument_ref: str,
        interval: MarketInterval,
        start_at: datetime,
        end_at: datetime,
    ) -> MarketWindow:
        return MarketWindow(
            instrument_ref=instrument_ref,
            interval=interval,
            source="BINANCE_DEMO_PUBLIC",
            source_cutoff=end_at,
            bars=(
                MarketBar(
                    open_at=start_at,
                    close_at=end_at,
                    open="100",
                    high="102",
                    low="99",
                    close="101",
                    volume="12.5",
                ),
            ),
        )

    async def fetch_funding_rate_history(
        self,
        instrument_ref: str,
    ) -> MarketFundingRateHistory:
        assert instrument_ref == "BTCUSDT-PERP"
        return MarketFundingRateHistory(
            instrument_ref=instrument_ref,
            source="BINANCE_DEMO_PUBLIC",
            source_cutoff=datetime(2026, 7, 20, 16, tzinfo=UTC),
            samples=tuple(
                MarketFundingRateSample(
                    settled_at=datetime(2026, 7, 18 + index // 3, (index % 3) * 8, tzinfo=UTC),
                    funding_rate="0.0001",
                )
                for index in range(6)
            ),
            average_funding_rate="0.0001",
            average_interval_seconds=28_800,
        )


class FakeInstrumentRules:
    async def fetch(self, instrument_ref: str) -> InstrumentOrderRules:
        assert instrument_ref == "BTCUSDT-PERP"
        return InstrumentOrderRules(
            source="BINANCE_DEMO_EXCHANGE_INFO",
            min_price="0.1",
            max_price="1000000",
            price_tick_size="0.1",
            limit_quantity_step="0.001",
            min_limit_quantity="0.001",
            max_limit_quantity="1000",
            market_quantity_step="0.01",
            min_market_quantity="0.01",
            max_market_quantity="100",
            min_notional="5",
            source_cutoff="2026-07-23T00:00:00+00:00",
        )

    async def list_contracts(self) -> BinanceContractCatalog:
        return BinanceContractCatalog(
            source="BINANCE_DEMO_EXCHANGE_INFO",
            source_cutoff=datetime(2026, 7, 23, tzinfo=UTC),
            contracts=(
                BinanceContract(
                    instrument_ref="BTCUSDT-PERP",
                    symbol="BTCUSDT",
                    base_asset="BTC",
                ),
                BinanceContract(
                    instrument_ref="ETHUSDT-PERP",
                    symbol="ETHUSDT",
                    base_asset="ETH",
                ),
            ),
        )


class FakeMarketStream:
    def stream(self, instrument_ref: str):
        async def events():
            yield MarketStreamStatus(
                state="LIVE",
                source="BINANCE_DEMO_PUBLIC",
                observed_at=datetime(2026, 7, 20, tzinfo=UTC),
            )
            yield MarketStreamQuote(
                instrument_ref=instrument_ref,
                source="BINANCE_DEMO_PUBLIC",
                source_cutoff=datetime(2026, 7, 20, tzinfo=UTC),
                received_at=datetime(2026, 7, 20, tzinfo=UTC),
                bid_price="100",
                ask_price="101",
                reference_price="100.5",
            )

        return events()

    async def close(self) -> None:
        return None


def make_client(
    tmp_path: Path,
    *,
    config_path: Path | None = None,
    projection: FakeProjection | None = None,
    market_context_provider: MarketContextProvider | None = None,
    market_stream_provider: PublicMarketStreamProvider | None = None,
    instrument_rules_provider: InstrumentRulesProvider | None = None,
    monotonic_provider: Callable[[], float] | None = None,
    schema_guard: Callable[[], None] | None = None,
    static_dist: Path | None = None,
) -> TestClient:
    settings = load_settings(
        config_path or ROOT / "config" / "halpha.example.toml"
    )
    app = create_app(
        settings,
        AppSecrets(
            database_password=SecretStr("database-test-secret"),
            csrf_signing_secret=SecretStr("csrf-test-secret-which-is-not-shared"),
        ),
        repo_root=ROOT,
        product_build_id="a" * 64,
        projection=projection or FakeProjection(),
        market_context_provider=market_context_provider,
        market_stream_provider=market_stream_provider,
        instrument_rules_provider=instrument_rules_provider,
        static_dist=static_dist or (tmp_path / "missing-dist"),
        monotonic_provider=monotonic_provider,
        schema_guard=schema_guard,
    )
    return TestClient(
        app,
        base_url=f"http://127.0.0.1:{settings.app.port}",
    )


def csrf(client: TestClient) -> str:
    response = client.get("/operations")
    assert response.status_code == 200
    token = client.cookies.get(csrf_cookie_name(client.base_url.port))
    assert token
    return token


def _programmatic_direct_plan_payload(
    *,
    creator_kind: str = "MONITOR",
) -> dict[str, Any]:
    return {
        "plan_name": "Monitor signal execution",
        "creator_kind": creator_kind,
        "decision_context": {
            "rationale": "A stable Monitor signal selected this execution.",
            "evidence": "monitor-signal:btc-breakout:20260720T000000Z",
            "limitations": "The signal does not bypass Trading facts or checks.",
            "intent": "PROFIT_SEEKING",
            "setup_family": "BREAKOUT_CONTINUATION",
            "playbook_ref": "MONITOR_BTC_BREAKOUT_V1",
            "invalidation": "Cancel if the fixed Monitor invalidation is reached.",
            "evidence_cutoff": "2026-07-20T00:00:00+00:00",
        },
        "decision_basis": {
            "kind": "DIRECT_EXECUTION",
            "decision_basis_ref": "DIRECT_EXECUTION@1",
            "parameters": {},
        },
        "order_schedule_spec": {
            "entry_program": {"kind": "ONE_TIME"},
            "price_distribution": {
                "kind": "SINGLE",
                "limit_price": "100",
            },
            "amount_distribution": {
                "mode": "FIXED",
                "base_notional": "100",
            },
            "venue_policy": {
                "order_type": "LIMIT",
                "time_in_force": "GTC",
            },
            "protection_policy": {
                "initial_stop": {"distance_bps": "100"},
                "time_exit_seconds": 3600,
                "full_fill_loss_budget": {
                    "entry_fee_bps": "2",
                    "exit_fee_bps": "5",
                },
            },
        },
        "instrument_ref": "BTCUSDT-PERP",
        "direction": "LONG",
        "target_exposure": "100",
        "max_margin": "100",
        "max_notional": "100",
        "max_allowed_loss": "10",
        "valid_minutes": 15,
    }


def test_direct_plan_accepts_the_simplified_record_without_a_playbook_ref() -> None:
    payload = _programmatic_direct_plan_payload()
    payload["decision_context"].pop("playbook_ref")

    validated = PlanCreatePayload.model_validate(payload)

    assert validated.decision_context.playbook_ref is None


def test_read_surface_is_available_without_login(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    overview = client.get("/api/v1/overview")

    assert overview.status_code == 200
    assert overview.json()["environment_kind"] == "DEMO"
    assert overview.json()["runtime_real_write_gate"] == "CLOSED"
    assert client.post("/api/v1/session/login").status_code == 403
    assert client.get("/api/v1/session/logout").status_code == 404


def test_overview_exposes_new_risk_discipline_without_return_target_tracking(
    tmp_path: Path,
) -> None:
    overview = make_client(tmp_path).get("/api/v1/overview")

    assert overview.status_code == 200
    payload = overview.json()
    assert "performance_reference" not in payload
    assert payload["new_risk_discipline"]["status"] == "UNKNOWN"
    assert payload["new_risk_discipline"]["new_risk_allowed"] is False
    assert payload["new_risk_discipline"]["blocker_codes"] == [
        "ACCOUNT_EQUITY_SNAPSHOT_UNAVAILABLE"
    ]


def test_overview_can_scope_discipline_capacity_to_the_direct_entry_target(
    tmp_path: Path,
) -> None:
    projection = FakeProjection()
    overview = make_client(tmp_path, projection=projection).get(
        "/api/v1/overview",
        params={
            "entry_instrument_ref": "BTCUSDT-PERP",
            "entry_direction": "LONG",
        },
    )

    assert overview.status_code == 200
    assert projection.overview_targets == [("BTCUSDT-PERP", "LONG")]


def test_overview_returns_typed_account_positions_and_orders(
    tmp_path: Path,
) -> None:
    cutoff = "2030-01-15T04:00:00Z"
    client = make_client(
        tmp_path,
        projection=FakeProjection(
            overview_overrides={
                "account_snapshot_status": "CURRENT",
                "account_snapshot_ref": "snapshot-1",
                "account_snapshot_cutoff": cutoff,
                "account_snapshot_age_seconds": 0,
                "account_ordinary_open_order_count": 1,
                "account_algo_open_order_count": 0,
                "account_summary": {
                    "can_trade": True,
                    "wallet_balance": "1000",
                    "unrealized_pnl": "-12.5",
                    "margin_balance": "987.5",
                    "available_balance": "800",
                    "initial_margin": "187.5",
                    "maintenance_margin": "25",
                    "position_initial_margin": "150",
                    "open_order_initial_margin": "37.5",
                    "cross_wallet_balance": "1000",
                    "cross_unrealized_pnl": "-12.5",
                    "source_update_time_ms": 1894680004000,
                    "fact_cutoff": cutoff,
                    "snapshot_ref": "snapshot-1",
                },
                "account_positions": [
                    {
                        "instrument_ref": "TESTUSDT-PERP",
                        "symbol": "TESTUSDT",
                        "direction": "SHORT",
                        "position_side": "SHORT",
                        "quantity": "-2.5",
                        "absolute_quantity": "2.5",
                        "entry_price": "100",
                        "break_even_price": "100.1",
                        "mark_price": "105",
                        "unrealized_pnl": "-12.5",
                        "liquidation_price": "200",
                        "leverage": 2,
                        "margin_mode": "CROSS",
                        "notional": "-262.5",
                        "isolated_margin": "0",
                        "fact_cutoff": cutoff,
                        "snapshot_ref": "snapshot-1",
                        "origin": "EXTERNAL_UNMANAGED",
                    }
                ],
                "account_orders": [
                    {
                        "kind": "ORDINARY",
                        "instrument_ref": "TESTUSDT-PERP",
                        "symbol": "TESTUSDT",
                        "order_id": "1234",
                        "client_order_id": "external-order",
                        "side": "BUY",
                        "position_side": "SHORT",
                        "order_type": "LIMIT",
                        "status": "NEW",
                        "time_in_force": "GTC",
                        "price": "150",
                        "trigger_price": "0",
                        "quantity": "1",
                        "executed_quantity": "0",
                        "reduce_only": True,
                        "close_position": False,
                        "source_create_time_ms": 1894680000000,
                        "source_update_time_ms": 1894680001000,
                        "fact_cutoff": cutoff,
                        "snapshot_ref": "snapshot-1",
                    }
                ],
            }
        ),
    )

    response = client.get("/api/v1/overview")

    assert response.status_code == 200
    payload = response.json()
    assert payload["account_positions"][0]["management_status"] == (
        "OBSERVED_ONLY"
    )
    assert payload["account_orders"][0]["order_id"] == "1234"
    assert payload["account_summary"]["margin_balance"] == "987.5"


def test_app_requires_schema_guard_before_serving(tmp_path: Path) -> None:
    calls: list[str] = []
    client = make_client(
        tmp_path,
        schema_guard=lambda: calls.append("checked"),
    )

    with client:
        assert client.get("/api/v1/overview").status_code == 200

    assert calls == ["checked"]


def test_app_startup_fails_when_schema_guard_rejects(tmp_path: Path) -> None:
    def reject() -> None:
        raise RuntimeError("DATABASE_SCHEMA_VERSION_MISMATCH")

    client = make_client(tmp_path, schema_guard=reject)

    with pytest.raises(RuntimeError, match="DATABASE_SCHEMA_VERSION_MISMATCH"):
        with client:
            pass


def test_apps_use_port_scoped_csrf_and_report_atomic_context_targets(
    tmp_path: Path,
) -> None:
    demo = make_client(
        tmp_path,
        config_path=ROOT / "config" / "halpha.example.toml",
    )
    live = make_client(
        tmp_path,
        config_path=ROOT / "config" / "halpha.live-copy-read-only.example.toml",
    )

    assert demo.get("/operations").status_code == 200
    assert live.get("/operations").status_code == 200
    demo_cookie = csrf_cookie_name(8765)
    live_cookie = csrf_cookie_name(8766)
    assert demo_cookie != live_cookie
    assert demo_cookie in demo.cookies
    assert live_cookie not in demo.cookies
    assert live_cookie in live.cookies
    assert demo_cookie not in live.cookies

    demo_status = demo.get("/api/v1/settings/status")
    live_status = live.get("/api/v1/settings/status")
    assert demo_status.status_code == 200
    assert live_status.status_code == 200
    assert demo_status.json()["port"] == 8765
    assert demo_status.json()["venue_account_type"] == "USDM_DEMO"
    assert live_status.json()["port"] == 8766
    assert live_status.json()["venue_account_type"] == "USDM_COPY_LEAD"
    expected_targets = {
        ("USDM_DEMO", "http://127.0.0.1:8765"),
        ("USDM_COPY_LEAD", "http://127.0.0.1:8766"),
        ("USDM_PERSONAL", "http://127.0.0.1:8767"),
    }
    for status in (demo_status.json(), live_status.json()):
        assert {
            (target["venue_account_type"], target["url"])
            for target in status["trading_contexts"]
        } == expected_targets


@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("POST", "/api/v1/plans"),
        ("PUT", "/api/v1/plans/plan-ro"),
        ("DELETE", "/api/v1/plans/plan-ro"),
        ("POST", "/api/v1/plans/plan-ro/submit-and-start"),
        ("POST", "/api/v1/activations"),
        ("POST", "/api/v1/scalping/cycles"),
        ("POST", "/api/v1/activations/activation-ro/exit"),
        ("PUT", "/api/v1/reviews/review-ro"),
        ("POST", "/api/v1/reviews/review-ro/complete"),
        ("POST", "/api/v1/stage-reviews"),
    ),
)
def test_live_read_only_http_boundary_rejects_all_product_mutations(
    tmp_path: Path,
    method: str,
    path: str,
) -> None:
    client = make_client(
        tmp_path,
        config_path=ROOT / "config" / "halpha.live-copy-read-only.example.toml",
    )
    token = csrf(client)

    response = client.request(
        method,
        path,
        headers={
            "Origin": "http://127.0.0.1:8766",
            "X-CSRFToken": token,
            "Idempotency-Key": "live-read-only-rejected",
            "If-Match": "1",
        },
        json={},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == {
        "code": "LIVE_READ_ONLY_PRODUCT_MUTATION_FORBIDDEN"
    }


def test_live_read_only_http_boundary_keeps_only_non_mutating_posts_open() -> None:
    assert _live_read_only_request_is_non_mutating(
        "POST",
        "/api/v1/decision-evidence/preview",
    )
    assert _live_read_only_request_is_non_mutating(
        "POST",
        "/api/v1/order-schedules/preview",
    )
    assert _live_read_only_request_is_non_mutating(
        "POST",
        "/api/v1/playbook-qualification/export",
    )
    assert _live_read_only_request_is_non_mutating(
        "POST",
        "/api/v1/plan-versions/version-ro/activation-preview",
    )
    assert _live_read_only_request_is_non_mutating(
        "POST",
        "/api/v1/settings/test-email",
    )
    assert _live_read_only_request_is_non_mutating(
        "POST",
        "/api/v1/scalping/recommendation",
    )
    assert not _live_read_only_request_is_non_mutating(
        "POST",
        "/api/v1/activations/activation-ro/control-preview",
    )


def test_demo_exports_a_content_addressed_playbook_qualification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample_started_at = datetime.now(UTC) - timedelta(days=1)
    sample_refs = [
        {
            "review_id": f"review-export-{index:02d}",
            "review_version": 1,
            "review_content_digest": content_digest({"review": index}),
            "fact_cutoff": (sample_started_at + timedelta(minutes=index)).isoformat(),
        }
        for index in range(30)
    ]

    def decision_evidence(
        _self: Any,
        payload: Any,
        *,
        decision_basis_ref: str,
        parameter_digest: str,
    ) -> dict[str, Any]:
        return {
            "instrument_ref": payload.instrument_ref,
            "direction": payload.direction.value,
            "decision_basis_ref": decision_basis_ref,
            "parameter_digest": parameter_digest,
            "intent": payload.intent.value,
            "setup_family": payload.setup_family.value,
            "playbook_ref": payload.playbook_ref,
            "source_cutoff": sample_refs[-1]["fact_cutoff"],
            "comparable_trade_count": 30,
            "sample_review_refs": sample_refs,
            "sample_traceability_complete": True,
            "sample_identity_digest": content_digest(sample_refs),
            "metrics": {
                "gross_profit": "120",
                "gross_loss": "60",
                "profit_factor": "2",
            },
            "repeatability": {
                "status": "EVIDENCE_CANDIDATE",
                "policy_version": "PLAYBOOK_REPEATABILITY_SCREEN@1",
                "minimum_trade_count": 30,
                "minimum_profit_factor": "1.2",
                "risk_basis_trade_count": 30,
                "net_r_multiple": "6",
                "average_r_multiple": "0.2",
                "net_r_without_best_trade": "4",
                "early_segment_net_r": "3",
                "recent_segment_net_r": "3",
                "mean_r_lower_confidence_bound": "0.05",
            },
        }

    monkeypatch.setattr(
        "halpha.app.outcomes_api.PostgreSQLOutcomesApi.decision_evidence",
        decision_evidence,
    )
    client = make_client(tmp_path)
    token = csrf(client)
    payload = {
        "instrument_ref": "BTCUSDT-PERP",
        "direction": "LONG",
        "decision_basis": {
            "kind": "DIRECT_EXECUTION",
            "decision_basis_ref": "DIRECT_EXECUTION@1",
            "parameters": {},
        },
        "intent": "PROFIT_SEEKING",
        "setup_family": "OTHER",
        "playbook_ref": "DIRECT_RULE_V1",
        "target_venue_account_type": "USDM_COPY_LEAD",
    }

    response = client.post(
        "/api/v1/playbook-qualification/export",
        headers={"Origin": ORIGIN, "X-CSRFToken": token},
        json=payload,
    )

    assert response.status_code == 200
    result = response.json()
    assert result["artifact"]["schema"] == "HALPHA_PLAYBOOK_QUALIFICATION@1"
    assert result["artifact"]["target_venue_account_type"] == "USDM_COPY_LEAD"
    assert result["artifact"]["comparable_trade_count"] == 30
    assert len(result["artifact_content_digest"]) == 64
    assert result["save_outside_repository"] is True


def test_live_context_cannot_export_demo_qualification(tmp_path: Path) -> None:
    client = make_client(
        tmp_path,
        config_path=ROOT / "config" / "halpha.live-copy-read-only.example.toml",
    )
    token = csrf(client)

    response = client.post(
        "/api/v1/playbook-qualification/export",
        headers={
            "Origin": "http://127.0.0.1:8766",
            "X-CSRFToken": token,
        },
        json={
            "instrument_ref": "BTCUSDT-PERP",
            "direction": "LONG",
            "decision_basis": {
                "kind": "DIRECT_EXECUTION",
                "decision_basis_ref": "DIRECT_EXECUTION@1",
                "parameters": {},
            },
            "intent": "PROFIT_SEEKING",
            "setup_family": "OTHER",
            "playbook_ref": "DIRECT_RULE_V1",
            "target_venue_account_type": "USDM_COPY_LEAD",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "PLAYBOOK_QUALIFICATION_EXPORT_REQUIRES_DEMO"
    }


def test_public_market_websocket_is_read_only_and_same_origin(tmp_path: Path) -> None:
    client = make_client(
        tmp_path,
        market_stream_provider=FakeMarketStream(),
    )

    with client.websocket_connect(
        "/api/v1/market-stream?instrument_ref=BTCUSDT-PERP",
        headers={"host": "127.0.0.1:8765", "origin": ORIGIN},
    ) as websocket:
        status = websocket.receive_json()
        quote = websocket.receive_json()

    assert status["type"] == "status"
    assert status["state"] == "LIVE"
    assert quote["type"] == "quote"
    assert quote["reference_price"] == "100.5"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"origin": "https://attacker.example"},
        {"origin": ORIGIN, "authorization": "Bearer forbidden"},
    ],
)
def test_public_market_websocket_rejects_non_local_or_authorized_clients(
    tmp_path: Path,
    headers: dict[str, str],
) -> None:
    client = make_client(
        tmp_path,
        market_stream_provider=FakeMarketStream(),
    )

    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(
            "/api/v1/market-stream?instrument_ref=BTCUSDT-PERP",
            headers={"host": "127.0.0.1:8765", **headers},
        ):
            pass

    assert caught.value.code == 1008


def test_strategy_and_status_reads_need_no_session(tmp_path: Path) -> None:
    client = make_client(
        tmp_path,
        market_context_provider=FakeMarketContext(),
    )

    strategies = client.get("/api/v1/strategies")
    schema = client.get(
        "/api/v1/strategies/ONE_SHOT_DONCHIAN_ATR_BREAKOUT/schema"
    )
    status = client.get("/api/v1/settings/status")
    market = client.get(
        "/api/v1/market-context?instrument_ref=BTCUSDT-PERP"
        "&channel_lookback_15m=20&stop_reference_interval=1h"
    )
    market_window = client.get(
        "/api/v1/market-window",
        params={
            "instrument_ref": "BTCUSDT-PERP",
            "interval": "1m",
            "start_at": "2026-07-20T00:00:00Z",
            "end_at": "2026-07-20T00:01:00Z",
        },
    )
    funding_history = client.get(
        "/api/v1/market-funding-history?instrument_ref=BTCUSDT-PERP"
    )
    naive_market_window = client.get(
        "/api/v1/market-window",
        params={
            "instrument_ref": "BTCUSDT-PERP",
            "interval": "1m",
            "start_at": "2026-07-20T00:00:00",
            "end_at": "2026-07-20T00:01:00",
        },
    )

    assert strategies.status_code == 200
    assert strategies.json()[0]["strategy_id"] == "ONE_SHOT_DONCHIAN_ATR_BREAKOUT"
    assert strategies.json()[0]["plan_key_parameters"][0] == {
        "parameter_key": "demo_immediate_entry",
        "label": "入场模式",
        "display_format": "BOOLEAN_LABEL",
        "unit": None,
        "true_label": "Demo 流程检查",
        "false_label": "自然突破信号",
    }
    assert "Donchian 通道" in strategies.json()[0]["value_logic"]
    assert "15 分钟通道突破" in strategies.json()[0]["applicable_scenarios"]
    assert "ATR 止损和两档止盈" in strategies.json()[0]["execution_behavior"]
    assert (
        strategies.json()[0]["economic_scope"]["profitability_evidence"]
        == "NO_POSITIVE_EXPECTANCY_EVIDENCE"
    )
    assert (
        strategies.json()[0]["economic_scope"]["recommended_use"]
        == "EXECUTION_CHAIN_VALIDATION_ONLY"
    )
    assert strategies.json()[0]["economic_scope"]["evidence_limit"] == (
        "固定短周期规则在费用后的历史开发样本中未取得正期望；"
        "仅用于验证计划、成交、保护和退出链路，不应用于盈利目标。"
    )
    assert schema.status_code == 200
    assert schema.json()["additionalProperties"] is False
    assert status.status_code == 200
    assert status.json()["runtime_real_write_gate"] == "CLOSED"
    assert status.json()["executor_status"] == "READY"
    assert status.json()["app_executor_product_build_consistent"] is True
    assert market.status_code == 200
    assert market.json()["reference_price"] == "100.5"
    assert market.json()["stop_reference_interval"] == "1h"
    assert market_window.status_code == 200
    assert market_window.json()["bars"][0]["close"] == "101"
    assert funding_history.status_code == 200
    assert funding_history.json()["average_funding_rate"] == "0.0001"
    assert len(funding_history.json()["samples"]) == 6
    assert naive_market_window.status_code == 422
    assert naive_market_window.json()["detail"]["code"] == "MARKET_WINDOW_TIMEZONE_REQUIRED"


def test_scalping_catalog_and_recommendation_are_read_only_current_context_inputs(
    tmp_path: Path,
) -> None:
    client = make_client(
        tmp_path,
        market_context_provider=FakeMarketContext(),
        instrument_rules_provider=FakeInstrumentRules(),
    )
    token = csrf(client)

    catalog = client.get("/api/v1/scalping/contracts")
    recommendation = client.post(
        "/api/v1/scalping/recommendation",
        headers={"Origin": ORIGIN, "X-CSRFToken": token},
        json={
            "instrument_ref": "BTCUSDT-PERP",
            "template": {
                "schema_version": "HALPHA_SCALP_TEMPLATE_V1",
                "notional": "100",
                "initial_stop_bps": "35",
                "take_profit_r": "1.5",
                "max_holding_seconds": 300,
                "max_spread_bps": "5",
                "entry_fee_bps": "6",
                "exit_fee_bps": "6",
            },
        },
    )

    assert catalog.status_code == 200
    assert [item["instrument_ref"] for item in catalog.json()["contracts"]] == [
        "BTCUSDT-PERP",
        "ETHUSDT-PERP",
    ]
    assert recommendation.status_code == 200
    assert recommendation.json()["instrument_ref"] == "BTCUSDT-PERP"
    assert recommendation.json()["recommended_template"]["notional"] == "100"
    assert recommendation.json()["basis"] == ["15m ATR × 0.8", "当前价差 × 2.5"]


def test_browser_scalping_trigger_compiles_fresh_rules_before_creating_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}
    template = ScalpTemplate(entry_mode="MAKER_ONLY_SAME_SIDE")

    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "scalp_cycle_replay",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "scalp_cycle_preview",
        lambda _self, payload, *, idempotency_key, maker_limit_price=None: {
            "plan_version_id": "scalp-preview-version",
            "decision_basis_kind": "DIRECT_EXECUTION",
            "venue_ref": "BINANCE_USDM",
            "instrument_ref": payload.instrument_ref,
            "direction": payload.direction.value,
            "trade_amount": payload.template.notional,
            "order_schedule_spec": payload.template.order_schedule_spec(
                maker_limit_price=maker_limit_price,
            ).model_dump(
                mode="json"
            ),
        },
    )

    def trigger(_self, payload, **kwargs):
        observed.update(kwargs)
        assert kwargs["order_schedule_snapshot"].valid is True
        return ScalpCycleCreateResponse(
            cycle=ScalpCycleRecord(
                cycle_id="20000000-0000-0000-0000-000000000001",
                environment_id="demo-main",
                account_ref="demo-account",
                instrument_ref=payload.instrument_ref,
                direction=payload.direction,
                template=payload.template,
                template_digest=payload.template.digest,
                request_digest="a" * 64,
                idempotency_key=kwargs["idempotency_key"],
                plan_id="20000000-0000-0000-0000-000000000002",
                plan_version_id="20000000-0000-0000-0000-000000000003",
                activation_id="20000000-0000-0000-0000-000000000004",
                triggered_at=kwargs["observed_at"],
            ),
            activation={"lifecycle": "RUNNING"},
            runtime_real_write_gate="CLOSED",
        )

    monkeypatch.setattr(PostgreSQLPlanningApi, "trigger_scalp_cycle", trigger)
    client = make_client(
        tmp_path,
        market_context_provider=FakeMarketContext(),
        instrument_rules_provider=FakeInstrumentRules(),
    )
    token = csrf(client)

    response = client.post(
        "/api/v1/scalping/cycles",
        headers={
            "Origin": ORIGIN,
            "X-CSRFToken": token,
            "Idempotency-Key": "scalp-one-click-1",
        },
        json={
            "instrument_ref": "BTCUSDT-PERP",
            "direction": "SHORT",
            "template": template.model_dump(mode="json"),
        },
    )

    assert response.status_code == 201
    assert response.json()["cycle"]["direction"] == "SHORT"
    assert observed["idempotency_key"] == "scalp-one-click-1"
    assert observed["maker_limit_price"] == "101"
    assert observed["order_schedule_snapshot"].schedule_ref == "scalp-preview-version"


def test_programmatic_monitor_cannot_invoke_browser_only_scalping_trigger(
    tmp_path: Path,
) -> None:
    response = make_client(tmp_path).post(
        "/api/v1/scalping/cycles",
        headers={
            "X-Halpha-Caller": "MONITOR",
            "Idempotency-Key": "programmatic-scalp-forbidden",
        },
        json={},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == {"code": "PROGRAMMATIC_API_SCOPE_FORBIDDEN"}


@pytest.mark.parametrize(
    ("path", "method"),
    (
        ("/api/v1/scalping/cycles/by-idempotency", "scalp_cycle_lookup"),
        (
            "/api/v1/activations/20000000-0000-0000-0000-000000000004/exit-by-idempotency",
            "exit_receipt_lookup",
        ),
    ),
)
def test_unknown_scalp_requests_can_be_queried_without_resubmitting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    method: str,
) -> None:
    observed: list[str] = []

    def lookup(_self, *_args, idempotency_key):
        observed.append(idempotency_key)
        return None

    monkeypatch.setattr(PostgreSQLPlanningApi, method, lookup)
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "trigger_scalp_cycle",
        lambda *_args, **_kwargs: pytest.fail("GET must not submit a cycle"),
    )
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "submit_control",
        lambda *_args, **_kwargs: pytest.fail("GET must not submit a control"),
    )
    client = make_client(tmp_path)

    response = client.get(path, params={"idempotency_key": "original-request"})

    assert response.status_code == 200
    assert response.json() is None
    assert observed == ["original-request"]
    assert client.get(path, params={"idempotency_key": "invalid key"}).status_code == 422


def test_market_window_query_cannot_switch_away_from_the_app_environment(
    tmp_path: Path,
) -> None:
    class RecordingMarketContext(FakeMarketContext):
        def __init__(self, source: str) -> None:
            self.source = source
            self.window_calls = 0

        async def fetch_window(
            self,
            instrument_ref: str,
            interval: MarketInterval,
            start_at: datetime,
            end_at: datetime,
        ) -> MarketWindow:
            self.window_calls += 1
            window = await super().fetch_window(
                instrument_ref,
                interval,
                start_at,
                end_at,
            )
            return window.model_copy(update={"source": self.source})

    environment_provider = RecordingMarketContext("BINANCE_DEMO_PUBLIC")
    client = make_client(
        tmp_path,
        market_context_provider=environment_provider,
    )
    params = {
        "instrument_ref": "BTCUSDT-PERP",
        "interval": "1m",
        "start_at": "2026-07-20T00:00:00Z",
        "end_at": "2026-07-20T00:01:00Z",
    }

    review_response = client.get("/api/v1/market-window", params=params)
    reference_response = client.get(
        "/api/v1/market-window",
        params={**params, "purpose": "PUBLIC_REFERENCE"},
    )

    assert review_response.status_code == 200
    assert review_response.json()["source"] == "BINANCE_DEMO_PUBLIC"
    assert reference_response.status_code == 200
    assert reference_response.json()["source"] == "BINANCE_DEMO_PUBLIC"
    assert environment_provider.window_calls == 2


def test_demo_app_rejects_cross_environment_http_market_sources(
    tmp_path: Path,
) -> None:
    class LiveMarketContext(FakeMarketContext):
        async def fetch(
            self,
            instrument_ref: str,
            lookback: int,
            stop_reference_interval: MarketInterval = "15m",
        ) -> MarketContext:
            context = await super().fetch(
                instrument_ref,
                lookback,
                stop_reference_interval,
            )
            return context.model_copy(update={"source": "BINANCE_LIVE_PUBLIC"})

        async def fetch_window(
            self,
            instrument_ref: str,
            interval: MarketInterval,
            start_at: datetime,
            end_at: datetime,
        ) -> MarketWindow:
            window = await super().fetch_window(
                instrument_ref,
                interval,
                start_at,
                end_at,
            )
            return window.model_copy(update={"source": "BINANCE_LIVE_PUBLIC"})

    client = make_client(
        tmp_path,
        market_context_provider=LiveMarketContext(),
    )
    market = client.get(
        "/api/v1/market-context",
        params={
            "instrument_ref": "BTCUSDT-PERP",
            "channel_lookback_15m": 20,
        },
    )
    window = client.get(
        "/api/v1/market-window",
        params={
            "instrument_ref": "BTCUSDT-PERP",
            "interval": "1m",
            "start_at": "2026-07-20T00:00:00Z",
            "end_at": "2026-07-20T00:01:00Z",
        },
    )

    assert market.status_code == 503
    assert market.json()["detail"] == {"code": "MARKET_SOURCE_ENVIRONMENT_MISMATCH"}
    assert window.status_code == 503
    assert window.json()["detail"] == {"code": "MARKET_SOURCE_ENVIRONMENT_MISMATCH"}


@pytest.mark.parametrize(
    "cross_environment_event",
    (
        MarketStreamStatus(
            state="LIVE",
            source="BINANCE_LIVE_PUBLIC",
            observed_at=datetime(2026, 7, 20, tzinfo=UTC),
        ),
        MarketStreamQuote(
            instrument_ref="BTCUSDT-PERP",
            source="BINANCE_LIVE_PUBLIC",
            source_cutoff=datetime(2026, 7, 20, tzinfo=UTC),
            received_at=datetime(2026, 7, 20, tzinfo=UTC),
            bid_price="100",
            ask_price="101",
            reference_price="100.5",
        ),
        MarketStreamBar(
            instrument_ref="BTCUSDT-PERP",
            interval="1m",
            source="BINANCE_LIVE_PUBLIC",
            source_cutoff=datetime(2026, 7, 20, tzinfo=UTC),
            received_at=datetime(2026, 7, 20, tzinfo=UTC),
            closed=False,
            bar=MarketBar(
                open_at=datetime(2026, 7, 20, tzinfo=UTC),
                close_at=datetime(2026, 7, 20, 0, 1, tzinfo=UTC),
                open="100",
                high="102",
                low="99",
                close="101",
                volume="12.5",
            ),
        ),
    ),
)
def test_demo_app_rejects_cross_environment_stream_source(
    tmp_path: Path,
    cross_environment_event: MarketStreamStatus | MarketStreamQuote | MarketStreamBar,
) -> None:
    class LiveMarketStream(FakeMarketStream):
        def stream(self, instrument_ref: str):
            async def events():
                yield MarketStreamStatus(
                    state="LIVE",
                    source="BINANCE_DEMO_PUBLIC",
                    observed_at=datetime(2026, 7, 20, tzinfo=UTC),
                )
                yield cross_environment_event

            return events()

    client = make_client(
        tmp_path,
        market_stream_provider=LiveMarketStream(),
    )

    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(
            "/api/v1/market-stream?instrument_ref=BTCUSDT-PERP",
            headers={"host": "127.0.0.1:8765", "origin": ORIGIN},
        ) as websocket:
            websocket.receive_json()
            websocket.receive_json()

    assert caught.value.code == 1013
    assert caught.value.reason == "MARKET_SOURCE_ENVIRONMENT_MISMATCH"


def test_market_order_preview_uses_the_current_environment_server_quote(
    tmp_path: Path,
) -> None:
    client = make_client(
        tmp_path,
        market_context_provider=FakeMarketContext(),
        instrument_rules_provider=FakeInstrumentRules(),
    )
    token = csrf(client)

    response = client.post(
        "/api/v1/order-schedules/preview",
        headers={"Origin": ORIGIN, "X-CSRFToken": token},
        json={
            "schedule_ref": "demo-market-preview",
            "instrument_ref": "BTCUSDT-PERP",
            "direction": "LONG",
            "max_notional": "100",
            "reference_price": "999",
            "spec": {
                "entry_program": {"kind": "ONE_TIME"},
                "price_distribution": {"kind": "SINGLE"},
                "amount_distribution": {
                    "mode": "FIXED",
                    "base_notional": "10",
                },
                "venue_policy": {
                    "order_type": "MARKET",
                    "time_in_force": None,
                },
                "protection_policy": {
                    "initial_stop": {"distance_bps": "100"},
                    "time_exit_seconds": 3600,
                    "full_fill_loss_budget": {
                        "entry_fee_bps": "2",
                        "exit_fee_bps": "5",
                    },
                },
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["reference_price"] == "100.5"


@pytest.mark.parametrize("venue_ref", ("BINANCE", "BINANCE_USDM_DEMO"))
def test_order_schedule_preview_rejects_noncanonical_venue_ref(
    tmp_path: Path,
    venue_ref: str,
) -> None:
    client = make_client(tmp_path)
    token = csrf(client)

    response = client.post(
        "/api/v1/order-schedules/preview",
        headers={"Origin": ORIGIN, "X-CSRFToken": token},
        json={
            "schedule_ref": "invalid-venue-preview",
            "venue_ref": venue_ref,
            "instrument_ref": "BTCUSDT-PERP",
            "direction": "LONG",
            "max_notional": "100",
            "spec": {
                "price_distribution": {
                    "kind": "SINGLE",
                    "limit_price": "100",
                },
                "amount_distribution": {
                    "mode": "FIXED",
                    "base_notional": "10",
                },
                "venue_policy": {
                    "order_type": "LIMIT",
                    "time_in_force": "GTC",
                },
                "protection_policy": {
                    "initial_stop": {"distance_bps": "100"},
                },
            },
        },
    )

    assert response.status_code == 422
    assert any(
        detail["loc"] == ["body", "venue_ref"]
        for detail in response.json()["detail"]
    )


def test_market_order_preview_rejects_a_cross_environment_quote(
    tmp_path: Path,
) -> None:
    class LiveMarketContext(FakeMarketContext):
        async def fetch(
            self,
            instrument_ref: str,
            lookback: int,
            stop_reference_interval: MarketInterval = "15m",
        ) -> MarketContext:
            context = await super().fetch(
                instrument_ref,
                lookback,
                stop_reference_interval,
            )
            return context.model_copy(update={"source": "BINANCE_LIVE_PUBLIC"})

    client = make_client(
        tmp_path,
        market_context_provider=LiveMarketContext(),
        instrument_rules_provider=FakeInstrumentRules(),
    )
    token = csrf(client)

    response = client.post(
        "/api/v1/order-schedules/preview",
        headers={"Origin": ORIGIN, "X-CSRFToken": token},
        json={
            "schedule_ref": "cross-environment-market-preview",
            "instrument_ref": "BTCUSDT-PERP",
            "direction": "LONG",
            "max_notional": "100",
            "spec": {
                "entry_program": {"kind": "ONE_TIME"},
                "price_distribution": {"kind": "SINGLE"},
                "amount_distribution": {
                    "mode": "FIXED",
                    "base_notional": "10",
                },
                "venue_policy": {
                    "order_type": "MARKET",
                    "time_in_force": None,
                },
                "protection_policy": {
                    "initial_stop": {"distance_bps": "100"},
                    "time_exit_seconds": 3600,
                    "full_fill_loss_budget": {
                        "entry_fee_bps": "2",
                        "exit_fee_bps": "5",
                    },
                },
            },
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == {"code": "MARKET_SOURCE_ENVIRONMENT_MISMATCH"}


def test_order_preview_rejects_cross_environment_instrument_rules(
    tmp_path: Path,
) -> None:
    class LiveInstrumentRules(FakeInstrumentRules):
        async def fetch(self, instrument_ref: str) -> InstrumentOrderRules:
            rules = await super().fetch(instrument_ref)
            return rules.model_copy(update={"source": "BINANCE_LIVE_EXCHANGE_INFO"})

    client = make_client(
        tmp_path,
        instrument_rules_provider=LiveInstrumentRules(),
    )
    token = csrf(client)

    response = client.post(
        "/api/v1/order-schedules/preview",
        headers={"Origin": ORIGIN, "X-CSRFToken": token},
        json={
            "schedule_ref": "cross-environment-rules-preview",
            "instrument_ref": "BTCUSDT-PERP",
            "direction": "LONG",
            "max_notional": "100",
            "spec": {
                "entry_program": {"kind": "ONE_TIME"},
                "price_distribution": {
                    "kind": "SINGLE",
                    "limit_price": "100",
                },
                "amount_distribution": {
                    "mode": "FIXED",
                    "base_notional": "10",
                },
                "venue_policy": {
                    "order_type": "LIMIT",
                    "time_in_force": "GTC",
                },
                "protection_policy": {
                    "initial_stop": {"distance_bps": "100"},
                    "time_exit_seconds": 3600,
                    "full_fill_loss_budget": {
                        "entry_fee_bps": "2",
                        "exit_fee_bps": "5",
                    },
                },
            },
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "INSTRUMENT_RULES_SOURCE_ENVIRONMENT_MISMATCH"
    }


def test_order_schedule_preview_uses_server_rules_and_returns_all_legs(
    tmp_path: Path,
) -> None:
    client = make_client(
        tmp_path,
        instrument_rules_provider=FakeInstrumentRules(),
    )
    token = csrf(client)

    response = client.post(
        "/api/v1/order-schedules/preview",
        headers={"Origin": ORIGIN, "X-CSRFToken": token},
        json={
            "schedule_ref": "schedule-preview-1",
            "instrument_ref": "BTCUSDT-PERP",
            "direction": "LONG",
            "max_notional": "100",
            "spec": {
                "entry_program": {"kind": "PRICE_LADDER"},
                "price_distribution": {
                    "kind": "LADDER",
                    "lower_price": "99",
                    "upper_price": "100",
                    "level_count": 5,
                },
                "amount_distribution": {
                    "mode": "FIXED",
                    "base_notional": "10",
                },
                "venue_policy": {
                    "order_type": "LIMIT",
                    "time_in_force": "GTC",
                    "post_only": True,
                },
                "protection_policy": {
                    "initial_stop": {"distance_bps": "100"},
                    "time_exit_seconds": 3600,
                    "full_fill_loss_budget": {
                        "entry_fee_bps": "2",
                        "exit_fee_bps": "5",
                    },
                },
            },
        },
    )

    assert response.status_code == 200
    preview = response.json()
    assert preview["valid"] is True
    assert [leg["price"] for leg in preview["legs"]] == [
        "99",
        "99.2",
        "99.5",
        "99.7",
        "100",
    ]
    assert preview["instrument_rules"]["source"] == "BINANCE_DEMO_EXCHANGE_INFO"


def test_order_schedule_preview_checks_gtd_against_request_time(
    tmp_path: Path,
) -> None:
    client = make_client(
        tmp_path,
        instrument_rules_provider=FakeInstrumentRules(),
    )
    token = csrf(client)
    expire_at = datetime.now(UTC) + timedelta(minutes=5)

    response = client.post(
        "/api/v1/order-schedules/preview",
        headers={"Origin": ORIGIN, "X-CSRFToken": token},
        json={
            "schedule_ref": "gtd-request-time-preview",
            "instrument_ref": "BTCUSDT-PERP",
            "direction": "LONG",
            "max_notional": "100",
            "spec": {
                "entry_program": {"kind": "ONE_TIME"},
                "price_distribution": {
                    "kind": "SINGLE",
                    "limit_price": "100",
                },
                "amount_distribution": {
                    "mode": "FIXED",
                    "base_notional": "10",
                },
                "venue_policy": {
                    "order_type": "LIMIT",
                    "time_in_force": "GTD",
                    "expire_at": expire_at.isoformat(),
                },
                "protection_policy": {
                    "initial_stop": {"distance_bps": "100"},
                    "time_exit_seconds": 3600,
                    "full_fill_loss_budget": {
                        "entry_fee_bps": "2",
                        "exit_fee_bps": "5",
                    },
                },
            },
        },
    )

    assert response.status_code == 200
    preview = response.json()
    assert preview["valid"] is False
    assert {issue["code"] for issue in preview["issues"]} == {
        "GTD_EXPIRY_TOO_SOON"
    }


@pytest.mark.parametrize(
    ("entry_program", "expected_code"),
    (
        (None, "DIRECT_EXECUTION_ENTRY_PROGRAM_REQUIRED"),
        ({"kind": "ONE_TIME"}, "DIRECT_EXECUTION_AUTOMATIC_EXIT_REQUIRED"),
    ),
)
def test_new_direct_preview_requires_current_creation_contract(
    tmp_path: Path,
    entry_program: dict[str, str] | None,
    expected_code: str,
) -> None:
    class RulesMustNotBeFetched:
        async def fetch(self, _instrument_ref: str) -> InstrumentOrderRules:
            pytest.fail("invalid new-plan contract must fail before rules lookup")

    client = make_client(
        tmp_path,
        instrument_rules_provider=RulesMustNotBeFetched(),
    )
    token = csrf(client)
    spec = {
        "price_distribution": {
            "kind": "SINGLE",
            "limit_price": "100",
        },
        "amount_distribution": {
            "mode": "FIXED",
            "base_notional": "10",
        },
        "venue_policy": {
            "order_type": "LIMIT",
            "time_in_force": "GTC",
        },
        "protection_policy": {
            "initial_stop": {"distance_bps": "100"},
        },
    }
    if entry_program is not None:
        spec["entry_program"] = entry_program

    response = client.post(
        "/api/v1/order-schedules/preview",
        headers={"Origin": ORIGIN, "X-CSRFToken": token},
        json={
            "schedule_ref": "current-contract-preview",
            "instrument_ref": "BTCUSDT-PERP",
            "direction": "LONG",
            "max_notional": "100",
            "spec": spec,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": expected_code}


@pytest.mark.parametrize(
    ("decision_basis_kind", "submission_mode", "expected_code"),
    (
        (
            "DIRECT_EXECUTION",
            "PREPROTECTED_PARALLEL",
            "PREPROTECTED_PARALLEL_NOT_VERIFIED",
        ),
        (
            "STRATEGY_SIGNAL",
            "SERIAL_PROTECTED",
            "STRATEGY_ORDER_SCHEDULE_NOT_SUPPORTED",
        ),
    ),
)
def test_order_schedule_preview_uses_the_persisted_capability_catalog(
    tmp_path: Path,
    decision_basis_kind: str,
    submission_mode: str,
    expected_code: str,
) -> None:
    class RulesMustNotBeFetched:
        async def fetch(self, _instrument_ref: str) -> InstrumentOrderRules:
            pytest.fail("unsupported schedule must be rejected before rules lookup")

    client = make_client(
        tmp_path,
        instrument_rules_provider=RulesMustNotBeFetched(),
    )
    token = csrf(client)

    response = client.post(
        "/api/v1/order-schedules/preview",
        headers={"Origin": ORIGIN, "X-CSRFToken": token},
        json={
            "schedule_ref": "unsupported-schedule-preview",
            "decision_basis_kind": decision_basis_kind,
            "instrument_ref": "BTCUSDT-PERP",
            "direction": "LONG",
            "max_notional": "100",
            "spec": {
                "price_distribution": {
                    "kind": "SINGLE",
                    "limit_price": "100",
                },
                "amount_distribution": {
                    "mode": "FIXED",
                    "base_notional": "10",
                },
                "venue_policy": {
                    "order_type": "LIMIT",
                    "time_in_force": "GTC",
                },
                "submission_mode": submission_mode,
                "protection_policy": {
                    "initial_stop": {"distance_bps": "100"},
                },
            },
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": expected_code}


def test_csrf_host_origin_and_authorization_boundaries(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    token = csrf(client)
    endpoint = "/api/v1/settings/test-email"

    assert client.post(endpoint, headers={"Origin": ORIGIN}).status_code == 403
    assert client.post(
        endpoint,
        headers={"Origin": "http://example.test", "X-CSRFToken": token},
    ).status_code == 403
    assert client.post(endpoint, headers={"X-CSRFToken": token}).status_code == 403
    assert client.post(
        endpoint,
        headers={"Origin": ORIGIN, "X-CSRFToken": token},
    ).status_code == 409
    assert client.get("/operations", headers={"Host": "example.test"}).status_code == 400
    bearer = client.get("/operations", headers={"Authorization": "Bearer forbidden"})
    assert bearer.status_code == 400
    assert bearer.json()["detail"]["code"] == "AUTHORIZATION_HEADER_FORBIDDEN"


def test_programmatic_monitor_uses_the_full_plan_create_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def save_new_plan(
        _self,
        payload,
        *,
        idempotency_key: str,
        observed_at: datetime,
    ) -> dict[str, Any]:
        captured.update(
            payload=payload,
            idempotency_key=idempotency_key,
            observed_at=observed_at,
        )
        raise ValueError("PROGRAMMATIC_CREATE_REACHED")

    monkeypatch.setattr(PostgreSQLPlanningApi, "save_new_plan", save_new_plan)
    client = make_client(tmp_path)
    csrf(client)  # Retain a browser cookie to prove the native channel skips CSRF.

    response = client.post(
        "/api/v1/plans",
        headers={
            "X-Halpha-Caller": "MONITOR",
            "Idempotency-Key": "monitor:signal-001:create",
        },
        json=_programmatic_direct_plan_payload(),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "PROGRAMMATIC_CREATE_REACHED"}
    assert captured["idempotency_key"] == "monitor:signal-001:create"
    payload = captured["payload"]
    assert payload.creator_kind.value == "MONITOR"
    assert payload.decision_basis.kind.value == "DIRECT_EXECUTION"
    assert payload.order_schedule_spec.entry_program.kind.value == "ONE_TIME"
    assert payload.order_schedule_spec.protection_policy.time_exit_seconds == 3600


def test_plan_creation_source_must_match_the_transport_channel(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    programmatic = client.post(
        "/api/v1/plans",
        headers={
            "X-Halpha-Caller": "MONITOR",
            "Idempotency-Key": "monitor:source-mismatch:create",
        },
        json=_programmatic_direct_plan_payload(creator_kind="AI"),
    )
    token = csrf(client)
    workbench = client.post(
        "/api/v1/plans",
        headers={
            "Origin": ORIGIN,
            "X-CSRFToken": token,
            "Idempotency-Key": "workbench:source-mismatch:create",
        },
        json=_programmatic_direct_plan_payload(),
    )

    assert programmatic.status_code == 403
    assert programmatic.json()["detail"] == {
        "code": "PROGRAMMATIC_CREATOR_KIND_MISMATCH"
    }
    assert workbench.status_code == 403
    assert workbench.json()["detail"] == {
        "code": "MONITOR_CREATOR_REQUIRES_PROGRAMMATIC_CALLER"
    }


@pytest.mark.parametrize(
    ("headers", "path", "expected_code"),
    (
        (
            {"X-Halpha-Caller": "UNKNOWN"},
            "/api/v1/plans",
            "PROGRAMMATIC_CALLER_INVALID",
        ),
        (
            {"X-Halpha-Caller": "MONITOR", "Origin": ORIGIN},
            "/api/v1/plans",
            "PROGRAMMATIC_BROWSER_ORIGIN_FORBIDDEN",
        ),
        (
            {"X-Halpha-Caller": "MONITOR"},
            "/api/v1/settings/test-email",
            "PROGRAMMATIC_API_SCOPE_FORBIDDEN",
        ),
    ),
)
def test_programmatic_monitor_channel_rejects_invalid_source_or_scope(
    tmp_path: Path,
    headers: dict[str, str],
    path: str,
    expected_code: str,
) -> None:
    client = make_client(tmp_path)

    response = client.post(path, headers=headers)

    assert response.status_code == 403
    assert response.json()["detail"] == {"code": expected_code}


def test_openapi_marks_the_shared_programmatic_plan_operations(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    document = client.get("/api/v1/openapi.json").json()
    expected_operations = (
        ("/api/v1/account-position-operations/preview", "post"),
        ("/api/v1/order-schedules/preview", "post"),
        ("/api/v1/plans", "post"),
        ("/api/v1/plans/{plan_id}", "put"),
        ("/api/v1/plans/{plan_id}", "delete"),
        ("/api/v1/plans/{plan_id}/submit-and-start", "post"),
        ("/api/v1/plan-versions/{plan_version_id}/activation-preview", "post"),
        ("/api/v1/activations", "post"),
    )

    for path, method in expected_operations:
        operation = document["paths"][path][method]
        assert operation["x-halpha-programmatic-caller"] == "MONITOR"
        assert {
            "$ref": "#/components/parameters/HalphaProgrammaticCaller"
        } in operation["parameters"]
    assert "x-halpha-programmatic-caller" not in document["paths"][
        "/api/v1/settings/test-email"
    ]["post"]
    assert document["components"]["schemas"]["PlanCreatorKind"]["enum"] == [
        "HUMAN",
        "AI",
        "MONITOR",
    ]


def test_programmatic_activation_keeps_the_same_executor_readiness_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activation_replay",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activate",
        lambda *_args, **_kwargs: pytest.fail("activation must not be created"),
    )
    client = make_client(
        tmp_path,
        projection=FakeProjection(executor_status="UNAVAILABLE"),
    )

    response = client.post(
        "/api/v1/activations",
        headers={
            "X-Halpha-Caller": "MONITOR",
            "Idempotency-Key": "monitor:signal-001:activate",
        },
        json={
            "plan_version_id": "plan-version-monitor-001",
            "expected_schedule_digest": None,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "EXECUTOR_NOT_READY"}


@pytest.mark.parametrize(
    "endpoint",
    (
        "/api/v1/activations/not-a-uuid",
        "/api/v1/activations/not-a-uuid/timeline",
        "/api/v1/activations/not-a-uuid/system-stop-release-preview",
    ),
)
def test_activation_reads_reject_invalid_uuid_before_database_access(
    tmp_path: Path,
    endpoint: str,
) -> None:
    client = make_client(tmp_path)

    response = client.get(endpoint)

    assert response.status_code == 422
    assert any(
        detail["loc"] == ["path", "activation_id"]
        for detail in response.json()["detail"]
    )


@pytest.mark.parametrize("idempotency_key", ("", "contains whitespace", "x" * 161))
def test_mutation_rejects_invalid_idempotency_header_before_database_access(
    tmp_path: Path,
    idempotency_key: str,
) -> None:
    client = make_client(tmp_path)
    token = csrf(client)

    response = client.post(
        "/api/v1/plans",
        headers={
            "Origin": ORIGIN,
            "X-CSRFToken": token,
            "Idempotency-Key": idempotency_key,
        },
        json={
            "plan_name": "幂等键边界检查",
            "creator_kind": "AI",
            "decision_basis": {
                "kind": "STRATEGY_SIGNAL",
                "decision_basis_ref": "ONE_SHOT_DONCHIAN_ATR_BREAKOUT",
                "parameters": {},
            },
            "instrument_ref": "BTCUSDT-PERP",
            "direction": "LONG",
            "target_exposure": "100",
            "max_margin": "100",
            "max_notional": "500",
            "max_allowed_loss": "10",
            "valid_minutes": 15,
        },
    )

    assert response.status_code == 422
    assert any(
        detail["loc"] == ["header", "Idempotency-Key"]
        for detail in response.json()["detail"]
    )


@pytest.mark.parametrize("venue_ref", ("BINANCE", "BINANCE_USDM_DEMO"))
def test_plan_mutation_rejects_noncanonical_venue_ref_before_database_access(
    tmp_path: Path,
    venue_ref: str,
) -> None:
    client = make_client(tmp_path)
    token = csrf(client)

    response = client.post(
        "/api/v1/plans",
        headers={
            "Origin": ORIGIN,
            "X-CSRFToken": token,
            "Idempotency-Key": f"invalid-venue-{venue_ref}",
        },
        json={
            "plan_name": "场所身份边界检查",
            "creator_kind": "AI",
            "decision_basis": {
                "kind": "STRATEGY_SIGNAL",
                "decision_basis_ref": "ONE_SHOT_DONCHIAN_ATR_BREAKOUT",
                "parameters": {},
            },
            "venue_ref": venue_ref,
            "instrument_ref": "BTCUSDT-PERP",
            "direction": "LONG",
            "target_exposure": "100",
            "max_margin": "100",
            "max_notional": "500",
            "max_allowed_loss": "10",
            "valid_minutes": 15,
        },
    )

    assert response.status_code == 422
    assert any(
        detail["loc"] == ["body", "venue_ref"]
        for detail in response.json()["detail"]
    )


@pytest.mark.parametrize(
    ("entry_program", "expected_code"),
    (
        (None, "DIRECT_EXECUTION_ENTRY_PROGRAM_REQUIRED"),
        ({"kind": "ONE_TIME"}, "DIRECT_EXECUTION_AUTOMATIC_EXIT_REQUIRED"),
    ),
)
def test_new_direct_plan_rejects_legacy_creation_shortcuts(
    tmp_path: Path,
    entry_program: dict[str, str] | None,
    expected_code: str,
) -> None:
    client = make_client(tmp_path)
    token = csrf(client)
    schedule = {
        "price_distribution": {
            "kind": "SINGLE",
            "limit_price": "100",
        },
        "amount_distribution": {
            "mode": "FIXED",
            "base_notional": "100",
        },
        "venue_policy": {
            "order_type": "LIMIT",
            "time_in_force": "GTC",
        },
        "protection_policy": {
            "initial_stop": {"distance_bps": "100"},
        },
    }
    if entry_program is not None:
        schedule["entry_program"] = entry_program

    response = client.post(
        "/api/v1/plans",
        headers={
            "Origin": ORIGIN,
            "X-CSRFToken": token,
            "Idempotency-Key": f"invalid-direct-contract-{expected_code}",
        },
        json={
            "plan_name": "直接执行新契约边界",
            "creator_kind": "AI",
            "decision_context": {
                "rationale": "Exercise the intended direct-contract boundary.",
                "evidence": "The fixture isolates server contract validation.",
                "limitations": "This request is not submitted to an exchange.",
                "intent": "VALIDATION",
                "setup_family": "OTHER",
                "playbook_ref": "DIRECT_CONTRACT_VALIDATION_V1",
                "invalidation": "Do not continue beyond this bounded contract check.",
                "evidence_cutoff": "2026-07-20T00:00:00+00:00",
            },
            "decision_basis": {
                "kind": "DIRECT_EXECUTION",
                "decision_basis_ref": "DIRECT_EXECUTION@1",
                "parameters": {},
            },
            "order_schedule_spec": schedule,
            "instrument_ref": "BTCUSDT-PERP",
            "direction": "LONG",
            "target_exposure": "100",
            "max_margin": "100",
            "max_notional": "100",
            "max_allowed_loss": "100",
            "valid_minutes": 15,
        },
    )

    assert response.status_code == 422
    assert any(
        expected_code in detail["msg"]
        for detail in response.json()["detail"]
    )


def test_operations_remains_usable_without_static_dist_or_password(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)

    operations = client.get("/operations")
    script = client.get("/operations.js")
    missing_static = client.get("/overview", follow_redirects=False)

    assert operations.status_code == 200
    assert "故障接管" in operations.text
    assert "当前环境没有未结束的策略运行" in operations.text
    assert "命令被接受不代表 Binance 已经撤单、保护或平仓" in operations.text
    assert 'href="https://demo.binance.com/"' in operations.text
    assert "password" not in operations.text.lower()
    assert "sign out" not in operations.text.lower()
    assert csrf_cookie_name(8765) in client.cookies
    assert script.status_code == 200
    assert "Idempotency-Key" in script.text
    assert "stop-new-risk" in script.text
    assert "RESUME_ACTIVATION" not in script.text
    assert "password" not in script.text.lower()
    assert missing_static.status_code == 503
    assert client.get("/login").status_code == 404


def test_scalping_route_serves_the_spa_shell(tmp_path: Path) -> None:
    static_dist = tmp_path / "dist"
    static_dist.mkdir()
    (static_dist / "index.html").write_text(
        "<!doctype html><title>Halpha scalping</title>",
        encoding="utf-8",
    )

    response = make_client(tmp_path, static_dist=static_dist).get("/scalping")

    assert response.status_code == 200
    assert "Halpha scalping" in response.text


def test_operations_projects_only_core_fallback_facts_and_controls(
    tmp_path: Path,
) -> None:
    activation = {
        "activation_id": "f75cb14b-73df-4da4-9f21-3c2d4d28cad1",
        "account_ref": "demo-account",
        "instrument_ref": "BTCUSDT-PERP",
        "direction": "LONG",
        "lifecycle": "RUNNING",
        "run_state": "PAUSED",
        "pause_reason": "WRITER_CONTINUITY_LOST",
        "state_version": 2,
        "protection_state": "WORKING",
        "latest_venue_cutoff": "2026-07-17T00:00:00+00:00",
        "plan_name": "BTC 跌破动量",
        "has_entry_fill": True,
        "entry_opportunity_consumed": True,
        "entry_valid_until": "2026-07-16T23:00:00+00:00",
        "stopped_categories": ["NEW_RISK"],
    }
    client = make_client(
        tmp_path,
        projection=FakeProjection(activations=[activation]),
    )

    operations = client.get("/operations")

    assert operations.status_code == 200
    assert "BTC 跌破动量" in operations.text
    assert "自动执行已暂停" in operations.text
    assert "保护订单工作中" in operations.text
    assert "WRITER_CONTINUITY_LOST" in operations.text
    assert "<summary>诊断信息</summary>" in operations.text
    assert 'data-activation-label="BTC 跌破动量"' in operations.text
    assert "新增风险</dt><dd class=\"stopped\">已停止新开仓" in operations.text
    assert operations.text.count('class="control-button"') == 3
    assert 'data-intent="STOP_NEW_RISK"' in operations.text
    assert 'data-intent="EXIT_STRATEGY"' in operations.text
    assert 'data-intent="USER_TAKEOVER"' in operations.text
    assert 'data-intent="RESUME_ACTIVATION"' not in operations.text
    assert "2026-07-17 08:00:00 UTC+8" in operations.text
    assert "<table" not in operations.text
    assert "show-more" not in operations.text


def test_operations_explains_waiting_plan_without_prominent_internal_states(
    tmp_path: Path,
) -> None:
    activation = {
        "activation_id": "31307570-43fc-5812-8fa3-e7297dfc6039",
        "account_ref": "demo-account",
        "instrument_ref": "BTCUSDT-PERP",
        "direction": "SHORT",
        "lifecycle": "RUNNING",
        "run_state": "ACTIVE",
        "pause_reason": None,
        "state_version": 7,
        "protection_state": "NONE",
        "latest_venue_cutoff": None,
        "plan_name": "Goal-07 BTC 15m 跌破动量做空",
        "has_entry_fill": False,
        "entry_opportunity_consumed": False,
        "entry_valid_until": "2026-07-27T05:07:23+08:00",
        "stopped_categories": [],
    }
    client = make_client(
        tmp_path,
        projection=FakeProjection(activations=[activation]),
    )

    operations = client.get("/operations")

    assert operations.status_code == 200
    assert "Goal-07 BTC 15m 跌破动量做空" in operations.text
    assert "BTCUSDT-PERP · 做空" in operations.text
    assert "等待入场" in operations.text
    assert "未入场，无需保护" in operations.text
    assert "2026-07-27 05:07:23 UTC+8" in operations.text
    assert "<code>31307570-43fc-5812-8fa3-e7297dfc6039</code>" in operations.text
    assert "<summary>诊断信息</summary>" in operations.text


def test_unavailable_database_is_truthful_and_fail_closed(tmp_path: Path) -> None:
    client = make_client(tmp_path, projection=FakeProjection(available=False))

    overview = client.get("/api/v1/overview")
    status = client.get("/api/v1/settings/status")
    operations = client.get("/operations")

    assert overview.status_code == 503
    assert overview.json()["detail"]["code"] == "DATABASE_FACTS_UNAVAILABLE"
    assert status.status_code == 200
    assert status.json()["database_available"] is False
    assert status.json()["database_reason_code"] == "DATABASE_UNAVAILABLE"
    assert "数据库</dt><dd>不可用" in operations.text
    assert "所有控制保持关闭" in operations.text


def test_security_headers_and_openapi_have_no_auth_contract(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.get("/operations")
    schema = client.get("/api/v1/openapi.json").json()

    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["referrer-policy"] == "same-origin"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "/operations" not in schema["paths"]
    assert all("/session/" not in path for path in schema["paths"])
    serialized = str(schema).lower()
    assert "owner_password" not in serialized
    assert "sessionresponse" not in serialized
    assert "database-test-secret" not in serialized


@pytest.mark.parametrize(
    ("conflict", "code"),
    (
        (CapitalConflict("ACCOUNT_LIMIT_EXCEEDED"), "ACCOUNT_LIMIT_EXCEEDED"),
        (CommandConflict("IDEMPOTENCY_CONTENT_CONFLICT"), "IDEMPOTENCY_CONTENT_CONFLICT"),
    ),
)
def test_domain_conflicts_are_stable_http_409(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conflict: RuntimeError,
    code: str,
) -> None:
    def raise_conflict(_self: PostgreSQLPlanningApi, _plan_id: str) -> dict[str, Any]:
        raise conflict

    monkeypatch.setattr(PostgreSQLPlanningApi, "get_plan", raise_conflict)
    response = make_client(tmp_path).get("/api/v1/plans/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == code


def test_validation_errors_are_sanitized_to_one_stable_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_validation(_self: PostgreSQLPlanningApi, _plan_id: str) -> dict[str, Any]:
        OneShotParameters(
            direction=Direction.LONG,
            take_profit_1_r="0.75",
            take_profit_2_r="1.5",
        )
        raise AssertionError("validation should have failed")

    monkeypatch.setattr(PostgreSQLPlanningApi, "get_plan", raise_validation)
    response = make_client(tmp_path).get(
        "/api/v1/plans/00000000-0000-0000-0000-000000000000"
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "PARAMETER_OUT_OF_RANGE"}


def test_activation_preview_projects_executor_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activation_preview",
        lambda _self, plan_version_id: {
            **_direct_activation_preview(plan_version_id),
            "runtime_compatible": False,
            "runtime_incompatibility_reason": "FIXTURE_PREVIEW_ONLY",
        },
    )
    client = make_client(
        tmp_path,
        projection=FakeProjection(executor_status="STARTING"),
    )
    token = csrf(client)

    response = client.post(
        "/api/v1/plan-versions/plan-version-001/activation-preview",
        headers={"Origin": ORIGIN, "X-CSRFToken": token},
    )

    assert response.status_code == 200
    assert response.json()["executor_status"] == "STARTING"
    assert response.json()["executor_status_checked_at"] == "2026-07-17T00:00:01Z"


def test_activation_rejects_before_mutation_when_executor_is_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activate",
        lambda *_args, **_kwargs: pytest.fail("activation must not be created"),
    )
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activation_replay",
        lambda *_args, **_kwargs: None,
    )
    client = make_client(
        tmp_path,
        projection=FakeProjection(executor_status="UNAVAILABLE"),
    )
    token = csrf(client)

    response = client.post(
        "/api/v1/activations",
        json={"plan_version_id": "plan-version-001"},
        headers={
            "Origin": ORIGIN,
            "X-CSRFToken": token,
            "Idempotency-Key": "executor-unavailable-001",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "EXECUTOR_NOT_READY"}


def test_submit_and_start_creates_only_one_final_activation_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "submit_and_start_replay",
        lambda *_args, **_kwargs: None,
    )

    def preview(
        _self: PostgreSQLPlanningApi,
        plan_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        captured["preview_plan_id"] = plan_id
        captured["preview_expected_version"] = kwargs["expected_version"]
        return _direct_activation_preview("run-snapshot-001")

    def submit(
        _self: PostgreSQLPlanningApi,
        plan_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        captured["submit_plan_id"] = plan_id
        captured["submit_expected_version"] = kwargs["expected_version"]
        captured["schedule"] = kwargs["order_schedule_snapshot"]
        return {
            "activation": _mock_activation("activation-submit-and-start-001"),
            "venue_write_created": False,
            "runtime_real_write_gate": "CLOSED",
        }

    monkeypatch.setattr(PostgreSQLPlanningApi, "submit_and_start_preview", preview)
    monkeypatch.setattr(PostgreSQLPlanningApi, "submit_and_start", submit)
    client = make_client(tmp_path, instrument_rules_provider=FakeInstrumentRules())
    token = csrf(client)

    response = client.post(
        "/api/v1/plans/draft-submit-and-start-001/submit-and-start",
        headers={
            "Origin": ORIGIN,
            "X-CSRFToken": token,
            "Idempotency-Key": "submit-and-start-001",
            "If-Match": "1",
        },
    )

    assert response.status_code == 201
    assert response.json()["activation"]["activation_id"] == (
        "activation-submit-and-start-001"
    )
    assert captured["preview_plan_id"] == "draft-submit-and-start-001"
    assert captured["submit_plan_id"] == "draft-submit-and-start-001"
    assert captured["preview_expected_version"] == 1
    assert captured["submit_expected_version"] == 1
    assert captured["schedule"].valid is True


def test_submit_and_start_rejection_does_not_call_the_final_persistence_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "submit_and_start_replay",
        lambda *_args, **_kwargs: None,
    )

    def reject_preview(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise ValueError("NEW_RISK_DAILY_LOSS_STOP_REACHED")

    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "submit_and_start_preview",
        reject_preview,
    )
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "submit_and_start",
        lambda *_args, **_kwargs: pytest.fail(
            "rejected submission must not persist a run snapshot or activation"
        ),
    )
    client = make_client(tmp_path)
    token = csrf(client)

    response = client.post(
        "/api/v1/plans/draft-rejected-001/submit-and-start",
        headers={
            "Origin": ORIGIN,
            "X-CSRFToken": token,
            "Idempotency-Key": "submit-and-start-rejected-001",
            "If-Match": "1",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "NEW_RISK_DAILY_LOSS_STOP_REACHED"
    }


def test_activation_rejects_a_semantically_incompatible_fixed_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activation_replay",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activation_preview",
        lambda *_args, **_kwargs: {
            "plan_version_id": "plan-version-incompatible",
            "runtime_compatible": False,
            "runtime_incompatibility_reason": "PLAN_STRATEGY_RUNTIME_INCOMPATIBLE",
        },
    )
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activate",
        lambda *_args, **_kwargs: pytest.fail("incompatible plan must not activate"),
    )
    client = make_client(tmp_path)
    token = csrf(client)

    response = client.post(
        "/api/v1/activations",
        json={"plan_version_id": "plan-version-incompatible"},
        headers={
            "Origin": ORIGIN,
            "X-CSRFToken": token,
            "Idempotency-Key": "incompatible-plan-001",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "PLAN_STRATEGY_RUNTIME_INCOMPATIBLE"
    }


def test_activation_replays_committed_result_before_current_readiness_or_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replay = {
        "activation": _mock_activation("activation-existing-001"),
        "venue_write_created": False,
        "runtime_real_write_gate": "CLOSED",
    }
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activation_replay",
        lambda *_args, **_kwargs: replay,
    )
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activation_preview",
        lambda *_args, **_kwargs: pytest.fail("replay must not refresh the preview"),
    )
    client = make_client(
        tmp_path,
        projection=FakeProjection(executor_status="UNAVAILABLE"),
    )
    token = csrf(client)

    response = client.post(
        "/api/v1/activations",
        json={
            "plan_version_id": "plan-version-001",
            "expected_schedule_digest": None,
        },
        headers={
            "Origin": ORIGIN,
            "X-CSRFToken": token,
            "Idempotency-Key": "activation-replay-001",
        },
    )

    assert response.status_code == 201
    assert response.json()["activation"]["activation_id"] == (
        "activation-existing-001"
    )
    assert response.json()["venue_write_created"] is False
    assert response.json()["runtime_real_write_gate"] == "CLOSED"


def _direct_activation_preview(plan_version_id: str) -> dict[str, Any]:
    return {
        "plan_version_id": plan_version_id,
        "plan_name": "Direct activation contract fixture",
        "created_at": "2026-07-17T00:00:00+00:00",
        "creator_kind": "HUMAN",
        "environment_id": "binance-demo-primary",
        "environment_kind": "DEMO",
        "authority_class": "SIMULATION",
        "account_ref": "binance-usdm-demo-owner-primary",
        "runtime_compatible": True,
        "runtime_incompatibility_reason": None,
        "decision_basis": {
            "kind": "DIRECT_EXECUTION",
            "decision_basis_ref": "DIRECT_EXECUTION@1",
        },
        "decision_basis_kind": "DIRECT_EXECUTION",
        "decision_basis_ref": "DIRECT_EXECUTION@1",
        "strategy_ref": None,
        "parameter_digest": "0" * 64,
        "strategy_parameters": {},
        "venue_ref": "BINANCE_USDM",
        "instrument_ref": "BTCUSDT-PERP",
        "direction": "LONG",
        "trade_amount": "100",
        "limits": {
            "max_margin": "100",
            "max_notional": "100",
            "max_allowed_loss": "10",
        },
        "valid_until": "2026-07-18T00:00:00+00:00",
        "allowed_actions": ["ENTER", "PROTECT", "EXIT"],
        "actual_account_configuration": (
            "PRE_SUBMIT_FACT_NOT_REQUIRED_FOR_PLAN_ACTIVATION"
        ),
        "account_mode_policy": (
            "NEW_RISK_SUPPORTS_ONE_WAY_SINGLE_ASSET_CROSSED_OR_ISOLATED"
        ),
        "product_build_id": "1" * 64,
        "product_build_consistent": True,
        "configured_runtime_real_write_gate": "CLOSED",
        "runtime_real_write_gate": "CLOSED",
        "live_activation_eligible": False,
        "live_profit_qualification": {
            "status": "NOT_APPLICABLE_DEMO",
            "required": False,
            "eligible_input": False,
            "blocker_codes": [],
        },
        "capital_notice": "Fixture notice.",
        "order_schedule_spec": {
            "price_distribution": {
                "kind": "SINGLE",
                "limit_price": "100",
            },
            "amount_distribution": {
                "mode": "FIXED",
                "base_notional": "10",
            },
            "venue_policy": {
                "order_type": "LIMIT",
                "time_in_force": "GTC",
            },
            "protection_policy": {
                "initial_stop": {"distance_bps": "100"},
            },
        },
        "position_alignment": None,
        "position_alignment_ready": None,
        "position_alignment_blocker": None,
    }


def test_activation_uses_the_exact_order_schedule_snapshot_from_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_version_id = "direct-plan-version-001"
    captured: dict[str, Any] = {}
    instrument_rules = FakeInstrumentRules()
    instrument_rules_calls = 0
    fetch_instrument_rules = instrument_rules.fetch

    async def counted_fetch(instrument_ref: str) -> InstrumentOrderRules:
        nonlocal instrument_rules_calls
        instrument_rules_calls += 1
        rules = await fetch_instrument_rules(instrument_ref)
        if instrument_rules_calls == 2:
            return rules.model_copy(
                update={"source_cutoff": "2026-07-23T00:01:00+00:00"}
            )
        return rules

    monkeypatch.setattr(instrument_rules, "fetch", counted_fetch)
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activation_preview",
        lambda _self, requested_id: _direct_activation_preview(requested_id),
    )
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activation_replay",
        lambda *_args, **_kwargs: None,
    )

    def activate(
        _self: PostgreSQLPlanningApi,
        _payload: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        captured["snapshot"] = kwargs["order_schedule_snapshot"]
        return {
            "activation": _mock_activation("activation-direct-001"),
            "venue_write_created": False,
            "runtime_real_write_gate": "CLOSED",
        }

    monkeypatch.setattr(PostgreSQLPlanningApi, "activate", activate)
    client = make_client(
        tmp_path,
        instrument_rules_provider=instrument_rules,
    )
    token = csrf(client)
    headers = {"Origin": ORIGIN, "X-CSRFToken": token}

    preview_response = client.post(
        f"/api/v1/plan-versions/{plan_version_id}/activation-preview",
        headers=headers,
    )
    assert preview_response.status_code == 200
    preview_snapshot = preview_response.json()["order_schedule_snapshot"]

    activation_response = client.post(
        "/api/v1/activations",
        json={
            "plan_version_id": plan_version_id,
            "expected_schedule_digest": preview_response.json()[
                "expected_schedule_digest"
            ],
        },
        headers={
            **headers,
            "Idempotency-Key": "activation-direct-preview-hit-001",
        },
    )

    assert activation_response.status_code == 201
    assert instrument_rules_calls == 2
    assert captured["snapshot"].schedule_digest == preview_snapshot["schedule_digest"]
    assert captured["snapshot"].source_cutoff == "2026-07-23T00:01:00+00:00"
    assert (
        captured["snapshot"].instrument_rules.source_cutoff
        == "2026-07-23T00:01:00+00:00"
    )


def test_direct_activation_rejects_changed_current_instrument_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_version_id = "direct-plan-version-rule-change"

    class ChangingInstrumentRules(FakeInstrumentRules):
        def __init__(self) -> None:
            self.calls = 0

        async def fetch(self, instrument_ref: str) -> InstrumentOrderRules:
            self.calls += 1
            rules = await super().fetch(instrument_ref)
            if self.calls == 2:
                return rules.model_copy(
                    update={
                        "price_tick_size": "1",
                        "source_cutoff": "2026-07-23T00:01:00+00:00",
                    }
                )
            return rules

    instrument_rules = ChangingInstrumentRules()
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activation_preview",
        lambda _self, requested_id: _direct_activation_preview(requested_id),
    )
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activation_replay",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activate",
        lambda *_args, **_kwargs: pytest.fail("changed rules must not activate"),
    )
    client = make_client(
        tmp_path,
        instrument_rules_provider=instrument_rules,
    )
    token = csrf(client)
    headers = {"Origin": ORIGIN, "X-CSRFToken": token}
    preview_response = client.post(
        f"/api/v1/plan-versions/{plan_version_id}/activation-preview",
        headers=headers,
    )
    assert preview_response.status_code == 200

    activation_response = client.post(
        "/api/v1/activations",
        json={
            "plan_version_id": plan_version_id,
            "expected_schedule_digest": preview_response.json()[
                "expected_schedule_digest"
            ],
        },
        headers={
            **headers,
            "Idempotency-Key": "activation-direct-rule-change-001",
        },
    )

    assert activation_response.status_code == 409
    assert activation_response.json()["detail"] == {
        "code": "ACTIVATION_PREVIEW_STALE"
    }
    assert instrument_rules.calls == 2


def test_direct_activation_keeps_the_confirmed_market_sizing_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_version_id = "direct-plan-version-reference-change"

    class ChangingMarketContext(FakeMarketContext):
        def __init__(self) -> None:
            self.calls = 0

        async def fetch(
            self,
            instrument_ref: str,
            lookback: int,
            stop_reference_interval: MarketInterval = "15m",
        ) -> MarketContext:
            self.calls += 1
            context = await super().fetch(
                instrument_ref,
                lookback,
                stop_reference_interval,
            )
            if self.calls == 2:
                return context.model_copy(
                    update={
                        "bid_price": "101",
                        "ask_price": "102",
                        "reference_price": "101.5",
                        "source_cutoff": datetime(2026, 7, 20, 0, 1, tzinfo=UTC),
                    }
                )
            return context

    def preview(_self: PostgreSQLPlanningApi, requested_id: str) -> dict[str, Any]:
        result = _direct_activation_preview(requested_id)
        result["order_schedule_spec"] = {
            **result["order_schedule_spec"],
            "price_distribution": {"kind": "SINGLE"},
            "venue_policy": {
                "order_type": "MARKET",
                "time_in_force": None,
            },
        }
        return result

    market_context = ChangingMarketContext()
    monkeypatch.setattr(PostgreSQLPlanningApi, "activation_preview", preview)
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activation_replay",
        lambda *_args, **_kwargs: None,
    )
    captured: dict[str, Any] = {}

    def activate(
        _self: PostgreSQLPlanningApi,
        _payload: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        captured["snapshot"] = kwargs["order_schedule_snapshot"]
        return {
            "activation": _mock_activation("activation-reference-change-001"),
            "venue_write_created": False,
            "runtime_real_write_gate": "CLOSED",
        }

    monkeypatch.setattr(PostgreSQLPlanningApi, "activate", activate)
    client = make_client(
        tmp_path,
        market_context_provider=market_context,
        instrument_rules_provider=FakeInstrumentRules(),
    )
    token = csrf(client)
    headers = {"Origin": ORIGIN, "X-CSRFToken": token}
    preview_response = client.post(
        f"/api/v1/plan-versions/{plan_version_id}/activation-preview",
        headers=headers,
    )
    assert preview_response.status_code == 200

    activation_response = client.post(
        "/api/v1/activations",
        json={
            "plan_version_id": plan_version_id,
            "expected_schedule_digest": preview_response.json()[
                "expected_schedule_digest"
            ],
        },
        headers={
            **headers,
            "Idempotency-Key": "activation-direct-reference-change-001",
        },
    )

    assert activation_response.status_code == 201
    assert (
        captured["snapshot"].schedule_digest
        == preview_response.json()["expected_schedule_digest"]
    )
    assert captured["snapshot"].reference_price == "100.5"
    assert market_context.calls == 1


def test_direct_activation_without_a_trusted_preview_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activation_preview",
        lambda _self, requested_id: _direct_activation_preview(requested_id),
    )
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activation_replay",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activate",
        lambda *_args, **_kwargs: pytest.fail("stale preview must not activate"),
    )
    client = make_client(
        tmp_path,
        instrument_rules_provider=FakeInstrumentRules(),
    )
    token = csrf(client)

    response = client.post(
        "/api/v1/activations",
        json={
            "plan_version_id": "direct-plan-version-without-preview",
            "expected_schedule_digest": "a" * 64,
        },
        headers={
            "Origin": ORIGIN,
            "X-CSRFToken": token,
            "Idempotency-Key": "activation-direct-preview-missing-001",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "ACTIVATION_PREVIEW_STALE"}


def test_direct_activation_rejects_a_digest_other_than_the_cached_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_version_id = "direct-plan-version-digest-mismatch"
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activation_preview",
        lambda _self, requested_id: _direct_activation_preview(requested_id),
    )
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activation_replay",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activate",
        lambda *_args, **_kwargs: pytest.fail("mismatched preview must not activate"),
    )
    client = make_client(
        tmp_path,
        instrument_rules_provider=FakeInstrumentRules(),
    )
    token = csrf(client)
    headers = {"Origin": ORIGIN, "X-CSRFToken": token}
    preview_response = client.post(
        f"/api/v1/plan-versions/{plan_version_id}/activation-preview",
        headers=headers,
    )
    assert preview_response.status_code == 200
    expected_digest = preview_response.json()["expected_schedule_digest"]
    mismatched_digest = (
        ("0" if expected_digest[0] != "0" else "1") + expected_digest[1:]
    )

    response = client.post(
        "/api/v1/activations",
        json={
            "plan_version_id": plan_version_id,
            "expected_schedule_digest": mismatched_digest,
        },
        headers={
            **headers,
            "Idempotency-Key": "activation-direct-preview-mismatch-001",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "ACTIVATION_PREVIEW_STALE"}


def test_direct_activation_rejects_an_expired_cached_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_version_id = "direct-plan-version-expired-preview"
    current_monotonic = [100.0]
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activation_preview",
        lambda _self, requested_id: _direct_activation_preview(requested_id),
    )
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activation_replay",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activate",
        lambda *_args, **_kwargs: pytest.fail("expired preview must not activate"),
    )
    client = make_client(
        tmp_path,
        instrument_rules_provider=FakeInstrumentRules(),
        monotonic_provider=lambda: current_monotonic[0],
    )
    token = csrf(client)
    headers = {"Origin": ORIGIN, "X-CSRFToken": token}
    preview_response = client.post(
        f"/api/v1/plan-versions/{plan_version_id}/activation-preview",
        headers=headers,
    )
    assert preview_response.status_code == 200

    current_monotonic[0] += 60.0
    response = client.post(
        "/api/v1/activations",
        json={
            "plan_version_id": plan_version_id,
            "expected_schedule_digest": preview_response.json()[
                "expected_schedule_digest"
            ],
        },
        headers={
            **headers,
            "Idempotency-Key": "activation-direct-preview-expired-001",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "ACTIVATION_PREVIEW_STALE"}


def test_strategy_activation_without_an_order_schedule_does_not_require_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def preview(_self: PostgreSQLPlanningApi, plan_version_id: str) -> dict[str, Any]:
        return {
            "plan_version_id": plan_version_id,
            "runtime_compatible": True,
            "runtime_incompatibility_reason": None,
            "venue_ref": "BINANCE_USDM",
            "instrument_ref": "BTCUSDT-PERP",
            "direction": "LONG",
            "trade_amount": "100",
            "order_schedule_spec": None,
        }

    def activate(
        _self: PostgreSQLPlanningApi,
        _payload: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        captured["snapshot"] = kwargs["order_schedule_snapshot"]
        return {
            "activation": _mock_activation("activation-strategy-001"),
            "venue_write_created": False,
            "runtime_real_write_gate": "CLOSED",
        }

    monkeypatch.setattr(PostgreSQLPlanningApi, "activation_preview", preview)
    monkeypatch.setattr(
        PostgreSQLPlanningApi,
        "activation_replay",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(PostgreSQLPlanningApi, "activate", activate)
    client = make_client(tmp_path)
    token = csrf(client)

    response = client.post(
        "/api/v1/activations",
        json={
            "plan_version_id": "strategy-plan-version-no-schedule",
            "expected_schedule_digest": None,
        },
        headers={
            "Origin": ORIGIN,
            "X-CSRFToken": token,
            "Idempotency-Key": "activation-strategy-no-preview-001",
        },
    )

    assert response.status_code == 201
    assert captured["snapshot"] is None
