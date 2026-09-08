from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import SecretStr
import win32api
import win32con
import win32security

from halpha.configuration import load_settings
from halpha.live_write_gate import (
    LiveWriteGateBinding,
    LiveWriteGateError,
    assert_live_write_gate_directory_security,
    assert_live_write_gate_security,
    evaluate_live_write_gate,
    require_live_write_credential_binding,
    require_live_write_gate_open,
    require_live_write_gate_precheck,
    require_live_write_gate_startup,
    require_live_write_gate_startup_precheck,
)
from halpha.live_write_gate_cli import (
    _apply_security,
    provision_live_write_gate_binding,
)


ROOT = Path(__file__).resolve().parents[2]
LIVE_CONFIG = ROOT / "config" / "halpha.live-copy-write.example.toml"
PRODUCT_BUILD_ID = "a" * 64
API_KEY = "synthetic-live-api-key"
API_KEY_SHA256 = hashlib.sha256(API_KEY.encode("utf-8")).hexdigest()
NOW = datetime(2026, 7, 18, 12, tzinfo=UTC)


def _settings(tmp_path: Path):
    config = tmp_path / "live.toml"
    content = LIVE_CONFIG.read_text(encoding="utf-8").replace(
        "C:/HalphaRuntime/live-copy-write-gate.json",
        (tmp_path / "live-copy-write-gate.json").as_posix(),
    )
    config.write_text(content, encoding="utf-8")
    return load_settings(config)


def _settings_with_current_acl_owner(tmp_path: Path):
    settings = _settings(tmp_path)
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(),
        win32con.TOKEN_QUERY,
    )
    current_sid = win32security.ConvertSidToStringSid(
        win32security.GetTokenInformation(
            token,
            win32security.TokenUser,
        )[0]
    )
    return settings.model_copy(
        update={
            "windows": settings.windows.model_copy(
                update={
                    "app_task_sid": "S-1-5-32-545",
                    "executor_task_sid": "S-1-5-32-546",
                    "backup_task_sid": "S-1-5-19",
                    "maintenance_sid": current_sid,
                }
            )
        }
    )


def _binding(settings, *, gate: str) -> LiveWriteGateBinding:
    return LiveWriteGateBinding(
        schema_version=5,
        environment_id=settings.release.environment_id,
        account_id=settings.release.account_id,
        venue_account_type=settings.release.venue_account_type,
        profile="BINANCE_LIVE_WRITE",
        runtime_real_write_gate=gate,
        product_build_id=PRODUCT_BUILD_ID,
        binance_api_key_sha256=API_KEY_SHA256,
        effective_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )


def _currently_effective_binding(settings, *, gate: str) -> LiveWriteGateBinding:
    now = datetime.now(UTC)
    return _binding(settings, gate=gate).model_copy(
        update={
            "effective_at": now - timedelta(minutes=1),
            "expires_at": now + timedelta(hours=1),
        }
    )


def _write_binding(settings, binding: LiveWriteGateBinding) -> None:
    path = Path(str(settings.release.live_write_gate_path))
    path.write_text(
        json.dumps(binding.model_dump(mode="json")),
        encoding="utf-8",
    )


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(
        self,
        *,
        activation_ids: tuple[str, ...] = ("activation-live-001",),
        product_build_id: str = PRODUCT_BUILD_ID,
        safety_index_ready: bool = True,
    ):
        self._activation_ids = activation_ids
        self._product_build_id = product_build_id
        self._safety_index_ready = safety_index_ready

    def execute(self, query: str, _parameters):
        if "pg_catalog.pg_index" in query:
            return _Result(
                (
                    False,
                    True,
                    True,
                    ["environment_id", "account_ref"],
                    "environment_kind::text = 'LIVE'::text "
                    "AND lifecycle::text <> 'COMPLETED'::text",
                )
                if self._safety_index_ready
                else None
            )
        assert "plan_activation" in query

        class _Rows:
            def __init__(
                self,
                activation_ids: tuple[str, ...],
                product_build_id: str,
            ):
                self._activation_ids = activation_ids
                self._product_build_id = product_build_id

            def fetchall(self):
                return [
                    (activation_id, self._product_build_id)
                    for activation_id in self._activation_ids
                ]

        return _Rows(self._activation_ids, self._product_build_id)


@pytest.fixture
def current_product_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "halpha.live_write_gate.calculate_product_build_id",
        lambda _repo_root, _settings: PRODUCT_BUILD_ID,
    )
    monkeypatch.setattr(
        "halpha.live_write_gate.assert_live_write_gate_security",
        lambda _path, _settings: None,
    )
    monkeypatch.setattr(
        "halpha.live_write_gate.assert_live_write_gate_directory_security",
        lambda _path, _settings: None,
    )


def test_closed_binding_separates_build_identity_and_runtime_switch(
    tmp_path: Path,
    current_product_build: None,
) -> None:
    settings = _settings(tmp_path)
    _write_binding(settings, _binding(settings, gate="CLOSED"))

    status = evaluate_live_write_gate(ROOT, settings, now=NOW)

    assert status.product_build_id == PRODUCT_BUILD_ID
    assert status.product_build_consistent is True
    assert status.configured_runtime_real_write_gate == "CLOSED"
    assert status.runtime_real_write_gate == "CLOSED"
    assert status.authorized_activation_ids == ()


def test_legacy_authorization_field_is_rejected_fail_closed(
    tmp_path: Path,
    current_product_build: None,
) -> None:
    settings = _settings(tmp_path)
    payload = _binding(settings, gate="CLOSED").model_dump(mode="json")
    payload["user_authorization_ref"] = "legacy-owner-decision"
    Path(str(settings.release.live_write_gate_path)).write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    status = evaluate_live_write_gate(ROOT, settings, now=NOW)

    assert status.product_build_consistent is None
    assert status.configured_runtime_real_write_gate == "CLOSED"
    assert status.runtime_real_write_gate == "CLOSED"
    assert status.violations == (
        "LIVE_WRITE_GATE_BINDING_INVALID_VALIDATIONERROR",
    )


def test_gate_without_a_credential_fingerprint_is_rejected_fail_closed(
    tmp_path: Path,
    current_product_build: None,
) -> None:
    settings = _settings(tmp_path)
    payload = _binding(settings, gate="OPEN").model_dump(mode="json")
    payload["schema_version"] = 3
    payload.pop("binance_api_key_sha256")
    Path(str(settings.release.live_write_gate_path)).write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    status = evaluate_live_write_gate(ROOT, settings, now=NOW)

    assert status.runtime_real_write_gate == "CLOSED"
    assert status.violations == (
        "LIVE_WRITE_GATE_BINDING_INVALID_VALIDATIONERROR",
    )


def test_open_binding_requires_database_verification_before_becoming_effective(
    tmp_path: Path,
    current_product_build: None,
) -> None:
    settings = _settings(tmp_path)
    _write_binding(settings, _binding(settings, gate="OPEN"))

    precheck = require_live_write_gate_precheck(ROOT, settings, now=NOW)
    assert precheck.configured_runtime_real_write_gate == "OPEN"
    assert precheck.runtime_real_write_gate == "CLOSED"
    assert precheck.violations == ("LIVE_WRITE_DATABASE_BINDING_NOT_VERIFIED",)

    effective = require_live_write_gate_open(
        ROOT,
        settings,
        _Connection(),
        now=NOW,
    )
    assert effective.runtime_real_write_gate == "OPEN"
    assert effective.authorized_activation_ids == ("activation-live-001",)
    assert effective.violations == ()
    assert "binance_api_key_sha256" not in effective.model_dump()
    require_live_write_credential_binding(effective, SecretStr(API_KEY))


def test_open_gate_rejects_a_valid_credential_from_another_account(
    tmp_path: Path,
    current_product_build: None,
) -> None:
    settings = _settings(tmp_path)
    _write_binding(settings, _binding(settings, gate="OPEN"))
    effective = require_live_write_gate_open(
        ROOT,
        settings,
        _Connection(),
        now=NOW,
    )

    with pytest.raises(
        LiveWriteGateError,
        match="LIVE_WRITE_CREDENTIAL_BINDING_MISMATCH",
    ):
        require_live_write_credential_binding(
            effective,
            SecretStr("synthetic-key-from-another-account"),
        )


def test_open_gate_requires_the_live_activation_safety_index(
    tmp_path: Path,
    current_product_build: None,
) -> None:
    settings = _settings(tmp_path)
    _write_binding(settings, _binding(settings, gate="OPEN"))

    status = evaluate_live_write_gate(
        ROOT,
        settings,
        connection=_Connection(safety_index_ready=False),
        now=NOW,
    )

    assert status.runtime_real_write_gate == "CLOSED"
    assert status.authorized_activation_ids == ()
    assert status.violations == (
        "LIVE_WRITE_ACTIVATION_SAFETY_INDEX_UNAVAILABLE",
    )


@pytest.mark.parametrize("expired", (False, True))
def test_live_startup_keeps_risk_control_available_after_gate_closes(
    tmp_path: Path,
    current_product_build: None,
    expired: bool,
) -> None:
    settings = _settings(tmp_path)
    binding = _binding(settings, gate="CLOSED")
    if expired:
        binding = binding.model_copy(
            update={
                "effective_at": NOW - timedelta(hours=2),
                "expires_at": NOW - timedelta(minutes=1),
            }
        )
    _write_binding(settings, binding)

    precheck = require_live_write_gate_startup_precheck(
        ROOT,
        settings,
        now=NOW,
    )
    assert precheck.runtime_real_write_gate == "CLOSED"
    effective = require_live_write_gate_startup(
        ROOT,
        settings,
        _Connection(),
        now=NOW,
    )

    assert effective.runtime_real_write_gate == "CLOSED"
    assert effective.risk_control_only is True
    assert effective.authorized_activation_ids == ("activation-live-001",)
    assert "LIVE_WRITE_RISK_CONTROL_ONLY" in effective.violations
    require_live_write_credential_binding(effective, SecretStr(API_KEY))


def test_live_recovery_tolerates_binding_build_drift_only_as_risk_control(
    tmp_path: Path,
    current_product_build: None,
) -> None:
    settings = _settings(tmp_path)
    binding_build = "b" * 64
    _write_binding(
        settings,
        _binding(settings, gate="CLOSED").model_copy(
            update={"product_build_id": binding_build}
        ),
    )

    precheck = require_live_write_gate_startup_precheck(
        ROOT,
        settings,
        now=NOW,
    )
    assert precheck.runtime_real_write_gate == "CLOSED"
    status = require_live_write_gate_startup(
        ROOT,
        settings,
        _Connection(),
        now=NOW,
    )

    assert status.runtime_real_write_gate == "CLOSED"
    assert status.risk_control_only is True
    assert status.authorized_activation_ids == ("activation-live-001",)
    assert "LIVE_WRITE_GATE_PRODUCT_BUILD_MISMATCH" in status.violations
    require_live_write_credential_binding(status, SecretStr(API_KEY))

    _write_binding(
        settings,
        _binding(settings, gate="OPEN").model_copy(
            update={"product_build_id": binding_build}
        ),
    )
    with pytest.raises(LiveWriteGateError, match="LIVE_WRITE_GATE_CLOSED"):
        require_live_write_gate_open(
            ROOT,
            settings,
            _Connection(),
            now=NOW,
        )


def test_current_open_gate_continues_an_older_fixed_plan_snapshot(
    tmp_path: Path,
    current_product_build: None,
) -> None:
    settings = _settings(tmp_path)
    _write_binding(settings, _binding(settings, gate="OPEN"))

    status = require_live_write_gate_open(
        ROOT,
        settings,
        _Connection(product_build_id="b" * 64),
        now=NOW,
    )

    assert status.runtime_real_write_gate == "OPEN"
    assert status.risk_control_only is False
    assert status.authorized_activation_ids == ("activation-live-001",)
    assert status.violations == ()


def test_live_recovery_tolerates_missing_safety_index_but_full_write_does_not(
    tmp_path: Path,
    current_product_build: None,
) -> None:
    settings = _settings(tmp_path)
    _write_binding(settings, _binding(settings, gate="CLOSED"))

    status = require_live_write_gate_startup(
        ROOT,
        settings,
        _Connection(safety_index_ready=False),
        now=NOW,
    )

    assert status.risk_control_only is True
    assert status.authorized_activation_ids == ("activation-live-001",)
    assert "LIVE_WRITE_ACTIVATION_SAFETY_INDEX_UNAVAILABLE" in status.violations

    _write_binding(settings, _binding(settings, gate="OPEN"))
    with pytest.raises(LiveWriteGateError, match="LIVE_WRITE_GATE_CLOSED"):
        require_live_write_gate_open(
            ROOT,
            settings,
            _Connection(safety_index_ready=False),
            now=NOW,
        )


def test_live_recovery_rejects_a_missing_gate_binding(
    tmp_path: Path,
    current_product_build: None,
) -> None:
    settings = _settings(tmp_path)

    with pytest.raises(
        LiveWriteGateError,
        match="LIVE_WRITE_GATE_STARTUP_PRECHECK_REJECTED",
    ):
        require_live_write_gate_startup_precheck(ROOT, settings, now=NOW)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        (
            "environment_id",
            "other-live-environment",
            "LIVE_WRITE_GATE_ENVIRONMENT_MISMATCH",
        ),
        ("account_id", "other-live-account", "LIVE_WRITE_GATE_ACCOUNT_MISMATCH"),
        (
            "venue_account_type",
            "USDM_PERSONAL",
            "LIVE_WRITE_GATE_ACCOUNT_TYPE_MISMATCH",
        ),
    ),
)
def test_live_recovery_rejects_binding_scope_mismatch(
    tmp_path: Path,
    current_product_build: None,
    field: str,
    value: str,
    reason: str,
) -> None:
    settings = _settings(tmp_path)
    _write_binding(
        settings,
        _binding(settings, gate="CLOSED").model_copy(
            update={field: value}
        ),
    )

    with pytest.raises(LiveWriteGateError, match=reason):
        require_live_write_gate_startup_precheck(ROOT, settings, now=NOW)


def test_live_recovery_rejects_a_not_yet_effective_binding(
    tmp_path: Path,
    current_product_build: None,
) -> None:
    settings = _settings(tmp_path)
    binding = _binding(settings, gate="CLOSED").model_copy(
        update={
            "effective_at": NOW + timedelta(minutes=1),
            "expires_at": NOW + timedelta(hours=1),
        }
    )
    _write_binding(settings, binding)

    with pytest.raises(
        LiveWriteGateError,
        match="LIVE_WRITE_GATE_STARTUP_PRECHECK_REJECTED",
    ):
        require_live_write_gate_startup_precheck(ROOT, settings, now=NOW)


def test_live_recovery_requires_at_least_one_current_activation(
    tmp_path: Path,
    current_product_build: None,
) -> None:
    settings = _settings(tmp_path)
    _write_binding(settings, _binding(settings, gate="CLOSED"))

    with pytest.raises(LiveWriteGateError, match="LIVE_WRITE_CURRENT_ACTIVATION_MISSING"):
        require_live_write_gate_startup(
            ROOT,
            settings,
            _Connection(activation_ids=()),
            now=NOW,
        )

    recovered = require_live_write_gate_startup(
        ROOT,
        settings,
        _Connection(
            activation_ids=("activation-live-001", "activation-live-002")
        ),
        now=NOW,
    )
    assert recovered.authorized_activation_ids == (
        "activation-live-001",
        "activation-live-002",
    )


def test_open_gate_can_idle_without_activations_and_binds_new_current_ones(
    tmp_path: Path,
    current_product_build: None,
) -> None:
    settings = _settings(tmp_path)
    _write_binding(settings, _binding(settings, gate="OPEN"))

    idle = require_live_write_gate_open(
        ROOT,
        settings,
        connection=_Connection(activation_ids=()),
        now=NOW,
    )
    assert idle.runtime_real_write_gate == "OPEN"
    assert idle.authorized_activation_ids == ()
    assert idle.violations == ()

    effective = require_live_write_gate_open(
        ROOT,
        settings,
        _Connection(
            activation_ids=("activation-live-001", "activation-live-002")
        ),
        now=NOW,
    )
    assert effective.authorized_activation_ids == (
        "activation-live-001",
        "activation-live-002",
    )


def test_gate_file_requires_the_exact_protected_windows_acl(tmp_path: Path) -> None:
    settings = _settings_with_current_acl_owner(tmp_path)
    path = Path(str(settings.release.live_write_gate_path))
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(
        LiveWriteGateError,
        match="LIVE_WRITE_GATE_FILE_(OWNER|DACL)",
    ):
        assert_live_write_gate_security(path, settings)

    _apply_security(path, settings, directory=False)
    assert_live_write_gate_security(path, settings)
    _apply_security(path.parent, settings, directory=True)
    assert_live_write_gate_directory_security(path.parent, settings)


def test_provisioning_rejects_product_build_mismatch_before_creating_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    target = tmp_path / "not-created" / "live-write-gate.json"
    settings = settings.model_copy(
        update={
            "release": settings.release.model_copy(
                update={"live_write_gate_path": str(target)}
            )
        }
    )
    monkeypatch.setattr(
        "halpha.live_write_gate.calculate_product_build_id",
        lambda _repo_root, _settings: "b" * 64,
    )
    monkeypatch.setattr(
        "halpha.live_write_gate_cli.require_process_identity",
        lambda _sid: None,
    )

    with pytest.raises(LiveWriteGateError, match="LIVE_WRITE_GATE_PRODUCT_BUILD_MISMATCH"):
        provision_live_write_gate_binding(
            ROOT,
            settings,
            _currently_effective_binding(settings, gate="CLOSED"),
        )

    assert not target.parent.exists()
    assert not target.exists()


def test_provisioning_accepts_only_the_exact_closed_postcondition(
    tmp_path: Path,
    current_product_build: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    target = Path(str(settings.release.live_write_gate_path))
    binding = _currently_effective_binding(settings, gate="CLOSED")
    monkeypatch.setattr(
        "halpha.live_write_gate_cli.require_process_identity",
        lambda _sid: None,
    )
    monkeypatch.setattr(
        "halpha.live_write_gate_cli._apply_security",
        lambda _path, _settings, *, directory: None,
    )
    monkeypatch.setattr(
        "halpha.live_write_gate_cli.assert_live_write_gate_security",
        lambda _path, _settings: None,
    )
    monkeypatch.setattr(
        "halpha.live_write_gate_cli.assert_live_write_gate_directory_security",
        lambda _path, _settings: None,
    )
    monkeypatch.setattr(
        "halpha.live_write_gate_cli.acquire_live_gate_maintenance_mutex",
        lambda **_kwargs: nullcontext(),
    )

    report = provision_live_write_gate_binding(ROOT, settings, binding)

    assert report == {
        "status": "PROVISIONED",
        "configured_runtime_real_write_gate": "CLOSED",
        "runtime_real_write_gate": "CLOSED",
        "product_build_id": PRODUCT_BUILD_ID,
        "product_build_consistent": True,
        "violations": [],
    }
    assert LiveWriteGateBinding.model_validate_json(
        target.read_text(encoding="utf-8")
    ) == binding


def test_provisioning_open_remains_effectively_closed_until_database_recheck(
    tmp_path: Path,
    current_product_build: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    binding = _currently_effective_binding(settings, gate="OPEN")
    monkeypatch.setattr(
        "halpha.live_write_gate_cli.require_process_identity",
        lambda _sid: None,
    )
    monkeypatch.setattr(
        "halpha.live_write_gate_cli._apply_security",
        lambda _path, _settings, *, directory: None,
    )
    monkeypatch.setattr(
        "halpha.live_write_gate_cli.assert_live_write_gate_security",
        lambda _path, _settings: None,
    )
    monkeypatch.setattr(
        "halpha.live_write_gate_cli.assert_live_write_gate_directory_security",
        lambda _path, _settings: None,
    )
    monkeypatch.setattr(
        "halpha.live_write_gate_cli.acquire_live_gate_maintenance_mutex",
        lambda **_kwargs: nullcontext(),
    )

    report = provision_live_write_gate_binding(ROOT, settings, binding)

    assert report["configured_runtime_real_write_gate"] == "OPEN"
    assert report["runtime_real_write_gate"] == "CLOSED"
    assert report["violations"] == ["LIVE_WRITE_DATABASE_BINDING_NOT_VERIFIED"]
