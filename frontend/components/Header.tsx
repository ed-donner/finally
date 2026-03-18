"use client";

import { useStore } from "@/lib/store";
import { formatCurrency } from "@/lib/format";

const statusColors: Record<string, string> = {
  connected: "bg-gain",
  connecting: "bg-accent-yellow",
  disconnected: "bg-loss",
};

export function Header() {
  const connectionStatus = useStore((s) => s.connectionStatus);
  const portfolio = useStore((s) => s.portfolio);
  const toggleChat = useStore((s) => s.toggleChat);
  const chatOpen = useStore((s) => s.chatOpen);

  return (
    <header className="flex items-center justify-between px-4 py-2 bg-bg-secondary border-b border-border">
      <div className="flex items-center gap-4">
        <h1 className="text-lg font-bold text-accent-yellow tracking-wider">
          FinAlly
        </h1>
        <div className="flex items-center gap-1.5">
          <span
            className={`w-2 h-2 rounded-full ${statusColors[connectionStatus]}`}
          />
          <span className="text-xs text-text-secondary">{connectionStatus}</span>
        </div>
      </div>

      <div className="flex items-center gap-6">
        {portfolio && (
          <>
            <div className="text-right">
              <div className="text-xs text-text-secondary">Portfolio Value</div>
              <div className="text-sm font-bold text-text-primary">
                {formatCurrency(portfolio.total_value)}
              </div>
            </div>
            <div className="text-right">
              <div className="text-xs text-text-secondary">Cash</div>
              <div className="text-sm font-bold text-accent-yellow">
                {formatCurrency(portfolio.cash_balance)}
              </div>
            </div>
          </>
        )}
        <button
          onClick={toggleChat}
          className="px-3 py-1.5 text-xs bg-accent-purple text-white rounded hover:opacity-80 transition-opacity"
        >
          {chatOpen ? "Close Chat" : "AI Chat"}
        </button>
      </div>
    </header>
  );
}
