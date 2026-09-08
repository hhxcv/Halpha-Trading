import { Box, Chip, Divider, Stack, Typography } from "@mui/material";
import { memo, useEffect, useMemo, useState } from "react";

import { formatUserVisibleTime, quoteAmount, tradingPrice } from "../format";
import {
  isUsableMarketStreamDepth,
  isUsableMarketStreamTrade,
  type MarketStreamClientStatus,
  type MarketStreamDepth,
  type MarketStreamTrade,
} from "../marketStream";
import { surfaceFrameSx } from "../theme";
import {
  aggregateMarketStreamTrades,
  formatTapeVolume,
  marketFlowBook,
  MARKET_FLOW_TAPE_WINDOW_MS,
  tapeBubbleRadius,
  tapePriceDomain,
  type OrderBookDisplayLevel,
  type TapePrint,
} from "./marketFlowTapeModel";

type MarketFlowTapeProps = Readonly<{
  depth: MarketStreamDepth | null;
  trades: ReadonlyArray<MarketStreamTrade>;
  streamStatus: MarketStreamClientStatus;
  priceTickSize: string | null;
}>;

type PositionedTapeBubble = Readonly<{
  print: TapePrint;
  x: number;
  y: number;
  anchorY: number;
  radius: number;
  tone: "up" | "down";
  volumeLabel: string;
}>;

const TAPE_WIDTH = 320;
const TAPE_HEIGHT = 320;
const TAPE_PADDING = { left: 9, right: 46, top: 12, bottom: 18 };
const TAPE_TIME_FORMATTER = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

function currentFlowState(
  depth: MarketStreamDepth | null,
  trades: ReadonlyArray<MarketStreamTrade>,
  streamStatus: MarketStreamClientStatus,
  now: number,
): Readonly<{ depth: MarketStreamDepth | null; trades: ReadonlyArray<MarketStreamTrade> }> {
  if (streamStatus !== "LIVE") return { depth: null, trades: [] };
  const usableDepth = depth !== null && isUsableMarketStreamDepth(
    depth,
    depth.source,
    now,
  ) ? depth : null;
  return {
    depth: usableDepth,
    trades: trades.filter((trade) => isUsableMarketStreamTrade(
      trade,
      trade.source,
      now,
    )),
  };
}

function BookRow({
  level,
  side,
  priceTickSize,
}: Readonly<{
  level: OrderBookDisplayLevel;
  side: "ask" | "bid";
  priceTickSize: string | null;
}>) {
  const marketTone = side === "ask" ? "down" : "up";
  return (
    <Box
      sx={{
        position: "relative",
        display: "grid",
        gridTemplateColumns: "minmax(0, 1fr) auto",
        gap: .75,
        minHeight: 20,
        px: .5,
        alignItems: "center",
        overflow: "hidden",
      }}
    >
      <Box
        aria-hidden
        sx={{
          position: "absolute",
          inset: 0,
          width: `${Math.max(7, level.shareOfVisibleDepth * 100)}%`,
          ml: "auto",
          bgcolor: `color-mix(in srgb, var(--halpha-market-${marketTone}) 13%, transparent)`,
          pointerEvents: "none",
        }}
      />
      <Typography
        className={`mono market-tone-${marketTone}`}
        variant="caption"
        noWrap
        sx={{ position: "relative", minWidth: 0, fontSize: 10.5 }}
      >
        {tradingPrice(level.price, priceTickSize)}
      </Typography>
      <Typography
        className="mono"
        variant="caption"
        noWrap
        sx={{ position: "relative", fontSize: 10.5, color: "text.secondary" }}
      >
        {quoteAmount(level.quantity)}
      </Typography>
    </Box>
  );
}

function TapeBubbles({
  prints,
  depth,
  priceTickSize,
  now,
}: Readonly<{
  prints: ReadonlyArray<TapePrint>;
  depth: MarketStreamDepth | null;
  priceTickSize: string | null;
  now: number;
}>) {
  const domain = tapePriceDomain(prints, depth);
  if (prints.length === 0 || domain === null) {
    return (
      <Box
        role="status"
        sx={{ height: "100%", display: "grid", placeItems: "center", color: "text.secondary" }}
      >
        <Typography variant="caption">成交带等待当前数据</Typography>
      </Box>
    );
  }
  const [low, high] = domain;
  const maximumQuantity = Math.max(...prints.map((print) => print.quantity));
  const chartWidth = TAPE_WIDTH - TAPE_PADDING.left - TAPE_PADDING.right;
  const chartHeight = TAPE_HEIGHT - TAPE_PADDING.top - TAPE_PADDING.bottom;
  const xForTime = (at: number): number => (
    TAPE_PADDING.left + Math.max(0, Math.min(1, (
      at - (now - MARKET_FLOW_TAPE_WINDOW_MS)
    ) / MARKET_FLOW_TAPE_WINDOW_MS)) * chartWidth
  );
  const yForPrice = (price: string): number => (
    TAPE_PADDING.top + (1 - (Number(price) - low) / (high - low)) * chartHeight
  );
  const clampBubbleY = (value: number, radius: number): number => Math.max(
    TAPE_PADDING.top + radius,
    Math.min(TAPE_HEIGHT - TAPE_PADDING.bottom - radius, value),
  );
  const positionedPrints: PositionedTapeBubble[] = [];
  prints.forEach((print) => {
    const x = xForTime(print.at);
    const tone = print.aggressorSide === "BUYER" ? "up" : "down";
    const volumeLabel = formatTapeVolume(print.quantity);
    const labelRadius = Math.min(18, 3 + volumeLabel.length * 2.25);
    const radius = Math.max(
      tapeBubbleRadius(print.quantity, maximumQuantity),
      labelRadius,
    );
    const anchorY = clampBubbleY(yForPrice(print.price), radius);
    const direction = print.aggressorSide === "BUYER" ? -1 : 1;
    const preferredOffset = direction * Math.min(7, radius * .3);
    const step = Math.max(9, radius * 1.15);
    const candidates = [
      preferredOffset,
      0,
      preferredOffset + direction * step,
      preferredOffset - direction * step,
      preferredOffset + direction * step * 2,
      preferredOffset - direction * step * 2,
      preferredOffset + direction * step * 3,
      preferredOffset - direction * step * 3,
    ].map((offset) => clampBubbleY(anchorY + offset, radius));
    const y = candidates.find((candidate) => positionedPrints.every((placed) => (
      Math.hypot(x - placed.x, candidate - placed.y) >= radius + placed.radius + 1
    ))) ?? anchorY;
    positionedPrints.push({ print, x, y, anchorY, radius, tone, volumeLabel });
  });
  const gridPrices = [low, (low + high) / 2, high];
  return (
    <Box sx={{ minHeight: 0, height: "100%", overflow: "hidden" }}>
      <svg
        aria-label="当前聚合成交带，圆圈大小和圆圈内数字表示同价同向短时聚合成交量"
        role="img"
        viewBox={`0 0 ${TAPE_WIDTH} ${TAPE_HEIGHT}`}
        preserveAspectRatio="none"
        width="100%"
        height="100%"
      >
        {gridPrices.map((price) => {
          const y = yForPrice(String(price));
          return (
            <g key={price}>
              <line
                x1={TAPE_PADDING.left}
                x2={TAPE_WIDTH - TAPE_PADDING.right}
                y1={y}
                y2={y}
                stroke="currentColor"
                opacity=".10"
                strokeDasharray="2 3"
              />
              <text
                x={TAPE_WIDTH - 2}
                y={y + 3}
                textAnchor="end"
                fill="currentColor"
                opacity=".62"
                fontSize="9"
              >
                {tradingPrice(price, priceTickSize)}
              </text>
            </g>
          );
        })}
        <line
          x1={TAPE_PADDING.left}
          x2={TAPE_WIDTH - TAPE_PADDING.right}
          y1={TAPE_HEIGHT - TAPE_PADDING.bottom}
          y2={TAPE_HEIGHT - TAPE_PADDING.bottom}
          stroke="currentColor"
          opacity=".2"
        />
        {positionedPrints.map(({ print, x, y, anchorY, radius, tone, volumeLabel }) => {
          const fontSize = Math.max(6.5, Math.min(9, radius * .55));
          return (
            <g key={print.id}>
              <title>
                {`${TAPE_TIME_FORMATTER.format(new Date(print.at))} · ${
                  print.aggressorSide === "BUYER" ? "主买" : "主卖"
                } · ${tradingPrice(print.price, priceTickSize)} · ${formatTapeVolume(print.quantity)}`}
              </title>
              {Math.abs(y - anchorY) > 1 ? (
                <line
                  x1={x}
                  x2={x}
                  y1={anchorY}
                  y2={y}
                  stroke={`var(--halpha-market-${tone})`}
                  strokeOpacity=".42"
                  strokeWidth="1"
                />
              ) : null}
              <circle
                cx={x}
                cy={y}
                r={radius}
                fill={`var(--halpha-market-${tone})`}
                fillOpacity=".73"
                stroke={`var(--halpha-market-${tone})`}
                strokeOpacity=".96"
                strokeWidth="1"
              />
              <text
                x={x}
                y={y}
                textAnchor="middle"
                dominantBaseline="central"
                fill="var(--halpha-surface)"
                fontSize={fontSize}
                fontWeight="800"
                pointerEvents="none"
              >
                {volumeLabel}
              </text>
            </g>
          );
        })}
        <text
          x={TAPE_PADDING.left}
          y={TAPE_HEIGHT - 3}
          fill="currentColor"
          opacity=".62"
          fontSize="9"
        >
          {`-${MARKET_FLOW_TAPE_WINDOW_MS / 1_000}s`}
        </text>
        <text
          x={TAPE_WIDTH - TAPE_PADDING.right}
          y={TAPE_HEIGHT - 3}
          textAnchor="end"
          fill="currentColor"
          opacity=".62"
          fontSize="9"
        >
          当前
        </text>
      </svg>
    </Box>
  );
}

function MarketFlowTape({
  depth,
  trades,
  streamStatus,
  priceTickSize,
}: MarketFlowTapeProps) {
  const [, refreshClock] = useState(0);
  const now = Date.now();
  const flow = currentFlowState(depth, trades, streamStatus, now);
  const hasCurrentFlow = flow.depth !== null || flow.trades.length > 0;
  useEffect(() => {
    if (!hasCurrentFlow) return undefined;
    const timer = window.setInterval(() => refreshClock((tick) => tick + 1), 250);
    return () => window.clearInterval(timer);
  }, [hasCurrentFlow]);
  const prints = useMemo(
    () => aggregateMarketStreamTrades(flow.trades, now),
    [flow.trades, now],
  );
  const book = useMemo(() => marketFlowBook(flow.depth), [flow.depth]);
  const recentPrints = [...prints].reverse().slice(0, 10);
  const cutoff = flow.depth?.source_cutoff
    ?? flow.trades.at(-1)?.source_cutoff
    ?? null;

  return (
    <Box
      component="section"
      aria-labelledby="market-flow-title"
      sx={{
        ...surfaceFrameSx,
        height: "100%",
        minHeight: 0,
        p: { xs: 1.25, sm: 1.5 },
        display: "grid",
        gridTemplateRows: "auto minmax(0, 1fr)",
        overflow: "hidden",
        containerType: "inline-size",
      }}
    >
      <Stack
        direction="row"
        spacing={1}
        useFlexGap
        sx={{ minWidth: 0, pb: 1, alignItems: "center", justifyContent: "space-between" }}
      >
        <Stack direction="row" spacing={.75} sx={{ minWidth: 0, alignItems: "center" }}>
          <Typography id="market-flow-title" component="h2" variant="h3" noWrap>
            订单簿 / 成交带
          </Typography>
          <Chip size="small" variant="outlined" label="公开行情" sx={{ height: 22 }} />
        </Stack>
        <Typography variant="caption" color="text.secondary" noWrap>
          {cutoff ? `截止 ${formatUserVisibleTime(cutoff)}` : "等待当前数据"}
        </Typography>
      </Stack>

      <Box
        sx={{
          minHeight: 0,
          display: "grid",
          gridTemplateAreas: '"book" "tape" "detail"',
          gridTemplateColumns: "minmax(0, 1fr)",
          gridTemplateRows: "minmax(122px, .72fr) minmax(170px, 1fr) minmax(112px, .62fr)",
          gap: 1.25,
          "@container (min-width: 390px)": {
            gridTemplateAreas: '"book tape" "book detail"',
            gridTemplateColumns: "minmax(125px, .7fr) minmax(0, 1.3fr)",
            gridTemplateRows: "minmax(0, 1fr) minmax(108px, .55fr)",
            gap: 1.5,
          },
          "@container (min-width: 720px)": {
            gridTemplateAreas: '"book tape detail"',
            gridTemplateColumns: "minmax(145px, .8fr) minmax(0, 1.45fr) minmax(132px, .72fr)",
            gridTemplateRows: "minmax(0, 1fr)",
          },
        }}
      >
        <Box sx={{ gridArea: "book", minHeight: 0, display: "grid", gridTemplateRows: "auto minmax(0, 1fr)" }}>
          <Typography variant="overline" color="text.secondary">订单簿 · 十档</Typography>
          {flow.depth === null ? (
            <Box role="status" sx={{ display: "grid", placeItems: "center", color: "text.secondary" }}>
              <Typography variant="caption">订单簿等待当前数据</Typography>
            </Box>
          ) : (
            <Box sx={{ minHeight: 0, display: "grid", gridTemplateRows: "1fr auto 1fr", gap: .35 }}>
              <Stack spacing={.15} sx={{ justifyContent: "end" }}>
                {book.asks.map((level) => (
                  <BookRow key={`ask-${level.price}`} level={level} side="ask" priceTickSize={priceTickSize} />
                ))}
              </Stack>
              <Box sx={{ py: .35, borderBlock: 1, borderColor: "divider" }}>
                <Typography className="mono" variant="caption" sx={{ display: "block", textAlign: "center", fontSize: 10.5 }}>
                  {`${tradingPrice(flow.depth.bids[0]?.price ?? "—", priceTickSize)} / ${tradingPrice(flow.depth.asks[0]?.price ?? "—", priceTickSize)}`}
                </Typography>
              </Box>
              <Stack spacing={.15}>
                {book.bids.map((level) => (
                  <BookRow key={`bid-${level.price}`} level={level} side="bid" priceTickSize={priceTickSize} />
                ))}
              </Stack>
            </Box>
          )}
        </Box>

        <Box sx={{ gridArea: "tape", minWidth: 0, minHeight: 0, display: "grid", gridTemplateRows: "auto minmax(0, 1fr)" }}>
          <Stack direction="row" spacing={1} sx={{ alignItems: "center", justifyContent: "space-between" }}>
            <Typography variant="overline" color="text.secondary">聚合成交</Typography>
            <Typography variant="caption" color="text.secondary" noWrap>圆圈数字 = 成交量</Typography>
          </Stack>
          <TapeBubbles prints={prints} depth={flow.depth} priceTickSize={priceTickSize} now={now} />
        </Box>

        <Box sx={{ gridArea: "detail", minHeight: 0, display: "grid", gridTemplateRows: "auto minmax(0, 1fr)" }}>
          <Typography variant="overline" color="text.secondary">成交明细</Typography>
          {recentPrints.length === 0 ? (
            <Box role="status" sx={{ display: "grid", placeItems: "center", color: "text.secondary" }}>
              <Typography variant="caption">成交明细等待当前数据</Typography>
            </Box>
          ) : (
            <Stack divider={<Divider flexItem />} sx={{ minHeight: 0, overflow: "hidden" }}>
              {recentPrints.map((print) => {
                const tone = print.aggressorSide === "BUYER" ? "up" : "down";
                return (
                  <Box
                    key={print.id}
                    sx={{ display: "grid", gridTemplateColumns: "auto minmax(0, 1fr) auto", gap: .5, py: .28, alignItems: "center" }}
                  >
                    <Typography className="mono" variant="caption" color="text.secondary" sx={{ fontSize: 10 }} noWrap>
                      {TAPE_TIME_FORMATTER.format(new Date(print.at))}
                    </Typography>
                    <Typography className={`mono market-tone-${tone}`} variant="caption" sx={{ minWidth: 0, fontSize: 10.5 }} noWrap>
                      {tradingPrice(print.price, priceTickSize)}
                    </Typography>
                    <Typography className="mono" variant="caption" sx={{ fontSize: 10.5 }} noWrap>
                      {`${formatTapeVolume(print.quantity)}${print.tradeCount > 1 ? ` ×${print.tradeCount}` : ""}`}
                    </Typography>
                  </Box>
                );
              })}
            </Stack>
          )}
        </Box>
      </Box>
    </Box>
  );
}

export default memo(MarketFlowTape);
