import { Panel } from '@/src/components/Panel';
import { compactNumber, money, pct } from '@/src/lib/format';
import { Position } from '@/src/types/trading';

export const Heatmap = ({ positions }: { positions: Position[] }) => {
  const totalValue = positions.reduce((sum, item) => sum + item.current_price * item.quantity, 0);

  return (
    <Panel title="Position Heatmap" className="h-full">
      <div className="grid h-[230px] grid-cols-2 gap-2 overflow-hidden rounded border border-terminal-border bg-terminal-panelAlt/30 p-2">
        {positions.length === 0 && <p className="col-span-2 text-sm text-terminal-dim">No open positions.</p>}
        {positions.map((position) => {
          const value = position.current_price * position.quantity;
          const weight = totalValue > 0 ? value / totalValue : 0;
          const positive = position.unrealized_pnl >= 0;
          const alpha = Math.min(0.8, 0.2 + Math.abs(position.change_percent) / 50);

          return (
            <div
              key={position.ticker}
              style={{
                gridColumn: weight > 0.4 ? 'span 2' : 'span 1',
                backgroundColor: positive ? `rgba(46,214,143,${alpha})` : `rgba(242,100,120,${alpha})`,
              }}
              className="flex min-h-[86px] flex-col justify-between rounded border border-terminal-border p-2"
            >
              <div className="flex items-center justify-between">
                <p className="font-semibold text-terminal-text">{position.ticker}</p>
                <p className="text-xs text-terminal-bg">{pct(position.change_percent)}</p>
              </div>
              <p className="text-xs text-terminal-bg">{compactNumber(position.quantity)} sh</p>
              <p className="text-sm font-semibold text-terminal-bg">{money(value)}</p>
            </div>
          );
        })}
      </div>
    </Panel>
  );
};
