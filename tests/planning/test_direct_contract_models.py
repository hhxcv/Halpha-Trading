from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from halpha.capital.models import (
    AuthorityClass,
    EnvironmentKind,
    NewRiskDisciplinePolicy,
)
from halpha.domain_values import content_digest
from halpha.planning.models import (
    PERSISTED_HISTORY_CONTEXT_KEY,
    PlanActivation,
    PlanDecisionContext,
    PlanLifecycle,
    RequestedLimits,
    TradePlanContent,
    TradePlanDraft,
    TradePlanVersion,
    new_risk_decision_context_incompatibility,
)
from halpha.planning.service import (
    PlanningApplicationService,
    plan_reward_risk_ratio,
    require_plan_reward_risk_discipline,
)
from halpha.planning.order_policies import (
    FullFillLossBudgetSpec,
    InitialStopSpec,
    ProtectionPolicy,
    TakeProfitLadderSpec,
    TakeProfitLevel,
)
from halpha.planning.order_schedule import (
    AmountDistribution,
    InstrumentOrderRules,
    OrderSchedulePreview,
    OrderScheduleSpec,
    SinglePrice,
    compile_order_schedule,
    direct_allowed_action_profiles,
)
from halpha.planning.registry import (
    DIRECT_EXECUTION_REF,
    DecisionBasisKind,
    Direction,
    DraftDecisionBasis,
    FixedDirectExecutionBasis,
)


NOW = datetime(2026, 7, 23, tzinfo=UTC)


def test_plan_decision_context_normalizes_readable_text() -> None:
    context = PlanDecisionContext(
        rationale="  current setup is favorable  ",
        evidence="venue facts\r\nand fixed parameters",
        limitations="future path remains unknown",
        intent="PROFIT_SEEKING",
        setup_family="BREAKOUT_CONTINUATION",
        playbook_ref="  BTC_BREAKOUT_V1  ",
        invalidation="  fixed boundary fails  ",
        evidence_cutoff=NOW,
    )

    assert context.rationale == "current setup is favorable"
    assert context.evidence == "venue facts\nand fixed parameters"
    assert context.invalidation == "fixed boundary fails"
    assert context.playbook_ref == "BTC_BREAKOUT_V1"
    assert context.experiment_complete is True


def test_draft_decision_context_preserves_incomplete_input_but_cannot_admit_risk() -> None:
    context = PlanDecisionContext(rationale="只填写到一半的交易理由")

    assert context.rationale == "只填写到一半的交易理由"
    assert context.experiment_complete is False
    assert new_risk_decision_context_incompatibility(
        decision_basis_kind=DecisionBasisKind.DIRECT_EXECUTION,
        decision_context=context,
    ) == "PLAN_DECISION_EXPERIMENT_INCOMPLETE"


def test_plan_decision_context_rejects_a_naive_evidence_cutoff() -> None:
    with pytest.raises(
        ValidationError,
        match="PLAN_DECISION_EVIDENCE_CUTOFF_INVALID",
    ):
        PlanDecisionContext(
            rationale="bounded reason",
            evidence="bounded evidence",
            limitations="bounded limitations",
            intent="PROFIT_SEEKING",
            setup_family="BREAKOUT_CONTINUATION",
            invalidation="bounded invalidation",
            evidence_cutoff=datetime(2026, 7, 23),
        )


@pytest.mark.parametrize(
    "playbook_ref",
    (
        "BTC BREAKOUT V1",
        "逐笔信号",
        "-leading-separator",
        "A" + "x" * 96,
    ),
)
def test_plan_decision_context_rejects_an_unstable_playbook_ref(
    playbook_ref: str,
) -> None:
    with pytest.raises(
        ValidationError,
        match="PLAN_DECISION_PLAYBOOK_REF_INVALID",
    ):
        PlanDecisionContext(
            rationale="bounded reason",
            evidence="bounded evidence",
            limitations="bounded limitations",
            playbook_ref=playbook_ref,
        )


@pytest.mark.parametrize("field", ("rationale", "evidence", "limitations"))
def test_plan_decision_context_rejects_missing_or_unsafe_text(field: str) -> None:
    values = {
        "rationale": "bounded reason",
        "evidence": "bounded evidence",
        "limitations": "bounded limitations",
    }
    values[field] = " \r\n "
    with pytest.raises(ValidationError, match="PLAN_DECISION_CONTEXT_INVALID"):
        PlanDecisionContext(**values)

    values[field] = "x" * 2001
    with pytest.raises(ValidationError):
        PlanDecisionContext(**values)


def _spec() -> OrderScheduleSpec:
    return OrderScheduleSpec(
        price_distribution=SinglePrice(limit_price="100"),
        amount_distribution=AmountDistribution(base_notional="20"),
        protection_policy=ProtectionPolicy(
            initial_stop=InitialStopSpec(distance_bps="100"),
            take_profit_ladder=TakeProfitLadderSpec(
                levels=(
                    TakeProfitLevel(trigger_r="2", quantity_fraction="1"),
                ),
            ),
        ),
    )


def test_price_exit_reward_risk_is_weighted_and_enforced() -> None:
    basis = FixedDirectExecutionBasis(
        parameter_digest=content_digest({}),
        product_build_id="a" * 64,
    )
    spec = _spec().model_copy(
        update={
            "protection_policy": _spec().protection_policy.model_copy(
                update={
                    "take_profit_ladder": TakeProfitLadderSpec(
                        levels=(
                            TakeProfitLevel(
                                trigger_r="0.5",
                                quantity_fraction="1",
                            ),
                        )
                    )
                }
            )
        }
    )

    assert plan_reward_risk_ratio(
        decision_basis=basis,
        order_schedule_spec=_spec(),
    ) == 2
    with pytest.raises(
        ValueError,
        match="PLAN_REWARD_RISK_BELOW_DISCIPLINE_MINIMUM",
    ):
        require_plan_reward_risk_discipline(
            decision_basis=basis,
            order_schedule_spec=spec,
            policy=NewRiskDisciplinePolicy(),
        )

    with pytest.raises(ValueError, match="PLAN_REWARD_RISK_UNAVAILABLE"):
        require_plan_reward_risk_discipline(
            decision_basis=basis,
            order_schedule_spec=_spec().model_copy(
                update={
                    "protection_policy": _spec().protection_policy.model_copy(
                        update={"take_profit_ladder": None}
                    )
                }
            ),
            policy=NewRiskDisciplinePolicy(),
        )


def _historical_split_spec() -> OrderScheduleSpec:
    return OrderScheduleSpec(
        price_distribution=SinglePrice(limit_price="100"),
        amount_distribution=AmountDistribution(base_notional="20"),
        protection_policy=ProtectionPolicy(
            initial_stop=InitialStopSpec(distance_bps="100"),
            take_profit_ladder=TakeProfitLadderSpec(
                levels=tuple(
                    TakeProfitLevel(
                        trigger_r=str(index),
                        quantity_fraction="0.2",
                    )
                    for index in range(1, 6)
                )
            ),
        ),
    )


def _historical_split_action_profiles() -> frozenset[str]:
    return frozenset(
        {
            "ENTRY_LIMIT",
            "PROTECTIVE_STOP_REDUCE_ONLY",
            "REDUCE_OR_CLOSE_MARKET",
            "CANCEL_ORDER",
            "TAKE_PROFIT_1",
            "TAKE_PROFIT_2",
        }
    )


def _snapshot(
    spec: OrderScheduleSpec | None = None,
    *,
    schedule_ref: str = "plan-version-direct",
    instrument_ref: str = "BTCUSDT-PERP",
    direction: Direction = Direction.LONG,
) -> OrderSchedulePreview:
    preview = compile_order_schedule(
        spec or _spec(),
        InstrumentOrderRules(
            source="BINANCE_DEMO_EXCHANGE_INFO",
            min_price="0.1",
            max_price="1000000",
            price_tick_size="0.1",
            limit_quantity_step="0.01",
            min_limit_quantity="0.01",
            max_limit_quantity="1000",
            market_quantity_step="0.1",
            min_market_quantity="0.1",
            max_market_quantity="100",
            min_notional="5",
            source_cutoff=NOW.isoformat(),
        ),
        venue_ref="BINANCE_USDM",
        instrument_ref=instrument_ref,
        direction=direction,
        max_notional="100",
        schedule_ref=schedule_ref,
    )
    assert preview.valid
    return preview


def _draft_content(*, allowed_actions: frozenset[str]) -> TradePlanContent:
    return TradePlanContent(
        plan_name="direct contract",
        created_at=NOW,
        creator_kind="AI",
        decision_basis=DraftDecisionBasis(
            kind="DIRECT_EXECUTION",
            decision_basis_ref=DIRECT_EXECUTION_REF,
            parameters={},
        ),
        order_schedule_spec=_spec(),
        environment_id="demo",
        environment_kind=EnvironmentKind.DEMO,
        authority_class=AuthorityClass.DEMO_VALIDATION,
        account_ref="demo-account",
        venue_ref="BINANCE_USDM",
        instrument_ref="BTCUSDT-PERP",
        direction=Direction.LONG,
        target_exposure="100",
        requested_limits=RequestedLimits(
            max_margin="100",
            max_notional="100",
            max_allowed_loss="10",
        ),
        valid_from=NOW,
        valid_until=NOW + timedelta(hours=1),
        allowed_actions=allowed_actions,
        terms={},
    )


def test_periodic_draft_save_is_a_noop_when_plan_fields_are_unchanged() -> None:
    content = _draft_content(
        allowed_actions=direct_allowed_action_profiles(_spec()),
    )
    current = TradePlanDraft(
        plan_id="plan-periodic-save",
        environment_id="demo",
        draft_version=7,
        content=content,
        content_digest=content_digest(content),
        updated_at=NOW,
    )
    saved: list[TradePlanDraft] = []
    service = object.__new__(PlanningApplicationService)
    service._environment_id = "demo"
    service._planning = SimpleNamespace(
        get_draft=lambda _plan_id, **_kwargs: current,
        has_activation=lambda _plan_id: False,
        save_draft=lambda draft, **_kwargs: saved.append(draft),
    )
    later = NOW + timedelta(minutes=5)
    unchanged_payload = content.model_copy(
        update={
            "valid_from": later,
            "valid_until": later + timedelta(hours=1),
        }
    )

    result = service.update_draft(
        plan_id=current.plan_id,
        expected_version=current.draft_version,
        content=unchanged_payload,
        observed_at=later,
    )

    assert result is current
    assert saved == []


def _fixed_version(*, allowed_actions: frozenset[str]) -> TradePlanVersion:
    return TradePlanVersion(
        plan_version_id="plan-version-direct",
        plan_id="plan-direct",
        environment_id="demo",
        fixed_at=NOW,
        plan_name="direct contract",
        created_at=NOW,
        creator_kind="AI",
        ai_review_ref="review-approved",
        decision_context=PlanDecisionContext(
            rationale="The fixed direct setup has a bounded market hypothesis.",
            evidence="Current venue facts and the fixed order schedule.",
            limitations="Future price, fills, fees, and funding remain uncertain.",
            intent="PROFIT_SEEKING",
            setup_family="OTHER",
            playbook_ref="DIRECT_RULE_V1",
            invalidation="Do not enter once the fixed invalidation boundary fails.",
            evidence_cutoff=NOW,
        ),
        decision_basis=FixedDirectExecutionBasis(
            parameter_digest=content_digest({}),
            product_build_id="a" * 64,
        ),
        order_schedule_spec=_spec(),
        account_ref="demo-account",
        venue_ref="BINANCE_USDM",
        instrument_ref="BTCUSDT-PERP",
        direction=Direction.LONG,
        target_exposure="100",
        requested_limits=RequestedLimits(
            max_margin="100",
            max_notional="100",
            max_allowed_loss="10",
        ),
        valid_from=NOW,
        valid_until=NOW + timedelta(hours=1),
        allowed_actions=allowed_actions,
        terms={},
        content_digest="b" * 64,
    )


def _approved_ai_review(version: TradePlanVersion) -> dict[str, object]:
    return {
        "review_id": version.ai_review_ref,
        "status": "APPROVED",
        "decision": "APPROVE",
        "draft_content_digest": "c" * 64,
    }


def test_new_risk_activation_fails_closed_without_an_exact_ai_approval() -> None:
    version = _fixed_version(
        allowed_actions=direct_allowed_action_profiles(_spec()),
    )
    service = object.__new__(PlanningApplicationService)
    service._environment_id = "demo"
    service._planning = SimpleNamespace(get_version=lambda _plan_version_id: version)

    with pytest.raises(ValueError, match="PLAN_AI_REVIEW_NOT_CONFIGURED"):
        service.activate_version(
            plan_version_id=version.plan_version_id,
            activation_id="activation-no-review-checker",
            environment_kind=EnvironmentKind.DEMO,
            authority_class=AuthorityClass.DEMO_VALIDATION,
            observed_at=NOW + timedelta(minutes=1),
        )

    with pytest.raises(ValueError, match="PLAN_AI_REVIEW_NOT_APPROVED"):
        service.activate_version(
            plan_version_id=version.plan_version_id,
            activation_id="activation-rejected-review",
            environment_kind=EnvironmentKind.DEMO,
            authority_class=AuthorityClass.DEMO_VALIDATION,
            observed_at=NOW + timedelta(minutes=1),
            ai_review_checker=lambda current: {
                "review_id": current.ai_review_ref,
                "status": "REJECTED",
                "decision": "REJECT",
                "draft_content_digest": "c" * 64,
            },
        )


def test_new_risk_activation_rejects_a_historical_incomplete_decision_record() -> None:
    version = _fixed_version(
        allowed_actions=direct_allowed_action_profiles(_spec()),
    ).model_copy(update={"decision_context": None})
    inserted: list[PlanActivation] = []
    service = object.__new__(PlanningApplicationService)
    service._environment_id = "demo"
    service._planning = SimpleNamespace(
        get_version=lambda _plan_version_id: version,
        lock_and_list_open_instrument_activations=lambda **_scope: (),
        insert_activation=inserted.append,
    )

    with pytest.raises(ValueError, match="PLAN_DECISION_EXPERIMENT_INCOMPLETE"):
        service.activate_version(
            plan_version_id="plan-version-direct",
            activation_id="activation-incomplete-decision",
            environment_kind=EnvironmentKind.DEMO,
            authority_class=AuthorityClass.DEMO_VALIDATION,
            observed_at=NOW + timedelta(minutes=1),
            order_schedule_snapshot=_snapshot(),
            ai_review_checker=_approved_ai_review,
        )

    assert inserted == []


def test_new_direct_risk_allows_the_simplified_record_without_a_playbook_identity() -> None:
    current = _fixed_version(
        allowed_actions=direct_allowed_action_profiles(_spec()),
    )
    assert current.decision_context is not None
    version = current.model_copy(
        update={
            "decision_context": current.decision_context.model_copy(
                update={"playbook_ref": None},
            )
        }
    )
    inserted: list[PlanActivation] = []
    service = object.__new__(PlanningApplicationService)
    service._environment_id = "demo"
    service._planning = SimpleNamespace(
        get_version=lambda _plan_version_id: version,
        lock_and_list_open_instrument_activations=lambda **_scope: (),
        insert_activation=inserted.append,
    )

    activation = service.activate_version(
        plan_version_id="plan-version-direct",
        activation_id="activation-without-playbook",
        environment_kind=EnvironmentKind.DEMO,
        authority_class=AuthorityClass.DEMO_VALIDATION,
        observed_at=NOW + timedelta(minutes=1),
        order_schedule_snapshot=_snapshot(),
        ai_review_checker=_approved_ai_review,
    )

    assert inserted == [activation]


def test_fix_rejects_a_persisted_strategy_draft_with_unqualified_profit_intent() -> None:
    content_values = _draft_content(
        allowed_actions=direct_allowed_action_profiles(_spec()),
    ).model_dump(mode="python")
    content_values.update(
        {
            "decision_context": {
                "rationale": "A breakout should continue after confirmation.",
                "evidence": "Current closed bars and venue facts.",
                "limitations": "The strategy has no positive-expectancy evidence.",
                "intent": "PROFIT_SEEKING",
                "setup_family": "BREAKOUT_CONTINUATION",
                "invalidation": "Reject if the fixed breakout boundary fails.",
                "evidence_cutoff": NOW,
            },
            "decision_basis": {
                "kind": "STRATEGY_SIGNAL",
                "decision_basis_ref": "ONE_SHOT_DONCHIAN_ATR_BREAKOUT",
                "parameters": {"direction": "LONG"},
            },
            "order_schedule_spec": None,
        }
    )
    content = TradePlanContent.model_validate(content_values)
    draft = TradePlanDraft(
        plan_id="plan-unqualified-profit",
        environment_id="demo",
        draft_version=1,
        content=content,
        content_digest="c" * 64,
        updated_at=NOW,
    )
    inserted: list[TradePlanVersion] = []
    service = object.__new__(PlanningApplicationService)
    service._environment_id = "demo"
    service._planning = SimpleNamespace(
        get_draft=lambda _plan_id, **_kwargs: draft,
        has_activation=lambda _plan_id: False,
        insert_version=inserted.append,
    )

    with pytest.raises(
        ValueError,
        match="STRATEGY_DECISION_INTENT_NOT_QUALIFIED",
    ):
        service.fix_draft(
            plan_id=draft.plan_id,
            expected_draft_version=1,
            plan_version_id="version-unqualified-profit",
            product_build_id="a" * 64,
            fixed_at=NOW,
            ai_review_ref="review-approved",
        )

    assert inserted == []


def _activation_service_with_scope(
    existing_scope: tuple[object, ...],
) -> tuple[PlanningApplicationService, list[PlanActivation]]:
    version = _fixed_version(
        allowed_actions=direct_allowed_action_profiles(_spec()),
    )
    inserted: list[PlanActivation] = []
    service = object.__new__(PlanningApplicationService)
    service._environment_id = "demo"
    service._planning = SimpleNamespace(
        get_version=lambda _plan_version_id: version,
        lock_and_list_open_instrument_activations=(
            lambda **_scope: existing_scope
        ),
        insert_activation=inserted.append,
    )
    return service, inserted


def test_new_risk_activation_fails_before_insertion_when_account_discipline_blocks() -> None:
    service, inserted = _activation_service_with_scope(())
    service.new_risk_discipline_status = lambda **_kwargs: SimpleNamespace(
        new_risk_allowed=False,
        blocker_codes=("NEW_RISK_DAILY_ATTEMPT_LIMIT_REACHED",),
    )

    with pytest.raises(ValueError, match="NEW_RISK_DAILY_ATTEMPT_LIMIT_REACHED"):
        service.activate_version(
            plan_version_id="plan-version-direct",
            activation_id="activation-blocked",
            environment_kind=EnvironmentKind.DEMO,
            authority_class=AuthorityClass.DEMO_VALIDATION,
            observed_at=NOW + timedelta(minutes=1),
            order_schedule_snapshot=_snapshot(),
            new_risk_discipline_policy=NewRiskDisciplinePolicy(),
            ai_review_checker=_approved_ai_review,
        )

    assert inserted == []


def test_same_direction_activations_share_one_account_instrument_scope() -> None:
    service, inserted = _activation_service_with_scope(
        (
            SimpleNamespace(
                direction=Direction.LONG,
                lifecycle=PlanLifecycle.RUNNING,
            ),
        )
    )

    activation = service.activate_version(
        plan_version_id="plan-version-direct",
        activation_id="activation-second-long",
        environment_kind=EnvironmentKind.DEMO,
        authority_class=AuthorityClass.DEMO_VALIDATION,
        observed_at=NOW + timedelta(minutes=1),
        order_schedule_snapshot=_snapshot(),
        ai_review_checker=_approved_ai_review,
    )

    assert activation.direction is Direction.LONG
    assert inserted == [activation]


def test_activation_rejects_projected_loss_above_the_plan_limit() -> None:
    spec = _spec().model_copy(
        update={
                "protection_policy": ProtectionPolicy(
                    initial_stop=InitialStopSpec(distance_bps="100"),
                    full_fill_loss_budget=FullFillLossBudgetSpec(
                        entry_fee_bps="2",
                        exit_fee_bps="5",
                    ),
                    take_profit_ladder=TakeProfitLadderSpec(
                        levels=(
                            TakeProfitLevel(
                                trigger_r="2",
                                quantity_fraction="1",
                            ),
                        )
                    ),
                )
        }
    )
    snapshot = _snapshot(spec)
    assert snapshot.full_fill_protection_estimate is not None
    version = _fixed_version(
        allowed_actions=direct_allowed_action_profiles(_spec()),
    ).model_copy(
        update={
            "order_schedule_spec": spec,
            "requested_limits": RequestedLimits(
                max_margin="100",
                max_notional="100",
                max_allowed_loss="0.01",
            ),
        }
    )
    inserted: list[PlanActivation] = []
    service = object.__new__(PlanningApplicationService)
    service._environment_id = "demo"
    service._planning = SimpleNamespace(
        get_version=lambda _plan_version_id: version,
        lock_and_list_open_instrument_activations=lambda **_scope: (),
        insert_activation=inserted.append,
    )

    with pytest.raises(
        ValueError,
        match="ORDER_SCHEDULE_LOSS_BUDGET_EXCEEDS_PLAN_LIMIT",
    ):
        service.activate_version(
            plan_version_id=version.plan_version_id,
            activation_id="activation-loss-limit",
            environment_kind=EnvironmentKind.DEMO,
            authority_class=AuthorityClass.DEMO_VALIDATION,
            observed_at=NOW + timedelta(minutes=1),
            order_schedule_snapshot=snapshot,
            ai_review_checker=_approved_ai_review,
        )

    assert inserted == []


@pytest.mark.parametrize(
    ("existing", "reason"),
    (
        (
            SimpleNamespace(
                direction=Direction.SHORT,
                lifecycle=PlanLifecycle.RUNNING,
            ),
            "ACCOUNT_INSTRUMENT_DIRECTION_CONFLICT",
        ),
        (
            SimpleNamespace(
                direction=Direction.LONG,
                lifecycle=PlanLifecycle.USER_TAKEOVER,
            ),
            "ACCOUNT_INSTRUMENT_TAKEOVER_CONFLICT",
        ),
    ),
)
def test_activation_rejects_unsafe_shared_position_scope(
    existing: object,
    reason: str,
) -> None:
    service, inserted = _activation_service_with_scope((existing,))

    with pytest.raises(ValueError, match=reason):
        service.activate_version(
            plan_version_id="plan-version-direct",
            activation_id="activation-conflict",
            environment_kind=EnvironmentKind.DEMO,
            authority_class=AuthorityClass.DEMO_VALIDATION,
            observed_at=NOW + timedelta(minutes=1),
            order_schedule_snapshot=_snapshot(),
            ai_review_checker=_approved_ai_review,
        )

    assert inserted == []


@pytest.mark.parametrize("model", (_draft_content, _fixed_version))
def test_direct_plan_models_require_the_exact_runtime_action_scope(model) -> None:
    expected = direct_allowed_action_profiles(_spec())

    assert model(allowed_actions=expected).allowed_actions == expected

    with pytest.raises(
        ValidationError,
        match="DIRECT_EXECUTION_ACTION_SCOPE_MISMATCH",
    ):
        model(allowed_actions=expected | {"TAKE_PROFIT_2"})

    with pytest.raises(
        ValidationError,
        match="DIRECT_EXECUTION_ACTION_SCOPE_MISMATCH",
    ):
        model(allowed_actions=expected - {"CANCEL_ORDER"})


def _activation(
    *,
    decision_basis_ref: str = DIRECT_EXECUTION_REF,
    snapshot: OrderSchedulePreview | None = None,
    lifecycle: PlanLifecycle = PlanLifecycle.RUNNING,
    closure_digest: str | None = None,
) -> PlanActivation:
    return PlanActivation(
        activation_id="activation-direct",
        environment_id="demo",
        environment_kind=EnvironmentKind.DEMO,
        authority_class=AuthorityClass.DEMO_VALIDATION,
        plan_version_ref="plan-version-direct",
        account_ref="demo-account",
        instrument_ref="BTCUSDT-PERP",
        direction=Direction.LONG,
        decision_basis_ref=decision_basis_ref,
        framework_strategy_id="HALPHA-DIRECT",
        order_schedule_snapshot=snapshot,
        target_exposure="100",
        lifecycle=lifecycle,
        rule_state={},
        closure_digest=closure_digest,
        created_at=NOW,
        updated_at=NOW,
    )


def test_direct_activation_requires_a_matching_snapshot() -> None:
    snapshot = _snapshot()

    assert _activation(snapshot=snapshot).order_schedule_snapshot == snapshot

    with pytest.raises(ValidationError, match="ORDER_SCHEDULE_SNAPSHOT_REQUIRED"):
        _activation()

    with pytest.raises(ValidationError, match="STRATEGY_ORDER_SCHEDULE_NOT_SUPPORTED"):
        _activation(
            decision_basis_ref="ONE_SHOT_DONCHIAN_ATR_BREAKOUT@1.0.1",
            snapshot=snapshot,
        )


def test_completed_activation_keeps_valid_history_outside_the_current_catalog() -> None:
    snapshot = _snapshot(_historical_split_spec())

    activation = _activation(
        snapshot=snapshot,
        lifecycle=PlanLifecycle.COMPLETED,
        closure_digest="historical-closure",
    )

    assert activation.order_schedule_snapshot == snapshot


def _historical_split_version() -> TradePlanVersion:
    current = _fixed_version(
        allowed_actions=direct_allowed_action_profiles(_spec()),
    )
    historical_spec = _historical_split_spec()
    payload = current.model_dump(mode="python")
    payload.update(
        {
            "order_schedule_spec": historical_spec,
            "allowed_actions": _historical_split_action_profiles(),
        }
    )
    return TradePlanVersion.model_validate(
        payload,
        context={PERSISTED_HISTORY_CONTEXT_KEY: True},
    )


def test_historical_version_is_readable_but_cannot_be_newly_activated() -> None:
    version = _historical_split_version()
    version_reads: list[dict[str, object]] = []

    def get_version(*_args, **kwargs):
        version_reads.append(dict(kwargs))
        return version

    service = object.__new__(PlanningApplicationService)
    service._environment_id = "demo"
    service._planning = type(
        "HistoricalPlanning",
        (),
        {
            "get_version": staticmethod(get_version),
            "insert_activation": staticmethod(
                lambda *_args, **_kwargs: pytest.fail(
                    "unsupported history must not create a new activation"
                )
            ),
        },
    )()

    with pytest.raises(
        ValueError,
        match="PLAN_ORDER_SCHEDULE_RUNTIME_INCOMPATIBLE",
    ):
        service.activate_version(
            plan_version_id=version.plan_version_id,
            activation_id="new-activation-from-history",
            environment_kind=EnvironmentKind.DEMO,
            authority_class=AuthorityClass.DEMO_VALIDATION,
            observed_at=NOW,
            order_schedule_snapshot=_snapshot(_historical_split_spec()),
            ai_review_checker=_approved_ai_review,
        )
    assert version_reads == [{}]


def test_historical_draft_is_readable_while_current_input_remains_strict() -> None:
    current = _draft_content(
        allowed_actions=direct_allowed_action_profiles(_spec()),
    )
    historical_spec = _historical_split_spec()
    payload = current.model_dump(mode="python")
    payload.update(
        {
            "order_schedule_spec": historical_spec,
            "allowed_actions": _historical_split_action_profiles(),
        }
    )

    with pytest.raises(
        ValidationError,
        match="DIRECT_EXECUTION_TAKE_PROFIT_LEVEL_COUNT_INVALID",
    ):
        TradePlanContent.model_validate(payload)

    historical = TradePlanContent.model_validate(
        payload,
        context={PERSISTED_HISTORY_CONTEXT_KEY: True},
    )
    assert historical.order_schedule_spec == historical_spec


def test_history_context_skips_only_current_catalog_admission() -> None:
    current = _draft_content(
        allowed_actions=direct_allowed_action_profiles(_spec()),
    )
    payload = current.model_dump(mode="python")
    payload["allowed_actions"] = current.allowed_actions | {"TAKE_PROFIT_2"}

    hydrated = TradePlanContent.model_validate(
        payload,
        context={PERSISTED_HISTORY_CONTEXT_KEY: True},
    )
    assert "TAKE_PROFIT_2" in hydrated.allowed_actions

    payload["authority_class"] = AuthorityClass.LIVE_REAL_CAPITAL
    with pytest.raises(ValidationError, match="AUTHORITY_ENVIRONMENT_MISMATCH"):
        TradePlanContent.model_validate(
            payload,
            context={PERSISTED_HISTORY_CONTEXT_KEY: True},
        )


@pytest.mark.parametrize(
    "snapshot",
    (
        _snapshot(schedule_ref="other-version"),
        _snapshot(instrument_ref="ETHUSDT-PERP"),
        _snapshot(direction=Direction.SHORT),
    ),
)
def test_direct_activation_rejects_snapshot_identity_mismatches(
    snapshot: OrderSchedulePreview,
) -> None:
    with pytest.raises(ValidationError, match="ORDER_SCHEDULE_SNAPSHOT_MISMATCH"):
        _activation(snapshot=snapshot)
