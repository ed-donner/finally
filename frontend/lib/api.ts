import type { Portfolio, PortfolioSnapshot, WatchlistEntry, ChatMessage } from "./types";

const BASE = "/api";

export async function fetchWatchlist(): Promise<WatchlistEntry[]> {
  const res = await fetch(`${BASE}/watchlist`);
  if (!res.ok) throw new Error("Failed to fetch watchlist");
  return res.json();
}

export async function addWatchlistTicker(ticker: string): Promise<void> {
  const res = await fetch(`${BASE}/watchlist`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ticker }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || "Failed to add ticker");
  }
}

export async function removeWatchlistTicker(ticker: string): Promise<void> {
  const res = await fetch(`${BASE}/watchlist/${ticker}`, { method: "DELETE" });
  if (!res.ok) throw new Error("Failed to remove ticker");
}

export async function fetchPortfolio(): Promise<Portfolio> {
  const res = await fetch(`${BASE}/portfolio`);
  if (!res.ok) throw new Error("Failed to fetch portfolio");
  return res.json();
}

export async function fetchPortfolioHistory(): Promise<PortfolioSnapshot[]> {
  const res = await fetch(`${BASE}/portfolio/history`);
  if (!res.ok) throw new Error("Failed to fetch portfolio history");
  return res.json();
}

export async function executeTrade(
  ticker: string,
  side: "buy" | "sell",
  quantity: number
): Promise<{ trade_id: string; price: number; portfolio: Portfolio }> {
  const res = await fetch(`${BASE}/portfolio/trade`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ticker, side, quantity }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || "Trade failed");
  }
  return res.json();
}

export async function fetchChatHistory(): Promise<ChatMessage[]> {
  const res = await fetch(`${BASE}/chat`);
  if (!res.ok) throw new Error("Failed to fetch chat history");
  return res.json();
}

export async function sendChatMessage(message: string): Promise<ChatMessage> {
  const res = await fetch(`${BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || "Chat failed");
  }
  return res.json();
}
