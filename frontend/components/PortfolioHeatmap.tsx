"use client";

import { ResponsiveContainer, Treemap } from "recharts";
import { fmtPct } from "@/utils/format";
import type { Portfolio } from "@/utils/types";
import { Panel, PanelMessage } from "./Panel";

/** P&L % at which the colour is fully saturated. */
const SATURATION_PCT = 5;
const NEUTRAL = [33, 38, 45]; // #21262d
const UP = [63, 185, 80]; // #3fb950
const DOWN = [248, 81, 73]; // #f85149

/** Blend from neutral toward green (profit) or red (loss) by |pnl%|, saturating at +/-5%. */
function pnlColor(pnlPct: number): string {
  const t = Math.min(Math.abs(pnlPct) / SATURATION_PCT, 1);
  const target = pnlPct >= 0 ? UP : DOWN;
  const [r, g, b] = NEUTRAL.map((n, i) => Math.round(n + (target[i] - n) * t * 0.85));
  return `rgb(${r},${g},${b})`;
}

interface CellProps {
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  name?: string;
  pnl?: number;
  depth?: number;
}

/** One treemap rectangle: sized by the layout, coloured by P&L, labelled if there is room. */
function HeatCell({ x = 0, y = 0, width = 0, height = 0, name, pnl = 0, depth }: CellProps) {
  if (depth === 0) return null; // the treemap's invisible root
  return (
    <g data-testid={`heatmap-cell-${name}`} data-pnl={pnl}>
      <rect x={x} y={y} width={width} height={height} fill={pnlColor(pnl)} stroke="#0d1117" strokeWidth={2} rx={3} />
      {width > 46 && height > 34 && (
        <>
          <text x={x + 8} y={y + 18} fill="#fff" fontSize={13} fontWeight={700}>
            {name}
          </text>
          <text x={x + 8} y={y + 34} fill="#fff" fillOpacity={0.85} fontSize={11}>
            {fmtPct(pnl)}
          </text>
        </>
      )}
    </g>
  );
}

/** Treemap of positions: area = portfolio weight (market value), colour = unrealized P&L %. */
export function PortfolioHeatmap({ portfolio, loading }: { portfolio: Portfolio | null; loading: boolean }) {
  const data = (portfolio?.positions ?? [])
    .filter((p) => p.market_value > 0)
    .map((p) => ({ name: p.ticker, size: p.market_value, pnl: p.pnl_percent }));

  return (
    <Panel title="Portfolio heatmap" right={<span className="text-[11px] text-muted">size = weight · colour = P&L</span>} bodyClassName="relative">
      {loading ? (
        <PanelMessage>Loading portfolio…</PanelMessage>
      ) : !data.length ? (
        <PanelMessage>No positions yet. Place a trade to see your portfolio here.</PanelMessage>
      ) : (
        <div className="absolute inset-0 p-1" data-testid="heatmap">
          <ResponsiveContainer width="100%" height="100%">
            <Treemap data={data} dataKey="size" isAnimationActive={false} content={<HeatCell />} />
          </ResponsiveContainer>
        </div>
      )}
    </Panel>
  );
}
