"""Explicit TOML-only non-secret runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Literal, Mapping
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict, TomlConfigSettingsSource

from halpha.capital.models import (
    DAILY_LOSS_STOP_FRACTION,
    MAX_CORRELATED_EXPOSURE_FRACTION,
    MAX_GROSS_EXPOSURE_FRACTION,
    MAX_INSTRUMENT_EXPOSURE_FRACTION,
    MAX_OPEN_RISK_FRACTION,
    MAX_PLAN_LOSS_FRACTION,
    MINIMUM_REWARD_RISK_RATIO,
    ROLLING_DRAWDOWN_LOOKBACK_DAYS,
    ROLLING_DRAWDOWN_STOP_FRACTION,
    WEEKLY_LOSS_STOP_FRACTION,
    NewRiskDisciplinePolicy,
)


class ConfigurationError(RuntimeError):
    """A sanitized, fail-closed configuration error."""


_SID_PATTERN = re.compile(r"^S-1-(?:\d+-)+\d+$")
_IDENTITY_PATTERN = r"^[a-z0-9][a-z0-9._-]{2,95}$"
_FORBIDDEN_VALUE_KEYS = {
    "api_key",
    "api_secret",
    "authorization",
    "cookie",
    "credential_value",
    "database_url",
    "dsn",
    "password",
    "password_hash",
    "private_key",
    "secret",
    "session_secret",
    "token",
}


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WinVaultReference(FrozenModel):
    service: str = Field(min_length=3, max_length=160)
    account: str = Field(min_length=2, max_length=96)


class VenueAccountType(StrEnum):
    """Fixed venue-account identity carried by every runtime boundary."""

    USDM_DEMO = "USDM_DEMO"
    USDM_COPY_LEAD = "USDM_COPY_LEAD"
    USDM_PERSONAL = "USDM_PERSONAL"


@dataclass(frozen=True)
class TradingContextSpec:
    environment_id: str
    account_id: str
    database_name: str
    port: int
    namespace: str
    environment_kind: Literal["DEMO", "LIVE"]


TRADING_CONTEXT_SPECS: dict[VenueAccountType, TradingContextSpec] = {
    VenueAccountType.USDM_DEMO: TradingContextSpec(
        environment_id="binance-demo-primary",
        account_id="binance-usdm-demo-owner-primary",
        database_name="halpha_demo",
        port=8765,
        namespace="BINANCE_DEMO",
        environment_kind="DEMO",
    ),
    VenueAccountType.USDM_COPY_LEAD: TradingContextSpec(
        environment_id="binance-live-copy-primary",
        account_id="binance-usdm-copy-lead-primary",
        database_name="halpha_live_copy",
        port=8766,
        namespace="BINANCE_LIVE_COPY",
        environment_kind="LIVE",
    ),
    VenueAccountType.USDM_PERSONAL: TradingContextSpec(
        environment_id="binance-live-personal-primary",
        account_id="binance-usdm-personal-primary",
        database_name="halpha_live_personal",
        port=8767,
        namespace="BINANCE_LIVE_PERSONAL",
        environment_kind="LIVE",
    ),
}


def trading_context_spec(account_type: VenueAccountType) -> TradingContextSpec:
    return TRADING_CONTEXT_SPECS[account_type]


class ReleaseConfig(FrozenModel):
    environment_id: str = Field(pattern=_IDENTITY_PATTERN)
    account_id: str = Field(pattern=_IDENTITY_PATTERN)
    venue_account_type: VenueAccountType
    profile: Literal["BINANCE_DEMO", "BINANCE_LIVE_READ_ONLY", "BINANCE_LIVE_WRITE"]
    authority_class: Literal[
        "DEMO_VALIDATION",
        "LIVE_REAL_CAPITAL",
        "NO_TRADING_AUTHORITY",
    ]
    database_name: str = Field(
        pattern=r"^halpha_(?:demo|live_(?:copy|personal))$"
    )
    live_write_gate_path: str | None = None

    @model_validator(mode="after")
    def validate_live_write_gate_path(self) -> "ReleaseConfig":
        path_value = self.live_write_gate_path
        if self.profile == "BINANCE_LIVE_WRITE":
            if path_value is None:
                raise ValueError("LIVE_WRITE_GATE_PATH_REQUIRED")
            path = Path(path_value)
            if not path.is_absolute() or path.suffix.lower() != ".json" or ".." in path.parts:
                raise ValueError("LIVE_WRITE_GATE_PATH_INVALID")
        elif path_value is not None:
            raise ValueError("LIVE_WRITE_GATE_PATH_PROFILE_MISMATCH")
        return self


class LiveProfitQualificationReference(FrozenModel):
    """One owner-selected local Demo evidence artifact and its content digest."""

    artifact_path: str
    expected_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("artifact_path")
    @classmethod
    def artifact_path_is_explicit_json(cls, value: str) -> str:
        path = Path(value)
        if (
            not path.is_absolute()
            or path.suffix.lower() != ".json"
            or ".." in path.parts
        ):
            raise ValueError("LIVE_PROFIT_QUALIFICATION_PATH_INVALID")
        return value


class TradingContextTarget(FrozenModel):
    venue_account_type: VenueAccountType
    environment_id: str = Field(pattern=_IDENTITY_PATTERN)
    account_id: str = Field(pattern=_IDENTITY_PATTERN)
    url: str

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("TRADING_CONTEXT_URL_MUST_BE_LOOPBACK_HTTP_ORIGIN")
        return value.rstrip("/")


class AppConfig(FrozenModel):
    bind: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    workers: Literal[1] = 1
    reload: Literal[False] = False
    public_market_proxy_url: str | None = None
    trading_contexts: tuple[TradingContextTarget, ...]
    database_role_name: str = Field(
        pattern=(
            r"^halpha_(?:demo_app|live_(?:copy|personal)_(?:app|app_reader))$"
        )
    )
    database_credential_reference: WinVaultReference
    csrf_signing_reference: WinVaultReference
    smtp_credential_reference: WinVaultReference

    @field_validator("public_market_proxy_url")
    @classmethod
    def validate_public_market_proxy_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("PUBLIC_MARKET_PROXY_MUST_BE_NON_SECRET_LOOPBACK_HTTP")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_distinct_trading_contexts(self) -> "AppConfig":
        account_types = tuple(target.venue_account_type for target in self.trading_contexts)
        environment_ids = tuple(target.environment_id for target in self.trading_contexts)
        account_ids = tuple(target.account_id for target in self.trading_contexts)
        origins = tuple(urlsplit(target.url).netloc.casefold() for target in self.trading_contexts)
        if not self.trading_contexts or any(
            len(set(values)) != len(values)
            for values in (account_types, environment_ids, account_ids, origins)
        ):
            raise ValueError("TRADING_CONTEXT_TARGETS_MUST_BE_DISTINCT")
        return self


class ExecutorConfig(FrozenModel):
    database_credential_reference: WinVaultReference
    binance_api_key_reference: WinVaultReference | None = None
    binance_api_secret_reference: WinVaultReference | None = None
    runtime_proxy_reference: WinVaultReference | None = None
    continuous_account_observation: bool = False
    mutex_name: str = Field(
        pattern=(
            r"^Global\\Halpha\.Executor\.BINANCE_(?:DEMO|LIVE_(?:COPY|PERSONAL))"
            r"\.WriteOwner$"
        )
    )


class DatabaseMaintenanceTarget(FrozenModel):
    environment_kind: Literal["DEMO", "LIVE"]
    venue_account_type: VenueAccountType
    database_name: str = Field(
        pattern=r"^halpha_(?:demo|live_(?:copy|personal))$"
    )
    backup_role_name: str = Field(
        pattern=r"^halpha_(?:demo|live_(?:copy|personal))_backup$"
    )
    migration_role_name: str = Field(
        pattern=r"^halpha_(?:demo|live_(?:copy|personal))_migration$"
    )
    backup_credential_reference: WinVaultReference
    migration_credential_reference: WinVaultReference


class MaintenanceConfig(FrozenModel):
    postgresql_bin_directory: str = Field(min_length=3, max_length=260)
    log_root: str = Field(min_length=3, max_length=200)
    backup_root: str = Field(min_length=3, max_length=200)
    temporary_root: str = Field(min_length=3, max_length=200)
    backup_retention_count: Literal[14] = 14
    backup_schedule_local: Literal["02:30"] = "02:30"
    demo: DatabaseMaintenanceTarget
    live_copy: DatabaseMaintenanceTarget
    live_personal: DatabaseMaintenanceTarget

    def named_targets(self) -> tuple[tuple[str, DatabaseMaintenanceTarget], ...]:
        return (
            ("demo", self.demo),
            ("live_copy", self.live_copy),
            ("live_personal", self.live_personal),
        )

    @model_validator(mode="after")
    def validate_targets_and_paths(self) -> "MaintenanceConfig":
        expected = {
            "demo": VenueAccountType.USDM_DEMO,
            "live_copy": VenueAccountType.USDM_COPY_LEAD,
            "live_personal": VenueAccountType.USDM_PERSONAL,
        }
        for name, target in self.named_targets():
            account_type = expected[name]
            spec = trading_context_spec(account_type)
            if (
                target.venue_account_type != account_type
                or target.environment_kind != spec.environment_kind
                or target.database_name != spec.database_name
            ):
                raise ValueError("MAINTENANCE_DATABASE_TARGET_MISMATCH")
            database = spec.database_name
            if target.backup_role_name != f"{database}_backup":
                raise ValueError("MAINTENANCE_BACKUP_ROLE_MISMATCH")
            if target.migration_role_name != f"{database}_migration":
                raise ValueError("MAINTENANCE_MIGRATION_ROLE_MISMATCH")
            if target.backup_credential_reference == target.migration_credential_reference:
                raise ValueError("MAINTENANCE_CREDENTIAL_REFERENCE_OVERLAP")
            namespace = spec.namespace
            expected_references = (
                (
                    target.backup_credential_reference,
                    f"Halpha/PostgreSQL/{namespace}/Backup",
                ),
                (
                    target.migration_credential_reference,
                    f"Halpha/PostgreSQL/{namespace}/Migration",
                ),
            )
            if any(
                reference.service != service
                or reference.account != "scram_password"
                for reference, service in expected_references
            ):
                raise ValueError(
                    "MAINTENANCE_CREDENTIAL_REFERENCE_ENVIRONMENT_MISMATCH"
                )
        relative_paths = (
            self.log_root,
            self.backup_root,
            self.temporary_root,
        )
        for value in relative_paths:
            path = Path(value)
            if path.is_absolute() or ".." in path.parts or "\\" in value:
                raise ValueError("MAINTENANCE_RELATIVE_PATH_INVALID")
        normalized_paths = {
            Path(value).as_posix().rstrip("/").casefold()
            for value in relative_paths
        }
        if len(normalized_paths) != len(relative_paths):
            raise ValueError("MAINTENANCE_RUNTIME_PATHS_MUST_BE_DISTINCT")
        return self


class EmailConfig(FrozenModel):
    delivery_enabled: bool = False
    smtp_host: str | None = Field(default=None, min_length=3, max_length=253)
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str | None = Field(default=None, min_length=1, max_length=320)
    sender: str | None = Field(default=None, min_length=3, max_length=320)
    owner_recipient: str | None = Field(default=None, min_length=3, max_length=320)
    require_starttls: Literal[True] = True
    timeout_seconds: int = Field(default=10, ge=1, le=60)

    @model_validator(mode="after")
    def validate_enabled_route(self) -> "EmailConfig":
        configured = (
            self.smtp_host,
            self.smtp_username,
            self.sender,
            self.owner_recipient,
        )
        if self.delivery_enabled and any(value is None for value in configured):
            raise ValueError("EMAIL_DELIVERY_CONFIGURATION_INCOMPLETE")
        for value in (self.sender, self.owner_recipient):
            if value is not None and ("\r" in value or "\n" in value or "@" not in value):
                raise ValueError("EMAIL_ADDRESS_INVALID")
        return self


class WindowsIdentityConfig(FrozenModel):
    app_task_sid: str
    executor_task_sid: str
    backup_task_sid: str
    maintenance_sid: str
    app_stop_event: str = Field(
        pattern=(
            r"^Global\\Halpha\.App\.BINANCE_(?:DEMO|LIVE_(?:COPY|PERSONAL))"
            r"\.Stop$"
        )
    )
    executor_stop_event: str = Field(
        pattern=(
            r"^Global\\Halpha\.Executor\.BINANCE_(?:DEMO|LIVE_(?:COPY|PERSONAL))"
            r"\.Stop$"
        )
    )

    @model_validator(mode="after")
    def validate_distinct_sids(self) -> "WindowsIdentityConfig":
        sids = (
            self.app_task_sid,
            self.executor_task_sid,
            self.backup_task_sid,
            self.maintenance_sid,
        )
        if any(_SID_PATTERN.fullmatch(sid) is None for sid in sids):
            raise ValueError("WINDOWS_SID_INVALID")
        if len(set(sids)) != len(sids):
            raise ValueError("WINDOWS_RUNTIME_SIDS_MUST_BE_DISTINCT")
        return self


class HalphaSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2]
    release: ReleaseConfig
    app: AppConfig
    executor: ExecutorConfig
    maintenance: MaintenanceConfig
    email: EmailConfig
    new_risk_discipline: NewRiskDisciplinePolicy = Field(
        default_factory=NewRiskDisciplinePolicy
    )
    live_profit_qualifications: tuple[LiveProfitQualificationReference, ...] = ()
    windows: WindowsIdentityConfig

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: Any,
        env_settings: Any,
        dotenv_settings: Any,
        file_secret_settings: Any,
    ) -> tuple[Any, ...]:
        # Direct tests may use explicit values only. The public loader below
        # adds exactly one explicit TomlConfigSettingsSource.
        return (init_settings,)

    @model_validator(mode="after")
    def validate_environment_and_secret_separation(self) -> "HalphaSettings":
        release = self.release
        spec = trading_context_spec(release.venue_account_type)
        if release.environment_id != spec.environment_id:
            raise ValueError("ENVIRONMENT_ID_PROFILE_MISMATCH")
        if release.account_id != spec.account_id:
            raise ValueError("ACCOUNT_ID_PROFILE_MISMATCH")
        if release.database_name != spec.database_name:
            raise ValueError("DATABASE_ACCOUNT_TYPE_MISMATCH")
        if self.app.port != spec.port:
            raise ValueError("APP_PORT_PROFILE_MISMATCH")
        if release.venue_account_type is VenueAccountType.USDM_DEMO:
            if release.profile != "BINANCE_DEMO":
                raise ValueError("PROFILE_ACCOUNT_TYPE_MISMATCH")
        elif release.profile not in {
            "BINANCE_LIVE_READ_ONLY",
            "BINANCE_LIVE_WRITE",
        }:
            raise ValueError("PROFILE_ACCOUNT_TYPE_MISMATCH")

        configured_targets = {
            target.venue_account_type: target for target in self.app.trading_contexts
        }
        if set(configured_targets) != set(TRADING_CONTEXT_SPECS):
            raise ValueError("TRADING_CONTEXT_TARGET_SET_MISMATCH")
        for account_type, target_spec in TRADING_CONTEXT_SPECS.items():
            target = configured_targets[account_type]
            if (
                target.environment_id != target_spec.environment_id
                or target.account_id != target_spec.account_id
                or urlsplit(target.url).port != target_spec.port
            ):
                raise ValueError("TRADING_CONTEXT_TARGET_IDENTITY_MISMATCH")

        environment_namespace = spec.namespace
        policy = self.new_risk_discipline
        if (
            Decimal(policy.max_plan_loss_fraction) > Decimal(MAX_PLAN_LOSS_FRACTION)
            or Decimal(policy.max_open_risk_fraction) > Decimal(MAX_OPEN_RISK_FRACTION)
            or Decimal(policy.max_gross_exposure_fraction)
            > Decimal(MAX_GROSS_EXPOSURE_FRACTION)
            or Decimal(policy.max_instrument_exposure_fraction)
            > Decimal(MAX_INSTRUMENT_EXPOSURE_FRACTION)
            or Decimal(policy.max_correlated_exposure_fraction)
            > Decimal(MAX_CORRELATED_EXPOSURE_FRACTION)
            or Decimal(policy.daily_loss_stop_fraction)
            > Decimal(DAILY_LOSS_STOP_FRACTION)
            or Decimal(policy.weekly_loss_stop_fraction)
            > Decimal(WEEKLY_LOSS_STOP_FRACTION)
            or Decimal(policy.rolling_drawdown_stop_fraction)
            > Decimal(ROLLING_DRAWDOWN_STOP_FRACTION)
            or Decimal(policy.minimum_reward_risk_ratio)
            < Decimal(MINIMUM_REWARD_RISK_RATIO)
            # A shorter lookback can forget the previous equity peak earlier and
            # therefore relax admission.  Only the baseline window or a longer
            # one is a monotonic tightening of this policy.
            or policy.rolling_drawdown_lookback_days
            < ROLLING_DRAWDOWN_LOOKBACK_DAYS
        ):
            raise ValueError("ACCOUNT_PORTFOLIO_RISK_POLICY_EXCEEDED")

        if release.profile == "BINANCE_DEMO":
            if release.authority_class != "DEMO_VALIDATION":
                raise ValueError("DEMO_AUTHORITY_CLASS_MISMATCH")
            if (
                self.executor.binance_api_key_reference is None
                or self.executor.binance_api_key_reference.service
                != "Halpha/Binance/BINANCE_DEMO"
            ):
                raise ValueError("DEMO_BINANCE_REFERENCE_MISMATCH")
        elif release.profile == "BINANCE_LIVE_WRITE":
            if release.authority_class != "LIVE_REAL_CAPITAL":
                raise ValueError("LIVE_AUTHORITY_CLASS_MISMATCH")
            if self.executor.binance_api_key_reference is None:
                raise ValueError("LIVE_WRITE_BINANCE_REFERENCE_REQUIRED")
            if (
                self.executor.binance_api_key_reference.service
                != f"Halpha/Binance/{environment_namespace}"
            ):
                raise ValueError("LIVE_BINANCE_REFERENCE_MISMATCH")
            gate_path = Path(release.live_write_gate_path or "")
            expected_gate_name = {
                VenueAccountType.USDM_COPY_LEAD: "live-copy-write-gate.json",
                VenueAccountType.USDM_PERSONAL: "live-personal-write-gate.json",
            }[release.venue_account_type]
            if gate_path.name != expected_gate_name:
                raise ValueError("LIVE_WRITE_GATE_ACCOUNT_TYPE_MISMATCH")
        else:
            if release.authority_class != "NO_TRADING_AUTHORITY":
                raise ValueError("LIVE_READ_ONLY_AUTHORITY_CLASS_MISMATCH")
            if (
                self.executor.binance_api_key_reference is not None
                and self.executor.binance_api_key_reference.service
                != f"Halpha/Binance/{environment_namespace}"
            ):
                raise ValueError("LIVE_BINANCE_REFERENCE_MISMATCH")

        qualification_refs = self.live_profit_qualifications
        if release.venue_account_type is VenueAccountType.USDM_DEMO and qualification_refs:
            raise ValueError("LIVE_PROFIT_QUALIFICATION_REQUIRES_LIVE_CONTEXT")
        qualification_paths = [item.artifact_path for item in qualification_refs]
        qualification_digests = [
            item.expected_content_digest for item in qualification_refs
        ]
        if (
            len(qualification_paths) != len(set(qualification_paths))
            or len(qualification_digests) != len(set(qualification_digests))
        ):
            raise ValueError("LIVE_PROFIT_QUALIFICATION_REFERENCE_DUPLICATED")

        app_role_kind = (
            "app_reader" if release.profile == "BINANCE_LIVE_READ_ONLY" else "app"
        )
        expected_app_database_role = f"{spec.database_name}_{app_role_kind}"
        expected_app_database_service = (
            f"Halpha/PostgreSQL/{environment_namespace}/"
            f"{'AppReader' if app_role_kind == 'app_reader' else 'App'}"
        )
        if self.app.database_role_name != expected_app_database_role:
            raise ValueError("APP_DATABASE_ROLE_PROFILE_MISMATCH")

        expected_role_services = (
            (
                self.app.database_credential_reference,
                expected_app_database_service,
                "scram_password",
            ),
            (
                self.app.csrf_signing_reference,
                f"Halpha/Web/{environment_namespace}",
                "csrf_signing",
            ),
            (
                self.app.smtp_credential_reference,
                f"Halpha/SMTP/{environment_namespace}",
                "password",
            ),
            (
                self.executor.database_credential_reference,
                f"Halpha/PostgreSQL/{environment_namespace}/Executor",
                "scram_password",
            ),
        )
        if any(
            reference.service != service or reference.account != account
            for reference, service, account in expected_role_services
        ):
            raise ValueError("RUNTIME_CREDENTIAL_REFERENCE_ENVIRONMENT_MISMATCH")

        proxy_reference = self.executor.runtime_proxy_reference
        if proxy_reference is not None:
            expected_proxy_service = {
                "BINANCE_DEMO": "Halpha/Network/BINANCE_DEMO",
                "BINANCE_LIVE_READ_ONLY": (
                    f"Halpha/Network/{environment_namespace}_READ_ONLY"
                ),
                "BINANCE_LIVE_WRITE": f"Halpha/Network/{environment_namespace}",
            }[release.profile]
            if (
                proxy_reference.service != expected_proxy_service
                or proxy_reference.account
                not in {"proxy_url", "runtime_proxy"}
            ):
                raise ValueError("RUNTIME_PROXY_REFERENCE_ENVIRONMENT_MISMATCH")

        expected_runtime_namespace = environment_namespace
        if self.executor.mutex_name != (
            f"Global\\Halpha.Executor.{expected_runtime_namespace}.WriteOwner"
        ):
            raise ValueError("EXECUTOR_MUTEX_ENVIRONMENT_MISMATCH")
        if self.windows.app_stop_event != (
            f"Global\\Halpha.App.{expected_runtime_namespace}.Stop"
        ):
            raise ValueError("APP_STOP_EVENT_ENVIRONMENT_MISMATCH")
        if self.windows.executor_stop_event != (
            f"Global\\Halpha.Executor.{expected_runtime_namespace}.Stop"
        ):
            raise ValueError("EXECUTOR_STOP_EVENT_ENVIRONMENT_MISMATCH")

        key_ref = self.executor.binance_api_key_reference
        secret_ref = self.executor.binance_api_secret_reference
        if (key_ref is None) != (secret_ref is None):
            raise ValueError("BINANCE_REFERENCE_PAIR_INCOMPLETE")
        if key_ref is not None and secret_ref is not None:
            if (
                key_ref.service != secret_ref.service
                or key_ref.account != "api_key"
                or secret_ref.account != "api_secret"
            ):
                raise ValueError("BINANCE_REFERENCE_PAIR_INVALID")
        if self.executor.continuous_account_observation:
            if release.profile != "BINANCE_LIVE_READ_ONLY":
                raise ValueError(
                    "CONTINUOUS_ACCOUNT_OBSERVATION_REQUIRES_LIVE_READ_ONLY"
                )
            if key_ref is None or secret_ref is None:
                raise ValueError(
                    "CONTINUOUS_ACCOUNT_OBSERVATION_CREDENTIALS_REQUIRED"
                )

        non_executor_references = {
            self.app.database_credential_reference,
            self.app.csrf_signing_reference,
            self.app.smtp_credential_reference,
            *(
                reference
                for _name, target in self.maintenance.named_targets()
                for reference in (
                    target.backup_credential_reference,
                    target.migration_credential_reference,
                )
            ),
        }
        executor_references = {self.executor.database_credential_reference}
        executor_references.update(
            reference for reference in (key_ref, secret_ref) if reference is not None
        )
        if self.executor.runtime_proxy_reference is not None:
            executor_references.add(self.executor.runtime_proxy_reference)
        if non_executor_references & executor_references:
            raise ValueError("APP_EXECUTOR_CREDENTIAL_REFERENCE_OVERLAP")
        return self


class AppSettingsView(FrozenModel):
    release: ReleaseConfig
    app: AppConfig
    email: EmailConfig
    app_task_sid: str
    maintenance_sid: str
    stop_event: str


class ExecutorSettingsView(FrozenModel):
    release: ReleaseConfig
    executor: ExecutorConfig
    new_risk_discipline: NewRiskDisciplinePolicy
    executor_task_sid: str
    maintenance_sid: str
    stop_event: str


class BackupSettingsView(FrozenModel):
    maintenance: MaintenanceConfig
    backup_task_sid: str


class MaintenanceSettingsView(FrozenModel):
    maintenance: MaintenanceConfig
    maintenance_sid: str


def _reject_secret_value_keys(value: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).lower()
            if key in _FORBIDDEN_VALUE_KEYS:
                location = ".".join((*path, str(raw_key)))
                raise ConfigurationError(f"SECRET_VALUE_KEY_FORBIDDEN field={location}")
            _reject_secret_value_keys(child, (*path, str(raw_key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_value_keys(child, (*path, str(index)))


def _sanitized_validation_error(exc: ValidationError) -> ConfigurationError:
    summaries = []
    for issue in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in issue.get("loc", ())) or "root"
        summaries.append(f"{location}:{issue.get('type', 'validation_error')}")
    return ConfigurationError("CONFIGURATION_INVALID " + ",".join(sorted(summaries)))


def load_settings(
    config_path: Path,
    *,
    constructor_values: Mapping[str, Any] | None = None,
) -> HalphaSettings:
    if config_path.is_symlink():
        raise ConfigurationError("CONFIGURATION_SYMLINK_FORBIDDEN")
    path = config_path.resolve()
    if not path.is_file():
        raise ConfigurationError("CONFIGURATION_FILE_MISSING")

    try:
        values = TomlConfigSettingsSource(HalphaSettings, toml_file=path)()
    except Exception as exc:
        raise ConfigurationError(f"CONFIGURATION_TOML_READ_FAILED type={type(exc).__name__}") from None
    _reject_secret_value_keys(values)
    if constructor_values:
        _reject_secret_value_keys(constructor_values)
        values.update(dict(constructor_values))
    try:
        return HalphaSettings.model_validate(values)
    except ValidationError as exc:
        raise _sanitized_validation_error(exc) from None


def settings_digest(settings: HalphaSettings) -> str:
    payload = json.dumps(
        settings.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def known_live_credential_references() -> tuple[WinVaultReference, ...]:
    """Return the complete fixed Live credential universe for vault convergence."""

    services_and_accounts = tuple(
        (service, account)
        for account_type in (
            VenueAccountType.USDM_COPY_LEAD,
            VenueAccountType.USDM_PERSONAL,
        )
        for namespace in (trading_context_spec(account_type).namespace,)
        for service, account in (
            (f"Halpha/PostgreSQL/{namespace}/App", "scram_password"),
            (f"Halpha/PostgreSQL/{namespace}/AppReader", "scram_password"),
            (f"Halpha/Web/{namespace}", "csrf_signing"),
            (f"Halpha/SMTP/{namespace}", "password"),
            (f"Halpha/PostgreSQL/{namespace}/Executor", "scram_password"),
            (f"Halpha/Binance/{namespace}", "api_key"),
            (f"Halpha/Binance/{namespace}", "api_secret"),
            (f"Halpha/Network/{namespace}_READ_ONLY", "proxy_url"),
            (f"Halpha/Network/{namespace}_READ_ONLY", "runtime_proxy"),
            (f"Halpha/Network/{namespace}", "proxy_url"),
            (f"Halpha/Network/{namespace}", "runtime_proxy"),
            (f"Halpha/PostgreSQL/{namespace}/Backup", "scram_password"),
            (f"Halpha/PostgreSQL/{namespace}/Migration", "scram_password"),
        )
    )
    return tuple(
        WinVaultReference(service=service, account=account)
        for service, account in services_and_accounts
    )


def runtime_log_directory(
    repository_root: Path,
    settings: HalphaSettings,
    *,
    role: Literal["app", "executor"],
) -> Path:
    """Return the environment-and-role-owned runtime log directory."""

    return (
        repository_root.resolve()
        / settings.maintenance.log_root
        / settings.release.environment_id
        / role
    )


def backup_log_directory(
    repository_root: Path,
    settings: HalphaSettings,
) -> Path:
    """Return the shared Backup identity's maintenance log directory."""

    return (
        repository_root.resolve()
        / settings.maintenance.log_root
        / "maintenance"
        / "backup"
    )


def forward_observation_directory(
    repository_root: Path,
    settings: HalphaSettings,
) -> Path:
    """Keep Live read-only evidence inside the Executor-owned write tree."""

    return (
        runtime_log_directory(
            repository_root,
            settings,
            role="executor",
        )
        / "forward-observation"
    )


def app_settings(settings: HalphaSettings) -> AppSettingsView:
    return AppSettingsView(
        release=settings.release,
        app=settings.app,
        email=settings.email,
        app_task_sid=settings.windows.app_task_sid,
        maintenance_sid=settings.windows.maintenance_sid,
        stop_event=settings.windows.app_stop_event,
    )


def executor_settings(settings: HalphaSettings) -> ExecutorSettingsView:
    return ExecutorSettingsView(
        release=settings.release,
        executor=settings.executor,
        new_risk_discipline=settings.new_risk_discipline,
        executor_task_sid=settings.windows.executor_task_sid,
        maintenance_sid=settings.windows.maintenance_sid,
        stop_event=settings.windows.executor_stop_event,
    )


def maintenance_settings(settings: HalphaSettings) -> MaintenanceSettingsView:
    return MaintenanceSettingsView(
        maintenance=settings.maintenance,
        maintenance_sid=settings.windows.maintenance_sid,
    )


def backup_settings(settings: HalphaSettings) -> BackupSettingsView:
    return BackupSettingsView(
        maintenance=settings.maintenance,
        backup_task_sid=settings.windows.backup_task_sid,
    )
