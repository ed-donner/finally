"use client";

import { hierarchy, treemap } from "d3-hierarchy";
import { useEffect, useMemo, useRef, useState } from "react";

import { Panel } from "@/components/ui/panel";
import { cn } from "@/lib/cn";
import { pct, usdShort } from "@/lib/format";
import { usePortfolio } from "@/lib/store/portfolio";

/** Map a P&L percent to a color along the red→neutral→green axis. */
function pnlColor(pnlPct: number): string {
  const clamped = Math.max(-10, Math.min(10, pnlPct));
  const t = clamped / 10; // -1..1
  if (t > 0) {
    // green ramp
    const alpha = 0.18 + Math.abs(t) * 0.55;
    return `rgba(63, 185, 80, ${alpha.toFixed(3)})`;
  }
  if (t < 0) {
    const alpha = 0.18 + Math.abs(t) * 0.55;
    return `rgba(248, 81, 73, ${alpha.toFixed(3)})`;
  }
  return "rgba(110,118,129,0.20)";
}

export function HeatmapPanel() {
  const { portfolio } = usePortfolio();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });

  useEffect(() => {
    if (!containerRef.current) return;
    const ro = new ResizeObserver((entries) => {
      const { width, height } = entries[0].contentRect;
      setSize({ width, height });
    });
    ro.observe(containerRef.current);
    return () => ro.disconnect();
  }, []);

  const cells = useMemo(() => {
    if (!portfolio || portfolio.positions.length === 0 || size.width === 0) return [];
    const root = hierarchy({
      children: portfolio.positions.map((p) => ({
        ticker: p.ticker,
        value: Math.max(p.market_value, 1),
        pnl_pct: p.unrealized_pnl_percent,
        pnl: p.unrealized_pnl,
      })),
    } as never)
      .sum((d: { value?: number }) => d.value ?? 0)
      .sort((a, b) => (b.value ?? 0) - (a.value ?? 0));

    const layout = treemap<{
      ticker: string;
      value: number;
      pnl_pct: number;
      pnl: number;
    }>()
      .size([size.width, size.height])
      .paddingInner(2)
      .round(true);

    const computed = layout(root as never);
    return computed.leaves();
  }, [portfolio, size]);

  return (
    <Panel
      title="Allocation"
      accent={portfolio ? `${portfolio.positions.length} pos` : undefined}
    >
      <div ref={containerRef} className="relative w-full h-full min-h-[200px]">
        {cells.length === 0 ? (
          <div className="absolute inset-0 grid place-items-center text-dim text-xs uppercase tracking-[0.3em]">
            no positions yet
          </div>
        ) : (
          cells.map((cell) => {
            const d = cell.data as unknown as {
              ticker: string;
              value: number;
              pnl_pct: number;
              pnl: number;
            };
            const w = cell.x1 - cell.x0;
            const h = cell.y1 - cell.y0;
            const big = w >= 80 && h >= 50;
            return (
              <div
                key={d.ticker}
                style={{
                  left: cell.x0,
                  top: cell.y0,
                  width: w,
                  height: h,
                  background: pnlColor(d.pnl_pct),
                }}
                className={cn(
                  "absolute flex flex-col justify-between p-2 border border-line/60",
                  "transition-transform hover:z-10 hover:scale-[1.015]",
                )}
              >
                <div className="flex items-baseline justify-between gap-2">
                  <span className="text-xs text-ink tracking-wider">{d.ticker}</span>
                  {big && (
                    <span
                      className={cn(
                        "text-[10px] tabular-nums",
                        d.pnl_pct > 0 ? "text-up" : d.pnl_pct < 0 ? "text-down" : "text-dim",
                      )}
                    >
                      {pct(d.pnl_pct)}
                    </span>
                  )}
                </div>
                {big && (
                  <div className="flex items-baseline justify-between text-[10px] text-muted tabular-nums">
                    <span>{usdShort(d.value)}</span>
                    <span className={d.pnl >= 0 ? "text-up/80" : "text-down/80"}>
                      {d.pnl >= 0 ? "+" : ""}
                      {usdShort(d.pnl)}
                    </span>
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    </Panel>
  );
}
