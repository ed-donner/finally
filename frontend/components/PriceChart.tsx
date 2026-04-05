"use client";

import { useEffect, useRef } from "react";
import { createChart, LineSeries, type IChartApi, type ISeriesApi, type LineData, type Time } from "lightweight-charts";

interface PriceChartProps {
  ticker: string | null;
  getHistory: (ticker: string) => number[];
}

export default function PriceChart({ ticker, getHistory }: PriceChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Line"> | null>(null);

  // Create chart once
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      autoSize: true,
      layout: {
        background: { color: "#161b22" },
        textColor: "#8b949e",
        fontFamily: "ui-monospace, monospace",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "#21262d" },
        horzLines: { color: "#21262d" },
      },
      crosshair: {
        vertLine: { color: "#30363d", labelBackgroundColor: "#30363d" },
        horzLine: { color: "#30363d", labelBackgroundColor: "#30363d" },
      },
      rightPriceScale: {
        borderColor: "#30363d",
      },
      timeScale: {
        borderColor: "#30363d",
        timeVisible: true,
        secondsVisible: false,
      },
    });

    const series = chart.addSeries(LineSeries, {
      color: "#209dd7",
      lineWidth: 2,
      crosshairMarkerRadius: 4,
      crosshairMarkerBackgroundColor: "#209dd7",
    });

    chartRef.current = chart;
    seriesRef.current = series;

    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  // Update data when ticker or history changes
  useEffect(() => {
    if (!seriesRef.current || !ticker) return;

    const history = getHistory(ticker);
    if (history.length === 0) {
      seriesRef.current.setData([]);
      return;
    }

    const now = Math.floor(Date.now() / 1000);
    const data: LineData[] = history.map((price, i) => ({
      time: (now - (history.length - 1 - i)) as Time,
      value: price,
    }));

    seriesRef.current.setData(data);
    chartRef.current?.timeScale().fitContent();
  }, [ticker, getHistory]);

  // Update latest data point on each render (called frequently as prices update)
  useEffect(() => {
    if (!seriesRef.current || !ticker) return;
    const history = getHistory(ticker);
    if (history.length === 0) return;

    const now = Math.floor(Date.now() / 1000);
    seriesRef.current.update({
      time: now as Time,
      value: history[history.length - 1],
    });
  });

  return (
    <div className="flex flex-col h-full">
      <div className="px-3 py-2 border-b border-border">
        <h2 className="text-xs font-bold text-text-secondary uppercase tracking-wider">
          {ticker ? `${ticker} -- Price Chart` : "Price Chart"}
        </h2>
      </div>
      <div className="flex-1 min-h-0 relative">
        <div ref={containerRef} style={{ position: "absolute", inset: 0 }} />
        {!ticker && (
          <div className="absolute inset-0 flex items-center justify-center text-text-muted text-sm">
            Select a ticker from the watchlist
          </div>
        )}
      </div>
    </div>
  );
}
