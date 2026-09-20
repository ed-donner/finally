"use client";

import { ColorType, createChart, type IChartApi, type ISeriesApi } from "lightweight-charts";
import { useEffect, useRef } from "react";
import { CHART_COLORS, toSeriesData } from "@/utils/chart";
import { fmtSignedUsd, signClass } from "@/utils/format";
import type { Snapshot } from "@/utils/types";
import { Panel } from "./Panel";

interface PnLChartProps {
  snapshots: Snapshot[] | null;
  loading: boolean;
  error: string | null;
  startingCash: number | undefined;
}

/** Line chart of total portfolio value over time, from the backend's portfolio_snapshots. */
export function PnLChart({ snapshots, loading, error, startingCash }: PnLChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const fitted = useRef(false);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: CHART_COLORS.text },
      grid: { vertLines: { color: CHART_COLORS.grid }, horzLines: { color: CHART_COLORS.grid } },
      rightPriceScale: { borderColor: CHART_COLORS.border },
      timeScale: { borderColor: CHART_COLORS.border, timeVisible: true, secondsVisible: false },
    });
    seriesRef.current = chart.addLineSeries({ color: CHART_COLORS.accent, lineWidth: 2 });
    chartRef.current = chart;
    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!seriesRef.current || !chartRef.current || !snapshots) return;
    seriesRef.current.setData(
      toSeriesData(snapshots.map((s) => ({ t: Date.parse(s.recorded_at) / 1000, v: s.total_value }))),
    );
    if (!fitted.current && snapshots.length) {
      chartRef.current.timeScale().fitContent();
      fitted.current = true;
    }
  }, [snapshots]);

  const latest = snapshots?.[snapshots.length - 1];
  const pnl = latest && startingCash != null ? latest.total_value - startingCash : null;

  return (
    <Panel
      title="Portfolio value"
      right={<span className={`num text-xs ${signClass(pnl)}`}>{fmtSignedUsd(pnl)}</span>}
      bodyClassName="relative"
    >
      <div ref={containerRef} className="absolute inset-0" data-testid="pnl-chart" />
      {(loading || !snapshots?.length) && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-muted">
          {loading ? "Loading history…" : error ?? "No history yet — snapshots are recorded every 30s"}
        </div>
      )}
    </Panel>
  );
}
