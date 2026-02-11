# Feature Research

**Domain:** AI-powered trading workstation (simulated portfolio, Bloomberg-inspired terminal)
**Researched:** 2026-02-11
**Confidence:** HIGH

## Feature Landscape

### Table Stakes (Users Expect These)

Features users assume exist in any trading terminal. Missing these = product feels broken or toy-like.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| **Live price watchlist** | Every trading platform shows real-time prices; Robinhood, Webull, TradingView all have this as the primary view | LOW | Already have SSE streaming + PriceCache. Frontend needs ticker grid with symbol, price, change %, direction indicator |
| **Price flash animation (uptick/downtick)** | Bloomberg, TradingView, and every broker flash green/red on price changes. Users read direction from color before numbers | LOW | CSS transition on background-color, ~500ms fade. Apply class on SSE update, remove after timeout |
| **Connection status indicator** | Users need to know if data is live. Stale data with no warning destroys trust | LOW | Small colored dot in header. Green=connected, yellow=reconnecting, red=disconnected. Trivial to implement from EventSource state |
| **Buy/sell trade execution** | Core interaction of any trading platform. Without it, it's just a ticker viewer | MEDIUM | Market orders only (no limit/stop). POST to /api/portfolio/trade. Validate cash (buy) or shares (sell). Instant fill at current price |
| **Portfolio positions table** | Robinhood, Webull, every broker shows: ticker, qty, avg cost, current price, unrealized P&L, % change | MEDIUM | Requires positions DB table + live price join. Standard tabular display |
| **Cash balance display** | Users must always know how much buying power they have. Robinhood puts this front and center | LOW | Read from users_profile table. Update after every trade. Display in header |
| **Total portfolio value** | The single most important number. Every trading app shows this prominently, usually with daily change | LOW | Sum of (position qty * current price) + cash. Update on every price tick |
| **Trade history** | Users expect to see what they did. Append-only log of executed trades | LOW | Read from trades table. Simple chronological list with ticker, side, qty, price, timestamp |
| **Price chart for selected ticker** | Clicking a ticker should show a larger chart. TradingView, Bloomberg, every terminal has this | MEDIUM | Accumulated from SSE stream since page load. Use canvas-based charting library (Lightweight Charts recommended) |
| **Sparkline mini-charts in watchlist** | TradingView and modern terminals show inline sparklines beside each ticker. Users expect visual price history at a glance | MEDIUM | Accumulate price points from SSE per ticker. Render small SVG or canvas sparkline per row. Fills in progressively as data arrives |
| **Dark theme / terminal aesthetic** | Trading terminals are always dark. Bloomberg, TradingView, Robinhood all default to dark. Light theme on a terminal looks amateur | LOW | Tailwind dark theme. Backgrounds #0d1117 / #1a1a2e, muted borders, no pure black. Professional data-dense layout |
| **Responsive error handling for trades** | When a trade fails (insufficient cash, insufficient shares), user needs clear immediate feedback. No silent failures | LOW | Return error from API with reason. Display inline in trade bar. Red text, auto-dismiss after a few seconds |

### Differentiators (Competitive Advantage)

Features that set FinAlly apart from typical trading demos and even some production platforms. Not expected, but create the "wow" factor.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| **AI chat assistant with auto-trade execution** | The core differentiator. No mainstream broker lets an LLM directly execute trades. Composer comes close with natural language strategies, but FinAlly's conversational auto-execution is unique for a demo | HIGH | LLM call via LiteLLM/OpenRouter. Structured output with trades[] and watchlist_changes[]. Auto-execute without confirmation. Requires portfolio context in prompt, conversation history, tool-use pattern |
| **Portfolio heatmap (treemap)** | Finviz popularized market heatmaps; applying the same to a personal portfolio is visually striking and immediately communicates position sizing and P&L at a glance | MEDIUM | Treemap with rectangles sized by portfolio weight, colored by P&L (green=profit, red=loss). Nivo or Recharts treemap component. Needs live price data to color dynamically |
| **Inline trade confirmations in chat** | When the AI executes trades, showing them inline as structured cards (not just text) makes the agentic capability tangible and visible | LOW | Parse actions from chat response JSON. Render trade/watchlist-change cards inline in chat history. Visual proof of AI agency |
| **P&L chart (portfolio value over time)** | Most simple trading demos don't track portfolio value history. A running chart shows the consequence of decisions over time | MEDIUM | Background task snapshots portfolio value every 30s + after each trade. Line chart from portfolio_snapshots table. Recharts or Lightweight Charts |
| **AI-driven watchlist management** | User says "add Tesla" or "track some energy stocks" and the LLM modifies the watchlist. Natural language CRUD on the watchlist | LOW | Already in structured output schema (watchlist_changes[]). Low marginal complexity on top of AI chat |
| **LLM portfolio analysis** | AI can analyze concentration risk, sector exposure, P&L attribution, suggest rebalancing. Goes beyond what simple trading apps offer | LOW | Complexity is in prompt engineering, not code. Feed portfolio context (positions, P&L, sector info) into system prompt. LLM does the analysis |
| **Mock LLM mode for testing** | Deterministic mock responses enable E2E testing without API keys or cost. Professional testing practice that most demos skip | LOW | Simple conditional: if LLM_MOCK=true, return canned JSON. Enables CI/CD and reproducible Playwright tests |
| **Watchlist add/remove via UI** | Users can manually add tickers beyond the default 10, or remove ones they don't care about. Combines with AI-driven management | LOW | POST/DELETE to /api/watchlist. Input field + button in watchlist panel. Dynamically adds ticker to SSE stream via source.add_ticker() |

### Anti-Features (Deliberately NOT Building)

Features that seem good but add complexity without proportional value, or violate project constraints.

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| **Limit/stop orders** | "Real" trading has them | Requires order book logic, partial fills, order lifecycle (pending/filled/cancelled), time-in-force. Triples portfolio code complexity for a simulated-money demo | Market orders only. Instant fill at current price. Zero order management complexity |
| **Multi-user auth** | "Production" apps have login | No actual money, no privacy concerns. Auth adds JWT/session handling, per-user data isolation, password management, registration flow. Massive scope increase for zero user value in a demo | Single hardcoded "default" user. Schema has user_id column for future-proofing, but no auth code |
| **Token-by-token chat streaming** | ChatGPT streams tokens | Cerebras inference is fast enough (~1-2s total). SSE streaming of chat adds bidirectional complexity, partial JSON parsing issues, and the structured output (trades/actions) can't execute until complete anyway | Loading indicator while waiting, then render complete response. Simpler, still feels fast |
| **Historical price data / candlestick charts** | "Real" terminals have OHLCV history | Requires historical data storage or API, candlestick rendering, timeframe selectors. Simulator doesn't generate historical data. Massive API historical data is a separate paid endpoint | Accumulate prices from SSE since page load. Charts build up progressively. Sparklines and line charts are sufficient |
| **Multiple chart timeframes (1m, 5m, 1h, 1D)** | TradingView has them | Needs historical data at multiple resolutions. Simulator only produces real-time ticks. Would require storing and aggregating tick data into OHLCV bars | Single rolling chart of price since page load. Simple and honest about the data we have |
| **Order confirmation dialogs** | "Safety" feature | This is fake money. Confirmation dialogs slow down the demo experience and undermine the AI auto-execution showcase. Every click should feel instant | Instant execution + clear visual feedback (position updates, cash changes, trade in history) |
| **Technical indicators (RSI, MACD, Bollinger)** | "Real" charting platforms have them | Requires indicator calculation engine, UI for selecting/configuring indicators, overlay rendering. Huge scope for a demo | Clean price line charts. The AI assistant can comment on price trends verbally |
| **Mobile-first / responsive design** | Users expect mobile | This is a data-dense terminal meant for wide screens. Cramming treemaps, watchlists, charts, and chat into a phone screen produces a bad experience | Desktop-first, functional on tablet. Not optimized for phone. State this clearly |
| **Persistent chat history across sessions** | ChatGPT remembers | Chat is stored in DB, so history persists. But loading extensive history into LLM context window wastes tokens and creates confusion across sessions | Load recent messages (last ~20) for context. Fresh feel on each session while maintaining short-term continuity |
| **Real-time news feed** | Bloomberg has news | Requires news API integration, NLP for relevance filtering, additional UI panel. Large scope, tangential to core value | AI assistant can discuss general market context from its training. No live news feed |

## Feature Dependencies

```
[SSE Price Streaming] (DONE)
    |
    +---> [Live Watchlist Display]
    |         |
    |         +---> [Sparkline Mini-Charts]
    |         +---> [Price Flash Animations]
    |         +---> [Selected Ticker Chart]
    |
    +---> [Portfolio Valuation]
              |
              +---> [Total Portfolio Value in Header]
              +---> [Positions Table with Live P&L]
              +---> [Portfolio Heatmap / Treemap]
              +---> [P&L Chart over Time]

[Database Schema + Initialization]
    |
    +---> [User Profile (cash balance)]
    |         |
    |         +---> [Trade Execution]
    |                   |
    |                   +---> [Trade History]
    |                   +---> [Portfolio Snapshots]
    |                   +---> [Positions Table]
    |
    +---> [Watchlist CRUD]
              |
              +---> [Dynamic Ticker Management in SSE]
              +---> [Watchlist UI (add/remove)]

[Trade Execution] + [Watchlist CRUD] + [Portfolio Valuation]
    |
    +---> [AI Chat Assistant]
              |
              +---> [Structured Output Parsing]
              +---> [Auto-Trade Execution from Chat]
              +---> [AI Watchlist Management]
              +---> [Inline Action Confirmations]
```

### Dependency Notes

- **Live Watchlist Display requires SSE Price Streaming:** Watchlist renders data pushed from PriceCache via SSE. Already built.
- **Portfolio Valuation requires SSE Price Streaming:** Current prices needed to calculate unrealized P&L and total value.
- **Trade Execution requires Database + User Profile:** Must check cash balance (buy) or position quantity (sell) before executing.
- **AI Chat requires Trade Execution + Watchlist CRUD + Portfolio Valuation:** The LLM needs portfolio context for analysis and the ability to execute trades and modify the watchlist.
- **Portfolio Heatmap requires Positions + Live Prices:** Treemap sizing = position value (qty * price), coloring = P&L (current vs avg cost).
- **P&L Chart requires Portfolio Snapshots:** Background task must record total portfolio value periodically.
- **Sparklines require accumulated SSE data:** Frontend must buffer price history per ticker from the SSE stream.

## MVP Definition

### Launch With (v1)

Minimum viable product -- what's needed to demonstrate the full vision end-to-end.

- [ ] **Database schema + lazy init** -- foundation for all stateful features
- [ ] **REST API endpoints** -- portfolio, trade, watchlist, chat, health
- [ ] **Live watchlist with price flashing** -- the first thing users see; must look alive
- [ ] **Trade execution (buy/sell)** -- core interaction
- [ ] **Positions table with live P&L** -- show consequences of trades
- [ ] **Cash balance + total portfolio value** -- always visible in header
- [ ] **Selected ticker chart** -- click a ticker, see its price chart
- [ ] **AI chat with auto-execution** -- the headline feature
- [ ] **Portfolio heatmap (treemap)** -- the visual "wow" moment
- [ ] **Dark terminal theme** -- aesthetic is part of the value proposition
- [ ] **Docker single-container deployment** -- one command to run

### Add After Validation (v1.x)

Features to add once core is working and stable.

- [ ] **Sparkline mini-charts** -- adds visual density to watchlist; defer if charting proves complex
- [ ] **P&L chart over time** -- requires snapshot background task; valuable but not blocking
- [ ] **Trade history view** -- simple list, low effort, but not needed for first demo
- [ ] **Watchlist add/remove UI** -- AI can manage watchlist initially; manual UI is polish
- [ ] **Connection status indicator** -- small UX detail; add during polish phase
- [ ] **Mock LLM mode** -- needed for E2E tests; add when building test suite

### Future Consideration (v2+)

Features to defer until the core platform is proven.

- [ ] **E2E Playwright tests** -- important for quality but not for initial demo
- [ ] **Cloud deployment (Terraform/App Runner)** -- stretch goal per PLAN.md
- [ ] **Massive API integration testing** -- simulator is the default path; real data is optional

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority |
|---------|------------|---------------------|----------|
| Database schema + lazy init | HIGH | LOW | P1 |
| REST API (portfolio, trade, watchlist) | HIGH | MEDIUM | P1 |
| Live watchlist with price flash | HIGH | LOW | P1 |
| Trade execution (buy/sell) | HIGH | MEDIUM | P1 |
| Positions table with live P&L | HIGH | MEDIUM | P1 |
| Cash + total value in header | HIGH | LOW | P1 |
| AI chat with auto-execution | HIGH | HIGH | P1 |
| Portfolio heatmap (treemap) | HIGH | MEDIUM | P1 |
| Selected ticker chart | MEDIUM | MEDIUM | P1 |
| Dark terminal theme | HIGH | LOW | P1 |
| Docker packaging | HIGH | MEDIUM | P1 |
| Sparkline mini-charts | MEDIUM | MEDIUM | P2 |
| P&L chart over time | MEDIUM | MEDIUM | P2 |
| Trade history view | LOW | LOW | P2 |
| Watchlist add/remove UI | LOW | LOW | P2 |
| Connection status indicator | LOW | LOW | P2 |
| Mock LLM mode | MEDIUM | LOW | P2 |
| E2E tests | MEDIUM | HIGH | P3 |
| Cloud deployment | LOW | MEDIUM | P3 |

**Priority key:**
- P1: Must have for launch -- the demo is incomplete without these
- P2: Should have, add when possible -- polish and completeness
- P3: Nice to have, future consideration

## Competitor Feature Analysis

| Feature | Bloomberg Terminal | TradingView | Robinhood | Composer | FinAlly Approach |
|---------|-------------------|-------------|-----------|----------|------------------|
| Real-time prices | Comprehensive, all asset classes | Broad coverage, free tier available | Stocks, crypto, options | ETF-focused | 10 default tickers, simulator or Massive API |
| Watchlist | Multiple watchlists, complex filtering | Linked to chart, customizable | Simple list with sparklines | Strategy-centric | Single watchlist, live updating, sparklines |
| Charting | Extensive (OHLCV, indicators, multi-timeframe) | Industry-leading charting | Simple line charts | Minimal | Line charts from SSE data since page load |
| Portfolio view | Comprehensive risk analytics | No portfolio tracking | Clean P&L, simple positions | Strategy performance | Positions table + heatmap + P&L chart |
| Heatmap/treemap | Market heatmaps (sector, index) | Market heatmap widget | None | None | Personal portfolio treemap (unique for demo) |
| Trade execution | Full OMS (all order types) | Broker-connected | Swipe to trade, market/limit | Automated strategies | Simple buy/sell bar, market orders only |
| AI assistant | Bloomberg GPT (internal) | None (Pine Script for automation) | None | Natural language strategy builder | Conversational AI with auto-execution (key differentiator) |
| AI trade execution | No direct auto-execution | No | No | Automated strategy execution | LLM structured output -> instant execution |
| Dark theme | Classic Bloomberg dark | Dark/light toggle | Dark/light toggle | Light default | Dark-only, terminal aesthetic |
| Price | $24,000/year | Free-$60/month | Free | $15/month | Free (self-hosted, fake money) |

## Sources

- [Bloomberg Terminal Features](https://www.bloomberg.com/professional/products/bloomberg-terminal/)
- [TradingView Features](https://www.tradingview.com/features/)
- [TradingView Heatmap Widgets](https://www.tradingview.com/widget-docs/widgets/heatmaps/)
- [Finviz S&P 500 Heatmap](https://finviz.com/map.ashx)
- [Robinhood Legend Announcement](https://newsroom.aboutrobinhood.com/the-legend-awakens/)
- [Webull P&L Documentation](https://www.webull.com/help/faq/10691-Profit-and-Loss-P-L)
- [Composer Trading Platform](https://www.composer.trade/)
- [AI Trading Tools 2026 - Pragmatic Coders](https://www.pragmaticcoders.com/blog/top-ai-tools-for-traders)
- [Best AI Trading Platforms - Monday.com](https://monday.com/blog/ai-agents/best-ai-for-stock-trading/)
- [LLM Trading Bots Comparison - FlowHunt](https://www.flowhunt.io/blog/llm-trading-bots-comparison/)
- [TradingAgents Multi-Agent Framework](https://github.com/TauricResearch/TradingAgents)
- [Fintech UX Best Practices 2026 - Eleken](https://www.eleken.co/blog-posts/fintech-ux-best-practices)
- [SSE Practical Guide](https://tigerabrodi.blog/server-sent-events-a-practical-guide-for-the-real-world)
- [SSE in React - OneUptime](https://oneuptime.com/blog/post/2026-01-15-server-sent-events-sse-react/view)

---
*Feature research for: AI-powered trading workstation*
*Researched: 2026-02-11*
