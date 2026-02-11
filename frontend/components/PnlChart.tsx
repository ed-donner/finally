"use client";

import { useEffect, useState } from "react";
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
} from "recharts";
import type { PortfolioSnapshot } from "@/lib/types";
import { getPortfolioHistory } from "@/lib/api";
import { formatCurrency } from "@/lib/format";

export default function PnlChart() {
  const [data, setData] = useState<PortfolioSnapshot[]>([]);

  useEffect(() => {
    let active = true;

    async function load() {
      try {
        const snapshots = await getPortfolioHistory();
        if (active) setData(snapshots);
      } catch {
        // Will retry on next interval
      }
    }

    load();
    const interval = setInterval(load, 30000);
    return () => {
      active = false;
      clearInterval(interval);
    };
  }, []);

  const chartData = data.map((s) => ({
    time: new Date(s.recorded_at).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    }),
    value: s.total_value,
  }));

  const startValue = 10000;
  const lastValue = chartData.length > 0 ? chartData[chartData.length - 1].value : startValue;
  const isPositive = lastValue >= startValue;

  return (
    <div className="flex flex-col h-full">
      <div className="px-3 py-2 border-b border-border">
        <h2 className="text-xs font-bold text-text-secondary uppercase tracking-wider">
          Portfolio P&L
        </h2>
      </div>
      <div className="flex-1 min-h-0 p-2">
        {chartData.length < 2 ? (
          <div className="flex items-center justify-center h-full text-text-muted text-xs">
            Waiting for portfolio history...
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
              <XAxis
                dataKey="time"
                tick={{ fill: "#8b949e", fontSize: 10 }}
                stroke="#30363d"
              />
              <YAxis
                tick={{ fill: "#8b949e", fontSize: 10 }}
                stroke="#30363d"
                tickFormatter={(v) => `$${(v / 1000).toFixed(1)}k`}
                domain={["dataMin - 100", "dataMax + 100"]}
              />
              <Tooltip
                contentStyle={{
                  backgroundColor: "#161b22",
                  border: "1px solid #30363d",
                  borderRadius: "4px",
                  fontSize: "11px",
                  color: "#e6edf3",
                }}
                formatter={(value) => [formatCurrency(value as number), "Value"]}
              />
              <Area
                type="monotone"
                dataKey="value"
                stroke={isPositive ? "#26a641" : "#f85149"}
                fill={isPositive ? "rgba(38,166,65,0.1)" : "rgba(248,81,73,0.1)"}
                strokeWidth={2}
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}
