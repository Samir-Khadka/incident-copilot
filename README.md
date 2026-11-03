# Incident Copilot

A small self-hosted tool that answers the two questions teams panic about:
**what changed?** and **can we trust our backups?**

Collectors pull events (GitHub commits/PRs, CI deploys, container restarts) into one
Postgres database. A timeline API shows everything in order with an "around the alert
time" filter. A nightly restore job proves the backups actually restore. A Telegram
bot answers "what changed in the last 2 hours?" in plain language.

## Roadmap

| Week | Scope |
|------|-------|
| **1 — done** | GitHub webhook collector, `events` table, timeline API (`GET /api/events`), generic ingest (`POST /api/events`), health check |
| 2 | CI + Docker events collectors, web timeline UI with time filter |
| 3 | Nightly restore job (restore latest Postgres dump into a temp container, run sanity checks, report to Telegram) |
| 4 | Telegram bot chat commands, demo GIF, one-command `docker-compose up` |

## Quickstart (Week 1)

```bash
# 1. Postgres for local dev
docker compose up -d db

# 2. Configure
cp .env.example .env
# set GITHUB_WEBHOOK_SECRET to a long random string

# 3. Run the API
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# 4. Check it
curl localhost:8000/health
curl "localhost:8000/api/events?limit=5"
```

## Receiving GitHub webhooks locally

GitHub can't reach `localhost`, so tunnel it with [smee.io](https://smee.io):

```bash
# new channel at https://smee.io -> copy the URL, then:
npx smee-client --url https://smee.io/<your-channel> --target http://localhost:8000/webhooks/github
```

In your repo: **Settings → Webhooks → Add webhook**
- Payload URL: `https://smee.io/<your-channel>`
- Content type: `application/json`
- Secret: the same value as `GITHUB_WEBHOOK_SECRET`
- Events: "Send me everything" for now (Week 1 handles `push` + `pull_request`; others are acknowledged and ignored)

## API reference

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/webhooks/github` | GitHub webhook receiver. Requires `X-Hub-Signature-256` HMAC-SHA256 (401/403 on missing/invalid). Handles `ping` → `pong`. Dedupes redeliveries via `X-GitHub-Delivery`. |
| `GET` | `/api/events` | Timeline. Filters: `since`, `until` (ISO datetimes), `source`, `type`, `repo`. Pagination: `limit` (1–500, default 50), `offset`. Newest first. |
| `POST` | `/api/events` | Generic ingest for future collectors. Body: `source*`, `type*`, `occurred_at`, `actor`, `repo`, `title`, `url`, `payload`. |
| `GET` | `/health` | Liveness + DB connectivity (`SELECT 1`). |

Example — "what changed in the last 2 hours":

```bash
curl "localhost:8000/api/events?since=$(date -u -d '2 hours ago' +%FT%TZ)"
```

## Security notes

- Webhook signatures are verified with `hmac.compare_digest` (constant time). The endpoint **fails closed**: no `GITHUB_WEBHOOK_SECRET` configured → 500, nothing accepted.
- Redeliveries are deduped on the unique `X-GitHub-Delivery` id — GitHub retries can't double-count a deploy.
- Raw payloads are stored as JSONB for forensics; never log the webhook secret.
- `.env` is gitignored — only `.env.example` is committed.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -v   # runs on in-memory SQLite; prod target is Postgres 16
```
