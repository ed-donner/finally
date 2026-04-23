"use client";

import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { Panel } from "@/components/ui/panel";
import { usd, usdShort } from "@/lib/format";
import { usePortfolio } from "@/lib/store/portfolio";

export function PnLChart() {
  const { history } = usePortfolio();
  const data = history.map((h) => ({
    t: new Date(h.recorded_at).getTime(),
    v: h.total_value,
  }));

  return (
    <Panel title="Equity curve" accent={`${data.length} pts`}>
      <div className="w-full h-full min-h-[180px] p-2">
        {data.length < 2 ? (
          <div className="grid place-items-center w-full h-full text-dim text-xs uppercase tracking-[0.3em]">
            collecting snapshots…
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data} margin={{ top: 8, right: 4, left: 8, bottom: 0 }}>
              <defs>
                <linearGradient id="pnlGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#ecad0a" stopOpacity={0.45} />
                  <stop offset="100%" stopColor="#ecad0a" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="rgba(255,255,255,0.04)" />
              <XAxis
                dataKey="t"
                type="number"
                domain={["dataMin", "dataMax"]}
                tickFormatter={(t) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                stroke="#6e7681"
                tick={{ fontSize: 10, fontFamily: "JetBrains Mono" }}
                axisLine={false}
                tickLine={false}
              />
              <YAxis
                dataKey="v"
                domain={["auto", "auto"]}
                tickFormatter={(v) => usdShort(v)}
                stroke="#6e7681"
                tick={{ fontSize: 10, fontFamily: "JetBrains Mono" }}
                axisLine={false}
                tickLine={false}
              />
              <Tooltip
                contentStyle={{
                  background: "#0c1219",
                  border: "1px solid #2a3340",
                  fontFamily: "JetBrains Mono",
                  fontSize: 11,
                  color: "#e6edf3",
                }}
                labelFormatter={(t) => new Date(t).toLocaleString()}
                formatter={(v: number) => [usd(v), "total"]}
              />
              <Area type="monotone" dataKey="v" stroke="#ecad0a" strokeWidth={1.5} fill="url(#pnlGrad)" />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
    </Panel>
  );
}
