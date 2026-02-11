import { create } from "zustand";

export interface Position {
  ticker: string;
  quantity: number;
  avg_cost: number;
  current_price: number;
  market_value: number;
  unrealized_pnl: number;
  unrealized_pnl_percent: number;
}

export interface Snapshot {
  total_value: number;
  recorded_at: string;
}

interface PortfolioStore {
  cashBalance: number;
  totalValue: number;
  loading: boolean;
  positions: Position[];
  snapshots: Snapshot[];
  tradeLoading: boolean;
  tradeError: string | null;
  fetchPortfolio: () => Promise<void>;
  fetchHistory: () => Promise<void>;
  executeTrade: (
    ticker: string,
    side: "buy" | "sell",
    quantity: number,
  ) => Promise<void>;
}

export const usePortfolioStore = create<PortfolioStore>()((set, get) => ({
  cashBalance: 0,
  totalValue: 0,
  loading: true,
  positions: [],
  snapshots: [],
  tradeLoading: false,
  tradeError: null,

  fetchPortfolio: async () => {
    try {
      const res = await fetch("/api/portfolio");
      if (res.ok) {
        const data = await res.json();
        set({
          cashBalance: data.cash_balance,
          totalValue: data.total_value,
          positions: data.positions,
          loading: false,
        });
      }
    } catch {
      // Portfolio fetch failed -- will retry on next interval or user action
    }
  },

  fetchHistory: async () => {
    try {
      const res = await fetch("/api/portfolio/history");
      if (res.ok) {
        const data = await res.json();
        set({ snapshots: data.snapshots });
      }
    } catch {
      // History fetch failed silently
    }
  },

  executeTrade: async (ticker, side, quantity) => {
    set({ tradeError: null, tradeLoading: true });
    try {
      const res = await fetch("/api/portfolio/trade", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ticker, side, quantity }),
      });
      if (res.ok) {
        set({ tradeLoading: false });
        get().fetchPortfolio();
        get().fetchHistory();
      } else {
        const err = await res.json();
        set({ tradeError: err.detail, tradeLoading: false });
      }
    } catch {
      set({ tradeError: "Trade failed", tradeLoading: false });
    }
  },
}));
