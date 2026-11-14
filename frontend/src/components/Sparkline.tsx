import type { PricePoint } from "@/lib/types";

/** Tiny SVG line of the ticks seen since page load. */
export function Sparkline({
  points,
  width = 76,
  height = 24,
}: {
  points: PricePoint[];
  width?: number;
  height?: number;
}) {
  if (points.length < 2) {
    return (
      <svg width={width} height={height} aria-hidden="true" className="opacity-40">
        <line x1="0" x2={width} y1={height / 2} y2={height / 2} stroke="var(--color-dim)" strokeDasharray="2 3" />
      </svg>
    );
  }
  const values = points.map((p) => p.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const step = width / (points.length - 1);
  const d = values
    .map((v, i) => `${(i * step).toFixed(1)},${(height - 2 - ((v - min) / span) * (height - 4)).toFixed(1)}`)
    .join(" ");
  const up = values[values.length - 1] >= values[0];
  return (
    <svg width={width} height={height} aria-hidden="true" data-testid="sparkline">
      <polyline
        points={d}
        fill="none"
        stroke={up ? "var(--color-up)" : "var(--color-down)"}
        strokeWidth="1.25"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}
