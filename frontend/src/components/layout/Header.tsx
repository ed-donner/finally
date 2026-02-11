"use client";

import { usePortfolioStore } from "@/stores/portfolio-store";
import { ConnectionDot } from "@/components/ui/ConnectionDot";

const currencyFormat = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
});

export function Header() {
  const totalValue = usePortfolioStore((s) => s.totalValue);
  const cashBalance = usePortfolioStore((s) => s.cashBalance);
  const loading = usePortfolioStore((s) => s.loading);

  return (
    <header className="h-12 flex items-center justify-between px-4 bg-terminal-bg border-b border-terminal-border">
      <div className="flex items-center gap-2">
        <span className="text-accent-yellow font-mono font-bold text-lg">
          FinAlly
        </span>
      </div>

      <div className="flex items-center gap-8">
        <div className="flex flex-col items-center">
          <span className="text-text-muted text-xs uppercase tracking-wider">
            Portfolio Value
          </span>
          <span className="text-text-primary font-mono text-sm">
            {loading ? "---" : currencyFormat.format(totalValue)}
          </span>
        </div>

        <div className="flex flex-col items-center">
          <span className="text-text-muted text-xs uppercase tracking-wider">
            Cash
          </span>
          <span className="text-text-primary font-mono text-sm">
            {loading ? "---" : currencyFormat.format(cashBalance)}
          </span>
        </div>
      </div>

      <ConnectionDot />
    </header>
  );
}
