import type {
  ChatMessage,
  ChatResponse,
  Portfolio,
  Snapshot,
  TradeResponse,
  WatchlistItem,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function detailMessage(body: unknown, status: number): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((d) => (d && typeof d === "object" && "msg" in d ? String(d.msg) : String(d)))
        .join("; ");
    }
  }
  return `Request failed (${status})`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch {
    throw new ApiError("Can't reach the server. Check that the backend is running.", 0);
  }
  const body = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(detailMessage(body, res.status), res.status);
  return body as T;
}

export const api = {
  getPortfolio: () => request<Portfolio>("/api/portfolio"),
  getHistory: () =>
    request<{ snapshots: Snapshot[] }>("/api/portfolio/history").then((r) => r.snapshots ?? []),
  trade: (ticker: string, quantity: number, side: "buy" | "sell") =>
    request<TradeResponse>("/api/portfolio/trade", {
      method: "POST",
      body: JSON.stringify({ ticker, quantity, side }),
    }),
  getWatchlist: () =>
    request<{ tickers: WatchlistItem[] }>("/api/watchlist").then((r) => r.tickers ?? []),
  addTicker: (ticker: string) =>
    request<WatchlistItem>("/api/watchlist", { method: "POST", body: JSON.stringify({ ticker }) }),
  removeTicker: (ticker: string) =>
    request<{ ticker: string; removed: boolean }>(
      `/api/watchlist/${encodeURIComponent(ticker)}`,
      { method: "DELETE" },
    ),
  chat: (message: string) =>
    request<ChatResponse>("/api/chat", { method: "POST", body: JSON.stringify({ message }) }),
  getChatHistory: () =>
    request<{ messages: ChatMessage[] }>("/api/chat/history").then((r) => r.messages ?? []),
};
