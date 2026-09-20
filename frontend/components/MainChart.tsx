"use client";

import { ColorType, createChart, type IChartApi, type ISeriesApi } from "lightweight-charts";
import { useEffect, useRef } from "react";
import { CHART_COLORS, toSeriesData } from "@/utils/chart";
import { fmtPct, fmtUsd, signClass } from "@/utils/format";
import type { PricePoint } from "@/utils/types";
import { Panel } from "./Panel";

interface MainChartProps {
  ticker: string | null;
  points: PricePoint[];
  openPrice: number | undefined;
}

/** Larger canvas chart (Lightweight Charts) of the selected ticker, built from the SSE stream. */
export function MainChart({ ticker, points, openPrice }: MainChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Area"> | null>(null);
  const shownTicker = useRef<string | null>(null);

  // Create the chart once. autoSize keeps it fitted to its container.
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: CHART_COLORS.text },
      grid: { vertLines: { color: CHART_COLORS.grid }, horzLines: { color: CHART_COLORS.grid } },
      rightPriceScale: { borderColor: CHART_COLORS.border },
      timeScale: { borderColor: CHART_COLORS.border, timeVisible: true, secondsVisible: true },
    });
    seriesRef.current = chart.addAreaSeries({
      lineColor: CHART_COLORS.primary,
      topColor: "rgba(32,157,215,0.35)",
      bottomColor: "rgba(32,157,215,0)",
      lineWidth: 2,
    });
    chartRef.current = chart;
    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  // Push data on every tick; only re-fit the view when the user switches ticker (so zoom/pan sticks).
  useEffect(() => {
    if (!seriesRef.current || !chartRef.current) return;
    seriesRef.current.setData(toSeriesData(points.map((p) => ({ t: p.t, v: p.p }))));
    if (shownTicker.current !== ticker && points.length) {
      chartRef.current.timeScale().fitContent();
      shownTicker.current = ticker;
    }
  }, [points, ticker]);

  const last = points[points.length - 1]?.p;
  const change = last != null && openPrice ? ((last - openPrice) / openPrice) * 100 : null;

  return (
    <Panel
      title={ticker ? `${ticker} — live` : "Chart"}
      right={
        <span className="num text-xs">
          <span className="text-white">{fmtUsd(last)}</span> <span className={signClass(change)}>{fmtPct(change)}</span>
        </span>
      }
      bodyClassName="relative"
    >
      <div ref={containerRef} className="absolute inset-0" data-testid="main-chart" />
      {(!ticker || points.length < 2) && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-muted">
          {ticker ? "Waiting for price data…" : "Select a ticker from the watchlist"}
        </div>
      )}
    </Panel>
  );
}
