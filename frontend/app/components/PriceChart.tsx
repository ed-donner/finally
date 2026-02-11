"use client";

import { useEffect, useRef } from "react";
import { PriceUpdate } from "../lib/types";

interface PriceChartProps {
  ticker: string | null;
  sparklines: Record<string, number[]>;
  prices: Record<string, PriceUpdate>;
}

export default function PriceChart({ ticker, sparklines, prices }: PriceChartProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !ticker) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const data = sparklines[ticker] || [];
    const p = prices[ticker];

    // Set canvas size to match element
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * 2;
    canvas.height = rect.height * 2;
    ctx.scale(2, 2);

    const w = rect.width;
    const h = rect.height;

    // Clear
    ctx.fillStyle = "#0d1117";
    ctx.fillRect(0, 0, w, h);

    if (data.length < 2) {
      ctx.fillStyle = "#8b949e";
      ctx.font = "13px monospace";
      ctx.textAlign = "center";
      ctx.fillText(
        ticker ? `Waiting for ${ticker} data...` : "Select a ticker",
        w / 2, h / 2
      );
      return;
    }

    const min = Math.min(...data);
    const max = Math.max(...data);
    const range = max - min || 1;
    const pad = 40;

    // Draw gridlines
    ctx.strokeStyle = "#30363d";
    ctx.lineWidth = 0.5;
    for (let i = 0; i < 5; i++) {
      const y = pad + (i / 4) * (h - 2 * pad);
      ctx.beginPath();
      ctx.moveTo(pad, y);
      ctx.lineTo(w - 10, y);
      ctx.stroke();

      // Price labels
      const price = max - (i / 4) * range;
      ctx.fillStyle = "#8b949e";
      ctx.font = "10px monospace";
      ctx.textAlign = "right";
      ctx.fillText(`$${price.toFixed(2)}`, pad - 4, y + 3);
    }

    // Draw line
    const isUp = data[data.length - 1] >= data[0];
    const lineColor = isUp ? "#3fb950" : "#f85149";
    const fillColor = isUp ? "rgba(63, 185, 80, 0.1)" : "rgba(248, 81, 73, 0.1)";

    ctx.beginPath();
    data.forEach((v, i) => {
      const x = pad + (i / (data.length - 1)) * (w - pad - 10);
      const y = pad + ((max - v) / range) * (h - 2 * pad);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = lineColor;
    ctx.lineWidth = 2;
    ctx.stroke();

    // Fill area under curve
    const lastX = pad + ((data.length - 1) / (data.length - 1)) * (w - pad - 10);
    ctx.lineTo(lastX, h - pad);
    ctx.lineTo(pad, h - pad);
    ctx.closePath();
    ctx.fillStyle = fillColor;
    ctx.fill();

    // Current price label
    if (p) {
      ctx.fillStyle = lineColor;
      ctx.font = "bold 16px monospace";
      ctx.textAlign = "left";
      ctx.fillText(`$${p.price.toFixed(2)}`, pad + 4, 20);

      const sign = p.change >= 0 ? "+" : "";
      ctx.font = "12px monospace";
      ctx.fillText(`${sign}${p.change.toFixed(2)} (${sign}${p.change_percent.toFixed(2)}%)`, pad + 120, 20);
    }

    // Ticker label
    ctx.fillStyle = "#209dd7";
    ctx.font = "bold 14px monospace";
    ctx.textAlign = "right";
    ctx.fillText(ticker, w - 14, 20);
  }, [ticker, sparklines, prices]);

  if (!ticker) {
    return (
      <div className="flex items-center justify-center h-full text-text-secondary">
        Click a ticker in the watchlist to view its chart
      </div>
    );
  }

  return <canvas ref={canvasRef} className="w-full h-full" />;
}
