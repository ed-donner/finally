"use client";

import { useEffect, useRef } from "react";
import { createChart, LineSeries, ColorType } from "lightweight-charts";
import type { IChartApi, ISeriesApi, UTCTimestamp } from "lightweight-charts";
import { useWatchlistStore } from "@/stores/watchlist-store";
import { usePriceStore } from "@/stores/price-store";

export function ChartPanel() {
  const selectedTicker = useWatchlistStore((s) => s.selectedTicker);
  const priceHistory = usePriceStore((s) => s.priceHistory);

  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Line"> | null>(null);

  // Create chart instance ONCE on mount
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
        secondsVisible: true,
      },
      rightPriceScale: {
        borderColor: "#2d2d44",
      },
      width: containerRef.current.clientWidth,
      height: containerRef.current.clientHeight,
    });

    const series = chart.addSeries(LineSeries, {
      color: "#209dd7",
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

  // Sync data when selectedTicker or priceHistory changes
  useEffect(() => {
    if (!seriesRef.current || !selectedTicker) return;
    const history = priceHistory[selectedTicker];
    if (!history || history.length === 0) return;

    seriesRef.current.setData(
      history.map((p) => ({ time: p.time as UTCTimestamp, value: p.value })),
    );
    chartRef.current?.timeScale().fitContent();
  }, [selectedTicker, priceHistory]);

  if (!selectedTicker) {
    return (
      <div className="h-full w-full p-3 bg-terminal-surface flex items-center justify-center">
        <span className="text-text-muted font-mono text-xs uppercase tracking-wider">
          Select a ticker to view chart
        </span>
      </div>
    );
  }

  return (
    <div className="h-full w-full flex flex-col bg-terminal-surface">
      <div className="px-3 pt-3 pb-1 flex items-center justify-between">
        <span className="text-text-muted font-mono text-xs uppercase tracking-wider">
          Chart
        </span>
        <span className="font-mono text-sm font-bold text-text-primary">
          {selectedTicker}
        </span>
      </div>
      <div ref={containerRef} className="flex-1 min-h-0" />
    </div>
  );
}
