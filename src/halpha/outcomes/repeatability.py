"""Deterministic screening for an exact playbook's repeatability evidence."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from math import ceil, sqrt
from random import Random
from typing import Sequence

from halpha.domain_values import canonical_decimal


@dataclass(frozen=True)
class PlaybookRepeatabilityPolicy:
    """Transparent screening policy; never a capital or live-write authority."""

    policy_version: str = "PLAYBOOK_REPEATABILITY_SCREEN@1"
    minimum_trade_count: int = 30
    minimum_recent_trade_count: int = 10
    minimum_profit_factor: Decimal = Decimal("1.2")
    confidence_level: Decimal = Decimal("0.95")
    bootstrap_resamples: int = 4000


DEFAULT_PLAYBOOK_REPEATABILITY_POLICY = PlaybookRepeatabilityPolicy()


def assess_playbook_repeatability(
    *,
    intent: str,
    net_pnl_values: Sequence[Decimal],
    max_allowed_losses: Sequence[Decimal | None],
    gross_profit: Decimal,
    gross_loss: Decimal,
    policy: PlaybookRepeatabilityPolicy = DEFAULT_PLAYBOOK_REPEATABILITY_POLICY,
) -> dict[str, object]:
    """Return a conservative, read-only screen for one pre-trade-tagged cohort."""

    if len(net_pnl_values) != len(max_allowed_losses):
        raise ValueError("PLAYBOOK_REPEATABILITY_INPUT_LENGTH_MISMATCH")
    if policy.minimum_trade_count < 2:
        raise ValueError("PLAYBOOK_REPEATABILITY_MINIMUM_TRADE_COUNT_INVALID")
    if policy.minimum_recent_trade_count < 1:
        raise ValueError("PLAYBOOK_REPEATABILITY_RECENT_TRADE_COUNT_INVALID")
    if policy.minimum_profit_factor <= 1:
        raise ValueError("PLAYBOOK_REPEATABILITY_PROFIT_FACTOR_INVALID")
    if not Decimal("0.5") < policy.confidence_level < Decimal("1"):
        raise ValueError("PLAYBOOK_REPEATABILITY_CONFIDENCE_LEVEL_INVALID")
    if policy.bootstrap_resamples < 100:
        raise ValueError("PLAYBOOK_REPEATABILITY_BOOTSTRAP_RESAMPLES_INVALID")

    trade_count = len(net_pnl_values)
    valid_r_values = [
        net_pnl / max_loss
        for net_pnl, max_loss in zip(
            net_pnl_values,
            max_allowed_losses,
            strict=True,
        )
        if max_loss is not None and max_loss > 0
    ]
    risk_basis_trade_count = len(valid_r_values)
    risk_basis_complete = risk_basis_trade_count == trade_count
    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0
        else None
    )
    profit_factor_passed = (
        gross_loss == 0 and gross_profit > 0
    ) or (
        profit_factor is not None
        and profit_factor >= policy.minimum_profit_factor
    )

    net_r: Decimal | None = None
    average_r: Decimal | None = None
    net_r_without_best: Decimal | None = None
    early_count = 0
    recent_count = 0
    early_net_r: Decimal | None = None
    recent_net_r: Decimal | None = None
    lower_confidence_bound: Decimal | None = None
    block_length: int | None = None
    if risk_basis_complete and valid_r_values:
        net_r = sum(valid_r_values, Decimal(0))
        average_r = net_r / trade_count
        net_r_without_best = net_r - max(valid_r_values)
        recent_count = min(
            trade_count - 1,
            max(policy.minimum_recent_trade_count, ceil(trade_count / 3)),
        )
        early_count = trade_count - recent_count
        early_net_r = sum(valid_r_values[:early_count], Decimal(0))
        recent_net_r = sum(valid_r_values[early_count:], Decimal(0))
        if len(valid_r_values) >= 2:
            lower_confidence_bound, block_length = _moving_block_bootstrap_lower_mean(
                valid_r_values,
                confidence_level=policy.confidence_level,
                resamples=policy.bootstrap_resamples,
            )

    reason_codes: list[str] = []
    if intent == "VALIDATION":
        status = "NOT_APPLICABLE_VALIDATION"
    else:
        if trade_count < policy.minimum_trade_count:
            reason_codes.append("MINIMUM_SAMPLE_NOT_MET")
        if not risk_basis_complete:
            reason_codes.append("RISK_BASIS_INCOMPLETE")
        if trade_count >= policy.minimum_trade_count and risk_basis_complete:
            if net_r is None or net_r <= 0:
                reason_codes.append("NET_EXPECTANCY_NOT_POSITIVE")
            if not profit_factor_passed:
                reason_codes.append("PROFIT_FACTOR_MARGIN_NOT_MET")
            if net_r_without_best is None or net_r_without_best <= 0:
                reason_codes.append("BEST_TRADE_DEPENDENCE")
            if early_net_r is None or early_net_r <= 0:
                reason_codes.append("EARLY_SEGMENT_NOT_POSITIVE")
            if recent_net_r is None or recent_net_r <= 0:
                reason_codes.append("RECENT_SEGMENT_NOT_POSITIVE")
            if lower_confidence_bound is None or lower_confidence_bound <= 0:
                reason_codes.append("MEAN_R_CONFIDENCE_NOT_POSITIVE")
        status = "EVIDENCE_CANDIDATE" if not reason_codes else "NOT_READY"

    return {
        "status": status,
        "reason_codes": reason_codes,
        "policy_version": policy.policy_version,
        "minimum_trade_count": policy.minimum_trade_count,
        "minimum_profit_factor": canonical_decimal(
            policy.minimum_profit_factor
        ),
        "confidence_level_percent": canonical_decimal(
            policy.confidence_level * Decimal(100)
        ),
        "bootstrap_resamples": policy.bootstrap_resamples,
        "bootstrap_block_length": block_length,
        "risk_basis_trade_count": risk_basis_trade_count,
        "net_r_multiple": _optional_decimal(net_r),
        "average_r_multiple": _optional_decimal(average_r),
        "net_r_without_best_trade": _optional_decimal(net_r_without_best),
        "early_segment_trade_count": early_count,
        "early_segment_net_r": _optional_decimal(early_net_r),
        "recent_segment_trade_count": recent_count,
        "recent_segment_net_r": _optional_decimal(recent_net_r),
        "mean_r_lower_confidence_bound": _optional_decimal(
            lower_confidence_bound
        ),
        "live_promotion_authority": False,
        "capital_scaling_authority": False,
        "limitations": [
            "筛查只消费交易前已固定为同一签名的费用后结果；规则身份填写错误会使结论失真。",
            "移动区块自举只缓解短程连续性影响，不能证明未来市场分布、独立性或资金容量不变。",
            "通过仅表示证据值得进入下一步评估，不自动允许实盘、加本金或提高单笔风险。",
        ],
    }


def _moving_block_bootstrap_lower_mean(
    values: Sequence[Decimal],
    *,
    confidence_level: Decimal,
    resamples: int,
) -> tuple[Decimal, int]:
    """Deterministic circular moving-block bootstrap lower mean percentile."""

    count = len(values)
    if count < 2:
        raise ValueError("PLAYBOOK_REPEATABILITY_BOOTSTRAP_SAMPLE_TOO_SMALL")
    block_length = max(2, min(count, round(sqrt(count))))
    seed_material = "|".join(canonical_decimal(value) for value in values)
    seed = int.from_bytes(
        sha256(seed_material.encode("utf-8")).digest()[:8],
        byteorder="big",
    )
    random = Random(seed)  # noqa: S311 - deterministic statistics, not security
    means: list[float] = []
    float_values = [float(value) for value in values]
    for _ in range(resamples):
        sample: list[float] = []
        while len(sample) < count:
            start = random.randrange(count)
            sample.extend(
                float_values[(start + offset) % count]
                for offset in range(block_length)
            )
        means.append(sum(sample[:count]) / count)
    means.sort()
    tail_probability = float(Decimal(1) - confidence_level)
    lower_index = max(0, min(resamples - 1, int(tail_probability * resamples)))
    return Decimal(format(means[lower_index], ".12g")), block_length


def _optional_decimal(value: Decimal | None) -> str | None:
    return canonical_decimal(value) if value is not None else None
