"use client";

import {
  AreaSeries,
  ColorType,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef } from "react";

import { Panel } from "@/components/ui/panel";
import { usePrices } from "@/lib/store/prices";
import { useWatchlist } from "@/lib/store/watchlist";

export function ChartPanel() {
  const { selected } = useWatchlist();
  const { sparklines, prices } = usePrices();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Area"> | null>(null);

  // Build / rebuild chart when the container is mounted
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#8b949e",
        fontFamily: "JetBrains Mono, ui-monospace, monospace",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "rgba(255,255,255,0.03)" },
        horzLines: { color: "rgba(255,255,255,0.03)" },
      },
      rightPriceScale: { borderColor: "#1f2630" },
      timeScale: { borderColor: "#1f2630", secondsVisible: true, timeVisible: true },
      crosshair: { vertLine: { color: "#2a3340" }, horzLine: { color: "#2a3340" } },
      autoSize: true,
    });
    const series = chart.addSeries(AreaSeries, {
      topColor: "rgba(32,157,215,0.30)",
      bottomColor: "rgba(32,157,215,0.00)",
      lineColor: "#209dd7",
      lineWidth: 2,
      priceFormat: { type: "price", precision: 2, minMove: 0.01 },
    });
    chartRef.current = chart;
    seriesRef.current = series;
    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  const update = selected ? prices[selected] : undefined;

  // Push selected ticker's points into the series. Re-runs whenever the
  // selected ticker's latest price changes — `sparklines` is a stable ref so
  // we depend on the latest price update to drive ticks.
  useEffect(() => {
    if (!seriesRef.current || !selected) return;
    const buf = sparklines[selected] ?? [];
    if (buf.length === 0) {
      seriesRef.current.setData([]);
      return;
    }
    const seen = new Set<number>();
    const data: { time: UTCTimestamp; value: number }[] = [];
    for (const p of buf) {
      const t = Math.floor(p.time);
      if (seen.has(t)) continue;
      seen.add(t);
      data.push({ time: t as UTCTimestamp, value: p.price });
    }
    data.sort((a, b) => (a.time as number) - (b.time as number));
    seriesRef.current.setData(data);
  }, [selected, sparklines, update?.timestamp]);

  return (
    <Panel
      title="Chart"
      accent={selected ?? undefined}
      actions={
        update && (
          <span className="text-xs tabular-nums">
            <span className="text-ink">${update.price.toFixed(2)}</span>{" "}
            <span className={update.change > 0 ? "text-up" : update.change < 0 ? "text-down" : "text-dim"}>
              {update.change > 0 ? "▲" : update.change < 0 ? "▼" : "·"}
              {Math.abs(update.change_percent).toFixed(2)}%
            </span>
          </span>
        )
      }
    >
      <div ref={containerRef} className="w-full h-full min-h-[260px]" />
    </Panel>
  );
}
