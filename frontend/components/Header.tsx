'use client';

import type { ConnectionStatus, Portfolio } from '@/lib/types';
import { formatUSD, formatPercent, plColor } from '@/lib/format';
import ConnectionDot from './ConnectionDot';

interface HeaderProps {
  portfolio: Portfolio | null;
  status: ConnectionStatus;
}

export default function Header({ portfolio, status }: HeaderProps) {
  const totalPct =
    portfolio && portfolio.positions_value > 0
      ? (portfolio.total_unrealized_pl /
          (portfolio.positions_value - portfolio.total_unrealized_pl || 1)) *
        100
      : 0;

  return (
    <header className="flex items-center justify-between px-4 h-14 border-b border-terminal-border bg-terminal-panel shrink-0">
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2">
          <span className="text-accent text-lg font-bold tracking-tight">Fin</span>
          <span className="text-brand text-lg font-bold tracking-tight -ml-2">Ally</span>
        </div>
        <span className="hidden sm:inline text-[11px] uppercase tracking-widest text-terminal-muted border-l border-terminal-border pl-3">
          AI Trading Workstation
        </span>
      </div>

      <div className="flex items-center gap-6">
        <div className="text-right">
          <div className="text-[10px] uppercase tracking-wider text-terminal-muted">Cash</div>
          <div className="text-sm num text-terminal-text" data-testid="cash-balance">
            {formatUSD(portfolio?.cash_balance ?? null)}
          </div>
        </div>

        <div className="text-right">
          <div className="text-[10px] uppercase tracking-wider text-terminal-muted">
            Total Value
          </div>
          <div className="flex items-baseline gap-2 justify-end">
            <span className="text-lg font-semibold num text-terminal-text" data-testid="total-value">
              {formatUSD(portfolio?.total_value ?? null)}
            </span>
            {portfolio && (
              <span className={`text-xs num ${plColor(portfolio.total_unrealized_pl)}`} data-testid="total-pl-pct">
                {formatPercent(totalPct)}
              </span>
            )}
          </div>
        </div>

        <ConnectionDot status={status} />
      </div>
    </header>
  );
}
