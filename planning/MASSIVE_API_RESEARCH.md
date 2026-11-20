# Massive API Research (formerly Polygon.io)

**Status:** Research note, 2026-11-19. Input to PLAN.md §5–§6 and spike S1 (PLAN.md §13.5).
**Scope:** What the Massive stocks REST API offers, which parts FinAlly needs, what it costs in access terms, and what is still unverified.
**Not a contract.** The HTTP contract for FinAlly's own endpoints lives in `API.md`. This document only feeds the Massive client.

---

## 1. Summary

- Massive is the rebranded Polygon.io. Existing keys and endpoints still work. The base host is **not confirmed** in this research (see §8, open item E).
- FinAlly needs one thing from Massive: a current price and a previous-session close for each watched or held ticker, refreshed about every 15 s. The **full-market snapshot endpoint** does this in one call per poll, and its response already contains `prevDay`. That removes the per-ticker baseline calls PLAN.md §6 planned for.
- **Access, not just calls, is the real constraint.** The snapshot endpoint is not available on the free Basic plan. Massive mode therefore needs a paid stocks plan. On a paid stocks plan, the documented rate limit does not apply. The `MASSIVE_CALLS_PER_MINUTE=5` default in PLAN.md §5 is only correct for the free tier, which cannot run Massive mode anyway.
- **Paid tiers that include the snapshot are 15-minute delayed** (Starter and Developer). Only Advanced is real-time, at $199/month, as confirmed on the pricing page. Prices shown in Massive mode will lag real time by 15 minutes on the cheaper tiers. This affects the demo story and must be a stated product decision.

---

## 2. Background

- The Polygon.io → Massive rebrand is confirmed by multiple sources ([Massive docs index](https://massive.com/docs/llms.txt); third-party summaries such as [apicostcalc.com](https://apicostcalc.com/blog/polygon-massive-rebrand-api-pricing.html)).
- The docs are at `massive.com/docs`. The machine-readable index is `https://massive.com/docs/llms.txt`.
- Stock REST endpoints relevant here (all under `/v2` or `/v3`):

| Purpose | Endpoint | Used by FinAlly? |
|---|---|---|
| Full-market (or filtered) snapshot | `GET /v2/snapshot/locale/us/markets/stocks/tickers` | **Yes**, the price poll |
| Previous-day aggregate | `GET /v2/aggs/ticker/{stocksTicker}/prev` | Only as a fallback (see §5) |
| Unified snapshot | `/v3/snapshot` (`ticker.any_of`) | No. Listed for reference. Its 250-ticker limit is the one quoted in the KB. |
| WebSocket aggregates / trades / quotes | `wss://…/stocks` | No. PLAN.md specifies REST polling. |

Sources: [Full Market Snapshot](https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot), [Previous Day Bar](https://massive.com/docs/rest/stocks/aggregates/previous-day-bar).

---

## 3. Plans and what each one can do

Confidence is marked per row. "Confirmed" means read on an official Massive page in this research. "Third-party" means a secondary source only.

| Plan | Price | Snapshot (full market) | Data recency | Prev-day bar | WebSocket | Notes |
|---|---|---|---|---|---|---|
| Basic (free) | $0 | **Not included** (confirmed) | Prev-day bar end-of-day | Included (confirmed) | — | Rate-limited to 5 req/min for stocks (confirmed; see §4). Cannot run snapshot-based Massive mode. |
| Starter | $29/mo (third-party) | Included (confirmed: "Starter and above") | **15-min delayed** (confirmed) | 15-min delayed (confirmed) | Included per the per-second aggregates page (conflicting third-party claim: not) | Price unconfirmed. Pricing table did not render. |
| Developer | $79/mo (third-party) | Included (confirmed) | **15-min delayed** (confirmed) | 15-min delayed (confirmed) | Included per docs; adds trades (third-party) | Price unconfirmed. |
| Advanced | **$199/mo** (confirmed on pricing page) | Included (confirmed) | **Real-time** (confirmed) | Real-time (confirmed) | Included (confirmed: "websockets") | Individual use only. |
| Business | Not researched | Included (confirmed) | Real-time (confirmed) | Real-time (confirmed) | — | Adds `fmv` field. Not needed. |

**Conclusion for FinAlly:**

- Massive mode works on Starter, Developer, Advanced, or Business. Starter and Developer give 15-minute-old prices.
- Advanced is the only real-time option found. At $199/month it is expensive for a course demo, which is why the simulator is the default.
- Prices for Starter and Developer should be checked on the live pricing page before any decision is made on them.

Sources: [Full Market Snapshot](https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot), [Massive pricing](https://massive.com/pricing), [Per-second aggregates](https://massive.com/docs/websocket/stocks/aggregates-per-second), [Snapshot ticker limit KB](https://massive.com/knowledge-base/article/what-is-the-max-number-of-tickers-i-can-pass-through-massives-snapshot).

---

## 4. Rate limits

Quoted from the official knowledge base, [What is the request limit for Massive's RESTful APIs](https://massive.com/knowledge-base/article/what-is-the-request-limit-for-massives-restful-apis):

> "Five requests a minute per asset class you have not subscribed to, and no limit on the ones you have."

Implications:

1. The limit is **per asset class**, not per account. A paid options plan does not lift the stocks limit, and a paid stocks plan does not lift options. FinAlly only uses stocks.
2. On a **subscribed** stocks plan (Starter and up), the documented REST limit is **none**. The limiter in PLAN.md §6 is still a correct safeguard against bugs and burst loops, but its default should not be 5/min.
3. On the free Basic plan the limit is 5/min. That is irrelevant to Massive mode, because the snapshot is not available there.
4. WebSocket connections are counted separately and are not requests. FinAlly does not use WebSocket.
5. Error responses:
   - `429`: body `{"status":"ERROR","error":"You've exceeded the maximum requests per minute, …"}`. PLAN.md §6 retry rule (keep last prices, warn, retry next interval) applies.
   - `403 NOT_AUTHORIZED` ("Your plan doesn't include this data timeframe…"): the requested data is more recent than the plan allows. Treat as a plan misconfiguration, not a transient failure. Log it clearly. It should count as a failed poll for the degraded rule (PLAN.md §6).

**Recommended spec change (for product owner approval):** Keep `MASSIVE_CALLS_PER_MINUTE` as a config value, but make its documented default match the paid-tier reality (for example, `0` meaning "no client-side cap") and keep a conservative client-side cap of about 12 calls/min as the default so that a bug cannot burn a quota. At the 15 s poll interval, FinAlly makes 4 snapshot calls per minute, so this cap never binds in normal operation.

---

## 5. Endpoints FinAlly will call

### 5.1 Full-market snapshot (the price poll)

`GET /v2/snapshot/locale/us/markets/stocks/tickers`

Query parameters (confirmed):

| Param | Type | Notes |
|---|---|---|
| `tickers` | comma-separated string | Case-sensitive. Empty means all tickers (~10,000+). FinAlly always passes an explicit list. |
| `include_otc` | boolean | Default `false`. FinAlly leaves it default. |

Maximum tickers per request: **not stated** on the full-market page. The KB's 250 limit applies to `/v3/snapshot`. PLAN.md §6 says to chunk at 250 regardless. Keep that: it is safe and the spike cannot prove a larger limit without testing.

Response (confirmed field names):

```json
{
  "count": 2,
  "status": "OK",
  "tickers": [
    {
      "ticker": "AAPL",
      "day":      { "o": 0, "h": 0, "l": 0, "c": 0, "v": 0, "vw": 0 },
      "prevDay":  { "o": 0, "h": 0, "l": 0, "c": 0, "v": 0, "vw": 0 },
      "min":      { "...": "per-minute aggregate" },
      "lastTrade": { "p": 0, "s": 0, "t": 0 },
      "lastQuote": { "P": 0, "p": 0, "t": 0 },
      "todaysChange": 0,
      "todaysChangePerc": 0,
      "updated": 0
    }
  ]
}
```

Field notes:

- `lastTrade` is returned only if the plan includes trades; `lastQuote` only if the plan includes quotes. Third-party sources say Starter has no `lastTrade` and Developer has no `lastQuote`. Verify in a live call before relying on either.
- `prevDay.c` is the previous session close. **This answers spike S1(c): yes, the snapshot includes the baseline.** PLAN.md §6 can drop the per-ticker `/prev` call for normal operation.
- `updated` is a timestamp. Its unit (often nanoseconds) must be confirmed against a live response before parsing. Do not assume milliseconds.
- `todaysChangePerc` is Massive's own day change. FinAlly computes its own `change_pct` from `prev_close` (PLAN.md §6). Do not use Massive's value, so both sources use the same definition.

**Mapping to `PriceUpdate` (proposed):**

| FinAlly field | Source |
|---|---|
| `price` | `lastTrade.p` when present; otherwise `day.c` (today's running close). Decision needed if both are absent: skip the ticker and keep its last price. |
| `timestamp` | `lastTrade.t` or `updated`, converted to UTC. Unit to confirm. |
| `prev_close` | `prevDay.c` |
| `previous_price` / `direction` | computed by the cache, not by Massive |

### 5.2 Previous-day aggregate (fallback only)

`GET /v2/aggs/ticker/{stocksTicker}/prev`

- Params: `stocksTicker` (required, case-sensitive), `adjusted` (default `true`; splits adjusted).
- Response: `ticker`, `adjusted`, `queryCount`, `request_id`, `resultsCount`, `status`, and `results[]` with `T, c, h, l, n, o, t, v, vw`. The close is `results[0].c`.
- Included on all stocks plans, but Basic is end-of-day only.
- **Unknown-ticker behavior: not stated** on the docs page. Spike S1(d) remains open. PLAN.md §6 treats both 404 and empty `results` as not-found, which covers either answer.

Use this only if `prevDay` is missing from the snapshot for a ticker (for example, a newly listed symbol). It goes through the limiter at lowest priority.

### 5.3 Ticker not found in the snapshot

The full-market page does not document a not-found row. The v3 snapshot documents unknown symbols as a row with `"error": "NOT_FOUND"`, not as a dropped row ([KB](https://massive.com/knowledge-base/article/what-is-the-max-number-of-tickers-i-can-pass-through-massives-snapshot)). The client must handle three cases the same way for `add_ticker`:

1. HTTP 404
2. HTTP 200 with zero matching `tickers[]`
3. HTTP 200 with a row carrying `"error"`

All three raise `UnknownTicker` (PLAN.md §6, §8). Confirm which one the full-market endpoint actually returns in the spike.

### 5.4 Authentication

- Key is passed as `apiKey` query parameter in the KB examples. The docs index says the key is read from the `MASSIVE_API_KEY` environment variable and created at `massive.com/dashboard/keys`.
- **Recommendation:** send the key in an `Authorization: Bearer <key>` header if the API accepts it (the spike must confirm). That keeps the key out of URLs, logs, and exception messages. If not supported, the client must scrub the `apiKey` query value from any logged URL.

---

## 6. Delays, reset window, and what "current price" means

- **Delay:** Starter and Developer data is 15 minutes old. Massive returns the data with no in-band flag, so FinAlly cannot tell from the response that it is delayed. The product must state this in its docs, and the header or heartbeat could report `source: "massive"` with a note.
- **Reset window:** the snapshot is cleared daily at 3:30 AM Eastern and repopulates from as early as 4:00 AM Eastern ([snapshot docs](https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot)). Between the reset and repopulation, `day` and `prevDay` can be empty or one session behind. The client must:
  - treat a missing `prevDay.c` as `prev_close = null` (not zero), and keep the previously stored value if one exists;
  - never record a snapshot value of zero (PLAN.md §7 restart rules).
- **Fill price:** a market order fills at the cache price (PLAN.md §8). In Massive mode on Starter or Developer, that is a 15-minute-old price. The spec must say so. It does not break the invariants, but a user comparing the fill to a live quote elsewhere will see a gap.

---

## 7. Operational plan for the Massive client

- One poll = one snapshot request per chunk of ≤250 tickers. For the default 10-ticker watchlist plus a few holdings, that is always one chunk.
- Poll interval: `MASSIVE_POLL_INTERVAL_SECONDS`, default 15 s (PLAN.md §5). With no effective rate cap on a paid plan, the interval can stay at 15 s; the auto-stretch rule in PLAN.md §6 only matters if the configured cap is set low.
- Priority: poll > baseline fallback (§5.2).
- Failure rules unchanged from PLAN.md §6: 429 and 5xx keep last prices and retry next interval; 3 consecutive failures → `degraded`; one success → `ok`.
- A `403 NOT_AUTHORIZED` should be logged as a configuration error with the plan name. It should not be retried silently.

---

## 8. Open items and spike results (update PLAN.md §13.5 when closed)

| Spike | Question | Result |
|---|---|---|
| S1(a) | Which plans serve the snapshot? Is free served? | **Resolved:** Starter and above. Basic (free) is not served. Massive mode requires a paid plan. Starter/Developer prices still need checking. |
| S1(b) | Per-request ticker limit on the full-market snapshot | **Open.** Not stated. Use 250 per chunk until a live call proves more. |
| S1(c) | Does the snapshot include `prevDay.c`? | **Resolved: yes.** Drop the per-ticker baseline call from the normal path. Still confirm live that `prevDay.c` is the prior session close around the 3:30 AM reset. |
| S1(d) | `/prev` response for an unknown symbol | **Open.** Handle 404 and empty `results` as not-found (already in PLAN.md). |
| S1(e) | Current base URL | **Open.** Not confirmed in research. Test `api.massive.com` and the legacy `api.polygon.io` with one live call, then pin the working host. |
| S1 (new) | Auth via `Authorization` header | **Open.** Check whether the header works. Prefer it over the query parameter. |
| S1 (new) | Unit of `updated` / `lastTrade.t` | **Open.** Confirm with a live response. |
| S1 (new) | Which of `lastTrade` / `lastQuote` is present on Starter and Developer | **Open.** Third-party sources conflict with each other. |
| Pricing | Starter and Developer prices and limits | **Open.** The pricing table did not render. Check `massive.com/pricing` by hand. |
| Product | Accept 15-minute delayed prices for Starter, or require Advanced for Massive mode? | **Decision for product owner.** |

**Spike procedure (one-off, with a real key, not in CI):** call the snapshot with a 3-ticker list and one bogus symbol, on the lowest paid tier available. Save the raw JSON in `planning/spikes/` (no key in it). That single capture settles S1(b)–(e) and the new items.

---

## 9. Sources

- [Massive docs index (llms.txt)](https://massive.com/docs/llms.txt)
- [Full Market Snapshot](https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot)
- [Previous Day Bar](https://massive.com/docs/rest/stocks/aggregates/previous-day-bar)
- [Snapshot ticker limit (KB)](https://massive.com/knowledge-base/article/what-is-the-max-number-of-tickers-i-can-pass-through-massives-snapshot)
- [Request limit for RESTful APIs (KB)](https://massive.com/knowledge-base/article/what-is-the-request-limit-for-massives-restful-apis)
- [Massive pricing](https://massive.com/pricing)
- [Per-second aggregates (WebSocket)](https://massive.com/docs/websocket/stocks/aggregates-per-second)
- Third-party, for conflicting claims only: [apicostcalc.com rebrand and pricing](https://apicostcalc.com/blog/polygon-massive-rebrand-api-pricing.html), [CostBench Polygon.io plans](https://costbench.com/software/financial-data-terminals/polygon-io/)

**Related:** [PLAN.md](PLAN.md) §5, §6, §13.5 · [API.md](API.md) · [DATA_INTERFACE_DESIGN.md](DATA_INTERFACE_DESIGN.md) · [SIMULATOR_RESEARCH.md](SIMULATOR_RESEARCH.md)

