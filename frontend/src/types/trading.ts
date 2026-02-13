export type Direction = 'up' | 'down' | 'flat';
export type ConnectionState = 'connected' | 'reconnecting' | 'disconnected';

export interface PriceUpdate {
  ticker: string;
  price: number;
  previous_price: number;
  timestamp: number;
  change: number;
  direction: Direction;
  change_percent?: number;
  previous_close?: number;
  day_baseline_price?: number;
  day_change?: number;
  day_change_percent?: number;
}

export interface WatchlistItem {
  ticker: string;
  price: number;
  previousPrice: number;
  dayBaselinePrice: number;
  changePercent: number;
  direction: Direction;
  flash: Direction;
  group: string;
}

export interface Position {
  ticker: string;
  quantity: number;
  avg_cost: number;
  current_price: number;
  unrealized_pnl: number;
  change_percent: number;
}

export interface Portfolio {
  cash_balance: number;
  total_value: number;
  unrealized_pnl: number;
  positions: Position[];
}

export interface PortfolioSnapshot {
  timestamp: string;
  total_value: number;
}

export interface TradeRequest {
  ticker: string;
  quantity: number;
  side: 'buy' | 'sell';
}

export interface ChatRequest {
  message: string;
}

export interface ChatTrade {
  ticker: string;
  side: 'buy' | 'sell';
  quantity: number;
  status?: 'executed' | 'failed';
  reason?: string;
}

export interface ChatWatchlistChange {
  ticker: string;
  action: 'add' | 'remove';
  status?: 'executed' | 'failed';
  reason?: string;
}

export interface ChatResponse {
  message: string;
  actions?: {
    trades?: ChatTrade[];
    watchlist_changes?: ChatWatchlistChange[];
    errors?: string[];
  };
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  message: string;
  actions?: {
    trades?: ChatTrade[];
    watchlist_changes?: ChatWatchlistChange[];
  };
}
