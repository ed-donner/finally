import { Panel } from '@/src/components/Panel';
import { Sparkline } from '@/src/components/Sparkline';
import { money } from '@/src/lib/format';
import { PortfolioSnapshot } from '@/src/types/trading';

export const PnlChart = ({ data }: { data: PortfolioSnapshot[] }) => {
  const values = data.map((item) => item.total_value);
  const latest = values[values.length - 1] ?? 10000;
  const start = values[0] ?? 10000;

  return (
    <Panel title="Portfolio P&L" className="flex h-full min-h-0 flex-col" contentClassName="flex min-h-0 flex-1">
      <div className="flex h-full min-h-0 flex-1 flex-col rounded border border-terminal-border bg-terminal-bg/40 p-3">
        <div className="min-h-0 flex-1">
          <Sparkline values={values.length > 1 ? values : [start, latest]} stroke="#ecad0a" height={180} className="h-full" />
        </div>
        <div className="mt-2 flex items-center justify-between text-xs text-terminal-dim">
          <span>Start {money(start)}</span>
          <span className={latest - start >= 0 ? 'text-terminal-positive' : 'text-terminal-negative'}>{money(latest - start)}</span>
        </div>
      </div>
    </Panel>
  );
};
