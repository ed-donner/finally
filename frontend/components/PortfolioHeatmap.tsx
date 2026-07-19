'use client';

import { useMemo } from 'react';
import { ResponsiveContainer, Treemap } from 'recharts';
import type { Position } from '@/lib/types';
import { formatPercent } from '@/lib/format';

interface PortfolioHeatmapProps {
  positions: Position[];
}

/** Map a P&L % to a green/red fill whose intensity grows with magnitude. */
function plFill(pct: number): string {
  const mag = Math.min(Math.abs(pct) / 8, 1); // saturate at ±8%
  const alpha = 0.25 + mag * 0.6;
  return pct >= 0 ? `rgba(38,162,105,${alpha.toFixed(2)})` : `rgba(229,72,77,${alpha.toFixed(2)})`;
}

interface TreemapNodeProps {
  x: number;
  y: number;
  width: number;
  height: number;
  name?: string;
  pct?: number;
}

function HeatCell(props: TreemapNodeProps) {
  const { x, y, width, height, name, pct } = props;
  if (width <= 0 || height <= 0 || !name) return null;
  const showLabel = width > 44 && height > 28;
  const fill = plFill(pct ?? 0);

  return (
    <g data-testid="heatmap-cell" data-ticker={name}>
      <rect x={x} y={y} width={width} height={height} fill={fill} stroke="#0d1117" strokeWidth={2} rx={3} />
      {showLabel && (
        <>
          <text x={x + 6} y={y + 16} fill="#e6edf3" fontSize={12} fontWeight={600} fontFamily="monospace">
            {name}
          </text>
          <text x={x + 6} y={y + 30} fill="#e6edf3" fontSize={10} opacity={0.85} fontFamily="monospace">
            {formatPercent(pct ?? 0)}
          </text>
        </>
      )}
    </g>
  );
}

export default function PortfolioHeatmap({ positions }: PortfolioHeatmapProps) {
  const data = useMemo(
    () =>
      positions
        .filter((p) => p.market_value > 0)
        .map((p) => ({
          name: p.ticker,
          size: p.market_value,
          pct: p.unrealized_pl_pct,
        })),
    [positions],
  );

  return (
    <div className="panel h-full" data-testid="heatmap">
      <div className="panel-title">
        <span>Position Heatmap</span>
        <span className="text-terminal-muted/60 normal-case tracking-normal">sized by weight · colored by P&amp;L</span>
      </div>
      <div className="flex-1 min-h-0 p-2">
        {data.length === 0 ? (
          <div className="h-full flex items-center justify-center text-terminal-muted text-xs">
            No positions yet — buy something to populate the heatmap.
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <Treemap
              data={data}
              dataKey="size"
              stroke="#0d1117"
              isAnimationActive={false}
              content={<HeatCell x={0} y={0} width={0} height={0} />}
            />
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}
