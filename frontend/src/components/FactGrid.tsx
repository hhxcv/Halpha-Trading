import { Box, Typography } from "@mui/material";
import type { ReactNode } from "react";

import { financialToneClassName, marketToneClassName, type FinancialTone, type MarketTone } from "../marketColors";

export type FactGridItem = {
  label: string;
  value: ReactNode;
  note?: string;
  tone?: MarketTone | FinancialTone;
};

export default function FactGrid({
  facts,
  columns = 2,
  dense = false,
}: {
  facts: FactGridItem[];
  columns?: 2 | 3;
  dense?: boolean;
}) {
  const remainder = facts.length % columns;

  return (
    <Box
      component="dl"
      sx={{
        m: 0,
        display: "grid",
        gridTemplateColumns: { xs: "1fr", sm: `repeat(${columns}, minmax(0, 1fr))` },
        gap: "1px",
        overflow: "hidden",
        border: 1,
        borderColor: "divider",
        borderRadius: "14px",
        bgcolor: "divider",
      }}
    >
      {facts.map((fact, index) => {
        const last = index === facts.length - 1;
        const toneClassName = fact.tone === "up" || fact.tone === "down"
          ? marketToneClassName(fact.tone)
          : financialToneClassName(fact.tone);
        const lastColumnSpan = last && remainder > 0
          ? columns - remainder + 1
          : 1;
        return (
          <Box
            key={`${fact.label}:${index}`}
            sx={{
              minWidth: 0,
              p: dense ? 1.5 : 2,
              bgcolor: "background.paper",
              gridColumn: { sm: lastColumnSpan > 1 ? `span ${lastColumnSpan}` : "auto" },
            }}
          >
            <Typography component="dt" variant="caption" color="text.secondary">{fact.label}</Typography>
            <Box component="dd" sx={{ m: 0, mt: dense ? .5 : .75 }}>
              <Typography component="span" className={`mono ${toneClassName ?? ""}`} sx={{ display: "block", fontSize: 13, fontWeight: 650, overflowWrap: "anywhere" }}>
                {fact.value}
              </Typography>
              {fact.note && (
                <Typography component="span" variant="caption" color="text.secondary" sx={{ display: "block", mt: .75 }}>
                  {fact.note}
                </Typography>
              )}
            </Box>
          </Box>
        );
      })}
    </Box>
  );
}
