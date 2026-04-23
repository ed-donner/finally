"use client";

import { useMemo } from "react";

type Point = { time: number; price: number };

export function Sparkline({
  points,
  width = 72,
  height = 20,
  color = "#8b949e",
}: {
  points: Point[];
  width?: number;
  height?: number;
  color?: string;
}) {
  const path = useMemo(() => {
    if (points.length < 2) return "";
    const prices = points.map((p) => p.price);
    const min = Math.min(...prices);
    const max = Math.max(...prices);
    const span = max - min || 1;
    const step = width / (points.length - 1);
    return points
      .map((p, i) => {
        const x = i * step;
        const y = height - ((p.price - min) / span) * height;
        return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
  }, [points, width, height]);

  if (!path) {
    return (
      <div
        className="inline-block"
        style={{ width, height, background: "repeating-linear-gradient(90deg, #1f2630 0 1px, transparent 1px 4px)" }}
      />
    );
  }

  return (
    <svg width={width} height={height} className="inline-block overflow-visible">
      <path d={path} fill="none" stroke={color} strokeWidth={1.25} strokeLinejoin="round" />
    </svg>
  );
}
