"use client";

import { TerminalGrid } from "@/components/layout/TerminalGrid";
import { WatchlistPanel } from "@/components/panels/WatchlistPanel";
import { ChartPanel } from "@/components/panels/ChartPanel";
import { PortfolioPanel } from "@/components/panels/PortfolioPanel";
import { ChatPanel } from "@/components/panels/ChatPanel";

export default function Home() {
  return (
    <div className="h-screen flex flex-col bg-terminal-bg">
      <div className="h-12 flex items-center px-4 border-b border-terminal-border">
        <span className="text-accent-yellow font-mono font-bold">FinAlly</span>
      </div>
      <TerminalGrid>
        <div className="col-span-3 row-span-2 bg-terminal-surface">
          <WatchlistPanel />
        </div>
        <div className="col-span-6 bg-terminal-surface">
          <ChartPanel />
        </div>
        <div className="col-span-3 row-span-2 bg-terminal-surface">
          <ChatPanel />
        </div>
        <div className="col-span-3 bg-terminal-surface">
          <PortfolioPanel />
        </div>
        <div className="col-span-3 bg-terminal-surface p-3">
          <span className="text-text-muted font-mono text-xs uppercase tracking-wider">
            Positions
          </span>
        </div>
      </TerminalGrid>
    </div>
  );
}
