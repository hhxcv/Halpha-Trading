"""Read-only evidence for chronological review results and their price paths."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from halpha.domain_values import canonical_decimal
from halpha.outcomes.price_path import ReviewPricePathInterval


ReviewSequenceScope = Literal["ACCOUNT_RESULTS", "PROFIT_SEEKING"]
ReviewSequenceResultKind = Literal["WIN", "LOSS", "FLAT"]

_PROFIT_SEEKING_CLASSIFICATIONS = frozenset(
    {"USABLE_SAMPLE", "TRADE_DECISION_ISSUE", "AS_EXPECTED"}
)
_LIMITATIONS = (
    "账户结果范围保留验证性交易、工具问题和待评价交易的可靠费用后结果；它描述账户发生了什么，不证明策略有效。",
    "盈利导向范围要求交易前目的、完成评价、允许的复盘分类、闭合结果、执行费用和策略归因全部可靠；历史缺项不补猜。",
    "连续段只表示按确切平仓时间相邻的同方向净结果，不证明市场状态、交易独立性或下一笔仍会延续。",
    "剔除最差一笔或最差三笔仅是事后尾部敏感性，不是策略表现；只有交易前固定并真实执行的规则才能证明损失可避免。",
    "名义金额回报不是账户权益收益率；缺少逐笔账户权益基线时不推算账户级未来表现。",
    "价路聚合只使用每笔末次入场与首次减仓之间的同环境完整 K 线；未知、无完整 K 线和同 K 线阈值先后不被改写。",
    "该投影不修改计划、复盘、资金边界或交易权限，也不授权加本金和放大风险。",
)


@dataclass(frozen=True)
class ReviewSequenceTrade:
    review: Mapping[str, Any]
    review_id: str
    review_version: int
    closed_at: datetime
    fact_cutoff: datetime | None
    instrument_ref: str | None
    direction: Literal["LONG", "SHORT"] | None
    classification: str | None
    intent: str | None
    net_pnl: Decimal
    commission: Decimal
    entry_notional: Decimal | None

    @property
    def result_kind(self) -> ReviewSequenceResultKind:
        if self.net_pnl > 0:
            return "WIN"
        if self.net_pnl < 0:
            return "LOSS"
        return "FLAT"


@dataclass(frozen=True)
class ReviewSequenceSelection:
    scope: ReviewSequenceScope
    range_start: datetime | None
    range_end: datetime | None
    source_review_count: int
    source_cutoff: datetime | None
    exclusions: dict[str, int]
    trades: tuple[ReviewSequenceTrade, ...]


def prepare_review_sequence(
    reviews: Iterable[Mapping[str, Any]],
    *,
    scope: ReviewSequenceScope,
    range_start: datetime | None = None,
    range_end: datetime | None = None,
) -> ReviewSequenceSelection:
    """Select exact chronological inputs without inferring missing trade intent."""

    if scope not in {"ACCOUNT_RESULTS", "PROFIT_SEEKING"}:
        raise ValueError("REVIEW_SEQUENCE_SCOPE_UNSUPPORTED")
    normalized_start = _optional_aware_datetime(range_start)
    normalized_end = _optional_aware_datetime(range_end)
    if range_start is not None and normalized_start is None:
        raise ValueError("REVIEW_SEQUENCE_RANGE_TIMEZONE_REQUIRED")
    if range_end is not None and normalized_end is None:
        raise ValueError("REVIEW_SEQUENCE_RANGE_TIMEZONE_REQUIRED")
    if (
        normalized_start is not None
        and normalized_end is not None
        and normalized_start > normalized_end
    ):
        raise ValueError("REVIEW_SEQUENCE_RANGE_INVALID")

    source = list(reviews)
    exclusions: dict[str, int] = {}
    selected: list[ReviewSequenceTrade] = []
    source_cutoffs = [
        value
        for review in source
        if (value := _aware_datetime(review.get("fact_cutoff"))) is not None
    ]

    def exclude(reason: str) -> None:
        exclusions[reason] = exclusions.get(reason, 0) + 1

    for review in source:
        context = _mapping(review.get("trade_context"))
        result = _mapping(review.get("resolved_trade_result"))
        fills = result.get("fills")
        fill_count = _nonnegative_int(result.get("fill_count"))
        if (
            result.get("result_scope") == "EXTERNAL_POSITION_DISPOSITION"
            or bool(_mapping(context.get("position_alignment")))
        ):
            exclude("EXTERNAL_POSITION_DISPOSITION")
            continue
        if fill_count == 0 or (
            fill_count is None
            and (not isinstance(fills, list) or len(fills) == 0)
        ):
            exclude("NO_ATTRIBUTED_TRADE")
            continue

        net_pnl = _decimal(result.get("net_pnl"))
        commission = _decimal(result.get("commission"))
        if (
            result.get("calculation_complete") is not True
            or result.get("closed") is not True
            or net_pnl is None
            or commission is None
            or commission < 0
        ):
            exclude("UNRELIABLE_RESULT")
            continue
        closed_at = _aware_datetime(result.get("last_fill_time"))
        if result.get("fill_times_complete") is not True or closed_at is None:
            exclude("FILL_TIME_UNKNOWN")
            continue
        if (
            (normalized_start is not None and closed_at < normalized_start)
            or (normalized_end is not None and closed_at > normalized_end)
        ):
            exclude("OUTSIDE_RANGE")
            continue

        evaluations = _mapping(review.get("evaluations"))
        owner = _mapping(evaluations.get("owner_conclusion"))
        classification = _optional_text(owner.get("result"))
        decision_context = _mapping(context.get("decision_context"))
        intent = _optional_text(decision_context.get("intent"))
        entry_notional = _positive_decimal(result.get("entry_notional"))

        if scope == "PROFIT_SEEKING":
            if intent != "PROFIT_SEEKING":
                exclude(
                    "VALIDATION_INTENT"
                    if intent == "VALIDATION"
                    else "MISSING_DECISION_INTENT"
                )
                continue
            if str(review.get("status", "")) != "COMPLETE":
                exclude("PENDING_REVIEW")
                continue
            if classification not in _PROFIT_SEEKING_CLASSIFICATIONS:
                exclude(classification or "MISSING_CLASSIFICATION")
                continue
            if result.get("execution_cost_complete") is not True:
                exclude("EXECUTION_COST_INCOMPLETE")
                continue
            if result.get("strategy_attribution_complete") is not True:
                exclude("STRATEGY_ATTRIBUTION_INCOMPLETE")
                continue
            if entry_notional is None:
                exclude("ENTRY_NOTIONAL_UNKNOWN")
                continue

        review_id = _optional_text(review.get("review_id"))
        review_version = _positive_int(review.get("review_version"))
        if review_id is None or review_version is None:
            exclude("REVIEW_IDENTITY_UNKNOWN")
            continue
        direction_value = _optional_text(context.get("direction"))
        direction = (
            direction_value if direction_value in {"LONG", "SHORT"} else None
        )
        selected.append(
            ReviewSequenceTrade(
                review=review,
                review_id=review_id,
                review_version=review_version,
                closed_at=closed_at,
                fact_cutoff=_aware_datetime(review.get("fact_cutoff")),
                instrument_ref=_optional_text(context.get("instrument_ref")),
                direction=direction,
                classification=classification,
                intent=intent,
                net_pnl=net_pnl,
                commission=commission,
                entry_notional=entry_notional,
            )
        )

    selected.sort(key=lambda item: (item.closed_at, item.review_id))
    return ReviewSequenceSelection(
        scope=scope,
        range_start=normalized_start,
        range_end=normalized_end,
        source_review_count=len(source),
        source_cutoff=max(source_cutoffs) if source_cutoffs else None,
        exclusions=dict(sorted(exclusions.items())),
        trades=tuple(selected),
    )


def summarize_review_sequence(
    selection: ReviewSequenceSelection,
    *,
    price_paths: Mapping[tuple[str, int], Mapping[str, Any]] | None = None,
    price_path_requested: bool = False,
    price_path_interval: ReviewPricePathInterval = "1m",
) -> dict[str, Any]:
    """Build sequence, streak, drawdown, tail and optional path evidence."""

    trades = selection.trades
    cumulative = Decimal(0)
    peak = Decimal(0)
    peak_trade: ReviewSequenceTrade | None = None
    maximum_drawdown = Decimal(0)
    drawdown_peak: ReviewSequenceTrade | None = None
    drawdown_trough: ReviewSequenceTrade | None = None
    trade_rows: list[dict[str, Any]] = []
    segment_builders: list[dict[str, Any]] = []

    for trade in trades:
        if (
            not segment_builders
            or segment_builders[-1]["result_kind"] != trade.result_kind
        ):
            segment_builders.append(
                {
                    "segment_index": len(segment_builders),
                    "result_kind": trade.result_kind,
                    "trades": [],
                }
            )
        segment = segment_builders[-1]
        segment["trades"].append(trade)

        cumulative += trade.net_pnl
        if cumulative > peak:
            peak = cumulative
            peak_trade = trade
        drawdown = peak - cumulative
        if drawdown > maximum_drawdown:
            maximum_drawdown = drawdown
            drawdown_peak = peak_trade
            drawdown_trough = trade
        path = (
            price_paths.get((trade.review_id, trade.review_version))
            if price_paths is not None
            else None
        )
        trade_rows.append(
            {
                "review_id": trade.review_id,
                "review_version": trade.review_version,
                "closed_at": trade.closed_at.isoformat(),
                "instrument_ref": trade.instrument_ref,
                "direction": trade.direction,
                "classification": trade.classification,
                "intent": trade.intent,
                "result_kind": trade.result_kind,
                "net_pnl": canonical_decimal(trade.net_pnl),
                "commission": canonical_decimal(trade.commission),
                "entry_notional": (
                    canonical_decimal(trade.entry_notional)
                    if trade.entry_notional is not None
                    else None
                ),
                "notional_return_percent": (
                    canonical_decimal(
                        trade.net_pnl / trade.entry_notional * Decimal(100)
                    )
                    if trade.entry_notional is not None
                    else None
                ),
                "cumulative_net_pnl": canonical_decimal(cumulative),
                "drawdown_from_peak": canonical_decimal(drawdown),
                "segment_index": int(segment["segment_index"]),
                "price_path": (
                    _public_trade_price_path(path)
                    if price_path_requested
                    else None
                ),
            }
        )

    segments = [
        _summarize_segment(
            builder,
            price_paths=price_paths,
            price_path_requested=price_path_requested,
            price_path_interval=price_path_interval,
        )
        for builder in segment_builders
    ]
    net_values = [trade.net_pnl for trade in trades]
    net_pnl = sum(net_values, Decimal(0))
    commission = sum((trade.commission for trade in trades), Decimal(0))
    gross_profit = sum((max(value, Decimal(0)) for value in net_values), Decimal(0))
    gross_loss = sum((max(-value, Decimal(0)) for value in net_values), Decimal(0))
    entry_notionals = [trade.entry_notional for trade in trades]
    total_entry_notional = (
        sum((value for value in entry_notionals if value is not None), Decimal(0))
        if trades and all(value is not None for value in entry_notionals)
        else None
    )
    loss_trades = sorted(
        (trade for trade in trades if trade.net_pnl < 0),
        key=lambda item: (item.net_pnl, item.closed_at, item.review_id),
    )
    worst_trade = loss_trades[0] if loss_trades else None
    top_losses = loss_trades[:3]
    top_loss_total = sum((-trade.net_pnl for trade in top_losses), Decimal(0))
    worst_loss = -worst_trade.net_pnl if worst_trade is not None else None

    current_segment_index = segments[-1]["segment_index"] if segments else None
    longest_win = _pick_segment(segments, "WIN", mode="LONGEST")
    longest_loss = _pick_segment(segments, "LOSS", mode="LONGEST")
    best_win = _pick_segment(segments, "WIN", mode="EXTREME")
    worst_loss_segment = _pick_segment(segments, "LOSS", mode="EXTREME")
    path_summary = (
        _summarize_price_paths(
            trades,
            price_paths or {},
            interval=price_path_interval,
            include_result_breakdown=True,
        )
        if price_path_requested
        else None
    )

    return {
        "scope": selection.scope,
        "range_start": (
            selection.range_start.isoformat()
            if selection.range_start is not None
            else None
        ),
        "range_end": (
            selection.range_end.isoformat()
            if selection.range_end is not None
            else None
        ),
        "source": "CURRENT_LATEST_REVIEWS",
        "source_cutoff": (
            selection.source_cutoff.isoformat()
            if selection.source_cutoff is not None
            else None
        ),
        "source_review_count": selection.source_review_count,
        "eligible_trade_count": len(trades),
        "excluded_review_count": sum(selection.exclusions.values()),
        "exclusions": selection.exclusions,
        "metrics": {
            "trade_count": len(trades),
            "wins": sum(value > 0 for value in net_values),
            "losses": len(loss_trades),
            "flat": sum(value == 0 for value in net_values),
            "net_pnl": canonical_decimal(net_pnl),
            "commission": canonical_decimal(commission),
            "gross_profit": canonical_decimal(gross_profit),
            "gross_loss": canonical_decimal(gross_loss),
            "profit_factor": (
                canonical_decimal(gross_profit / gross_loss)
                if gross_loss > 0
                else None
            ),
            "total_entry_notional": (
                canonical_decimal(total_entry_notional)
                if total_entry_notional is not None
                else None
            ),
            "notional_return_percent": (
                canonical_decimal(net_pnl / total_entry_notional * Decimal(100))
                if total_entry_notional is not None and total_entry_notional > 0
                else None
            ),
            "maximum_drawdown": canonical_decimal(maximum_drawdown),
            "maximum_drawdown_peak_review_id": (
                drawdown_peak.review_id if drawdown_peak is not None else None
            ),
            "maximum_drawdown_peak_at": (
                drawdown_peak.closed_at.isoformat()
                if drawdown_peak is not None
                else None
            ),
            "maximum_drawdown_trough_review_id": (
                drawdown_trough.review_id if drawdown_trough is not None else None
            ),
            "maximum_drawdown_trough_at": (
                drawdown_trough.closed_at.isoformat()
                if drawdown_trough is not None
                else None
            ),
            "current_segment_index": current_segment_index,
            "longest_win_segment_index": _segment_index(longest_win),
            "longest_loss_segment_index": _segment_index(longest_loss),
            "best_win_segment_index": _segment_index(best_win),
            "worst_loss_segment_index": _segment_index(worst_loss_segment),
            "worst_trade_review_id": (
                worst_trade.review_id if worst_trade is not None else None
            ),
            "worst_trade_net_pnl": (
                canonical_decimal(worst_trade.net_pnl)
                if worst_trade is not None
                else None
            ),
            "net_pnl_without_worst_trade": (
                canonical_decimal(net_pnl + worst_loss)
                if worst_loss is not None
                else None
            ),
            "largest_loss_share_percent": (
                canonical_decimal(worst_loss / gross_loss * Decimal(100))
                if worst_loss is not None and gross_loss > 0
                else None
            ),
            "top_loss_count": len(top_losses),
            "top_loss_total": (
                canonical_decimal(top_loss_total) if top_losses else None
            ),
            "net_pnl_without_top_losses": (
                canonical_decimal(net_pnl + top_loss_total)
                if top_losses
                else None
            ),
            "top_loss_share_percent": (
                canonical_decimal(top_loss_total / gross_loss * Decimal(100))
                if top_losses and gross_loss > 0
                else None
            ),
        },
        "segments": segments,
        "trades": trade_rows,
        "price_path": path_summary,
        "capital_scaling_authority": False,
        "limitations": list(_LIMITATIONS),
    }


def unavailable_sequence_price_path(
    *,
    interval: ReviewPricePathInterval,
    reason: str = "MARKET_READ_FAILED",
) -> dict[str, Any]:
    """Represent one batch-only market failure without inventing zero motion."""

    return {
        "evidence_status": "UNKNOWN",
        "reason_codes": [reason],
        "interval": interval,
        "market_source_cutoff": None,
        "ever_favorable_on_complete_bars": None,
        "maximum_favorable_excursion_percent": None,
        "maximum_adverse_excursion_percent": None,
        "maximum_favorable_excursion_r": None,
        "maximum_adverse_excursion_r": None,
        "first_complete_bar_favorable": None,
        "threshold_sequence": "UNKNOWN",
    }


def _summarize_segment(
    builder: Mapping[str, Any],
    *,
    price_paths: Mapping[tuple[str, int], Mapping[str, Any]] | None,
    price_path_requested: bool,
    price_path_interval: ReviewPricePathInterval,
) -> dict[str, Any]:
    trades = list(builder["trades"])
    net_pnl = sum((trade.net_pnl for trade in trades), Decimal(0))
    commission = sum((trade.commission for trade in trades), Decimal(0))
    entry_values = [trade.entry_notional for trade in trades]
    total_entry_notional = (
        sum((value for value in entry_values if value is not None), Decimal(0))
        if trades and all(value is not None for value in entry_values)
        else None
    )
    return {
        "segment_index": int(builder["segment_index"]),
        "result_kind": str(builder["result_kind"]),
        "start_at": trades[0].closed_at.isoformat(),
        "end_at": trades[-1].closed_at.isoformat(),
        "trade_count": len(trades),
        "net_pnl": canonical_decimal(net_pnl),
        "commission": canonical_decimal(commission),
        "total_entry_notional": (
            canonical_decimal(total_entry_notional)
            if total_entry_notional is not None
            else None
        ),
        "notional_return_percent": (
            canonical_decimal(net_pnl / total_entry_notional * Decimal(100))
            if total_entry_notional is not None and total_entry_notional > 0
            else None
        ),
        "review_refs": [
            {"review_id": trade.review_id, "review_version": trade.review_version}
            for trade in trades
        ],
        "price_path": (
            _summarize_price_paths(
                trades,
                price_paths or {},
                interval=price_path_interval,
                include_result_breakdown=False,
            )
            if price_path_requested
            else None
        ),
    }


def _pick_segment(
    segments: list[dict[str, Any]],
    kind: ReviewSequenceResultKind,
    *,
    mode: Literal["LONGEST", "EXTREME"],
) -> dict[str, Any] | None:
    candidates = [item for item in segments if item["result_kind"] == kind]
    if not candidates:
        return None
    if mode == "LONGEST":
        return sorted(
            candidates,
            key=lambda item: (
                -int(item["trade_count"]),
                Decimal(str(item["net_pnl"])) if kind == "LOSS" else -Decimal(str(item["net_pnl"])),
                int(item["segment_index"]),
            ),
        )[0]
    return sorted(
        candidates,
        key=lambda item: (
            Decimal(str(item["net_pnl"])) if kind == "LOSS" else -Decimal(str(item["net_pnl"])),
            -int(item["trade_count"]),
            int(item["segment_index"]),
        ),
    )[0]


def _summarize_price_paths(
    trades: Iterable[ReviewSequenceTrade],
    paths: Mapping[tuple[str, int], Mapping[str, Any]],
    *,
    interval: ReviewPricePathInterval,
    include_result_breakdown: bool,
) -> dict[str, Any]:
    trade_list = list(trades)
    path_list = [
        paths.get(
            (trade.review_id, trade.review_version),
            unavailable_sequence_price_path(interval=interval),
        )
        for trade in trade_list
    ]
    available = [
        path for path in path_list if path.get("evidence_status") == "AVAILABLE"
    ]
    reasons: dict[str, int] = {}
    threshold_counts: dict[str, int] = {}
    for path in path_list:
        for reason in path.get("reason_codes", []):
            reasons[str(reason)] = reasons.get(str(reason), 0) + 1
        threshold = str(path.get("threshold_sequence", "UNKNOWN"))
        threshold_counts[threshold] = threshold_counts.get(threshold, 0) + 1
    summary = {
        "requested": True,
        "interval": interval,
        "trade_count": len(path_list),
        "available_count": len(available),
        "unknown_count": sum(
            path.get("evidence_status") == "UNKNOWN" for path in path_list
        ),
        "not_applicable_count": sum(
            path.get("evidence_status") == "NOT_APPLICABLE" for path in path_list
        ),
        "ever_favorable_count": sum(
            path.get("ever_favorable_on_complete_bars") is True
            for path in available
        ),
        "never_favorable_count": sum(
            path.get("ever_favorable_on_complete_bars") is False
            for path in available
        ),
        "first_complete_bar_favorable_count": sum(
            path.get("first_complete_bar_favorable") is True for path in available
        ),
        "median_mfe_percent": _median_decimal_field(
            available, "maximum_favorable_excursion_percent"
        ),
        "median_mae_percent": _median_decimal_field(
            available, "maximum_adverse_excursion_percent"
        ),
        "median_mfe_r": _median_decimal_field(
            available, "maximum_favorable_excursion_r"
        ),
        "median_mae_r": _median_decimal_field(
            available, "maximum_adverse_excursion_r"
        ),
        "reason_counts": dict(sorted(reasons.items())),
        "threshold_counts": dict(sorted(threshold_counts.items())),
        "market_source_cutoff": max(
            (
                cutoff
                for path in available
                if (cutoff := _aware_datetime(path.get("market_source_cutoff")))
                is not None
            ),
            default=None,
        ),
    }
    summary["market_source_cutoff"] = (
        summary["market_source_cutoff"].isoformat()
        if summary["market_source_cutoff"] is not None
        else None
    )
    if include_result_breakdown:
        summary["by_result"] = {
            kind: _summarize_price_paths(
                (trade for trade in trade_list if trade.result_kind == kind),
                paths,
                interval=interval,
                include_result_breakdown=False,
            )
            for kind in ("WIN", "LOSS", "FLAT")
        }
    return summary


def _public_trade_price_path(path: Mapping[str, Any] | None) -> dict[str, Any]:
    source = path or unavailable_sequence_price_path(interval="1m")
    return {
        "evidence_status": str(source.get("evidence_status", "UNKNOWN")),
        "reason_codes": [str(item) for item in source.get("reason_codes", [])],
        "ever_favorable_on_complete_bars": source.get(
            "ever_favorable_on_complete_bars"
        ),
        "maximum_favorable_excursion_percent": source.get(
            "maximum_favorable_excursion_percent"
        ),
        "maximum_adverse_excursion_percent": source.get(
            "maximum_adverse_excursion_percent"
        ),
        "maximum_favorable_excursion_r": source.get(
            "maximum_favorable_excursion_r"
        ),
        "maximum_adverse_excursion_r": source.get(
            "maximum_adverse_excursion_r"
        ),
        "first_complete_bar_favorable": source.get(
            "first_complete_bar_favorable"
        ),
        "threshold_sequence": str(source.get("threshold_sequence", "UNKNOWN")),
    }


def _median_decimal_field(
    paths: Iterable[Mapping[str, Any]], field: str
) -> str | None:
    values = sorted(
        value
        for path in paths
        if (value := _decimal(path.get(field))) is not None
    )
    if not values:
        return None
    middle = len(values) // 2
    median = (
        values[middle]
        if len(values) % 2
        else (values[middle - 1] + values[middle]) / Decimal(2)
    )
    return canonical_decimal(median)


def _segment_index(segment: Mapping[str, Any] | None) -> int | None:
    return int(segment["segment_index"]) if segment is not None else None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _positive_decimal(value: object) -> Decimal | None:
    parsed = _decimal(value)
    return parsed if parsed is not None and parsed > 0 else None


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _positive_int(value: object) -> int | None:
    parsed = _nonnegative_int(value)
    return parsed if parsed is not None and parsed > 0 else None


def _aware_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _optional_aware_datetime(value: datetime | None) -> datetime | None:
    return _aware_datetime(value) if value is not None else None
