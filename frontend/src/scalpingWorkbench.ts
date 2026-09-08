import type {
  OrderScheduleSpec,
  ScalpTemplate,
} from "./api/client";

export const DEFAULT_SCALP_TEMPLATE: ScalpTemplate = {
  schema_version: "HALPHA_SCALP_TEMPLATE_V1",
  notional: "100",
  entry_mode: "MARKET",
  initial_stop_bps: "35",
  take_profit_r: "1.5",
  max_holding_seconds: 300,
  max_spread_bps: "5",
  entry_fee_bps: "6",
  exit_fee_bps: "6",
};

const TEMPLATE_STORAGE_PREFIX = "halpha.scalp-template.v1";
const INSTRUMENT_STORAGE_PREFIX = "halpha.scalp-instrument.v1";
const ANCHOR_STORAGE_PREFIX = "halpha.scalp-anchor.v1";

type PreferenceStorage = Pick<Storage, "getItem" | "setItem">;

type StoredTemplate = Readonly<{
  version: 1;
  template: ScalpTemplate;
}>;

export type ScalpTemplateField = Exclude<keyof ScalpTemplate, "schema_version">;
export type ScalpTemplateErrors = Partial<Record<ScalpTemplateField, string>>;

function preferenceKey(prefix: string, environmentId: string, accountId: string): string {
  return `${prefix}:${encodeURIComponent(environmentId)}:${encodeURIComponent(accountId)}`;
}

function browserStorage(): PreferenceStorage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function finiteDecimal(value: string): number | null {
  const trimmed = value.trim();
  if (!/^(?:0|[1-9]\d*)(?:\.\d+)?$/u.test(trimmed)) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

function boundedDecimal(
  value: string,
  minimum: number,
  maximum: number,
): boolean {
  const parsed = finiteDecimal(value);
  return parsed !== null && parsed >= minimum && parsed <= maximum;
}

export function validateScalpTemplate(template: ScalpTemplate): ScalpTemplateErrors {
  const errors: ScalpTemplateErrors = {};
  if (template.entry_mode !== "MARKET" && template.entry_mode !== "MAKER_ONLY_SAME_SIDE") {
    errors.entry_mode = "请选择支持的入场方式";
  }
  if (!boundedDecimal(template.notional, Number.MIN_VALUE, 100_000_000)) {
    errors.notional = "输入大于 0 的名义金额";
  }
  if (!boundedDecimal(template.initial_stop_bps, 1, 5_000)) {
    errors.initial_stop_bps = "范围 1–5000 bps";
  }
  if (!boundedDecimal(template.take_profit_r, 0.1, 20)) {
    errors.take_profit_r = "范围 0.1–20 R";
  }
  if (
    !Number.isInteger(template.max_holding_seconds)
    || template.max_holding_seconds < 30
    || template.max_holding_seconds > 86_400
  ) {
    errors.max_holding_seconds = "范围 30–86400 秒";
  }
  if (!boundedDecimal(template.max_spread_bps, 0, 1_000)) {
    errors.max_spread_bps = "范围 0–1000 bps";
  }
  if (!boundedDecimal(template.entry_fee_bps, 0, 1_000)) {
    errors.entry_fee_bps = "范围 0–1000 bps";
  }
  if (!boundedDecimal(template.exit_fee_bps, 0, 1_000)) {
    errors.exit_fee_bps = "范围 0–1000 bps";
  }
  return errors;
}

function isScalpTemplate(value: unknown): value is ScalpTemplate {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const item = value as Record<string, unknown>;
  const template: ScalpTemplate = {
    schema_version: item.schema_version as ScalpTemplate["schema_version"],
    notional: item.notional as string,
    entry_mode: (item.entry_mode ?? "MARKET") as ScalpTemplate["entry_mode"],
    initial_stop_bps: item.initial_stop_bps as string,
    take_profit_r: item.take_profit_r as string,
    max_holding_seconds: item.max_holding_seconds as number,
    max_spread_bps: item.max_spread_bps as string,
    entry_fee_bps: item.entry_fee_bps as string,
    exit_fee_bps: item.exit_fee_bps as string,
  };
  return template.schema_version === "HALPHA_SCALP_TEMPLATE_V1"
    && Object.entries(template).every(([key, field]) => (
      key === "max_holding_seconds"
        ? typeof field === "number"
        : typeof field === "string"
    ))
    && Object.keys(validateScalpTemplate(template)).length === 0;
}

export function readScalpTemplate(
  environmentId: string,
  accountId: string,
  storage: PreferenceStorage | null = browserStorage(),
): ScalpTemplate | null {
  if (storage === null) return null;
  try {
    const raw = storage.getItem(
      preferenceKey(TEMPLATE_STORAGE_PREFIX, environmentId, accountId),
    );
    if (!raw) return null;
    const stored: unknown = JSON.parse(raw);
    if (
      typeof stored !== "object"
      || stored === null
      || Array.isArray(stored)
      || (stored as Record<string, unknown>).version !== 1
      || !isScalpTemplate((stored as Record<string, unknown>).template)
    ) {
      return null;
    }
    const template = (stored as StoredTemplate).template;
    return { ...template, entry_mode: template.entry_mode ?? "MARKET" };
  } catch {
    return null;
  }
}

export function saveScalpTemplate(
  environmentId: string,
  accountId: string,
  template: ScalpTemplate,
  storage: PreferenceStorage | null = browserStorage(),
): boolean {
  if (storage === null || Object.keys(validateScalpTemplate(template)).length > 0) {
    return false;
  }
  try {
    storage.setItem(
      preferenceKey(TEMPLATE_STORAGE_PREFIX, environmentId, accountId),
      JSON.stringify({ version: 1, template } satisfies StoredTemplate),
    );
    return true;
  } catch {
    return false;
  }
}

export function readScalpInstrument(
  environmentId: string,
  accountId: string,
  storage: PreferenceStorage | null = browserStorage(),
): string | null {
  if (storage === null) return null;
  try {
    return storage.getItem(
      preferenceKey(INSTRUMENT_STORAGE_PREFIX, environmentId, accountId),
    );
  } catch {
    return null;
  }
}

export function saveScalpInstrument(
  environmentId: string,
  accountId: string,
  instrumentRef: string,
  storage: PreferenceStorage | null = browserStorage(),
): void {
  if (storage === null) return;
  try {
    storage.setItem(
      preferenceKey(INSTRUMENT_STORAGE_PREFIX, environmentId, accountId),
      instrumentRef,
    );
  } catch {
    // A blocked preference store must not prevent contract selection.
  }
}

export function readScalpAnchor(
  environmentId: string,
  accountId: string,
  storage: PreferenceStorage | null = browserStorage(),
): string | null {
  if (storage === null) return null;
  try {
    const value = storage.getItem(
      preferenceKey(ANCHOR_STORAGE_PREFIX, environmentId, accountId),
    );
    return value && Number.isFinite(Date.parse(value)) ? value : null;
  } catch {
    return null;
  }
}

export function saveScalpAnchor(
  environmentId: string,
  accountId: string,
  anchorAt: string,
  storage: PreferenceStorage | null = browserStorage(),
): void {
  if (storage === null || !Number.isFinite(Date.parse(anchorAt))) return;
  try {
    storage.setItem(
      preferenceKey(ANCHOR_STORAGE_PREFIX, environmentId, accountId),
      anchorAt,
    );
  } catch {
    // A blocked preference store must not prevent range selection.
  }
}

export function buildScalpScheduleSpec(
  template: ScalpTemplate,
  makerLimitPrice: string | null = null,
): OrderScheduleSpec {
  const makerOnly = template.entry_mode === "MAKER_ONLY_SAME_SIDE";
  return {
    entry_program: {
      kind: "ONE_TIME",
      slice_count: 1,
      first_slice_delay_seconds: 0,
      slice_interval_seconds: 0,
    },
    price_distribution: {
      kind: "SINGLE",
      limit_price: makerOnly ? makerLimitPrice : null,
    },
    amount_distribution: {
      mode: "FIXED",
      direction: "LOW_TO_HIGH",
      base_notional: template.notional,
      linear_step: "0",
      exponential_ratio: "1",
      custom_notionals: [],
    },
    venue_policy: {
      order_type: makerOnly ? "LIMIT" : "MARKET",
      time_in_force: makerOnly ? "GTC" : null,
      post_only: makerOnly,
      price_match: null,
      expire_at: null,
    },
    submission_mode: "SERIAL_PROTECTED",
    submission_order: "LOW_TO_HIGH",
    entry_conditions: {
      operator: "ALL",
      items: [
        { kind: "DECISION_BASIS_READY" },
        { kind: "SPREAD_BPS", maximum_bps: template.max_spread_bps },
      ],
    },
    protection_policy: {
      initial_stop: {
        distance_bps: template.initial_stop_bps,
        trigger_source: "MARK_PRICE",
        coverage: "EACH_CONFIRMED_FILL",
      },
      full_fill_loss_budget: {
        entry_fee_bps: template.entry_fee_bps,
        exit_fee_bps: template.exit_fee_bps,
      },
      take_profit_ladder: {
        levels: [{ trigger_r: template.take_profit_r, quantity_fraction: "1" }],
      },
      time_exit_seconds: template.max_holding_seconds,
    },
    dynamic_rules: [],
  };
}
