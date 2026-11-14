"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { api } from "@/lib/api";
import type { Portfolio, Snapshot, WatchlistItem } from "@/lib/types";

export const HISTORY_REFRESH_MS = 30_000;
export const PORTFOLIO_REFRESH_MS = 15_000;

interface AppData {
  portfolio: Portfolio | null;
  snapshots: Snapshot[];
  watchlist: WatchlistItem[];
  selected: string | null;
  loadError: string | null;
  select: (ticker: string) => void;
  setPortfolio: (p: Portfolio) => void;
  refreshPortfolio: () => Promise<void>;
  refreshHistory: () => Promise<void>;
  refreshWatchlist: () => Promise<void>;
  refreshAll: () => Promise<void>;
  addTicker: (ticker: string) => Promise<void>;
  removeTicker: (ticker: string) => Promise<void>;
}

const AppDataContext = createContext<AppData | null>(null);

export function useAppData(): AppData {
  const ctx = useContext(AppDataContext);
  if (!ctx) throw new Error("useAppData must be used inside <AppDataProvider>");
  return ctx;
}

export function AppDataProvider({ children }: { children: ReactNode }) {
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [watchlist, setWatchlist] = useState<WatchlistItem[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const refreshPortfolio = useCallback(async () => {
    try {
      setPortfolio(await api.getPortfolio());
      setLoadError(null);
    } catch (e) {
      setLoadError((e as Error).message);
    }
  }, []);

  const refreshHistory = useCallback(async () => {
    try {
      setSnapshots(await api.getHistory());
    } catch {
      // chart keeps its last data
    }
  }, []);

  const refreshWatchlist = useCallback(async () => {
    try {
      const items = await api.getWatchlist();
      setWatchlist(items);
      setSelected((cur) => cur ?? items[0]?.ticker ?? null);
    } catch (e) {
      setLoadError((e as Error).message);
    }
  }, []);

  const refreshAll = useCallback(async () => {
    await Promise.all([refreshPortfolio(), refreshHistory(), refreshWatchlist()]);
  }, [refreshPortfolio, refreshHistory, refreshWatchlist]);

  const addTicker = useCallback(
    async (ticker: string) => {
      const item = await api.addTicker(ticker);
      setWatchlist((cur) =>
        cur.some((w) => w.ticker === item.ticker) ? cur : [...cur, item],
      );
      void refreshWatchlist();
    },
    [refreshWatchlist],
  );

  const removeTicker = useCallback(
    async (ticker: string) => {
      await api.removeTicker(ticker);
      setWatchlist((cur) => cur.filter((w) => w.ticker !== ticker));
      void refreshWatchlist();
    },
    [refreshWatchlist],
  );

  useEffect(() => {
    void refreshAll();
    const h = setInterval(() => void refreshHistory(), HISTORY_REFRESH_MS);
    const p = setInterval(() => void refreshPortfolio(), PORTFOLIO_REFRESH_MS);
    return () => {
      clearInterval(h);
      clearInterval(p);
    };
  }, [refreshAll, refreshHistory, refreshPortfolio]);

  const value = useMemo<AppData>(
    () => ({
      portfolio,
      snapshots,
      watchlist,
      selected,
      loadError,
      select: setSelected,
      setPortfolio,
      refreshPortfolio,
      refreshHistory,
      refreshWatchlist,
      refreshAll,
      addTicker,
      removeTicker,
    }),
    [
      portfolio,
      snapshots,
      watchlist,
      selected,
      loadError,
      refreshPortfolio,
      refreshHistory,
      refreshWatchlist,
      refreshAll,
      addTicker,
      removeTicker,
    ],
  );

  return <AppDataContext.Provider value={value}>{children}</AppDataContext.Provider>;
}
