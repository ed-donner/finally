"use client";

import type { ConnectionStatus } from "@/lib/types";
import { formatCurrency } from "@/lib/format";

interface HeaderProps {
  totalValue: number;
  cashBalance: number;
  status: ConnectionStatus;
}

const statusColors: Record<ConnectionStatus, string> = {
  connected: "bg-gain",
  connecting: "bg-accent-yellow",
  disconnected: "bg-loss",
};

const statusLabels: Record<ConnectionStatus, string> = {
  connected: "Live",
  connecting: "Connecting...",
  disconnected: "Disconnected",
};

export default function Header({ totalValue, cashBalance, status }: HeaderProps) {
  return (
    <header className="flex items-center justify-between border-b border-border bg-bg-panel px-4 py-2">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-bold text-accent-yellow tracking-wide">FinAlly</h1>
        <span className="text-text-muted text-xs">AI Trading Workstation</span>
      </div>

      <div className="flex items-center gap-6 text-sm">
        <div>
          <span className="text-text-muted mr-2">Portfolio</span>
          <span className="font-bold text-text-primary">{formatCurrency(totalValue)}</span>
        </div>
        <div>
          <span className="text-text-muted mr-2">Cash</span>
          <span className="font-bold text-text-primary">{formatCurrency(cashBalance)}</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className={`inline-block h-2 w-2 rounded-full ${statusColors[status]}`} />
          <span className="text-text-muted text-xs">{statusLabels[status]}</span>
        </div>
      </div>
    </header>
  );
}
