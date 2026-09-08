from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from halpha.planning.models import (
    AuthorityClass,
    EnvironmentKind,
    PlanCreatorKind,
    PlanDecisionContext,
    PlanDecisionIntent,
    PlanSetupFamily,
    RequestedLimits,
    TradePlanContent,
)
from halpha.planning.order_schedule import direct_allowed_action_profiles
from halpha.planning.registry import (
    DIRECT_EXECUTION_REF,
    DecisionBasisKind,
    Direction,
    DraftDecisionBasis,
)
from halpha.scalping.models import (
    SCALP_WORKFLOW_KIND,
    ScalpTemplate,
    is_internal_scalp_cycle,
    recommended_scalp_template,
    scalp_playbook_ref,
)


def test_scalp_template_builds_one_protected_market_cycle() -> None:
    template = ScalpTemplate(
        notional="250",
        initial_stop_bps="40",
        take_profit_r="1.8",
        max_holding_seconds=180,
        max_spread_bps="4",
        entry_fee_bps="5",
        exit_fee_bps="6",
    )

    spec = template.order_schedule_spec()

    assert spec.entry_program is not None
    assert spec.entry_program.kind.value == "ONE_TIME"
    assert spec.venue_policy.order_type.value == "MARKET"
    assert spec.submission_mode.value == "SERIAL_PROTECTED"
    assert [item.kind for item in spec.entry_conditions.items] == [
        "DECISION_BASIS_READY",
        "SPREAD_BPS",
    ]
    assert spec.entry_conditions.items[1].maximum_bps == "4"
    assert spec.protection_policy is not None
    assert spec.protection_policy.initial_stop.distance_bps == "40"
    assert spec.protection_policy.time_exit_seconds == 180
    assert spec.protection_policy.take_profit_ladder is not None
    assert spec.protection_policy.take_profit_ladder.levels[0].trigger_r == "1.8"
    assert spec.protection_policy.take_profit_ladder.levels[0].quantity_fraction == "1"


def test_scalp_template_builds_one_protected_same_side_maker_cycle() -> None:
    template = ScalpTemplate(entry_mode="MAKER_ONLY_SAME_SIDE")

    spec = template.order_schedule_spec(maker_limit_price="99.5")

    assert spec.price_distribution.limit_price == "99.5"
    assert spec.venue_policy.order_type.value == "LIMIT"
    assert spec.venue_policy.time_in_force.value == "GTC"
    assert spec.venue_policy.post_only is True
    with pytest.raises(ValueError, match="SCALP_MAKER_LIMIT_PRICE_REQUIRED"):
        template.order_schedule_spec()


@pytest.mark.parametrize(
    ("field", "value", "code"),
    (
        ("notional", "0", "SCALP_TEMPLATE_NOTIONAL_INVALID"),
        ("initial_stop_bps", "5001", "SCALP_TEMPLATE_STOP_INVALID"),
        ("take_profit_r", "0.01", "SCALP_TEMPLATE_TAKE_PROFIT_INVALID"),
        ("max_spread_bps", "1001", "SCALP_TEMPLATE_SPREAD_INVALID"),
        ("entry_fee_bps", "1001", "SCALP_TEMPLATE_FEE_INVALID"),
    ),
)
def test_scalp_template_rejects_unsafe_bounds(
    field: str,
    value: str,
    code: str,
) -> None:
    with pytest.raises(ValueError, match=code):
        ScalpTemplate(**{field: value})


def test_recommendation_changes_only_market_sensitive_template_fields() -> None:
    template = ScalpTemplate(
        notional="432",
        entry_fee_bps="3",
        exit_fee_bps="4",
    )
    market = SimpleNamespace(
        instrument_ref="ETHUSDT-PERP",
        source="BINANCE_DEMO_PUBLIC",
        source_cutoff=datetime(2026, 8, 16, tzinfo=UTC),
        reference_price="2000",
        atr_14="10",
        bid_price="1999.8",
        ask_price="2000.2",
    )

    result = recommended_scalp_template(template, market)

    assert result.instrument_ref == "ETHUSDT-PERP"
    assert result.recommended_template.notional == "432"
    assert result.recommended_template.entry_fee_bps == "3"
    assert result.recommended_template.exit_fee_bps == "4"
    assert result.recommended_template.initial_stop_bps == "40"
    assert result.recommended_template.max_spread_bps == "5"
    assert result.basis == ("15m ATR × 0.8", "当前价差 × 2.5")


def _scalp_content() -> TradePlanContent:
    observed_at = datetime(2026, 8, 16, tzinfo=UTC)
    template = ScalpTemplate()
    return TradePlanContent(
        plan_name="剥头皮 BTCUSDT 做多 08:00:00",
        created_at=observed_at,
        creator_kind=PlanCreatorKind.HUMAN,
        decision_context=PlanDecisionContext(
            rationale="人工选择方向并触发剥头皮周期",
            intent=PlanDecisionIntent.PROFIT_SEEKING,
            setup_family=PlanSetupFamily.OTHER,
            playbook_ref=scalp_playbook_ref(template),
            evidence_cutoff=observed_at,
        ),
        decision_basis=DraftDecisionBasis(
            kind=DecisionBasisKind.DIRECT_EXECUTION,
            decision_basis_ref=DIRECT_EXECUTION_REF,
            parameters={},
        ),
        order_schedule_spec=template.order_schedule_spec(),
        environment_id="demo-main",
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
            max_allowed_loss="1",
        ),
        valid_from=observed_at,
        valid_until=observed_at + timedelta(minutes=15),
        allowed_actions=direct_allowed_action_profiles(
            template.order_schedule_spec()
        ),
        terms={
            "workflow_kind": SCALP_WORKFLOW_KIND,
            "scalp_template": template.model_dump(mode="json"),
            "scalp_template_digest": template.digest,
        },
    )


def test_internal_maker_cycle_requires_the_frozen_same_side_price() -> None:
    content = _scalp_content()
    template = ScalpTemplate(entry_mode="MAKER_ONLY_SAME_SIDE")
    maker_content = content.model_copy(
        update={
            "order_schedule_spec": template.order_schedule_spec(maker_limit_price="99.5"),
            "decision_context": content.decision_context.model_copy(
                update={"playbook_ref": scalp_playbook_ref(template)}
            ),
            "allowed_actions": direct_allowed_action_profiles(
                template.order_schedule_spec(maker_limit_price="99.5")
            ),
            "terms": {
                **content.terms,
                "scalp_template": template.model_dump(mode="json"),
                "scalp_template_digest": template.digest,
                "scalp_entry_limit_price": "99.5",
            },
        }
    )

    assert is_internal_scalp_cycle(maker_content) is True
    assert is_internal_scalp_cycle(
        maker_content.model_copy(
            update={
                "terms": {
                    **maker_content.terms,
                    "scalp_entry_limit_price": "99.6",
                }
            }
        )
    ) is False


def test_only_exact_internal_cycle_shape_can_use_scalp_path() -> None:
    content = _scalp_content()

    assert is_internal_scalp_cycle(content) is True
    assert is_internal_scalp_cycle(
        content.model_copy(
            update={
                "decision_context": content.decision_context.model_copy(
                    update={"rationale": "ordinary direct plan"}
                )
            }
        )
    ) is False
    assert is_internal_scalp_cycle(
        content.model_copy(
            update={
                "decision_context": content.decision_context.model_copy(
                    update={"playbook_ref": "SCALP_TEMPLATE:wrong"}
                )
            }
        )
    ) is False
    assert is_internal_scalp_cycle(
        content.model_copy(
            update={
                "terms": {
                    **content.terms,
                    "scalp_template": ScalpTemplate(
                        initial_stop_bps="50"
                    ).model_dump(mode="json"),
                }
            }
        )
    ) is False


@pytest.mark.parametrize("digest", (None, "0" * 64))
def test_internal_cycle_requires_the_exact_template_digest(digest: str | None) -> None:
    content = _scalp_content()
    terms = {**content.terms, "scalp_template_digest": digest}

    assert not is_internal_scalp_cycle(content.model_copy(update={"terms": terms}))
