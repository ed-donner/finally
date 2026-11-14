import { vi } from "vitest";
import type { ChatMessage, Portfolio, PriceUpdate, WatchlistItem } from "@/lib/types";

export const portfolioFixture: Portfolio = {
  cash_balance: 8100,
  positions_value: 1950,
  total_value: 10050,
  unrealized_pnl: 50,
  unrealized_pnl_percent: 2.63,
  positions: [
    {
      ticker: "AAPL",
      quantity: 10,
      avg_cost: 190,
      current_price: 195,
      market_value: 1950,
      unrealized_pnl: 50,
      unrealized_pnl_percent: 2.63,
      weight: 1,
    },
  ],
};

export const watchlistFixture: WatchlistItem[] = [
  { ticker: "AAPL", price: 195, previous_price: 194, change_percent: 0.5, day_change_percent: 1.2, direction: "up", timestamp: 1 },
  { ticker: "MSFT", price: 410, previous_price: 411, change_percent: -0.2, day_change_percent: -0.8, direction: "down", timestamp: 1 },
];

export function priceUpdate(ticker: string, price: number, timestamp: number, previous = price): PriceUpdate {
  return {
    ticker,
    price,
    previous_price: previous,
    timestamp,
    change: price - previous,
    change_percent: previous ? ((price - previous) / previous) * 100 : 0,
    direction: price > previous ? "up" : price < previous ? "down" : "flat",
    reference_price: previous,
    day_change: price - previous,
    day_change_percent: previous ? ((price - previous) / previous) * 100 : 0,
  };
}

type Handler = (init: RequestInit | undefined, url: string) => { status?: number; body: unknown };

/** Install a fetch mock keyed by "METHOD /path" (exact) or "METHOD /prefix*". */
export function mockFetch(routes: Record<string, Handler>) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const method = (init?.method ?? "GET").toUpperCase();
    const key = `${method} ${url}`;
    const handler =
      routes[key] ??
      Object.entries(routes).find(([k]) => k.endsWith("*") && key.startsWith(k.slice(0, -1)))?.[1];
    if (!handler) return new Response(JSON.stringify({ detail: `no mock for ${key}` }), { status: 404 });
    const { status = 200, body } = handler(init, url);
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

export function defaultRoutes(overrides: Record<string, Handler> = {}): Record<string, Handler> {
  return {
    "GET /api/portfolio": () => ({ body: portfolioFixture }),
    "GET /api/portfolio/history": () => ({ body: { snapshots: [] } }),
    "GET /api/watchlist": () => ({ body: { tickers: watchlistFixture } }),
    "GET /api/chat/history": () => ({ body: { messages: [] as ChatMessage[] } }),
    ...overrides,
  };
}

/** Minimal EventSource stand-in that tests drive by hand. */
export class MockEventSource {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;
  static instances: MockEventSource[] = [];
  readyState = MockEventSource.CONNECTING;
  onopen: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  closed = false;
  constructor(public url: string) {
    MockEventSource.instances.push(this);
  }
  open() {
    this.readyState = MockEventSource.OPEN;
    this.onopen?.(new Event("open"));
  }
  send(data: unknown) {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(data) }));
  }
  fail(closed: boolean) {
    this.readyState = closed ? MockEventSource.CLOSED : MockEventSource.CONNECTING;
    this.onerror?.(new Event("error"));
  }
  close() {
    this.closed = true;
    this.readyState = MockEventSource.CLOSED;
  }
  static latest() {
    return MockEventSource.instances[MockEventSource.instances.length - 1];
  }
}
