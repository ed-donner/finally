export type Direction = "up" | "down" | "flat";

/** One ticker in the SSE `data` map (see TEAM_CONTRACTS §5). */
export interface PriceUpdate {
  ticker: string;
  price: number;
  previous_price: number;
  timestamp: number;
  change: number;
  change_percent: number;
  direction: Direction;
  reference_price?: number | null;
  day_change?: number | null;
  day_change_percent?: number | null;
}

export type PriceMap = Record<string, PriceUpdate>;

export type ConnectionStatus = "connected" | "reconnecting" | "disconnected";

export interface PricePoint {
  /** seconds since epoch */
  time: number;
  value: number;
}

export interface Position {
  ticker: string;
  quantity: number;
  avg_cost: number;
  current_price: number;
  market_value: number;
  unrealized_pnl: number;
  unrealized_pnl_percent: number;
  weight: number;
}

export interface Portfolio {
  cash_balance: number;
  positions_value: number;
  total_value: number;
  unrealized_pnl: number;
  unrealized_pnl_percent: number;
  positions: Position[];
}

export interface Snapshot {
  total_value: number;
  recorded_at: string;
}

export interface WatchlistItem {
  ticker: string;
  price: number | null;
  previous_price: number | null;
  change_percent: number | null;
  day_change_percent: number | null;
  direction: Direction | null;
  timestamp: number | null;
}

export interface Trade {
  id: string;
  ticker: string;
  side: "buy" | "sell";
  quantity: number;
  price: number;
  executed_at: string;
}

export interface TradeResponse {
  trade: Trade;
  portfolio: Portfolio;
}

export type ActionStatus = "executed" | "failed";

export interface ChatTradeAction {
  ticker: string;
  side: "buy" | "sell";
  quantity: number;
  status: ActionStatus;
  price?: number | null;
  error?: string | null;
}

export interface ChatWatchlistAction {
  ticker: string;
  action: "add" | "remove";
  status: ActionStatus;
  error?: string | null;
}

export interface ChatActions {
  trades?: ChatTradeAction[];
  watchlist_changes?: ChatWatchlistAction[];
}

export interface ChatResponse extends ChatActions {
  message: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  actions: ChatActions | null;
  created_at: string;
}
