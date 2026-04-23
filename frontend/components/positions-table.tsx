"use client";

import { Panel } from "@/components/ui/panel";
import { cn } from "@/lib/cn";
import { pct, qty, usd } from "@/lib/format";
import { usePortfolio } from "@/lib/store/portfolio";
import { usePrices } from "@/lib/store/prices";

export function PositionsTable() {
  const { portfolio } = usePortfolio();
  const { prices } = usePrices();
  const positions = portfolio?.positions ?? [];

  return (
    <Panel title="Positions" accent={`${positions.length}`}>
      {positions.length === 0 ? (
        <div className="grid place-items-center w-full h-full min-h-[120px] text-dim text-xs uppercase tracking-[0.3em]">
          no open positions
        </div>
      ) : (
        <table className="w-full text-xs">
          <thead className="text-[9px] uppercase tracking-[0.22em] text-dim border-b border-line">
            <tr>
              <th className="text-left px-3 py-2 font-normal">Sym</th>
              <th className="text-right px-2 py-2 font-normal">Qty</th>
              <th className="text-right px-2 py-2 font-normal">Avg</th>
              <th className="text-right px-2 py-2 font-normal">Last</th>
              <th className="text-right px-2 py-2 font-normal">Mkt val</th>
              <th className="text-right px-3 py-2 font-normal">P&amp;L</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {positions.map((p) => {
              // Prefer streaming price for live updates between portfolio refreshes
              const live = prices[p.ticker]?.price ?? p.current_price ?? p.avg_cost;
              const mv = live * p.quantity;
              const pnl = mv - p.avg_cost * p.quantity;
              const pnlPct = (pnl / (p.avg_cost * p.quantity)) * 100;
              return (
                <tr key={p.ticker} className="border-b border-line/60 hover:bg-[#0e131a]">
                  <td className="px-3 py-2 text-ink tracking-wider">{p.ticker}</td>
                  <td className="px-2 py-2 text-right text-muted tabular-nums">{qty(p.quantity)}</td>
                  <td className="px-2 py-2 text-right text-muted tabular-nums">{usd(p.avg_cost)}</td>
                  <td className="px-2 py-2 text-right text-ink tabular-nums">{usd(live)}</td>
                  <td className="px-2 py-2 text-right text-ink tabular-nums">{usd(mv)}</td>
                  <td
                    className={cn(
                      "px-3 py-2 text-right tabular-nums",
                      pnl > 0 ? "text-up" : pnl < 0 ? "text-down" : "text-muted",
                    )}
                  >
                    {pnl >= 0 ? "+" : ""}
                    {usd(pnl)}{" "}
                    <span className="text-[10px] text-dim">{pct(pnlPct)}</span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
