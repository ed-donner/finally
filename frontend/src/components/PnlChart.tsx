import { Panel } from '@/src/components/Panel';
import { Sparkline } from '@/src/components/Sparkline';
import { money } from '@/src/lib/format';
import { PortfolioSnapshot } from '@/src/types/trading';

export const PnlChart = ({ data }: { data: PortfolioSnapshot[] }) => {
  const values = data.map((item) => item.total_value);
  const latest = values[values.length - 1] ?? 10000;
  const start = values[0] ?? 10000;

  return (
    <Panel title="Portfolio P&L" className="h-full">
      <div className="rounded border border-terminal-border bg-terminal-bg/40 p-3">
        <Sparkline values={values.length > 1 ? values : [start, latest]} stroke="#ecad0a" height={110} />
        <div className="mt-2 flex items-center justify-between text-xs text-terminal-dim">
          <span>Start {money(start)}</span>
          <span className={latest - start >= 0 ? 'text-terminal-positive' : 'text-terminal-negative'}>{money(latest - start)}</span>
        </div>
      </div>
    </Panel>
  );
};
