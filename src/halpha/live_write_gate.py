"""Detached, fail-closed LIVE_WRITE deployment switch verification.

The switch binds a build to one environment and account. The user's fixed
plans and explicit activations are the only trading authority; the switch does
not reproduce capital limits, allocations, acknowledgements, or authorization
records. New activations are admitted only from the current build; an already
running activation remains bound to its immutable snapshot and persisted action
identities when a later build takes over execution.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any, Callable, Literal

import pywintypes
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    model_validator,
)
import win32file
import win32security

from halpha.configuration import HalphaSettings, VenueAccountType
from halpha.database.security_contract import (
    LIVE_ACTIVATION_SAFETY_INDEX,
    live_activation_safety_index_qualified,
)
from halpha.product_build import calculate_product_build_id
from halpha.source_identity import SourceIdentityError
from halpha.windows_runtime import BUILTIN_ADMINISTRATORS_SID, SYSTEM_SID


class LiveWriteGateError(RuntimeError):
    """Sanitized fail-closed LIVE_WRITE gate failure."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LiveWriteGateBinding(_FrozenModel):
    schema_version: Literal[5]
    environment_id: str
    account_id: str
    venue_account_type: Literal[
        VenueAccountType.USDM_COPY_LEAD,
        VenueAccountType.USDM_PERSONAL,
    ]
    profile: Literal["BINANCE_LIVE_WRITE"]
    runtime_real_write_gate: Literal["CLOSED", "OPEN"]
    product_build_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    binance_api_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_window(self) -> "LiveWriteGateBinding":
        if self.effective_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("LIVE_WRITE_GATE_TIMEZONE_REQUIRED")
        if self.expires_at <= self.effective_at:
            raise ValueError("LIVE_WRITE_GATE_WINDOW_INVALID")
        return self


class LiveWriteGateStatus(_FrozenModel):
    configured_runtime_real_write_gate: Literal["CLOSED", "OPEN"]
    runtime_real_write_gate: Literal["CLOSED", "OPEN"]
    risk_control_only: bool = False
    product_build_id: str | None = None
    product_build_consistent: bool | None = None
    authorized_activation_ids: tuple[str, ...] = ()
    binance_api_key_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        exclude=True,
        repr=False,
    )
    binding_effective_at: datetime | None = None
    binding_expires_at: datetime | None = None
    violations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_effective_state(self) -> "LiveWriteGateStatus":
        if (
            self.runtime_real_write_gate == "OPEN"
            and self.configured_runtime_real_write_gate != "OPEN"
        ):
            raise ValueError("LIVE_WRITE_EFFECTIVE_GATE_CONFIGURATION_MISMATCH")
        if self.risk_control_only and (
            self.runtime_real_write_gate != "CLOSED"
            or not self.authorized_activation_ids
        ):
            raise ValueError("LIVE_WRITE_RISK_CONTROL_SCOPE_REQUIRED")
        return self


def _file_grants(settings: HalphaSettings) -> dict[str, int]:
    return {
        SYSTEM_SID: win32file.FILE_ALL_ACCESS,
        BUILTIN_ADMINISTRATORS_SID: win32file.FILE_ALL_ACCESS,
        settings.windows.maintenance_sid: win32file.FILE_ALL_ACCESS,
        settings.windows.app_task_sid: win32file.FILE_GENERIC_READ,
        settings.windows.executor_task_sid: win32file.FILE_GENERIC_READ,
    }


def _assert_live_write_gate_security(
    path: Path,
    settings: HalphaSettings,
    *,
    expected_ace_flags: int,
    subject: str,
) -> None:

    try:
        descriptor = win32security.GetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION
            | win32security.DACL_SECURITY_INFORMATION,
        )
        owner = str(
            win32security.ConvertSidToStringSid(
                descriptor.GetSecurityDescriptorOwner()
            )
        )
        dacl = descriptor.GetSecurityDescriptorDacl()
        control, _revision = descriptor.GetSecurityDescriptorControl()
    except pywintypes.error as exc:
        raise LiveWriteGateError(
            f"LIVE_WRITE_GATE_SECURITY_READ_FAILED code={exc.winerror}"
        ) from None
    except Exception as exc:
        raise LiveWriteGateError(
            f"LIVE_WRITE_GATE_SECURITY_READ_FAILED type={type(exc).__name__}"
        ) from None
    if owner != settings.windows.maintenance_sid:
        raise LiveWriteGateError(f"LIVE_WRITE_GATE_{subject}_OWNER_MISMATCH")
    if not control & win32security.SE_DACL_PROTECTED:
        raise LiveWriteGateError(f"LIVE_WRITE_GATE_{subject}_DACL_NOT_PROTECTED")
    expected = _file_grants(settings)
    if dacl is None or dacl.GetAceCount() != len(expected):
        raise LiveWriteGateError(f"LIVE_WRITE_GATE_{subject}_DACL_COUNT_MISMATCH")
    actual: dict[str, int] = {}
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        if ace[0][0] != win32security.ACCESS_ALLOWED_ACE_TYPE:
            raise LiveWriteGateError(
                f"LIVE_WRITE_GATE_{subject}_DACL_ACE_TYPE_MISMATCH"
            )
        if int(ace[0][1]) != expected_ace_flags:
            raise LiveWriteGateError(
                f"LIVE_WRITE_GATE_{subject}_DACL_ACE_FLAGS_MISMATCH"
            )
        sid = str(win32security.ConvertSidToStringSid(ace[2]))
        if sid in actual:
            raise LiveWriteGateError(
                f"LIVE_WRITE_GATE_{subject}_DACL_DUPLICATE_IDENTITY"
            )
        actual[sid] = int(ace[1])
    if actual != expected:
        raise LiveWriteGateError(f"LIVE_WRITE_GATE_{subject}_DACL_GRANTS_MISMATCH")


def assert_live_write_gate_security(path: Path, settings: HalphaSettings) -> None:
    """Require an owner-controlled, protected, exact read-only file DACL."""

    _assert_live_write_gate_security(
        path,
        settings,
        expected_ace_flags=0,
        subject="FILE",
    )


def assert_live_write_gate_directory_security(
    path: Path,
    settings: HalphaSettings,
) -> None:
    """Prevent a broader parent ACL from replacing an otherwise protected file."""

    _assert_live_write_gate_security(
        path,
        settings,
        expected_ace_flags=(
            win32security.OBJECT_INHERIT_ACE
            | win32security.CONTAINER_INHERIT_ACE
        ),
        subject="DIRECTORY",
    )


def require_live_write_gate_binding_provisionable(
    repo_root: Path,
    settings: HalphaSettings,
    binding: LiveWriteGateBinding,
    *,
    now: datetime | None = None,
) -> str:
    """Reject an invalid binding before the maintenance command changes disk."""

    if settings.release.profile != "BINANCE_LIVE_WRITE":
        raise LiveWriteGateError("LIVE_WRITE_GATE_PROFILE_REQUIRED")
    if (
        binding.environment_id != settings.release.environment_id
        or binding.account_id != settings.release.account_id
        or binding.venue_account_type != settings.release.venue_account_type
    ):
        raise LiveWriteGateError("LIVE_WRITE_GATE_BINDING_SCOPE_MISMATCH")
    observed_at = now or datetime.now(UTC)
    if not (binding.effective_at <= observed_at < binding.expires_at):
        raise LiveWriteGateError("LIVE_WRITE_GATE_BINDING_NOT_CURRENT")
    try:
        current_product_build_id = calculate_product_build_id(repo_root, settings)
    except SourceIdentityError as exc:
        raise LiveWriteGateError(
            f"LIVE_WRITE_GATE_PRODUCT_BUILD_UNAVAILABLE reason={exc}"
        ) from None
    if binding.product_build_id != current_product_build_id:
        raise LiveWriteGateError("LIVE_WRITE_GATE_PRODUCT_BUILD_MISMATCH")
    return current_product_build_id


def _read_binding(
    settings: HalphaSettings,
    repo_root: Path,
) -> tuple[LiveWriteGateBinding | None, list[str]]:
    raw_path = settings.release.live_write_gate_path
    if raw_path is None:
        return None, ["LIVE_WRITE_GATE_PATH_MISSING"]
    path = Path(raw_path)
    try:
        if path.resolve().is_relative_to(repo_root.resolve()):
            return None, ["LIVE_WRITE_GATE_PATH_INSIDE_REPOSITORY"]
    except OSError:
        return None, ["LIVE_WRITE_GATE_PATH_INVALID"]
    if path.is_symlink():
        return None, ["LIVE_WRITE_GATE_SYMLINK_FORBIDDEN"]
    if not path.is_file():
        return None, ["LIVE_WRITE_GATE_BINDING_MISSING"]
    try:
        assert_live_write_gate_directory_security(path.parent, settings)
        assert_live_write_gate_security(path, settings)
    except LiveWriteGateError as exc:
        return None, [str(exc)]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("binding root")
        return LiveWriteGateBinding.model_validate(payload), []
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, TypeError) as exc:
        return None, [f"LIVE_WRITE_GATE_BINDING_INVALID_{type(exc).__name__.upper()}"]


def _database_assessment(
    connection: Any,
    settings: HalphaSettings,
) -> tuple[list[str], tuple[str, ...]]:
    if not _live_activation_safety_index_ready(connection):
        return ["LIVE_WRITE_ACTIVATION_SAFETY_INDEX_UNAVAILABLE"], ()
    rows = connection.execute(
        """
        SELECT activation.activation_id
        FROM halpha.plan_activation AS activation
        WHERE activation.environment_id = %s
          AND activation.environment_kind = 'LIVE'
          AND activation.authority_class = 'LIVE_REAL_CAPITAL'
          AND activation.account_ref = %s
          AND activation.lifecycle IN ('RUNNING', 'EXITING')
          AND activation.responsibility_owner = 'HALPHA'
        ORDER BY activation.created_at, activation.activation_id
        """,
        (settings.release.environment_id, settings.release.account_id),
    ).fetchall()
    return [], tuple(str(row[0]) for row in rows)


def _live_activation_safety_index_ready(connection: Any) -> bool:
    safety_index = connection.execute(
        """
        SELECT index.indisunique, index.indisvalid, index.indisready,
               ARRAY(
                   SELECT attribute.attname
                   FROM unnest(index.indkey) WITH ORDINALITY
                        AS key_column(attnum, ordinal)
                   JOIN pg_catalog.pg_attribute AS attribute
                     ON attribute.attrelid = index.indrelid
                    AND attribute.attnum = key_column.attnum
                   ORDER BY key_column.ordinal
               ),
               pg_get_expr(index.indpred, index.indrelid, true)
        FROM pg_catalog.pg_index AS index
        WHERE index.indexrelid = to_regclass(%s)
        """,
        (f"halpha.{LIVE_ACTIVATION_SAFETY_INDEX}",),
    ).fetchone()
    return live_activation_safety_index_qualified(safety_index)


def require_live_activation_safety_index(connection: Any) -> None:
    """Forbid a new Live activation without its bounded lookup index."""

    try:
        ready = _live_activation_safety_index_ready(connection)
    except Exception:
        raise LiveWriteGateError(
            "LIVE_WRITE_ACTIVATION_SAFETY_INDEX_UNAVAILABLE"
        ) from None
    if not ready:
        raise LiveWriteGateError("LIVE_WRITE_ACTIVATION_SAFETY_INDEX_UNAVAILABLE")


def _database_recovery_assessment(
    connection: Any,
    settings: HalphaSettings,
) -> tuple[list[str], tuple[str, ...]]:
    """Bind recovery to current activations without requiring new-risk guards."""

    violations: list[str] = []
    if not _live_activation_safety_index_ready(connection):
        violations.append("LIVE_WRITE_ACTIVATION_SAFETY_INDEX_UNAVAILABLE")
    rows = connection.execute(
        """
        SELECT activation.activation_id
        FROM halpha.plan_activation AS activation
        WHERE activation.environment_id = %s
          AND activation.environment_kind = 'LIVE'
          AND activation.authority_class = 'LIVE_REAL_CAPITAL'
          AND activation.account_ref = %s
          AND activation.lifecycle IN ('RUNNING', 'EXITING')
          AND activation.responsibility_owner = 'HALPHA'
        ORDER BY activation.created_at, activation.activation_id
        """,
        (settings.release.environment_id, settings.release.account_id),
    ).fetchall()
    if not rows:
        return [*violations, "LIVE_WRITE_CURRENT_ACTIVATION_MISSING"], ()
    return violations, tuple(str(row[0]) for row in rows)


def closed_live_write_gate_status(
    product_build_id: str | None = None,
) -> LiveWriteGateStatus:
    """Return the profile-neutral closed state used by non-LIVE_WRITE callers."""

    return LiveWriteGateStatus(
        configured_runtime_real_write_gate="CLOSED",
        runtime_real_write_gate="CLOSED",
        product_build_id=product_build_id,
    )


def evaluate_live_write_gate(
    repo_root: Path,
    settings: HalphaSettings,
    *,
    current_product_build_id: str | None = None,
    connection: Any | None = None,
    now: datetime | None = None,
) -> LiveWriteGateStatus:
    """Return the effective gate; any missing or conflicting input closes it."""

    if current_product_build_id is not None:
        product_build_id = current_product_build_id
        product_build_violations: list[str] = []
    else:
        try:
            product_build_id = calculate_product_build_id(repo_root, settings)
            product_build_violations = []
        except SourceIdentityError as exc:
            product_build_id = None
            product_build_violations = [
                f"LIVE_WRITE_PRODUCT_BUILD_UNAVAILABLE_{type(exc).__name__.upper()}"
            ]

    if settings.release.profile != "BINANCE_LIVE_WRITE":
        return LiveWriteGateStatus(
            configured_runtime_real_write_gate="CLOSED",
            runtime_real_write_gate="CLOSED",
            product_build_id=product_build_id,
            violations=tuple(product_build_violations),
        )

    observed_at = now or datetime.now(UTC)
    violations = list(product_build_violations)
    binding, binding_violations = _read_binding(settings, repo_root)
    violations.extend(binding_violations)
    if binding is None:
        return LiveWriteGateStatus(
            configured_runtime_real_write_gate="CLOSED",
            runtime_real_write_gate="CLOSED",
            product_build_id=product_build_id,
            violations=tuple(sorted(set(violations))),
        )

    if binding.environment_id != settings.release.environment_id:
        violations.append("LIVE_WRITE_GATE_ENVIRONMENT_MISMATCH")
    if binding.account_id != settings.release.account_id:
        violations.append("LIVE_WRITE_GATE_ACCOUNT_MISMATCH")
    if binding.venue_account_type != settings.release.venue_account_type:
        violations.append("LIVE_WRITE_GATE_ACCOUNT_TYPE_MISMATCH")
    product_build_consistent = (
        product_build_id is not None and binding.product_build_id == product_build_id
    )
    if not product_build_consistent:
        violations.append("LIVE_WRITE_GATE_PRODUCT_BUILD_MISMATCH")
    if not (binding.effective_at <= observed_at < binding.expires_at):
        violations.append("LIVE_WRITE_GATE_BINDING_EXPIRED_OR_NOT_EFFECTIVE")

    configured_gate = binding.runtime_real_write_gate if not violations else "CLOSED"
    authorized_activation_ids: tuple[str, ...] = ()
    if configured_gate == "OPEN":
        if connection is None:
            violations.append("LIVE_WRITE_DATABASE_BINDING_NOT_VERIFIED")
        else:
            try:
                database_violations, authorized_activation_ids = _database_assessment(
                    connection,
                    settings,
                )
                violations.extend(database_violations)
            except Exception as exc:
                violations.append(f"LIVE_WRITE_DATABASE_BINDING_UNAVAILABLE_{type(exc).__name__.upper()}")
    effective_gate = "OPEN" if configured_gate == "OPEN" and not violations else "CLOSED"
    return LiveWriteGateStatus(
        configured_runtime_real_write_gate=configured_gate,
        runtime_real_write_gate=effective_gate,
        product_build_id=product_build_id,
        product_build_consistent=product_build_consistent,
        authorized_activation_ids=(
            authorized_activation_ids if effective_gate == "OPEN" else ()
        ),
        binance_api_key_sha256=binding.binance_api_key_sha256,
        binding_effective_at=binding.effective_at,
        binding_expires_at=binding.expires_at,
        violations=tuple(sorted(set(violations))),
    )


def require_live_write_gate_precheck(
    repo_root: Path,
    settings: HalphaSettings,
    *,
    current_product_build_id: str | None = None,
    now: datetime | None = None,
) -> LiveWriteGateStatus:
    status = evaluate_live_write_gate(
        repo_root,
        settings,
        current_product_build_id=current_product_build_id,
        now=now,
    )
    expected_only = {"LIVE_WRITE_DATABASE_BINDING_NOT_VERIFIED"}
    if (
        status.configured_runtime_real_write_gate != "OPEN"
        or set(status.violations) != expected_only
    ):
        raise LiveWriteGateError(
            "LIVE_WRITE_GATE_PRECHECK_REJECTED reasons=" + ",".join(status.violations or ("GATE_CLOSED",))
        )
    return status


def _risk_control_recovery_candidate(
    status: LiveWriteGateStatus,
    *,
    observed_at: datetime,
) -> bool:
    """Admit only trusted binding defects that cannot authorize new risk."""

    if (
        status.product_build_id is None
        or status.binance_api_key_sha256 is None
        or status.binding_effective_at is None
        or status.binding_expires_at is None
    ):
        return False
    violations = set(status.violations)
    if not violations:
        return status.configured_runtime_real_write_gate == "CLOSED"
    allowed = {
        "LIVE_WRITE_GATE_BINDING_EXPIRED_OR_NOT_EFFECTIVE",
        "LIVE_WRITE_GATE_PRODUCT_BUILD_MISMATCH",
        "LIVE_WRITE_ACTIVATION_SAFETY_INDEX_UNAVAILABLE",
    }
    if not violations <= allowed:
        return False
    if (
        "LIVE_WRITE_GATE_BINDING_EXPIRED_OR_NOT_EFFECTIVE" in violations
        and observed_at < status.binding_expires_at
    ):
        return False
    return True


def require_live_write_gate_startup_precheck(
    repo_root: Path,
    settings: HalphaSettings,
    *,
    current_product_build_id: str | None = None,
    now: datetime | None = None,
) -> LiveWriteGateStatus:
    """Admit full Live startup or a narrowly bound risk-control recovery."""

    observed_at = now or datetime.now(UTC)
    status = evaluate_live_write_gate(
        repo_root,
        settings,
        current_product_build_id=current_product_build_id,
        now=observed_at,
    )
    if (
        status.configured_runtime_real_write_gate == "OPEN"
        and set(status.violations)
        == {"LIVE_WRITE_DATABASE_BINDING_NOT_VERIFIED"}
    ):
        return status
    if _risk_control_recovery_candidate(status, observed_at=observed_at):
        return status
    raise LiveWriteGateError(
        "LIVE_WRITE_GATE_STARTUP_PRECHECK_REJECTED reasons="
        + ",".join(status.violations or ("GATE_CLOSED_UNBOUND",))
    )


def require_live_write_gate_startup(
    repo_root: Path,
    settings: HalphaSettings,
    connection: Any,
    *,
    current_product_build_id: str | None = None,
    now: datetime | None = None,
) -> LiveWriteGateStatus:
    """Return full-write state, or CLOSED state bound only for recovery duties."""

    observed_at = now or datetime.now(UTC)
    status = evaluate_live_write_gate(
        repo_root,
        settings,
        current_product_build_id=current_product_build_id,
        connection=connection,
        now=observed_at,
    )
    if status.runtime_real_write_gate == "OPEN":
        return status
    if not _risk_control_recovery_candidate(status, observed_at=observed_at):
        raise LiveWriteGateError(
            "LIVE_WRITE_GATE_STARTUP_REJECTED reasons="
            + ",".join(status.violations or ("GATE_CLOSED_UNBOUND",))
        )
    product_build_id = status.product_build_id
    if product_build_id is None:
        raise LiveWriteGateError("LIVE_WRITE_GATE_STARTUP_REJECTED reasons=BUILD_UNKNOWN")
    try:
        database_violations, authorized_activation_ids = _database_recovery_assessment(
            connection,
            settings,
        )
    except Exception as exc:
        raise LiveWriteGateError(
            "LIVE_WRITE_GATE_STARTUP_REJECTED reasons="
            f"LIVE_WRITE_DATABASE_BINDING_UNAVAILABLE_{type(exc).__name__.upper()}"
        ) from None
    fatal_database_violations = set(database_violations) - {
        "LIVE_WRITE_ACTIVATION_SAFETY_INDEX_UNAVAILABLE",
    }
    if fatal_database_violations or not authorized_activation_ids:
        raise LiveWriteGateError(
            "LIVE_WRITE_GATE_STARTUP_REJECTED reasons="
            + ",".join(
                sorted(fatal_database_violations)
                or ("ACTIVATION_UNKNOWN",)
            )
        )
    return status.model_copy(
        update={
            "authorized_activation_ids": authorized_activation_ids,
            "risk_control_only": True,
            "violations": tuple(
                sorted(
                    set(status.violations)
                    | set(database_violations)
                    | {"LIVE_WRITE_RISK_CONTROL_ONLY"}
                )
            ),
        }
    )


def require_live_write_gate_open(
    repo_root: Path,
    settings: HalphaSettings,
    connection: Any,
    *,
    current_product_build_id: str | None = None,
    now: datetime | None = None,
) -> LiveWriteGateStatus:
    status = evaluate_live_write_gate(
        repo_root,
        settings,
        current_product_build_id=current_product_build_id,
        connection=connection,
        now=now,
    )
    if status.runtime_real_write_gate != "OPEN":
        raise LiveWriteGateError(
            "LIVE_WRITE_GATE_CLOSED reasons=" + ",".join(status.violations or ("GATE_CLOSED",))
        )
    return status


def require_live_write_credential_binding(
    status: LiveWriteGateStatus,
    api_key: SecretStr,
) -> None:
    """Bind an effective Live gate to the exact private credential in use."""

    expected = status.binance_api_key_sha256
    if (
        status.runtime_real_write_gate != "OPEN"
        and not status.risk_control_only
    ) or expected is None:
        raise LiveWriteGateError("LIVE_WRITE_CREDENTIAL_BINDING_UNAVAILABLE")
    observed = hashlib.sha256(
        api_key.get_secret_value().encode("utf-8")
    ).hexdigest()
    if not hmac.compare_digest(observed, expected):
        raise LiveWriteGateError("LIVE_WRITE_CREDENTIAL_BINDING_MISMATCH")


GateStatusProvider = Callable[[], LiveWriteGateStatus]
