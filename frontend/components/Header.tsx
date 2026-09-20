import { fmtPct, fmtSignedUsd, fmtUsd, signClass } from "@/utils/format";
import type { ConnectionStatus, Portfolio } from "@/utils/types";

const STATUS: Record<ConnectionStatus, { dot: string; label: string; pulse: boolean }> = {
  connected: { dot: "bg-up", label: "Live", pulse: false },
  reconnecting: { dot: "bg-accent", label: "Reconnecting", pulse: true },
  disconnected: { dot: "bg-down", label: "Disconnected", pulse: false },
};

interface HeaderProps {
  portfolio: Portfolio | null;
  status: ConnectionStatus;
  mock: boolean;
  chatOpen: boolean;
  onToggleChat: () => void;
}

export function Header({ portfolio, status, mock, chatOpen, onToggleChat }: HeaderProps) {
  const s = STATUS[status];
  const pnlPct = portfolio && portfolio.starting_cash ? (portfolio.total_pnl / portfolio.starting_cash) * 100 : null;

  return (
    <header className="sticky top-0 z-40 flex flex-wrap items-center gap-x-8 gap-y-2 border-b border-line bg-canvas/95 px-4 py-2 backdrop-blur">
      <div className="flex items-center gap-2">
        <span className="text-lg font-bold tracking-tight text-accent">FinAlly</span>
        <span className="hidden text-[11px] uppercase tracking-widest text-muted sm:inline">AI Trading Workstation</span>
        {mock && (
          <span className="rounded border border-secondary px-1.5 py-0.5 text-[10px] font-semibold uppercase text-secondary">
            mock data
          </span>
        )}
      </div>

      <dl className="flex items-center gap-8">
        <Stat testId="header-total" label="Portfolio value" value={fmtUsd(portfolio?.total_value)} big />
        <Stat
          testId="header-pnl"
          label="Total P&L"
          value={portfolio ? `${fmtSignedUsd(portfolio.total_pnl)} (${fmtPct(pnlPct)})` : "—"}
          className={signClass(portfolio?.total_pnl)}
        />
        <Stat testId="header-cash" label="Cash" value={fmtUsd(portfolio?.cash_balance)} />
      </dl>

      <div className="ml-auto flex items-center gap-4">
        <div
          className="flex items-center gap-2"
          title={`Price stream: ${s.label}`}
          role="status"
          data-testid="connection-status"
          data-status={status}
        >
          <span className={`h-2.5 w-2.5 rounded-full ${s.dot} ${s.pulse ? "pulse-dot" : ""}`} />
          <span className="text-[11px] uppercase tracking-wider text-muted">{s.label}</span>
        </div>
        <button
          onClick={onToggleChat}
          className="rounded border border-line px-3 py-1 text-xs text-primary hover:border-primary"
          aria-pressed={chatOpen}
        >
          {chatOpen ? "Hide AI" : "Show AI"}
        </button>
      </div>
    </header>
  );
}

function Stat({
  label,
  value,
  testId,
  big,
  className = "",
}: {
  label: string;
  value: string;
  testId: string;
  big?: boolean;
  className?: string;
}) {
  return (
    <div>
      <dt className="text-[10px] uppercase tracking-wider text-muted">{label}</dt>
      <dd data-testid={testId} className={`num ${big ? "text-xl font-semibold text-white" : "text-sm"} ${className}`}>{value}</dd>
    </div>
  );
}
