"use client";

import { useEffect, useRef } from "react";
import { createChart, AreaSeries, ColorType } from "lightweight-charts";
import type { IChartApi, ISeriesApi, UTCTimestamp } from "lightweight-charts";
import type { Snapshot } from "@/stores/portfolio-store";

export function PnlChart({ snapshots }: { snapshots: Snapshot[] }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Area"> | null>(null);

  // Create chart instance once on mount
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "#1a1a2e" },
        textColor: "#8b949e",
      },
      grid: {
        vertLines: { color: "#2d2d44" },
        horzLines: { color: "#2d2d44" },
      },
      timeScale: {
        borderColor: "#2d2d44",
        timeVisible: true,
      },
      rightPriceScale: {
        borderColor: "#2d2d44",
      },
      width: containerRef.current.clientWidth,
      height: containerRef.current.clientHeight,
    });

    const series = chart.addSeries(AreaSeries, {
      lineColor: "#ecad0a",
      topColor: "rgba(236, 173, 10, 0.4)",
      bottomColor: "rgba(236, 173, 10, 0.05)",
      lineWidth: 2,
    });

    chartRef.current = chart;
    seriesRef.current = series;

    const observer = new ResizeObserver((entries) => {
      const { width, height } = entries[0].contentRect;
      chart.applyOptions({ width, height });
    });
    observer.observe(containerRef.current);

    return () => {
      observer.disconnect();
      chart.remove();
    };
  }, []);

  // Sync data when snapshots change
  useEffect(() => {
    if (!seriesRef.current || snapshots.length === 0) return;

    const mapped = snapshots.map((s) => ({
      time: (new Date(s.recorded_at).getTime() / 1000) as UTCTimestamp,
      value: s.total_value,
    }));

    seriesRef.current.setData(mapped);
    chartRef.current?.timeScale().fitContent();
  }, [snapshots]);

  return (
    <div className="h-full w-full flex flex-col bg-terminal-surface">
      <div className="px-3 pt-3 pb-1">
        <span className="text-text-muted font-mono text-xs uppercase tracking-wider">
          P&L
        </span>
      </div>
      <div ref={containerRef} className="flex-1 min-h-0" />
    </div>
  );
}
