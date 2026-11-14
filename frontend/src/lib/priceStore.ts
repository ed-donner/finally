import { useSyncExternalStore } from "react";
import type { ConnectionStatus, PriceMap, PricePoint, PriceUpdate } from "./types";

/** Max ticks kept per ticker (~5 min at 2 ticks/s). */
export const HISTORY_LIMIT = 600;

const EMPTY: PricePoint[] = [];

/**
 * In-memory store for the SSE price stream. Holds the latest price per ticker,
 * the tick history accumulated since page load (for sparklines and the main
 * chart), and the connection status. Components subscribe via the hooks below.
 */
export class PriceStore {
  prices: PriceMap = {};
  history: Record<string, PricePoint[]> = {};
  status: ConnectionStatus = "reconnecting";
  private listeners = new Set<() => void>();

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  private emit() {
    this.listeners.forEach((l) => l());
  }

  setStatus(status: ConnectionStatus) {
    if (status === this.status) return;
    this.status = status;
    this.emit();
  }

  /** Merge a full SSE ticker map. */
  ingest(map: PriceMap) {
    const nextPrices: PriceMap = {};
    const nextHistory = { ...this.history };
    for (const [ticker, update] of Object.entries(map)) {
      if (!update || typeof update.price !== "number") continue;
      const prev = this.prices[ticker];
      // Keep the previous object when nothing changed so selectors stay referentially stable.
      if (prev && prev.timestamp === update.timestamp && prev.price === update.price) {
        nextPrices[ticker] = prev;
        continue;
      }
      nextPrices[ticker] = update;
      const series = nextHistory[ticker] ?? EMPTY;
      const point = { time: update.timestamp, value: update.price };
      const appended = series.length >= HISTORY_LIMIT ? series.slice(1) : series.slice();
      appended.push(point);
      nextHistory[ticker] = appended;
    }
    this.prices = nextPrices;
    this.history = nextHistory;
    this.emit();
  }

  reset() {
    this.prices = {};
    this.history = {};
    this.status = "reconnecting";
    this.emit();
  }
}

export const priceStore = new PriceStore();

export function usePrices(store: PriceStore = priceStore): PriceMap {
  return useSyncExternalStore(store.subscribe, () => store.prices, () => store.prices);
}

export function usePrice(ticker: string, store: PriceStore = priceStore): PriceUpdate | undefined {
  const get = () => store.prices[ticker];
  return useSyncExternalStore(store.subscribe, get, get);
}

export function usePriceHistory(ticker: string, store: PriceStore = priceStore): PricePoint[] {
  const get = () => store.history[ticker] ?? EMPTY;
  return useSyncExternalStore(store.subscribe, get, get);
}

export function useConnectionStatus(store: PriceStore = priceStore): ConnectionStatus {
  const get = () => store.status;
  return useSyncExternalStore(store.subscribe, get, get);
}
