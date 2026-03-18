"use client";

import { useMemo } from "react";
import { Treemap, ResponsiveContainer, Tooltip } from "recharts";
import { useStore } from "@/lib/store";
import { formatPct } from "@/lib/format";

interface TreemapContentProps {
  x: number;
  y: number;
  width: number;
  height: number;
  name: string;
  pnl_pct: number;
}

function CustomContent({ x, y, width, height, name, pnl_pct }: TreemapContentProps) {
  const color =
    pnl_pct >= 0
      ? `rgba(63, 185, 80, ${Math.min(0.2 + Math.abs(pnl_pct) * 0.03, 0.8)})`
      : `rgba(248, 81, 73, ${Math.min(0.2 + Math.abs(pnl_pct) * 0.03, 0.8)})`;

  if (width < 30 || height < 20) return null;

  return (
    <g>
      <rect
        x={x}
        y={y}
        width={width}
        height={height}
        fill={color}
        stroke="#0d1117"
        strokeWidth={2}
        rx={2}
      />
      {width > 40 && height > 30 && (
        <>
          <text
            x={x + width / 2}
            y={y + height / 2 - 6}
            textAnchor="middle"
            fill="#e6edf3"
            fontSize={11}
            fontWeight="bold"
            fontFamily="monospace"
          >
            {name}
          </text>
          <text
            x={x + width / 2}
            y={y + height / 2 + 10}
            textAnchor="middle"
            fill="#e6edf3"
            fontSize={9}
            fontFamily="monospace"
          >
            {formatPct(pnl_pct)}
          </text>
        </>
      )}
    </g>
  );
}

export function PortfolioHeatmap() {
  const portfolio = useStore((s) => s.portfolio);

  const data = useMemo(() => {
    if (!portfolio?.positions.length) return [];
    return portfolio.positions.map((p) => ({
      name: p.ticker,
      size: Math.max(p.market_value, 1),
      pnl_pct: p.pnl_pct,
      unrealized_pnl: p.unrealized_pnl,
      market_value: p.market_value,
    }));
  }, [portfolio]);

  if (!data.length) {
    return (
      <div className="flex items-center justify-center h-full text-text-secondary text-xs">
        No positions yet
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height="100%">
      <Treemap
        data={data}
        dataKey="size"
        aspectRatio={4 / 3}
        content={<CustomContent x={0} y={0} width={0} height={0} name="" pnl_pct={0} />}
      >
        <Tooltip
          contentStyle={{
            background: "#161b22",
            border: "1px solid #30363d",
            borderRadius: 4,
            fontSize: 11,
            color: "#e6edf3",
          }}
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          formatter={((value: any, name: any) => [`${Number(value).toFixed(0)}`, name]) as any}
        />
      </Treemap>
    </ResponsiveContainer>
  );
}
