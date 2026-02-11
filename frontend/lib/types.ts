/** Price update from SSE stream */
export interface PriceUpdate {
  ticker: string;
  price: number;
  previous_price: number;
  timestamp: number;
  change: number;
  change_percent: number;
  direction: "up" | "down" | "flat";
}

/** Map of ticker -> PriceUpdate from SSE */
export type PriceMap = Record<string, PriceUpdate>;

/** Portfolio position */
export interface Position {
  ticker: string;
  quantity: number;
  avg_cost: number;
  current_price: number;
  unrealized_pnl: number;
  pnl_percent: number;
  market_value: number;
}

/** Portfolio response from GET /api/portfolio */
export interface Portfolio {
  cash_balance: number;
  total_value: number;
  positions: Position[];
}

/** Trade request body for POST /api/portfolio/trade */
export interface TradeRequest {
  ticker: string;
  quantity: number;
  side: "buy" | "sell";
}

/** Trade response */
export interface TradeResponse {
  id: string;
  ticker: string;
  side: "buy" | "sell";
  quantity: number;
  price: number;
  executed_at: string;
}

/** Watchlist item from GET /api/watchlist */
export interface WatchlistItem {
  ticker: string;
  price?: number;
  previous_price?: number;
  change?: number;
  change_percent?: number;
  direction?: "up" | "down" | "flat";
}

/** Portfolio snapshot for P&L chart */
export interface PortfolioSnapshot {
  total_value: number;
  recorded_at: string;
}

/** Chat message */
export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  actions?: ChatActions | null;
  created_at: string;
}

/** Actions the LLM took */
export interface ChatActions {
  trades?: TradeResponse[];
  watchlist_changes?: { ticker: string; action: "add" | "remove" }[];
  errors?: string[];
}

/** Chat request body */
export interface ChatRequest {
  message: string;
}

/** Chat response from POST /api/chat */
export interface ChatResponse {
  message: ChatMessage;
}

/** Connection status for SSE */
export type ConnectionStatus = "connected" | "connecting" | "disconnected";
