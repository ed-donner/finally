"use client";

import { useEffect, useRef, useState } from "react";
import { useAppData } from "./AppDataProvider";
import { livePositions } from "@/lib/portfolio";
import { usePrices } from "@/lib/priceStore";
import { squarify } from "@/lib/treemap";
import { formatPercent } from "@/lib/format";

/** Background for a P&L percentage: deeper green/red as the move grows (saturates at ±5%). */
export function heatColor(pnlPercent: number): string {
  const t = Math.min(Math.abs(pnlPercent) / 5, 1);
  const alpha = (0.16 + 0.64 * t).toFixed(2);
  if (Math.abs(pnlPercent) < 0.005) return "rgba(124, 135, 150, 0.18)";
  return pnlPercent > 0 ? `rgba(46, 194, 126, ${alpha})` : `rgba(240, 80, 94, ${alpha})`;
}

export function Heatmap() {
  const { portfolio, select } = useAppData();
  const prices = usePrices();
  const box = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 400, h: 220 });

  useEffect(() => {
    const el = box.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      if (width > 0 && height > 0) setSize({ w: width, h: height });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const positions = livePositions(portfolio?.positions ?? [], prices);
  const byTicker = new Map(positions.map((p) => [p.ticker, p]));
  const rects = squarify(
    positions.map((p) => ({ key: p.ticker, value: p.market_value })),
    size.w,
    size.h,
  );

  return (
    <section data-testid="portfolio-heatmap" className="panel flex min-h-0 flex-col" aria-label="Portfolio heatmap">
      <div className="panel-head">
        <h2 className="panel-title">Holdings map</h2>
        <span className="text-[11px] text-dim">Size = weight · color = P&L</span>
      </div>
      <div ref={box} className="relative min-h-0 flex-1 m-1.5">
        {rects.map((r) => {
          const p = byTicker.get(r.key)!;
          const small = r.w < 56 || r.h < 34;
          return (
            <button
              type="button"
              key={r.key}
              data-testid={`heatmap-cell-${r.key}`}
              data-pnl={p.unrealized_pnl >= 0 ? "profit" : "loss"}
              onClick={() => select(r.key)}
              title={`${r.key}: ${(p.weight * 100).toFixed(1)}% of holdings, ${formatPercent(p.unrealized_pnl_percent)}`}
              className="heat-cell"
              style={{
                left: r.x,
                top: r.y,
                width: r.w,
                height: r.h,
                backgroundColor: heatColor(p.unrealized_pnl_percent),
              }}
            >
              <span className="font-semibold text-text">{r.key}</span>
              {!small && (
                <span className="num text-[11px] text-text/80">{formatPercent(p.unrealized_pnl_percent)}</span>
              )}
            </button>
          );
        })}
        {rects.length === 0 && (
          <div className="grid h-full place-items-center px-6 text-center text-xs text-muted">
            No holdings yet. Each position you buy appears here, sized by its share of the portfolio.
          </div>
        )}
      </div>
    </section>
  );
}
