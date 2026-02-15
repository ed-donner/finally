# Fly.io Deployment Plan (Demo / Ephemeral DB)

Date: 2026-02-13

This plan deploys FinAlly to Fly.io using the existing `Dockerfile` and **does not persist database state**. If a machine is replaced/restarted, SQLite data may be lost. This is acceptable for demo use.

## 1. Deployment model and assumptions

- App runs as a single containerized FastAPI service.
- Container listens on port `8003`.
- SQLite is stored at `/app/db/finally.db` inside the container filesystem.
- No Fly volume is attached.
- Run **one machine only** to avoid inconsistent demo state.

## 2. Prerequisites

- Fly account and billing enabled.
- `flyctl` installed locally.
- Repository available locally with this project root as working directory.
- Required runtime secrets prepared (at minimum your market API key if using live data).

## 3. One-time Fly setup

```bash
# 1) authenticate
fly auth login

# 2) choose a unique app name (example: finally-demo-<yourname>)
# this creates fly.toml without deploying
fly launch --no-deploy --name finally-demo-<yourname> --region ord
```

Notes:
- `--region ord` is an example; choose your preferred region.
- If `fly launch` asks to create Postgres/Redis/etc., answer `no` for this demo.

## 4. Configure `fly.toml`

After `fly launch --no-deploy`, edit `fly.toml` to match this baseline:

```toml
app = "finally-demo-<yourname>"
primary_region = "ord"

[build]
  dockerfile = "Dockerfile"

[env]
  PORT = "8003"
  APP_MODULE = "app.main:app"
  MASSIVE_POLL_INTERVAL_SECONDS = "0.5"

[http_service]
  internal_port = 8003
  force_https = true
  auto_stop_machines = "stop"
  auto_start_machines = true
  min_machines_running = 0
  processes = ["app"]

[[vm]]
  cpu_kind = "shared"
  cpus = 1
  memory_mb = 1024
```

Important:
- Do **not** add `[[mounts]]` for this demo plan.
- Keep `internal_port = 8003` to match the container.

## 5. Set secrets

Set any sensitive env vars through Fly secrets, not in `fly.toml`.

```bash
# Example: Massive API key
fly secrets set MASSIVE_API_KEY=your_real_key

# Optional: add others your backend expects
# fly secrets set SOME_KEY=some_value
```

To inspect non-sensitive runtime config:

```bash
fly config show
```

## 6. Deploy

```bash
# deploy current repo state
fly deploy
```

Recommended for this demo:

```bash
# force single instance
fly scale count 1
```

## 7. Verify after deploy

```bash
# status and machine health
fly status
fly machine list

# app logs
fly logs

# health endpoint (replace with your actual app hostname)
curl -fsS https://finally-demo-<yourname>.fly.dev/api/health
```

Expected health response should indicate app is up (e.g., JSON with status ok).

## 8. Release/update workflow

For each code update:

```bash
git push  # optional, if you track source remotely
fly deploy
fly logs
```

## 9. Rollback workflow

If a bad release is deployed:

```bash
# list recent releases
fly releases

# rollback to previous version
fly releases rollback <release-id>
```

## 10. Known demo tradeoffs

- Database is ephemeral: data can reset on restart/replacement.
- With a single machine, brief downtime can occur during deploys.
- App can cold-start if machine is stopped (`min_machines_running = 0`).

## 11. Hardening path for later (non-demo)

When you want durability:

1. Add a Fly volume mounted at `/app/db`.
2. Keep machine count at 1 while using SQLite.
3. For horizontal scaling, migrate DB to Postgres.

## 12. Security checklist

- Never expose environment secrets in any API response.
- Keep debug/admin endpoints disabled in production.
- Use Fly secrets for all credentials.
- Validate CORS/origin policy before sharing the public URL.
