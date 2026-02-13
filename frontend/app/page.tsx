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
    <main className="min-h-screen overflow-x-clip bg-transparent">
      <Header totalValue={portfolio.total_value} cash={portfolio.cash_balance} connectionState={streamState} />

      <div className="grid w-full grid-cols-1 gap-3 p-3 xl:h-[calc(100vh-98px)] xl:grid-cols-[minmax(0,1fr)_minmax(0,0.95fr)_minmax(240px,280px)]">
        <div className="min-h-0 min-w-0 xl:h-full">
          <WatchlistPanel
            watchlist={watchlist}
            tickerHistory={tickerHistory}
            selectedTicker={selectedTicker}
            onSelectTicker={setSelectedTicker}
            onRemoveTicker={(ticker) => {
              void removeTicker(ticker);
            }}
          />
        </div>

        <div className="grid min-h-0 min-w-0 gap-3 xl:grid-rows-[minmax(0,1.25fr)_minmax(0,1fr)_minmax(0,0.95fr)]">
          <MainChart ticker={selectedTicker} series={selectedSeries} />
          <div className="grid min-h-0 gap-3 lg:grid-cols-2">
            <Heatmap positions={portfolio.positions} />
            <PnlChart data={history} />
          </div>
          <div className="grid min-h-0 gap-3">
            <PositionsTable positions={portfolio.positions} />
          </div>
        </div>

        <div className="grid min-h-0 min-w-0 gap-3 xl:grid-rows-[minmax(0,1fr)_auto]">
          <ChatPanel
            messages={chatMessages}
            loading={isChatLoading}
            onSubmit={submitChat}
          />
          <TradeBar
            defaultTicker={selectedTicker}
            onTrade={trade}
            onAddTicker={addTicker}
          />
        </div>
      </div>
    </main>
  );
}
