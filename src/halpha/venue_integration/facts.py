"""Pure DAT fact construction and same-environment attribution checks."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from uuid import NAMESPACE_URL, UUID, uuid5

from halpha.domain_values import (
    canonical_decimal,
    content_digest,
    decimal_from_string,
)
from halpha.venue_integration.models import (
    ExecutionAction,
    ExecutionActionKind,
    VenueFact,
    VenueFactAttributionClass,
    VenueFactKind,
    VenueFactSourceClass,
    venue_fact_content_digest,
)


TERMINAL_ORDER_STATUSES = frozenset(
    {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
)


def _trade_fact_role(fact: VenueFact) -> str:
    if fact.action_ref is not None:
        return "HALPHA_EXECUTION"
    impact_scope = fact.impact_scope
    if (
        isinstance(impact_scope, dict)
        and impact_scope.get("classification") == "EXTERNAL_ACCOUNT_CLOSURE"
    ):
        return "EXTERNAL_ACCOUNT_CLOSURE"
    if fact.attribution_class is not None:
        return f"ATTRIBUTED:{fact.attribution_class.value}"
    return "UNCLAIMED_VENUE_ACTIVITY"


def venue_trade_fact_is_canonicalizable(fact: VenueFact) -> bool:
    return (
        fact.kind in {VenueFactKind.FILL, VenueFactKind.COMMISSION}
        and fact.impact_scope is None
        and fact.supersedes_ref is None
        and fact.correction_reason is None
        and fact.correction_evidence_refs is None
        and fact.correction_effective_time is None
        and fact.affected_reference_refs is None
        and _trade_fact_role(fact)
        in {"HALPHA_EXECUTION", "UNCLAIMED_VENUE_ACTIVITY"}
    )


def _canonical_money_amount(value: object, currency: str) -> str:
    parts = str(value).split()
    if not parts or len(parts) > 2:
        raise ValueError("VENUE_TRADE_COMMISSION_INVALID")
    if len(parts) == 2 and parts[1].upper() != currency:
        raise ValueError("VENUE_TRADE_COMMISSION_CURRENCY_MISMATCH")
    return canonical_decimal(
        decimal_from_string(
            parts[0],
            code="VENUE_TRADE_COMMISSION_INVALID",
        )
    )


def venue_trade_economic_version_key(
    fact: VenueFact,
    *,
    include_role: bool = True,
) -> dict[str, Any]:
    """Return a source-path-independent key for one fill or commission version."""

    if fact.kind not in {VenueFactKind.FILL, VenueFactKind.COMMISSION}:
        raise ValueError("VENUE_TRADE_FACT_REQUIRED")
    trade_id = str(fact.source_object_id or "")
    if not trade_id or str(fact.payload.get("trade_id", "")) != trade_id:
        raise ValueError("VENUE_TRADE_IDENTITY_MISMATCH")
    common: dict[str, Any] = {
        "version": 1,
        "environment_id": fact.environment_id,
        "venue_ref": fact.venue_ref,
        "account_ref": fact.account_ref,
        "instrument_ref": fact.instrument_ref,
        "kind": fact.kind.value,
        "trade_id": trade_id,
        "schema_version": fact.schema_version,
        "source_time": fact.source_time,
    }
    if include_role:
        common["fact_role"] = _trade_fact_role(fact)
    if fact.kind is VenueFactKind.FILL:
        common["economic_payload"] = {
            "trade_id": str(fact.payload.get("trade_id", "")),
            "client_order_id": fact.payload.get("client_order_id"),
            "venue_order_ref": fact.payload.get("venue_order_ref"),
            "last_price": canonical_decimal(
                decimal_from_string(
                    str(fact.payload.get("last_price", "")),
                    code="VENUE_TRADE_PRICE_INVALID",
                    positive=True,
                )
            ),
            "last_quantity": canonical_decimal(
                decimal_from_string(
                    str(fact.payload.get("last_quantity", "")),
                    code="VENUE_TRADE_QUANTITY_INVALID",
                    positive=True,
                )
            ),
            "order_side": str(fact.payload.get("order_side", "")).upper(),
            "liquidity_side": str(
                fact.payload.get("liquidity_side", "")
            ).upper(),
        }
    else:
        raw_currency = fact.payload.get("currency")
        if not isinstance(raw_currency, str) or not raw_currency.strip():
            raise ValueError("VENUE_TRADE_COMMISSION_CURRENCY_REQUIRED")
        currency = raw_currency.strip().upper()
        common["economic_payload"] = {
            "trade_id": str(fact.payload.get("trade_id", "")),
            "client_order_id": fact.payload.get("client_order_id"),
            "amount": _canonical_money_amount(
                fact.payload.get("amount", ""),
                currency,
            ),
            "currency": currency,
        }
    return common


def venue_trade_economic_version_digest(
    fact: VenueFact,
    *,
    include_role: bool = True,
) -> str:
    return content_digest(
        venue_trade_economic_version_key(fact, include_role=include_role)
    )


def venue_trade_fact_id(fact: VenueFact) -> str:
    # Attribution is a local claim about an external trade, not part of that
    # trade's economic identity.  Excluding it makes competing attribution
    # claims collide on the same primary key so a concurrent loser must read
    # the winner and fail closed instead of persisting a second fact.
    digest = venue_trade_economic_version_digest(fact, include_role=False)
    return str(
        uuid5(
            NAMESPACE_URL,
            f"urn:halpha:venue-trade-version:v1:{digest}",
        )
    )


def collapse_synthetic_reconciliation_fills(
    facts: Iterable[VenueFact],
) -> tuple[VenueFact, ...]:
    """Prefer Binance trade-query fills over Nautilus query placeholders.

    A Binance order-status reconciliation can synthesize a UUID or ``S-``
    trade identity from cumulative order data.  Once the authenticated
    user-trade query supplies the real numeric trade identity, retaining both
    observations would double the same fill.  Collapse only exact
    action/order/time/price/quantity matches and keep the placeholder when no
    stronger trade fact exists.
    """

    materialized = tuple(facts)
    authoritative_keys = {
        key
        for fact in materialized
        if fact.kind is VenueFactKind.FILL
        and not _synthetic_reconciliation_fill(fact)
        if (key := _fill_execution_key(fact)) is not None
    }
    return tuple(
        fact
        for fact in materialized
        if not (
            _synthetic_reconciliation_fill(fact)
            and _fill_execution_key(fact) in authoritative_keys
        )
    )


def _synthetic_reconciliation_fill(fact: VenueFact) -> bool:
    if (
        fact.kind is not VenueFactKind.FILL
        or fact.payload.get("reconciliation") is not True
        or fact.payload.get("event_type") != "OrderFilled"
    ):
        return False
    return synthetic_reconciliation_trade_id(fact.payload.get("trade_id"))


def synthetic_reconciliation_trade_id(trade_id: object) -> bool:
    """Recognize the placeholder identities emitted by qualified Nautilus builds."""

    rendered = str(trade_id or "")
    if rendered.startswith("S-") and len(rendered) > 2:
        return True
    try:
        UUID(rendered)
    except (TypeError, ValueError):
        return False
    return True


def _fill_execution_key(fact: VenueFact) -> tuple[object, ...] | None:
    try:
        price = canonical_decimal(
            decimal_from_string(
                str(fact.payload.get("last_price", "")),
                code="VENUE_TRADE_PRICE_INVALID",
                positive=True,
            )
        )
        quantity = canonical_decimal(
            decimal_from_string(
                str(fact.payload.get("last_quantity", "")),
                code="VENUE_TRADE_QUANTITY_INVALID",
                positive=True,
            )
        )
    except ValueError:
        return None
    return (
        getattr(fact, "environment_id", None),
        getattr(fact, "account_ref", None),
        getattr(fact, "instrument_ref", None),
        getattr(fact, "activation_ref", None),
        getattr(fact, "action_ref", None),
        fact.payload.get("client_order_id"),
        fact.payload.get("venue_order_ref"),
        getattr(fact, "source_time", None),
        price,
        quantity,
        str(fact.payload.get("order_side", "")).upper(),
    )


def latest_execution_status(facts: Iterable[VenueFact]) -> str | None:
    """Project technical order status from authoritative Nautilus facts.

    The durable execution action deliberately does not copy Nautilus' order
    lifecycle. Consumers that need the current technical status derive it from
    the original order and fill observations.
    """

    observations: list[tuple[tuple[datetime, datetime, datetime, str], str]] = []
    for fact in facts:
        status: str | None = None
        if fact.kind is VenueFactKind.ORDER_STATE:
            value = str(fact.payload.get("status", "")).upper()
            status = {
                "ACCEPTED": "WORKING",
                "ACKNOWLEDGED": "WORKING",
                "NEW": "WORKING",
                "CANCELED": "CANCELLED",
            }.get(value, value)
        elif fact.kind is VenueFactKind.FILL:
            try:
                leaves = Decimal(str(fact.payload.get("leaves_quantity")))
            except (InvalidOperation, TypeError, ValueError):
                status = "PARTIALLY_FILLED"
            else:
                status = "FILLED" if leaves == 0 else "PARTIALLY_FILLED"
        if status:
            observations.append(
                (
                    (
                        fact.source_time or fact.cutoff,
                        fact.cutoff,
                        fact.received_at,
                        fact.venue_fact_id,
                    ),
                    status,
                )
            )
    if not observations:
        return None
    terminal_observations = tuple(
        observation
        for observation in observations
        if observation[1] in TERMINAL_ORDER_STATUSES
    )
    if terminal_observations:
        # Nautilus can emit a late OrderUpdated callback after OrderFilled.
        # Retain the raw callback as a fact, but never let a non-terminal
        # callback reopen a venue order whose terminal result is already
        # authoritative. Competing terminal facts still resolve by event order.
        return max(terminal_observations)[1]
    return max(observations)[1]


def order_is_working(facts: Iterable[VenueFact]) -> bool:
    return latest_execution_status(facts) in {"WORKING", "PARTIALLY_FILLED"}


def terminal_order_status(facts: Iterable[VenueFact]) -> str | None:
    status = latest_execution_status(facts)
    return status if status in TERMINAL_ORDER_STATUSES else None


def terminal_fills_complete(
    action: ExecutionAction,
    facts: Iterable[VenueFact],
) -> bool:
    """Prove that every fill preceding a terminal order result is persisted.

    A terminal status alone is not a cumulative execution report.  In
    particular, CANCELLED and EXPIRED can race a final fill callback.  The
    terminal observation must therefore carry the venue-derived cumulative
    filled quantity, and the locally persisted fill facts must add up to it.
    A final fill with zero leaves is the equivalent cumulative proof for the
    Nautilus path which does not emit a separate FILLED order-state callback.
    """

    materialized = collapse_synthetic_reconciliation_fills(facts)
    try:
        requested = Decimal(str(action.action_terms["quantity"]))
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return False
    if requested < 0:
        return False

    persisted = Decimal(0)
    for fact in materialized:
        if fact.kind is not VenueFactKind.FILL:
            continue
        try:
            quantity = Decimal(str(fact.payload.get("last_quantity")))
        except (InvalidOperation, TypeError, ValueError):
            return False
        if quantity <= 0:
            return False
        persisted += quantity

    # Binance conditional orders can emit a wrapper cancellation milliseconds
    # after the generated ordinary child order was fully filled.  Exact user
    # trades plus a FILLED child-order query prove the whole position effect;
    # the later wrapper terminal must not reopen that completed responsibility.
    if persisted == requested and any(
        terminal_order_status((fact,)) == "FILLED"
        and _terminal_cumulative_quantity(fact, requested) == requested
        for fact in materialized
    ):
        return True

    terminal_cumulatives: list[Decimal] = []
    for terminal_fact in materialized:
        status = terminal_order_status((terminal_fact,))
        if status is None:
            continue
        cumulative = _terminal_cumulative_quantity(terminal_fact, requested)
        if cumulative is None:
            continue
        if cumulative < 0 or cumulative > requested:
            return False
        if status == "FILLED" and cumulative != requested:
            return False
        if status == "REJECTED" and cumulative != 0:
            return False
        terminal_cumulatives.append(cumulative)
    if not terminal_cumulatives:
        return False

    # Cumulative execution for one venue order is monotonic.  Nautilus startup
    # recovery can replay an OrderExpired/OrderCanceled callback with zero after
    # Binance's authenticated order query already reported the real partial
    # fill.  The replay remains useful terminal evidence, but must not erase the
    # stronger cumulative quantity.  Taking the greatest proven cumulative also
    # keeps a cancel/final-fill race closed until every reported fill is stored.
    return persisted == max(terminal_cumulatives)


def _terminal_cumulative_quantity(
    terminal_fact: VenueFact,
    requested: Decimal,
) -> Decimal | None:
    cumulative_raw = terminal_fact.payload.get("cumulative_filled_quantity")
    if cumulative_raw is not None:
        try:
            return Decimal(str(cumulative_raw))
        except (InvalidOperation, TypeError, ValueError):
            return None
    try:
        terminal_leaves = Decimal(
            str(terminal_fact.payload.get("leaves_quantity"))
        )
    except (InvalidOperation, TypeError, ValueError):
        return None
    if (
        terminal_fact.kind is VenueFactKind.FILL
        and terminal_order_status((terminal_fact,)) == "FILLED"
        and terminal_leaves == 0
    ):
        return requested
    return None


def terminal_fills_accounted_for_exit(
    action: ExecutionAction,
    facts: Iterable[VenueFact],
) -> bool:
    """Resolve fill/cancel competition without accepting quantity drift as closure."""

    materialized = collapse_synthetic_reconciliation_fills(facts)
    if terminal_fills_complete(action, materialized):
        return True
    terminal_fact = _latest_terminal_fact(materialized)
    if terminal_fact is None:
        return False
    if terminal_order_status((terminal_fact,)) not in {"CANCELLED", "EXPIRED"}:
        return False
    try:
        cumulative = Decimal(
            str(terminal_fact.payload["cumulative_filled_quantity"])
        )
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return False
    if cumulative < 0:
        return False
    persisted = Decimal(0)
    for fact in materialized:
        if fact.kind is not VenueFactKind.FILL:
            continue
        try:
            quantity = Decimal(str(fact.payload["last_quantity"]))
        except (InvalidOperation, KeyError, TypeError, ValueError):
            return False
        if quantity <= 0:
            return False
        persisted += quantity
    return persisted == cumulative


def terminal_fills_accounted_for_user_takeover(
    action: ExecutionAction,
    facts: Iterable[VenueFact],
) -> bool:
    """Account for a venue-downsized terminal order after explicit takeover.

    Binance can reduce a reduce-only conditional order to the remaining
    position before filling it.  The immutable Halpha action keeps its original
    requested quantity, so the normal exact-quantity closure proof deliberately
    reports drift.  After user takeover, an account-level zero-position and
    no-open-order proof may pair with this narrower fact proof: the venue must
    report an explicit terminal cumulative quantity, every persisted fill must
    sum to that quantity, and the venue quantity may only have decreased.

    This helper does not itself prove account closure and must not be used for
    an active Halpha-owned responsibility.
    """

    materialized = collapse_synthetic_reconciliation_fills(facts)
    terminal_status = terminal_order_status(materialized)
    if terminal_status is None:
        return False
    try:
        requested = Decimal(str(action.action_terms["quantity"]))
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return False
    if not requested.is_finite() or requested <= 0:
        return False

    persisted = Decimal(0)
    for fact in materialized:
        if fact.kind is not VenueFactKind.FILL:
            continue
        try:
            quantity = Decimal(str(fact.payload["last_quantity"]))
        except (InvalidOperation, KeyError, TypeError, ValueError):
            return False
        if not quantity.is_finite() or quantity <= 0:
            return False
        persisted += quantity

    for terminal_fact in materialized:
        if terminal_order_status((terminal_fact,)) != terminal_status:
            continue
        cumulative_raw = terminal_fact.payload.get("cumulative_filled_quantity")
        if cumulative_raw is None:
            continue
        try:
            cumulative = Decimal(str(cumulative_raw))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if (
            not cumulative.is_finite()
            or cumulative < 0
            or cumulative > requested
            or persisted != cumulative
            or (terminal_status == "REJECTED" and cumulative != 0)
        ):
            continue
        return True
    return False


def action_quantity_conflict(
    action: ExecutionAction,
    facts: Iterable[VenueFact],
) -> bool:
    """Detect venue quantity drift without discarding authoritative facts."""

    try:
        requested = Decimal(str(action.action_terms["quantity"]))
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return True
    if requested <= 0:
        return True

    materialized = collapse_synthetic_reconciliation_fills(facts)
    for fact in materialized:
        venue_order_quantity = fact.payload.get("venue_order_quantity")
        if venue_order_quantity is None:
            continue
        try:
            if Decimal(str(venue_order_quantity)) != requested:
                return True
        except (InvalidOperation, TypeError, ValueError):
            return True

    if action.action_kind is not ExecutionActionKind.ENTRY:
        return False
    cumulative = Decimal(0)
    for fact in materialized:
        if fact.kind is not VenueFactKind.FILL:
            continue
        try:
            quantity = Decimal(str(fact.payload["last_quantity"]))
        except (InvalidOperation, KeyError, TypeError, ValueError):
            return True
        if quantity <= 0:
            return True
        cumulative += quantity
    return cumulative > requested


def _latest_terminal_fact(facts: tuple[VenueFact, ...]) -> VenueFact | None:
    terminal_facts = tuple(
        fact for fact in facts if terminal_order_status((fact,)) is not None
    )
    if not terminal_facts:
        return None
    return max(
        terminal_facts,
        key=lambda fact: (
            fact.source_time or fact.cutoff,
            fact.cutoff,
            fact.received_at,
            fact.venue_fact_id,
        ),
    )


def build_venue_fact(
    *,
    venue_fact_id: str,
    environment_id: str,
    venue_ref: str,
    account_ref: str | None,
    instrument_ref: str | None,
    kind: VenueFactKind,
    source_class: VenueFactSourceClass,
    source_object_id: str,
    source_sequence: str,
    source_time: datetime | None,
    received_at: datetime,
    cutoff: datetime,
    payload: dict[str, Any],
    action: ExecutionAction | None = None,
    impact_scope: dict[str, Any] | None = None,
    affected_reference_refs: tuple[str, ...] | None = None,
) -> VenueFact:
    """Normalize one authoritative observation without inventing venue identity."""

    if action is not None:
        payload_client_order_id = payload.get("client_order_id")
        requires_trade_identity = kind in {
            VenueFactKind.FILL,
            VenueFactKind.COMMISSION,
        }
        if (
            action.environment_id != environment_id
            or action.account_ref != account_ref
            or action.action_terms.get("instrument_ref") != instrument_ref
            or (
                requires_trade_identity
                and (
                    action.client_order_id is None
                    or payload_client_order_id != action.client_order_id
                )
            )
            or (
                not requires_trade_identity
                and payload_client_order_id is not None
                and payload_client_order_id != action.client_order_id
            )
        ):
            raise ValueError("VENUE_FACT_ATTRIBUTION_INVALID")
        activation_ref = action.activation_id
        action_ref = action.execution_action_id
        attribution_class = VenueFactAttributionClass.HALPHA_EXECUTION
        attribution_digest = content_digest(
            {
                "environment_id": environment_id,
                "activation_ref": activation_ref,
                "action_ref": action_ref,
                "client_order_id": action.client_order_id,
                "cancel_target": action.cancel_target,
                "action_terms_digest": action.action_terms_digest,
                "source_object_id": source_object_id,
                "source_sequence": source_sequence,
            }
        )
    else:
        activation_ref = None
        action_ref = None
        attribution_class = None
        attribution_digest = None
    fields: dict[str, Any] = {
        "venue_fact_id": venue_fact_id,
        "environment_id": environment_id,
        "venue_ref": venue_ref,
        "account_ref": account_ref,
        "instrument_ref": instrument_ref,
        "kind": kind,
        "source_class": source_class,
        "source_object_id": source_object_id,
        "source_sequence": source_sequence,
        "source_time": source_time,
        "received_at": received_at,
        "cutoff": cutoff,
        "schema_version": 1,
        "payload": payload,
        "activation_ref": activation_ref,
        "action_ref": action_ref,
        "attribution_digest": attribution_digest,
        "attribution_class": attribution_class,
        "handover_command_ref": None,
        "supersedes_ref": None,
        "correction_reason": None,
        "correction_evidence_refs": None,
        "correction_effective_time": None,
        "impact_scope": impact_scope,
        "affected_reference_refs": affected_reference_refs,
    }
    fields["content_digest"] = venue_fact_content_digest(fields)
    return VenueFact(**fields)


def build_activation_allocation_fact(
    *,
    venue_fact_id: str,
    environment_id: str,
    venue_ref: str,
    account_ref: str,
    instrument_ref: str,
    kind: VenueFactKind,
    source_object_id: str,
    source_sequence: str,
    source_time: datetime,
    received_at: datetime,
    cutoff: datetime,
    payload: dict[str, Any],
    activation_ref: str,
    aggregate_fact_ref: str,
) -> VenueFact:
    """Build one auditable allocation of an account-level venue fact."""

    if kind is not VenueFactKind.FUNDING:
        raise ValueError("ACTIVATION_ALLOCATION_KIND_UNSUPPORTED")
    attribution_digest = content_digest(
        {
            "environment_id": environment_id,
            "account_ref": account_ref,
            "instrument_ref": instrument_ref,
            "activation_ref": activation_ref,
            "aggregate_fact_ref": aggregate_fact_ref,
            "source_object_id": source_object_id,
            "source_sequence": source_sequence,
            "payload": payload,
        }
    )
    fields: dict[str, Any] = {
        "venue_fact_id": venue_fact_id,
        "environment_id": environment_id,
        "venue_ref": venue_ref,
        "account_ref": account_ref,
        "instrument_ref": instrument_ref,
        "kind": kind,
        "source_class": VenueFactSourceClass.FRAMEWORK_DERIVED,
        "source_object_id": source_object_id,
        "source_sequence": source_sequence,
        "source_time": source_time,
        "received_at": received_at,
        "cutoff": cutoff,
        "schema_version": 1,
        "payload": payload,
        "activation_ref": activation_ref,
        "action_ref": None,
        "attribution_digest": attribution_digest,
        "attribution_class": (
            VenueFactAttributionClass.HALPHA_ACTIVATION_ALLOCATION
        ),
        "handover_command_ref": None,
        "supersedes_ref": None,
        "correction_reason": None,
        "correction_evidence_refs": None,
        "correction_effective_time": None,
        "impact_scope": {
            "classification": "ACCOUNT_FUNDING_ALLOCATION",
            "aggregate_fact_ref": aggregate_fact_ref,
        },
        "affected_reference_refs": (aggregate_fact_ref,),
    }
    fields["content_digest"] = venue_fact_content_digest(fields)
    return VenueFact(**fields)
