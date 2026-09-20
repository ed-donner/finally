"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useChat } from "@/hooks/useChat";
import { usePriceStream } from "@/hooks/usePriceStream";
import { useResource } from "@/hooks/useResource";
import { useToasts } from "@/hooks/useToasts";
import { api, isMock } from "@/utils/api";
import { fmtQty, fmtUsd } from "@/utils/format";
import { revalue } from "@/utils/portfolio";
import type { ChatResponse, TradeOrder } from "@/utils/types";
import { ChatPanel } from "./ChatPanel";
import { Header } from "./Header";
import { InsightsBar } from "./InsightsBar";
import { MainChart } from "./MainChart";
import { PnLChart } from "./PnLChart";
import { PortfolioHeatmap } from "./PortfolioHeatmap";
import { PositionsTable } from "./PositionsTable";
import { Toasts } from "./Toasts";
import { TradeForm } from "./TradeForm";
import { Watchlist } from "./Watchlist";

/** Slow background refresh; live prices come from the SSE stream, so these are just re-syncs. */
const POLL_MS = 30_000;

/**
 * The page layout: owns the data hooks and wires every panel together.
 *
 *   Header
 *   ┌───────────── main ─────────────┬── chat ──┐
 *   │ Watchlist        MainChart     │          │
 *   │ Heatmap          PnLChart      │ AI panel │
 *   │ Positions        TradeForm     │          │
 *   └────────────────────────────────┴──────────┘
 */
export function Dashboard() {
  const stream = usePriceStream();
  const portfolioRes = useResource(api.portfolio, POLL_MS);
  const historyRes = useResource(api.history, POLL_MS);
  const watchlistRes = useResource(api.watchlist, POLL_MS * 2);
  const { toasts, push, dismiss } = useToasts();

  const [selected, setSelected] = useState<string | null>(null);
  const [chatOpen, setChatOpen] = useState(true);
  const [insightsKey, setInsightsKey] = useState(0);
  // isMock() reads the URL, so resolve it after mount to keep server and client markup identical.
  const [mock, setMock] = useState(false);
  useEffect(() => setMock(isMock()), []);

  const { refresh: refreshPortfolio } = portfolioRes;
  const { refresh: refreshHistory } = historyRes;
  const { refresh: refreshWatchlist } = watchlistRes;

  // Value the last fetched portfolio at streamed prices so totals tick live.
  const portfolio = useMemo(
    () => (portfolioRes.data ? revalue(portfolioRes.data, stream.prices) : null),
    [portfolioRes.data, stream.prices],
  );

  // Select the first watched ticker once the watchlist loads (and if the selection is removed).
  const watched = watchlistRes.data;
  useEffect(() => {
    if (watched?.length && (!selected || !watched.some((w) => w.ticker === selected))) {
      const held = portfolioRes.data?.positions.some((p) => p.ticker === selected);
      if (!held) setSelected(watched[0].ticker);
    }
  }, [watched, selected, portfolioRes.data]);

  const refreshAll = useCallback(async () => {
    await Promise.all([refreshPortfolio(), refreshHistory(), refreshWatchlist()]);
    setInsightsKey((k) => k + 1);
  }, [refreshPortfolio, refreshHistory, refreshWatchlist]);

  // ------------------------------------------------------------------ actions

  const handleTrade = useCallback(
    async (order: TradeOrder): Promise<boolean> => {
      try {
        const { trade } = await api.trade(order);
        push(
          "success",
          `${trade.side === "buy" ? "Bought" : "Sold"} ${fmtQty(trade.quantity)} ${trade.ticker} @ ${fmtUsd(trade.price)}`,
        );
        trade.warnings.forEach((w) => push("info", w));
        await refreshAll();
        return true;
      } catch (e) {
        push("error", e instanceof Error ? e.message : "Trade failed");
        return false;
      }
    },
    [push, refreshAll],
  );

  const handleAdd = useCallback(
    async (ticker: string) => {
      try {
        await api.addTicker(ticker);
        await refreshWatchlist();
      } catch (e) {
        push("error", e instanceof Error ? e.message : "Could not add ticker");
        throw e; // lets the form keep the input so the user can correct it
      }
    },
    [push, refreshWatchlist],
  );

  const handleRemove = useCallback(
    async (ticker: string) => {
      try {
        await api.removeTicker(ticker);
        await refreshWatchlist();
      } catch (e) {
        push("error", e instanceof Error ? e.message : "Could not remove ticker");
      }
    },
    [push, refreshWatchlist],
  );

  const handleChatResponse = useCallback(
    (r: ChatResponse) => {
      if (r.executed_trades.some((t) => t.ok) || r.watchlist_changes.some((c) => c.ok)) void refreshAll();
    },
    [refreshAll],
  );
  const chat = useChat(handleChatResponse);

  const backendDown = portfolioRes.error && !portfolioRes.data;

  return (
    <div className="flex min-h-screen flex-col lg:h-screen">
      <Header portfolio={portfolio} status={stream.status} mock={mock} chatOpen={chatOpen} onToggleChat={() => setChatOpen((o) => !o)} />

      <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        <main className="min-w-0 flex-1 space-y-3 p-3 lg:overflow-y-auto">
          {backendDown && (
            <div className="flex flex-wrap items-center gap-3 rounded-md border border-down/50 bg-down/10 px-3 py-2 text-down" role="alert">
              <span>{portfolioRes.error}</span>
              <button onClick={() => void refreshAll()} className="rounded border border-down/60 px-2 py-0.5 text-xs hover:bg-down/20">
                Retry
              </button>
              <span className="text-xs text-muted">
                Tip: add <code className="text-accent">?mock=1</code> to the URL to preview with mock data.
              </span>
            </div>
          )}

          <div className="grid gap-3 lg:grid-cols-12">
            <div className="h-[420px] lg:col-span-4">
              <Watchlist
                entries={watchlistRes.data}
                loading={watchlistRes.loading}
                error={watchlistRes.error}
                prices={stream.prices}
                history={stream.history}
                open={stream.open}
                selected={selected}
                onSelect={setSelected}
                onAdd={handleAdd}
                onRemove={handleRemove}
              />
            </div>
            <div className="h-[420px] lg:col-span-8">
              <MainChart
                ticker={selected}
                points={(selected && stream.history[selected]) || []}
                openPrice={selected ? stream.open[selected] : undefined}
              />
            </div>

            <div className="h-[300px] lg:col-span-5">
              <PortfolioHeatmap portfolio={portfolio} loading={portfolioRes.loading} />
            </div>
            <div className="h-[300px] lg:col-span-7">
              <PnLChart
                snapshots={historyRes.data}
                loading={historyRes.loading}
                error={historyRes.error}
                startingCash={portfolio?.starting_cash}
              />
            </div>

            <div className="h-[280px] lg:col-span-8">
              <PositionsTable
                portfolio={portfolio}
                loading={portfolioRes.loading}
                error={portfolioRes.error}
                selected={selected}
                onSelect={setSelected}
              />
            </div>
            <div className="lg:col-span-4">
              <TradeForm selectedTicker={selected} priceOf={(t) => stream.prices[t]?.price} onTrade={handleTrade} />
            </div>
          </div>

          <InsightsBar refreshKey={insightsKey} />
        </main>

        {chatOpen && (
          <ChatPanel
            messages={chat.messages}
            loading={chat.loading}
            onSend={chat.send}
            className="h-[480px] shrink-0 border-t lg:h-auto lg:w-[360px] lg:border-t-0"
          />
        )}
      </div>

      <Toasts toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}
