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
import type { PricePoint } from '@/lib/types';
import { formatPrice } from '@/lib/format';

interface MainChartProps {
  ticker: string | null;
  data: PricePoint[];
}

export default function MainChart({ ticker, data }: MainChartProps) {
  const chartData = useMemo(
    () =>
      data.map((d) => ({
        t: d.t,
        price: d.price,
        label: new Date(d.t).toLocaleTimeString('en-US', { hour12: false }),
      })),
    [data],
  );

  const up = data.length >= 2 && data[data.length - 1].price >= data[0].price;
  const stroke = up ? '#26a269' : '#e5484d';
  const last = data.length ? data[data.length - 1].price : null;

  return (
    <div className="panel h-full" data-testid="main-chart">
      <div className="panel-title">
        <span className="flex items-center gap-2">
          <span className="text-terminal-text font-semibold text-sm">
            {ticker ?? '—'}
          </span>
          <span className="text-terminal-muted normal-case tracking-normal">
            price since page load
          </span>
        </span>
        {last !== null && (
          <span className="num text-terminal-text" style={{ color: stroke }}>
            {formatPrice(last)}
          </span>
        )}
      </div>

      <div className="flex-1 min-h-0 p-2">
        {chartData.length < 2 ? (
          <div className="h-full flex items-center justify-center text-terminal-muted text-xs">
            Accumulating live data…
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={chartData} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
              <defs>
                <linearGradient id="priceFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={stroke} stopOpacity={0.35} />
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
                width={52}
                tickFormatter={(v) => formatPrice(v as number)}
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
                formatter={(value: number | string) => [formatPrice(value as number), 'Price']}
              />
              <Area
                type="monotone"
                dataKey="price"
                stroke={stroke}
                strokeWidth={1.75}
                fill="url(#priceFill)"
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
