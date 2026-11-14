import type { Portfolio, Position, PriceMap } from "./types";

export interface LiveTotals {
  cash: number;
  positionsValue: number;
  totalValue: number;
  unrealizedPnl: number;
  /** Unrealized P&L as a percent of cost basis. */
  unrealizedPnlPercent: number;
}

/**
 * Revalue positions with the latest streamed prices. Falls back to the REST
 * `current_price` when no tick has arrived for a ticker. Weights are each
 * position's share of total market value. Returns new objects (safe to sort).
 */
export function livePositions(positions: Position[], prices: PriceMap): Position[] {
  const revalued: Position[] = positions.map((p) => {
    const tick = prices[p.ticker];
    const current_price = tick && Number.isFinite(tick.price) ? tick.price : p.current_price;
    const market_value = p.quantity * current_price;
    const cost = p.quantity * p.avg_cost;
    const unrealized_pnl = market_value - cost;
    const unrealized_pnl_percent = cost !== 0 ? (unrealized_pnl / cost) * 100 : 0;
    return { ...p, current_price, market_value, unrealized_pnl, unrealized_pnl_percent, weight: 0 };
  });
  const total = revalued.reduce((s, p) => s + p.market_value, 0);
  for (const p of revalued) p.weight = total > 0 ? p.market_value / total : 0;
  return revalued;
}

/** Live header totals: cash plus revalued positions. Null until the portfolio loads. */
export function liveTotals(portfolio: Portfolio | null | undefined, prices: PriceMap): LiveTotals | null {
  if (!portfolio) return null;
  const positions = livePositions(portfolio.positions, prices);
  const positionsValue = positions.reduce((s, p) => s + p.market_value, 0);
  const cost = positions.reduce((s, p) => s + p.quantity * p.avg_cost, 0);
  const unrealizedPnl = positionsValue - cost;
  return {
    cash: portfolio.cash_balance,
    positionsValue,
    totalValue: portfolio.cash_balance + positionsValue,
    unrealizedPnl,
    unrealizedPnlPercent: cost !== 0 ? (unrealizedPnl / cost) * 100 : 0,
  };
}
