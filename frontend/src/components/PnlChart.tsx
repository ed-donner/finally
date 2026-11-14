"use client";

import { useEffect, useRef } from "react";
import { BaselineSeries, createChart, type ISeriesApi, type UTCTimestamp } from "lightweight-charts";
import { useAppData } from "./AppDataProvider";
import { baseChartOptions, chartColors, toChartSeries } from "@/lib/chartTheme";
import { formatSignedMoney, toneClass } from "@/lib/format";

export const STARTING_CASH = 10_000;

export function PnlChart() {
  const { snapshots } = useAppData();
  const container = useRef<HTMLDivElement>(null);
  const series = useRef<ISeriesApi<"Baseline"> | null>(null);

  useEffect(() => {
    if (!container.current) return;
    const c = createChart(container.current, {
      ...baseChartOptions,
      timeScale: { ...baseChartOptions.timeScale, secondsVisible: false },
    });
    series.current = c.addSeries(BaselineSeries, {
      baseValue: { type: "price", price: STARTING_CASH },
      topLineColor: chartColors.up,
      topFillColor1: "rgba(46, 194, 126, 0.22)",
      topFillColor2: "rgba(46, 194, 126, 0.02)",
      bottomLineColor: chartColors.down,
      bottomFillColor1: "rgba(240, 80, 94, 0.02)",
      bottomFillColor2: "rgba(240, 80, 94, 0.22)",
      lineWidth: 2,
    });
    return () => {
      c.remove();
      series.current = null;
    };
  }, []);

  const points = toChartSeries(
    snapshots.map((s) => ({ time: Date.parse(s.recorded_at) / 1000, value: s.total_value })),
  );

  useEffect(() => {
    series.current?.setData(points.map((p) => ({ time: p.time as UTCTimestamp, value: p.value })));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snapshots]);

  const last = points[points.length - 1]?.value;
  const delta = last != null ? last - STARTING_CASH : null;

  return (
    <section data-testid="pnl-chart" data-points={points.length} className="panel flex min-h-0 flex-col" aria-label="Portfolio value over time">
      <div className="panel-head">
        <h2 className="panel-title">Portfolio value</h2>
        <span className={`num text-xs ${toneClass(delta)}`}>
          {delta == null ? "—" : `${formatSignedMoney(delta)} since start`}
        </span>
      </div>
      <div className="relative min-h-0 flex-1">
        <div ref={container} className="absolute inset-0" />
        {points.length < 2 && (
          <div className="pointer-events-none absolute inset-0 grid place-items-center px-6 text-center text-xs text-muted">
            Value is recorded every 30 seconds and after each trade. The line fills in as you go.
          </div>
        )}
      </div>
    </section>
  );
}
