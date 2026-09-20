import { fmtPct, fmtQty, fmtSignedUsd, fmtUsd, signClass } from "@/utils/format";
import type { Portfolio } from "@/utils/types";
import { Panel, PanelMessage } from "./Panel";

interface PositionsTableProps {
  portfolio: Portfolio | null;
  loading: boolean;
  error: string | null;
  selected: string | null;
  onSelect: (ticker: string) => void;
}

export function PositionsTable({ portfolio, loading, error, selected, onSelect }: PositionsTableProps) {
  const positions = [...(portfolio?.positions ?? [])].sort((a, b) => b.market_value - a.market_value);

  return (
    <Panel
      title="Positions"
      right={<span className="text-[11px] text-muted">{positions.length} open</span>}
      bodyClassName="overflow-auto"
    >
      {loading ? (
        <PanelMessage>Loading positions…</PanelMessage>
      ) : error && !portfolio ? (
        <PanelMessage>
          <span className="text-down">{error}</span>
        </PanelMessage>
      ) : !positions.length ? (
        <PanelMessage>No open positions.</PanelMessage>
      ) : (
        <table className="w-full" data-testid="positions-table">
          <thead className="sticky top-0 bg-panel text-[10px] uppercase tracking-wider text-muted">
            <tr>
              <th className="px-3 py-1.5 text-left font-medium">Ticker</th>
              <th className="px-2 py-1.5 text-right font-medium">Qty</th>
              <th className="px-2 py-1.5 text-right font-medium">Avg cost</th>
              <th className="px-2 py-1.5 text-right font-medium">Price</th>
              <th className="px-2 py-1.5 text-right font-medium">Unrealized P&L</th>
              <th className="px-3 py-1.5 text-right font-medium">Chg %</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <tr
                key={p.ticker}
                data-testid={`position-row-${p.ticker}`}
                onClick={() => onSelect(p.ticker)}
                className={`cursor-pointer border-b border-line/40 hover:bg-raised ${p.ticker === selected ? "bg-raised" : ""}`}
              >
                <td className="px-3 py-1.5 font-semibold text-white">{p.ticker}</td>
                <td className="num px-2 py-1.5 text-right">{fmtQty(p.quantity)}</td>
                <td className="num px-2 py-1.5 text-right">{fmtUsd(p.avg_cost)}</td>
                <td className="num px-2 py-1.5 text-right">{fmtUsd(p.current_price)}</td>
                <td className={`num px-2 py-1.5 text-right ${signClass(p.unrealized_pnl)}`}>{fmtSignedUsd(p.unrealized_pnl)}</td>
                <td className={`num px-3 py-1.5 text-right ${signClass(p.pnl_percent)}`}>{fmtPct(p.pnl_percent)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
