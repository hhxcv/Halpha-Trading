"""Read-only price-path evidence for one attributed activation review."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from halpha.domain_values import canonical_decimal
from halpha.public_market import (
    MARKET_INTERVAL_MILLISECONDS,
    MAX_MARKET_WINDOW_BARS,
    MarketInterval,
    MarketWindow,
)


ReviewPricePathInterval = MarketInterval

CALCULATION_VERSION = "REVIEW_PRICE_PATH_V1"
_SUPPORTED_INTERVALS = frozenset(MARKET_INTERVAL_MILLISECONDS)
_REDUCING_ACTIONS = frozenset(
    {
        "PROTECTION",
        "TAKE_PROFIT",
        "RISK_REDUCTION",
        "EXIT",
        "EXTERNAL_ACCOUNT_CLOSURE",
    }
)
_LIMITATIONS = (
    "只使用末次入场成交之后、首次减仓成交之前已经完整闭合的同环境 K 线；入场与退出所在的部分 K 线不进入极值。",
    "K 线高低价不能证明同一根 K 线内阈值的先后；同时触及时保持顺序不确定。",
    "R 倍数按冻结订单计划的初始止损距离和实际平均入场价折算；缺少任一输入时保持未知。",
    "首根完整 K 线收盘仅是方向诊断，不代表等待收盘一定能够按该价格成交，也不构成策略反事实证明。",
    "该投影不自动判定交易决策对错、策略有效性或资金容量，也不授权扩大本金与风险。",
)


@dataclass(frozen=True)
class ReviewPricePathBasis:
    review_id: str
    review_version: int
    instrument_ref: str
    direction: Literal["LONG", "SHORT"]
    interval: ReviewPricePathInterval
    average_entry_price: Decimal
    last_entry_fill_at: datetime
    first_reduction_fill_at: datetime
    window_start_at: datetime
    window_end_open_at: datetime
    analysis_end_at: datetime
    complete_bar_count: int
    initial_stop_distance_bps: Decimal | None
    response_seed: dict[str, Any]


def prepare_review_price_path(
    review: Mapping[str, Any],
    *,
    interval: ReviewPricePathInterval,
) -> ReviewPricePathBasis | dict[str, Any]:
    """Resolve exact review inputs before requesting any public market bars."""

    if interval not in _SUPPORTED_INTERVALS:
        raise ValueError("REVIEW_PRICE_PATH_INTERVAL_UNSUPPORTED")

    review_id = str(review.get("review_id", ""))
    try:
        review_version = int(review.get("review_version", 0))
    except (TypeError, ValueError):
        review_version = 0
    context = _mapping(review.get("trade_context"))
    result = _mapping(review.get("resolved_trade_result"))
    instrument_ref = _optional_text(context.get("instrument_ref"))
    direction_value = _optional_text(context.get("direction"))
    direction = direction_value if direction_value in {"LONG", "SHORT"} else None
    seed = _empty_response(
        review_id=review_id,
        review_version=review_version,
        instrument_ref=instrument_ref,
        direction=direction,
        interval=interval,
    )

    position_alignment = _mapping(context.get("position_alignment"))
    if position_alignment or result.get("result_scope") == "EXTERNAL_POSITION_DISPOSITION":
        return _terminal(seed, "NOT_APPLICABLE", "EXTERNAL_POSITION_DISPOSITION")

    fills = review.get("resolved_trade_result", {})
    raw_fills = fills.get("fills") if isinstance(fills, Mapping) else None
    fill_records = [item for item in raw_fills if isinstance(item, Mapping)] if isinstance(raw_fills, list) else []
    entry_records = [item for item in fill_records if item.get("action_kind") == "ENTRY"]
    reducing_records = [
        item for item in fill_records if item.get("action_kind") in _REDUCING_ACTIONS
    ]
    if not entry_records:
        status = (
            "NOT_APPLICABLE"
            if str(review.get("primary_result", "")) == "NO_ACTION"
            or result.get("fill_count") == 0
            else "UNKNOWN"
        )
        return _terminal(seed, status, "NO_ATTRIBUTED_ENTRY")
    if result.get("closed") is not True:
        return _terminal(seed, "UNKNOWN", "TRADE_NOT_CLOSED")
    if result.get("strategy_attribution_complete") is not True:
        return _terminal(seed, "UNKNOWN", "STRATEGY_ATTRIBUTION_INCOMPLETE")
    if result.get("fill_times_complete") is not True:
        return _terminal(seed, "UNKNOWN", "FILL_TIMES_INCOMPLETE")
    if direction is None:
        return _terminal(seed, "UNKNOWN", "DIRECTION_UNKNOWN")

    average_entry_price = _positive_decimal(result.get("average_entry_price"))
    if average_entry_price is None:
        return _terminal(seed, "UNKNOWN", "AVERAGE_ENTRY_PRICE_UNKNOWN")
    entry_times = tuple(
        value
        for value in (_aware_datetime(item.get("fill_time")) for item in entry_records)
        if value is not None
    )
    reduction_times = tuple(
        value
        for value in (
            _aware_datetime(item.get("fill_time")) for item in reducing_records
        )
        if value is not None
    )
    if len(entry_times) != len(entry_records) or len(reduction_times) != len(
        reducing_records
    ):
        return _terminal(seed, "UNKNOWN", "FILL_TIMES_INCOMPLETE")
    if not reduction_times:
        return _terminal(seed, "UNKNOWN", "FIRST_REDUCTION_UNKNOWN")

    last_entry_fill_at = max(entry_times)
    first_reduction_fill_at = min(reduction_times)
    seed.update(
        {
            "average_entry_price": canonical_decimal(average_entry_price),
            "last_entry_fill_at": last_entry_fill_at.isoformat(),
            "first_reduction_fill_at": first_reduction_fill_at.isoformat(),
        }
    )
    if first_reduction_fill_at <= last_entry_fill_at:
        return _terminal(seed, "UNKNOWN", "FILL_SEQUENCE_INVALID")

    interval_delta = timedelta(
        milliseconds=MARKET_INTERVAL_MILLISECONDS[interval]
    )
    window_start_at = _ceil_boundary(last_entry_fill_at, interval_delta)
    analysis_end_at = _floor_boundary(first_reduction_fill_at, interval_delta)
    window_end_open_at = analysis_end_at - interval_delta
    seed.update(
        {
            "analysis_start_at": window_start_at.isoformat(),
            "analysis_end_at": analysis_end_at.isoformat(),
        }
    )
    if window_end_open_at < window_start_at:
        return _terminal(seed, "UNKNOWN", "NO_COMPLETE_HOLDING_BAR")

    complete_bar_count = int(
        (window_end_open_at - window_start_at) / interval_delta
    ) + 1
    seed["complete_bar_count"] = complete_bar_count
    if complete_bar_count > MAX_MARKET_WINDOW_BARS:
        return _terminal(seed, "UNKNOWN", "INTERVAL_TOO_FINE")

    initial_stop_distance_bps = _initial_stop_distance_bps(context)
    if initial_stop_distance_bps is None:
        seed["reason_codes"] = ["INITIAL_RISK_UNKNOWN"]
    else:
        seed["initial_stop_distance_bps"] = canonical_decimal(
            initial_stop_distance_bps
        )
        risk_distance = average_entry_price * initial_stop_distance_bps / Decimal(
            10_000
        )
        planned_stop = (
            average_entry_price - risk_distance
            if direction == "LONG"
            else average_entry_price + risk_distance
        )
        seed["planned_stop_price"] = canonical_decimal(planned_stop)

    return ReviewPricePathBasis(
        review_id=review_id,
        review_version=review_version,
        instrument_ref=instrument_ref or "",
        direction=direction,
        interval=interval,
        average_entry_price=average_entry_price,
        last_entry_fill_at=last_entry_fill_at,
        first_reduction_fill_at=first_reduction_fill_at,
        window_start_at=window_start_at,
        window_end_open_at=window_end_open_at,
        analysis_end_at=analysis_end_at,
        complete_bar_count=complete_bar_count,
        initial_stop_distance_bps=initial_stop_distance_bps,
        response_seed=seed,
    )


def summarize_review_price_path(
    basis: ReviewPricePathBasis,
    window: MarketWindow,
) -> dict[str, Any]:
    """Project directional excursion and threshold order from complete bars."""

    seed = dict(basis.response_seed)
    if (
        window.instrument_ref != basis.instrument_ref
        or window.interval != basis.interval
        or len(window.bars) != basis.complete_bar_count
        or window.bars[0].open_at != basis.window_start_at
        or window.bars[-1].open_at != basis.window_end_open_at
        or window.bars[-1].close_at != basis.analysis_end_at
    ):
        return _terminal(seed, "UNKNOWN", "MARKET_WINDOW_COVERAGE_MISMATCH")
    if window.source_cutoff < basis.analysis_end_at:
        return _terminal(seed, "UNKNOWN", "MARKET_WINDOW_NOT_CLOSED")

    direction_sign = Decimal(1) if basis.direction == "LONG" else Decimal(-1)
    if basis.direction == "LONG":
        best_bar = max(window.bars, key=lambda item: Decimal(item.high))
        worst_bar = min(window.bars, key=lambda item: Decimal(item.low))
        best_price = Decimal(best_bar.high)
        worst_price = Decimal(worst_bar.low)
    else:
        best_bar = min(window.bars, key=lambda item: Decimal(item.low))
        worst_bar = max(window.bars, key=lambda item: Decimal(item.high))
        best_price = Decimal(best_bar.low)
        worst_price = Decimal(worst_bar.high)

    best_move = direction_sign * (best_price - basis.average_entry_price)
    worst_move = direction_sign * (worst_price - basis.average_entry_price)
    mfe = max(best_move, Decimal(0))
    mae = max(-worst_move, Decimal(0))
    first_bar = window.bars[0]
    first_close = Decimal(first_bar.close)
    first_close_move = direction_sign * (first_close - basis.average_entry_price)

    seed.update(
        {
            "evidence_status": "AVAILABLE",
            "market_source": window.source,
            "market_source_cutoff": window.source_cutoff.isoformat(),
            "best_favorable_price": canonical_decimal(best_price),
            "best_favorable_bar_open_at": best_bar.open_at.isoformat(),
            "worst_adverse_price": canonical_decimal(worst_price),
            "worst_adverse_bar_open_at": worst_bar.open_at.isoformat(),
            "best_directional_move_percent": _percent(
                best_move, basis.average_entry_price
            ),
            "worst_directional_move_percent": _percent(
                worst_move, basis.average_entry_price
            ),
            "maximum_favorable_excursion_percent": _percent(
                mfe, basis.average_entry_price
            ),
            "maximum_adverse_excursion_percent": _percent(
                mae, basis.average_entry_price
            ),
            "ever_favorable_on_complete_bars": best_move > 0,
            "first_complete_bar_close_price": canonical_decimal(first_close),
            "first_complete_bar_close_at": first_bar.close_at.isoformat(),
            "first_complete_bar_directional_return_percent": _percent(
                first_close_move, basis.average_entry_price
            ),
            "first_complete_bar_favorable": first_close_move > 0,
        }
    )

    if basis.initial_stop_distance_bps is None:
        return seed

    risk_distance = (
        basis.average_entry_price
        * basis.initial_stop_distance_bps
        / Decimal(10_000)
    )
    one_r_touches: list[int] = []
    two_r_touches: list[int] = []
    stop_touches: list[int] = []
    for index, bar in enumerate(window.bars):
        high = Decimal(bar.high)
        low = Decimal(bar.low)
        if basis.direction == "LONG":
            one_r = high >= basis.average_entry_price + risk_distance
            two_r = high >= basis.average_entry_price + risk_distance * 2
            stop = low <= basis.average_entry_price - risk_distance
        else:
            one_r = low <= basis.average_entry_price - risk_distance
            two_r = low <= basis.average_entry_price - risk_distance * 2
            stop = high >= basis.average_entry_price + risk_distance
        if one_r:
            one_r_touches.append(index)
        if two_r:
            two_r_touches.append(index)
        if stop:
            stop_touches.append(index)

    one_r_index = one_r_touches[0] if one_r_touches else None
    stop_index = stop_touches[0] if stop_touches else None
    seed.update(
        {
            "maximum_favorable_excursion_r": canonical_decimal(
                mfe / risk_distance
            ),
            "maximum_adverse_excursion_r": canonical_decimal(
                mae / risk_distance
            ),
            "one_r_touched": one_r_index is not None,
            "two_r_touched": bool(two_r_touches),
            "planned_stop_touched": stop_index is not None,
            "threshold_sequence": _threshold_sequence(one_r_index, stop_index),
        }
    )
    return seed


def _empty_response(
    *,
    review_id: str,
    review_version: int,
    instrument_ref: str | None,
    direction: str | None,
    interval: ReviewPricePathInterval,
) -> dict[str, Any]:
    return {
        "review_id": review_id,
        "review_version": review_version,
        "evidence_status": "UNKNOWN",
        "reason_codes": [],
        "instrument_ref": instrument_ref,
        "direction": direction,
        "interval": interval,
        "calculation_version": CALCULATION_VERSION,
        "market_source": None,
        "market_source_cutoff": None,
        "last_entry_fill_at": None,
        "first_reduction_fill_at": None,
        "analysis_start_at": None,
        "analysis_end_at": None,
        "complete_bar_count": 0,
        "average_entry_price": None,
        "initial_stop_distance_bps": None,
        "planned_stop_price": None,
        "best_favorable_price": None,
        "best_favorable_bar_open_at": None,
        "worst_adverse_price": None,
        "worst_adverse_bar_open_at": None,
        "best_directional_move_percent": None,
        "worst_directional_move_percent": None,
        "maximum_favorable_excursion_percent": None,
        "maximum_adverse_excursion_percent": None,
        "maximum_favorable_excursion_r": None,
        "maximum_adverse_excursion_r": None,
        "ever_favorable_on_complete_bars": None,
        "first_complete_bar_close_price": None,
        "first_complete_bar_close_at": None,
        "first_complete_bar_directional_return_percent": None,
        "first_complete_bar_favorable": None,
        "one_r_touched": None,
        "two_r_touched": None,
        "planned_stop_touched": None,
        "threshold_sequence": "UNKNOWN",
        "capital_scaling_authority": False,
        "limitations": list(_LIMITATIONS),
    }


def _terminal(
    seed: Mapping[str, Any],
    status: Literal["NOT_APPLICABLE", "UNKNOWN"],
    reason: str,
) -> dict[str, Any]:
    result = dict(seed)
    result["evidence_status"] = status
    existing = [str(item) for item in result.get("reason_codes", [])]
    result["reason_codes"] = list(dict.fromkeys([*existing, reason]))
    return result


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _positive_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


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


def _ceil_boundary(value: datetime, interval: timedelta) -> datetime:
    interval_seconds = int(interval.total_seconds())
    epoch_seconds = int(value.timestamp())
    boundary = (epoch_seconds // interval_seconds) * interval_seconds
    if value > datetime.fromtimestamp(boundary, tz=UTC):
        boundary += interval_seconds
    return datetime.fromtimestamp(boundary, tz=UTC)


def _floor_boundary(value: datetime, interval: timedelta) -> datetime:
    interval_seconds = int(interval.total_seconds())
    boundary = int(value.timestamp()) // interval_seconds * interval_seconds
    return datetime.fromtimestamp(boundary, tz=UTC)


def _initial_stop_distance_bps(context: Mapping[str, Any]) -> Decimal | None:
    snapshot = _mapping(context.get("order_schedule_snapshot"))
    schedule = _mapping(snapshot.get("schedule_spec"))
    protection = _mapping(schedule.get("protection_policy"))
    initial_stop = _mapping(protection.get("initial_stop"))
    return _positive_decimal(initial_stop.get("distance_bps"))


def _percent(numerator: Decimal, denominator: Decimal) -> str:
    return canonical_decimal(numerator / denominator * Decimal(100))


def _threshold_sequence(one_r_index: int | None, stop_index: int | None) -> str:
    if one_r_index is None and stop_index is None:
        return "NEITHER"
    if one_r_index is not None and stop_index is None:
        return "ONE_R_ONLY"
    if one_r_index is None and stop_index is not None:
        return "STOP_ONLY"
    if one_r_index == stop_index:
        return "SAME_BAR_AMBIGUOUS"
    if one_r_index is not None and stop_index is not None and one_r_index < stop_index:
        return "ONE_R_BEFORE_STOP"
    return "STOP_BEFORE_ONE_R"
