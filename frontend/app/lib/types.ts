export interface PriceUpdate {
  ticker: string;
  price: number;
  previous_price: number;
  timestamp: number;
  change: number;
  change_percent: number;
  direction: "up" | "down" | "flat";
}

export interface WatchlistItem {
  ticker: string;
  added_at: string;
  price: number | null;
  previous_price: number | null;
  change: number | null;
  change_percent: number | null;
  direction: string | null;
}

export interface Position {
  ticker: string;
  quantity: number;
  avg_cost: number;
  current_price: number;
  market_value: number;
  unrealized_pnl: number;
  pnl_percent: number;
}

export interface Portfolio {
  cash: number;
  positions: Position[];
  total_value: number;
}

export interface TradeResult {
  ticker: string;
  side: string;
  quantity: number;
  price: number;
  total_cost: number;
  cash_after: number;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  created_at: string;
  actions?: {
    trades: { ticker: string; side: string; quantity: number; price: number }[];
    trade_errors: string[];
    watchlist_changes: { ticker: string; action: string }[];
  };
}

export interface PortfolioSnapshot {
  total_value: number;
  recorded_at: string;
}

export type ConnectionStatus = "connected" | "reconnecting" | "disconnected";
