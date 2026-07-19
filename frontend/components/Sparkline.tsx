'use client';

import { useMemo } from 'react';
import type { PricePoint } from '@/lib/types';

interface SparklineProps {
  data: PricePoint[];
  width?: number;
  height?: number;
  /** Overrides the up/down auto-color derived from first→last. */
  color?: string;
}

/**
 * Tiny inline SVG sparkline. Kept dependency-free and cheap so it can render
 * once per watchlist row at stream cadence. Colors green/red by net direction.
 */
export default function Sparkline({ data, width = 90, height = 26, color }: SparklineProps) {
  const { path, stroke } = useMemo(() => {
    if (!data || data.length < 2) {
      return { path: '', stroke: color ?? '#8b96a5' };
    }
    const prices = data.map((d) => d.price);
    const min = Math.min(...prices);
    const max = Math.max(...prices);
    const range = max - min || 1;
    const stepX = width / (data.length - 1);
    const pad = 2;
    const usableH = height - pad * 2;

    const points = prices.map((p, i) => {
      const x = i * stepX;
      const y = pad + usableH - ((p - min) / range) * usableH;
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    });

    const up = prices[prices.length - 1] >= prices[0];
    return {
      path: `M ${points.join(' L ')}`,
      stroke: color ?? (up ? '#26a269' : '#e5484d'),
    };
  }, [data, width, height, color]);

  if (!path) {
    return (
      <svg width={width} height={height} className="opacity-40" aria-hidden="true">
        <line
          x1={0}
          y1={height / 2}
          x2={width}
          y2={height / 2}
          stroke="#8b96a5"
          strokeDasharray="2 3"
          strokeWidth={1}
        />
      </svg>
    );
  }

  return (
    <svg width={width} height={height} aria-hidden="true" className="overflow-visible">
      <path d={path} fill="none" stroke={stroke} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}
