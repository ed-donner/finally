"""FinAlly Market Data — Browser Demo.

A minimal, self-contained web view of the live market-data subsystem. It does
NOT replace the planned Next.js frontend — it's a ~single-file demo that proves
the existing backend (price cache + SSE endpoint) streams to a browser.

Run with:

    cd backend
    uv run web_demo.py
    # then open http://localhost:8000

What it wires together (all existing code, nothing new in app/):
  - PriceCache + create_market_data_source()  → the simulator (or Massive)
  - create_stream_router(cache)               → GET /api/stream/prices (SSE)
  - one HTML page using EventSource to render live prices with flash + day %%
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.market import PriceCache, create_market_data_source, create_stream_router

DEFAULT_TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]

# Shared price cache + data source for the process lifetime.
cache = PriceCache()
source = create_market_data_source(cache)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the market-data source on boot, stop it on shutdown."""
    await source.start(DEFAULT_TICKERS)
    try:
        yield
    finally:
        await source.stop()


app = FastAPI(title="FinAlly Market Data Demo", lifespan=lifespan)
# Mount the real SSE endpoint: GET /api/stream/prices
app.include_router(create_stream_router(cache))


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>FinAlly — Market Data Demo</title>
<style>
  :root {
    --bg: #0d1117; --panel: #161b22; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e;
    --up: #2ea043; --down: #f85149;
    --yellow: #ecad0a; --blue: #209dd7; --purple: #753991;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  header {
    display: flex; align-items: center; gap: 16px;
    padding: 14px 20px; border-bottom: 1px solid var(--border); background: var(--panel);
  }
  header h1 { font-size: 16px; margin: 0; font-weight: 700; }
  header h1 .ally { color: var(--yellow); }
  .status { display: flex; align-items: center; gap: 7px; color: var(--muted); font-size: 12px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--muted); }
  .dot.connected { background: var(--up); box-shadow: 0 0 8px var(--up); }
  .dot.reconnecting { background: var(--yellow); box-shadow: 0 0 8px var(--yellow); }
  .dot.disconnected { background: var(--down); box-shadow: 0 0 8px var(--down); }
  .spacer { flex: 1; }
  .src { color: var(--purple); font-weight: 700; }

  .grid {
    display: grid; gap: 12px; padding: 20px;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  }
  .card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 14px 16px; transition: background-color 0.5s ease;
  }
  .card.flash-up { background-color: rgba(46, 160, 67, 0.28); }
  .card.flash-down { background-color: rgba(248, 81, 73, 0.28); }
  .card .row { display: flex; align-items: baseline; justify-content: space-between; }
  .ticker { font-size: 15px; font-weight: 700; letter-spacing: 0.5px; }
  .price { font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .sub { margin-top: 6px; display: flex; align-items: center; justify-content: space-between; }
  .day { font-size: 13px; font-weight: 600; font-variant-numeric: tabular-nums; }
  .up { color: var(--up); } .down { color: var(--down); } .flat { color: var(--muted); }
  svg.spark { display: block; margin-top: 10px; width: 100%; height: 32px; }
  footer { padding: 10px 20px; color: var(--muted); font-size: 12px; border-top: 1px solid var(--border); }
</style>
</head>
<body>
  <header>
    <h1>Fin<span class="ally">Ally</span> · Market Data</h1>
    <span class="src" id="src">streaming</span>
    <div class="spacer"></div>
    <div class="status"><span class="dot" id="dot"></span><span id="statusText">connecting…</span></div>
  </header>

  <div class="grid" id="grid"></div>
  <footer>Live via <code>GET /api/stream/prices</code> (Server-Sent Events). Sparklines accumulate since page load.</footer>

<script>
  const grid = document.getElementById("grid");
  const dot = document.getElementById("dot");
  const statusText = document.getElementById("statusText");
  const cards = {};          // ticker -> elements
  const histories = {};      // ticker -> number[]
  const HIST = 40;

  function setStatus(state, text) {
    dot.className = "dot " + state;
    statusText.textContent = text;
  }

  function cls(direction) {
    return direction === "up" ? "up" : direction === "down" ? "down" : "flat";
  }

  function sparkPath(values, w, h) {
    if (values.length < 2) return "";
    const lo = Math.min(...values), hi = Math.max(...values);
    const span = hi - lo || 1;
    const step = w / (values.length - 1);
    return values.map((v, i) => {
      const x = (i * step).toFixed(1);
      const y = (h - ((v - lo) / span) * h).toFixed(1);
      return (i === 0 ? "M" : "L") + x + " " + y;
    }).join(" ");
  }

  function ensureCard(ticker) {
    if (cards[ticker]) return cards[ticker];
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML =
      '<div class="row"><span class="ticker"></span><span class="price"></span></div>' +
      '<div class="sub"><span class="day"></span><span class="chg flat"></span></div>' +
      '<svg class="spark" viewBox="0 0 200 32" preserveAspectRatio="none">' +
      '<path fill="none" stroke-width="1.5"></path></svg>';
    grid.appendChild(card);
    const els = {
      card,
      ticker: card.querySelector(".ticker"),
      price: card.querySelector(".price"),
      day: card.querySelector(".day"),
      chg: card.querySelector(".chg"),
      path: card.querySelector("path"),
    };
    els.ticker.textContent = ticker;
    cards[ticker] = els;
    histories[ticker] = [];
    return els;
  }

  function render(ticker, u) {
    const els = ensureCard(ticker);
    els.price.textContent = "$" + u.price.toFixed(2);

    const dayCls = cls(u.day_change_percent > 0 ? "up" : u.day_change_percent < 0 ? "down" : "flat");
    els.day.className = "day " + dayCls;
    els.day.textContent = (u.day_change_percent >= 0 ? "+" : "") + u.day_change_percent.toFixed(2) + "% day";

    els.chg.className = "chg " + cls(u.direction);
    const arrow = u.direction === "up" ? "▲" : u.direction === "down" ? "▼" : "─";
    els.chg.textContent = arrow + " " + (u.change >= 0 ? "+" : "") + u.change.toFixed(2);

    // Flash on a real tick change.
    if (u.direction !== "flat") {
      const f = u.direction === "up" ? "flash-up" : "flash-down";
      els.card.classList.remove("flash-up", "flash-down");
      void els.card.offsetWidth;            // restart the CSS transition
      els.card.classList.add(f);
      setTimeout(() => els.card.classList.remove(f), 500);
    }

    // Sparkline accumulated client-side since page load.
    const h = histories[ticker];
    h.push(u.price);
    if (h.length > HIST) h.shift();
    const color = getComputedStyle(els.day).color;
    els.path.setAttribute("d", sparkPath(h, 200, 32));
    els.path.setAttribute("stroke", color);
  }

  const es = new EventSource("/api/stream/prices");
  es.onopen = () => setStatus("connected", "connected");
  es.onerror = () => setStatus("reconnecting", "reconnecting…");
  es.onmessage = (e) => {
    setStatus("connected", "connected");
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    for (const ticker of Object.keys(data)) render(ticker, data[ticker]);
  };
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    """Serve the single-page live-price view."""
    return INDEX_HTML


@app.get("/api/source")
async def source_info() -> dict:
    """Expose which data source is active + its health (handy for the demo)."""
    return {"source": type(source).__name__, "health": source.health()}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
