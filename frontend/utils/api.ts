import { ApiError } from "./errors";
import { mockApi } from "./mock";
import type {
  AnalysisReport,
  ChatResponse,
  Portfolio,
  RiskAssessment,
  Snapshot,
  TradeOrder,
  TradeResponse,
  WatchlistEntry,
  WatchlistResponse,
} from "./types";

/** Empty = same origin (production static export, or the `next dev` proxy). */
export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

let mockFlag: boolean | null = null;

/**
 * Mock mode serves built-in data so the UI runs without a backend.
 * Enable with NEXT_PUBLIC_USE_MOCK=true, or open the app with ?mock=1. Client-side only.
 */
export function isMock(): boolean {
  if (mockFlag === null) {
    const fromUrl =
      typeof window !== "undefined" && new URLSearchParams(window.location.search).get("mock") === "1";
    mockFlag = process.env.NEXT_PUBLIC_USE_MOCK === "true" || fromUrl;
  }
  return mockFlag;
}

export { ApiError };

/** Pull a readable message out of the backend's error shapes ({error}, {detail: str | [{msg}]}). */
function errorMessage(body: unknown, fallback: string): string {
  if (body && typeof body === "object") {
    const b = body as { error?: string; detail?: unknown };
    if (typeof b.error === "string") return b.error;
    if (typeof b.detail === "string") return b.detail;
    if (Array.isArray(b.detail)) {
      return b.detail.map((d: { msg?: string }) => d?.msg ?? "invalid input").join("; ");
    }
  }
  return fallback;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...init?.headers },
    });
  } catch {
    throw new ApiError("Cannot reach the backend. Is it running on port 8000?");
  }
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    throw new ApiError(
      errorMessage(body, `${res.status} ${res.statusText}`),
      res.status,
      (body as { reasons?: string[] } | null)?.reasons ?? [],
    );
  }
  return body as T;
}

const post = <T>(path: string, data: unknown) =>
  request<T>(path, { method: "POST", body: JSON.stringify(data) });

/** Every backend route the UI uses; each dispatches to the mock when mock mode is on. */
export const api = {
  portfolio: (): Promise<Portfolio> =>
    isMock() ? mockApi.portfolio() : request("/api/portfolio"),

  history: (): Promise<Snapshot[]> =>
    isMock() ? mockApi.history() : request("/api/portfolio/history"),

  trade: (order: TradeOrder): Promise<TradeResponse> =>
    isMock() ? mockApi.trade(order) : post("/api/trade", order),

  watchlist: (): Promise<WatchlistEntry[]> =>
    isMock() ? mockApi.watchlist() : request("/api/watchlist"),

  addTicker: (ticker: string): Promise<WatchlistResponse> =>
    isMock() ? mockApi.addTicker(ticker) : post("/api/watchlist", { ticker, action: "add" }),

  removeTicker: (ticker: string): Promise<WatchlistResponse> =>
    isMock()
      ? mockApi.removeTicker(ticker)
      : request(`/api/watchlist/${encodeURIComponent(ticker)}`, { method: "DELETE" }),

  chat: (message: string): Promise<ChatResponse> =>
    isMock() ? mockApi.chat(message) : post("/api/chat", { message }),

  analysis: (): Promise<AnalysisReport> =>
    isMock() ? mockApi.analysis() : request("/api/analysis"),

  riskAssessment: (): Promise<RiskAssessment> =>
    isMock() ? mockApi.riskAssessment() : request("/api/risk-assessment"),
};
