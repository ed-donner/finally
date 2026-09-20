import type { UTCTimestamp } from "lightweight-charts";

export const CHART_COLORS = {
  text: "#8b949e",
  grid: "#21262d",
  border: "#30363d",
  primary: "#209dd7",
  accent: "#ecad0a",
};

export interface SeriesPoint {
  time: UTCTimestamp;
  value: number;
}

/**
 * Lightweight Charts requires strictly ascending, unique integer-second timestamps. Ticks arrive
 * every ~500ms, so several share a second: keep the last value of each second.
 */
export function toSeriesData(points: { t: number; v: number }[]): SeriesPoint[] {
  const out: SeriesPoint[] = [];
  for (const { t, v } of points) {
    const time = Math.floor(t) as UTCTimestamp;
    const last = out[out.length - 1];
    if (last && time < last.time) continue; // out-of-order: drop
    if (last && time === last.time) last.value = v;
    else out.push({ time, value: v });
  }
  return out;
}
