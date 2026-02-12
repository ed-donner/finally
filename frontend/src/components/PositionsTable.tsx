import { Panel } from '@/src/components/Panel';
import { money, pct } from '@/src/lib/format';
import { Position } from '@/src/types/trading';

export const PositionsTable = ({ positions }: { positions: Position[] }) => (
  <Panel title="Positions" className="h-full" testId="panel-positions">
    <div className="max-h-[240px] overflow-auto">
      <table className="w-full text-sm">
        <thead className="text-xs uppercase text-terminal-dim">
          <tr>
            <th className="px-2 py-1 text-left">Ticker</th>
            <th className="px-2 py-1 text-right">Qty</th>
            <th className="px-2 py-1 text-right">Avg Cost</th>
            <th className="px-2 py-1 text-right">Price</th>
            <th className="px-2 py-1 text-right">P&L</th>
            <th className="px-2 py-1 text-right">%</th>
          </tr>
        </thead>
        <tbody>
          {positions.length === 0 && (
            <tr>
              <td className="px-2 py-3 text-terminal-dim" colSpan={6}>
                No positions yet.
              </td>
            </tr>
          )}
          {positions.map((position) => (
            <tr data-testid={`position-row-${position.ticker}`} key={position.ticker} className="border-t border-terminal-border/60 text-terminal-text">
              <td className="px-2 py-1.5 font-semibold">{position.ticker}</td>
              <td className="px-2 py-1.5 text-right">{position.quantity.toFixed(4)}</td>
              <td className="px-2 py-1.5 text-right">{money(position.avg_cost)}</td>
              <td className="px-2 py-1.5 text-right">{money(position.current_price)}</td>
              <td className={`px-2 py-1.5 text-right ${position.unrealized_pnl >= 0 ? 'text-terminal-positive' : 'text-terminal-negative'}`}>
                {money(position.unrealized_pnl)}
              </td>
              <td className={`px-2 py-1.5 text-right ${position.change_percent >= 0 ? 'text-terminal-positive' : 'text-terminal-negative'}`}>
                {pct(position.change_percent)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  </Panel>
);
