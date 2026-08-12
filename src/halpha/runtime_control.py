"""One Windows lifecycle view for Halpha tasks, processes, and listeners."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable, Iterable, Mapping

import pywintypes
import win32api
import win32com.client
import win32con
import win32event
import win32security
import winerror

from halpha.configuration import HalphaSettings
from halpha.windows_deployment import (
    SHARED_BACKUP_TASK,
    SHARED_BACKUP_USER,
    peer_windows_deployments,
    windows_deployment,
)
from halpha.windows_runtime import WindowsRuntimeError, signal_stop_event


TASK_FOLDER = r"\Halpha"
TASK_MODULES = {
    "app": "halpha.app",
    "executor": "halpha.executor",
    "backup": "halpha.backup",
}
TASK_STATE_NAMES = {
    0: "UNKNOWN",
    1: "DISABLED",
    2: "QUEUED",
    3: "READY",
    4: "RUNNING",
}
_TASK_NOT_FOUND_HRESULTS = frozenset({0x80070002, 0x80070003})
_CONFIG_ARGUMENT_PATTERN = re.compile(
    r"""(?:^|\s)--config(?:=|\s+)(?:"(?P<double>[^"]+)"|'(?P<single>[^']+)'|(?P<bare>\S+))(?=\s|$)""",
    re.IGNORECASE,
)


class RuntimeControlError(RuntimeError):
    """A sanitized lifecycle discovery or control failure."""


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    parent_pid: int
    name: str
    executable: str | None
    command_line: str | None
    worktree: str


@dataclass(frozen=True)
class ListenerSnapshot:
    local_address: str
    local_port: int
    pid: int

    @property
    def endpoint(self) -> str:
        return f"{self.local_address}:{self.local_port}"


@dataclass(frozen=True)
class TaskSnapshot:
    service: str
    task_name: str
    state: str
    enabled: bool
    engine_pids: tuple[int, ...]
    present: bool = True
    principal_user_id: str | None = None
    principal_sid: str | None = None
    action_count: int | None = None
    action_path: str | None = None
    action_arguments: str | None = None
    working_directory: str | None = None
    contract_violations: tuple[str, ...] = ()
    in_scope: bool = True
    persistent_service: bool = True


@dataclass(frozen=True)
class ServiceSnapshot:
    service: str
    kind: str
    manager: str
    recognized_as: str | None
    enabled: bool | None
    state: str
    health: str
    root_pid: int | None
    process_ids: tuple[int, ...]
    listeners: tuple[str, ...]
    worktree: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _ProductProcessGroup:
    service_id: str
    recognized_as: str
    process_ids: tuple[int, ...]
    config_matches_current: bool | None


@dataclass(frozen=True)
class RuntimeInventory:
    status: str
    repository_root: str
    worktrees: tuple[str, ...]
    services: tuple[ServiceSnapshot, ...]
    unmanaged_service_ids: tuple[str, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "repository_root": self.repository_root,
            "worktrees": list(self.worktrees),
            "services": [service.to_dict() for service in self.services],
            "unmanaged_service_ids": list(self.unmanaged_service_ids),
            "warnings": list(self.warnings),
        }


def _path_key(value: str | Path) -> str:
    return os.path.normcase(str(value)).replace("/", "\\").rstrip("\\")


def _command_key(value: str) -> str:
    return value.strip().replace("/", "\\").casefold()


def _configured_paths(command_line: str | None) -> tuple[str, ...]:
    if not command_line:
        return ()
    paths = []
    for match in _CONFIG_ARGUMENT_PATTERN.finditer(command_line):
        value = match.group("double") or match.group("single") or match.group("bare")
        if value:
            paths.append(value)
    return tuple(paths)


def _config_matches_current(
    command_lines: Iterable[str | None],
    current_config: Path,
) -> bool | None:
    configured_paths = [
        configured_path
        for command_line in command_lines
        for configured_path in _configured_paths(command_line)
    ]
    if not configured_paths:
        return None
    canonical_paths = []
    for configured_path in configured_paths:
        path = Path(configured_path)
        if not path.is_absolute():
            return None
        try:
            canonical_paths.append(_path_key(path.resolve()))
        except OSError:
            return None
    distinct_paths = set(canonical_paths)
    if len(distinct_paths) != 1:
        return None
    # The absolute config path is the process-ownership fact.  A deployment
    # name is not sufficient because LIVE_READ_ONLY and LIVE_WRITE deliberately
    # share one Windows task namespace but carry different authority.
    return distinct_paths.pop() == _path_key(current_config.resolve())


def _principal_sid(user_id: str | None) -> str | None:
    if not user_id:
        return None
    try:
        if user_id.upper().startswith("S-1-"):
            sid = win32security.ConvertStringSidToSid(user_id)
        else:
            sid, _domain, _kind = win32security.LookupAccountName(None, user_id)
        return str(win32security.ConvertSidToStringSid(sid))
    except Exception:
        return None


def _task_definition_fields(
    task: Any,
) -> tuple[
    str | None,
    str | None,
    int | None,
    str | None,
    str | None,
    str | None,
]:
    try:
        definition = task.Definition
        principal_user_id = str(definition.Principal.UserId).strip() or None
        actions = definition.Actions
        action_count = int(actions.Count)
        action = actions.Item(1) if action_count == 1 else None
        return (
            principal_user_id,
            _principal_sid(principal_user_id),
            action_count,
            (
                str(action.Path).strip()
                if action is not None and action.Path is not None
                else None
            ),
            (
                str(action.Arguments).strip()
                if action is not None and action.Arguments is not None
                else None
            ),
            (
                str(action.WorkingDirectory).strip()
                if action is not None and action.WorkingDirectory is not None
                else None
            ),
        )
    except Exception:
        return None, None, None, None, None, None


def _task_scheduler_object_not_found(exc: pywintypes.com_error) -> bool:
    excepinfo = getattr(exc, "excepinfo", None)
    codes = (
        getattr(exc, "hresult", None),
        (
            excepinfo[5]
            if isinstance(excepinfo, tuple) and len(excepinfo) > 5
            else None
        ),
    )
    return any(
        isinstance(code, int)
        and (code & 0xFFFFFFFF) in _TASK_NOT_FOUND_HRESULTS
        for code in codes
    )


def _scheduled_task_snapshot(
    service: str,
    task_name: str,
    task: Any,
    *,
    in_scope: bool = True,
    discover_instances: bool = True,
) -> TaskSnapshot:
    state = int(task.State)
    engine_pids: tuple[int, ...] = ()
    if discover_instances:
        try:
            engine_pids = tuple(
                sorted(
                    int(instance.EnginePID)
                    for instance in task.GetInstances(1)
                    if int(instance.EnginePID) > 0
                )
            )
        except Exception as exc:
            raise RuntimeControlError(
                f"WINDOWS_TASK_INSTANCE_DISCOVERY_FAILED "
                f"task={task_name} type={type(exc).__name__}"
            ) from None
    (
        principal_user_id,
        principal_sid,
        action_count,
        action_path,
        action_arguments,
        working_directory,
    ) = _task_definition_fields(task)
    return TaskSnapshot(
        service=service,
        task_name=task_name,
        state=TASK_STATE_NAMES.get(state, f"UNKNOWN_{state}"),
        enabled=bool(task.Enabled),
        engine_pids=engine_pids,
        principal_user_id=principal_user_id,
        principal_sid=principal_sid,
        action_count=action_count,
        action_path=action_path,
        action_arguments=action_arguments,
        working_directory=working_directory,
        in_scope=in_scope,
    )


def _task_process_matches_definition(
    task: TaskSnapshot,
    processes: tuple[ProcessSnapshot, ...],
) -> bool:
    if task.action_path is None or task.action_arguments is None:
        return False
    expected_executable = _path_key(task.action_path)
    expected_arguments = _command_key(task.action_arguments)
    return any(
        _path_key(process.executable or "") == expected_executable
        and expected_arguments
        and expected_arguments in _command_key(process.command_line or "")
        for process in processes
    )


def discover_worktrees(repository_root: Path) -> tuple[Path, ...]:
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(repository_root),
                "worktree",
                "list",
                "--porcelain",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise RuntimeControlError(
            f"GIT_WORKTREE_DISCOVERY_FAILED type={type(exc).__name__}"
        ) from None
    roots = []
    for line in completed.stdout.splitlines():
        if line.startswith("worktree "):
            roots.append(Path(line.removeprefix("worktree ")).resolve())
    current = repository_root.resolve()
    if current not in roots:
        roots.append(current)
    return tuple(sorted(set(roots), key=lambda path: _path_key(path)))


def _matching_worktree(
    executable: str | None,
    command_line: str | None,
    worktrees: Iterable[Path],
) -> Path | None:
    haystack = _path_key(f"{executable or ''}\0{command_line or ''}")
    matches = []
    for worktree in worktrees:
        key = _path_key(worktree)
        if f"{key}\\" in haystack or haystack.endswith(key):
            matches.append(worktree)
    return max(matches, key=lambda path: len(_path_key(path)), default=None)


def read_project_processes(
    worktrees: tuple[Path, ...],
) -> dict[int, ProcessSnapshot]:
    try:
        service = win32com.client.GetObject(r"winmgmts:root\cimv2")
        rows = service.ExecQuery(
            "SELECT ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine "
            "FROM Win32_Process"
        )
    except Exception as exc:
        raise RuntimeControlError(
            f"WINDOWS_PROCESS_DISCOVERY_FAILED type={type(exc).__name__}"
        ) from None
    processes: dict[int, ProcessSnapshot] = {}
    for row in rows:
        executable = str(row.ExecutablePath) if row.ExecutablePath else None
        command_line = str(row.CommandLine) if row.CommandLine else None
        worktree = _matching_worktree(executable, command_line, worktrees)
        if worktree is None:
            continue
        pid = int(row.ProcessId)
        processes[pid] = ProcessSnapshot(
            pid=pid,
            parent_pid=int(row.ParentProcessId),
            name=str(row.Name),
            executable=executable,
            command_line=command_line,
            worktree=str(worktree),
        )
    return processes


def read_tcp_listeners() -> tuple[ListenerSnapshot, ...]:
    try:
        service = win32com.client.GetObject(
            r"winmgmts:{impersonationLevel=impersonate}!\\.\root\StandardCimv2"
        )
        rows = service.ExecQuery(
            "SELECT LocalAddress,LocalPort,OwningProcess,State "
            "FROM MSFT_NetTCPConnection WHERE State=2"
        )
    except Exception as exc:
        raise RuntimeControlError(
            f"WINDOWS_LISTENER_DISCOVERY_FAILED type={type(exc).__name__}"
        ) from None
    return tuple(
        sorted(
            (
                ListenerSnapshot(
                    local_address=str(row.LocalAddress),
                    local_port=int(row.LocalPort),
                    pid=int(row.OwningProcess),
                )
                for row in rows
            ),
            key=lambda item: (item.local_port, item.local_address, item.pid),
        )
    )


def _scheduled_task_service() -> Any:
    try:
        service = win32com.client.Dispatch("Schedule.Service")
        service.Connect()
        return service
    except Exception as exc:
        raise RuntimeControlError(
            f"WINDOWS_TASK_SCHEDULER_UNAVAILABLE type={type(exc).__name__}"
        ) from None


def read_scheduled_tasks(
    task_service: Any,
    task_names: Mapping[str, str],
    *,
    in_scope: bool = True,
) -> tuple[TaskSnapshot, ...]:
    try:
        folder = task_service.GetFolder(TASK_FOLDER)
    except pywintypes.com_error as exc:
        if _task_scheduler_object_not_found(exc):
            return tuple(
                TaskSnapshot(
                    service=service,
                    task_name=task_name,
                    state="MISSING",
                    enabled=False,
                    engine_pids=(),
                    present=False,
                    in_scope=in_scope,
                )
                for service, task_name in task_names.items()
            )
        raise RuntimeControlError(
            "WINDOWS_TASK_FOLDER_DISCOVERY_FAILED "
            f"type={type(exc).__name__}"
        ) from None
    snapshots = []
    for service, task_name in task_names.items():
        try:
            task = folder.GetTask(task_name)
        except pywintypes.com_error as exc:
            if not _task_scheduler_object_not_found(exc):
                raise RuntimeControlError(
                    "WINDOWS_TASK_DISCOVERY_FAILED "
                    f"task={task_name} type={type(exc).__name__}"
                ) from None
            snapshots.append(
                TaskSnapshot(
                    service=service,
                    task_name=task_name,
                    state="MISSING",
                    enabled=False,
                    engine_pids=(),
                    present=False,
                    in_scope=in_scope,
                )
            )
            continue
        snapshot = _scheduled_task_snapshot(
            service,
            task_name,
            task,
            in_scope=in_scope,
        )
        if (
            not in_scope
            and snapshot.state == "RUNNING"
            and not snapshot.engine_pids
        ):
            raise RuntimeControlError(
                "OUT_OF_SCOPE_TASK_PROCESS_IDENTITY_UNAVAILABLE "
                f"task={task_name}"
            )
        snapshots.append(snapshot)
    return tuple(snapshots)


def _descendants(root_pid: int, processes: dict[int, ProcessSnapshot]) -> tuple[int, ...]:
    selected = {root_pid}
    changed = True
    while changed:
        changed = False
        for process in processes.values():
            if process.parent_pid in selected and process.pid not in selected:
                selected.add(process.pid)
                changed = True
    return tuple(sorted(selected))


def _root_pid(pid: int, processes: dict[int, ProcessSnapshot]) -> int:
    current = pid
    visited = set()
    while current not in visited:
        visited.add(current)
        process = processes.get(current)
        if process is None or process.parent_pid not in processes:
            return current
        parent = processes[process.parent_pid]
        if _path_key(parent.command_line or "") != _path_key(process.command_line or ""):
            return current
        current = parent.pid
    return pid


def _listeners_for(
    process_ids: Iterable[int],
    listeners: tuple[ListenerSnapshot, ...],
) -> tuple[str, ...]:
    selected = set(process_ids)
    return tuple(
        sorted({listener.endpoint for listener in listeners if listener.pid in selected})
    )


def _recognized_service(processes: Iterable[ProcessSnapshot]) -> str | None:
    commands = tuple(
        (process.command_line or "").replace("\\", "/").casefold()
        for process in processes
    )
    for service, signatures in (
        ("app", (" -m halpha.app ", "/halpha-app.exe")),
        ("executor", (" -m halpha.executor ", "/halpha-executor.exe")),
    ):
        if any(
            signature in f" {command} "
            for command in commands
            for signature in signatures
        ):
            return service
    return None


def build_inventory(
    *,
    repository_root: Path,
    worktrees: tuple[Path, ...],
    processes: dict[int, ProcessSnapshot],
    listeners: tuple[ListenerSnapshot, ...],
    tasks: tuple[TaskSnapshot, ...],
) -> RuntimeInventory:
    services: list[ServiceSnapshot] = []
    assigned_pids: set[int] = set()
    warnings: list[str] = []
    for task in tasks:
        process_ids = tuple(
            sorted(
                {
                    pid
                    for engine_pid in task.engine_pids
                    for pid in _descendants(engine_pid, processes)
                }
            )
        )
        assigned_pids.update(process_ids)
        if not task.in_scope:
            continue
        group = tuple(processes[pid] for pid in process_ids if pid in processes)
        health = "OK"
        if not task.present:
            health = "TASK_MISSING"
            warnings.append(f"TASK_MISSING:{task.service}")
        elif task.contract_violations:
            health = "TASK_CONTRACT_MISMATCH"
            warnings.extend(
                f"TASK_CONTRACT_MISMATCH:{task.service}:{violation}"
                for violation in task.contract_violations
            )
        elif not task.persistent_service:
            if task.state == "RUNNING" or task.engine_pids:
                health = "LIVE_READ_ONLY_EXECUTOR_RUNNING"
                warnings.append(
                    f"LIVE_READ_ONLY_EXECUTOR_RUNNING:{task.service}"
                )
            elif task.enabled:
                health = "EXPLICIT_SESSION_TASK_ENABLED"
                warnings.append(
                    f"EXPLICIT_SESSION_TASK_ENABLED:{task.service}"
                )
            elif task.state not in {"DISABLED", "READY"}:
                health = "LIVE_READ_ONLY_EXECUTOR_STATE_UNSAFE"
                warnings.append(
                    f"LIVE_READ_ONLY_EXECUTOR_STATE_UNSAFE:{task.service}"
                )
            else:
                health = "EXPLICIT_OBSERVATION_SESSION_REQUIRED"
        elif task.service != "backup" and task.state != "RUNNING":
            health = "EXPECTED_PROCESS_MISSING" if task.enabled else "STOPPED"
            if task.enabled:
                warnings.append(f"EXPECTED_PROCESS_MISSING:{task.service}")
        elif task.state == "RUNNING" and not task.engine_pids:
            health = "TASK_PROCESS_UNKNOWN"
            warnings.append(f"TASK_PROCESS_UNKNOWN:{task.service}")
        elif task.state == "RUNNING" and not process_ids:
            health = "TASK_PROCESS_OUTSIDE_WORKTREE"
            warnings.append(f"TASK_PROCESS_OUTSIDE_WORKTREE:{task.service}")
        elif (
            task.state == "RUNNING"
            and task.action_path is not None
            and not _task_process_matches_definition(task, group)
        ):
            health = "TASK_PROCESS_IDENTITY_MISMATCH"
            warnings.append(f"TASK_PROCESS_IDENTITY_MISMATCH:{task.service}")
        root_pid = task.engine_pids[0] if task.engine_pids else None
        root = processes.get(root_pid) if root_pid is not None else None
        task_listeners = _listeners_for(process_ids, listeners)
        if (
            task.service == "app"
            and task.state == "RUNNING"
            and not task_listeners
            and health == "OK"
        ):
            health = "EXPECTED_LISTENER_MISSING"
            warnings.append("EXPECTED_LISTENER_MISSING:app")
        services.append(
            ServiceSnapshot(
                service=task.service,
                kind="JOB" if task.service == "backup" else "PRODUCT_SERVICE",
                manager="WINDOWS_TASK",
                recognized_as=task.service,
                enabled=task.enabled,
                state=task.state,
                health=health,
                root_pid=root_pid,
                process_ids=process_ids,
                listeners=task_listeners,
                worktree=root.worktree if root is not None else None,
            )
        )

    candidate_pids = {
        listener.pid
        for listener in listeners
        if listener.pid in processes and listener.pid not in assigned_pids
    }
    candidate_pids.update(
        process.pid
        for process in processes.values()
        if process.pid not in assigned_pids
        and _recognized_service((process,)) is not None
    )
    candidate_roots = {_root_pid(pid, processes) for pid in candidate_pids}
    for root_pid in sorted(candidate_roots):
        process_ids = _descendants(root_pid, processes)
        assigned_pids.update(process_ids)
        group = tuple(processes[pid] for pid in process_ids if pid in processes)
        recognized_as = _recognized_service(group)
        root = processes.get(root_pid)
        process_listeners = _listeners_for(process_ids, listeners)
        services.append(
            ServiceSnapshot(
                service=f"unmanaged:{root_pid}",
                kind=(
                    "UNMANAGED_PROJECT_SERVICE"
                    if recognized_as is not None
                    else "UNMANAGED_LISTENER"
                ),
                manager="DISCOVERED_ONLY",
                recognized_as=recognized_as,
                enabled=None,
                state="RUNNING",
                health="UNREGISTERED",
                root_pid=root_pid,
                process_ids=process_ids,
                listeners=process_listeners,
                worktree=root.worktree if root is not None else None,
            )
        )
    unmanaged_services = tuple(
        service for service in services if service.manager == "DISCOVERED_ONLY"
    )
    unmanaged = tuple(dict.fromkeys(service.service for service in unmanaged_services))
    if unmanaged_services:
        warnings.append("UNMANAGED_PROJECT_PROCESS_FOUND")
    return RuntimeInventory(
        status="CONTROLLED" if not warnings else "ATTENTION_REQUIRED",
        repository_root=str(repository_root.resolve()),
        worktrees=tuple(str(path) for path in worktrees),
        services=tuple(
            sorted(
                services,
                key=lambda service: (
                    {"app": 0, "executor": 1, "backup": 2}.get(service.service, 3),
                    service.service,
                    service.root_pid or 0,
                ),
            )
        ),
        unmanaged_service_ids=unmanaged,
        warnings=tuple(sorted(warnings)),
    )


class RuntimeController:
    def __init__(
        self,
        repository_root: Path,
        settings: HalphaSettings,
        config_path: Path,
        *,
        task_service_factory: Callable[[], Any] = _scheduled_task_service,
    ) -> None:
        self._root = repository_root.resolve()
        self._settings = settings
        self._config_path = config_path.resolve()
        self._task_service_factory = task_service_factory
        deployment = windows_deployment(settings.release.venue_account_type.value)
        self._deployment = deployment
        peers = peer_windows_deployments(settings.release.venue_account_type.value)
        self._task_names = {
            "app": deployment.app_task,
            "executor": deployment.executor_task,
        }
        if deployment.owns_shared_backup:
            self._task_names["backup"] = SHARED_BACKUP_TASK
        self._task_users = {
            "app": deployment.app_user,
            "executor": deployment.executor_user,
        }
        if deployment.owns_shared_backup:
            self._task_users["backup"] = SHARED_BACKUP_USER
        self._peer_task_names = {
            f"peer:{peer.namespace}:app": peer.app_task
            for peer in peers
        } | {
            f"peer:{peer.namespace}:executor": peer.executor_task
            for peer in peers
        }
        if not deployment.owns_shared_backup:
            self._peer_task_names["peer:backup"] = SHARED_BACKUP_TASK

    def _expected_task_sid(self, service: str) -> str:
        windows = self._settings.windows
        return {
            "app": windows.app_task_sid,
            "executor": windows.executor_task_sid,
            "backup": windows.backup_task_sid,
        }[service]

    def _expected_task_arguments(self, service: str) -> str:
        arguments = (
            f'-m {TASK_MODULES[service]} --config "{self._config_path}"'
        )
        return f"{arguments} backup" if service == "backup" else arguments

    def _task_contract_violations(self, task: TaskSnapshot) -> tuple[str, ...]:
        violations: list[str] = []
        principal_user_id = task.principal_user_id
        if (
            principal_user_id
            and not principal_user_id.upper().startswith("S-1-")
            and principal_user_id.rsplit("\\", 1)[-1].casefold()
            != self._task_users[task.service].casefold()
        ):
            violations.append("PRINCIPAL_USER_MISMATCH")
        if task.principal_sid is None:
            violations.append("PRINCIPAL_SID_UNAVAILABLE")
        elif task.principal_sid.casefold() != self._expected_task_sid(
            task.service
        ).casefold():
            violations.append("PRINCIPAL_SID_MISMATCH")
        if task.action_count != 1:
            violations.append("ACTION_COUNT_MISMATCH")
        expected_action = self._root / ".venv" / "Scripts" / "python.exe"
        if (
            task.action_path is None
            or _path_key(task.action_path) != _path_key(expected_action)
        ):
            violations.append("ACTION_PATH_MISMATCH")
        if (
            task.action_arguments is None
            or _command_key(task.action_arguments)
            != _command_key(self._expected_task_arguments(task.service))
        ):
            violations.append("ACTION_ARGUMENTS_MISMATCH")
        if (
            task.working_directory is None
            or _path_key(task.working_directory) != _path_key(self._root)
        ):
            violations.append("WORKING_DIRECTORY_MISMATCH")
        return tuple(violations)

    def _validated_task_snapshot(
        self,
        service: str,
        task: Any,
    ) -> TaskSnapshot:
        # Principal, action and working-directory validation must remain
        # available even if Task Scheduler cannot enumerate a running instance.
        # Explicit stop uses this definition-only path so emergency stop does
        # not depend on process inventory.
        snapshot = _scheduled_task_snapshot(
            service,
            self._task_names[service],
            task,
            discover_instances=False,
        )
        return replace(
            snapshot,
            contract_violations=self._task_contract_violations(snapshot),
        )

    def _require_task_contract(self, service: str, task: Any) -> None:
        try:
            violations = self._validated_task_snapshot(
                service,
                task,
            ).contract_violations
        except Exception as exc:
            raise RuntimeControlError(
                f"TASK_CONTRACT_READ_FAILED service={service} "
                f"type={type(exc).__name__}"
            ) from None
        if violations:
            raise RuntimeControlError(
                f"TASK_CONTRACT_MISMATCH service={service} "
                f"violations={','.join(violations)}"
            )

    def inventory(self) -> RuntimeInventory:
        try:
            worktrees = discover_worktrees(self._root)
            processes = read_project_processes(worktrees)
            listeners = read_tcp_listeners()
            task_service = self._task_service_factory()
            current_tasks = tuple(
                (
                    replace(
                        task,
                        contract_violations=self._task_contract_violations(task),
                        persistent_service=not (
                            self._settings.release.profile
                            == "BINANCE_LIVE_READ_ONLY"
                            and task.service == "executor"
                            and not self._settings.executor.continuous_account_observation
                        ),
                    )
                    if task.present
                    else task
                )
                for task in read_scheduled_tasks(
                    task_service,
                    self._task_names,
                )
            )
            peer_tasks = read_scheduled_tasks(
                task_service,
                self._peer_task_names,
                in_scope=False,
            )
            return build_inventory(
                repository_root=self._root,
                worktrees=worktrees,
                processes=processes,
                listeners=listeners,
                tasks=(
                    *current_tasks,
                    *peer_tasks,
                ),
            )
        except RuntimeControlError:
            raise
        except Exception as exc:
            raise RuntimeControlError(
                f"RUNTIME_INVENTORY_FAILED type={type(exc).__name__}"
            ) from None

    def _require_live_read_only_executor_quiescent(
        self,
        inventory: RuntimeInventory,
    ) -> None:
        if self._settings.release.profile != "BINANCE_LIVE_READ_ONLY":
            return
        if self._settings.executor.continuous_account_observation:
            return
        executors = tuple(
            instance
            for instance in inventory.services
            if instance.recognized_as == "executor"
        )
        if len(executors) == 1:
            executor = executors[0]
            if (
                executor.manager == "WINDOWS_TASK"
                and executor.enabled is False
                and executor.state in {"DISABLED", "READY"}
                and executor.health
                == "EXPLICIT_OBSERVATION_SESSION_REQUIRED"
                and executor.root_pid is None
                and not executor.process_ids
            ):
                return
        raise RuntimeControlError(
            "LIVE_READ_ONLY_EXECUTOR_NOT_QUIESCENT "
            f"instances={len(executors)}"
        )

    def start(
        self,
        target: str,
        *,
        observation_session: bool = False,
        timeout_seconds: float = 15.0,
    ) -> dict[str, object]:
        read_only = self._settings.release.profile == "BINANCE_LIVE_READ_ONLY"
        continuous_observer = (
            read_only
            and self._settings.executor.continuous_account_observation
        )
        if observation_session and (target != "executor" or not read_only):
            raise RuntimeControlError(
                "OBSERVATION_SESSION_REQUIRES_LIVE_READ_ONLY_EXECUTOR"
            )
        if observation_session and continuous_observer:
            raise RuntimeControlError(
                "OBSERVATION_SESSION_NOT_AVAILABLE_FOR_CONTINUOUS_ACCOUNT_OBSERVER"
            )
        if target == "product":
            services = (
                # Restore the execution and protection loop before waiting for the
                # user-facing listener. A slow App cold start must not prevent the
                # Executor from resuming existing responsibilities.
                ("executor", "app")
                if not read_only or continuous_observer
                else ("app",)
            )
            results = {
                service: self._start_task(service, timeout_seconds=timeout_seconds)
                for service in services
            }
            if read_only and not continuous_observer:
                results["executor"] = {
                    "status": "EXPLICIT_OBSERVATION_SESSION_REQUIRED",
                    "service": "executor",
                }
            return {"status": "STARTED", "target": target, "results": results}
        if (
            target == "executor"
            and read_only
            and not continuous_observer
        ):
            if observation_session:
                return self._start_read_only_observation_session(
                    timeout_seconds=timeout_seconds
                )
            raise RuntimeControlError(
                "READ_ONLY_EXECUTOR_REQUIRES_EXPLICIT_OBSERVATION_SESSION"
            )
        if target in self._task_names:
            return self._start_task(target, timeout_seconds=timeout_seconds)
        raise RuntimeControlError(f"SERVICE_TARGET_UNSUPPORTED target={target}")

    def _start_read_only_observation_session(
        self,
        *,
        timeout_seconds: float,
    ) -> dict[str, object]:
        service = "executor"
        task = self._task(service)
        self._require_task_contract(service, task)
        inventory = self.inventory()
        self._require_live_read_only_executor_quiescent(inventory)
        try:
            task.Enabled = True
            if not bool(task.Enabled):
                raise RuntimeControlError(
                    "READ_ONLY_OBSERVATION_TASK_ENABLE_FAILED"
                )
            task.Run("")
            self._wait_for_task(
                task,
                running=True,
                timeout_seconds=timeout_seconds,
            )
        except RuntimeControlError:
            raise
        except Exception as exc:
            raise RuntimeControlError(
                "READ_ONLY_OBSERVATION_START_FAILED "
                f"type={type(exc).__name__}"
            ) from None
        finally:
            try:
                task.Enabled = False
            except Exception as exc:
                raise RuntimeControlError(
                    "READ_ONLY_OBSERVATION_TASK_DISABLE_FAILED "
                    f"type={type(exc).__name__}"
                ) from None
        if bool(task.Enabled):
            raise RuntimeControlError(
                "READ_ONLY_OBSERVATION_TASK_DISABLE_FAILED"
            )
        return {
            "status": "STARTED",
            "service": service,
            "runtime_mode": "EXPLICIT_OBSERVATION_SESSION",
            "enabled": False,
        }

    def set_autostart(
        self,
        target: str,
        *,
        enabled: bool,
    ) -> dict[str, object]:
        """Change only task trigger eligibility; never start or stop a task here."""
        if target == "product":
            services = ("app", "executor")
        elif target in {"app", "executor"}:
            services = (target,)
        else:
            raise RuntimeControlError(f"SERVICE_TARGET_UNSUPPORTED target={target}")
        results: dict[str, object] = {}
        for service in services:
            task = self._task(service)
            self._require_task_contract(service, task)
            explicit_read_only_executor = (
                service == "executor"
                and self._settings.release.profile == "BINANCE_LIVE_READ_ONLY"
                and not self._settings.executor.continuous_account_observation
            )
            if explicit_read_only_executor:
                if enabled:
                    results[service] = {
                        "status": "EXPLICIT_OBSERVATION_SESSION_REQUIRED",
                        "service": service,
                        "enabled": False,
                    }
                    continue
                task.Enabled = False
                results[service] = {
                    "status": "AUTOSTART_DISABLED",
                    "service": service,
                    "enabled": False,
                }
                continue
            task.Enabled = enabled
            if bool(task.Enabled) is not enabled:
                raise RuntimeControlError(
                    f"TASK_AUTOSTART_UPDATE_FAILED service={service}"
                )
            results[service] = {
                "status": "AUTOSTART_ENABLED" if enabled else "AUTOSTART_DISABLED",
                "service": service,
                "enabled": enabled,
            }
        return {
            "status": "AUTOSTART_ENABLED" if enabled else "AUTOSTART_DISABLED",
            "target": target,
            "enabled": enabled,
            "results": results,
        }

    def stop(
        self,
        target: str,
        *,
        force: bool = False,
        timeout_seconds: float = 30.0,
    ) -> dict[str, object]:
        if target == "product":
            targets = ("app", "executor")
        elif target == "all":
            targets = tuple(self._task_names)
        else:
            targets = (target,)
        results: dict[str, object] = {}
        failures = False
        seen = set()
        for service in targets:
            if service in seen:
                continue
            seen.add(service)
            try:
                if service in self._task_names:
                    result = self._stop_task(
                        service,
                        force=force,
                        timeout_seconds=timeout_seconds,
                    )
                elif service.startswith("unmanaged:"):
                    result = self._stop_discovered(service, timeout_seconds=timeout_seconds)
                else:
                    raise RuntimeControlError(
                        f"SERVICE_TARGET_UNSUPPORTED target={service}"
                    )
                results[service] = result
            except (RuntimeControlError, WindowsRuntimeError) as exc:
                failures = True
                results[service] = {"status": "REJECTED", "reason": str(exc)}
        if target == "all":
            try:
                product_groups = self._discover_product_process_groups()
            except RuntimeControlError as exc:
                failures = True
                results["unmanaged"] = {
                    "status": "REJECTED",
                    "reason": str(exc),
                }
            else:
                for group in product_groups:
                    if group.config_matches_current is False:
                        continue
                    if group.config_matches_current is None:
                        failures = True
                        results[group.service_id] = {
                            "status": "REJECTED",
                            "reason": (
                                "UNMANAGED_ENVIRONMENT_IDENTITY_UNAVAILABLE "
                                f"recognized_as={group.recognized_as}"
                            ),
                        }
                        continue
                    managed_result = results.get(group.recognized_as, {})
                    if (
                        not isinstance(managed_result, Mapping)
                        or managed_result.get("status") == "REJECTED"
                    ):
                        failures = True
                        results[group.service_id] = {
                            "status": "REJECTED",
                            "reason": (
                                "MANAGED_STOP_INCOMPLETE "
                                f"service={group.recognized_as}"
                            ),
                        }
                        continue
                    try:
                        self._terminate_current_config_group(
                            group,
                            timeout_seconds=timeout_seconds,
                        )
                        results[group.service_id] = {
                            "status": "STOPPED",
                            "service": group.service_id,
                            "instances": 1,
                        }
                    except (RuntimeControlError, WindowsRuntimeError) as exc:
                        failures = True
                        results[group.service_id] = {
                            "status": "REJECTED",
                            "reason": str(exc),
                        }
                try:
                    remaining_groups = self._discover_product_process_groups()
                except RuntimeControlError as exc:
                    failures = True
                    results["unmanaged-postcondition"] = {
                        "status": "REJECTED",
                        "reason": str(exc),
                    }
                else:
                    for group in remaining_groups:
                        if group.config_matches_current is False:
                            continue
                        failures = True
                        results[group.service_id] = {
                            "status": "REJECTED",
                            "reason": (
                                (
                                    "CURRENT_CONFIG_PRODUCT_PROCESS_STILL_RUNNING"
                                    if group.config_matches_current is True
                                    else "UNMANAGED_ENVIRONMENT_IDENTITY_UNAVAILABLE"
                                )
                                + f" recognized_as={group.recognized_as}"
                            ),
                        }
        return {
            "status": "PARTIAL" if failures else "STOPPED",
            "target": target,
            "results": results,
        }

    def _discover_product_process_groups(
        self,
    ) -> tuple[_ProductProcessGroup, ...]:
        worktrees = discover_worktrees(self._root)
        processes = read_project_processes(worktrees)
        return self._product_process_groups_from(
            processes,
            self._config_path,
        )

    @staticmethod
    def _product_process_groups_from(
        processes: dict[int, ProcessSnapshot],
        current_config: Path,
    ) -> tuple[_ProductProcessGroup, ...]:
        candidate_pids = {
            process.pid
            for process in processes.values()
            if _recognized_service((process,)) is not None
        }
        roots = {_root_pid(pid, processes) for pid in candidate_pids}
        groups = []
        for root_pid in sorted(roots):
            process_ids = _descendants(root_pid, processes)
            group = tuple(
                processes[pid] for pid in process_ids if pid in processes
            )
            recognized_as = _recognized_service(group)
            if recognized_as is None:
                continue
            groups.append(
                _ProductProcessGroup(
                    service_id=f"unmanaged:{root_pid}",
                    recognized_as=recognized_as,
                    process_ids=process_ids,
                    config_matches_current=_config_matches_current(
                        (process.command_line for process in group),
                        current_config,
                    ),
                )
            )
        return tuple(groups)

    def _terminate_current_config_group(
        self,
        expected: _ProductProcessGroup,
        *,
        timeout_seconds: float,
    ) -> None:
        current = next(
            (
                group
                for group in self._discover_product_process_groups()
                if group.service_id == expected.service_id
            ),
            None,
        )
        if current is None:
            return
        if (
            current.recognized_as != expected.recognized_as
            or current.config_matches_current is not True
        ):
            raise RuntimeControlError(
                "UNMANAGED_PROCESS_IDENTITY_CHANGED "
                f"service={expected.service_id}"
            )
        self._terminate_process_tree(
            current.process_ids,
            timeout_seconds=timeout_seconds,
            expected_service_id=current.service_id,
            expected_recognized_as=current.recognized_as,
            expected_config_path=self._config_path,
        )

    def _task(self, service: str) -> Any:
        try:
            folder = self._task_service_factory().GetFolder(TASK_FOLDER)
            return folder.GetTask(self._task_names[service])
        except Exception as exc:
            raise RuntimeControlError(
                f"WINDOWS_TASK_LOOKUP_FAILED service={service} type={type(exc).__name__}"
            ) from None

    def _start_task(self, service: str, *, timeout_seconds: float) -> dict[str, object]:
        if (
            service == "executor"
            and self._settings.release.profile == "BINANCE_LIVE_READ_ONLY"
            and not self._settings.executor.continuous_account_observation
        ):
            raise RuntimeControlError(
                "READ_ONLY_EXECUTOR_REQUIRES_EXPLICIT_OBSERVATION_SESSION"
            )
        task = self._task(service)
        self._require_task_contract(service, task)
        inventory = self.inventory()
        self._require_live_read_only_executor_quiescent(inventory)
        unmanaged = [
            instance
            for instance in inventory.services
            if instance.manager == "DISCOVERED_ONLY"
            and instance.recognized_as == service
        ]
        if unmanaged:
            raise RuntimeControlError(
                f"UNMANAGED_SERVICE_INSTANCE_FOUND service={service} "
                f"instances={len(unmanaged)}"
            )
        if int(task.State) == 4:
            task.Enabled = True
            if service == "app":
                self._wait_for_managed_listener(
                    service,
                    port=self._settings.app.port,
                    timeout_seconds=timeout_seconds,
                )
            return {"status": "ALREADY_RUNNING", "service": service}
        if service == "app":
            occupied = [
                listener
                for listener in read_tcp_listeners()
                if listener.local_port == self._settings.app.port
            ]
            if occupied:
                raise RuntimeControlError(
                    f"SERVICE_PORT_ALREADY_IN_USE service=app port={self._settings.app.port}"
                )
        task.Enabled = True
        task.Run("")
        if service == "backup":
            return {"status": "START_REQUESTED", "service": service}
        self._wait_for_task(task, running=True, timeout_seconds=timeout_seconds)
        if service == "app":
            self._wait_for_managed_listener(
                service,
                port=self._settings.app.port,
                timeout_seconds=timeout_seconds,
            )
        return {"status": "STARTED", "service": service}

    def _stop_task(
        self,
        service: str,
        *,
        force: bool,
        timeout_seconds: float,
    ) -> dict[str, object]:
        task = self._task(service)
        self._require_task_contract(service, task)
        task.Enabled = False
        if int(task.State) != 4:
            return {"status": "ALREADY_STOPPED", "service": service, "enabled": False}
        if service == "backup":
            task.Stop(0)
        else:
            windows = self._settings.windows
            name, task_sid = (
                (windows.app_stop_event, windows.app_task_sid)
                if service == "app"
                else (windows.executor_stop_event, windows.executor_task_sid)
            )
            signal_stop_event(
                name=name,
                task_sid=task_sid,
                maintenance_sid=windows.maintenance_sid,
            )
        try:
            self._wait_for_task(task, running=False, timeout_seconds=timeout_seconds)
        except RuntimeControlError:
            if not force:
                raise
            task.Stop(0)
            self._wait_for_task(task, running=False, timeout_seconds=timeout_seconds)
        return {"status": "STOPPED", "service": service, "enabled": False}

    @staticmethod
    def _wait_for_task(task: Any, *, running: bool, timeout_seconds: float) -> None:
        expected = 4 if running else 3
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            state = int(task.State)
            if (running and state == expected) or (not running and state != 4):
                return
            time.sleep(0.1)
        raise RuntimeControlError(
            f"WINDOWS_TASK_STATE_TIMEOUT task={task.Name} expected="
            f"{'RUNNING' if running else 'STOPPED'} actual={int(task.State)}"
        )

    def _wait_for_managed_listener(
        self,
        service: str,
        *,
        port: int,
        timeout_seconds: float,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            managed = next(
                (
                    instance
                    for instance in self.inventory().services
                    if instance.service == service
                    and instance.manager == "WINDOWS_TASK"
                ),
                None,
            )
            if managed is not None and any(
                endpoint.endswith(f":{port}") for endpoint in managed.listeners
            ):
                return
            time.sleep(0.1)
        raise RuntimeControlError(
            f"SERVICE_LISTENER_TIMEOUT service={service} port={port}"
        )

    def _stop_discovered(
        self,
        service_id: str,
        *,
        timeout_seconds: float,
    ) -> dict[str, object]:
        inventory = self.inventory()
        matches = [service for service in inventory.services if service.service == service_id]
        if not matches:
            return {"status": "ALREADY_STOPPED", "service": service_id}
        for service in matches:
            if service.manager == "WINDOWS_TASK":
                raise RuntimeControlError("DISCOVERED_STOP_TARGET_IS_SCHEDULED_TASK")
            self._terminate_process_tree(
                service.process_ids,
                timeout_seconds=timeout_seconds,
            )
        return {
            "status": "STOPPED",
            "service": service_id,
            "instances": len(matches),
        }

    def _terminate_process_tree(
        self,
        process_ids: tuple[int, ...],
        *,
        timeout_seconds: float,
        expected_service_id: str | None = None,
        expected_recognized_as: str | None = None,
        expected_config_path: Path | None = None,
    ) -> None:
        if os.getpid() in process_ids:
            raise RuntimeControlError("CONTROL_PROCESS_TERMINATION_FORBIDDEN")
        worktrees = discover_worktrees(self._root)
        processes = read_project_processes(worktrees)
        if (
            expected_service_id is not None
            or expected_recognized_as is not None
            or expected_config_path is not None
        ):
            if (
                expected_service_id is None
                or expected_recognized_as is None
                or expected_config_path is None
            ):
                raise RuntimeControlError(
                    "PROCESS_IDENTITY_EXPECTATION_INCOMPLETE"
                )
            current_group = next(
                (
                    group
                    for group in self._product_process_groups_from(
                        processes,
                        expected_config_path,
                    )
                    if group.service_id == expected_service_id
                ),
                None,
            )
            if (
                current_group is None
                or current_group.recognized_as != expected_recognized_as
                or current_group.config_matches_current is not True
                or current_group.process_ids != tuple(sorted(set(process_ids)))
            ):
                raise RuntimeControlError(
                    "UNMANAGED_PROCESS_IDENTITY_CHANGED "
                    f"service={expected_service_id}"
                )

        def depth(pid: int) -> int:
            value = 0
            current = processes.get(pid)
            while current is not None and current.parent_pid in processes:
                value += 1
                current = processes.get(current.parent_pid)
            return value

        for pid in sorted(set(process_ids), key=depth, reverse=True):
            if pid not in processes:
                continue
            self._terminate_pid(pid, timeout_seconds=timeout_seconds)

    @staticmethod
    def _terminate_pid(pid: int, *, timeout_seconds: float) -> None:
        try:
            handle = win32api.OpenProcess(
                win32con.PROCESS_TERMINATE | win32con.SYNCHRONIZE,
                False,
                pid,
            )
        except pywintypes.error as exc:
            if exc.winerror == winerror.ERROR_INVALID_PARAMETER:
                return
            raise RuntimeControlError(
                f"PROCESS_OPEN_FAILED pid={pid} code={exc.winerror}"
            ) from None
        try:
            win32api.TerminateProcess(handle, 1)
            result = win32event.WaitForSingleObject(handle, int(timeout_seconds * 1000))
            if result == win32con.WAIT_TIMEOUT:
                raise RuntimeControlError(f"PROCESS_STOP_TIMEOUT pid={pid}")
        finally:
            handle.Close()
