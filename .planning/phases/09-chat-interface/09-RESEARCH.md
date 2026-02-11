# Phase 9: Chat Interface - Research

**Researched:** 2026-02-11
**Domain:** React chat UI, Zustand state management, CSS Grid collapsible sidebar
**Confidence:** HIGH

## Summary

This phase implements the AI chat panel in the FinAlly frontend. The backend chat API is fully complete at `POST /api/chat`, accepting `{ message: string }` and returning `{ message, trades[], watchlist_changes[] }` with execution results. The frontend already has a placeholder `ChatPanel` component in the correct grid position (col-span-3, row-span-2, right side). The existing codebase uses Zustand stores with granular selectors, fetch-based API calls, and Tailwind v4 CSS-first theming.

The implementation requires: (1) a new `chat-store.ts` following the existing store pattern, (2) expanding the `ChatPanel.tsx` placeholder into a full chat interface with message history, input, loading state, and inline action confirmations, (3) making the panel collapsible while maintaining the CSS Grid layout, and (4) refreshing portfolio/watchlist data after chat-driven actions.

**Primary recommendation:** Build a Zustand chat store that mirrors the existing store patterns (fetch-based, granular selectors), with message history and sending state. The ChatPanel itself is straightforward React -- scrollable message list, input form, and inline action cards for trades/watchlist changes. No additional npm packages needed.

## Standard Stack

### Core (Already Installed)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| React | 19.2.3 | UI rendering | Already in project |
| Zustand | 5.0.11 | State management | Already used for price/portfolio/watchlist stores |
| Tailwind CSS | 4.x | Styling | Already configured with custom dark theme |
| Next.js | 16.1.6 | Framework | Already in project, static export mode |

### Supporting (No New Dependencies Needed)
The chat interface requires no additional npm packages. The existing stack provides everything needed:
- Zustand for chat state management
- Native `fetch` for API calls (consistent with existing stores)
- Tailwind for styling (consistent with all existing panels)
- `useRef` + `scrollIntoView` for auto-scroll behavior

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Custom scroll management | react-virtualized | Overkill -- chat history will be dozens of messages, not thousands |
| Plain text rendering | markdown renderer | Unnecessary complexity -- LLM responses are short, data-driven text |
| Custom loading animation | spinner library | Tailwind `animate-pulse` is sufficient and already used in ConnectionDot |

## Architecture Patterns

### New Files to Create
```
frontend/src/
├── stores/
│   └── chat-store.ts         # NEW: Chat state management
└── components/
    └── panels/
        └── ChatPanel.tsx      # MODIFY: Replace placeholder with full implementation
```

### Pattern 1: Chat Store (Zustand)
**What:** A Zustand store managing chat messages, loading state, and the send action.
**When to use:** Follows the exact same pattern as `portfolio-store.ts` and `watchlist-store.ts`.
**Example:**
```typescript
// Source: Existing pattern from portfolio-store.ts and watchlist-store.ts
interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  trades?: TradeResult[];
  watchlist_changes?: WatchlistResult[];
  timestamp?: string;
}

interface TradeResult {
  status: string;
  ticker: string;
  side: string;
  quantity?: number;
  price?: number;
  total?: number;
  error?: string;
}

interface WatchlistResult {
  status: string;
  ticker: string;
  action: string;
  error?: string;
}

interface ChatStore {
  messages: ChatMessage[];
  sending: boolean;
  error: string | null;
  sendMessage: (message: string) => Promise<void>;
}
```

The `sendMessage` action should:
1. Add the user message to `messages` immediately (optimistic)
2. Set `sending: true`
3. POST to `/api/chat` with `{ message }`
4. On success, add the assistant response to `messages` (with trades/watchlist_changes attached)
5. After actions executed, refresh portfolio and watchlist stores
6. Set `sending: false`

### Pattern 2: Collapsible Sidebar via CSS Grid
**What:** The chat panel occupies `col-span-3 row-span-2` in the 12-column grid. To make it collapsible, toggle between `col-span-3` (open) and a narrow collapsed state, redistributing the freed columns.
**When to use:** The PLAN specifies "docked/collapsible sidebar".

**Approach:** Use a `chatOpen` boolean in state (can live in the chat store or a simple `useState` in page.tsx). When collapsed:
- The chat column shrinks to a narrow toggle button strip (effectively 0 content width)
- The adjacent columns (chart and portfolio areas) expand to fill the space
- The most practical implementation: conditionally change the grid column spans in `page.tsx`

**Simpler alternative (recommended):** Keep the chat panel always in the grid but toggle its content between full chat view and a minimal collapsed strip with just a toggle button. This avoids CSS Grid restructuring entirely. The panel itself handles open/closed state internally.

```typescript
// In ChatPanel.tsx
const [isOpen, setIsOpen] = useState(true);

if (!isOpen) {
  return (
    <div className="h-full w-full bg-terminal-surface flex flex-col items-center pt-3">
      <button onClick={() => setIsOpen(true)}
        className="font-mono text-xs text-text-muted hover:text-brand-blue">
        Chat
      </button>
    </div>
  );
}
```

### Pattern 3: Auto-Scroll to Bottom
**What:** Chat should auto-scroll to the latest message when new messages appear.
**When to use:** Every time `messages` array changes.
**Example:**
```typescript
// Source: Standard React pattern
const messagesEndRef = useRef<HTMLDivElement>(null);

useEffect(() => {
  messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
}, [messages]);

// In JSX:
<div className="flex-1 overflow-y-auto">
  {messages.map((msg, i) => <ChatMessage key={i} message={msg} />)}
  <div ref={messagesEndRef} />
</div>
```

### Pattern 4: Inline Action Confirmations
**What:** When the assistant response includes executed trades or watchlist changes, render them as distinct visual cards within the chat bubble.
**When to use:** Any assistant message that has `trades.length > 0` or `watchlist_changes.length > 0`.
**Example:**
```typescript
// Trade execution card
{msg.trades?.filter(t => t.status === "executed").map((trade, i) => (
  <div key={i} className="mt-2 p-2 rounded bg-terminal-bg border border-terminal-border">
    <span className={`font-mono text-xs font-bold ${trade.side === "buy" ? "text-price-up" : "text-price-down"}`}>
      {trade.side.toUpperCase()} {trade.quantity} {trade.ticker} @ ${trade.price?.toFixed(2)}
    </span>
  </div>
))}

// Failed trade
{msg.trades?.filter(t => t.status === "failed").map((trade, i) => (
  <div key={i} className="mt-2 p-2 rounded bg-price-down/10 border border-price-down/30">
    <span className="font-mono text-xs text-price-down">
      Failed: {trade.side} {trade.ticker} -- {trade.error}
    </span>
  </div>
))}

// Watchlist change card
{msg.watchlist_changes?.filter(w => w.status === "applied").map((change, i) => (
  <div key={i} className="mt-2 p-2 rounded bg-terminal-bg border border-terminal-border">
    <span className="font-mono text-xs text-brand-blue">
      {change.action === "add" ? "+" : "-"} {change.ticker} {change.action === "add" ? "added to" : "removed from"} watchlist
    </span>
  </div>
))}
```

### Pattern 5: Cross-Store Refresh After Chat Actions
**What:** When the chat response contains executed trades or watchlist changes, the portfolio and watchlist stores must be refreshed to reflect the new state.
**When to use:** After successful `POST /api/chat` that returns trades or watchlist changes.
**Example:**
```typescript
// In chat store's sendMessage action:
const data = await res.json();

// Refresh other stores if actions were taken
if (data.trades?.some((t: TradeResult) => t.status === "executed")) {
  usePortfolioStore.getState().fetchPortfolio();
  usePortfolioStore.getState().fetchHistory();
}
if (data.watchlist_changes?.some((w: WatchlistResult) => w.status === "applied")) {
  useWatchlistStore.getState().fetchWatchlist();
}
```

This pattern of calling other stores' actions from within a store is already used in the codebase (see `portfolio-store.ts` line 83-84 where `fetchPortfolio` and `fetchHistory` are called after a trade).

### Anti-Patterns to Avoid
- **Don't stream the chat response:** The backend returns a complete JSON response (not SSE). The LLM uses Cerebras inference which is fast enough that a loading indicator suffices. Do NOT try to implement token streaming.
- **Don't persist chat messages on the frontend:** The backend already persists all messages in the `chat_messages` table. The frontend store only needs to hold messages for the current session display. If chat history persistence across page reloads is desired later, add a `GET /api/chat/history` endpoint -- but that is not required for this phase.
- **Don't add a confirmation dialog before trades:** The PLAN explicitly states "no confirmation dialog" -- trades auto-execute for a fluid demo experience.
- **Don't use a third-party chat UI library:** The styling is very specific (terminal aesthetic, monospace fonts, custom colors). A custom implementation is simpler than overriding a library's styles.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| State management | Custom context/reducer | Zustand store | Consistent with 3 existing stores, simpler API |
| Smooth scroll | Custom scroll tracking | `scrollIntoView({ behavior: "smooth" })` | Native browser API, works perfectly |
| Loading animation | Custom spinner component | Tailwind `animate-pulse` on placeholder dots | Already used in project (ConnectionDot) |
| Form submission | Complex form library | Simple `useState` + `onSubmit` | One input field, no validation complexity |

**Key insight:** The chat UI is fundamentally simple -- a message list, an input, and some styled cards. The existing project patterns (Zustand + fetch + Tailwind) cover 100% of what's needed with zero new dependencies.

## Common Pitfalls

### Pitfall 1: Scroll Position Jumps
**What goes wrong:** Auto-scrolling fires even when the user has scrolled up to read history, yanking them back to the bottom.
**Why it happens:** Unconditional `scrollIntoView` on every message change.
**How to avoid:** Only auto-scroll if the user is already near the bottom. Check `scrollHeight - scrollTop - clientHeight < threshold` before scrolling.
**Warning signs:** Users unable to read previous messages while new ones arrive.

### Pitfall 2: Double-Send on Fast Click
**What goes wrong:** User clicks Send twice quickly, sending the same message twice.
**Why it happens:** The `sending` guard isn't checked fast enough or the button isn't disabled.
**How to avoid:** Disable the send button and input while `sending === true`. Also clear the input immediately after starting the send.
**Warning signs:** Duplicate messages in chat history.

### Pitfall 3: Stale Portfolio/Watchlist After Chat Actions
**What goes wrong:** User asks AI to buy AAPL, the trade executes, but the portfolio panel still shows old data.
**Why it happens:** Forgot to trigger cross-store refresh after chat response.
**How to avoid:** After receiving a chat response with executed trades/watchlist changes, call the refresh methods on portfolio and watchlist stores.
**Warning signs:** User has to manually refresh or wait for the next polling interval to see their trade.

### Pitfall 4: Lost Messages on Error
**What goes wrong:** User sends a message, API call fails, the user message disappears.
**Why it happens:** Rolling back the optimistic user message on error.
**How to avoid:** Keep the user message in the list even on error. Show the error inline (e.g., "Failed to get response, please try again") as a system message.
**Warning signs:** User types a long message, it vanishes on network error.

### Pitfall 5: Chat Panel Height Overflow
**What goes wrong:** The chat panel overflows its grid cell, pushing other panels around.
**Why it happens:** Not constraining the message list with `min-h-0` and `overflow-y-auto` in a flex column.
**How to avoid:** The panel structure must be `flex flex-col h-full` with the message area `flex-1 min-h-0 overflow-y-auto`. This is the same pattern used in WatchlistPanel and the Positions column.
**Warning signs:** Scrollbar appears on the main page instead of within the chat panel.

## Code Examples

### Backend API Contract (Verified from source code)

**Request:**
```
POST /api/chat
Content-Type: application/json
{ "message": "buy 10 shares of AAPL" }
```

**Response (200):**
```json
{
  "message": "Done! I've bought 10 shares of AAPL for you at $150.00.",
  "trades": [
    {
      "status": "executed",
      "ticker": "AAPL",
      "side": "buy",
      "quantity": 10,
      "price": 150.00,
      "total": 1500.00,
      "error": null
    }
  ],
  "watchlist_changes": [
    {
      "status": "applied",
      "ticker": "PYPL",
      "action": "add",
      "error": null
    }
  ]
}
```

**Error response for failed trade (still 200):**
```json
{
  "message": "I tried to sell AAPL but you don't own any shares.",
  "trades": [
    {
      "status": "failed",
      "ticker": "AAPL",
      "side": "sell",
      "quantity": null,
      "price": null,
      "total": null,
      "error": "Insufficient shares: have 0, need 5"
    }
  ],
  "watchlist_changes": []
}
```

**Validation error (422):**
- Empty or missing `message` field

### Complete ChatPanel Structure
```typescript
// Source: Derived from existing panel patterns (WatchlistPanel, PortfolioPanel)
<div className="h-full w-full flex flex-col bg-terminal-surface">
  {/* Header */}
  <div className="px-3 pt-3 pb-1 flex items-center justify-between">
    <span className="text-text-muted font-mono text-xs uppercase tracking-wider">
      AI Assistant
    </span>
    <button className="text-text-muted hover:text-brand-blue text-xs">
      {/* collapse toggle */}
    </button>
  </div>

  {/* Message list */}
  <div className="flex-1 min-h-0 overflow-y-auto px-3 py-2 space-y-3">
    {messages.map((msg, i) => (
      <div key={i} className={msg.role === "user" ? "text-right" : "text-left"}>
        {/* Message bubble */}
        {/* Inline action cards if assistant */}
      </div>
    ))}
    {sending && <LoadingIndicator />}
    <div ref={messagesEndRef} />
  </div>

  {/* Input area */}
  <div className="p-2 border-t border-terminal-border">
    <form onSubmit={handleSend} className="flex items-center gap-1">
      <input ... />
      <button type="submit" disabled={sending} ... >Send</button>
    </form>
  </div>
</div>
```

### Existing Layout Grid (from page.tsx)
```
12-column grid, 2 rows:
| Watchlist (3 cols, 2 rows) | Chart (6 cols, 1 row)     | Chat (3 cols, 2 rows) |
|                            | Portfolio (3) | Positions (3) |                      |
```

The ChatPanel is already placed at `col-span-3 row-span-2` in the rightmost position. No grid changes needed for the basic implementation.

### Styling Tokens (from globals.css)
```
Background: bg-terminal-surface (#1a1a2e)
Borders: border-terminal-border (#2d2d44)
Text primary: text-text-primary (#e6edf3)
Text secondary: text-text-secondary (#8b949e)
Text muted: text-text-muted (#484f58)
Accent: text-accent-yellow (#ecad0a)
Blue: text-brand-blue (#209dd7)
Purple: bg-brand-purple (#753991) -- for submit buttons per PLAN
Green: text-price-up (#22c55e) -- for successful buy actions
Red: text-price-down (#ef4444) -- for sell actions / errors
Font mono: font-mono (JetBrains Mono)
```

### Loading Indicator Pattern
```typescript
// Three-dot pulsing indicator, consistent with terminal aesthetic
function LoadingIndicator() {
  return (
    <div className="flex items-center gap-1 px-3 py-2">
      <div className="flex gap-1">
        <span className="w-1.5 h-1.5 bg-brand-blue rounded-full animate-pulse" />
        <span className="w-1.5 h-1.5 bg-brand-blue rounded-full animate-pulse [animation-delay:150ms]" />
        <span className="w-1.5 h-1.5 bg-brand-blue rounded-full animate-pulse [animation-delay:300ms]" />
      </div>
      <span className="font-mono text-xs text-text-muted ml-2">Thinking...</span>
    </div>
  );
}
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| useContext + useReducer | Zustand store | Already adopted | All 3 existing stores use Zustand pattern |
| CSS Modules or styled-components | Tailwind v4 CSS-first | Already adopted | Theme tokens in globals.css @theme block |
| Separate API utils | Inline fetch in stores | Already adopted | Keep consistent -- no separate API layer for chat |

**No deprecated patterns to worry about:** The project already uses current React 19, Zustand 5, Tailwind 4, and Next.js 16. All patterns are current.

## Open Questions

1. **Should chat history persist across page reloads?**
   - What we know: Backend persists all messages in `chat_messages` table. There is no `GET /api/chat/history` endpoint currently.
   - What's unclear: Whether to add a history-loading endpoint or start fresh each session.
   - Recommendation: Start fresh each session for Phase 9. The session-only approach is simpler and consistent with the "just opened the terminal" feel. A history endpoint can be added later if desired.

2. **Should the collapsed state reallocate grid columns?**
   - What we know: The grid is 12 columns. Chat currently takes 3. Reallocating would mean changing chart from 6 to 9 cols when collapsed.
   - What's unclear: Whether the collapsible behavior means "hide content but keep space" or "reclaim space."
   - Recommendation: Keep the col-span-3 allocation but show a minimal collapsed view within it (just a toggle button). This avoids dynamic grid restructuring which could cause layout shift and chart resize flicker. The 3-column space when collapsed shows a clean, narrow sidebar strip.

## Sources

### Primary (HIGH confidence)
- Backend chat router: `/Users/ed/projects/finally/backend/app/llm/router.py` -- POST /api/chat endpoint definition
- Backend chat models: `/Users/ed/projects/finally/backend/app/llm/models.py` -- ChatRequest, ChatResponse, TradeResult, WatchlistResult schemas
- Backend chat service: `/Users/ed/projects/finally/backend/app/llm/service.py` -- Full orchestration flow
- Backend chat tests: `/Users/ed/projects/finally/backend/tests/llm/test_chat_routes.py` -- Verified response shapes and edge cases
- Frontend page layout: `/Users/ed/projects/finally/frontend/src/app/page.tsx` -- Grid structure, ChatPanel placement
- Frontend stores: `/Users/ed/projects/finally/frontend/src/stores/*.ts` -- Zustand patterns (portfolio, price, watchlist)
- Frontend theme: `/Users/ed/projects/finally/frontend/src/app/globals.css` -- All color tokens, animations
- Existing panels: `/Users/ed/projects/finally/frontend/src/components/panels/*.tsx` -- Component structure patterns

### Secondary (MEDIUM confidence)
- Chat mock responses: `/Users/ed/projects/finally/backend/app/llm/mock.py` -- Test response fixtures for LLM_MOCK mode

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- No new packages, all existing patterns verified from source
- Architecture: HIGH -- Backend API fully implemented and tested, frontend patterns thoroughly documented
- Pitfalls: HIGH -- Common React chat UI issues, verified against actual codebase constraints
- API contract: HIGH -- Verified from Pydantic models, route code, and integration tests

**Research date:** 2026-02-11
**Valid until:** 2026-03-11 (stable -- no external dependencies or fast-moving APIs)
