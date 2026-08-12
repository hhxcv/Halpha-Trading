"""Build-time code strategy registry and authoritative parameter validation."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from halpha.domain_values import canonical_decimal, content_digest, decimal_from_string
from halpha.source_identity import source_file_sha256


ONE_SHOT_STRATEGY_ID = "ONE_SHOT_DONCHIAN_ATR_BREAKOUT"
ONE_SHOT_STRATEGY_VERSION = "1.0.1"
PARAMETER_SCHEMA_VERSION = "1.3.0"
DIRECT_EXECUTION_REF = "DIRECT_EXECUTION@1"
DIRECT_EXECUTION_PARAMETER_SCHEMA_VERSION = "1"
DIRECT_EXECUTION_ALLOWED_ACTION_PROFILES = (
    "ENTRY_MARKET",
    "ENTRY_LIMIT",
    "PROTECTIVE_STOP_REDUCE_ONLY",
    "TAKE_PROFIT_1",
    "TAKE_PROFIT_2",
    "CANCEL_ORDER",
    "REDUCE_OR_CLOSE_MARKET",
)
VALID_STRATEGY_PLAN_INTENTS = frozenset({"PROFIT_SEEKING", "VALIDATION"})


def strategy_allowed_plan_intents(economic_scope: dict[str, Any]) -> frozenset[str]:
    """Read an explicit, closed strategy-to-plan-intent qualification."""

    raw = economic_scope.get("allowed_plan_intents")
    if not isinstance(raw, (list, tuple)) or not raw:
        return frozenset()
    if any(not isinstance(item, str) for item in raw):
        return frozenset()
    values = frozenset(raw)
    if len(values) != len(raw) or not values.issubset(VALID_STRATEGY_PLAN_INTENTS):
        return frozenset()
    return values


def strategy_decision_intent_incompatibility(
    economic_scope: dict[str, Any],
    intent: str | None,
) -> str | None:
    """Fail closed unless a strategy explicitly qualifies the requested intent."""

    if intent not in strategy_allowed_plan_intents(economic_scope):
        return "STRATEGY_DECISION_INTENT_NOT_QUALIFIED"
    return None


class DecisionBasisKind(StrEnum):
    STRATEGY_SIGNAL = "STRATEGY_SIGNAL"
    DIRECT_EXECUTION = "DIRECT_EXECUTION"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class DraftDecisionBasis(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_serialization_defaults_required=True,
    )

    kind: DecisionBasisKind
    decision_basis_ref: str
    parameters: dict[str, Any]

    @model_validator(mode="after")
    def reference_matches_kind(self) -> DraftDecisionBasis:
        if self.kind is DecisionBasisKind.DIRECT_EXECUTION:
            if self.decision_basis_ref != DIRECT_EXECUTION_REF or self.parameters:
                raise ValueError("DIRECT_EXECUTION_BASIS_INVALID")
        elif self.decision_basis_ref == DIRECT_EXECUTION_REF:
            raise ValueError("STRATEGY_DECISION_BASIS_INVALID")
        return self


class PlanParameterDisplayFormat(StrEnum):
    VALUE = "VALUE"
    PERCENT = "PERCENT"
    BOOLEAN_LABEL = "BOOLEAN_LABEL"


class PlanKeyParameterDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    parameter_key: str
    label: str
    display_format: PlanParameterDisplayFormat = PlanParameterDisplayFormat.VALUE
    unit: str | None = None
    true_label: str | None = None
    false_label: str | None = None

    @model_validator(mode="after")
    def boolean_labels_match_format(self) -> PlanKeyParameterDefinition:
        labels = (self.true_label, self.false_label)
        if self.display_format is PlanParameterDisplayFormat.BOOLEAN_LABEL:
            if any(label is None for label in labels):
                raise ValueError("PLAN_PARAMETER_BOOLEAN_LABEL_MISSING")
        elif any(label is not None for label in labels):
            raise ValueError("PLAN_PARAMETER_BOOLEAN_LABEL_UNEXPECTED")
        return self


class OneShotParameters(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=False,
        validate_default=True,
    )

    direction: Direction
    demo_immediate_entry: bool = False
    channel_lookback_15m: int = Field(default=20, ge=4, le=96)
    confirmation_bars_1m: int = Field(default=2, ge=1, le=3)
    initial_stop_atr_multiple: str = "1.5"
    max_entry_extension_atr: str = "0.5"
    take_profit_1_r: str = "1.5"
    take_profit_1_fraction: str = "0.50"
    take_profit_2_r: str = "3.0"
    max_hold_bars_15m: int = Field(default=4, ge=4, le=672)
    entry_valid_minutes: int = Field(default=60, ge=15, le=10080)

    @field_validator(
        "initial_stop_atr_multiple",
        "max_entry_extension_atr",
        "take_profit_1_r",
        "take_profit_1_fraction",
        "take_profit_2_r",
    )
    @classmethod
    def validate_decimal_strings(cls, value: str, info: Any) -> str:
        ranges = {
            "initial_stop_atr_multiple": (Decimal("1.0"), Decimal("3.0")),
            "max_entry_extension_atr": (Decimal("0.1"), Decimal("1.0")),
            "take_profit_1_r": (Decimal("1.0"), Decimal("3.0")),
            "take_profit_1_fraction": (Decimal("0.25"), Decimal("0.75")),
            "take_profit_2_r": (Decimal("2.0"), Decimal("6.0")),
        }
        parsed = decimal_from_string(value, code="PARAMETER_INVALID", positive=True)
        minimum, maximum = ranges[info.field_name]
        if parsed < minimum or parsed > maximum:
            raise ValueError("PARAMETER_OUT_OF_RANGE")
        return canonical_decimal(parsed)

    @model_validator(mode="after")
    def validate_cross_constraints(self) -> OneShotParameters:
        if Decimal(self.take_profit_2_r) <= Decimal(self.take_profit_1_r):
            raise ValueError("TAKE_PROFIT_ORDER_INVALID")
        return self

    @property
    def effective_confirmation_bars_1m(self) -> int:
        return 1 if self.demo_immediate_entry else self.confirmation_bars_1m


class CodeStrategyDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy_id: str
    strategy_version: str
    display_name: str
    value_logic: str
    applicable_scenarios: str
    execution_behavior: str
    implementation_path: str
    implementation_digest: str
    parameter_schema_version: str
    parameter_schema: dict[str, Any]
    native_indicators: tuple[str, ...]
    allowed_action_profiles: tuple[str, ...]
    supported_directions: tuple[Direction, ...]
    economic_scope: dict[str, Any]
    plan_key_parameters: tuple[PlanKeyParameterDefinition, ...]

    @model_validator(mode="after")
    def plan_key_parameters_belong_to_schema(self) -> CodeStrategyDefinition:
        schema_keys = set(self.parameter_schema.get("properties", {}))
        parameter_keys = [item.parameter_key for item in self.plan_key_parameters]
        if len(parameter_keys) != len(set(parameter_keys)):
            raise ValueError("PLAN_KEY_PARAMETER_DUPLICATED")
        if not set(parameter_keys).issubset(schema_keys):
            raise ValueError("PLAN_KEY_PARAMETER_UNKNOWN")
        allowed_intents = strategy_allowed_plan_intents(self.economic_scope)
        if not allowed_intents:
            raise ValueError("STRATEGY_ALLOWED_PLAN_INTENTS_INVALID")
        if (
            "PROFIT_SEEKING" in allowed_intents
            and self.economic_scope.get("profitability_evidence")
            != "POSITIVE_EXPECTANCY_SUPPORTED"
        ):
            raise ValueError("STRATEGY_PROFIT_INTENT_NOT_EVIDENCE_QUALIFIED")
        if (
            self.economic_scope.get("recommended_use")
            == "EXECUTION_CHAIN_VALIDATION_ONLY"
            and "PROFIT_SEEKING" in allowed_intents
        ):
            raise ValueError("STRATEGY_VALIDATION_ONLY_SCOPE_CONFLICT")
        return self


class FixedStrategyPlanBasis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[DecisionBasisKind.STRATEGY_SIGNAL] = (
        DecisionBasisKind.STRATEGY_SIGNAL
    )
    decision_basis_ref: str
    strategy_id: str
    strategy_version: str
    implementation_digest: str
    parameter_schema_version: str
    normalized_parameters: dict[str, Any]
    parameter_digest: str
    fact_input_contract: dict[str, Any] = Field(default_factory=dict)
    allowed_action_profiles: tuple[str, ...] = ()
    economic_scope: dict[str, Any] = Field(default_factory=dict)
    product_build_id: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def basis_is_complete(self) -> FixedStrategyPlanBasis:
        if self.decision_basis_ref != f"{self.strategy_id}@{self.strategy_version}":
            raise ValueError("STRATEGY_DECISION_BASIS_INVALID")
        if (
            not self.implementation_digest
            or not self.fact_input_contract
            or not self.allowed_action_profiles
            or not self.economic_scope
        ):
            raise ValueError("STRATEGY_DECISION_BASIS_INCOMPLETE")
        return self


class FixedDirectExecutionBasis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[DecisionBasisKind.DIRECT_EXECUTION] = (
        DecisionBasisKind.DIRECT_EXECUTION
    )
    decision_basis_ref: Literal[DIRECT_EXECUTION_REF] = DIRECT_EXECUTION_REF
    parameter_schema_version: Literal[DIRECT_EXECUTION_PARAMETER_SCHEMA_VERSION] = (
        DIRECT_EXECUTION_PARAMETER_SCHEMA_VERSION
    )
    normalized_parameters: dict[str, Any] = Field(default_factory=dict)
    parameter_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    product_build_id: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def parameters_are_canonical_empty(self) -> FixedDirectExecutionBasis:
        if self.normalized_parameters or self.parameter_digest != content_digest({}):
            raise ValueError("DIRECT_EXECUTION_BASIS_INVALID")
        return self


FixedDecisionBasis = Annotated[
    FixedStrategyPlanBasis | FixedDirectExecutionBasis,
    Field(discriminator="kind"),
]


def _implementation_path() -> Path:
    return Path(__file__).resolve().parent / "strategies" / "one_shot.py"


def _implementation_digest() -> str:
    return source_file_sha256(_implementation_path())


def _parameter_schema() -> dict[str, Any]:
    decimal_pattern = r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$"
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:halpha:strategy:{ONE_SHOT_STRATEGY_ID}:parameters:{PARAMETER_SCHEMA_VERSION}",
        "title": "单次 Donchian 突破与 ATR 风险退出",
        "type": "object",
        "additionalProperties": False,
        "required": ["direction"],
        "properties": {
            "direction": {"type": "string", "enum": ["LONG", "SHORT"]},
            "demo_immediate_entry": {"type": "boolean", "default": False},
            "channel_lookback_15m": {
                "type": "integer",
                "minimum": 4,
                "maximum": 96,
                "default": 20,
            },
            "confirmation_bars_1m": {
                "type": "integer",
                "minimum": 1,
                "maximum": 3,
                "default": 2,
            },
            "initial_stop_atr_multiple": {
                "type": "string",
                "pattern": decimal_pattern,
                "default": "1.5",
                "x-halpha-minimum": "1.0",
                "x-halpha-maximum": "3.0",
            },
            "max_entry_extension_atr": {
                "type": "string",
                "pattern": decimal_pattern,
                "default": "0.5",
                "x-halpha-minimum": "0.1",
                "x-halpha-maximum": "1.0",
            },
            "take_profit_1_r": {
                "type": "string",
                "pattern": decimal_pattern,
                "default": "1.5",
                "x-halpha-minimum": "1.0",
                "x-halpha-maximum": "3.0",
            },
            "take_profit_1_fraction": {
                "type": "string",
                "pattern": decimal_pattern,
                "default": "0.50",
                "x-halpha-minimum": "0.25",
                "x-halpha-maximum": "0.75",
            },
            "take_profit_2_r": {
                "type": "string",
                "pattern": decimal_pattern,
                "default": "3.0",
                "x-halpha-minimum": "2.0",
                "x-halpha-maximum": "6.0",
            },
            "max_hold_bars_15m": {
                "type": "integer",
                "minimum": 4,
                "maximum": 672,
                "default": 4,
            },
            "entry_valid_minutes": {
                "type": "integer",
                "minimum": 15,
                "maximum": 10080,
                "default": 60,
            },
        },
        "allOf": [
            {
                "x-halpha-cross-constraint": {
                    "code": "TAKE_PROFIT_ORDER_INVALID",
                    "expression": "take_profit_2_r > take_profit_1_r",
                }
            }
        ],
    }


def _definition() -> CodeStrategyDefinition:
    return CodeStrategyDefinition(
        strategy_id=ONE_SHOT_STRATEGY_ID,
        strategy_version=ONE_SHOT_STRATEGY_VERSION,
        display_name="单次 Donchian 突破与 ATR 风险退出",
        value_logic=(
            "用 Donchian 通道识别价格脱离近期区间的方向性突破，并用 ATR 统一入场距离、"
            "仓位风险和退出尺度，争取捕捉短周期动量，同时限制单次交易损失。"
        ),
        applicable_scenarios=(
            "适用于 Binance USDⓈ-M 的 BTCUSDT 永续合约出现明确 15 分钟通道突破、"
            "希望以单次多头或空头交易参与短周期趋势的场景；震荡行情可能产生假突破。"
        ),
        execution_behavior=(
            "激活后检查已闭合 15 分钟 K 线和配置数量的 1 分钟确认收盘；满足突破且未过度"
            "延伸时发起一次市价入场，成交后设置 ATR 止损和两档止盈，并记录最长持仓期限；"
            "每个激活最多入场一次。仅 Demo 流程检查开关会跳过自然突破等待。"
        ),
        implementation_path="halpha.planning.strategies.one_shot:OneShotDonchianAtrLogic",
        implementation_digest=_implementation_digest(),
        parameter_schema_version=PARAMETER_SCHEMA_VERSION,
        parameter_schema=_parameter_schema(),
        native_indicators=(
            "nautilus_trader.indicators.DonchianChannel",
            "nautilus_trader.indicators.AverageTrueRange",
        ),
        allowed_action_profiles=(
            "ENTRY_MARKET",
            "PROTECTIVE_STOP_REDUCE_ONLY",
            "TAKE_PROFIT_1",
            "TAKE_PROFIT_2",
            "CANCEL_ORDER",
            "REDUCE_OR_CLOSE_MARKET",
        ),
        supported_directions=(Direction.LONG, Direction.SHORT),
        economic_scope={
            "venue": "BINANCE_USDM",
            "qualified_live_instrument": "BTCUSDT-PERP",
            "one_entry_cycle": True,
            "funding_model": "NOT_MODELED_IN_BACKTEST",
            "profitability_evidence": "NO_POSITIVE_EXPECTANCY_EVIDENCE",
            "recommended_use": "EXECUTION_CHAIN_VALIDATION_ONLY",
            "allowed_plan_intents": ["VALIDATION"],
            "evidence_limit": (
                "固定短周期规则在费用后的历史开发样本中未取得正期望；"
                "仅用于验证计划、成交、保护和退出链路，不应用于盈利目标。"
            ),
        },
        plan_key_parameters=(
            PlanKeyParameterDefinition(
                parameter_key="demo_immediate_entry",
                label="入场模式",
                display_format=PlanParameterDisplayFormat.BOOLEAN_LABEL,
                true_label="Demo 流程检查",
                false_label="自然突破信号",
            ),
            PlanKeyParameterDefinition(
                parameter_key="channel_lookback_15m",
                label="15m 通道回看",
                unit="根",
            ),
            PlanKeyParameterDefinition(
                parameter_key="confirmation_bars_1m",
                label="1m 确认根数",
                unit="根",
            ),
            PlanKeyParameterDefinition(
                parameter_key="entry_valid_minutes",
                label="入场等待窗口",
                unit="分钟",
            ),
            PlanKeyParameterDefinition(
                parameter_key="initial_stop_atr_multiple",
                label="初始止损",
                unit="ATR",
            ),
            PlanKeyParameterDefinition(
                parameter_key="max_entry_extension_atr",
                label="最大追价",
                unit="ATR",
            ),
            PlanKeyParameterDefinition(
                parameter_key="take_profit_1_fraction",
                label="止盈一仓位比例",
                display_format=PlanParameterDisplayFormat.PERCENT,
            ),
            PlanKeyParameterDefinition(
                parameter_key="take_profit_1_r",
                label="止盈一目标",
                unit="R",
            ),
            PlanKeyParameterDefinition(
                parameter_key="take_profit_2_r",
                label="止盈二目标",
                unit="R",
            ),
            PlanKeyParameterDefinition(
                parameter_key="max_hold_bars_15m",
                label="最大持仓（15m）",
                unit="根",
            ),
        ),
    )


def list_strategies() -> tuple[CodeStrategyDefinition, ...]:
    return (_definition(),)


def strategy_registry_payload() -> dict[str, Any]:
    strategies = [item.model_dump(mode="json") for item in list_strategies()]
    payload: dict[str, Any] = {
        "schema_version": 2,
        "registry_kind": "STATIC_BUILD_ARTIFACT",
        "strategies": strategies,
    }
    payload["registry_digest"] = content_digest(payload)
    return payload


def render_strategy_registry() -> str:
    return (
        json.dumps(
            strategy_registry_payload(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def describe_strategy(strategy_id: str) -> CodeStrategyDefinition:
    if strategy_id != ONE_SHOT_STRATEGY_ID:
        raise KeyError("STRATEGY_UNAVAILABLE")
    return _definition()


def strategy_parameter_schema(strategy_id: str) -> dict[str, Any]:
    return describe_strategy(strategy_id).parameter_schema


def validate_parameters(strategy_id: str, parameters: dict[str, Any]) -> dict[str, Any]:
    describe_strategy(strategy_id)
    normalized = OneShotParameters.model_validate(parameters)
    return normalized.model_dump(mode="json")


def fixed_decision_basis_runtime_incompatibility(
    basis: FixedDecisionBasis,
) -> str | None:
    """Return why the current runtime cannot consume one frozen decision basis.

    A product build id is provenance, not a semantic compatibility version.
    Direct execution has a stable typed contract. Strategy plans additionally
    require the exact frozen strategy implementation still present in the
    current registry.
    """

    if isinstance(basis, FixedDirectExecutionBasis):
        return None
    try:
        definition = describe_strategy(basis.strategy_id)
    except KeyError:
        return "PLAN_STRATEGY_RUNTIME_UNAVAILABLE"
    if (
        definition.strategy_version != basis.strategy_version
        or definition.implementation_digest != basis.implementation_digest
        or definition.parameter_schema_version != basis.parameter_schema_version
        or definition.allowed_action_profiles != basis.allowed_action_profiles
    ):
        return "PLAN_STRATEGY_RUNTIME_INCOMPATIBLE"
    if content_digest(basis.normalized_parameters) != basis.parameter_digest:
        return "PLAN_STRATEGY_PARAMETERS_CORRUPT"
    try:
        normalized = validate_parameters(
            basis.strategy_id,
            basis.normalized_parameters,
        )
    except ValueError:
        return "PLAN_STRATEGY_PARAMETERS_INCOMPATIBLE"
    if normalized != basis.normalized_parameters:
        return "PLAN_STRATEGY_PARAMETERS_INCOMPATIBLE"
    return None


def build_fixed_plan_basis(
    strategy_id: str,
    parameters: dict[str, Any],
    *,
    product_build_id: str,
) -> FixedStrategyPlanBasis:
    definition = describe_strategy(strategy_id)
    normalized = validate_parameters(strategy_id, parameters)
    return FixedStrategyPlanBasis(
        decision_basis_ref=f"{definition.strategy_id}@{definition.strategy_version}",
        strategy_id=definition.strategy_id,
        strategy_version=definition.strategy_version,
        implementation_digest=definition.implementation_digest,
        parameter_schema_version=definition.parameter_schema_version,
        normalized_parameters=normalized,
        parameter_digest=content_digest(normalized),
        fact_input_contract={
            "source_1m": "1-MINUTE-LAST-EXTERNAL",
            "target_15m": "15-MINUTE-LAST-EXTERNAL",
            "closed_and_continuous": True,
            "risk_increase_freshness_seconds": 65,
        },
        allowed_action_profiles=definition.allowed_action_profiles,
        economic_scope=definition.economic_scope,
        product_build_id=product_build_id,
    )


def build_fixed_decision_basis(
    basis: DraftDecisionBasis,
    *,
    product_build_id: str,
) -> FixedDecisionBasis:
    if basis.kind is DecisionBasisKind.DIRECT_EXECUTION:
        return FixedDirectExecutionBasis(
            parameter_digest=content_digest({}),
            product_build_id=product_build_id,
        )
    return build_fixed_plan_basis(
        basis.decision_basis_ref,
        basis.parameters,
        product_build_id=product_build_id,
    )
