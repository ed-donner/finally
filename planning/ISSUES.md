# FinAlly — Issue Tracker

Maintained mainly by `integration-tester`; any team member may add issues. Owners update status when fixed.

Format:

```
## ISSUE-<n>: <short title>
- Owner: <role name>
- Status: open | fixed | wontfix
- Found by: <name>, <how — E2E test / unit test / review>
- Repro: <steps or failing test>
- Expected / Actual: ...
- Fix notes: <owner fills in>
```

---

## ISSUE-1: `.gitignore` excluded `frontend/src/lib/`
- Owner: devops-engineer
- Status: fixed
- Found by: lead, review (`git check-ignore`)
- Expected / Actual: frontend lib sources tracked / Python-template rule `lib/` ignored them
- Fix notes: anchored to `/lib/` and `/lib64/`.

## ISSUE-2: Frontend build broken — `lib/format.ts`, `lib/portfolio.ts` missing
- Owner: frontend-engineer
- Status: fixed
- Found by: lead, `npx tsc --noEmit` / `npm run build`
- Fix notes: both modules implemented; tsc, lint, 36 unit tests and static export pass.

## ISSUE-3: E2E browser tests failed with `ERR_SSL_PROTOCOL_ERROR`
- Owner: integration-tester
- Status: fixed
- Found by: lead, E2E run (24/33 failed)
- Expected / Actual: Chromium loads `http://app:8000` / Chromium HSTS-preloads the `.app` TLD and forces HTTPS
- Fix notes: compose service renamed `app` → `finally`, `BASE_URL=http://finally:8000`.

## ISSUE-4: SSE resilience test could not simulate a drop; chat action regex too strict
- Owner: integration-tester
- Status: fixed
- Found by: lead, E2E run
- Fix notes: `context.setOffline` does not close an open EventSource in Chromium — test now drops the stream via `page.route` (close, then abort retries, then restore). Chat test accepts "Bought". Full suite: 33/33 passing.
