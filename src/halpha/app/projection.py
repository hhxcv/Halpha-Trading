"""Read-only PostgreSQL projections used by the local owner workbench."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import psycopg
from pydantic import SecretStr

from halpha.binance_contracts import (
    BINANCE_USDM_ACCOUNT_SNAPSHOT_SCHEMA,
    parse_complete_binance_usdm_account_snapshot,
)
from halpha.capital.discipline import (
    evaluate_new_risk_discipline,
    read_new_risk_discipline,
)
from halpha.capital.models import NewRiskDisciplinePolicy
from halpha.product_build import (
    EXECUTOR_READY_APPLICATION_NAME_PREFIX,
    EXECUTOR_STARTING_APPLICATION_NAME,
    executor_ready_application_name,
)
from halpha.domain_values import canonical_decimal


ACCOUNT_SNAPSHOT_CURRENT_SECONDS = 90
ACCOUNT_SNAPSHOT_MAX_FUTURE_SKEW_SECONDS = 5


class ProjectionUnavailable(RuntimeError):
    """Sanitized database-unavailable result for the interaction layer."""


class WorkbenchProjection(Protocol):
    def overview(self) -> dict[str, Any]: ...

    def availability(self) -> dict[str, Any]: ...

    def operations(self) -> dict[str, Any]: ...

    def executor_status(self, product_build_id: str) -> dict[str, Any]: ...


def _executor_status_from_application_names(
    application_names: tuple[str, ...],
    *,
    product_build_id: str,
) -> tuple[str, bool | None]:
    expected = executor_ready_application_name(product_build_id)
    names = tuple(name for name in application_names if name)
    if names == (expected,):
        return "READY", True
    if len(names) > 1:
        return "AMBIGUOUS", None
    if names == (EXECUTOR_STARTING_APPLICATION_NAME,):
        return "STARTING", None
    if len(names) == 1 and names[0].startswith(
        EXECUTOR_READY_APPLICATION_NAME_PREFIX
    ):
        return "BUILD_MISMATCH", False
    if not names:
        return "UNAVAILABLE", None
    return "UNKNOWN", None


def _aware_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _snapshot_decimal(value: object) -> str:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("ACCOUNT_SNAPSHOT_POSITION_INVALID") from None
    if not parsed.is_finite():
        raise ValueError("ACCOUNT_SNAPSHOT_POSITION_INVALID")
    return canonical_decimal(parsed)


def _empty_account_snapshot(status: str) -> dict[str, object]:
    return {
        "account_snapshot_status": status,
        "account_snapshot_ref": None,
        "account_snapshot_cutoff": None,
        "account_snapshot_age_seconds": None,
        "account_ordinary_open_order_count": None,
        "account_algo_open_order_count": None,
        "account_summary": None,
        "account_positions": [],
        "account_orders": [],
    }


def _snapshot_optional_order_decimal(value: object) -> str | None:
    if value is None:
        return None
    normalized = _snapshot_decimal(value)
    if Decimal(normalized) < 0:
        raise ValueError("ACCOUNT_SNAPSHOT_ORDER_INVALID")
    return normalized


def _project_account_summary(
    raw: object,
    *,
    observed_at: datetime,
    snapshot_ref: str,
) -> dict[str, object]:
    if not isinstance(raw, dict) or type(raw.get("can_trade")) is not bool:
        raise ValueError("ACCOUNT_SNAPSHOT_SUMMARY_INVALID")
    non_negative_fields = (
        "wallet_balance",
        "margin_balance",
        "initial_margin",
        "maintenance_margin",
        "position_initial_margin",
        "open_order_initial_margin",
    )
    decimal_fields = (
        *non_negative_fields,
        "unrealized_pnl",
        "available_balance",
        "cross_wallet_balance",
        "cross_unrealized_pnl",
    )
    values = {name: _snapshot_decimal(raw[name]) for name in decimal_fields}
    if any(Decimal(values[name]) < 0 for name in non_negative_fields):
        raise ValueError("ACCOUNT_SNAPSHOT_SUMMARY_INVALID")
    source_update_time_ms = raw.get("source_update_time_ms")
    if source_update_time_ms is not None and (
        type(source_update_time_ms) is not int or source_update_time_ms < 0
    ):
        raise ValueError("ACCOUNT_SNAPSHOT_SUMMARY_INVALID")
    return {
        "can_trade": raw["can_trade"],
        **values,
        "source_update_time_ms": source_update_time_ms,
        "fact_cutoff": observed_at.isoformat().replace("+00:00", "Z"),
        "snapshot_ref": snapshot_ref,
    }


def _snapshot_order_text(
    value: object,
    *,
    required: bool = True,
    maximum_length: int = 128,
) -> str | None:
    if value is None:
        if required:
            raise ValueError("ACCOUNT_SNAPSHOT_ORDER_INVALID")
        return None
    text = str(value).strip()
    if not text:
        if required:
            raise ValueError("ACCOUNT_SNAPSHOT_ORDER_INVALID")
        return None
    if len(text) > maximum_length or any(
        ord(character) < 32 or ord(character) == 127 for character in text
    ):
        raise ValueError("ACCOUNT_SNAPSHOT_ORDER_INVALID")
    return text


def _project_account_order(
    raw: object,
    *,
    expected_kind: str,
    observed_at: datetime,
    snapshot_ref: str,
) -> dict[str, object]:
    if not isinstance(raw, dict) or raw.get("kind") != expected_kind:
        raise ValueError("ACCOUNT_SNAPSHOT_ORDER_INVALID")
    instrument_ref = str(raw["instrument_ref"])
    symbol = str(raw["symbol"])
    side = str(raw["side"])
    position_side = str(raw["position_side"])
    order_id = _snapshot_order_text(raw.get("order_id"))
    client_order_id = _snapshot_order_text(
        raw.get("client_order_id"),
        required=False,
    )
    order_type = _snapshot_order_text(raw.get("order_type"))
    status = _snapshot_order_text(raw.get("status"))
    time_in_force = _snapshot_order_text(
        raw.get("time_in_force"),
        required=False,
    )
    if (
        not instrument_ref.endswith("-PERP")
        or symbol != instrument_ref.removesuffix("-PERP")
        or side not in {"BUY", "SELL"}
        or position_side not in {"BOTH", "LONG", "SHORT"}
        or not str(order_type).isupper()
        or not str(status).isupper()
    ):
        raise ValueError("ACCOUNT_SNAPSHOT_ORDER_INVALID")
    if time_in_force is not None and not time_in_force.isupper():
        raise ValueError("ACCOUNT_SNAPSHOT_ORDER_INVALID")
    reduce_only = raw.get("reduce_only")
    close_position = raw.get("close_position")
    if reduce_only is not None and type(reduce_only) is not bool:
        raise ValueError("ACCOUNT_SNAPSHOT_ORDER_INVALID")
    if close_position is not None and type(close_position) is not bool:
        raise ValueError("ACCOUNT_SNAPSHOT_ORDER_INVALID")
    source_create_time_ms = raw.get("source_create_time_ms")
    source_update_time_ms = raw.get("source_update_time_ms")
    for timestamp in (source_create_time_ms, source_update_time_ms):
        if timestamp is not None and (type(timestamp) is not int or timestamp < 0):
            raise ValueError("ACCOUNT_SNAPSHOT_ORDER_INVALID")
    quantity = _snapshot_optional_order_decimal(raw.get("quantity"))
    executed_quantity = _snapshot_optional_order_decimal(
        raw.get("executed_quantity")
    )
    if expected_kind == "ORDINARY" and (
        quantity is None or Decimal(quantity) <= 0 or executed_quantity is None
    ):
        raise ValueError("ACCOUNT_SNAPSHOT_ORDER_INVALID")
    return {
        "kind": expected_kind,
        "instrument_ref": instrument_ref,
        "symbol": symbol,
        "order_id": order_id,
        "client_order_id": client_order_id,
        "side": side,
        "position_side": position_side,
        "order_type": order_type,
        "status": status,
        "time_in_force": time_in_force,
        "price": _snapshot_optional_order_decimal(raw.get("price")),
        "trigger_price": _snapshot_optional_order_decimal(
            raw.get("trigger_price")
        ),
        "quantity": quantity,
        "executed_quantity": executed_quantity,
        "reduce_only": reduce_only,
        "close_position": close_position,
        "source_create_time_ms": source_create_time_ms,
        "source_update_time_ms": source_update_time_ms,
        "fact_cutoff": observed_at.isoformat().replace("+00:00", "Z"),
        "snapshot_ref": snapshot_ref,
    }


def _project_account_snapshot(
    *,
    server_cutoff: object,
    fact_ref: object = None,
    fact_cutoff: object,
    payload: object,
    attributed_instruments: tuple[str, ...] = (),
) -> dict[str, object]:
    """Defensively expose one complete account fact and its freshness."""

    if fact_cutoff is None and payload is None:
        return _empty_account_snapshot("UNAVAILABLE")
    server_time = _aware_utc(server_cutoff)
    observed_at = _aware_utc(fact_cutoff)
    if server_time is None or observed_at is None or not isinstance(payload, dict):
        return _empty_account_snapshot("UNKNOWN")
    age_seconds = int((server_time - observed_at).total_seconds())
    if age_seconds < -ACCOUNT_SNAPSHOT_MAX_FUTURE_SKEW_SECONDS:
        return _empty_account_snapshot("UNKNOWN")
    age_seconds = max(0, age_seconds)
    status = (
        "CURRENT"
        if age_seconds <= ACCOUNT_SNAPSHOT_CURRENT_SECONDS
        else "STALE"
    )
    snapshot = parse_complete_binance_usdm_account_snapshot(payload)
    if snapshot is None:
        return _empty_account_snapshot("UNKNOWN")
    schema = snapshot["schema"]
    attributed = frozenset(attributed_instruments)
    snapshot_ref = str(fact_ref or "").strip()
    if not snapshot_ref or len(snapshot_ref) > 160:
        return _empty_account_snapshot("UNKNOWN")
    positions: list[dict[str, object]] = []
    orders: list[dict[str, object]] = []
    account_summary: dict[str, object] | None = None
    try:
        if schema == BINANCE_USDM_ACCOUNT_SNAPSHOT_SCHEMA:
            account_summary = _project_account_summary(
                snapshot["account_summary"],
                observed_at=observed_at,
                snapshot_ref=snapshot_ref,
            )
        for raw in snapshot["positions"]:
            if not isinstance(raw, dict):
                raise ValueError("ACCOUNT_SNAPSHOT_POSITION_INVALID")
            instrument_ref = str(raw["instrument_ref"])
            symbol = str(raw["symbol"])
            direction = str(raw["direction"])
            position_side = str(raw["position_side"])
            margin_mode = str(raw["margin_mode"])
            leverage = int(raw["leverage"])
            if (
                not instrument_ref.endswith("-PERP")
                or symbol != instrument_ref.removesuffix("-PERP")
                or direction not in {"LONG", "SHORT"}
                or position_side not in {"BOTH", "LONG", "SHORT"}
                or margin_mode not in {"CROSS", "ISOLATED"}
                or leverage <= 0
            ):
                raise ValueError("ACCOUNT_SNAPSHOT_POSITION_INVALID")
            optional_fields = {
                name: (
                    None
                    if raw.get(name) is None
                    else _snapshot_decimal(raw[name])
                )
                for name in (
                    "break_even_price",
                    "liquidation_price",
                    "isolated_margin",
                )
            }
            origin = (
                "ACCOUNT_TOTAL_WITH_HALPHA_ATTRIBUTION"
                if instrument_ref in attributed
                else "EXTERNAL_UNMANAGED"
            )
            positions.append(
                {
                    "instrument_ref": instrument_ref,
                    "symbol": symbol,
                    "direction": direction,
                    "position_side": position_side,
                    "quantity": _snapshot_decimal(raw["quantity"]),
                    "absolute_quantity": _snapshot_decimal(
                        raw["absolute_quantity"]
                    ),
                    "entry_price": _snapshot_decimal(raw["entry_price"]),
                    **optional_fields,
                    "mark_price": _snapshot_decimal(raw["mark_price"]),
                    "unrealized_pnl": _snapshot_decimal(
                        raw["unrealized_pnl"]
                    ),
                    "leverage": leverage,
                    "margin_mode": margin_mode,
                    "notional": _snapshot_decimal(raw["notional"]),
                    "fact_cutoff": observed_at.isoformat().replace(
                        "+00:00",
                        "Z",
                    ),
                    "snapshot_ref": snapshot_ref,
                    "origin": origin,
                }
            )
        orders.extend(
            _project_account_order(
                raw,
                expected_kind="ORDINARY",
                observed_at=observed_at,
                snapshot_ref=snapshot_ref,
            )
                for raw in snapshot["ordinary_open_orders"]
        )
        orders.extend(
            _project_account_order(
                raw,
                expected_kind="ALGO",
                observed_at=observed_at,
                snapshot_ref=snapshot_ref,
            )
                for raw in snapshot["algo_open_orders"]
        )
    except (KeyError, TypeError, ValueError):
        return _empty_account_snapshot("UNKNOWN")
    positions.sort(key=lambda item: (str(item["symbol"]), str(item["position_side"])))
    orders.sort(
        key=lambda item: (
            -int(
                item.get("source_update_time_ms")
                or item.get("source_create_time_ms")
                or 0
            ),
            str(item["symbol"]),
            str(item["kind"]),
            str(item["order_id"]),
        )
    )
    return {
        "account_snapshot_status": status,
        "account_snapshot_ref": snapshot_ref,
        "account_snapshot_cutoff": observed_at.isoformat().replace("+00:00", "Z"),
        "account_snapshot_age_seconds": age_seconds,
        "account_ordinary_open_order_count": int(
            snapshot["ordinary_open_order_count"]
        ),
        "account_algo_open_order_count": int(snapshot["algo_open_order_count"]),
        "account_summary": account_summary,
        "account_positions": positions,
        "account_orders": orders,
    }


@dataclass(frozen=True, repr=False)
class PostgreSQLWorkbenchProjection:
    database_name: str
    database_role_name: str
    password: SecretStr
    environment_id: str
    account_id: str
    new_risk_discipline_policy: NewRiskDisciplinePolicy = field(
        default_factory=NewRiskDisciplinePolicy
    )
    host: str = "127.0.0.1"
    port: int = 5432
    read_only: bool = False

    def _connect(self) -> psycopg.Connection[Any]:
        try:
            return psycopg.connect(
                host=self.host,
                port=self.port,
                dbname=self.database_name,
                user=self.database_role_name,
                password=self.password.get_secret_value(),
                connect_timeout=2,
                autocommit=True,
                options=(
                    "-c default_transaction_read_only=on"
                    if self.read_only
                    else None
                ),
            )
        except Exception as exc:
            raise ProjectionUnavailable(
                f"DATABASE_UNAVAILABLE type={type(exc).__name__}"
            ) from None

    def overview(self) -> dict[str, Any]:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        clock_timestamp() AT TIME ZONE 'UTC',
                        (SELECT count(*) FROM halpha.plan_activation
                         WHERE environment_id = %s AND lifecycle <> 'COMPLETED'),
                        current_database(),
                        current_user,
                        snapshot.venue_fact_id,
                        snapshot.cutoff,
                        snapshot.payload,
                        (SELECT array_agg(DISTINCT instrument_ref ORDER BY instrument_ref)
                         FROM halpha.plan_activation
                         WHERE environment_id = %s
                           AND lifecycle <> 'COMPLETED'
                           AND has_entry_fill)
                    FROM (SELECT 1) AS singleton
                    LEFT JOIN LATERAL (
                        SELECT venue_fact_id, cutoff, payload
                        FROM halpha.venue_fact
                        WHERE environment_id = %s
                          AND account_ref = %s
                          AND kind = 'ACCOUNT_STATE'
                          AND source_class = 'VENUE_QUERY'
                        ORDER BY cutoff DESC, received_at DESC, venue_fact_id DESC
                        LIMIT 1
                    ) AS snapshot ON TRUE
                    """,
                    (
                        self.environment_id,
                        self.environment_id,
                        self.environment_id,
                        self.account_id,
                    ),
                )
                row = cursor.fetchone()
                observed_at = _aware_utc(row[0]) if row is not None else None
                if observed_at is None:
                    discipline = evaluate_new_risk_discipline(
                        policy=self.new_risk_discipline_policy,
                        observed_at=datetime.now(UTC),
                        account_equity=None,
                        attempts=(),
                    )
                else:
                    try:
                        discipline = read_new_risk_discipline(
                            connection,
                            environment_id=self.environment_id,
                            account_ref=self.account_id,
                            policy=self.new_risk_discipline_policy,
                            observed_at=observed_at,
                        )
                    except Exception:
                        discipline = evaluate_new_risk_discipline(
                            policy=self.new_risk_discipline_policy,
                            observed_at=observed_at,
                            account_equity=None,
                            attempts=(),
                        )
        except ProjectionUnavailable:
            raise
        except Exception as exc:
            raise ProjectionUnavailable(
                f"DATABASE_PROJECTION_FAILED type={type(exc).__name__}"
            ) from None
        if row is None:
            raise ProjectionUnavailable("DATABASE_PROJECTION_EMPTY")
        cutoff = row[0]
        if isinstance(cutoff, datetime):
            cutoff = cutoff.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
        account_snapshot = _project_account_snapshot(
            server_cutoff=row[0],
            fact_ref=row[4],
            fact_cutoff=row[5],
            payload=row[6],
            attributed_instruments=tuple(str(item) for item in (row[7] or ())),
        )
        return {
            "database_available": True,
            "server_fact_cutoff": str(cutoff),
            "open_activation_count": int(row[1]),
            "database_name": str(row[2]),
            "database_role": str(row[3]),
            "new_risk_discipline": discipline.model_dump(mode="json"),
            **account_snapshot,
        }

    def availability(self) -> dict[str, Any]:
        try:
            summary = self.overview()
        except ProjectionUnavailable:
            return {
                "database_available": False,
                "reason_code": "DATABASE_UNAVAILABLE",
                "server_fact_cutoff": None,
            }
        return {
            "database_available": True,
            "reason_code": None,
            "server_fact_cutoff": summary["server_fact_cutoff"],
        }

    def executor_status(self, product_build_id: str) -> dict[str, Any]:
        """Project the current Executor session without adding a heartbeat store."""

        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT clock_timestamp() AT TIME ZONE 'UTC', application_name
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND usename = %s
                      AND (
                        application_name = %s
                        OR application_name LIKE %s
                      )
                    ORDER BY pid
                    """,
                    (
                        f"{self.database_name}_executor",
                        EXECUTOR_STARTING_APPLICATION_NAME,
                        f"{EXECUTOR_READY_APPLICATION_NAME_PREFIX}%",
                    ),
                )
                rows = cursor.fetchall()
                checked_at = cursor.execute(
                    "SELECT clock_timestamp() AT TIME ZONE 'UTC'"
                ).fetchone()[0]
        except ProjectionUnavailable:
            raise
        except Exception as exc:
            raise ProjectionUnavailable(
                f"EXECUTOR_STATUS_FAILED type={type(exc).__name__}"
            ) from None
        status, consistent = _executor_status_from_application_names(
            tuple(str(row[1]) for row in rows),
            product_build_id=product_build_id,
        )
        if rows:
            checked_at = rows[-1][0]
        if isinstance(checked_at, datetime):
            checked_at = checked_at.replace(tzinfo=UTC).isoformat().replace(
                "+00:00", "Z"
            )
        return {
            "status": status,
            "checked_at": str(checked_at),
            "product_build_consistent": consistent,
        }

    def operations(self) -> dict[str, Any]:
        """Project the small authoritative fact set required by `/operations`."""

        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cutoff = cursor.execute(
                    "SELECT clock_timestamp() AT TIME ZONE 'UTC'"
                ).fetchone()[0]
                activation_rows = cursor.execute(
                    """
                    SELECT a.activation_id, a.account_ref, a.instrument_ref, a.direction,
                           a.lifecycle, a.run_state, a.pause_reason, a.state_version,
                           a.protection_state, a.latest_venue_cutoff, a.updated_at,
                           v.terms ->> 'plan_name', a.has_entry_fill,
                           a.entry_opportunity_consumed,
                           a.rule_state -> 'deadlines' ->> 'entry_valid_until'
                    FROM halpha.plan_activation AS a
                    LEFT JOIN halpha.trade_plan_version AS v
                      ON v.environment_id = a.environment_id
                     AND v.plan_version_id = a.plan_version_ref
                    WHERE a.environment_id = %s AND a.lifecycle <> 'COMPLETED'
                    ORDER BY a.updated_at DESC, a.activation_id
                    """,
                    (self.environment_id,),
                ).fetchall()
                activations: list[dict[str, Any]] = []
                for row in activation_rows:
                    activation_id = str(row[0])
                    account_ref = str(row[1])
                    stop_rows = cursor.execute(
                        """
                        SELECT stopped_categories
                        FROM (
                            SELECT DISTINCT ON (
                                CASE WHEN activation_id IS NULL
                                     THEN 'ACCOUNT'
                                     ELSE activation_id::text END
                            ) activation_id, stopped_categories, version
                            FROM halpha.stop_state_version
                            WHERE environment_id = %s
                              AND account_ref = %s
                              AND (activation_id IS NULL OR activation_id = %s)
                            ORDER BY CASE WHEN activation_id IS NULL
                                          THEN 'ACCOUNT'
                                          ELSE activation_id::text END,
                                     version DESC
                        ) AS current_stops
                        """,
                        (self.environment_id, account_ref, row[0]),
                    ).fetchall()
                    stopped_categories = {
                        str(category)
                        for stop_row in stop_rows
                        for category in stop_row[0]
                    }
                    activations.append(
                        {
                            "activation_id": activation_id,
                            "account_ref": account_ref,
                            "instrument_ref": str(row[2]),
                            "direction": str(row[3]),
                            "lifecycle": str(row[4]),
                            "run_state": str(row[5]),
                            "pause_reason": str(row[6]) if row[6] is not None else None,
                            "state_version": int(row[7]),
                            "protection_state": str(row[8]),
                            "latest_venue_cutoff": (
                                row[9].isoformat() if row[9] is not None else None
                            ),
                            "updated_at": row[10].isoformat(),
                            "plan_name": str(row[11]) if row[11] is not None else None,
                            "has_entry_fill": bool(row[12]),
                            "entry_opportunity_consumed": bool(row[13]),
                            "entry_valid_until": (
                                str(row[14]) if row[14] is not None else None
                            ),
                            "stopped_categories": sorted(stopped_categories),
                        }
                    )
        except ProjectionUnavailable:
            raise
        except Exception as exc:
            raise ProjectionUnavailable(
                f"OPERATIONS_PROJECTION_FAILED type={type(exc).__name__}"
            ) from None
        if isinstance(cutoff, datetime):
            cutoff = cutoff.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
        return {
            "database_available": True,
            "server_fact_cutoff": str(cutoff),
            "activations": activations,
        }
