import { create } from "zustand";

interface PortfolioStore {
  cashBalance: number;
  totalValue: number;
  loading: boolean;
  fetchPortfolio: () => Promise<void>;
}

export const usePortfolioStore = create<PortfolioStore>()((set) => ({
  cashBalance: 0,
  totalValue: 0,
  loading: true,
  fetchPortfolio: async () => {
    try {
      const res = await fetch("/api/portfolio");
      if (res.ok) {
        const data = await res.json();
        set({
          cashBalance: data.cash_balance,
          totalValue: data.total_value,
          loading: false,
        });
      }
    } catch {
      // Portfolio fetch failed -- will retry on next interval or user action
    }
  },
}));
