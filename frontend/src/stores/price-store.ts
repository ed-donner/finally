import { create } from "zustand";

export type ConnectionStatus = "connected" | "connecting" | "disconnected";

export interface PriceUpdate {
  ticker: string;
  price: number;
  previous_price: number;
  timestamp: number;
  change: number;
  change_percent: number;
  direction: "up" | "down" | "flat";
}

export interface PriceHistoryPoint {
  time: number;
  value: number;
}

interface PriceStore {
  prices: Record<string, PriceUpdate>;
  priceHistory: Record<string, PriceHistoryPoint[]>;
  connectionStatus: ConnectionStatus;
  setPrices: (prices: Record<string, PriceUpdate>) => void;
  setConnectionStatus: (status: ConnectionStatus) => void;
}

export const usePriceStore = create<PriceStore>()((set) => ({
  prices: {},
  priceHistory: {},
  connectionStatus: "disconnected",
  setPrices: (incoming) =>
    set((state) => {
      const newHistory = { ...state.priceHistory };
      for (const [ticker, update] of Object.entries(incoming)) {
        const existing = newHistory[ticker] || [];
        const appended = [...existing, { time: update.timestamp, value: update.price }];
        newHistory[ticker] = appended.length > 5000 ? appended.slice(-5000) : appended;
      }
      return { prices: incoming, priceHistory: newHistory };
    }),
  setConnectionStatus: (status) => set({ connectionStatus: status }),
}));
