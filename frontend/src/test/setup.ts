import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
});

// Lightweight Charts needs a real canvas; tests only check our wrappers.
vi.mock("lightweight-charts", () => {
  const series = { setData: vi.fn(), update: vi.fn() };
  const chart = {
    addSeries: vi.fn(() => series),
    remove: vi.fn(),
    timeScale: vi.fn(() => ({ fitContent: vi.fn() })),
    applyOptions: vi.fn(),
  };
  return {
    createChart: vi.fn(() => chart),
    AreaSeries: "Area",
    BaselineSeries: "Baseline",
    LineSeries: "Line",
    ColorType: { Solid: "solid", VerticalGradient: "gradient" },
  };
});
