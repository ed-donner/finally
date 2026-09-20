import type { Portfolio, PriceTick } from "./types";

/**
 * Re-value a portfolio snapshot at the latest streamed prices, so the header, positions table
 * and heatmap tick live between (infrequent) /api/portfolio fetches. Cash is unchanged.
 */
export function revalue(p: Portfolio, prices: Record<string, PriceTick>): Portfolio {
  const positions = p.positions.map((pos) => {
    const price = prices[pos.ticker]?.price ?? pos.current_price ?? pos.avg_cost;
    const market_value = pos.quantity * price;
    const basis = pos.quantity * pos.avg_cost;
    return {
      ...pos,
      current_price: price,
      market_value,
      unrealized_pnl: market_value - basis,
      pnl_percent: basis ? ((market_value - basis) / basis) * 100 : 0,
    };
  });
  const positions_value = positions.reduce((s, x) => s + x.market_value, 0);
  const total_value = p.cash_balance + positions_value;
  return {
    ...p,
    positions,
    positions_value,
    total_value,
    unrealized_pnl: positions.reduce((s, x) => s + x.unrealized_pnl, 0),
    total_pnl: total_value - p.starting_cash,
  };
}
