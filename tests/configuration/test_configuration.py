from __future__ import annotations

from pathlib import Path

import pytest

from halpha.capital.models import NewRiskDisciplinePolicy
from halpha.configuration import (
    ConfigurationError,
    app_settings,
    backup_settings,
    executor_settings,
    forward_observation_directory,
    known_live_credential_references,
    load_settings,
    maintenance_settings,
    runtime_log_directory,
    settings_digest,
)


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "halpha.example.toml"
LIVE_READ_ONLY = ROOT / "config" / "halpha.live-copy-read-only.example.toml"
LIVE_WRITE = ROOT / "config" / "halpha.live-copy-write.example.toml"
PERSONAL_READ_ONLY = ROOT / "config" / "halpha.live-personal-read-only.example.toml"
PERSONAL_WRITE = ROOT / "config" / "halpha.live-personal-write.example.toml"


def test_explicit_toml_loads_with_stable_digest() -> None:
    first = load_settings(EXAMPLE)
    second = load_settings(EXAMPLE)
    assert first.release.profile == "BINANCE_DEMO"
    assert first.release.authority_class == "DEMO_VALIDATION"
    assert settings_digest(first) == settings_digest(second)


def test_environment_variables_are_not_a_settings_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HALPHA_RELEASE__PROFILE", "BINANCE_LIVE_WRITE")
    settings = load_settings(EXAMPLE)
    assert settings.release.profile == "BINANCE_DEMO"


def test_process_views_do_not_cross_secret_reference_boundaries() -> None:
    settings = load_settings(EXAMPLE)
    app_view = app_settings(settings).model_dump(mode="json")
    executor_view = executor_settings(settings).model_dump(mode="json")
    backup_view = backup_settings(settings)
    maintenance_view = maintenance_settings(settings)
    assert "binance_api_key_reference" not in app_view["app"]
    assert "binance_api_secret_reference" not in app_view["app"]
    assert "csrf_signing_reference" not in executor_view["executor"]
    assert "smtp_credential_reference" not in executor_view["executor"]
    assert backup_view.backup_task_sid == settings.windows.backup_task_sid
    assert maintenance_view.maintenance_sid == settings.windows.maintenance_sid
    assert backup_view.backup_task_sid != maintenance_view.maintenance_sid
    assert len(
        {
            settings.windows.app_task_sid,
            settings.windows.executor_task_sid,
            settings.windows.backup_task_sid,
            settings.windows.maintenance_sid,
        }
    ) == 4


def test_runtime_proxy_is_an_optional_executor_only_reference() -> None:
    settings = load_settings(EXAMPLE)
    assert settings.executor.runtime_proxy_reference is None
    executor = settings.executor.model_dump(mode="json")
    executor["runtime_proxy_reference"] = {
        "service": "Halpha/Network/BINANCE_DEMO",
        "account": "runtime_proxy",
    }
    configured = load_settings(EXAMPLE, constructor_values={"executor": executor})
    assert configured.executor.runtime_proxy_reference is not None
    assert configured.executor.runtime_proxy_reference.account == "runtime_proxy"


def test_public_market_proxy_is_non_secret_and_loopback_only() -> None:
    settings = load_settings(EXAMPLE)
    app = settings.app.model_dump(mode="json")
    app["public_market_proxy_url"] = "http://127.0.0.1:7897"
    configured = load_settings(EXAMPLE, constructor_values={"app": app})
    assert configured.app.public_market_proxy_url == "http://127.0.0.1:7897"

    for invalid in (
        "https://proxy.example.test:7897",
        "http://synthetic-user:synthetic-secret@127.0.0.1:7897",
        "http://127.0.0.1:7897/path",
    ):
        app["public_market_proxy_url"] = invalid
        with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
            load_settings(EXAMPLE, constructor_values={"app": app})


def test_maintenance_targets_are_fixed_and_paths_are_repository_relative() -> None:
    settings = load_settings(EXAMPLE)
    assert settings.maintenance.demo.database_name == "halpha_demo"
    assert settings.maintenance.live_copy.database_name == "halpha_live_copy"
    assert settings.maintenance.live_personal.database_name == "halpha_live_personal"
    assert settings.maintenance.backup_retention_count == 14
    maintenance = settings.maintenance.model_dump(mode="json")
    maintenance["backup_root"] = "../outside"
    with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
        load_settings(EXAMPLE, constructor_values={"maintenance": maintenance})

    maintenance = settings.maintenance.model_dump(mode="json")
    maintenance["backup_root"] = maintenance["log_root"].upper()
    with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
        load_settings(EXAMPLE, constructor_values={"maintenance": maintenance})


def test_email_delivery_is_disabled_until_complete_nonsecret_route_exists() -> None:
    settings = load_settings(EXAMPLE)
    assert settings.email.delivery_enabled is False
    email = settings.email.model_dump(mode="json")
    email["delivery_enabled"] = True
    with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
        load_settings(EXAMPLE, constructor_values={"email": email})


def test_new_risk_discipline_is_bounded() -> None:
    settings = load_settings(EXAMPLE)
    assert settings.new_risk_discipline.max_plan_loss_fraction == "0.0075"
    assert settings.new_risk_discipline.max_open_risk_fraction == "0.025"
    assert settings.new_risk_discipline.daily_loss_stop_fraction == "0.015"
    assert settings.new_risk_discipline.weekly_loss_stop_fraction == "0.04"
    policy = settings.new_risk_discipline.model_dump(mode="json")
    policy["max_plan_loss_fraction"] = "0.01"
    with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
        load_settings(
            EXAMPLE,
            constructor_values={"new_risk_discipline": policy},
        )

@pytest.mark.parametrize(
    "context",
    (EXAMPLE, LIVE_READ_ONLY, LIVE_WRITE, PERSONAL_READ_ONLY, PERSONAL_WRITE),
)
def test_every_trading_context_rejects_a_wider_portfolio_risk_policy(context: Path) -> None:
    settings = load_settings(context)
    assert settings.new_risk_discipline == NewRiskDisciplinePolicy()

    policy = settings.new_risk_discipline.model_dump(mode="json")
    policy.update(
        max_plan_loss_fraction="0.01",
        max_open_risk_fraction="0.03",
        max_gross_exposure_fraction="3",
        max_instrument_exposure_fraction="1.5",
        max_correlated_exposure_fraction="2",
        daily_loss_stop_fraction="0.02",
        weekly_loss_stop_fraction="0.05",
        rolling_drawdown_stop_fraction="0.12",
    )
    with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
        load_settings(
            context,
            constructor_values={"new_risk_discipline": policy},
        )

    shortened_window = settings.new_risk_discipline.model_dump(mode="json")
    shortened_window["rolling_drawdown_lookback_days"] = 29
    with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
        load_settings(
            context,
            constructor_values={"new_risk_discipline": shortened_window},
        )

    longer_window = settings.new_risk_discipline.model_dump(mode="json")
    longer_window["rolling_drawdown_lookback_days"] = 31
    assert (
        load_settings(
            context,
            constructor_values={"new_risk_discipline": longer_window},
        ).new_risk_discipline.rolling_drawdown_lookback_days
        == 31
    )


def test_demo_rejects_a_live_profit_qualification_reference() -> None:
    with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
        load_settings(
            EXAMPLE,
            constructor_values={
                "live_profit_qualifications": [
                    {
                        "artifact_path": "C:/HalphaRuntime/evidence/playbook.json",
                        "expected_content_digest": "a" * 64,
                    }
                ]
            },
        )


def test_live_profile_cannot_reuse_demo_credential_reference() -> None:
    settings = load_settings(EXAMPLE)
    release = settings.release.model_dump(mode="json")
    release.update(
        profile="BINANCE_LIVE_WRITE",
        authority_class="LIVE_REAL_CAPITAL",
        database_name="halpha_live_copy",
        live_write_gate_path="C:/HalphaRuntime/live-copy-write-gate.json",
    )
    with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
        load_settings(EXAMPLE, constructor_values={"release": release})


def test_live_write_profile_requires_one_detached_absolute_gate_binding() -> None:
    settings = load_settings(LIVE_WRITE)
    assert settings.release.profile == "BINANCE_LIVE_WRITE"
    assert settings.release.live_write_gate_path == (
        "C:/HalphaRuntime/live-copy-write-gate.json"
    )
    assert settings.executor.binance_api_key_reference is not None
    assert "BINANCE_DEMO" not in settings.executor.binance_api_key_reference.service

    release = settings.release.model_dump(mode="json")
    release["live_write_gate_path"] = "build/live-copy-write-gate.json"
    with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
        load_settings(LIVE_WRITE, constructor_values={"release": release})


def test_non_live_profile_forbids_a_live_write_gate_binding() -> None:
    settings = load_settings(EXAMPLE)
    release = settings.release.model_dump(mode="json")
    release["live_write_gate_path"] = (
        "C:/HalphaRuntime/live-write-gate.json"
    )
    with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
        load_settings(EXAMPLE, constructor_values={"release": release})


def test_live_read_only_accepts_only_an_exact_optional_private_read_pair() -> None:
    settings = load_settings(EXAMPLE)
    release = settings.release.model_dump(mode="json")
    release.update(
        profile="BINANCE_LIVE_READ_ONLY",
        authority_class="NO_TRADING_AUTHORITY",
        database_name="halpha_live_copy",
    )
    with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
        load_settings(EXAMPLE, constructor_values={"release": release})

    read_only = load_settings(LIVE_READ_ONLY)
    assert read_only.executor.binance_api_key_reference is not None
    assert read_only.executor.binance_api_secret_reference is not None
    assert read_only.executor.binance_api_key_reference.service == (
        "Halpha/Binance/BINANCE_LIVE_COPY"
    )
    assert read_only.release.authority_class == "NO_TRADING_AUTHORITY"

    executor = read_only.executor.model_dump(mode="json")
    executor["binance_api_secret_reference"] = None
    with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
        load_settings(LIVE_READ_ONLY, constructor_values={"executor": executor})

    executor = read_only.executor.model_dump(mode="json")
    executor["binance_api_key_reference"]["service"] = (
        "Halpha/Binance/BINANCE_LIVE_PERSONAL"
    )
    executor["binance_api_secret_reference"]["service"] = (
        "Halpha/Binance/BINANCE_LIVE_PERSONAL"
    )
    with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
        load_settings(LIVE_READ_ONLY, constructor_values={"executor": executor})


def test_continuous_account_observation_is_explicit_and_read_only() -> None:
    read_only = load_settings(LIVE_READ_ONLY)
    executor = read_only.executor.model_dump(mode="json")
    executor["continuous_account_observation"] = True

    continuous = load_settings(
        LIVE_READ_ONLY,
        constructor_values={"executor": executor},
    )

    assert continuous.executor.continuous_account_observation is True

    executor["binance_api_key_reference"] = None
    executor["binance_api_secret_reference"] = None
    with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
        load_settings(
            LIVE_READ_ONLY,
            constructor_values={"executor": executor},
        )

    demo = load_settings(EXAMPLE)
    demo_executor = demo.executor.model_dump(mode="json")
    demo_executor["continuous_account_observation"] = True
    with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
        load_settings(
            EXAMPLE,
            constructor_values={"executor": demo_executor},
        )

def test_app_database_role_and_credential_are_exact_for_each_profile() -> None:
    demo = load_settings(EXAMPLE)
    read_only = load_settings(LIVE_READ_ONLY)
    live_write = load_settings(LIVE_WRITE)
    personal_read_only = load_settings(PERSONAL_READ_ONLY)
    personal_write = load_settings(PERSONAL_WRITE)

    assert demo.app.database_role_name == "halpha_demo_app"
    assert demo.app.database_credential_reference.service.endswith(
        "/BINANCE_DEMO/App"
    )
    assert read_only.app.database_role_name == "halpha_live_copy_app_reader"
    assert read_only.app.database_credential_reference.service.endswith(
        "/BINANCE_LIVE_COPY/AppReader"
    )
    assert live_write.app.database_role_name == "halpha_live_copy_app"
    assert live_write.app.database_credential_reference.service.endswith(
        "/BINANCE_LIVE_COPY/App"
    )
    assert personal_read_only.app.database_role_name == (
        "halpha_live_personal_app_reader"
    )
    assert personal_write.app.database_role_name == "halpha_live_personal_app"

    app = read_only.app.model_dump(mode="json")
    app["database_role_name"] = "halpha_live_copy_app"
    with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
        load_settings(LIVE_READ_ONLY, constructor_values={"app": app})


def test_three_trading_context_targets_are_atomic_and_exact() -> None:
    expected = {
        "USDM_DEMO": ("binance-demo-primary", "binance-usdm-demo-owner-primary", 8765),
        "USDM_COPY_LEAD": (
            "binance-live-copy-primary",
            "binance-usdm-copy-lead-primary",
            8766,
        ),
        "USDM_PERSONAL": (
            "binance-live-personal-primary",
            "binance-usdm-personal-primary",
            8767,
        ),
    }
    for config_path in (EXAMPLE, LIVE_READ_ONLY, PERSONAL_READ_ONLY):
        settings = load_settings(config_path)
        assert {
            target.venue_account_type.value: (
                target.environment_id,
                target.account_id,
                int(target.url.rsplit(":", 1)[1]),
            )
            for target in settings.app.trading_contexts
        } == expected

    settings = load_settings(EXAMPLE)
    app = settings.app.model_dump(mode="json")
    app["trading_contexts"][2]["url"] = "http://127.0.0.1:9999"
    with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
        load_settings(EXAMPLE, constructor_values={"app": app})

    release = settings.release.model_dump(mode="json")
    release["venue_account_type"] = "USDM_COPY_LEAD"
    with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
        load_settings(EXAMPLE, constructor_values={"release": release})


def test_known_live_credential_universe_covers_both_app_profiles_and_executor_write() -> None:
    references = known_live_credential_references()
    identities = {(reference.service, reference.account) for reference in references}

    assert len(identities) == len(references)
    for namespace in ("BINANCE_LIVE_COPY", "BINANCE_LIVE_PERSONAL"):
        assert (
            f"Halpha/PostgreSQL/{namespace}/App",
            "scram_password",
        ) in identities
        assert (
            f"Halpha/PostgreSQL/{namespace}/AppReader",
            "scram_password",
        ) in identities
        assert (
            f"Halpha/PostgreSQL/{namespace}/Executor",
            "scram_password",
        ) in identities
        assert (f"Halpha/Binance/{namespace}", "api_key") in identities
        assert (f"Halpha/Binance/{namespace}", "api_secret") in identities


def test_app_and_executor_credential_references_must_not_overlap() -> None:
    settings = load_settings(EXAMPLE)
    executor = settings.executor.model_dump(mode="json")
    executor["database_credential_reference"] = settings.app.database_credential_reference.model_dump(
        mode="json"
    )
    with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
        load_settings(EXAMPLE, constructor_values={"executor": executor})


def test_secret_value_key_is_rejected_without_echoing_value(tmp_path: Path) -> None:
    secret = "must-never-appear-in-error"
    path = tmp_path / "bad.toml"
    path.write_text(EXAMPLE.read_text(encoding="utf-8") + f'\napi_secret = "{secret}"\n', encoding="utf-8")
    with pytest.raises(ConfigurationError) as captured:
        load_settings(path)
    assert "SECRET_VALUE_KEY_FORBIDDEN" in str(captured.value)
    assert secret not in str(captured.value)


def test_same_windows_sid_for_multiple_roles_is_rejected() -> None:
    settings = load_settings(EXAMPLE)
    windows = settings.windows.model_dump(mode="json")
    windows["executor_task_sid"] = windows["app_task_sid"]
    with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
        load_settings(EXAMPLE, constructor_values={"windows": windows})


def test_backup_task_sid_cannot_reuse_app_task_sid() -> None:
    settings = load_settings(EXAMPLE)
    windows = settings.windows.model_dump(mode="json")
    windows["backup_task_sid"] = windows["app_task_sid"]
    with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
        load_settings(EXAMPLE, constructor_values={"windows": windows})


@pytest.mark.parametrize(
    ("config_path", "section", "reference_path"),
    (
        (EXAMPLE, "app", ("database_credential_reference",)),
        (EXAMPLE, "app", ("csrf_signing_reference",)),
        (EXAMPLE, "app", ("smtp_credential_reference",)),
        (EXAMPLE, "executor", ("database_credential_reference",)),
        (EXAMPLE, "executor", ("binance_api_key_reference",)),
        (EXAMPLE, "executor", ("binance_api_secret_reference",)),
        (LIVE_READ_ONLY, "app", ("database_credential_reference",)),
        (LIVE_READ_ONLY, "app", ("csrf_signing_reference",)),
        (LIVE_READ_ONLY, "app", ("smtp_credential_reference",)),
        (
            LIVE_READ_ONLY,
            "executor",
            ("database_credential_reference",),
        ),
        (LIVE_READ_ONLY, "executor", ("runtime_proxy_reference",)),
        (LIVE_WRITE, "executor", ("binance_api_key_reference",)),
        (LIVE_WRITE, "executor", ("binance_api_secret_reference",)),
        (
            EXAMPLE,
            "maintenance",
            ("demo", "backup_credential_reference"),
        ),
        (
            EXAMPLE,
            "maintenance",
            ("demo", "migration_credential_reference"),
        ),
        (
            EXAMPLE,
            "maintenance",
            ("live_copy", "backup_credential_reference"),
        ),
        (
            EXAMPLE,
            "maintenance",
            ("live_copy", "migration_credential_reference"),
        ),
        (
            EXAMPLE,
            "maintenance",
            ("live_personal", "backup_credential_reference"),
        ),
        (
            EXAMPLE,
            "maintenance",
            ("live_personal", "migration_credential_reference"),
        ),
    ),
)
@pytest.mark.parametrize(
    ("reference_field", "invalid_value"),
    (
        ("service", "Halpha/Unexpected/ForeignEnvironment"),
        ("account", "wrong_account"),
    ),
)
def test_every_runtime_credential_reference_field_is_exact(
    config_path: Path,
    section: str,
    reference_path: tuple[str, ...],
    reference_field: str,
    invalid_value: str,
) -> None:
    settings = load_settings(config_path)
    section_values = getattr(settings, section).model_dump(mode="json")
    reference: dict[str, object] = section_values
    for part in reference_path:
        reference = reference[part]  # type: ignore[assignment]
    reference[reference_field] = invalid_value

    with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
        load_settings(
            config_path,
            constructor_values={section: section_values},
        )


def test_demo_and_live_runtime_primitives_and_logs_are_isolated(
    tmp_path: Path,
) -> None:
    demo = load_settings(EXAMPLE)
    live_read_only = load_settings(LIVE_READ_ONLY)
    live_write = load_settings(LIVE_WRITE)
    personal_read_only = load_settings(PERSONAL_READ_ONLY)

    assert demo.executor.mutex_name != live_read_only.executor.mutex_name
    assert personal_read_only.executor.mutex_name != live_read_only.executor.mutex_name
    assert demo.windows.app_stop_event != live_read_only.windows.app_stop_event
    assert personal_read_only.windows.app_stop_event != live_read_only.windows.app_stop_event
    assert (
        demo.windows.executor_stop_event
        != live_read_only.windows.executor_stop_event
    )

    demo_log = runtime_log_directory(tmp_path, demo, role="executor")
    live_log = runtime_log_directory(
        tmp_path,
        live_read_only,
        role="executor",
    )
    assert demo_log != live_log
    assert live_log != runtime_log_directory(
        tmp_path, personal_read_only, role="executor"
    )
    assert demo_log.parent.name == demo.release.environment_id
    assert live_log.parent.name == live_read_only.release.environment_id
    assert runtime_log_directory(tmp_path, demo, role="app") != demo_log
    assert forward_observation_directory(
        tmp_path,
        live_read_only,
    ).is_relative_to(live_log)
    assert not forward_observation_directory(
        tmp_path,
        live_read_only,
    ).is_relative_to(tmp_path / "build")

    assert live_read_only.release.environment_id == live_write.release.environment_id
    assert live_read_only.release.account_id == live_write.release.account_id
    assert live_read_only.release.database_name == live_write.release.database_name
    assert live_read_only.executor.mutex_name == live_write.executor.mutex_name
    assert (
        live_read_only.windows.app_stop_event
        == live_write.windows.app_stop_event
    )
    assert (
        live_read_only.windows.executor_stop_event
        == live_write.windows.executor_stop_event
    )
    assert runtime_log_directory(
        tmp_path,
        live_read_only,
        role="executor",
    ) == (
        runtime_log_directory(
            tmp_path,
            live_write,
            role="executor",
        )
    )


@pytest.mark.parametrize(
    ("section", "field", "foreign_value"),
    (
        (
            "executor",
            "mutex_name",
            r"Global\Halpha.Executor.BINANCE_LIVE_COPY.WriteOwner",
        ),
        (
            "windows",
            "app_stop_event",
            r"Global\Halpha.App.BINANCE_LIVE_COPY.Stop",
        ),
        (
            "windows",
            "executor_stop_event",
            r"Global\Halpha.Executor.BINANCE_LIVE_COPY.Stop",
        ),
    ),
)
def test_demo_rejects_live_runtime_primitive_names(
    section: str,
    field: str,
    foreign_value: str,
) -> None:
    settings = load_settings(EXAMPLE)
    section_values = getattr(settings, section).model_dump(mode="json")
    section_values[field] = foreign_value

    with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
        load_settings(
            EXAMPLE,
            constructor_values={section: section_values},
        )


@pytest.mark.parametrize(
    ("config_path", "section", "field", "invalid_value"),
    (
        (
            EXAMPLE,
            "release",
            "environment_id",
            "binance-live-copy-primary",
        ),
        (
            EXAMPLE,
            "release",
            "account_id",
            "binance-usdm-copy-lead-primary",
        ),
        (EXAMPLE, "app", "port", 9876),
        (
            LIVE_READ_ONLY,
            "release",
            "environment_id",
            "binance-demo-primary",
        ),
        (
            LIVE_READ_ONLY,
            "release",
            "account_id",
            "binance-usdm-demo-owner-primary",
        ),
        (LIVE_READ_ONLY, "app", "port", 9876),
    ),
)
def test_profile_rejects_cross_environment_identity_and_switch_coordinates(
    config_path: Path,
    section: str,
    field: str,
    invalid_value: object,
) -> None:
    settings = load_settings(config_path)
    section_values = getattr(settings, section).model_dump(mode="json")
    section_values[field] = invalid_value

    with pytest.raises(ConfigurationError, match="CONFIGURATION_INVALID"):
        load_settings(
            config_path,
            constructor_values={section: section_values},
        )
