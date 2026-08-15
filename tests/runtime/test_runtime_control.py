from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
import pywintypes

from halpha.configuration import load_settings
from halpha.database.schema_version import SchemaVersionError
from halpha.control import main as control_main
from halpha.control import render_action_report, render_error, render_status_report
from halpha.runtime_control import (
    ListenerSnapshot,
    ProcessSnapshot,
    RuntimeControlError,
    RuntimeController,
    TaskSnapshot,
    build_inventory,
)
from halpha.windows_deployment import (
    DEMO_DEPLOYMENT,
    LIVE_COPY_DEPLOYMENT as LIVE_DEPLOYMENT,
    SHARED_BACKUP_TASK,
)


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = ROOT / "config/halpha.example.toml"
LIVE_EXAMPLE_CONFIG = ROOT / "config/halpha.live-copy-read-only.example.toml"


def test_status_report_is_a_human_readable_table() -> None:
    rendered = render_status_report(
        {
            "status": "ATTENTION_REQUIRED",
            "services": [
                {
                    "service": "app",
                    "state": "RUNNING",
                    "health": "OK",
                    "enabled": True,
                    "root_pid": 123,
                    "listeners": ("127.0.0.1:8765",),
                    "manager": "WINDOWS_TASK",
                },
                {
                    "service": "unmanaged:456",
                    "state": "RUNNING",
                    "health": "UNREGISTERED",
                    "enabled": None,
                    "root_pid": 456,
                    "listeners": ["127.0.0.1:8766"],
                    "manager": "DISCOVERED_ONLY",
                },
            ],
            "warnings": ["UNMANAGED_PROJECT_PROCESS_FOUND"],
            "unmanaged_service_ids": ["unmanaged:789"],
        }
    )

    assert rendered.startswith("Halpha service status: Attention Required\n\n")
    assert "SERVICE" in rendered
    assert "STATE" in rendered
    assert "app" in rendered
    assert "Running" in rendered
    assert "Discovered Only" in rendered
    assert "127.0.0.1:8765" in rendered
    assert rendered.isascii()
    assert not rendered.lstrip().startswith("{")


def test_all_cli_generated_help_and_action_text_is_ascii(capsys) -> None:
    action = render_action_report(
        {
            "status": "STOPPED",
            "target": "product",
            "results": {
                "app": {
                    "status": "STOPPED",
                    "enabled": False,
                }
            },
        }
    )

    assert action.isascii()
    with pytest.raises(SystemExit) as exit_info:
        control_main(["--help"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.isascii()


def test_cli_error_prefix_is_ascii_and_passive_text_is_preserved() -> None:
    passive_error = "外部错误"

    rendered = render_error(passive_error)

    assert rendered.startswith("Halpha operation failed: ")
    assert rendered.removeprefix("Halpha operation failed: ") == passive_error


def test_product_source_does_not_reference_the_independent_boundary() -> None:
    product_source = ROOT / "src" / "halpha"

    references = [
        path.relative_to(ROOT)
        for path in product_source.rglob("*.py")
        if "research" in path.read_text(encoding="utf-8").casefold()
    ]

    assert references == []


def _process(pid: int, parent_pid: int, command: str, worktree: Path = ROOT):
    return ProcessSnapshot(
        pid=pid,
        parent_pid=parent_pid,
        name="python.exe",
        executable=str(worktree / ".venv/Scripts/python.exe"),
        command_line=command,
        worktree=str(worktree),
    )


def test_inventory_correlates_tasks_worktrees_ports_and_unmanaged_listeners() -> None:
    peer_tree = ROOT.parent / "peer-worktree" / "Halpha"
    external_command = "-m independent_service.monitor serve"
    processes = {
        100: _process(100, 1, "-m halpha.app"),
        101: _process(101, 100, "-m halpha.app"),
        200: _process(
            200,
            1,
            external_command,
            peer_tree,
        ),
        201: _process(
            201,
            200,
            external_command,
            peer_tree,
        ),
        300: _process(300, 1, "-m http.server 9876"),
        301: _process(301, 300, "-m http.server 9876"),
        400: _process(400, 1, "-m halpha.executor --config config/halpha.toml"),
    }
    inventory = build_inventory(
        repository_root=ROOT,
        worktrees=(ROOT, peer_tree),
        processes=processes,
        listeners=(
            ListenerSnapshot("127.0.0.1", 8765, 101),
            ListenerSnapshot("127.0.0.1", 8766, 200),
            ListenerSnapshot("127.0.0.1", 9876, 301),
        ),
        tasks=(
            TaskSnapshot("app", "App", "RUNNING", True, (100,)),
            TaskSnapshot("executor", "Executor", "READY", True, ()),
            TaskSnapshot("backup", "Backup", "READY", True, ()),
        ),
    )

    services = {(service.service, service.root_pid): service for service in inventory.services}
    assert services[("app", 100)].process_ids == (100, 101)
    assert services[("app", 100)].listeners == ("127.0.0.1:8765",)
    assert services[("executor", None)].health == "EXPECTED_PROCESS_MISSING"
    peer = services[("unmanaged:200", 200)]
    assert peer.manager == "DISCOVERED_ONLY"
    assert peer.health == "UNREGISTERED"
    assert peer.recognized_as is None
    assert peer.process_ids == (200, 201)
    assert peer.worktree == str(peer_tree)
    assert services[("unmanaged:300", 300)].health == "UNREGISTERED"
    assert services[("unmanaged:400", 400)].recognized_as == "executor"
    assert inventory.unmanaged_service_ids == (
        "unmanaged:200",
        "unmanaged:300",
        "unmanaged:400",
    )
    assert inventory.status == "ATTENTION_REQUIRED"
    assert inventory.warnings == (
        "EXPECTED_PROCESS_MISSING:executor",
        "UNMANAGED_PROJECT_PROCESS_FOUND",
    )
def test_inventory_reports_task_contract_and_running_process_identity_mismatch() -> None:
    task_arguments = (
        f'-m halpha.executor --config "{EXAMPLE_CONFIG.resolve()}"'
    )
    inventory = build_inventory(
        repository_root=ROOT,
        worktrees=(ROOT,),
        processes={
            100: _process(
                100,
                1,
                '-m halpha.executor --config "D:\\wrong\\halpha.toml"',
            ),
        },
        listeners=(),
        tasks=(
            TaskSnapshot(
                "app",
                "App",
                "READY",
                True,
                (),
                contract_violations=("ACTION_ARGUMENTS_MISMATCH",),
            ),
            TaskSnapshot(
                "executor",
                "Executor",
                "RUNNING",
                True,
                (100,),
                action_path=str(ROOT / ".venv/Scripts/python.exe"),
                action_arguments=task_arguments,
            ),
            TaskSnapshot("backup", "Backup", "READY", True, ()),
        ),
    )

    services = {service.service: service for service in inventory.services}
    assert services["app"].health == "TASK_CONTRACT_MISMATCH"
    assert services["executor"].health == "TASK_PROCESS_IDENTITY_MISMATCH"
    assert inventory.warnings == (
        "TASK_CONTRACT_MISMATCH:app:ACTION_ARGUMENTS_MISMATCH",
        "TASK_PROCESS_IDENTITY_MISMATCH:executor",
    )


def test_disabled_ready_product_task_is_explicitly_stopped_without_false_alarm() -> None:
    inventory = build_inventory(
        repository_root=ROOT,
        worktrees=(ROOT,),
        processes={},
        listeners=(),
        tasks=(
            TaskSnapshot("app", "App", "READY", False, ()),
            TaskSnapshot("executor", "Executor", "READY", False, ()),
            TaskSnapshot("backup", "Backup", "READY", True, ()),
        ),
    )

    services = {service.service: service for service in inventory.services}
    assert services["app"].health == "STOPPED"
    assert services["executor"].health == "STOPPED"
    assert inventory.status == "CONTROLLED"
    assert inventory.warnings == ()


def test_inventory_reports_live_read_only_executor_as_explicit_session() -> None:
    inventory = build_inventory(
        repository_root=ROOT,
        worktrees=(ROOT,),
        processes={},
        listeners=(),
        tasks=(
            TaskSnapshot(
                "executor",
                LIVE_DEPLOYMENT.executor_task,
                "DISABLED",
                False,
                (),
                persistent_service=False,
            ),
        ),
    )

    executor = inventory.services[0]
    assert executor.health == "EXPLICIT_OBSERVATION_SESSION_REQUIRED"
    assert inventory.warnings == ()


def test_inventory_rejects_enabled_explicit_session_task() -> None:
    inventory = build_inventory(
        repository_root=ROOT,
        worktrees=(ROOT,),
        processes={},
        listeners=(),
        tasks=(
            TaskSnapshot(
                "executor",
                LIVE_DEPLOYMENT.executor_task,
                "READY",
                True,
                (),
                persistent_service=False,
            ),
        ),
    )

    assert inventory.services[0].health == "EXPLICIT_SESSION_TASK_ENABLED"
    assert inventory.warnings == (
        "EXPLICIT_SESSION_TASK_ENABLED:executor",
    )


def test_inventory_reports_any_live_read_only_executor_instance_as_attention() -> None:
    inventory = build_inventory(
        repository_root=ROOT,
        worktrees=(ROOT,),
        processes={
            711: _process(
                711,
                1,
                f'-m halpha.executor --config "{LIVE_EXAMPLE_CONFIG.resolve()}"',
            ),
        },
        listeners=(),
        tasks=(
            TaskSnapshot(
                "executor",
                LIVE_DEPLOYMENT.executor_task,
                "RUNNING",
                False,
                (711,),
                persistent_service=False,
            ),
        ),
    )

    executor = inventory.services[0]
    assert executor.health == "LIVE_READ_ONLY_EXECUTOR_RUNNING"
    assert inventory.status == "ATTENTION_REQUIRED"
    assert inventory.warnings == (
        "LIVE_READ_ONLY_EXECUTOR_RUNNING:executor",
    )


@pytest.mark.parametrize("state", ("QUEUED", "UNKNOWN_0"))
def test_inventory_reports_ambiguous_live_read_only_executor_state_as_attention(
    state: str,
) -> None:
    inventory = build_inventory(
        repository_root=ROOT,
        worktrees=(ROOT,),
        processes={},
        listeners=(),
        tasks=(
            TaskSnapshot(
                "executor",
                LIVE_DEPLOYMENT.executor_task,
                state,
                False,
                (),
                persistent_service=False,
            ),
        ),
    )

    executor = inventory.services[0]
    assert executor.health == "LIVE_READ_ONLY_EXECUTOR_STATE_UNSAFE"
    assert inventory.status == "ATTENTION_REQUIRED"
    assert inventory.warnings == (
        "LIVE_READ_ONLY_EXECUTOR_STATE_UNSAFE:executor",
    )


class _FakeActions:
    def __init__(self, action: SimpleNamespace) -> None:
        self.Count = 1
        self._action = action

    def Item(self, index: int) -> SimpleNamespace:
        assert index == 1
        return self._action


class _FakeTask:
    def __init__(
        self,
        name: str,
        state: int,
        *,
        enabled: bool = True,
        config_path: Path = EXAMPLE_CONFIG,
        engine_pids: tuple[int, ...] = (),
        principal_sid: str | None = None,
    ) -> None:
        service = name.split(".", 1)[0].casefold()
        module = {
            "app": "halpha.app",
            "executor": "halpha.executor",
            "backup": "halpha.backup",
        }[service]
        sid = principal_sid or {
            "app": "S-1-5-21-0-0-0-1101",
            "executor": "S-1-5-21-0-0-0-1102",
            "backup": "S-1-5-21-0-0-0-1104",
        }[service]
        arguments = f'-m {module} --config "{config_path.resolve()}"'
        if service == "backup":
            arguments += " backup"
        self.Name = name
        self.State = state
        self.Enabled = enabled
        self.run_calls = 0
        self.stop_calls = 0
        self._engine_pids = engine_pids
        self.Definition = SimpleNamespace(
            Principal=SimpleNamespace(UserId=sid),
            Actions=_FakeActions(
                SimpleNamespace(
                    Path=str(ROOT / ".venv/Scripts/python.exe"),
                    Arguments=arguments,
                    WorkingDirectory=str(ROOT),
                )
            ),
        )

    def GetInstances(self, _flags: int) -> tuple[object, ...]:
        return tuple(
            SimpleNamespace(EnginePID=pid) for pid in self._engine_pids
        )

    def Run(self, _arguments: str):
        self.run_calls += 1
        self.State = 4
        return SimpleNamespace()

    def Stop(self, _flags: int) -> None:
        self.stop_calls += 1
        self.State = 3


class _FakeTaskService:
    def __init__(self, tasks: dict[str, _FakeTask]) -> None:
        self.tasks = tasks
        self.requests: list[str] = []

    def GetFolder(self, _path: str):
        return self

    def GetTask(self, name: str):
        self.requests.append(name)
        try:
            return self.tasks[name]
        except KeyError:
            raise pywintypes.com_error(
                -2147024894,
                "not found",
                None,
                None,
            ) from None


def test_runtime_controller_targets_only_the_current_environment_tasks() -> None:
    demo_settings = load_settings(EXAMPLE_CONFIG)
    live_settings = load_settings(LIVE_EXAMPLE_CONFIG)
    demo_service = _FakeTaskService(
        {
            DEMO_DEPLOYMENT.app_task: _FakeTask(
                DEMO_DEPLOYMENT.app_task,
                3,
            ),
            DEMO_DEPLOYMENT.executor_task: _FakeTask(
                DEMO_DEPLOYMENT.executor_task,
                3,
            ),
            SHARED_BACKUP_TASK: _FakeTask(SHARED_BACKUP_TASK, 3),
        }
    )
    live_service = _FakeTaskService(
        {
            LIVE_DEPLOYMENT.app_task: _FakeTask(
                LIVE_DEPLOYMENT.app_task,
                3,
                config_path=LIVE_EXAMPLE_CONFIG,
                principal_sid=live_settings.windows.app_task_sid,
            ),
            LIVE_DEPLOYMENT.executor_task: _FakeTask(
                LIVE_DEPLOYMENT.executor_task,
                3,
                config_path=LIVE_EXAMPLE_CONFIG,
                principal_sid=live_settings.windows.executor_task_sid,
            ),
        }
    )
    demo = RuntimeController(
        ROOT,
        demo_settings,
        EXAMPLE_CONFIG,
        task_service_factory=lambda: demo_service,
    )
    live = RuntimeController(
        ROOT,
        live_settings,
        LIVE_EXAMPLE_CONFIG,
        task_service_factory=lambda: live_service,
    )

    assert demo.stop("product")["status"] == "STOPPED"
    assert demo.stop("backup")["status"] == "STOPPED"
    assert live.stop("product")["status"] == "STOPPED"
    live_backup = live.stop("backup")

    assert demo_service.requests == [
        DEMO_DEPLOYMENT.app_task,
        DEMO_DEPLOYMENT.executor_task,
        SHARED_BACKUP_TASK,
    ]
    assert live_service.requests == [
        LIVE_DEPLOYMENT.app_task,
        LIVE_DEPLOYMENT.executor_task,
    ]
    assert live_backup["status"] == "PARTIAL"
    assert live_backup["results"]["backup"]["status"] == "REJECTED"


def test_peer_environment_tasks_are_not_misclassified_as_unmanaged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    peer_pid = 800
    task_service = _FakeTaskService(
        {
            DEMO_DEPLOYMENT.app_task: _FakeTask(
                DEMO_DEPLOYMENT.app_task,
                3,
                enabled=False,
            ),
            DEMO_DEPLOYMENT.executor_task: _FakeTask(
                DEMO_DEPLOYMENT.executor_task,
                3,
                enabled=False,
            ),
            SHARED_BACKUP_TASK: _FakeTask(SHARED_BACKUP_TASK, 3),
            LIVE_DEPLOYMENT.app_task: _FakeTask(
                LIVE_DEPLOYMENT.app_task,
                4,
                config_path=LIVE_EXAMPLE_CONFIG,
                engine_pids=(peer_pid,),
            ),
            LIVE_DEPLOYMENT.executor_task: _FakeTask(
                LIVE_DEPLOYMENT.executor_task,
                3,
                config_path=LIVE_EXAMPLE_CONFIG,
            ),
        }
    )
    controller = RuntimeController(
        ROOT,
        settings,
        EXAMPLE_CONFIG,
        task_service_factory=lambda: task_service,
    )
    monkeypatch.setattr(
        "halpha.runtime_control.discover_worktrees",
        lambda _root: (ROOT,),
    )
    monkeypatch.setattr(
        "halpha.runtime_control.read_project_processes",
        lambda _worktrees: {
            peer_pid: _process(
                peer_pid,
                1,
                f'-m halpha.app --config "{LIVE_EXAMPLE_CONFIG.resolve()}"',
            )
        },
    )
    monkeypatch.setattr("halpha.runtime_control.read_tcp_listeners", lambda: ())

    inventory = controller.inventory()

    assert {service.service for service in inventory.services} == {
        "app",
        "executor",
        "backup",
    }
    assert inventory.unmanaged_service_ids == ()
    assert inventory.warnings == ()


def test_peer_task_instance_discovery_failure_does_not_block_scoped_stop_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    peer_pid = 801
    peer_app = _FakeTask(
        LIVE_DEPLOYMENT.app_task,
        4,
        config_path=LIVE_EXAMPLE_CONFIG,
        engine_pids=(peer_pid,),
    )

    def fail_instances(_flags: int) -> tuple[object, ...]:
        raise pywintypes.com_error(
            -2147024891,
            "access denied",
            None,
            None,
        )

    peer_app.GetInstances = fail_instances  # type: ignore[method-assign]
    task_service = _FakeTaskService(
        {
            DEMO_DEPLOYMENT.app_task: _FakeTask(
                DEMO_DEPLOYMENT.app_task,
                3,
                enabled=False,
            ),
            DEMO_DEPLOYMENT.executor_task: _FakeTask(
                DEMO_DEPLOYMENT.executor_task,
                3,
                enabled=False,
            ),
            SHARED_BACKUP_TASK: _FakeTask(SHARED_BACKUP_TASK, 3),
            LIVE_DEPLOYMENT.app_task: peer_app,
            LIVE_DEPLOYMENT.executor_task: _FakeTask(
                LIVE_DEPLOYMENT.executor_task,
                3,
                config_path=LIVE_EXAMPLE_CONFIG,
            ),
        }
    )
    controller = RuntimeController(
        ROOT,
        settings,
        EXAMPLE_CONFIG,
        task_service_factory=lambda: task_service,
    )
    monkeypatch.setattr(
        "halpha.runtime_control.discover_worktrees",
        lambda _root: (ROOT,),
    )
    monkeypatch.setattr(
        "halpha.runtime_control.read_project_processes",
        lambda _worktrees: {
            peer_pid: _process(
                peer_pid,
                1,
                f'-m halpha.app --config "{LIVE_EXAMPLE_CONFIG.resolve()}"',
            )
        },
    )
    monkeypatch.setattr("halpha.runtime_control.read_tcp_listeners", lambda: ())
    terminated: list[tuple[int, ...]] = []
    monkeypatch.setattr(
        controller,
        "_terminate_process_tree",
        lambda process_ids, **_kwargs: terminated.append(process_ids),
    )

    with pytest.raises(
        RuntimeControlError,
        match="WINDOWS_TASK_INSTANCE_DISCOVERY_FAILED",
    ):
        controller.inventory()

    result = controller.stop("all")

    assert result["status"] == "STOPPED"
    assert terminated == []


def test_running_peer_task_without_engine_pid_does_not_expand_scoped_stop_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    peer_pid = 802
    task_service = _FakeTaskService(
        {
            DEMO_DEPLOYMENT.app_task: _FakeTask(
                DEMO_DEPLOYMENT.app_task,
                3,
                enabled=False,
            ),
            DEMO_DEPLOYMENT.executor_task: _FakeTask(
                DEMO_DEPLOYMENT.executor_task,
                3,
                enabled=False,
            ),
            SHARED_BACKUP_TASK: _FakeTask(SHARED_BACKUP_TASK, 3),
            LIVE_DEPLOYMENT.app_task: _FakeTask(
                LIVE_DEPLOYMENT.app_task,
                4,
                config_path=LIVE_EXAMPLE_CONFIG,
                engine_pids=(),
            ),
            LIVE_DEPLOYMENT.executor_task: _FakeTask(
                LIVE_DEPLOYMENT.executor_task,
                3,
                config_path=LIVE_EXAMPLE_CONFIG,
            ),
        }
    )
    controller = RuntimeController(
        ROOT,
        settings,
        EXAMPLE_CONFIG,
        task_service_factory=lambda: task_service,
    )
    monkeypatch.setattr(
        "halpha.runtime_control.discover_worktrees",
        lambda _root: (ROOT,),
    )
    monkeypatch.setattr(
        "halpha.runtime_control.read_project_processes",
        lambda _worktrees: {
            peer_pid: _process(
                peer_pid,
                1,
                f'-m halpha.app --config "{LIVE_EXAMPLE_CONFIG.resolve()}"',
            )
        },
    )
    monkeypatch.setattr("halpha.runtime_control.read_tcp_listeners", lambda: ())
    terminated: list[tuple[int, ...]] = []
    monkeypatch.setattr(
        controller,
        "_terminate_process_tree",
        lambda process_ids, **_kwargs: terminated.append(process_ids),
    )

    with pytest.raises(
        RuntimeControlError,
        match="OUT_OF_SCOPE_TASK_PROCESS_IDENTITY_UNAVAILABLE",
    ):
        controller.inventory()

    result = controller.stop("all")

    assert result["status"] == "STOPPED"
    assert terminated == []


def test_explicit_executor_stop_does_not_require_instance_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    executor = _FakeTask(
        DEMO_DEPLOYMENT.executor_task,
        4,
        enabled=True,
    )

    def fail_instances(_flags: int) -> tuple[object, ...]:
        raise pywintypes.com_error(
            -2147024891,
            "access denied",
            None,
            None,
        )

    executor.GetInstances = fail_instances  # type: ignore[method-assign]
    task_service = _FakeTaskService(
        {
            DEMO_DEPLOYMENT.executor_task: executor,
        }
    )
    controller = RuntimeController(
        ROOT,
        settings,
        EXAMPLE_CONFIG,
        task_service_factory=lambda: task_service,
    )

    def signal_stop(**_kwargs: object) -> None:
        executor.State = 3

    monkeypatch.setattr("halpha.runtime_control.signal_stop_event", signal_stop)

    result = controller.stop("executor", timeout_seconds=0.1)

    assert result["status"] == "STOPPED"
    assert result["results"]["executor"]["status"] == "STOPPED"
    assert executor.Enabled is False


def test_product_task_start_and_stop_use_one_controller_and_disable_restart(
    monkeypatch,
) -> None:
    settings = load_settings(ROOT / "config/halpha.example.toml")
    app = _FakeTask(DEMO_DEPLOYMENT.app_task, 3)
    executor = _FakeTask(DEMO_DEPLOYMENT.executor_task, 3)
    backup = _FakeTask(SHARED_BACKUP_TASK, 3)
    task_service = _FakeTaskService(
        {
            DEMO_DEPLOYMENT.app_task: app,
            DEMO_DEPLOYMENT.executor_task: executor,
            SHARED_BACKUP_TASK: backup,
        }
    )
    controller = RuntimeController(
        ROOT,
        settings,
        EXAMPLE_CONFIG,
        task_service_factory=lambda: task_service,
    )
    monkeypatch.setattr("halpha.runtime_control.read_tcp_listeners", lambda: ())
    monkeypatch.setattr(
        controller,
        "inventory",
        lambda: SimpleNamespace(services=(), warnings=()),
    )
    monkeypatch.setattr(
        controller,
        "_wait_for_managed_listener",
        lambda *_args, **_kwargs: None,
    )

    started = controller.start("product", timeout_seconds=0.1)

    assert started["status"] == "STARTED"
    assert app.run_calls == 1
    assert executor.run_calls == 1
    events = []

    def signal(**kwargs) -> None:
        service = "app" if kwargs["name"] == settings.windows.app_stop_event else "executor"
        task = app if service == "app" else executor
        events.append((service, task.Enabled))
        task.State = 3

    monkeypatch.setattr("halpha.runtime_control.signal_stop_event", signal)

    stopped = controller.stop("product", timeout_seconds=0.1)

    assert stopped["status"] == "STOPPED"
    assert events == [("app", False), ("executor", False)]
    assert app.Enabled is False
    assert executor.Enabled is False


def test_product_start_rejects_stale_database_before_touching_tasks() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    app = _FakeTask(DEMO_DEPLOYMENT.app_task, 3, enabled=False)
    executor = _FakeTask(DEMO_DEPLOYMENT.executor_task, 3, enabled=False)
    task_service = _FakeTaskService(
        {
            DEMO_DEPLOYMENT.app_task: app,
            DEMO_DEPLOYMENT.executor_task: executor,
        }
    )

    def reject_schema() -> None:
        raise SchemaVersionError(
            "DATABASE_SCHEMA_VERSION_MISMATCH expected=current actual=old"
        )

    controller = RuntimeController(
        ROOT,
        settings,
        EXAMPLE_CONFIG,
        task_service_factory=lambda: task_service,
        schema_readiness=reject_schema,
    )

    with pytest.raises(
        RuntimeControlError,
        match="DATABASE_SCHEMA_NOT_CURRENT.*expected=current actual=old",
    ):
        controller.start("product")

    assert task_service.requests == []
    assert app.run_calls == executor.run_calls == 0
    assert app.Enabled is executor.Enabled is False


def test_autostart_enable_rejects_unavailable_database_schema() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    app = _FakeTask(DEMO_DEPLOYMENT.app_task, 3, enabled=False)
    executor = _FakeTask(DEMO_DEPLOYMENT.executor_task, 3, enabled=False)
    controller = RuntimeController(
        ROOT,
        settings,
        EXAMPLE_CONFIG,
        task_service_factory=lambda: _FakeTaskService(
            {
                DEMO_DEPLOYMENT.app_task: app,
                DEMO_DEPLOYMENT.executor_task: executor,
            }
        ),
        schema_readiness=lambda: (_ for _ in ()).throw(RuntimeError("private")),
    )

    with pytest.raises(
        RuntimeControlError,
        match=(
            "DATABASE_SCHEMA_READINESS_UNAVAILABLE "
            "reason=DATABASE_SCHEMA_READINESS_FAILED type=RuntimeError"
        ),
    ):
        controller.set_autostart("product", enabled=True)

    assert app.Enabled is executor.Enabled is False


def test_database_schema_status_distinguishes_stale_from_unavailable() -> None:
    settings = load_settings(EXAMPLE_CONFIG)

    def unavailable_schema() -> None:
        raise SchemaVersionError("DATABASE_SCHEMA_VERSION_UNAVAILABLE")

    controller = RuntimeController(
        ROOT,
        settings,
        EXAMPLE_CONFIG,
        schema_readiness=unavailable_schema,
    )

    assert controller.database_schema_status() == {
        "status": "UNAVAILABLE",
        "reason": "DATABASE_SCHEMA_VERSION_UNAVAILABLE",
    }


def test_product_autostart_changes_task_eligibility_without_running_tasks() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    app = _FakeTask(DEMO_DEPLOYMENT.app_task, 3, enabled=False)
    executor = _FakeTask(DEMO_DEPLOYMENT.executor_task, 3, enabled=False)
    task_service = _FakeTaskService(
        {
            DEMO_DEPLOYMENT.app_task: app,
            DEMO_DEPLOYMENT.executor_task: executor,
        }
    )
    controller = RuntimeController(
        ROOT,
        settings,
        EXAMPLE_CONFIG,
        task_service_factory=lambda: task_service,
    )

    enabled = controller.set_autostart("product", enabled=True)
    disabled = controller.set_autostart("product", enabled=False)

    assert enabled["status"] == "AUTOSTART_ENABLED"
    assert disabled["status"] == "AUTOSTART_DISABLED"
    assert app.run_calls == executor.run_calls == 0
    assert app.stop_calls == executor.stop_calls == 0
    assert app.Enabled is executor.Enabled is False


def test_read_only_product_autostart_never_enables_explicit_observation_executor() -> None:
    settings = load_settings(LIVE_EXAMPLE_CONFIG)
    app = _FakeTask(
        LIVE_DEPLOYMENT.app_task,
        3,
        enabled=False,
        config_path=LIVE_EXAMPLE_CONFIG,
        principal_sid=settings.windows.app_task_sid,
    )
    executor = _FakeTask(
        LIVE_DEPLOYMENT.executor_task,
        3,
        enabled=False,
        config_path=LIVE_EXAMPLE_CONFIG,
        principal_sid=settings.windows.executor_task_sid,
    )
    controller = RuntimeController(
        ROOT,
        settings,
        LIVE_EXAMPLE_CONFIG,
        task_service_factory=lambda: _FakeTaskService(
            {
                LIVE_DEPLOYMENT.app_task: app,
                LIVE_DEPLOYMENT.executor_task: executor,
            }
        ),
    )

    result = controller.set_autostart("product", enabled=True)

    assert app.Enabled is True
    assert executor.Enabled is False
    assert result["results"]["executor"] == {
        "status": "EXPLICIT_OBSERVATION_SESSION_REQUIRED",
        "service": "executor",
        "enabled": False,
    }


def test_product_start_restores_executor_before_waiting_for_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_settings(ROOT / "config/halpha.example.toml")
    controller = RuntimeController(ROOT, settings, EXAMPLE_CONFIG)
    calls: list[str] = []

    def start_task(service: str, *, timeout_seconds: float) -> dict[str, object]:
        calls.append(service)
        if service == "app":
            raise RuntimeControlError("SERVICE_LISTENER_TIMEOUT service=app port=8765")
        return {"status": "STARTED", "service": service}

    monkeypatch.setattr(controller, "_start_task", start_task)

    with pytest.raises(RuntimeControlError, match="SERVICE_LISTENER_TIMEOUT"):
        controller.start("product", timeout_seconds=0.1)

    assert calls == ["executor", "app"]


def test_live_read_only_product_start_does_not_start_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_settings(LIVE_EXAMPLE_CONFIG)
    app = _FakeTask(
        LIVE_DEPLOYMENT.app_task,
        3,
        config_path=LIVE_EXAMPLE_CONFIG,
        principal_sid=settings.windows.app_task_sid,
    )
    executor = _FakeTask(
        LIVE_DEPLOYMENT.executor_task,
        1,
        enabled=False,
        config_path=LIVE_EXAMPLE_CONFIG,
        principal_sid=settings.windows.executor_task_sid,
    )
    task_service = _FakeTaskService(
        {
            LIVE_DEPLOYMENT.app_task: app,
            LIVE_DEPLOYMENT.executor_task: executor,
        }
    )
    controller = RuntimeController(
        ROOT,
        settings,
        LIVE_EXAMPLE_CONFIG,
        task_service_factory=lambda: task_service,
    )
    monkeypatch.setattr("halpha.runtime_control.read_tcp_listeners", lambda: ())
    monkeypatch.setattr(
        controller,
        "inventory",
        lambda: SimpleNamespace(
            services=(
                SimpleNamespace(
                    manager="WINDOWS_TASK",
                    recognized_as="executor",
                    enabled=False,
                    state="DISABLED",
                    health="EXPLICIT_OBSERVATION_SESSION_REQUIRED",
                    root_pid=None,
                    process_ids=(),
                ),
            ),
            warnings=(),
        ),
    )
    monkeypatch.setattr(
        controller,
        "_wait_for_managed_listener",
        lambda *_args, **_kwargs: None,
    )

    started = controller.start("product", timeout_seconds=0.1)

    assert app.run_calls == 1
    assert executor.run_calls == 0
    assert started["results"]["executor"] == {
        "status": "EXPLICIT_OBSERVATION_SESSION_REQUIRED",
        "service": "executor",
    }
    with pytest.raises(
        RuntimeControlError,
        match="READ_ONLY_EXECUTOR_REQUIRES_EXPLICIT_OBSERVATION_SESSION",
    ):
        controller.start("executor")
    assert executor.Enabled is False

    observation = controller.start(
        "executor",
        observation_session=True,
        timeout_seconds=0.1,
    )

    assert observation == {
        "status": "STARTED",
        "service": "executor",
        "runtime_mode": "EXPLICIT_OBSERVATION_SESSION",
        "enabled": False,
    }
    assert executor.run_calls == 1
    assert executor.Enabled is False


def test_continuous_live_read_only_product_starts_account_observer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_settings = load_settings(LIVE_EXAMPLE_CONFIG)
    settings = base_settings.model_copy(
        update={
            "executor": base_settings.executor.model_copy(
                update={"continuous_account_observation": True}
            )
        }
    )
    app = _FakeTask(
        LIVE_DEPLOYMENT.app_task,
        3,
        config_path=LIVE_EXAMPLE_CONFIG,
        principal_sid=settings.windows.app_task_sid,
    )
    executor = _FakeTask(
        LIVE_DEPLOYMENT.executor_task,
        3,
        config_path=LIVE_EXAMPLE_CONFIG,
        principal_sid=settings.windows.executor_task_sid,
    )
    controller = RuntimeController(
        ROOT,
        settings,
        LIVE_EXAMPLE_CONFIG,
        task_service_factory=lambda: _FakeTaskService(
            {
                LIVE_DEPLOYMENT.app_task: app,
                LIVE_DEPLOYMENT.executor_task: executor,
            }
        ),
    )
    monkeypatch.setattr("halpha.runtime_control.read_tcp_listeners", lambda: ())
    monkeypatch.setattr(
        controller,
        "inventory",
        lambda: SimpleNamespace(services=(), warnings=()),
    )
    monkeypatch.setattr(
        controller,
        "_wait_for_managed_listener",
        lambda *_args, **_kwargs: None,
    )

    started = controller.start("product", timeout_seconds=0.1)

    assert started["status"] == "STARTED"
    assert app.run_calls == 1
    assert executor.run_calls == 1
    assert started["results"]["executor"]["status"] == "STARTED"
    with pytest.raises(
        RuntimeControlError,
        match=(
            "OBSERVATION_SESSION_NOT_AVAILABLE_FOR_CONTINUOUS_ACCOUNT_OBSERVER"
        ),
    ):
        controller.start("executor", observation_session=True)


@pytest.mark.parametrize("target", ("app", "product"))
def test_observation_session_rejects_non_executor_target(target: str) -> None:
    settings = load_settings(LIVE_EXAMPLE_CONFIG)
    controller = RuntimeController(ROOT, settings, LIVE_EXAMPLE_CONFIG)

    with pytest.raises(
        RuntimeControlError,
        match="OBSERVATION_SESSION_REQUIRES_LIVE_READ_ONLY_EXECUTOR",
    ):
        controller.start(target, observation_session=True)


def test_observation_session_rejects_non_read_only_profile() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    controller = RuntimeController(ROOT, settings, EXAMPLE_CONFIG)

    with pytest.raises(
        RuntimeControlError,
        match="OBSERVATION_SESSION_REQUIRES_LIVE_READ_ONLY_EXECUTOR",
    ):
        controller.start("executor", observation_session=True)


def test_observation_session_start_failure_re_disables_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_settings(LIVE_EXAMPLE_CONFIG)
    executor = _FakeTask(
        LIVE_DEPLOYMENT.executor_task,
        1,
        enabled=False,
        config_path=LIVE_EXAMPLE_CONFIG,
        principal_sid=settings.windows.executor_task_sid,
    )
    controller = RuntimeController(
        ROOT,
        settings,
        LIVE_EXAMPLE_CONFIG,
        task_service_factory=lambda: _FakeTaskService(
            {LIVE_DEPLOYMENT.executor_task: executor}
        ),
    )
    monkeypatch.setattr(
        controller,
        "inventory",
        lambda: SimpleNamespace(
            services=(
                SimpleNamespace(
                    manager="WINDOWS_TASK",
                    recognized_as="executor",
                    enabled=False,
                    state="DISABLED",
                    health="EXPLICIT_OBSERVATION_SESSION_REQUIRED",
                    root_pid=None,
                    process_ids=(),
                ),
            ),
            warnings=(),
        ),
    )

    def fail_start(_arguments: str) -> None:
        raise OSError("task scheduler unavailable")

    executor.Run = fail_start

    with pytest.raises(
        RuntimeControlError,
        match="READ_ONLY_OBSERVATION_START_FAILED type=OSError",
    ):
        controller.start("executor", observation_session=True)

    assert executor.Enabled is False


@pytest.mark.parametrize(
    ("enabled", "state", "health", "root_pid", "process_ids"),
    (
        (
            False,
            "RUNNING",
            "LIVE_READ_ONLY_EXECUTOR_RUNNING",
            820,
            (820,),
        ),
        (
            True,
            "READY",
            "EXPLICIT_SESSION_TASK_ENABLED",
            None,
            (),
        ),
        (
            False,
            "QUEUED",
            "LIVE_READ_ONLY_EXECUTOR_STATE_UNSAFE",
            None,
            (),
        ),
        (
            False,
            "UNKNOWN_0",
            "LIVE_READ_ONLY_EXECUTOR_STATE_UNSAFE",
            None,
            (),
        ),
        (
            False,
            "READY",
            "TASK_CONTRACT_MISMATCH",
            None,
            (),
        ),
    ),
    ids=("running", "enabled", "queued", "unknown", "contract-mismatch"),
)
def test_live_read_only_product_start_rejects_nonquiescent_executor(
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    state: str,
    health: str,
    root_pid: int | None,
    process_ids: tuple[int, ...],
) -> None:
    settings = load_settings(LIVE_EXAMPLE_CONFIG)
    app = _FakeTask(
        LIVE_DEPLOYMENT.app_task,
        3,
        config_path=LIVE_EXAMPLE_CONFIG,
        principal_sid=settings.windows.app_task_sid,
    )
    controller = RuntimeController(
        ROOT,
        settings,
        LIVE_EXAMPLE_CONFIG,
        task_service_factory=lambda: _FakeTaskService(
            {LIVE_DEPLOYMENT.app_task: app}
        ),
    )
    monkeypatch.setattr(
        controller,
        "inventory",
        lambda: SimpleNamespace(
                services=(
                    SimpleNamespace(
                        manager="WINDOWS_TASK",
                        recognized_as="executor",
                        enabled=enabled,
                        state=state,
                        health=health,
                        root_pid=root_pid,
                        process_ids=process_ids,
                    ),
                ),
                warnings=(f"{health}:executor",),
            ),
        )

    with pytest.raises(
        RuntimeControlError,
        match="LIVE_READ_ONLY_EXECUTOR_NOT_QUIESCENT instances=1",
    ):
        controller.start("product")

    assert app.run_calls == 0


def test_task_start_refuses_an_unmanaged_instance(monkeypatch) -> None:
    settings = load_settings(ROOT / "config/halpha.example.toml")
    executor = _FakeTask(DEMO_DEPLOYMENT.executor_task, 3)
    controller = RuntimeController(
        ROOT,
        settings,
        EXAMPLE_CONFIG,
        task_service_factory=lambda: _FakeTaskService(
            {DEMO_DEPLOYMENT.executor_task: executor}
        ),
    )
    monkeypatch.setattr(
        controller,
        "inventory",
        lambda: SimpleNamespace(
            services=(
                SimpleNamespace(
                    manager="DISCOVERED_ONLY",
                    recognized_as="executor",
                ),
            ),
            warnings=(),
        ),
    )

    with pytest.raises(RuntimeControlError, match="UNMANAGED_SERVICE_INSTANCE_FOUND"):
        controller.start("executor")

    assert executor.run_calls == 0


@pytest.mark.parametrize(
    ("mutate", "violation"),
    (
        (
            lambda task: setattr(
                task.Definition.Principal,
                "UserId",
                "HOST\\WrongExecutor",
            ),
            "PRINCIPAL_USER_MISMATCH",
        ),
        (
            lambda task: setattr(
                task.Definition.Principal,
                "UserId",
                "S-1-5-21-0-0-0-9999",
            ),
            "PRINCIPAL_SID_MISMATCH",
        ),
        (
            lambda task: setattr(
                task.Definition.Actions._action,
                "Path",
                "D:\\wrong\\python.exe",
            ),
            "ACTION_PATH_MISMATCH",
        ),
        (
            lambda task: setattr(
                task.Definition.Actions._action,
                "Arguments",
                f'-m halpha.app --config "{EXAMPLE_CONFIG.resolve()}"',
            ),
            "ACTION_ARGUMENTS_MISMATCH",
        ),
        (
            lambda task: setattr(
                task.Definition.Actions._action,
                "Arguments",
                '-m halpha.executor --config "config/halpha.example.toml"',
            ),
            "ACTION_ARGUMENTS_MISMATCH",
        ),
        (
            lambda task: setattr(
                task.Definition.Actions._action,
                "WorkingDirectory",
                "D:\\wrong",
            ),
            "WORKING_DIRECTORY_MISMATCH",
        ),
        (
            lambda task: setattr(task.Definition.Actions, "Count", 2),
            "ACTION_COUNT_MISMATCH",
        ),
    ),
    ids=(
        "principal-user",
        "principal-sid",
        "action-path",
        "module",
        "absolute-config",
        "working-directory",
        "action-count",
    ),
)
def test_task_contract_mismatch_blocks_start_and_stop_before_any_change(
    monkeypatch: pytest.MonkeyPatch,
    mutate,
    violation: str,
) -> None:
    settings = load_settings(EXAMPLE_CONFIG)

    start_task = _FakeTask(DEMO_DEPLOYMENT.executor_task, 3, enabled=False)
    mutate(start_task)
    start_controller = RuntimeController(
        ROOT,
        settings,
        EXAMPLE_CONFIG,
        task_service_factory=lambda: _FakeTaskService(
            {DEMO_DEPLOYMENT.executor_task: start_task}
        ),
    )

    with pytest.raises(
        RuntimeControlError,
        match=rf"TASK_CONTRACT_MISMATCH.*{violation}",
    ):
        start_controller.start("executor")

    assert start_task.Enabled is False
    assert start_task.run_calls == 0

    stop_task = _FakeTask(DEMO_DEPLOYMENT.executor_task, 4)
    mutate(stop_task)
    stop_controller = RuntimeController(
        ROOT,
        settings,
        EXAMPLE_CONFIG,
        task_service_factory=lambda: _FakeTaskService(
            {DEMO_DEPLOYMENT.executor_task: stop_task}
        ),
    )
    monkeypatch.setattr(
        "halpha.runtime_control.signal_stop_event",
        lambda **_kwargs: pytest.fail("invalid task must not receive a stop event"),
    )

    stopped = stop_controller.stop("executor")

    assert stopped["status"] == "PARTIAL"
    assert violation in stopped["results"]["executor"]["reason"]
    assert stop_task.Enabled is True
    assert stop_task.stop_calls == 0


def test_stop_all_is_scoped_to_current_environment_tasks(monkeypatch) -> None:
    settings = load_settings(ROOT / "config/halpha.example.toml")
    controller = RuntimeController(ROOT, settings, EXAMPLE_CONFIG)
    calls: list[str] = []

    def stop_task(service: str, **_kwargs):
        calls.append(service)
        return {"status": "STOPPED", "service": service}

    def stop_discovered(service: str, **_kwargs):
        calls.append(service)
        return {"status": "STOPPED", "service": service}

    monkeypatch.setattr(
        controller,
        "inventory",
        lambda: pytest.fail("scoped stop all must not depend on global inventory"),
    )
    monkeypatch.setattr(controller, "_discover_product_process_groups", lambda: ())
    monkeypatch.setattr(controller, "_stop_task", stop_task)
    monkeypatch.setattr(controller, "_stop_discovered", stop_discovered)

    result = controller.stop("all")

    assert result["status"] == "STOPPED"
    assert calls == [
        "app",
        "executor",
        "backup",
    ]


def test_stop_all_terminates_only_proven_current_config_unmanaged_processes(
    monkeypatch,
) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    controller = RuntimeController(ROOT, settings, EXAMPLE_CONFIG)
    terminated: list[tuple[int, ...]] = []
    current = SimpleNamespace(
        service_id="unmanaged:400",
        recognized_as="executor",
        process_ids=(400, 401),
        config_matches_current=True,
    )
    peer = SimpleNamespace(
        service_id="unmanaged:500",
        recognized_as="executor",
        process_ids=(500,),
        config_matches_current=False,
    )
    discoveries = iter(((current, peer), ()))
    monkeypatch.setattr(
        controller,
        "_stop_task",
        lambda service, **_kwargs: {"status": "STOPPED", "service": service},
    )
    monkeypatch.setattr(
        controller,
        "_discover_product_process_groups",
        lambda: next(discoveries),
    )
    monkeypatch.setattr(
        controller,
        "_terminate_current_config_group",
        lambda group, **_kwargs: terminated.append(group.process_ids),
    )

    result = controller.stop("all")

    assert result["status"] == "STOPPED"
    assert terminated == [(400, 401)]
    assert result["results"]["unmanaged:400"]["status"] == "STOPPED"
    assert "unmanaged:500" not in result["results"]


def test_stop_all_reports_unresolved_product_environment_without_terminating(
    monkeypatch,
) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    controller = RuntimeController(ROOT, settings, EXAMPLE_CONFIG)
    monkeypatch.setattr(
        controller,
        "_stop_task",
        lambda service, **_kwargs: {"status": "STOPPED", "service": service},
    )
    monkeypatch.setattr(
        controller,
        "_discover_product_process_groups",
        lambda: (
            SimpleNamespace(
                service_id="unmanaged:400",
                recognized_as="executor",
                process_ids=(400,),
                config_matches_current=None,
            ),
        ),
    )
    monkeypatch.setattr(
        controller,
        "_terminate_process_tree",
        lambda *_args, **_kwargs: pytest.fail(
            "an unresolved Demo/Live process must not be terminated"
        ),
    )

    result = controller.stop("all")

    assert result["status"] == "PARTIAL"
    assert (
        "UNMANAGED_ENVIRONMENT_IDENTITY_UNAVAILABLE"
        in result["results"]["unmanaged:400"]["reason"]
    )


def test_stop_all_does_not_force_current_config_process_after_managed_stop_failure(
    monkeypatch,
) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    controller = RuntimeController(ROOT, settings, EXAMPLE_CONFIG)

    def stop_task(service: str, **_kwargs):
        if service == "executor":
            raise RuntimeControlError("WINDOWS_TASK_STATE_TIMEOUT")
        return {"status": "STOPPED", "service": service}

    monkeypatch.setattr(controller, "_stop_task", stop_task)
    monkeypatch.setattr(
        controller,
        "_discover_product_process_groups",
        lambda: (
            SimpleNamespace(
                service_id="unmanaged:400",
                recognized_as="executor",
                process_ids=(400,),
                config_matches_current=True,
            ),
        ),
    )
    monkeypatch.setattr(
        controller,
        "_terminate_process_tree",
        lambda *_args, **_kwargs: pytest.fail(
            "a possibly managed process must keep the explicit force boundary"
        ),
    )

    result = controller.stop("all")

    assert result["status"] == "PARTIAL"
    assert "CURRENT_CONFIG_PRODUCT_PROCESS_STILL_RUNNING" in (
        result["results"]["unmanaged:400"]["reason"]
    )


def test_product_process_discovery_classifies_current_peer_and_unresolved_configs(
    monkeypatch,
) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    controller = RuntimeController(ROOT, settings, EXAMPLE_CONFIG)
    current = str(EXAMPLE_CONFIG.resolve())
    peer = str(LIVE_EXAMPLE_CONFIG.resolve())
    monkeypatch.setattr(
        "halpha.runtime_control.discover_worktrees",
        lambda _root: (ROOT,),
    )
    monkeypatch.setattr(
        "halpha.runtime_control.read_project_processes",
        lambda _worktrees: {
            400: _process(
                400,
                1,
                f'-m halpha.executor --config "{current}"',
            ),
            500: _process(
                500,
                1,
                f"-m halpha.executor --config={peer}",
            ),
            600: _process(600, 1, "-m halpha.executor"),
        },
    )

    groups = {
        group.service_id: group
        for group in controller._discover_product_process_groups()
    }

    assert groups["unmanaged:400"].config_matches_current is True
    assert groups["unmanaged:500"].config_matches_current is False
    assert groups["unmanaged:600"].config_matches_current is None


def test_product_process_discovery_rejects_relative_and_conflicting_configs(
    monkeypatch,
) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    controller = RuntimeController(ROOT, settings, EXAMPLE_CONFIG)
    current = str(EXAMPLE_CONFIG.resolve())
    peer = str(LIVE_EXAMPLE_CONFIG.resolve())
    monkeypatch.setattr(
        "halpha.runtime_control.discover_worktrees",
        lambda _root: (ROOT,),
    )
    monkeypatch.setattr(
        "halpha.runtime_control.read_project_processes",
        lambda _worktrees: {
            400: _process(
                400,
                1,
                "-m halpha.executor --config config/halpha.example.toml",
            ),
            500: _process(
                500,
                1,
                f'-m halpha.executor --config "{current}" --config "{peer}"',
            ),
            600: _process(
                600,
                1,
                f'-m halpha.executor --config "{current}" --config "{current}"',
            ),
        },
    )

    groups = {
        group.service_id: group
        for group in controller._discover_product_process_groups()
    }

    assert groups["unmanaged:400"].config_matches_current is None
    assert groups["unmanaged:500"].config_matches_current is None
    assert groups["unmanaged:600"].config_matches_current is True


def test_product_process_discovery_requires_the_exact_live_config_path(
    monkeypatch,
    tmp_path,
) -> None:
    alias = tmp_path / "live-alias.toml"
    alias.write_text(
        LIVE_EXAMPLE_CONFIG.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    settings = load_settings(LIVE_EXAMPLE_CONFIG)
    controller = RuntimeController(ROOT, settings, LIVE_EXAMPLE_CONFIG)
    monkeypatch.setattr(
        "halpha.runtime_control.discover_worktrees",
        lambda _root: (ROOT,),
    )
    monkeypatch.setattr(
        "halpha.runtime_control.read_project_processes",
        lambda _worktrees: {
            400: _process(
                400,
                1,
                f'-m halpha.executor --config "{alias}"',
            ),
        },
    )

    groups = controller._discover_product_process_groups()

    assert len(groups) == 1
    assert groups[0].config_matches_current is False


def test_stop_all_postcondition_rejects_new_unresolved_product_process(
    monkeypatch,
) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    controller = RuntimeController(ROOT, settings, EXAMPLE_CONFIG)
    unresolved = SimpleNamespace(
        service_id="unmanaged:400",
        recognized_as="executor",
        process_ids=(400,),
        config_matches_current=None,
    )
    discoveries = iter(((), (unresolved,)))
    monkeypatch.setattr(
        controller,
        "_stop_task",
        lambda service, **_kwargs: {"status": "STOPPED", "service": service},
    )
    monkeypatch.setattr(
        controller,
        "_discover_product_process_groups",
        lambda: next(discoveries),
    )

    result = controller.stop("all")

    assert result["status"] == "PARTIAL"
    assert (
        "UNMANAGED_ENVIRONMENT_IDENTITY_UNAVAILABLE"
        in result["results"]["unmanaged:400"]["reason"]
    )


def test_current_config_termination_rejects_pid_identity_change(
    monkeypatch,
) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    controller = RuntimeController(ROOT, settings, EXAMPLE_CONFIG)
    expected = SimpleNamespace(
        service_id="unmanaged:400",
        recognized_as="executor",
        process_ids=(400,),
        config_matches_current=True,
    )
    changed = SimpleNamespace(
        service_id="unmanaged:400",
        recognized_as="executor",
        process_ids=(400,),
        config_matches_current=False,
    )
    monkeypatch.setattr(
        controller,
        "_discover_product_process_groups",
        lambda: (changed,),
    )
    monkeypatch.setattr(
        controller,
        "_terminate_process_tree",
        lambda *_args, **_kwargs: pytest.fail(
            "changed PID identity must not be terminated"
        ),
    )

    with pytest.raises(
        RuntimeControlError,
        match="UNMANAGED_PROCESS_IDENTITY_CHANGED",
    ):
        controller._terminate_current_config_group(
            expected,
            timeout_seconds=1,
        )


def test_process_tree_termination_rechecks_current_config_identity(
    monkeypatch,
) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    controller = RuntimeController(ROOT, settings, EXAMPLE_CONFIG)
    peer = str(LIVE_EXAMPLE_CONFIG.resolve())
    monkeypatch.setattr(
        "halpha.runtime_control.discover_worktrees",
        lambda _root: (ROOT,),
    )
    monkeypatch.setattr(
        "halpha.runtime_control.read_project_processes",
        lambda _worktrees: {
            400: _process(
                400,
                1,
                f'-m halpha.executor --config "{peer}"',
            ),
        },
    )
    monkeypatch.setattr(
        controller,
        "_terminate_pid",
        lambda *_args, **_kwargs: pytest.fail(
            "a reused peer PID must not be terminated"
        ),
    )

    with pytest.raises(
        RuntimeControlError,
        match="UNMANAGED_PROCESS_IDENTITY_CHANGED",
    ):
        controller._terminate_process_tree(
            (400,),
            timeout_seconds=1,
            expected_service_id="unmanaged:400",
            expected_recognized_as="executor",
            expected_config_path=EXAMPLE_CONFIG,
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process inventory contract")
def test_discovered_unmanaged_listener_can_be_stopped_end_to_end() -> None:
    settings = load_settings(ROOT / "config/halpha.example.toml")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = int(reservation.getsockname()[1])
    process = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        deadline = time.monotonic() + 10
        service_id = None
        while time.monotonic() < deadline:
            status = subprocess.run(
                [
                    str(ROOT / ".venv/Scripts/halpha-control.exe"),
                    "status",
                    "--config",
                    str(EXAMPLE_CONFIG),
                    "--json",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=15,
            )
            assert status.returncode in {0, 3}, status.stderr
            report = json.loads(status.stdout)
            match = next(
                (
                    service
                    for service in report["services"]
                    if f"127.0.0.1:{port}" in service["listeners"]
                ),
                None,
            )
            if match is not None:
                service_id = match["service"]
                break
            time.sleep(0.1)
        assert service_id is not None
        assert service_id.startswith("unmanaged:")
        assert status.returncode == 3

        stopped = subprocess.run(
            [
                str(ROOT / ".venv/Scripts/halpha-control.exe"),
                "stop",
                service_id,
                "--config",
                str(EXAMPLE_CONFIG),
                "--json",
                "--timeout-seconds",
                "5",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        result = json.loads(stopped.stdout)

        assert stopped.returncode == 0, stopped.stderr
        assert result["status"] == "STOPPED", result
        process.wait(timeout=5)
        controller = RuntimeController(ROOT, settings, EXAMPLE_CONFIG)
        assert all(
            f"127.0.0.1:{port}" not in service.listeners
            for service in controller.inventory().services
        )
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
