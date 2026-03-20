"use client";

import { useEffect, useRef } from "react";
import type { Portfolio, PortfolioSnapshot } from "../lib/types";
import type { PriceMap } from "../hooks/usePriceStream";

interface PortfolioPanelProps {
  portfolio: Portfolio | null;
  history: PortfolioSnapshot[];
  prices: PriceMap;
  onSelectTicker: (ticker: string) => void;
}

function Treemap({ portfolio, onSelectTicker }: { portfolio: Portfolio; onSelectTicker: (t: string) => void }) {
  const positions = portfolio.positions;
  if (positions.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-xs" style={{ color: "var(--text-secondary)" }}>
        No open positions
      </div>
    );
  }

  const totalValue = positions.reduce((s, p) => s + p.market_value, 0) || 1;

  return (
    <div className="flex flex-wrap gap-1 p-1 h-full content-start">
      {positions.map((pos) => {
        const weight = pos.market_value / totalValue;
        const pct = pos.pnl_percent;
        const isPos = pct >= 0;
        const intensity = Math.min(Math.abs(pct) / 5, 1);
        const bg = isPos
          ? `rgba(63,185,80,${0.15 + intensity * 0.35})`
          : `rgba(248,81,73,${0.15 + intensity * 0.35})`;
        const border = isPos ? "#3fb950" : "#f85149";
        const minW = Math.max(60, weight * 200);

        return (
          <div
            key={pos.ticker}
            className="flex flex-col items-center justify-center rounded cursor-pointer transition-opacity hover:opacity-80"
            style={{
              backgroundColor: bg,
              border: `1px solid ${border}30`,
              minWidth: `${minW}px`,
              flex: `${weight} 1 ${minW}px`,
              minHeight: 52,
              padding: "4px 6px",
            }}
            onClick={() => onSelectTicker(pos.ticker)}
            title={`${pos.ticker}: $${pos.market_value.toFixed(2)} | P&L: ${pct.toFixed(2)}%`}
          >
            <span className="font-bold text-xs tracking-wide">{pos.ticker}</span>
            <span className="text-xs font-mono" style={{ color: isPos ? "var(--green)" : "var(--red)" }}>
              {isPos ? "+" : ""}{pct.toFixed(1)}%
            </span>
            <span className="text-xs" style={{ color: "var(--text-secondary)" }}>
              ${pos.market_value.toFixed(0)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function PnLChart({ history }: { history: PortfolioSnapshot[] }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container || history.length < 2) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    const { width, height } = container.getBoundingClientRect();
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    ctx.scale(dpr, dpr);

    ctx.clearRect(0, 0, width, height);

    const values = history.map((h) => h.total_value);
    const min = Math.min(...values) * 0.999;
    const max = Math.max(...values) * 1.001;
    const range = max - min || 1;

    const padL = 56, padR = 8, padT = 8, padB = 20;
    const w = width - padL - padR;
    const h = height - padT - padB;

    const getX = (i: number) => padL + (i / (values.length - 1)) * w;
    const getY = (v: number) => padT + h - ((v - min) / range) * h;

    const baseline = 10000;
    const current = values[values.length - 1];
    const trend = current >= baseline;
    const lineColor = trend ? "#3fb950" : "#f85149";

    // Grid
    ctx.strokeStyle = "rgba(48,54,61,0.5)";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 3; i++) {
      const y = padT + (h / 3) * i;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(width - padR, y);
      ctx.stroke();
      const v = max - (range / 3) * i;
      ctx.fillStyle = "rgba(139,148,158,0.7)";
      ctx.font = "9px ui-monospace, monospace";
      ctx.textAlign = "right";
      ctx.fillText(`$${v.toFixed(0)}`, padL - 4, y + 3);
    }

    // Baseline $10k line
    const baseY = getY(baseline);
    if (baseY >= padT && baseY <= padT + h) {
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = "rgba(236,173,10,0.4)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padL, baseY);
      ctx.lineTo(width - padR, baseY);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // Fill
    ctx.beginPath();
    ctx.moveTo(getX(0), getY(values[0]));
    for (let i = 1; i < values.length; i++) {
      ctx.lineTo(getX(i), getY(values[i]));
    }
    ctx.lineTo(getX(values.length - 1), padT + h);
    ctx.lineTo(getX(0), padT + h);
    ctx.closePath();
    ctx.fillStyle = trend ? "rgba(63,185,80,0.08)" : "rgba(248,81,73,0.08)";
    ctx.fill();

    // Line
    ctx.beginPath();
    ctx.moveTo(getX(0), getY(values[0]));
    for (let i = 1; i < values.length; i++) {
      ctx.lineTo(getX(i), getY(values[i]));
    }
    ctx.strokeStyle = lineColor;
    ctx.lineWidth = 1.5;
    ctx.lineJoin = "round";
    ctx.stroke();
  }, [history]);

  if (history.length < 2) {
    return (
      <div className="flex items-center justify-center h-full text-xs" style={{ color: "var(--text-secondary)" }}>
        Collecting data…
      </div>
    );
  }

  return (
    <div ref={containerRef} className="relative w-full h-full">
      <canvas ref={canvasRef} className="absolute inset-0 w-full h-full" />
    </div>
  );
}

export function PortfolioPanel({ portfolio, history, prices, onSelectTicker }: PortfolioPanelProps) {
  if (!portfolio) return null;

  const totalPnl = portfolio.total_value - 10000;
  const pnlPct = (totalPnl / 10000) * 100;

  return (
    <div className="flex flex-col h-full gap-0">
      {/* Treemap header */}
      <div
        className="px-3 py-1.5 flex-shrink-0 flex items-center justify-between"
        style={{ borderBottom: "1px solid var(--border-color)", backgroundColor: "var(--bg-surface)" }}
      >
        <span className="text-xs font-semibold tracking-widest uppercase" style={{ color: "var(--text-secondary)" }}>
          Positions
        </span>
        <span
          className="text-xs font-mono font-semibold"
          style={{ color: totalPnl >= 0 ? "var(--green)" : "var(--red)" }}
        >
          {totalPnl >= 0 ? "+" : ""}${totalPnl.toFixed(2)} ({pnlPct >= 0 ? "+" : ""}{pnlPct.toFixed(2)}%)
        </span>
      </div>

      {/* Treemap */}
      <div className="flex-1 overflow-hidden" style={{ minHeight: 80 }}>
        <Treemap portfolio={portfolio} onSelectTicker={onSelectTicker} />
      </div>

      {/* P&L chart */}
      <div
        className="flex-shrink-0"
        style={{ height: 90, borderTop: "1px solid var(--border-color)" }}
      >
        <div
          className="px-3 py-1 flex items-center"
          style={{ borderBottom: "1px solid var(--border-color)", backgroundColor: "var(--bg-surface)" }}
        >
          <span className="text-xs font-semibold tracking-widest uppercase" style={{ color: "var(--text-secondary)" }}>
            Portfolio Value
          </span>
        </div>
        <div style={{ height: 66 }}>
          <PnLChart history={history} />
        </div>
      </div>
    </div>
  );
}
