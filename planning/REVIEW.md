# Review: changes since `14550e1` (`Ready for Teams`)

## Findings

1. **[P1] Massive polling reads a nonexistent SDK timestamp attribute, so real-data snapshots are discarded.** [`massive_client.py:103`](../backend/app/market/massive_client.py#L103) reads `snap.last_trade.timestamp`, catches the resulting `AttributeError`, and skips each snapshot. The Massive SDK field is `sip_timestamp` in nanoseconds (documented in [`MASSIVE_API.md`](MASSIVE_API.md)); convert it to seconds. The implementation also assumes `last_trade` and its price are always present, while the new reference documents fallback fields. Add model-backed test coverage so permissive mocks do not conceal incorrect SDK attributes.

2. **[P1] README quick start cannot run from this checkout.** [`README.md:31-34`](../README.md#L31) references `.env.example` and `scripts/start.sh`/`scripts/stop.sh` (plus the Windows script), but neither `scripts/` nor `.env.example` exists. Add the referenced files or revise the quick start to match runnable commands and current repo contents.

3. **[P2] The plan and new interface spec promise session-change fields that the market code does not provide.** [`PLAN.md:183-189`](PLAN.md#L183) and [`MARKET_INTERFACE.md`](MARKET_INTERFACE.md) say `PriceUpdate`/`PriceCache` provide `session_open` and `session_change_percent`; [`models.py`](../backend/app/market/models.py) and [`cache.py`](../backend/app/market/cache.py) do not. The SSE serialization therefore cannot produce the documented contract, and older docs such as `MARKET_DATA_SUMMARY.md` and `backend/CLAUDE.md` also describe the previous shape. Align the specs with implementation or implement the fields and update related docs/tests.

4. **[P2] The plugin's Stop hook can overwrite a project's existing review document.** [`hooks.json:8`](../independent-reviewer/hooks/hooks.json#L8) runs in any consuming repository and hardcodes `planning/REVIEW.md`; an existing file at that path may be replaced whenever the session stops. Scope the hook to an explicit opt-in/project context or use a collision-safe output path and preserve existing content.

## Review scope and checks

Reviewed tracked changes against `HEAD` (`14550e1`) and untracked additions under `.claude-plugin/`, `.claude/`, `independent-reviewer/`, and `planning/`. Checked the referenced market-data implementation and repository paths. `git diff --check HEAD` reported no whitespace errors. No tests were run.
