# Review Summary

Review scope: `git diff` against the last commit on `main`.

## Findings

### Medium
- **README no longer describes Docker quick start or required port, but still references a “Single Docker container” architecture.** This creates ambiguity for new users about the supported entrypoint. If Docker is still the primary/official path, the README should keep the `docker build/run` path (or explicitly deprecate it). If scripts are now primary, the architecture section should not assert a single container as the default path. (`README.md`)

### Low
- **Test instructions now imply local dev without Docker but don’t mention prerequisites.** `uv`, `npm`, and Docker (for E2E) are implied but not called out in a prerequisites section, which may lead to failed setup for new users. (`README.md`)
- **PLAN.md says watchlist ticks are ordered by `added_at` but doesn’t mention tie-breaking.** If `added_at` is identical for multiple inserts (e.g., bulk insert), order could be nondeterministic unless `ORDER BY added_at, ticker` or similar is used. (`planning/PLAN.md`)

## Notes
- `.claude/settings.json` only adds a plugin enable flag; no functional impact in app code.
- `.claude/skills/cerebras/SKILL.md` is a rename and whitespace cleanup; no behavioral impact.
- `planning/PLAN.md` adds useful clarification about LLM parse errors returning 200 with a friendly message; ensure backend actually implements this behavior.

## Open Questions
- Is Docker still a supported/first-class run path? If yes, README should keep or link to it explicitly.
- Should the “Single Docker container” statement be revised if scripts run the frontend/backend separately?

## Test Gaps
- No tests updated. If behavior about malformed LLM JSON is intended, consider adding a backend test that asserts the 200 response and error message.
