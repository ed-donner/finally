"use client";

import { ConnectionStatus } from "../lib/types";

const statusColors: Record<ConnectionStatus, string> = {
  connected: "bg-green",
  reconnecting: "bg-accent-yellow",
  disconnected: "bg-red",
};

interface HeaderProps {
  totalValue: number;
  cash: number;
  connectionStatus: ConnectionStatus;
}

export default function Header({ totalValue, cash, connectionStatus }: HeaderProps) {
  const pnl = totalValue - 10000;
  const pnlColor = pnl >= 0 ? "text-green" : "text-red";
  const pnlSign = pnl >= 0 ? "+" : "";

  return (
    <header className="flex items-center justify-between border-b border-border bg-bg-secondary px-4 py-2">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-bold text-accent-yellow tracking-wider">FinAlly</h1>
        <span className="text-text-secondary text-xs">AI Trading Workstation</span>
      </div>
      <div className="flex items-center gap-6 text-sm">
        <div>
          <span className="text-text-secondary mr-1">Portfolio:</span>
          <span className="font-bold">${totalValue.toLocaleString("en-US", { minimumFractionDigits: 2 })}</span>
          <span className={`ml-2 ${pnlColor}`}>
            {pnlSign}${pnl.toLocaleString("en-US", { minimumFractionDigits: 2 })}
          </span>
        </div>
        <div>
          <span className="text-text-secondary mr-1">Cash:</span>
          <span>${cash.toLocaleString("en-US", { minimumFractionDigits: 2 })}</span>
        </div>
        <div className="flex items-center gap-1.5">
          <div className={`w-2 h-2 rounded-full ${statusColors[connectionStatus]}`} />
          <span className="text-text-secondary text-xs capitalize">{connectionStatus}</span>
        </div>
      </div>
    </header>
  );
}
