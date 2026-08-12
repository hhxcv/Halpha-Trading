"""Shared readers for frozen direct-execution protection state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from halpha.planning.models import PlanActivation


def direct_time_exit_at(activation: PlanActivation) -> datetime | None:
    """Return the persisted direct-protection deadline without scheduling work."""

    state = activation.rule_state.get("direct_protection")
    if not isinstance(state, dict):
        return None
    anchor_ref = state.get("anchor_fill_ref")
    fills = state.get("fills")
    if not isinstance(anchor_ref, str) or not isinstance(fills, dict):
        return None
    anchor = fills.get(anchor_ref)
    if not isinstance(anchor, dict):
        return None
    policy = anchor.get("protection_policy")
    fill_time_value = anchor.get("fill_time")
    if not isinstance(policy, dict) or not isinstance(fill_time_value, str):
        return None
    seconds = policy.get("time_exit_seconds")
    if seconds is None:
        return None
    if not isinstance(seconds, int) or seconds <= 0:
        raise ValueError("DIRECT_TIME_EXIT_INVALID")
    try:
        fill_time = datetime.fromisoformat(fill_time_value)
    except ValueError:
        raise ValueError("DIRECT_TIME_EXIT_INVALID") from None
    if fill_time.utcoffset() is None:
        raise ValueError("DIRECT_TIME_EXIT_INVALID")
    return fill_time.astimezone(UTC) + timedelta(seconds=seconds)
