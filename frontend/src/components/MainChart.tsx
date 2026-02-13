import { Panel } from '@/src/components/Panel';
import { Sparkline } from '@/src/components/Sparkline';
import { money } from '@/src/lib/format';

interface MainChartProps {
  ticker: string;
  series: number[];
}

export const MainChart = ({ ticker, series }: MainChartProps) => {
  const latest = series[series.length - 1] ?? 0;
  const start = series[0] ?? latest;
  const delta = latest - start;

  return (
    <Panel
      title={`Main Chart · ${ticker}`}
      rightSlot={<p className={`text-xs ${delta >= 0 ? 'text-terminal-positive' : 'text-terminal-negative'}`}>{money(delta)}</p>}
      className="flex h-full min-h-0 flex-col"
      contentClassName="flex min-h-0 flex-1"
    >
      <div className="flex h-full min-h-0 flex-1 flex-col rounded border border-terminal-border bg-terminal-bg/40 p-3">
        <div className="min-h-0 flex-1">
          <Sparkline values={series} stroke="#209dd7" height={180} className="h-full" />
        </div>
        <div className="mt-1.5 flex items-center justify-between text-xs text-terminal-dim">
          <span>Session open</span>
          <span className="text-terminal-text">{money(latest)}</span>
        </div>
      </div>
    </Panel>
  );
};
