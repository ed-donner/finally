import {
  ChatRequest,
  ChatResponse,
  Portfolio,
  PortfolioSnapshot,
  Position,
  PriceUpdate,
  TradeRequest,
  WatchlistItem,
} from '@/src/types/trading';

const DEFAULT_WATCHLIST_GROUPS: Array<{ label: string; tickers: string[] }> = [
  { label: 'Tech', tickers: ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'ORCL', 'CRM', 'ADBE', 'INTC'] },
  { label: 'Financials', tickers: ['JPM', 'BAC', 'WFC', 'C', 'GS', 'MS', 'V', 'MA', 'AXP', 'BLK'] },
  { label: 'Healthcare', tickers: ['JNJ', 'PFE', 'MRK', 'UNH', 'ABBV', 'LLY', 'TMO', 'ABT', 'DHR', 'BMY'] },
  { label: 'Consumer', tickers: ['WMT', 'COST', 'HD', 'MCD', 'NKE', 'SBUX', 'KO', 'PEP', 'DIS', 'NFLX'] },
  { label: 'Industrials & Energy', tickers: ['XOM', 'CVX', 'CAT', 'DE', 'BA', 'GE', 'RTX', 'UPS', 'UNP', 'HON'] },
];

const DEFAULT_TICKERS = DEFAULT_WATCHLIST_GROUPS.flatMap((group) => group.tickers);

const DEFAULT_GROUP_BY_TICKER = new Map<string, string>(
  DEFAULT_WATCHLIST_GROUPS.flatMap((group) => group.tickers.map((ticker) => [ticker, group.label] as const)),
);

const request = async <T>(input: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(input, {
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
    ...init,
  });

  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const contentType = response.headers?.get?.('content-type') ?? '';
  if (!contentType.includes('application/json') && typeof response.json !== 'function') {
    return undefined as T;
  }

  return (await response.json()) as T;
};

const safeNumber = (value: unknown, fallback = 0): number => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

const resolveGroup = (raw: Record<string, unknown>, ticker: string): string => {
  const group = raw.group;
  if (group && typeof group === 'object') {
    const label = String((group as { label?: unknown }).label ?? '').trim();
    if (label) return label;
  }
  if (typeof group === 'string' && group.trim()) return group.trim();
  return String(raw.sector ?? raw.sector_group ?? DEFAULT_GROUP_BY_TICKER.get(ticker) ?? 'Other');
};

const resolveDayBaselinePrice = (raw: Record<string, unknown>, price: number, previousPrice: number): number => {
  const baseline = safeNumber(
    raw.day_baseline_price ??
      raw.dayBaselinePrice ??
      raw.previous_close ??
      raw.previousClose ??
      raw.day_previous_close ??
      raw.dayPreviousClose,
    Number.NaN,
  );
  if (Number.isFinite(baseline) && baseline > 0) return baseline;
  if (previousPrice > 0) return previousPrice;
  return price;
};

const resolveDayChangePercent = (raw: Record<string, unknown>, price: number, dayBaselinePrice: number): number => {
  const explicit = safeNumber(
    raw.day_change_percent ??
      raw.dayChangePercent ??
      raw.session_change_percent ??
      raw.sessionChangePercent ??
      raw.change_percent_day ??
      raw.changePercentDay ??
      raw.change_percent ??
      raw.changePercent,
    Number.NaN,
  );
  if (Number.isFinite(explicit)) return explicit;
  if (dayBaselinePrice <= 0) return 0;
  return ((price - dayBaselinePrice) / dayBaselinePrice) * 100;
};

const normalizePosition = (value: unknown): Position | null => {
  if (!value || typeof value !== 'object') return null;
  const raw = value as Record<string, unknown>;
  const ticker = String(raw.ticker ?? '').toUpperCase();
  if (!ticker) return null;

  return {
    ticker,
    quantity: safeNumber(raw.quantity),
    avg_cost: safeNumber(raw.avg_cost ?? raw.avgCost),
    current_price: safeNumber(raw.current_price ?? raw.currentPrice),
    unrealized_pnl: safeNumber(raw.unrealized_pnl ?? raw.unrealizedPnl),
    change_percent: safeNumber(raw.change_percent ?? raw.changePercent),
  };
};

export const toWatchlistItems = (tickers: string[]): WatchlistItem[] =>
  tickers.map((ticker) => ({
    ticker,
    price: 0,
    previousPrice: 0,
    dayBaselinePrice: 0,
    changePercent: 0,
    direction: 'flat',
    flash: 'flat',
    group: DEFAULT_GROUP_BY_TICKER.get(ticker) ?? 'Other',
  }));

export const parseSsePayload = (raw: string): Record<string, PriceUpdate> => {
  const parsed = JSON.parse(raw) as Record<string, PriceUpdate>;
  return parsed;
};

export const fetchWatchlist = async (): Promise<WatchlistItem[]> => {
  try {
    const data = await request<unknown>('/api/watchlist');
    if (data && typeof data === 'object' && 'items' in data) {
      const items = Array.isArray((data as { items: unknown[] }).items)
        ? (data as { items: unknown[] }).items
        : [];
      const normalized = items
        .map((item) => {
          if (!item || typeof item !== 'object') return null;
          const raw = item as Record<string, unknown>;
          const ticker = String(raw.ticker ?? '').toUpperCase();
          if (!ticker) return null;
          const price = safeNumber(raw.price);
          const previousPrice = safeNumber(raw.previous_price ?? raw.previousPrice, price);
          const dayBaselinePrice = resolveDayBaselinePrice(raw, price, previousPrice);
          const changePercent = resolveDayChangePercent(raw, price, dayBaselinePrice);
          const directionValue = String(raw.direction ?? 'flat') as WatchlistItem['direction'];
          const direction = directionValue === 'up' || directionValue === 'down' ? directionValue : 'flat';
          return {
            ticker,
            price,
            previousPrice,
            dayBaselinePrice,
            changePercent,
            direction,
            flash: direction,
            group: resolveGroup(raw, ticker),
          };
        })
        .filter((item): item is WatchlistItem => Boolean(item));
      return normalized.length ? normalized : toWatchlistItems(DEFAULT_TICKERS);
    }

    if (Array.isArray(data)) {
      const tickers = data
        .map((item) => {
          if (typeof item === 'string') return item.toUpperCase();
          if (item && typeof item === 'object' && 'ticker' in item) {
            return String((item as { ticker: string }).ticker).toUpperCase();
          }
          return '';
        })
        .filter(Boolean);
      return toWatchlistItems(tickers.length ? tickers : DEFAULT_TICKERS);
    }

    if (data && typeof data === 'object' && 'tickers' in data) {
      const tickers = Array.isArray((data as { tickers: string[] }).tickers)
        ? (data as { tickers: string[] }).tickers.map((ticker) => ticker.toUpperCase())
        : [];
      return toWatchlistItems(tickers.length ? tickers : DEFAULT_TICKERS);
    }
  } catch {
    return toWatchlistItems(DEFAULT_TICKERS);
  }

  return toWatchlistItems(DEFAULT_TICKERS);
};

export const fetchPortfolio = async (): Promise<Portfolio> => {
  try {
    const data = await request<unknown>('/api/portfolio');
    if (data && typeof data === 'object') {
      const raw = data as Record<string, unknown>;
      const rawPositions = Array.isArray(raw.positions) ? raw.positions : [];
      const positions = rawPositions.map(normalizePosition).filter((item): item is Position => Boolean(item));
      return {
        cash_balance: safeNumber(raw.cash_balance ?? raw.cashBalance, 10000),
        total_value: safeNumber(raw.total_value ?? raw.totalValue, 10000),
        unrealized_pnl: safeNumber(raw.unrealized_pnl ?? raw.unrealizedPnl),
        positions,
      };
    }
  } catch {
    return {
      cash_balance: 10000,
      total_value: 10000,
      unrealized_pnl: 0,
      positions: [],
    };
  }

  return {
    cash_balance: 10000,
    total_value: 10000,
    unrealized_pnl: 0,
    positions: [],
  };
};

export const fetchPortfolioHistory = async (): Promise<PortfolioSnapshot[]> => {
  try {
    const data = await request<unknown>('/api/portfolio/history');
    if (data && typeof data === 'object' && 'items' in data) {
      const rawItems = Array.isArray((data as { items: unknown[] }).items)
        ? (data as { items: unknown[] }).items
        : [];
      return rawItems
        .map((item) => {
          if (!item || typeof item !== 'object') return null;
          const raw = item as Record<string, unknown>;
          const timestamp = String(raw.recorded_at ?? raw.timestamp ?? '');
          const totalValue = safeNumber(raw.total_value ?? raw.totalValue);
          return timestamp ? { timestamp, total_value: totalValue } : null;
        })
        .filter((item): item is PortfolioSnapshot => Boolean(item));
    }

    if (Array.isArray(data)) {
      return data
        .map((item) => {
          if (!item || typeof item !== 'object') return null;
          const raw = item as Record<string, unknown>;
          const timestamp = String(raw.timestamp ?? '');
          const totalValue = safeNumber(raw.total_value ?? raw.totalValue);
          return timestamp ? { timestamp, total_value: totalValue } : null;
        })
        .filter((item): item is PortfolioSnapshot => Boolean(item));
    }
  } catch {
    return [];
  }

  return [];
};

export const postTrade = async (payload: TradeRequest): Promise<void> => {
  await request('/api/portfolio/trade', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
};

export const addWatchlistTicker = async (ticker: string): Promise<void> => {
  await request('/api/watchlist', {
    method: 'POST',
    body: JSON.stringify({ ticker }),
  });
};

export const removeWatchlistTicker = async (ticker: string): Promise<void> => {
  await request(`/api/watchlist/${ticker}`, {
    method: 'DELETE',
  });
};

export const sendChat = async (payload: ChatRequest): Promise<ChatResponse> =>
  request('/api/chat', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
