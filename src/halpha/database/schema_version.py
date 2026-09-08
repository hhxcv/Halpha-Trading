"""Runtime guard for the single supported product database schema."""

from __future__ import annotations

from typing import Any


CURRENT_SCHEMA_REVISION = "20260816_0016"
_SCHEMA_VERSION_QUERY = "SELECT version_num FROM halpha_meta.alembic_version"


class SchemaVersionError(RuntimeError):
    """Sanitized startup failure for an unavailable or stale schema."""


def require_current_schema(connection: Any) -> None:
    """Require the one Alembic head understood by this product build."""

    try:
        # psycopg exposes ``execute`` while SQLAlchemy 2.x intentionally
        # requires driver SQL to be explicit for a plain string statement.
        # The one guard is shared by runtime roles and maintenance verification,
        # so keep both supported connection contracts at this boundary.
        execute_driver_sql = getattr(connection, "exec_driver_sql", None)
        cursor = (
            execute_driver_sql(_SCHEMA_VERSION_QUERY)
            if callable(execute_driver_sql)
            else connection.execute(_SCHEMA_VERSION_QUERY)
        )
        rows = cursor.fetchall()
    except Exception:
        raise SchemaVersionError("DATABASE_SCHEMA_VERSION_UNAVAILABLE") from None
    actual = tuple(str(row[0]) for row in rows)
    if actual != (CURRENT_SCHEMA_REVISION,):
        actual_label = actual[0] if len(actual) == 1 else "INVALID"
        raise SchemaVersionError(
            "DATABASE_SCHEMA_VERSION_MISMATCH "
            f"expected={CURRENT_SCHEMA_REVISION} actual={actual_label}"
        )
