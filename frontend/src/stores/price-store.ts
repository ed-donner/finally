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

interface PriceStore {
  prices: Record<string, PriceUpdate>;
  connectionStatus: ConnectionStatus;
  setPrices: (prices: Record<string, PriceUpdate>) => void;
  setConnectionStatus: (status: ConnectionStatus) => void;
}

export const usePriceStore = create<PriceStore>()((set) => ({
  prices: {},
  connectionStatus: "disconnected",
  setPrices: (prices) => set({ prices }),
  setConnectionStatus: (status) => set({ connectionStatus: status }),
}));
