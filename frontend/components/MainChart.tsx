"use client";

import { useMemo } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import { useStore } from "@/lib/store";

export function MainChart() {
  const selectedTicker = useStore((s) => s.selectedTicker);
  const priceHistory = useStore((s) => s.priceHistory);
  const prices = useStore((s) => s.prices);

  const data = useMemo(() => {
    if (!selectedTicker) return [];
    const history = priceHistory[selectedTicker] || [];
    return history.map((p) => ({
      time: new Date(p.timestamp).toLocaleTimeString(),
      price: p.price,
    }));
  }, [selectedTicker, priceHistory]);

  const currentPrice = selectedTicker ? prices[selectedTicker] : null;
  const isUp =
    data.length >= 2 ? data[data.length - 1].price >= data[0].price : true;

  if (!selectedTicker) {
    return (
      <div className="flex items-center justify-center h-full text-text-secondary text-sm">
        Select a ticker from the watchlist
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full p-3">
      <div className="flex items-baseline gap-3 mb-2">
        <span className="text-lg font-bold text-text-primary">
          {selectedTicker}
        </span>
        {currentPrice && (
          <>
            <span className="text-xl font-bold text-text-primary">
              ${currentPrice.price.toFixed(2)}
            </span>
            <span
              className={`text-sm ${
                currentPrice.direction === "up"
                  ? "text-gain"
                  : currentPrice.direction === "down"
                    ? "text-loss"
                    : "text-text-secondary"
              }`}
            >
              {currentPrice.direction === "up" ? "+" : ""}
              {(currentPrice.price - currentPrice.previous_price).toFixed(2)}
            </span>
          </>
        )}
      </div>

      {data.length < 2 ? (
        <div className="flex-1 flex items-center justify-center text-text-secondary text-sm">
          Waiting for price data...
        </div>
      ) : (
        <div className="flex-1">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data}>
              <XAxis
                dataKey="time"
                tick={{ fontSize: 10, fill: "#8b949e" }}
                axisLine={{ stroke: "#30363d" }}
                tickLine={false}
                minTickGap={40}
              />
              <YAxis
                domain={["auto", "auto"]}
                tick={{ fontSize: 10, fill: "#8b949e" }}
                axisLine={{ stroke: "#30363d" }}
                tickLine={false}
                width={60}
                tickFormatter={(v: number) => `$${v.toFixed(0)}`}
              />
              <Tooltip
                contentStyle={{
                  background: "#161b22",
                  border: "1px solid #30363d",
                  borderRadius: 4,
                  fontSize: 12,
                  color: "#e6edf3",
                }}
                // eslint-disable-next-line @typescript-eslint/no-explicit-any
                formatter={((value: any) => [`$${Number(value).toFixed(2)}`, "Price"]) as any}
              />
              <Line
                type="monotone"
                dataKey="price"
                stroke={isUp ? "#3fb950" : "#f85149"}
                dot={false}
                strokeWidth={1.5}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}
