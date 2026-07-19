'use client';

import { useMemo } from 'react';
import {
  Area,
  AreaChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { Snapshot } from '@/lib/types';
import { formatUSD } from '@/lib/format';

interface PnLChartProps {
  snapshots: Snapshot[];
  /** Live total value appended as the latest point for a real-time feel. */
  liveValue?: number | null;
}

export default function PnLChart({ snapshots, liveValue }: PnLChartProps) {
  const data = useMemo(() => {
    const base = snapshots.map((s) => ({
      t: Date.parse(s.recorded_at) || 0,
      value: s.total_value,
      label: new Date(s.recorded_at).toLocaleTimeString('en-US', { hour12: false }),
    }));
    if (typeof liveValue === 'number') {
      base.push({ t: Date.now(), value: liveValue, label: 'now' });
    }
    return base;
  }, [snapshots, liveValue]);

  // Green if the portfolio is up vs. the $10k starting line (or first snapshot).
  const start = data.length ? data[0].value : 10000;
  const last = data.length ? data[data.length - 1].value : 10000;
  const up = last >= start;
  const stroke = up ? '#26a269' : '#e5484d';

  return (
    <div className="panel h-full" data-testid="pnl-chart">
      <div className="panel-title">
        <span>Portfolio Value</span>
        <span className="num text-terminal-text" style={{ color: stroke }}>
          {formatUSD(last)}
        </span>
      </div>
      <div className="flex-1 min-h-0 p-2">
        {data.length < 2 ? (
          <div className="h-full flex items-center justify-center text-terminal-muted text-xs">
            Awaiting snapshots…
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
              <defs>
                <linearGradient id="pnlFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={stroke} stopOpacity={0.3} />
                  <stop offset="100%" stopColor={stroke} stopOpacity={0} />
                </linearGradient>
              </defs>
              <XAxis
                dataKey="label"
                tick={{ fill: '#8b96a5', fontSize: 10 }}
                stroke="#232d3b"
                minTickGap={40}
              />
              <YAxis
                domain={['auto', 'auto']}
                tick={{ fill: '#8b96a5', fontSize: 10 }}
                stroke="#232d3b"
                width={64}
                tickFormatter={(v) => formatUSD(v as number)}
                orientation="right"
              />
              <Tooltip
                contentStyle={{
                  background: '#131a24',
                  border: '1px solid #232d3b',
                  borderRadius: 6,
                  fontSize: 12,
                }}
                labelStyle={{ color: '#8b96a5' }}
                formatter={(value: number | string) => [formatUSD(value as number), 'Value']}
              />
              <Area
                type="monotone"
                dataKey="value"
                stroke={stroke}
                strokeWidth={1.75}
                fill="url(#pnlFill)"
                isAnimationActive={false}
                dot={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}
