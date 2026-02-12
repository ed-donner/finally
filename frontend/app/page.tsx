'use client';

import { useEffect } from 'react';

import { ChatPanel } from '@/src/components/ChatPanel';
import { Header } from '@/src/components/Header';
import { Heatmap } from '@/src/components/Heatmap';
import { MainChart } from '@/src/components/MainChart';
import { PnlChart } from '@/src/components/PnlChart';
import { PositionsTable } from '@/src/components/PositionsTable';
import { TradeBar } from '@/src/components/TradeBar';
import { WatchlistPanel } from '@/src/components/WatchlistPanel';
import { useMarketStream } from '@/src/hooks/useMarketStream';
import { useTradingData } from '@/src/hooks/useTradingData';

export default function HomePage() {
  const {
    watchlist,
    selectedTicker,
    setSelectedTicker,
    portfolio,
    history,
    tickerHistory,
    selectedSeries,
    setConnectionState,
    onPriceBatch,
    trade,
    addTicker,
    removeTicker,
    chatMessages,
    isChatLoading,
    submitChat,
  } = useTradingData();

  const streamState = useMarketStream({ onPriceBatch });

  useEffect(() => {
    setConnectionState(streamState);
  }, [setConnectionState, streamState]);

  return (
    <main className="min-h-screen bg-transparent">
      <Header totalValue={portfolio.total_value} cash={portfolio.cash_balance} connectionState={streamState} />

      <div className="mx-auto grid max-w-[1680px] grid-cols-1 gap-3 p-3 xl:grid-cols-[320px_1fr_360px]">
        <div className="xl:min-h-[calc(100vh-98px)]">
          <WatchlistPanel
            watchlist={watchlist}
            sparklineByTicker={tickerHistory}
            selectedTicker={selectedTicker}
            onSelectTicker={setSelectedTicker}
            onRemoveTicker={(ticker) => {
              void removeTicker(ticker);
            }}
          />
        </div>

        <div className="grid gap-3 xl:grid-rows-[2fr_1fr_1fr]">
          <MainChart ticker={selectedTicker} series={selectedSeries} />
          <div className="grid gap-3 lg:grid-cols-2">
            <Heatmap positions={portfolio.positions} />
            <PnlChart data={history} />
          </div>
          <div className="grid gap-3 lg:grid-cols-2">
            <PositionsTable positions={portfolio.positions} />
            <TradeBar
              defaultTicker={selectedTicker}
              onTrade={trade}
              onAddTicker={addTicker}
            />
          </div>
        </div>

        <div className="xl:min-h-[calc(100vh-98px)]">
          <ChatPanel
            messages={chatMessages}
            loading={isChatLoading}
            onSubmit={submitChat}
          />
        </div>
      </div>
    </main>
  );
}
