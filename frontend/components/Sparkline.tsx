import { memo } from "react";
import type { PricePoint } from "@/utils/types";

interface SparklineProps {
  points: PricePoint[];
  width?: number;
  height?: number;
}

/** Tiny dependency-free SVG line. Green if the series is up since its first point, red if down. */
export const Sparkline = memo(function Sparkline({ points, width = 84, height = 24 }: SparklineProps) {
  if (points.length < 2) {
    // Fills in progressively as ticks arrive (PLAN.md section 2).
    return <div style={{ width, height }} className="flex items-center text-[10px] text-muted">···</div>;
  }
  const values = points.map((p) => p.p);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pad = 2;
  const path = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * (width - pad * 2) + pad;
      const y = height - pad - ((v - min) / span) * (height - pad * 2);
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const color = values[values.length - 1] >= values[0] ? "#3fb950" : "#f85149";

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden>
      <path d={path} fill="none" stroke={color} strokeWidth={1.4} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
});
