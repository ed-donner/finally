import { describe, expect, it } from "vitest";
import { liveTotals, livePositions } from "./portfolio";
import { squarify } from "./treemap";
import { formatMoney, formatPercent, formatSignedMoney } from "./format";
import { toChartSeries } from "./chartTheme";
import { portfolioFixture, priceUpdate } from "@/test/fixtures";

describe("portfolio calculations", () => {
  it("revalues positions with streamed prices", () => {
    const [p] = livePositions(portfolioFixture.positions, { AAPL: priceUpdate("AAPL", 200, 1) });
    expect(p.current_price).toBe(200);
    expect(p.market_value).toBe(2000);
    expect(p.unrealized_pnl).toBe(100);
    expect(p.unrealized_pnl_percent).toBeCloseTo(5.263, 2);
    expect(p.weight).toBe(1);
  });

  it("falls back to the REST price when no tick has arrived", () => {
    const [p] = livePositions(portfolioFixture.positions, {});
    expect(p.current_price).toBe(195);
    expect(p.market_value).toBe(1950);
  });

  it("computes live totals as cash plus positions", () => {
    const t = liveTotals(portfolioFixture, { AAPL: priceUpdate("AAPL", 180, 1) })!;
    expect(t.cash).toBe(8100);
    expect(t.positionsValue).toBe(1800);
    expect(t.totalValue).toBe(9900);
    expect(t.unrealizedPnl).toBe(-100);
    expect(t.unrealizedPnlPercent).toBeCloseTo(-5.263, 2);
  });

  it("splits weights across positions", () => {
    const positions = livePositions(
      [
        { ...portfolioFixture.positions[0] },
        { ...portfolioFixture.positions[0], ticker: "MSFT", quantity: 30, current_price: 100 },
      ],
      {},
    );
    expect(positions[0].weight + positions[1].weight).toBeCloseTo(1);
    expect(positions[1].weight).toBeCloseTo(3000 / 4950);
  });

  it("returns null without a portfolio", () => {
    expect(liveTotals(null, {})).toBeNull();
  });
});

describe("squarify", () => {
  it("fills the box with areas proportional to value", () => {
    const rects = squarify(
      [
        { key: "A", value: 6 },
        { key: "B", value: 3 },
        { key: "C", value: 1 },
      ],
      100,
      50,
    );
    expect(rects).toHaveLength(3);
    const area = (k: string) => {
      const r = rects.find((x) => x.key === k)!;
      return r.w * r.h;
    };
    expect(area("A")).toBeCloseTo(3000);
    expect(area("B")).toBeCloseTo(1500);
    expect(area("C")).toBeCloseTo(500);
    for (const r of rects) {
      expect(r.x).toBeGreaterThanOrEqual(-1e-9);
      expect(r.y).toBeGreaterThanOrEqual(-1e-9);
      expect(r.x + r.w).toBeLessThanOrEqual(100 + 1e-9);
      expect(r.y + r.h).toBeLessThanOrEqual(50 + 1e-9);
    }
  });

  it("drops non-positive values and handles empty input", () => {
    expect(squarify([{ key: "A", value: 0 }], 10, 10)).toEqual([]);
    expect(squarify([], 10, 10)).toEqual([]);
  });
});

describe("formatting", () => {
  it("formats money and signs", () => {
    expect(formatMoney(10000)).toBe("$10,000.00");
    expect(formatSignedMoney(-4)).toBe("−$4.00");
    expect(formatSignedMoney(12.3)).toBe("+$12.30");
    expect(formatPercent(1.234)).toBe("+1.23%");
    expect(formatPercent(null)).toBe("—");
  });
});

describe("toChartSeries", () => {
  it("collapses sub-second ticks to one point per second", () => {
    expect(
      toChartSeries([
        { time: 100.2, value: 1 },
        { time: 100.7, value: 2 },
        { time: 101.1, value: 3 },
      ]),
    ).toEqual([
      { time: 100, value: 2 },
      { time: 101, value: 3 },
    ]);
  });
});
