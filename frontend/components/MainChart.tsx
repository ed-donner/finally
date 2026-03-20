"use client";

import { useEffect, useRef } from "react";
import type { SparklineMap } from "../hooks/usePriceStream";
import type { PriceMap } from "../hooks/usePriceStream";

interface MainChartProps {
  ticker: string | null;
  prices: PriceMap;
  sparklines: SparklineMap;
}

export function MainChart({ ticker, prices, sparklines }: MainChartProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container || !ticker) return;

    const data = sparklines[ticker];
    if (!data || data.length < 2) return;

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

    const min = Math.min(...data) * 0.9995;
    const max = Math.max(...data) * 1.0005;
    const range = max - min || 1;

    const padLeft = 60;
    const padRight = 16;
    const padTop = 16;
    const padBottom = 32;
    const w = width - padLeft - padRight;
    const h = height - padTop - padBottom;

    const getX = (i: number) => padLeft + (i / (data.length - 1)) * w;
    const getY = (v: number) => padTop + h - ((v - min) / range) * h;

    const trend = data[data.length - 1] >= data[0];
    const lineColor = trend ? "#3fb950" : "#f85149";
    const fillColor = trend ? "rgba(63,185,80,0.08)" : "rgba(248,81,73,0.08)";

    // Grid lines
    ctx.strokeStyle = "rgba(48,54,61,0.6)";
    ctx.lineWidth = 1;
    const gridLines = 5;
    for (let i = 0; i <= gridLines; i++) {
      const y = padTop + (h / gridLines) * i;
      ctx.beginPath();
      ctx.moveTo(padLeft, y);
      ctx.lineTo(width - padRight, y);
      ctx.stroke();

      // Price label
      const price = max - (range / gridLines) * i;
      ctx.fillStyle = "rgba(139,148,158,0.8)";
      ctx.font = "10px ui-monospace, monospace";
      ctx.textAlign = "right";
      ctx.fillText(`$${price.toFixed(2)}`, padLeft - 6, y + 3);
    }

    // Time labels (first, middle, last)
    ctx.fillStyle = "rgba(139,148,158,0.6)";
    ctx.font = "10px ui-monospace, monospace";
    ctx.textAlign = "center";
    const timeLabels = [0, Math.floor(data.length / 2), data.length - 1];
    for (const i of timeLabels) {
      if (i < data.length) {
        ctx.fillText(`t-${data.length - 1 - i}`, getX(i), height - 8);
      }
    }

    // Fill area
    ctx.beginPath();
    ctx.moveTo(getX(0), getY(data[0]));
    for (let i = 1; i < data.length; i++) {
      ctx.lineTo(getX(i), getY(data[i]));
    }
    ctx.lineTo(getX(data.length - 1), padTop + h);
    ctx.lineTo(getX(0), padTop + h);
    ctx.closePath();
    ctx.fillStyle = fillColor;
    ctx.fill();

    // Price line
    ctx.beginPath();
    ctx.moveTo(getX(0), getY(data[0]));
    for (let i = 1; i < data.length; i++) {
      ctx.lineTo(getX(i), getY(data[i]));
    }
    ctx.strokeStyle = lineColor;
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.stroke();

    // Current price dot
    const lastX = getX(data.length - 1);
    const lastY = getY(data[data.length - 1]);
    ctx.beginPath();
    ctx.arc(lastX, lastY, 4, 0, Math.PI * 2);
    ctx.fillStyle = lineColor;
    ctx.fill();
  }, [ticker, sparklines, prices]);

  if (!ticker) {
    return (
      <div
        ref={containerRef}
        className="flex items-center justify-center h-full"
        style={{ color: "var(--text-secondary)" }}
      >
        <p className="text-sm">Select a ticker to view chart</p>
      </div>
    );
  }

  const priceUpdate = prices[ticker];
  const sparkData = sparklines[ticker] ?? [];
  const trend = sparkData.length >= 2 ? sparkData[sparkData.length - 1] >= sparkData[0] : true;

  return (
    <div className="flex flex-col h-full">
      {/* Chart header */}
      <div
        className="flex items-center gap-4 px-4 py-2 flex-shrink-0"
        style={{ borderBottom: "1px solid var(--border-color)" }}
      >
        <span className="font-bold text-lg tracking-wide">{ticker}</span>
        {priceUpdate && (
          <>
            <span className="font-mono text-xl font-semibold">
              ${priceUpdate.price.toFixed(2)}
            </span>
            <span
              className="text-sm font-mono"
              style={{ color: priceUpdate.direction === "up" ? "var(--green)" : priceUpdate.direction === "down" ? "var(--red)" : "var(--text-secondary)" }}
            >
              {priceUpdate.direction === "up" ? "▲" : priceUpdate.direction === "down" ? "▼" : "—"}{" "}
              {priceUpdate.change_percent >= 0 ? "+" : ""}{priceUpdate.change_percent.toFixed(2)}%
            </span>
          </>
        )}
        {sparkData.length < 2 && (
          <span className="text-xs" style={{ color: "var(--text-secondary)" }}>
            Waiting for data…
          </span>
        )}
      </div>

      {/* Canvas chart */}
      <div ref={containerRef} className="flex-1 relative">
        <canvas ref={canvasRef} className="absolute inset-0 w-full h-full" />
      </div>
    </div>
  );
}
