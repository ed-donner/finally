// Types mirror the backend Pydantic models (backend/app/agents/models.py).

export type Side = "buy" | "sell";
export type Direction = "up" | "down" | "flat";
export type ConnectionStatus = "connected" | "reconnecting" | "disconnected";

/** One entry of the SSE payload: {"AAPL": PriceTick, ...}. `timestamp` is unix seconds. */
export interface PriceTick {
  ticker: string;
  price: number;
  previous_price: number;
  timestamp: number;
  change: number;
  change_percent: number;
  direction: Direction;
}

/** A price observed on the stream, accumulated on the client for sparklines and charts. */
export interface PricePoint {
  t: number; // unix seconds
  p: number;
}

export interface Position {
  ticker: string;
  quantity: number;
  avg_cost: number;
  current_price: number | null;
  market_value: number;
  unrealized_pnl: number;
  pnl_percent: number;
}

export interface Portfolio {
  cash_balance: number;
  positions: Position[];
  positions_value: number;
  total_value: number;
  starting_cash: number;
  unrealized_pnl: number;
  realized_pnl: number;
  total_pnl: number;
}

export interface Snapshot {
  total_value: number;
  cash_balance: number;
  positions_value: number;
  total_pnl: number;
  recorded_at: string; // ISO-8601
}

export interface WatchlistEntry {
  ticker: string;
  added_by: string;
  added_at: string;
  price: number | null;
  previous_price: number | null;
  change_percent: number | null;
  direction: Direction | null;
}

export interface TradeOrder {
  ticker: string;
  side: Side;
  quantity: number;
}

export interface TradeResult {
  ok: boolean;
  trade_id: string | null;
  ticker: string;
  side: Side;
  quantity: number;
  price: number | null;
  realized_pnl: number | null;
  error: string | null;
  warnings: string[];
}

export interface TradeResponse {
  trade: TradeResult;
  portfolio: Portfolio;
}

export interface WatchlistResult {
  ok: boolean;
  ticker: string;
  action: string;
  error: string | null;
  note: string | null;
}

export interface WatchlistResponse {
  result: WatchlistResult;
  watchlist: WatchlistEntry[];
}

export interface ChatResponse {
  message: string;
  executed_trades: TradeResult[];
  watchlist_changes: WatchlistResult[];
  warnings: string[];
}

export interface PositionMetric {
  ticker: string;
  weight_pct: number;
  unrealized_pnl: number;
  pnl_pct: number;
  day_change_pct: number | null;
}

export interface AnalysisReport {
  total_value: number;
  cash_pct: number;
  positions: PositionMetric[];
  top_concentration: PositionMetric | null;
  herfindahl_index: number;
  best_performer: PositionMetric | null;
  worst_performer: PositionMetric | null;
  watchlist_movers: { ticker: string; price: number; change_percent: number }[];
  flags: string[];
}

export interface RiskAssessment {
  total_value: number;
  cash_pct: number;
  largest_position: PositionMetric | null;
  herfindahl_index: number;
  positions_over_warn: string[];
  positions_over_limit: string[];
  level: "low" | "moderate" | "high";
  flags: string[];
}
