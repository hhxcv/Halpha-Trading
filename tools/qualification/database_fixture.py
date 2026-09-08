"""Small local-database helpers shared by direct product checks."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import keyring
import psycopg

from halpha.app.plan_ai_review import (
    PlanAiReviewConfiguration,
    PlanAiReviewDecision,
    PlanAiReviewResult,
    PostgreSQLPlanAiReviewStore,
)
from halpha.capital.models import AuthorityClass, EnvironmentKind
from halpha.planning.models import (
    PlanDecisionContext,
    PlanDecisionIntent,
    PlanSetupFamily,
    RequestedLimits,
    TradePlanContent,
)
from halpha.planning.registry import (
    ONE_SHOT_STRATEGY_ID,
    DecisionBasisKind,
    DraftDecisionBasis,
    describe_strategy,
)
from halpha.planning.service import PlanningApplicationService
from halpha.winvault import require_win_vault_backend


class NoExchangeClient:
    """Fail if a database-only check attempts an exchange request."""

    def submit_order(self, action: object) -> None:
        raise AssertionError("EXCHANGE_REQUEST_FORBIDDEN_IN_DATABASE_CHECK")

    def cancel_order(self, action: object) -> None:
        raise AssertionError("EXCHANGE_REQUEST_FORBIDDEN_IN_DATABASE_CHECK")

    def query_order(self, action: object) -> None:
        raise AssertionError("EXCHANGE_QUERY_UNUSED_IN_DATABASE_CHECK")


def database_secret(role: str) -> str:
    backend = keyring.get_keyring()
    require_win_vault_backend(backend)
    secret = backend.get_password(
        f"Halpha/PostgreSQL/BINANCE_DEMO/{role}",
        "scram_password",
    )
    if not secret:
        raise RuntimeError(f"DEMO_{role.upper()}_DATABASE_REFERENCE_MISSING")
    return secret


def connect_app(
    database_name: str = "halpha_demo",
) -> psycopg.Connection[Any]:
    secret = database_secret("App")
    try:
        return psycopg.connect(
            host="127.0.0.1",
            port=5432,
            dbname=database_name,
            user="halpha_demo_app",
            password=secret,
        )
    finally:
        secret = ""


def connect_executor(
    database_name: str = "halpha_demo",
) -> psycopg.Connection[Any]:
    secret = database_secret("Executor")
    try:
        return psycopg.connect(
            host="127.0.0.1",
            port=5432,
            dbname=database_name,
            user="halpha_demo_executor",
            password=secret,
        )
    finally:
        secret = ""


def plan_content(
    *,
    environment_id: str,
    account_ref: str,
    instrument_ref: str,
    now: datetime,
    limits: tuple[str, str, str],
) -> TradePlanContent:
    strategy = describe_strategy(ONE_SHOT_STRATEGY_ID)
    return TradePlanContent(
        decision_context=PlanDecisionContext(
            rationale="Validate the bounded workbench fixture plan.",
            evidence="Deterministic fixture market and venue facts.",
            limitations="Future price, fill and funding remain uncertain.",
            intent=PlanDecisionIntent.VALIDATION,
            setup_family=PlanSetupFamily.BREAKOUT_CONTINUATION,
            invalidation="Do not enter after the fixed fixture boundary.",
            evidence_cutoff=now,
        ),
        decision_basis=DraftDecisionBasis(
            kind=DecisionBasisKind.STRATEGY_SIGNAL,
            decision_basis_ref=strategy.strategy_id,
            parameters={"direction": "LONG"},
        ),
        environment_id=environment_id,
        environment_kind=EnvironmentKind.DEMO,
        authority_class=AuthorityClass.DEMO_VALIDATION,
        account_ref=account_ref,
        venue_ref="BINANCE_USDM",
        instrument_ref=instrument_ref,
        direction="LONG",
        target_exposure="0.01",
        requested_limits=RequestedLimits(
            max_margin=limits[0],
            max_notional=limits[1],
            max_allowed_loss=limits[2],
        ),
        valid_from=now - timedelta(minutes=1),
        valid_until=now + timedelta(days=1),
        allowed_actions=frozenset(strategy.allowed_action_profiles),
        terms={"one_entry_cycle": True, "resume_policy": "MANUAL_PLAN_RESUME"},
    )


def create_and_activate(
    connection: psycopg.Connection[Any],
    *,
    environment_id: str,
    account_ref: str,
    now: datetime,
    instrument_ref: str,
    limits: tuple[str, str, str],
) -> dict[str, str]:
    ids = {
        "plan_id": str(uuid4()),
        "plan_version_id": str(uuid4()),
        "activation_id": str(uuid4()),
    }
    service = PlanningApplicationService(connection, environment_id)
    draft = service.create_draft(
        plan_id=ids["plan_id"],
        content=plan_content(
            environment_id=environment_id,
            account_ref=account_ref,
            instrument_ref=instrument_ref,
            now=now,
            limits=limits,
        ),
        observed_at=now,
    )
    review_id = str(uuid4())
    review_store = PostgreSQLPlanAiReviewStore(connection, environment_id)
    review_store.create_queued(
        review_id=review_id,
        plan_id=ids["plan_id"],
        draft_version=draft.draft_version,
        draft_content_digest=draft.content_digest,
        configuration=PlanAiReviewConfiguration(),
        idempotency_key=f"fixture-ai-review:{review_id}",
        observed_at=now,
    )
    review_store.mark_running(
        review_id,
        market_context_digest="b" * 64,
        market_source_cutoff=now,
        observed_at=now,
    )
    review = review_store.complete(
        review_id,
        result=PlanAiReviewResult(
            decision=PlanAiReviewDecision.APPROVE,
            reason="Deterministic workbench fixture approval.",
        ),
        observed_at=now,
    )
    service.fix_and_activate(
        plan_id=ids["plan_id"],
        expected_draft_version=1,
        plan_version_id=ids["plan_version_id"],
        activation_id=ids["activation_id"],
        environment_kind=EnvironmentKind.DEMO,
        authority_class=AuthorityClass.DEMO_VALIDATION,
        product_build_id="a" * 64,
        observed_at=now,
        ai_review_ref=review.review_id,
        ai_review_checker=lambda _version: {
            "review_id": review.review_id,
            "status": review.status.value,
            "decision": review.decision.value if review.decision is not None else None,
            "draft_content_digest": review.draft_content_digest,
        },
    )
    return ids


def cleanup_app(connection: psycopg.Connection[Any], environment_id: str) -> None:
    for table in (
        "stop_state_version",
        "receipt",
        "plan_event",
        "plan_activation",
        "command",
        "trade_plan_version",
        "plan_ai_review",
        "trade_plan_draft",
    ):
        connection.execute(
            f"DELETE FROM halpha.{table} WHERE environment_id = %s",
            (environment_id,),
        )
