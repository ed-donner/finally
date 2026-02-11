"use client";

import { ResponsiveContainer, Treemap } from "recharts";
import type { Position } from "@/stores/portfolio-store";

function pnlColor(pnlPercent: number): string {
  const clamped = Math.max(-10, Math.min(10, pnlPercent));
  const t = (clamped + 10) / 20; // 0 = red, 0.5 = neutral, 1 = green
  let r: number, g: number, b: number;
  if (t < 0.5) {
    const s = t / 0.5;
    r = Math.round(239 + s * (72 - 239));
    g = Math.round(68 + s * (79 - 68));
    b = Math.round(68 + s * (88 - 68));
  } else {
    const s = (t - 0.5) / 0.5;
    r = Math.round(72 + s * (34 - 72));
    g = Math.round(79 + s * (197 - 79));
    b = Math.round(88 + s * (94 - 88));
  }
  return `rgb(${r},${g},${b})`;
}

function CustomContent(props: any) {
  const { x, y, width, height, name, pnlPercent, pnl } = props;
  if (width < 2 || height < 2) return null;
  return (
    <g>
      <rect
        x={x}
        y={y}
        width={width}
        height={height}
        fill={pnlColor(pnlPercent)}
        stroke="#2d2d44"
        strokeWidth={1}
      />
      {width > 40 && height > 30 && (
        <>
          <text
            x={x + width / 2}
            y={y + height / 2 - 6}
            textAnchor="middle"
            fill="#e6edf3"
            fontSize={12}
            fontFamily="JetBrains Mono, monospace"
          >
            {name}
          </text>
          <text
            x={x + width / 2}
            y={y + height / 2 + 10}
            textAnchor="middle"
            fill="#e6edf3"
            fontSize={10}
            fontFamily="JetBrains Mono, monospace"
          >
            {pnl >= 0 ? "+" : ""}
            {pnl.toFixed(2)}%
          </text>
        </>
      )}
    </g>
  );
}

export function Heatmap({ positions }: { positions: Position[] }) {
  const data = positions
    .filter((p) => p.market_value > 0)
    .map((p) => ({
      name: p.ticker,
      size: p.market_value,
      pnlPercent: p.unrealized_pnl_percent,
      pnl: p.unrealized_pnl_percent,
    }));

  if (data.length === 0) {
    return (
      <div className="h-full w-full flex items-center justify-center">
        <span className="text-text-muted font-mono text-xs">
          No positions
        </span>
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height="100%">
      <Treemap
        data={data}
        dataKey="size"
        content={<CustomContent />}
        isAnimationActive={false}
      />
    </ResponsiveContainer>
  );
}
