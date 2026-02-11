# Phase 5: LLM Chat Integration - Research

**Researched:** 2026-02-11
**Domain:** LLM integration via LiteLLM + OpenRouter, structured output parsing, trade auto-execution
**Confidence:** MEDIUM-HIGH

## Summary

This phase wires an AI chat assistant into the existing FinAlly backend. The user sends a natural language message, the backend builds a prompt with full portfolio context, calls gpt-oss-120b via LiteLLM/OpenRouter (routed through Cerebras for speed), parses a structured JSON response containing a message plus optional trades and watchlist changes, auto-executes those actions through the existing portfolio and watchlist services, and returns everything to the frontend.

The main technical challenge is getting structured outputs reliably through the LiteLLM-to-OpenRouter pipeline. LiteLLM's `supports_response_schema()` returns `False` for OpenRouter models, so the `response_format` parameter must be passed via `extra_body` to bypass this check. Additionally, there are reports of gpt-oss-120b ignoring `json_schema` strict mode on some providers, so a defensive parsing strategy with fallback is essential. The recommended approach is: use `extra_body` with `json_schema` type for structured output, instruct JSON format in the system prompt as a belt-and-suspenders measure, and validate with Pydantic's `model_validate_json()`.

The existing codebase provides clean service functions (`execute_trade`, `add_ticker`, `remove_ticker`) that the chat service can call directly. The chat_messages table is already defined in the schema. The `app/llm/` directory exists but is empty -- ready for this phase's code.

**Primary recommendation:** Use LiteLLM `acompletion` with `extra_body` for structured outputs, Pydantic models for request/response schemas, and a service-layer pattern matching the existing portfolio/watchlist architecture.

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| litellm | >=1.81 | Unified LLM API client | Handles OpenRouter routing, async support, model name normalization |
| pydantic | (already installed via FastAPI) | Structured output schema + validation | Native FastAPI integration, `model_validate_json()` for LLM response parsing |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| json (stdlib) | - | JSON parsing fallback | When Pydantic validation fails on malformed LLM output |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| litellm | Direct OpenRouter HTTP calls via httpx | More control but lose model name normalization, retry logic, token counting |
| litellm | instructor library | Adds automatic retries on schema violation but adds dependency; LiteLLM + Pydantic is sufficient here |
| response_format json_schema | Tool/function calling | More reliable structured output but more complex prompt engineering; json_schema with fallback parsing is simpler |

**Installation:**
```bash
cd backend && uv add litellm
```

**Note:** litellm has many transitive dependencies. The `uv add` will handle resolution. No other new packages needed -- pydantic, fastapi, aiosqlite are already in the project.

## Architecture Patterns

### Recommended Module Structure
```
backend/app/llm/
    __init__.py          # Public API exports
    models.py            # Pydantic schemas for LLM request/response
    service.py           # Core chat logic: build prompt, call LLM, execute actions
    prompt.py            # System prompt template and context builder
    mock.py              # Deterministic mock responses for LLM_MOCK=true
    router.py            # FastAPI router factory for POST /api/chat
```

### Pattern 1: Service Layer with Dependency Injection (Closure Factory)

**What:** Follow the existing pattern from `create_portfolio_router` and `create_watchlist_router` -- a factory function that takes dependencies (db, price_cache, market_source) and returns an APIRouter with endpoints that close over those dependencies.

**When to use:** Always -- this is the established pattern in the codebase.

**Example:**
```python
# router.py
def create_chat_router(
    db: aiosqlite.Connection,
    price_cache: PriceCache,
    market_source: MarketDataSource,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["chat"])

    @router.post("/chat")
    async def post_chat(request: ChatRequest) -> ChatResponse:
        result = await process_chat_message(
            db=db,
            price_cache=price_cache,
            market_source=market_source,
            user_message=request.message,
        )
        return result

    return router
```

### Pattern 2: LiteLLM acompletion with extra_body for Structured Output

**What:** Use LiteLLM's async `acompletion` function with `extra_body` to pass `response_format` directly to OpenRouter, bypassing LiteLLM's provider capability check that incorrectly returns False for OpenRouter.

**When to use:** Every LLM call through OpenRouter with structured output.

**Example:**
```python
# Source: LiteLLM GitHub Discussion #11652, OpenRouter docs
import json
import litellm

schema = ChatLLMResponse.model_json_schema()

response = await litellm.acompletion(
    model="openrouter/openai/gpt-oss-120b",
    messages=messages,
    extra_body={
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "chat_response",
                "strict": True,
                "schema": schema,
            },
        },
        "provider": {
            "order": ["Cerebras"],
            "allow_fallbacks": True,
        },
    },
)

content = response.choices[0].message.content
parsed = ChatLLMResponse.model_validate_json(content)
```

### Pattern 3: Defensive JSON Parsing with Fallback

**What:** Since there are reports of gpt-oss-120b occasionally ignoring strict JSON schema mode, always validate with Pydantic and have a fallback that creates a plain-message response if JSON parsing fails.

**When to use:** Every LLM response parse.

**Example:**
```python
from pydantic import ValidationError

def parse_llm_response(content: str) -> ChatLLMResponse:
    """Parse LLM response with fallback for malformed JSON."""
    try:
        return ChatLLMResponse.model_validate_json(content)
    except (ValidationError, json.JSONDecodeError):
        # Fallback: treat entire content as the message, no actions
        return ChatLLMResponse(message=content, trades=[], watchlist_changes=[])
```

### Pattern 4: Action Execution with Error Collection

**What:** Execute each trade/watchlist action from the LLM response independently, collecting successes and failures rather than failing the entire request on one bad action.

**When to use:** Processing the trades and watchlist_changes arrays from the LLM response.

**Example:**
```python
trade_results = []
for trade in parsed.trades:
    try:
        result = await execute_trade(db, price_cache, trade.ticker, trade.side, trade.quantity)
        trade_results.append({"status": "executed", **result})
    except ValueError as e:
        trade_results.append({"status": "failed", "ticker": trade.ticker, "error": str(e)})
```

### Anti-Patterns to Avoid
- **Passing Pydantic model directly to response_format:** LiteLLM's internal conversion from Pydantic to JSON schema is unreliable. Always use `model_json_schema()` explicitly and pass via `extra_body`.
- **Assuming structured output always works:** gpt-oss-120b may return free-text. Always have a fallback parser.
- **Raising HTTPException from the chat service:** The watchlist service raises HTTPException for 409/404 -- the chat service should catch those and report them as action failures, not crash the request.
- **Blocking the event loop with synchronous LLM calls:** Always use `acompletion`, never `completion`.
- **Storing raw LLM response in DB without validation:** Parse and validate first, store the validated+serialized version.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| LLM API calling | Custom HTTP client for OpenRouter | litellm.acompletion | Handles auth, retries, model routing, token counting |
| JSON schema from Pydantic | Manual schema dict construction | `MyModel.model_json_schema()` | Pydantic generates correct JSON Schema automatically |
| Response validation | Manual dict key checking | `MyModel.model_validate_json(content)` | Full type validation, default values, error messages |
| Trade execution | New trade logic in chat service | `app.portfolio.service.execute_trade()` | Already built, tested, handles all edge cases |
| Watchlist management | New watchlist logic in chat service | `app.watchlist.service.add_ticker()` / `remove_ticker()` | Already built, tested, handles duplicates and missing tickers |
| Mock mode switching | Manual if/else in every call | Single factory function checking LLM_MOCK env var | Clean separation, testable |

**Key insight:** This phase is primarily an integration/orchestration layer. The hard parts (trade execution, watchlist management, price cache, database) are already built. The chat service's job is to assemble context, call the LLM, parse the response, and dispatch to existing services.

## Common Pitfalls

### Pitfall 1: LiteLLM Drops response_format for OpenRouter
**What goes wrong:** LiteLLM's `supports_response_schema()` returns False for OpenRouter, so if you pass `response_format` as a top-level parameter, LiteLLM silently drops it. The LLM returns free-text instead of JSON.
**Why it happens:** OpenRouter is not in LiteLLM's hardcoded list of providers supporting response_format.
**How to avoid:** Always pass response_format inside `extra_body`, which is forwarded verbatim to the provider API.
**Warning signs:** LLM returns conversational text instead of JSON.

### Pitfall 2: gpt-oss-120b Ignores Strict JSON Schema
**What goes wrong:** Even with response_format set correctly, the model occasionally returns non-conforming JSON or free text.
**Why it happens:** Known issue reported on Groq community forums and instructor GitHub. May be provider-dependent.
**How to avoid:** Always validate with Pydantic's `model_validate_json()`. Have a fallback that wraps raw text in a message-only response. Also reinforce JSON format requirements in the system prompt.
**Warning signs:** ValidationError or JSONDecodeError when parsing response content.

### Pitfall 3: HTTPException from Watchlist Service Crashes Chat Endpoint
**What goes wrong:** When the LLM asks to add a ticker that's already on the watchlist, `add_ticker` raises HTTPException(409). This propagates up and returns a 409 to the user instead of a friendly chat message.
**Why it happens:** The watchlist service was designed for direct API use, not internal service-to-service calls.
**How to avoid:** Catch both ValueError and HTTPException when executing LLM-initiated actions. Report them as action failures in the response, not as HTTP errors.
**Warning signs:** Chat endpoint returns 409 or 404 instead of 200 with error details.

### Pitfall 4: Missing Portfolio Context Makes LLM Responses Generic
**What goes wrong:** The AI gives vague responses because it doesn't know what the user owns or how much cash they have.
**Why it happens:** System prompt doesn't include actual portfolio data.
**How to avoid:** Build dynamic context by calling `get_portfolio()` and querying the watchlist with live prices. Include cash balance, each position with current price and P&L, total portfolio value, and available tickers.
**Warning signs:** AI says "I'd need to check your portfolio" or gives advice without referencing specific positions.

### Pitfall 5: Conversation History Grows Without Bound
**What goes wrong:** Including all historical messages in the LLM context exceeds the token limit or slows down responses.
**Why it happens:** No limit on how many messages are loaded from chat_messages table.
**How to avoid:** Load only the most recent N messages (e.g., last 20). The gpt-oss-120b model has 131K context, so this is generous, but still set a reasonable limit.
**Warning signs:** Increasing response latency over time, eventual context length errors.

### Pitfall 6: Race Condition Between Trade Execution and Portfolio Snapshot
**What goes wrong:** After executing an LLM-initiated trade, the portfolio snapshot records the pre-trade value because the snapshot task runs concurrently.
**Why it happens:** The portfolio router already calls `record_snapshot` after each trade. The chat service should do the same.
**How to avoid:** Call `record_snapshot(db, price_cache)` after successfully executing any trades from the LLM response, matching the pattern in `routes/portfolio.py`.
**Warning signs:** P&L chart doesn't reflect AI-executed trades immediately.

## Code Examples

### Pydantic Models for LLM Structured Output

```python
# Source: OpenRouter structured output docs + PLAN.md schema
from pydantic import BaseModel, Field


class TradeAction(BaseModel):
    """A trade the LLM wants to execute."""
    ticker: str
    side: str = Field(pattern=r"^(buy|sell)$")
    quantity: float = Field(gt=0)


class WatchlistAction(BaseModel):
    """A watchlist change the LLM wants to make."""
    ticker: str
    action: str = Field(pattern=r"^(add|remove)$")


class ChatLLMResponse(BaseModel):
    """Schema for the LLM's structured JSON response."""
    message: str
    trades: list[TradeAction] = []
    watchlist_changes: list[WatchlistAction] = []
```

### System Prompt Builder

```python
# Build dynamic context from live portfolio state
def build_system_prompt(portfolio: dict, watchlist_prices: list[dict]) -> str:
    positions_text = ""
    for p in portfolio["positions"]:
        positions_text += (
            f"  {p['ticker']}: {p['quantity']} shares, "
            f"avg cost ${p['avg_cost']:.2f}, "
            f"current ${p['current_price']:.2f}, "
            f"P&L ${p['unrealized_pnl']:.2f} ({p['unrealized_pnl_percent']:+.1f}%)\n"
        )

    watchlist_text = ""
    for w in watchlist_prices:
        watchlist_text += f"  {w['ticker']}: ${w['price']:.2f}\n"

    return f"""You are FinAlly, an AI trading assistant for a simulated portfolio.
You analyze positions, suggest trades, execute trades, and manage the watchlist.
Be concise and data-driven. Always respond with valid JSON.

Current portfolio:
  Cash: ${portfolio['cash_balance']:.2f}
  Total value: ${portfolio['total_value']:.2f}
  Positions:
{positions_text or '  (none)'}
Watchlist prices:
{watchlist_text or '  (none)'}

You MUST respond with JSON matching this exact schema:
{{
  "message": "your response text",
  "trades": [{{"ticker": "AAPL", "side": "buy", "quantity": 10}}],
  "watchlist_changes": [{{"ticker": "PYPL", "action": "add"}}]
}}
trades and watchlist_changes are optional arrays (use empty arrays if no actions needed)."""
```

### Mock LLM Service

```python
# mock.py - deterministic responses for testing
import json

MOCK_RESPONSES = {
    "default": {
        "message": "I can see your portfolio. You have $10,000 in cash. How can I help?",
        "trades": [],
        "watchlist_changes": [],
    },
    "buy": {
        "message": "Done! I've bought 5 shares of AAPL for you.",
        "trades": [{"ticker": "AAPL", "side": "buy", "quantity": 5}],
        "watchlist_changes": [],
    },
    "sell": {
        "message": "Done! I've sold 5 shares of AAPL for you.",
        "trades": [{"ticker": "AAPL", "side": "sell", "quantity": 5}],
        "watchlist_changes": [],
    },
    "watchlist": {
        "message": "I've added PYPL to your watchlist.",
        "trades": [],
        "watchlist_changes": [{"ticker": "PYPL", "action": "add"}],
    },
}


def get_mock_response(user_message: str) -> str:
    """Return a deterministic mock response based on keyword matching."""
    lower = user_message.lower()
    if "buy" in lower:
        return json.dumps(MOCK_RESPONSES["buy"])
    elif "sell" in lower:
        return json.dumps(MOCK_RESPONSES["sell"])
    elif "add" in lower or "watch" in lower:
        return json.dumps(MOCK_RESPONSES["watchlist"])
    return json.dumps(MOCK_RESPONSES["default"])
```

### Chat Message Persistence

```python
# Store user message and AI response with actions
from datetime import datetime, timezone
from uuid import uuid4

async def save_chat_message(
    db, role: str, content: str, actions: dict | None = None
) -> None:
    """Save a chat message to the database."""
    now = datetime.now(timezone.utc).isoformat()
    actions_json = json.dumps(actions) if actions else None
    await db.execute(
        "INSERT INTO chat_messages (id, user_id, role, content, actions, created_at) "
        "VALUES (?, 'default', ?, ?, ?, ?)",
        (str(uuid4()), role, content, actions_json, now),
    )
    await db.commit()


async def load_chat_history(db, limit: int = 20) -> list[dict]:
    """Load recent chat messages for LLM context."""
    rows = await db.execute_fetchall(
        "SELECT role, content FROM chat_messages WHERE user_id = 'default' "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    # Reverse to chronological order
    return [{"role": row[0], "content": row[1]} for row in reversed(rows)]
```

### Full Chat Processing Flow

```python
async def process_chat_message(
    db, price_cache, market_source, user_message: str
) -> ChatResponse:
    """Full chat flow: context -> LLM -> parse -> execute -> persist -> respond."""
    # 1. Build portfolio context
    portfolio = await get_portfolio(db, price_cache)
    watchlist_rows = await get_watchlist(db)
    watchlist_prices = []
    for row in watchlist_rows:
        ticker = row["ticker"]
        price = price_cache.get_price(ticker)
        if price is not None:
            watchlist_prices.append({"ticker": ticker, "price": price})

    # 2. Load conversation history
    history = await load_chat_history(db)

    # 3. Build messages array
    system_prompt = build_system_prompt(portfolio, watchlist_prices)
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    # 4. Call LLM (or mock)
    if os.environ.get("LLM_MOCK", "").lower() == "true":
        content = get_mock_response(user_message)
    else:
        response = await litellm.acompletion(
            model="openrouter/openai/gpt-oss-120b",
            messages=messages,
            extra_body={
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "chat_response",
                        "strict": True,
                        "schema": ChatLLMResponse.model_json_schema(),
                    },
                },
                "provider": {
                    "order": ["Cerebras"],
                    "allow_fallbacks": True,
                },
            },
        )
        content = response.choices[0].message.content

    # 5. Parse response
    parsed = parse_llm_response(content)

    # 6. Execute trades
    trade_results = []
    for trade in parsed.trades:
        try:
            result = await execute_trade(
                db, price_cache, trade.ticker, trade.side, trade.quantity
            )
            trade_results.append({"status": "executed", **result})
        except (ValueError, Exception) as e:
            trade_results.append({
                "status": "failed",
                "ticker": trade.ticker,
                "side": trade.side,
                "error": str(e),
            })

    # 7. Execute watchlist changes
    watchlist_results = []
    for change in parsed.watchlist_changes:
        try:
            if change.action == "add":
                await add_ticker(db, change.ticker)
                await market_source.add_ticker(change.ticker.upper())
            else:
                await remove_ticker(db, change.ticker)
                await market_source.remove_ticker(change.ticker.upper())
            watchlist_results.append({"status": "applied", "ticker": change.ticker, "action": change.action})
        except (HTTPException, Exception) as e:
            error_msg = e.detail if hasattr(e, "detail") else str(e)
            watchlist_results.append({
                "status": "failed",
                "ticker": change.ticker,
                "action": change.action,
                "error": error_msg,
            })

    # 8. Record portfolio snapshot if any trades executed
    if any(r["status"] == "executed" for r in trade_results):
        await record_snapshot(db, price_cache)

    # 9. Persist messages
    await save_chat_message(db, "user", user_message)
    actions = {
        "trades": trade_results,
        "watchlist_changes": watchlist_results,
    }
    await save_chat_message(db, "assistant", parsed.message, actions)

    # 10. Return response
    return ChatResponse(
        message=parsed.message,
        trades=trade_results,
        watchlist_changes=watchlist_results,
    )
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| litellm.completion (sync) | litellm.acompletion (async) | Stable since litellm 1.0+ | Must use async in FastAPI context |
| response_format as top-level param | extra_body for OpenRouter | Ongoing LiteLLM issue since May 2025 | Required workaround for structured output |
| Pydantic model passed directly to response_format | model_json_schema() via extra_body | LiteLLM issue #6848 | More reliable schema conversion |

**Deprecated/outdated:**
- Passing Pydantic BaseModel directly to `response_format` parameter: unreliable schema conversion in LiteLLM. Use `model_json_schema()` instead.
- Using `litellm.completion()` (synchronous) in async FastAPI: blocks the event loop.

## Open Questions

1. **gpt-oss-120b Strict Mode Reliability**
   - What we know: Cerebras docs say strict mode uses constrained decoding. Groq community reports it being ignored. OpenRouter routing may affect behavior.
   - What's unclear: Whether Cerebras-routed requests on OpenRouter reliably enforce strict JSON schema.
   - Recommendation: Implement defensive parsing with fallback. Also reinforce JSON format in the system prompt as belt-and-suspenders. This handles both strict and non-strict scenarios correctly.

2. **LiteLLM OpenRouter response_format Fix Timeline**
   - What we know: Issues #10465 and #13438 are open requesting native support.
   - What's unclear: Whether a recent LiteLLM version (1.81+) has fixed this.
   - Recommendation: Use the `extra_body` workaround regardless -- it's stable, well-documented, and future-proof.

3. **Token Counting for Context Window Management**
   - What we know: gpt-oss-120b has 131K context window. 20 messages of history is well within limits.
   - What's unclear: Exact token usage of the system prompt + portfolio context.
   - Recommendation: Start with a 20-message history limit. If issues arise, reduce or add token counting via litellm's `token_counter()`.

## Sources

### Primary (HIGH confidence)
- Existing codebase: `backend/app/portfolio/service.py`, `backend/app/watchlist/service.py`, `backend/app/db/schema.py` -- verified exact function signatures and patterns
- [LiteLLM GitHub Discussion #11652](https://github.com/BerriAI/litellm/discussions/11652) -- extra_body workaround for OpenRouter structured output (FIXED)
- [OpenRouter Structured Outputs docs](https://openrouter.ai/docs/guides/features/structured-outputs) -- response_format json_schema format
- [OpenRouter Provider Routing docs](https://openrouter.ai/docs/guides/routing/provider-selection) -- provider order for Cerebras
- [gpt-oss-120b on OpenRouter](https://openrouter.ai/openai/gpt-oss-120b) -- model capabilities, 131K context, pricing

### Secondary (MEDIUM confidence)
- [LiteLLM Structured Outputs docs](https://docs.litellm.ai/docs/completion/json_mode) -- general response_format usage
- [LiteLLM OpenRouter provider docs](https://docs.litellm.ai/docs/providers/openrouter) -- model name format: `openrouter/openai/gpt-oss-120b`
- [LiteLLM Mock Completion docs](https://docs.litellm.ai/docs/completion/mock_requests) -- mock_response parameter for testing
- [Cerebras Structured Outputs docs](https://inference-docs.cerebras.ai/capabilities/structured-outputs) -- confirms strict mode support
- [litellm on PyPI](https://pypi.org/project/litellm/) -- latest version 1.81.9 as of 2026-02-11

### Tertiary (LOW confidence)
- [Groq community: structured outputs ignored by gpt-oss-120b](https://community.groq.com/t/structured-outputs-ignored-by-openai-gpt-oss-120b/687) -- reports of strict mode being ignored (may be Groq-specific, not Cerebras)
- [instructor issue #1766](https://github.com/567-labs/instructor/issues/1766) -- Cerebras gpt-oss-120b structured output issues

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - litellm is the established library for multi-provider LLM calls; version verified on PyPI today
- Architecture: HIGH - follows exact patterns already established in the codebase (closure-based router factories, service layer)
- LLM integration: MEDIUM - extra_body workaround is well-documented and verified by community, but gpt-oss-120b strict mode reliability on Cerebras via OpenRouter has conflicting reports
- Pitfalls: HIGH - identified from actual GitHub issues, community reports, and codebase analysis (HTTPException from watchlist service, response_format being dropped)

**Research date:** 2026-02-11
**Valid until:** 2026-03-11 (30 days -- LiteLLM releases frequently, OpenRouter model support evolves)
