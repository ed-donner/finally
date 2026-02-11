const BASE = "";

export async function fetchPortfolio() {
  const res = await fetch(`${BASE}/api/portfolio`);
  if (!res.ok) throw new Error("Failed to fetch portfolio");
  return res.json();
}

export async function executeTrade(ticker: string, side: string, quantity: number) {
  const res = await fetch(`${BASE}/api/portfolio/trade`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ticker, side, quantity }),
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Trade failed");
  }
  return res.json();
}

export async function fetchPortfolioHistory() {
  const res = await fetch(`${BASE}/api/portfolio/history`);
  if (!res.ok) throw new Error("Failed to fetch history");
  return res.json();
}

export async function fetchWatchlist() {
  const res = await fetch(`${BASE}/api/watchlist`);
  if (!res.ok) throw new Error("Failed to fetch watchlist");
  return res.json();
}

export async function addToWatchlist(ticker: string) {
  const res = await fetch(`${BASE}/api/watchlist`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ticker }),
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Failed to add ticker");
  }
  return res.json();
}

export async function removeFromWatchlist(ticker: string) {
  const res = await fetch(`${BASE}/api/watchlist/${ticker}`, { method: "DELETE" });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Failed to remove ticker");
  }
  return res.json();
}

export async function sendChatMessage(message: string) {
  const res = await fetch(`${BASE}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (!res.ok) throw new Error("Chat request failed");
  return res.json();
}

export async function fetchChatHistory() {
  const res = await fetch(`${BASE}/api/chat/history`);
  if (!res.ok) throw new Error("Failed to fetch chat history");
  return res.json();
}
