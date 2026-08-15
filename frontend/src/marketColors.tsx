import { Box } from "@mui/material";
import type { ReactNode } from "react";

export type MarketColorScheme = "RED_DOWN_GREEN_UP" | "RED_UP_GREEN_DOWN";
export type MarketTone = "up" | "down";
export type FinancialTone = "profit" | "loss";

export const DEFAULT_MARKET_COLOR_SCHEME: MarketColorScheme = "RED_DOWN_GREEN_UP";
const MARKET_COLOR_STORAGE_KEY = "halpha.market-color-scheme.v1";

export function readMarketColorScheme(): MarketColorScheme {
  if (typeof window === "undefined") return DEFAULT_MARKET_COLOR_SCHEME;
  try {
    const stored = window.localStorage.getItem(MARKET_COLOR_STORAGE_KEY);
    return stored === "RED_UP_GREEN_DOWN" ? stored : DEFAULT_MARKET_COLOR_SCHEME;
  } catch {
    return DEFAULT_MARKET_COLOR_SCHEME;
  }
}

export function saveMarketColorScheme(scheme: MarketColorScheme): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(MARKET_COLOR_STORAGE_KEY, scheme);
  } catch {
    // A blocked preference store must not prevent use of the workbench.
  }
}

export function applyMarketColorScheme(scheme: MarketColorScheme): void {
  if (typeof document === "undefined") return;
  document.documentElement.dataset.halphaMarketColorScheme = scheme;
}

export function marketToneForDirection(direction: unknown): MarketTone | undefined {
  if (direction === "LONG") return "up";
  if (direction === "SHORT") return "down";
  return undefined;
}

/** Financial signs are stable; their color follows the active K-line palette. */
export function financialToneForSignedValue(value: unknown): FinancialTone | undefined {
  const amount = Number(value);
  if (!Number.isFinite(amount) || amount === 0) return undefined;
  return amount > 0 ? "profit" : "loss";
}

export function marketToneClassName(tone: MarketTone | undefined): string | undefined {
  return tone ? `market-tone-${tone}` : undefined;
}

export function financialToneClassName(tone: FinancialTone | undefined): string | undefined {
  return tone === "profit"
    ? "market-tone-up"
    : tone === "loss"
      ? "market-tone-down"
      : undefined;
}

export function MarketToneText({ tone, children }: { tone: MarketTone | undefined; children: ReactNode }) {
  return <Box component="span" className={marketToneClassName(tone)}>{children}</Box>;
}

export function FinancialToneText({ tone, children }: { tone: FinancialTone | undefined; children: ReactNode }) {
  return <Box component="span" className={financialToneClassName(tone)}>{children}</Box>;
}
