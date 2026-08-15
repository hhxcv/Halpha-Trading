"""Central lifecycle CLI for Halpha services and project listeners."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from halpha.configuration import ConfigurationError, load_settings
from halpha.database.migrate import verify_current_schema_for_config
from halpha.runtime_control import RuntimeControlError, RuntimeController
from halpha.runtime_identity import (
    RuntimeIdentityError,
    repository_root,
    require_repository_runtime,
)
from halpha.windows_runtime import WindowsRuntimeError


DEFAULT_CONFIG = Path("config/halpha.toml")
TOKEN_LABELS = {
    "OK": "OK",
    "WINDOWS_TASK": "Windows Task",
    "DISCOVERED_ONLY": "Discovered Only",
}


def _human_token(value: object) -> str:
    text = str(value)
    return TOKEN_LABELS.get(text, text.replace("_", " ").title())


def _table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    widths = [
        max(len(value) for value in (header, *(row[index] for row in rows)))
        for index, header in enumerate(headers)
    ]

    def line(values: tuple[str, ...]) -> str:
        return "  ".join(value.ljust(widths[index]) for index, value in enumerate(values))

    separator = tuple("-" * width for width in widths)
    return "\n".join((line(headers), line(separator), *(line(row) for row in rows)))


def _enabled_label(value: object) -> str:
    if value is True:
        return "YES"
    if value is False:
        return "NO"
    return "-"


def render_status_report(report: dict[str, object]) -> str:
    rows: list[tuple[str, ...]] = []
    services = report.get("services", [])
    if isinstance(services, list):
        for item in services:
            if not isinstance(item, dict):
                continue
            listeners = item.get("listeners")
            endpoint_text = (
                ", ".join(str(value) for value in listeners)
                if isinstance(listeners, (list, tuple)) and listeners
                else "-"
            )
            rows.append(
                (
                    str(item.get("service", "-")),
                    _human_token(item.get("state", "UNKNOWN")),
                    _human_token(item.get("health", "UNKNOWN")),
                    _enabled_label(item.get("enabled")),
                    str(item.get("root_pid") or "-"),
                    endpoint_text,
                    _human_token(item.get("manager", "UNKNOWN")),
                )
            )
    lines = [
        f"Halpha service status: {_human_token(report.get('status', 'UNKNOWN'))}"
    ]
    schema = report.get("database_schema")
    if isinstance(schema, dict):
        schema_status = _human_token(schema.get("status", "UNKNOWN"))
        schema_reason = schema.get("reason")
        detail = f" ({schema_reason})" if schema_reason else ""
        lines.append(f"Database schema: {schema_status}{detail}")
    lines.extend(
        (
            "",
            _table(
                (
                    "SERVICE",
                    "STATE",
                    "HEALTH",
                    "ENABLED",
                    "PID",
                    "LISTENERS",
                    "MANAGED BY",
                ),
                rows,
            ),
        )
    )
    warnings = report.get("warnings")
    if isinstance(warnings, list) and warnings:
        lines.extend(("", "Warnings:", *(f"  - {warning}" for warning in warnings)))
    unmanaged = report.get("unmanaged_service_ids")
    if isinstance(unmanaged, list) and unmanaged:
        lines.extend(("", f"Unmanaged services available for stop: {', '.join(unmanaged)}"))
    return "\n".join(lines)


def _result_details(result: dict[str, object]) -> str:
    details = []
    if result.get("reason"):
        details.append(str(result["reason"]))
    if result.get("listener"):
        details.append(f"listener {result['listener']}")
    if result.get("root_pid"):
        details.append(f"PID {result['root_pid']}")
    if result.get("instances") is not None:
        details.append(f"instances {result['instances']}")
    if result.get("enabled") is False:
        details.append("automatic start disabled")
    return "; ".join(details) if details else "-"


def render_action_report(report: dict[str, object]) -> str:
    target = str(report.get("target") or report.get("service") or "-")
    rows: list[tuple[str, ...]] = []
    results = report.get("results")
    if isinstance(results, dict):
        for service, value in results.items():
            result = value if isinstance(value, dict) else {"status": value}
            rows.append(
                (
                    str(service),
                    _human_token(result.get("status", "UNKNOWN")),
                    _result_details(result),
                )
            )
    else:
        rows.append(
            (
                str(report.get("service") or target),
                _human_token(report.get("status", "UNKNOWN")),
                _result_details(report),
            )
        )
    return "\n".join(
        (
            f"Halpha operation result: {_human_token(report.get('status', 'UNKNOWN'))}",
            "",
            _table(("SERVICE", "RESULT", "DETAILS"), rows),
        )
    )


def render_error(reason: object) -> str:
    return f"Halpha operation failed: {reason}"


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        help="override the default config/halpha.toml",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="halpha-control",
        description="Inspect, start, and stop Halpha long-running processes.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="show all service states")
    _add_common_arguments(status)

    start = subparsers.add_parser("start", help="start a service")
    start.add_argument(
        "service",
        choices=("app", "executor", "backup", "product"),
    )
    _add_common_arguments(start)
    start.add_argument(
        "--observation-session",
        action="store_true",
        help="explicitly start a LIVE_READ_ONLY Executor observation session",
    )
    start.add_argument("--timeout-seconds", type=float, default=60.0)

    stop = subparsers.add_parser("stop", help="stop a service")
    stop.add_argument("service", nargs="?", default="product")
    _add_common_arguments(stop)
    stop.add_argument("--force", action="store_true")
    stop.add_argument("--timeout-seconds", type=float, default=30.0)

    autostart = subparsers.add_parser(
        "autostart",
        help="enable or disable task-triggered product startup without starting it now",
    )
    autostart.add_argument("service", choices=("app", "executor", "product"))
    autostart.add_argument("setting", choices=("on", "off"))
    _add_common_arguments(autostart)
    args = parser.parse_args(argv)
    try:
        root = repository_root()
        require_repository_runtime(root)
        config_path = args.config or root / DEFAULT_CONFIG
        settings = load_settings(config_path)
        controller = RuntimeController(
            root,
            settings,
            config_path,
            schema_readiness=lambda: verify_current_schema_for_config(config_path),
        )
        if args.command == "status":
            report = controller.inventory().to_dict()
            database_schema = controller.database_schema_status()
            report["database_schema"] = database_schema
            if database_schema["status"] != "CURRENT":
                report["status"] = "ATTENTION_REQUIRED"
                warning = str(
                    database_schema.get(
                        "reason",
                        "DATABASE_SCHEMA_READINESS_NOT_CHECKED",
                    )
                )
                warnings = list(report["warnings"])
                if warning not in warnings:
                    warnings.append(warning)
                report["warnings"] = warnings
            exit_code = 0 if report["status"] == "CONTROLLED" else 3
        elif args.command == "start":
            report = controller.start(
                args.service,
                observation_session=args.observation_session,
                timeout_seconds=args.timeout_seconds,
            )
            exit_code = 0
        elif args.command == "autostart":
            report = controller.set_autostart(
                args.service,
                enabled=args.setting == "on",
            )
            exit_code = 0
        else:
            report = controller.stop(
                args.service,
                force=args.force,
                timeout_seconds=args.timeout_seconds,
            )
            exit_code = 0 if report["status"] == "STOPPED" else 2
    except (
        ConfigurationError,
        RuntimeControlError,
        RuntimeIdentityError,
        WindowsRuntimeError,
    ) as exc:
        if args.json:
            print(json.dumps({"status": "REJECTED", "reason": str(exc)}, sort_keys=True))
        else:
            print(render_error(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    elif args.command == "status":
        print(render_status_report(report))
    else:
        print(render_action_report(report))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
