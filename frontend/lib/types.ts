export interface PriceUpdate {
  ticker: string;
  price: number;
  previous_price: number;
  timestamp: number;
  change: number;
  change_percent: number;
  direction: "up" | "down" | "flat";
}

export interface WatchlistEntry extends PriceUpdate {
  added_at: string;
}

export interface Position {
  ticker: string;
  quantity: number;
  avg_cost: number;
  current_price: number;
  market_value: number;
  unrealized_pnl: number;
  pnl_percent: number;
  updated_at: string;
}

export interface Portfolio {
  cash_balance: number;
  total_value: number;
  positions_value: number;
  positions: Position[];
}

export interface PortfolioSnapshot {
  total_value: number;
  recorded_at: string;
}

export interface TradeResult {
  ticker: string;
  side: string;
  quantity?: number;
  price?: number;
  success?: boolean;
  error?: string;
}

export interface WatchlistChangeResult {
  ticker: string;
  action: string;
  success?: boolean;
  error?: string;
}

export interface ChatActions {
  trades: TradeResult[];
  watchlist_changes: WatchlistChangeResult[];
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  actions: ChatActions | null;
  created_at: string;
}

export type ConnectionStatus = "connected" | "reconnecting" | "disconnected";
