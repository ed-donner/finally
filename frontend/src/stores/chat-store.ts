import { create } from "zustand";
import { usePortfolioStore } from "@/stores/portfolio-store";
import { useWatchlistStore } from "@/stores/watchlist-store";

export interface TradeResult {
  status: string;
  ticker: string;
  side: string;
  quantity: number | null;
  price: number | null;
  total: number | null;
  error: string | null;
}

export interface WatchlistResult {
  status: string;
  ticker: string;
  action: string;
  error: string | null;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  trades?: TradeResult[];
  watchlist_changes?: WatchlistResult[];
}

interface ChatStore {
  messages: ChatMessage[];
  sending: boolean;
  error: string | null;
  sendMessage: (message: string) => Promise<void>;
}

export const useChatStore = create<ChatStore>()((set, get) => ({
  messages: [],
  sending: false,
  error: null,

  sendMessage: async (message: string) => {
    set({
      messages: [...get().messages, { role: "user", content: message }],
      sending: true,
      error: null,
    });

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
      });

      if (res.ok) {
        const data = await res.json();
        set({
          messages: [
            ...get().messages,
            {
              role: "assistant",
              content: data.message,
              trades: data.trades,
              watchlist_changes: data.watchlist_changes,
            },
          ],
          sending: false,
        });

        if (data.trades?.some((t: TradeResult) => t.status === "executed")) {
          usePortfolioStore.getState().fetchPortfolio();
          usePortfolioStore.getState().fetchHistory();
        }
        if (
          data.watchlist_changes?.some(
            (w: WatchlistResult) => w.status === "applied",
          )
        ) {
          useWatchlistStore.getState().fetchWatchlist();
        }
      } else {
        set({
          messages: [
            ...get().messages,
            {
              role: "assistant",
              content: "Failed to get a response. Please try again.",
            },
          ],
          sending: false,
        });
      }
    } catch {
      set({
        messages: [
          ...get().messages,
          {
            role: "assistant",
            content: "Failed to get a response. Please try again.",
          },
        ],
        sending: false,
      });
    }
  },
}));
