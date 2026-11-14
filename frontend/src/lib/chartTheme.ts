import { ColorType, type DeepPartial, type ChartOptions } from "lightweight-charts";
import type { PricePoint } from "./types";

export const chartColors = {
  text: "#7c8796",
  grid: "rgba(37, 48, 65, 0.55)",
  border: "#253041",
  blue: "#209dd7",
  accent: "#ecad0a",
  up: "#2ec27e",
  down: "#f0505e",
};

export const baseChartOptions: DeepPartial<ChartOptions> = {
  autoSize: true,
  layout: {
    background: { type: ColorType.Solid, color: "transparent" },
    textColor: chartColors.text,
    fontFamily: "'IBM Plex Mono', ui-monospace, monospace",
    fontSize: 11,
    attributionLogo: false,
  },
  grid: {
    vertLines: { color: chartColors.grid },
    horzLines: { color: chartColors.grid },
  },
  rightPriceScale: { borderColor: chartColors.border },
  timeScale: { borderColor: chartColors.border, timeVisible: true, secondsVisible: true },
  crosshair: {
    vertLine: { color: "#4f5a69", labelBackgroundColor: "#1a2230" },
    horzLine: { color: "#4f5a69", labelBackgroundColor: "#1a2230" },
  },
};

/** Lightweight Charts needs strictly increasing integer-second times; keep the last value per second. */
export function toChartSeries(points: PricePoint[]): { time: number; value: number }[] {
  const out: { time: number; value: number }[] = [];
  for (const p of points) {
    const t = Math.floor(p.time);
    const last = out[out.length - 1];
    if (last && t < last.time) continue;
    if (last && last.time === t) last.value = p.value;
    else out.push({ time: t, value: p.value });
  }
  return out;
}
