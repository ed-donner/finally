"use client";

import { useEffect, useState, useCallback } from "react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import { getPortfolioHistory } from "@/lib/api";
import type { PortfolioSnapshot } from "@/lib/types";

export function PnLChart() {
  const [snapshots, setSnapshots] = useState<PortfolioSnapshot[]>([]);

  const load = useCallback(async () => {
    try {
      const data = await getPortfolioHistory();
      setSnapshots(data);
    } catch {
      // retry on next interval
    }
  }, []);

  useEffect(() => {
    load();
    const interval = setInterval(load, 30000);
    return () => clearInterval(interval);
  }, [load]);

  const chartData = snapshots.map((s) => ({
    time: new Date(s.recorded_at).toLocaleTimeString(),
    value: s.total_value,
  }));

  const startValue = 10000;
  const currentValue = chartData.length
    ? chartData[chartData.length - 1].value
    : startValue;
  const isUp = currentValue >= startValue;

  if (!chartData.length) {
    return (
      <div className="flex items-center justify-center h-full text-text-secondary text-xs">
        Portfolio history will appear here
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart data={chartData}>
        <defs>
          <linearGradient id="pnlGradient" x1="0" y1="0" x2="0" y2="1">
            <stop
              offset="5%"
              stopColor={isUp ? "#3fb950" : "#f85149"}
              stopOpacity={0.3}
            />
            <stop
              offset="95%"
              stopColor={isUp ? "#3fb950" : "#f85149"}
              stopOpacity={0}
            />
          </linearGradient>
        </defs>
        <XAxis
          dataKey="time"
          tick={{ fontSize: 9, fill: "#8b949e" }}
          axisLine={{ stroke: "#30363d" }}
          tickLine={false}
          minTickGap={40}
        />
        <YAxis
          domain={["auto", "auto"]}
          tick={{ fontSize: 9, fill: "#8b949e" }}
          axisLine={{ stroke: "#30363d" }}
          tickLine={false}
          width={55}
          tickFormatter={(v: number) => `$${(v / 1000).toFixed(1)}k`}
        />
        <Tooltip
          contentStyle={{
            background: "#161b22",
            border: "1px solid #30363d",
            borderRadius: 4,
            fontSize: 11,
            color: "#e6edf3",
          }}
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          formatter={((value: any) => [`$${Number(value).toFixed(2)}`, "Portfolio"]) as any}
        />
        <Area
          type="monotone"
          dataKey="value"
          stroke={isUp ? "#3fb950" : "#f85149"}
          fill="url(#pnlGradient)"
          strokeWidth={1.5}
          isAnimationActive={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
