import { expect, type APIRequestContext, type Page } from "@playwright/test";
import net from "node:net";
import type { TradeFormPage } from "./pages/TradeFormPage";

// --------------------------------------------------------------------------- parsing

/** "$1,234.56" -> 1234.56, "-$5.00" -> -5, "+$12.34 (+0.12%)" -> 12.34 (first number only). */
export function parseUsd(text: string): number {
  const match = text.replace(/,/g, "").match(/-?\$?\s*-?\d+(?:\.\d+)?/);
  if (!match) throw new Error(`No dollar amount in "${text}"`);
  return Number(match[0].replace(/[$\s]/g, ""));
}

// --------------------------------------------------------------------------- high-level actions

export type Side = "buy" | "sell";

/** A trade that went through, as reported by the UI ("Bought 10 AAPL @ $190.02"). */
export interface Fill {
  ticker: string;
  quantity: number;
  price: number;
}

const FILL_RE = /(?:Bought|Sold)\s+([\d.]+)\s+([A-Z.]+)\s+@\s+(\$[\d,.]+)/;

function parseFill(text: string): Fill {
  const m = text.match(FILL_RE);
  if (!m) throw new Error(`Could not read a fill from "${text}"`);
  return { quantity: Number(m[1]), ticker: m[2], price: parseUsd(m[3]) };
}

/** Place a manual market order through the trade form and return the reported fill. */
export async function placeTrade(
  page: Page,
  form: TradeFormPage,
  side: Side,
  ticker: string,
  quantity: number,
): Promise<Fill> {
  await form.submit(side, ticker, quantity);
  const toast = page.getByTestId("toast").filter({ hasText: side === "buy" ? "Bought" : "Sold" }).last();
  await expect(toast).toBeVisible();
  return parseFill(await toast.innerText());
}

export { parseFill };

// --------------------------------------------------------------------------- backend access

export interface ApiPortfolio {
  cash_balance: number;
  total_value: number;
  positions: { ticker: string; quantity: number; avg_cost: number }[];
}

/** Read the backend directly (source of truth) to cross-check what the UI shows. */
export async function getPortfolio(request: APIRequestContext): Promise<ApiPortfolio> {
  const res = await request.get("/api/portfolio");
  expect(res.ok()).toBeTruthy();
  return res.json();
}

export async function getHistory(request: APIRequestContext): Promise<{ total_value: number; recorded_at: string }[]> {
  const res = await request.get("/api/portfolio/history");
  expect(res.ok()).toBeTruthy();
  return res.json();
}

// --------------------------------------------------------------------------- price-flash recorder

/**
 * The flash classes (flash-up / flash-down) live for only 500ms, shorter than Playwright's polling
 * interval, so asserting on them directly is flaky. Instead, install a MutationObserver that counts
 * every time a watchlist price element receives one of the classes.
 */
export async function startFlashRecorder(page: Page): Promise<void> {
  await page.evaluate(() => {
    const w = window as unknown as { __flashes?: { up: number; down: number } };
    if (w.__flashes) return;
    w.__flashes = { up: 0, down: 0 };
    const count = (el: Element) => {
      if (!el.matches('[data-testid^="watch-price-"]')) return;
      if (el.classList.contains("flash-up")) w.__flashes!.up++;
      if (el.classList.contains("flash-down")) w.__flashes!.down++;
    };
    // The price span is re-keyed on every change, so a flash shows up as an added node
    // (and the class being cleared later shows up as an attribute change).
    new MutationObserver((mutations) => {
      for (const m of mutations) {
        if (m.type === "attributes" && m.target instanceof Element) count(m.target);
        m.addedNodes.forEach((n) => n instanceof Element && count(n));
      }
    }).observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ["class"] });
  });
}

export async function readFlashCounts(page: Page): Promise<{ up: number; down: number }> {
  return page.evaluate(() => (window as unknown as { __flashes: { up: number; down: number } }).__flashes);
}

// --------------------------------------------------------------------------- network drop simulation

export interface SeverableProxy {
  /** URL the browser should load instead of the real app. */
  url: string;
  /** Kill every open connection and refuse new ones (like the server/network going away). */
  sever(): Promise<void>;
  /** Accept connections again. */
  restore(): Promise<void>;
  close(): Promise<void>;
}

/**
 * A minimal TCP proxy in front of the app, which the test can cut and restore at will.
 *
 * Why not `context.setOffline(true)`? Chromium's offline emulation does not tear down an
 * already-open EventSource, so the stream would silently keep flowing. Killing the sockets is a
 * real connection drop, which is exactly what the browser's SSE reconnect logic must survive.
 */
export async function startSeverableProxy(target: URL): Promise<SeverableProxy> {
  const port = await pickFreePort();
  const sockets = new Set<net.Socket>();
  let server: net.Server | null = null;

  const track = (s: net.Socket) => {
    sockets.add(s);
    s.on("close", () => sockets.delete(s));
    s.on("error", () => s.destroy());
  };

  const listen = () =>
    new Promise<void>((resolve, reject) => {
      server = net.createServer((client) => {
        const upstream = net.connect(Number(target.port || 80), target.hostname);
        track(client);
        track(upstream);
        client.pipe(upstream);
        upstream.pipe(client);
      });
      server.once("error", reject);
      server.listen(port, "127.0.0.1", () => resolve());
    });

  const stop = async () => {
    sockets.forEach((s) => s.destroy());
    const s = server;
    server = null;
    if (s) await new Promise<void>((resolve) => s.close(() => resolve()));
  };

  await listen();
  return {
    url: `http://127.0.0.1:${port}`,
    sever: stop,
    restore: async () => {
      if (!server) await listen();
    },
    close: stop,
  };
}

function pickFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.once("error", reject);
    s.listen(0, "127.0.0.1", () => {
      const { port } = s.address() as net.AddressInfo;
      s.close(() => resolve(port));
    });
  });
}
