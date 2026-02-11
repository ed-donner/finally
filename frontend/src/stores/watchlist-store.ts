import { create } from "zustand";

interface WatchlistStore {
  tickers: string[];
  selectedTicker: string | null;
  loading: boolean;
  fetchWatchlist: () => Promise<void>;
  addTicker: (ticker: string) => Promise<void>;
  removeTicker: (ticker: string) => Promise<void>;
  selectTicker: (ticker: string) => void;
}

export const useWatchlistStore = create<WatchlistStore>()((set, get) => ({
  tickers: [],
  selectedTicker: null,
  loading: true,

  fetchWatchlist: async () => {
    try {
      const res = await fetch("/api/watchlist");
      if (res.ok) {
        const data = await res.json();
        const tickers: string[] = data.items.map(
          (i: { ticker: string }) => i.ticker,
        );
        set((state) => ({
          tickers,
          loading: false,
          selectedTicker: state.selectedTicker ?? tickers[0] ?? null,
        }));
      }
    } catch {
      // Watchlist fetch failed -- will retry on next user action
    }
  },

  addTicker: async (ticker: string) => {
    const normalized = ticker.toUpperCase().trim();
    if (!normalized || get().tickers.includes(normalized)) return;
    try {
      const res = await fetch("/api/watchlist", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ticker: normalized }),
      });
      if (res.ok) {
        await get().fetchWatchlist();
      }
    } catch {
      // Add ticker failed
    }
  },

  removeTicker: async (ticker: string) => {
    try {
      const res = await fetch(`/api/watchlist/${ticker}`, {
        method: "DELETE",
      });
      if (res.ok) {
        const { selectedTicker } = get();
        if (selectedTicker === ticker) {
          const remaining = get().tickers.filter((t) => t !== ticker);
          set({ selectedTicker: remaining[0] ?? null });
        }
        await get().fetchWatchlist();
      }
    } catch {
      // Remove ticker failed
    }
  },

  selectTicker: (ticker: string) => set({ selectedTicker: ticker }),
}));
