"use client";

import { useEffect, useState } from "react";
import { AppDataProvider, useAppData } from "./AppDataProvider";
import { ChatPanel } from "./ChatPanel";
import { Header } from "./Header";
import { Heatmap } from "./Heatmap";
import { MainChart } from "./MainChart";
import { PnlChart } from "./PnlChart";
import { PositionsTable } from "./PositionsTable";
import { TradeBar } from "./TradeBar";
import { Watchlist } from "./Watchlist";
import { usePriceStream } from "@/lib/usePriceStream";

export function Terminal() {
  return (
    <AppDataProvider>
      <TerminalLayout />
    </AppDataProvider>
  );
}

function TerminalLayout() {
  usePriceStream();
  const { loadError } = useAppData();
  const [chatCollapsed, setChatCollapsed] = useState(false);

  useEffect(() => {
    if (window.matchMedia?.("(max-width: 1100px)").matches) setChatCollapsed(true);
  }, []);

  return (
    <div className="flex h-dvh flex-col max-lg:h-auto max-lg:min-h-dvh">
      <Header />
      {loadError && (
        <div role="alert" className="border-b border-down/40 bg-down/10 px-4 py-1.5 text-xs text-down">
          {loadError}
        </div>
      )}
      <main
        className="terminal-grid min-h-0 flex-1"
        data-chat={chatCollapsed ? "collapsed" : "open"}
      >
        <div className="area-watch min-h-0">
          <Watchlist />
        </div>
        <div className="area-center flex min-h-0 flex-col gap-px">
          <div className="min-h-[260px] flex-[1.25]">
            <MainChart />
          </div>
          <TradeBar />
          <div className="grid min-h-[220px] flex-1 grid-cols-2 gap-px max-md:grid-cols-1">
            <Heatmap />
            <PnlChart />
          </div>
          <div className="min-h-[150px] flex-[0.8]">
            <PositionsTable />
          </div>
        </div>
        <div className="area-chat min-h-0">
          <ChatPanel collapsed={chatCollapsed} onToggle={() => setChatCollapsed((c) => !c)} />
        </div>
      </main>
    </div>
  );
}
