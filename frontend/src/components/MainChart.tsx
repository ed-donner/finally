"use client";

import { useEffect, useRef } from "react";
import { AreaSeries, createChart, type IChartApi, type ISeriesApi, type UTCTimestamp } from "lightweight-charts";
import { useAppData } from "./AppDataProvider";
import { usePrice, usePriceHistory } from "@/lib/priceStore";
import { baseChartOptions, chartColors, toChartSeries } from "@/lib/chartTheme";
import { formatPercent, formatPrice, formatSignedMoney, toneClass } from "@/lib/format";

export function MainChart() {
  const { selected } = useAppData();
  const ticker = selected ?? "";
  const history = usePriceHistory(ticker);
  const live = usePrice(ticker);
  const container = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Area"> | null>(null);

  useEffect(() => {
    if (!container.current) return;
    const c = createChart(container.current, baseChartOptions);
    series.current = c.addSeries(AreaSeries, {
      lineColor: chartColors.blue,
      lineWidth: 2,
      topColor: "rgba(32, 157, 215, 0.28)",
      bottomColor: "rgba(32, 157, 215, 0.0)",
      priceLineColor: chartColors.accent,
      priceLineStyle: 2,
    });
    chart.current = c;
    return () => {
      c.remove();
      chart.current = null;
      series.current = null;
    };
  }, []);

  useEffect(() => {
    if (!series.current) return;
    series.current.setData(
      toChartSeries(history).map((p) => ({ time: p.time as UTCTimestamp, value: p.value })),
    );
  }, [history]);

  useEffect(() => {
    chart.current?.timeScale().fitContent();
  }, [ticker]);

  const day = live?.day_change_percent;
  return (
    <section data-testid="main-chart" className="panel flex min-h-0 flex-col" aria-label="Price chart">
      <div className="panel-head gap-4">
        <div className="flex items-baseline gap-3">
          <h2 data-testid="selected-ticker" className="text-lg font-semibold tracking-tight text-text">
            {ticker || "—"}
          </h2>
          <span className="num text-lg text-text">{formatPrice(live?.price)}</span>
          {live && (
            <span className={`num text-xs ${toneClass(live.day_change ?? live.change)}`}>
              {formatSignedMoney(live.day_change ?? live.change)} ({formatPercent(day ?? live.change_percent)})
            </span>
          )}
        </div>
        <span className="text-[11px] text-dim">Since page load · {history.length} ticks</span>
      </div>
      <div className="relative min-h-0 flex-1">
        <div ref={container} className="absolute inset-0" />
        {history.length < 2 && (
          <div className="pointer-events-none absolute inset-0 grid place-items-center text-xs text-muted">
            {ticker ? `Waiting for ${ticker} ticks…` : "Select a ticker from the watchlist"}
          </div>
        )}
      </div>
    </section>
  );
}
