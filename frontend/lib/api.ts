import type {
  Portfolio,
  TradeRequest,
  TradeResponse,
  WatchlistItem,
  PortfolioSnapshot,
  ChatResponse,
  ChatMessage,
} from "./types";

const BASE = "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status}: ${body}`);
  }
  return res.json();
}

export async function getPortfolio(): Promise<Portfolio> {
  const data = await request<{
    positions: Portfolio["positions"];
    cash: number;
    total_value: number;
  }>("/portfolio");
  return {
    positions: data.positions,
    cash_balance: data.cash,
    total_value: data.total_value,
  };
}

export async function executeTrade(trade: TradeRequest): Promise<TradeResponse> {
  const data = await request<{ trade: TradeResponse }>("/portfolio/trade", {
    method: "POST",
    body: JSON.stringify(trade),
  });
  return data.trade;
}

export async function getPortfolioHistory(): Promise<PortfolioSnapshot[]> {
  const data = await request<{ snapshots: PortfolioSnapshot[] }>("/portfolio/history");
  return data.snapshots;
}

export async function getWatchlist(): Promise<WatchlistItem[]> {
  const data = await request<{ watchlist: WatchlistItem[] }>("/watchlist");
  return data.watchlist;
}

export async function addToWatchlist(ticker: string): Promise<void> {
  await request("/watchlist", {
    method: "POST",
    body: JSON.stringify({ ticker }),
  });
}

export async function removeFromWatchlist(ticker: string): Promise<void> {
  await request(`/watchlist/${ticker}`, { method: "DELETE" });
}

export async function sendChatMessage(message: string): Promise<ChatResponse> {
  const data = await request<{
    message: string;
    trades: Array<{ ticker: string; side: string; quantity: number; price: number; status?: string; error?: string }>;
    watchlist_changes: Array<{ ticker: string; action: string; status?: string; error?: string }>;
  }>("/chat", {
    method: "POST",
    body: JSON.stringify({ message }),
  });

  const executedTrades = data.trades
    .filter((t) => t.status === "executed")
    .map((t) => ({
      id: crypto.randomUUID(),
      ticker: t.ticker,
      side: t.side as "buy" | "sell",
      quantity: t.quantity,
      price: t.price,
      executed_at: new Date().toISOString(),
    }));

  const errors = data.trades
    .filter((t) => t.error)
    .map((t) => t.error!);

  const assistantMessage: ChatMessage = {
    id: crypto.randomUUID(),
    role: "assistant",
    content: data.message,
    actions: {
      trades: executedTrades.length > 0 ? executedTrades : undefined,
      watchlist_changes: data.watchlist_changes.length > 0 ? data.watchlist_changes.map((w) => ({
        ticker: w.ticker,
        action: w.action as "add" | "remove",
      })) : undefined,
      errors: errors.length > 0 ? errors : undefined,
    },
    created_at: new Date().toISOString(),
  };

  return { message: assistantMessage };
}

export async function getHealth(): Promise<{ status: string }> {
  return request<{ status: string }>("/health");
}
