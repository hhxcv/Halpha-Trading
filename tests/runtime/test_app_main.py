from types import SimpleNamespace
from typing import cast

from fastapi import FastAPI

from halpha.app.__main__ import _runtime_schema_failure_log_fields, _uvicorn_config
from halpha.configuration import AppSettingsView
from halpha.database.schema_version import SchemaVersionError


def test_uvicorn_config_bounds_graceful_shutdown() -> None:
    role_settings = cast(
        AppSettingsView,
        SimpleNamespace(
            app=SimpleNamespace(
                bind="127.0.0.1",
                port=8765,
                workers=1,
                reload=False,
            )
        ),
    )

    config = _uvicorn_config(FastAPI(), role_settings)

    assert config.timeout_graceful_shutdown == 10
    assert config.proxy_headers is False
    assert config.server_header is False


def test_app_schema_failure_log_fields_keep_only_stable_diagnostics() -> None:
    assert _runtime_schema_failure_log_fields(
        SchemaVersionError("DATABASE_SCHEMA_VERSION_MISMATCH expected=current actual=old")
    ) == {
        "exception_type": "SchemaVersionError",
        "reason_code": "DATABASE_SCHEMA_VERSION_MISMATCH expected=current actual=old",
    }
    assert _runtime_schema_failure_log_fields(RuntimeError("secret=value")) == {
        "exception_type": "RuntimeError",
        "reason_code": "APP_RUNTIME_SCHEMA_CHECK_FAILED type=RuntimeError",
    }
