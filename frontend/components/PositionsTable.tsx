'use client';

import type { Position } from '@/lib/types';
import { formatPrice, formatQty, formatSigned, formatUSD, formatPercent, plColor } from '@/lib/format';

interface PositionsTableProps {
  positions: Position[];
}

/** Tabular view of all holdings (PLAN §10). Values update at stream cadence. */
export default function PositionsTable({ positions }: PositionsTableProps) {
  return (
    <div className="panel h-full" data-testid="positions-table">
      <div className="panel-title">
        <span>Positions</span>
        <span className="text-terminal-muted/60">{positions.length}</span>
      </div>
      <div className="flex-1 overflow-y-auto">
        <table className="w-full text-sm">
          <thead className="sticky top-0 bg-terminal-panel z-10">
            <tr className="text-[10px] uppercase tracking-wider text-terminal-muted">
              <th className="text-left font-normal px-3 py-1.5">Symbol</th>
              <th className="text-right font-normal px-2 py-1.5">Qty</th>
              <th className="text-right font-normal px-2 py-1.5">Avg Cost</th>
              <th className="text-right font-normal px-2 py-1.5">Price</th>
              <th className="text-right font-normal px-2 py-1.5">Mkt Value</th>
              <th className="text-right font-normal px-2 py-1.5">Unreal. P&amp;L</th>
              <th className="text-right font-normal px-3 py-1.5">%</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <tr
                key={p.ticker}
                data-testid="positions-row"
                data-ticker={p.ticker}
                className="border-b border-terminal-border/40 hover:bg-terminal-raised/60"
              >
                <td className="px-3 py-1.5 font-semibold text-terminal-text">{p.ticker}</td>
                <td className="px-2 py-1.5 text-right num">{formatQty(p.quantity)}</td>
                <td className="px-2 py-1.5 text-right num text-terminal-muted">{formatPrice(p.avg_cost)}</td>
                <td className="px-2 py-1.5 text-right num">{formatPrice(p.current_price)}</td>
                <td className="px-2 py-1.5 text-right num">{formatUSD(p.market_value)}</td>
                <td className={`px-2 py-1.5 text-right num ${plColor(p.unrealized_pl)}`} data-testid="position-pl">
                  {formatSigned(p.unrealized_pl)}
                </td>
                <td className={`px-3 py-1.5 text-right num ${plColor(p.unrealized_pl_pct)}`}>
                  {formatPercent(p.unrealized_pl_pct)}
                </td>
              </tr>
            ))}
            {positions.length === 0 && (
              <tr>
                <td colSpan={7} className="text-center text-terminal-muted py-6 text-xs">
                  No open positions. Use the trade bar or ask the AI to buy something.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
