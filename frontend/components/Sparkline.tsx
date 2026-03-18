"use client";

import { useMemo } from "react";

interface SparklineProps {
  data: { price: number }[];
  width?: number;
  height?: number;
  color?: string;
}

export function Sparkline({
  data,
  width = 80,
  height = 24,
  color = "#209dd7",
}: SparklineProps) {
  const path = useMemo(() => {
    if (data.length < 2) return "";
    const prices = data.map((d) => d.price);
    const min = Math.min(...prices);
    const max = Math.max(...prices);
    const range = max - min || 1;
    const xStep = width / (data.length - 1);

    return prices
      .map((p, i) => {
        const x = i * xStep;
        const y = height - ((p - min) / range) * (height - 2) - 1;
        return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
  }, [data, width, height]);

  if (data.length < 2) {
    return <div style={{ width, height }} />;
  }

  // Determine color based on first vs last price
  const actualColor =
    data[data.length - 1].price >= data[0].price
      ? "var(--color-gain)"
      : "var(--color-loss)";

  return (
    <svg width={width} height={height} className="inline-block">
      <path
        d={path}
        fill="none"
        stroke={color === "#209dd7" ? actualColor : color}
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
