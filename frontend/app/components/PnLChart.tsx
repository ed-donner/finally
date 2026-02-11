"use client";

import { useEffect, useState } from "react";
import { PortfolioSnapshot } from "../lib/types";
import { fetchPortfolioHistory } from "../lib/api";

export default function PnLChart() {
  const [snapshots, setSnapshots] = useState<PortfolioSnapshot[]>([]);

  useEffect(() => {
    const load = () => {
      fetchPortfolioHistory().then(setSnapshots).catch(() => {});
    };
    load();
    const interval = setInterval(load, 10000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="flex flex-col h-full">
      <div className="px-3 py-2 border-b border-border">
        <span className="text-accent-yellow font-bold text-xs tracking-wider uppercase">P&L</span>
      </div>
      <div className="flex-1 p-2">
        <PnLCanvas snapshots={snapshots} />
      </div>
    </div>
  );
}

function PnLCanvas({ snapshots }: { snapshots: PortfolioSnapshot[] }) {
  const canvasRef = (el: HTMLCanvasElement | null) => {
    if (!el) return;
    const ctx = el.getContext("2d");
    if (!ctx) return;

    const rect = el.getBoundingClientRect();
    el.width = rect.width * 2;
    el.height = rect.height * 2;
    ctx.scale(2, 2);

    const w = rect.width;
    const h = rect.height;

    ctx.fillStyle = "#0d1117";
    ctx.fillRect(0, 0, w, h);

    if (snapshots.length < 2) {
      ctx.fillStyle = "#8b949e";
      ctx.font = "11px monospace";
      ctx.textAlign = "center";
      ctx.fillText("P&L chart populates over time", w / 2, h / 2);
      return;
    }

    const values = snapshots.map((s) => s.total_value);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min || 1;
    const pad = 8;

    // Base line at 10000
    const baseY = pad + ((max - 10000) / range) * (h - 2 * pad);
    ctx.strokeStyle = "#30363d";
    ctx.lineWidth = 0.5;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(0, baseY);
    ctx.lineTo(w, baseY);
    ctx.stroke();
    ctx.setLineDash([]);

    // Draw line
    const lastVal = values[values.length - 1];
    const isUp = lastVal >= 10000;
    const lineColor = isUp ? "#3fb950" : "#f85149";

    ctx.beginPath();
    values.forEach((v, i) => {
      const x = pad + (i / (values.length - 1)) * (w - 2 * pad);
      const y = pad + ((max - v) / range) * (h - 2 * pad);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = lineColor;
    ctx.lineWidth = 1.5;
    ctx.stroke();
  };

  return <canvas ref={canvasRef} className="w-full h-full" />;
}
