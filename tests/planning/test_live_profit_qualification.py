from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from halpha.capital.models import AuthorityClass, EnvironmentKind
from halpha.domain_values import content_digest
from halpha.planning.live_profit_qualification import (
    PlaybookQualificationArtifact,
    build_playbook_qualification_artifact,
    evaluate_live_profit_qualification,
)
from halpha.planning.models import (
    PlanDecisionContext,
    PlanDecisionIntent,
    RequestedLimits,
    TradePlanVersion,
)
from halpha.planning.order_policies import InitialStopSpec, ProtectionPolicy
from halpha.planning.order_schedule import (
    AmountDistribution,
    InstrumentOrderRules,
    OrderScheduleSpec,
    SinglePrice,
    compile_order_schedule,
    direct_allowed_action_profiles,
)
from halpha.planning.registry import (
    DIRECT_EXECUTION_REF,
    DecisionBasisKind,
    Direction,
    FixedDirectExecutionBasis,
)
from halpha.planning.service import PlanningApplicationService


NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)


def _spec() -> OrderScheduleSpec:
    return OrderScheduleSpec(
        price_distribution=SinglePrice(limit_price="100"),
        amount_distribution=AmountDistribution(base_notional="20"),
        protection_policy=ProtectionPolicy(
            initial_stop=InitialStopSpec(distance_bps="100"),
        ),
    )


def _version() -> TradePlanVersion:
    spec = _spec()
    return TradePlanVersion(
        plan_version_id="plan-version-live-profit",
        plan_id="plan-live-profit",
        environment_id="binance-live-copy-primary",
        fixed_at=NOW,
        plan_name="live profit qualification contract",
        created_at=NOW,
        creator_kind="AI",
        ai_review_ref="review-approved",
        decision_context=PlanDecisionContext(
            rationale="The fixed setup has a bounded continuation hypothesis.",
            evidence="The exact Demo cohort passed the repeatability screen.",
            limitations="Future path, fills, fees, and capacity remain unknown.",
            intent="PROFIT_SEEKING",
            setup_family="OTHER",
            playbook_ref="DIRECT_RULE_V1",
            invalidation="Do not enter once the fixed boundary fails.",
            evidence_cutoff=NOW,
        ),
        decision_basis=FixedDirectExecutionBasis(
            parameter_digest=content_digest({}),
            product_build_id="a" * 64,
        ),
        order_schedule_spec=spec,
        account_ref="copy-lead-account",
        venue_ref="BINANCE_USDM",
        instrument_ref="BTCUSDT-PERP",
        direction=Direction.LONG,
        target_exposure="100",
        requested_limits=RequestedLimits(
            max_margin="100",
            max_notional="100",
            max_allowed_loss="1",
        ),
        valid_from=NOW,
        valid_until=NOW + timedelta(days=30),
        allowed_actions=direct_allowed_action_profiles(spec),
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


def _snapshot():
    snapshot = compile_order_schedule(
        _spec(),
        InstrumentOrderRules(
            source="BINANCE_LIVE_EXCHANGE_INFO",
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
        instrument_ref="BTCUSDT-PERP",
        direction=Direction.LONG,
        max_notional="100",
        schedule_ref="plan-version-live-profit",
    )
    assert snapshot.valid
    return snapshot


def _decision_evidence() -> dict[str, object]:
    sample_refs = [
        {
            "review_id": f"review-{index:02d}",
            "review_version": 1,
            "review_content_digest": content_digest({"review": index}),
            "fact_cutoff": (NOW + timedelta(minutes=index)).isoformat(),
        }
        for index in range(30)
    ]
    return {
        "instrument_ref": "BTCUSDT-PERP",
        "direction": "LONG",
        "decision_basis_ref": DIRECT_EXECUTION_REF,
        "parameter_digest": content_digest({}),
        "intent": "PROFIT_SEEKING",
        "setup_family": "OTHER",
        "playbook_ref": "DIRECT_RULE_V1",
        "source": "CURRENT_COMPLETED_REVIEWS",
        # An excluded review may be newer, but it must not refresh the artifact.
        "source_cutoff": (NOW + timedelta(days=30)).isoformat(),
        "matched_review_count": 31,
        "comparable_trade_count": 30,
        "excluded_review_count": 1,
        "exclusions": {"VALIDATION_INTENT": 1},
        "sample_review_refs": sample_refs,
        "sample_traceability_complete": True,
        "sample_identity_digest": content_digest(sample_refs),
        "evidence_grade": "REPEATED_OBSERVATION_UNPROVEN",
        "repeated_sample_floor": 10,
        "metrics": {
            "gross_profit": "120",
            "gross_loss": "60",
            "profit_factor": "2",
        },
        "repeatability": {
            "status": "EVIDENCE_CANDIDATE",
            "reason_codes": [],
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


def _artifact(
    *,
    issued_at: datetime | None = None,
) -> tuple[PlaybookQualificationArtifact, str]:
    return build_playbook_qualification_artifact(
        _decision_evidence(),
        source_environment_id="binance-demo-primary",
        source_product_build_id="c" * 64,
        target_venue_account_type="USDM_COPY_LEAD",
        decision_basis_kind=DecisionBasisKind.DIRECT_EXECUTION,
        issued_at=issued_at or NOW + timedelta(hours=1),
    )


def _write_artifact(
    path: Path,
    artifact: PlaybookQualificationArtifact,
) -> SimpleNamespace:
    path.write_text(
        json.dumps(artifact.model_dump(mode="json", by_alias=True)),
        encoding="utf-8",
    )
    return SimpleNamespace(
        artifact_path=str(path),
        expected_content_digest=content_digest(artifact),
    )


def test_export_uses_only_the_comparable_sample_cutoff() -> None:
    artifact, digest = _artifact()

    assert artifact.evidence_cutoff == NOW + timedelta(minutes=29)
    assert artifact.evidence_cutoff < datetime.fromisoformat(
        str(_decision_evidence()["source_cutoff"])
    )
    assert artifact.comparable_trade_count == 30
    assert artifact.risk_basis_trade_count == 30
    assert digest == content_digest(artifact)
    assert artifact.live_activation_authority is False
    assert artifact.capital_scaling_authority is False


def test_artifact_revalidates_candidate_invariants() -> None:
    artifact, _ = _artifact()
    invalid = artifact.model_dump(mode="json", by_alias=True)
    invalid["net_r_without_best_trade"] = "0"

    with pytest.raises(ValueError, match="PLAYBOOK_QUALIFICATION_REPEATABILITY_INVALID"):
        PlaybookQualificationArtifact.model_validate(invalid)


def test_exact_current_artifact_is_an_eligible_input(tmp_path: Path) -> None:
    artifact, digest = _artifact()
    reference = _write_artifact(tmp_path / "qualification.json", artifact)

    status = evaluate_live_profit_qualification(
        _version(),
        environment_kind=EnvironmentKind.LIVE,
        target_venue_account_type="USDM_COPY_LEAD",
        references=(reference,),
        observed_at=artifact.evidence_cutoff + timedelta(days=1),
    )

    assert status.status == "ELIGIBLE_INPUT"
    assert status.admission_satisfied is True
    assert status.artifact_content_digest == digest
    assert status.comparable_trade_count == 30
    assert status.live_activation_authority is False


def test_digest_mismatch_fails_closed(tmp_path: Path) -> None:
    artifact, _ = _artifact()
    reference = _write_artifact(tmp_path / "qualification.json", artifact)
    reference.expected_content_digest = "f" * 64

    status = evaluate_live_profit_qualification(
        _version(),
        environment_kind=EnvironmentKind.LIVE,
        target_venue_account_type="USDM_COPY_LEAD",
        references=(reference,),
        observed_at=artifact.evidence_cutoff + timedelta(days=1),
    )

    assert status.status == "INVALID"
    assert status.blocker_codes == ("LIVE_PROFIT_QUALIFICATION_DIGEST_MISMATCH",)


def test_target_or_cohort_mismatch_is_not_accepted(tmp_path: Path) -> None:
    artifact, _ = _artifact()
    reference = _write_artifact(tmp_path / "qualification.json", artifact)

    status = evaluate_live_profit_qualification(
        _version(),
        environment_kind=EnvironmentKind.LIVE,
        target_venue_account_type="USDM_PERSONAL",
        references=(reference,),
        observed_at=artifact.evidence_cutoff + timedelta(days=1),
    )

    assert status.status == "NOT_MATCHED"
    assert status.eligible_input is False


def test_artifact_expires_seven_days_after_comparable_cutoff(tmp_path: Path) -> None:
    artifact, _ = _artifact()
    reference = _write_artifact(tmp_path / "qualification.json", artifact)

    status = evaluate_live_profit_qualification(
        _version(),
        environment_kind=EnvironmentKind.LIVE,
        target_venue_account_type="USDM_COPY_LEAD",
        references=(reference,),
        observed_at=artifact.evidence_cutoff + timedelta(days=7, seconds=1),
    )

    assert status.status == "STALE"
    assert status.blocker_codes == ("LIVE_PROFIT_QUALIFICATION_EVIDENCE_STALE",)


def test_multiple_current_exact_artifacts_are_ambiguous(tmp_path: Path) -> None:
    first, _ = _artifact()
    second, _ = _artifact(issued_at=NOW + timedelta(hours=2))
    references = (
        _write_artifact(tmp_path / "first.json", first),
        _write_artifact(tmp_path / "second.json", second),
    )

    status = evaluate_live_profit_qualification(
        _version(),
        environment_kind=EnvironmentKind.LIVE,
        target_venue_account_type="USDM_COPY_LEAD",
        references=references,
        observed_at=first.evidence_cutoff + timedelta(days=1),
    )

    assert status.status == "AMBIGUOUS"
    assert status.blocker_codes == (
        "LIVE_PROFIT_QUALIFICATION_MULTIPLE_CURRENT_ARTIFACTS",
    )


def test_demo_validation_and_position_disposition_do_not_require_handoff() -> None:
    version = _version()
    demo = evaluate_live_profit_qualification(
        version,
        environment_kind=EnvironmentKind.DEMO,
        target_venue_account_type="USDM_DEMO",
        references=(),
        observed_at=NOW,
    )
    assert demo.status == "NOT_APPLICABLE_DEMO"
    assert demo.admission_satisfied is True

    assert version.decision_context is not None
    validation_version = version.model_copy(
        update={
            "decision_context": version.decision_context.model_copy(
                update={"intent": PlanDecisionIntent.VALIDATION}
            )
        }
    )
    validation = evaluate_live_profit_qualification(
        validation_version,
        environment_kind=EnvironmentKind.LIVE,
        target_venue_account_type="USDM_COPY_LEAD",
        references=(),
        observed_at=NOW,
    )
    assert validation.status == "NOT_APPLICABLE_VALIDATION"

    disposition = evaluate_live_profit_qualification(
        SimpleNamespace(position_alignment=object()),
        environment_kind=EnvironmentKind.LIVE,
        target_venue_account_type="USDM_COPY_LEAD",
        references=(),
        observed_at=NOW,
    )
    assert disposition.status == "NOT_APPLICABLE_POSITION_DISPOSITION"


def test_live_profit_activation_has_a_server_side_qualification_gate() -> None:
    version = _version()
    service = object.__new__(PlanningApplicationService)
    service._environment_id = "binance-live-copy-primary"
    service._planning = SimpleNamespace(
        get_version=lambda _plan_version_id: version,
    )

    with pytest.raises(ValueError, match="LIVE_PROFIT_QUALIFICATION_NOT_CONFIGURED"):
        service.activate_version(
            plan_version_id=version.plan_version_id,
            activation_id="activation-live-profit",
            environment_kind=EnvironmentKind.LIVE,
            authority_class=AuthorityClass.LIVE_REAL_CAPITAL,
            observed_at=NOW + timedelta(minutes=1),
            ai_review_checker=_approved_ai_review,
        )

    with pytest.raises(ValueError, match="LIVE_PROFIT_QUALIFICATION_EVIDENCE_STALE"):
        service.activate_version(
            plan_version_id=version.plan_version_id,
            activation_id="activation-live-profit",
            environment_kind=EnvironmentKind.LIVE,
            authority_class=AuthorityClass.LIVE_REAL_CAPITAL,
            observed_at=NOW + timedelta(minutes=1),
            live_profit_qualification_checker=lambda _version: (_ for _ in ()).throw(
                ValueError("LIVE_PROFIT_QUALIFICATION_EVIDENCE_STALE")
            ),
            ai_review_checker=_approved_ai_review,
        )


def test_live_activation_persists_the_consumed_evidence_identity() -> None:
    version = _version()
    inserted = []
    service = object.__new__(PlanningApplicationService)
    service._environment_id = "binance-live-copy-primary"
    service._planning = SimpleNamespace(
        get_version=lambda _plan_version_id: version,
        lock_and_list_open_instrument_activations=lambda **_scope: (),
        insert_activation=inserted.append,
    )
    qualification_snapshot = {
        "status": "ELIGIBLE_INPUT",
        "required": True,
        "eligible_input": True,
        "blocker_codes": [],
        "artifact_content_digest": "d" * 64,
        "source_environment_id": "binance-demo-primary",
        "evidence_cutoff": NOW.isoformat(),
        "target_venue_account_type": "USDM_COPY_LEAD",
        "comparable_trade_count": 30,
        "risk_basis_trade_count": 30,
        "repeatability_policy_version": "PLAYBOOK_REPEATABILITY_SCREEN@1",
        "live_activation_authority": False,
        "capital_scaling_authority": False,
    }

    activation = service.activate_version(
        plan_version_id=version.plan_version_id,
        activation_id="activation-live-profit-qualified",
        environment_kind=EnvironmentKind.LIVE,
        authority_class=AuthorityClass.LIVE_REAL_CAPITAL,
        observed_at=NOW + timedelta(minutes=1),
        order_schedule_snapshot=_snapshot(),
        live_profit_qualification_checker=lambda _version: qualification_snapshot,
        ai_review_checker=_approved_ai_review,
    )

    assert inserted == [activation]
    assert activation.rule_state["live_profit_qualification"] == (
        qualification_snapshot
    )
