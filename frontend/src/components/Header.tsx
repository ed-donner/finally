"use client";

import { useAppData } from "./AppDataProvider";
import { liveTotals } from "@/lib/portfolio";
import { useConnectionStatus, usePrices } from "@/lib/priceStore";
import { formatMoney, formatPercent, formatSignedMoney, toneClass } from "@/lib/format";
import type { ConnectionStatus } from "@/lib/types";

const STATUS_LABEL: Record<ConnectionStatus, string> = {
  connected: "Live",
  reconnecting: "Reconnecting",
  disconnected: "Offline",
};

export function ConnectionDot({ status }: { status: ConnectionStatus }) {
  return (
    <div
      data-testid="connection-status"
      data-status={status}
      role="status"
      aria-label={`Price stream ${STATUS_LABEL[status].toLowerCase()}`}
      className="flex items-center gap-2 text-xs text-muted"
    >
      <span className={`status-dot status-${status}`} />
      <span>{STATUS_LABEL[status]}</span>
    </div>
  );
}

export function Header() {
  const { portfolio } = useAppData();
  const prices = usePrices();
  const status = useConnectionStatus();
  const totals = liveTotals(portfolio, prices);

  return (
    <header className="flex h-14 shrink-0 items-stretch border-b border-line bg-ink-1">
      <div className="flex items-center gap-2 border-r border-line px-4">
        <span className="brand-mark" aria-hidden="true" />
        <span className="text-lg font-semibold tracking-tight text-text">
          Fin<span className="text-accent">Ally</span>
        </span>
      </div>

      <div className="flex min-w-0 flex-1 items-stretch">
        <div className="flex flex-col justify-center border-r border-line px-5">
          <span className="text-[11px] text-muted">Portfolio value</span>
          <span data-testid="header-total-value" className="num text-xl font-medium leading-tight text-accent">
            {formatMoney(totals?.totalValue)}
          </span>
        </div>
        <HeaderStat label="Cash" testId="header-cash" value={formatMoney(totals?.cash)} />
        <HeaderStat label="Invested" value={formatMoney(totals?.positionsValue)} />
        <HeaderStat
          label="Unrealized P&L"
          value={
            totals ? (
              <span className={toneClass(totals.unrealizedPnl)}>
                {formatSignedMoney(totals.unrealizedPnl)}{" "}
                <span className="text-xs">({formatPercent(totals.unrealizedPnlPercent)})</span>
              </span>
            ) : (
              "—"
            )
          }
          hideOnSmall
        />
      </div>

      <div className="flex items-center px-4">
        <ConnectionDot status={status} />
      </div>
    </header>
  );
}

function HeaderStat({
  label,
  value,
  testId,
  hideOnSmall,
}: {
  label: string;
  value: React.ReactNode;
  testId?: string;
  hideOnSmall?: boolean;
}) {
  return (
    <div
      className={`flex flex-col justify-center border-r border-line px-5 ${hideOnSmall ? "max-md:hidden" : ""}`}
    >
      <span className="text-[11px] text-muted">{label}</span>
      <span data-testid={testId} className="num text-sm leading-tight text-text">
        {value}
      </span>
    </div>
  );
}
