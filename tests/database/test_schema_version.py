from __future__ import annotations

import pytest

from halpha.database.schema_version import (
    CURRENT_SCHEMA_REVISION,
    SchemaVersionError,
    require_current_schema,
)


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, rows=None, error: Exception | None = None):
        self.rows = rows
        self.error = error
        self.queries: list[str] = []

    def execute(self, query: str):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return _Cursor(self.rows)


def test_current_schema_is_accepted() -> None:
    connection = _Connection([(CURRENT_SCHEMA_REVISION,)])

    require_current_schema(connection)

    assert connection.queries == [
        "SELECT version_num FROM halpha_meta.alembic_version"
    ]


def test_current_schema_is_accepted_through_sqlalchemy_driver_sql() -> None:
    calls: list[str] = []

    class _SqlAlchemyConnection:
        @staticmethod
        def exec_driver_sql(query: str) -> _Cursor:
            calls.append(query)
            return _Cursor([(CURRENT_SCHEMA_REVISION,)])

    require_current_schema(_SqlAlchemyConnection())

    assert calls == ["SELECT version_num FROM halpha_meta.alembic_version"]


@pytest.mark.parametrize("rows", [[], [("old",)], [(CURRENT_SCHEMA_REVISION,), ("old",)]])
def test_missing_stale_or_ambiguous_schema_is_rejected(rows) -> None:
    with pytest.raises(SchemaVersionError, match="DATABASE_SCHEMA_VERSION_MISMATCH"):
        require_current_schema(_Connection(rows))


def test_unavailable_schema_version_is_sanitized() -> None:
    with pytest.raises(
        SchemaVersionError,
        match="DATABASE_SCHEMA_VERSION_UNAVAILABLE",
    ):
        require_current_schema(_Connection(error=RuntimeError("database details")))
