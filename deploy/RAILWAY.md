# Deploying FinAlly to Railway

Railway builds the repo's `Dockerfile` (configured by `railway.toml`) and gives you a public
`https://<name>.up.railway.app` link. Setup takes about 10 minutes and needs a Railway account.

## What the repo already handles

| Railway requirement | How FinAlly meets it |
|---|---|
| App must listen on the injected `$PORT` | The `Dockerfile` CMD binds `0.0.0.0:${PORT:-8000}` |
| No `VOLUME` instruction in Dockerfiles | Removed; local Docker still uses named volumes |
| Health check before routing traffic | `railway.toml` sets `healthcheckPath = "/api/health"` |
| Builder | `railway.toml` selects the Dockerfile builder |

## Steps

1. **Get the code on GitHub.** Railway deploys from your repo
   (`Paul-Orlando/multi_agent_trading_app`). Merge the feature branch into `main`, or pick the
   branch under the service's *Settings, Source*.
2. **Create the project.** On railway.com: *New Project, Deploy from GitHub repo*, choose the repo.
   Railway finds `railway.toml` and starts the first build (a few minutes).
3. **Add variables** (service *Variables* tab):

   | Variable | Value | Why |
   |---|---|---|
   | `OPENROUTER_API_KEY` | your `sk-or-...` key | AI chat |
   | `RAILWAY_RUN_UID` | `0` | **Required.** The image runs as a non-root user, and Railway volumes are root-owned; without this the database cannot be written ([docs](https://docs.railway.com/reference/volumes)) |
   | `RATE_LIMIT_CHAT_PER_HOUR` | `15` (default) | optional; max `POST /api/chat` per IP per hour |
   | `RATE_LIMIT_TRADES_PER_HOUR` | `20` (default) | optional; max trades per IP per hour |
   | `LLM_MOCK` | `false` (or `true` to run without the model) | optional |
   | `MASSIVE_API_KEY` | optional | real market data instead of the simulator |

   Do **not** set `PORT`; Railway injects it.
4. **Attach a volume for the database.** Service, *Settings, Volumes* (or the command palette,
   Ctrl/Cmd+K, then "Volume"), *Add volume*, mount path **`/app/db`**. Without it the database is
   wiped on every deploy and restart.
5. **Generate the public link.** Service, *Settings, Networking, Public Networking, Generate
   Domain*. If asked for a port, use the one Railway shows (it is `$PORT`).
6. **Check it.** Open `https://<your-domain>/api/health` (expect `{"status":"ok"}`), then the
   root URL for the app.

Changing a variable or attaching the volume triggers a redeploy; watch *Deployments, View logs*.

## Read this before sharing the link

- **There is no login.** Anyone with the URL can trade in your portfolio and use the AI chat,
  which **spends your OpenRouter credits**. Keep the link private, use the two protection layers
  below, or set `LLM_MOCK=true`. Basic-auth protection is a small addition if you want it.
- **Chat model:** `openai/gpt-oss-120b` through OpenRouter, pinned to the Cerebras provider.

## Abuse protection: two layers

1. **Layer 1, code (per IP, in memory).** The app answers `429 Too Many Requests` with a
   `Retry-After` header once an IP exceeds:
   - **15** `POST /api/chat` requests per hour (`RATE_LIMIT_CHAT_PER_HOUR`)
   - **20** trades per hour (`RATE_LIMIT_TRADES_PER_HOUR`), shared by `/api/trade` and
     `/api/portfolio/trade`

   Counters live in process memory: they reset on redeploy or restart, and they assume the single
   replica above. The IP is the last `X-Forwarded-For` entry (the one Railway's proxy added).
2. **Layer 2, provider (hard spend cap).** Code limits can be bypassed or misconfigured, so also
   cap the money itself. In the OpenRouter web UI: *Settings, Keys* (openrouter.ai/settings/keys),
   open the key used as `OPENROUTER_API_KEY`, set a **Credit limit**, and save. Use a dedicated key
   for this deployment. Once the limit is reached OpenRouter rejects requests, so chat fails
   ("AuthenticationError"/402-style errors) instead of running up a bill. Keep the account's
   credit balance small as well.

Layer 1 stops casual abuse and keeps normal use smooth; layer 2 bounds the worst case.
- **Run exactly one replica.** The database is a single SQLite file and prices come from an
  in-process simulator, so scaling out would give each copy its own state.
- **Simulated prices** are the default (no Massive key). Trading uses virtual money.
- Live prices use Server-Sent Events over one long-lived connection. That is expected to work
  through Railway's proxy, but confirm after the first deploy: the header dot should turn green.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "Application failed to respond" / 502 | The app is not on `$PORT`, or the health check failed; read the deploy logs |
| Deploy fails with a `VOLUME` error | A `VOLUME` line crept back into the Dockerfile; remove it |
| Logs show `unable to open database file` / permission denied | `RAILWAY_RUN_UID=0` is missing, or the volume is not mounted at `/app/db` |
| Chat replies "AuthenticationError" | `OPENROUTER_API_KEY` is missing or invalid in Railway variables |
| Data resets after every deploy | The volume is not attached, or is mounted at a different path |
