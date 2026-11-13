# Local Docker Compose Setup

This guide explains how to run AI Video Studio on a local server using Docker Compose.

## 1. Create environment file

```bash
cp .env.local.example .env
```

Edit `.env` and change `POSTGRES_PASSWORD` and `DATABASE_URL` credentials.

## 2. Start services

```bash
make docker-local-up
```

This starts:

- PostgreSQL
- Redis
- API
- Worker
- Beat (one dedicated scheduler for periodic reconciliation and maintenance)
- Studio Web

`make docker-local-build` rebuilds and starts the same services, including Beat.
Repeated `make docker-local-up` updates the same Compose services and leaves one
Beat; never scale `beat` or enable Beat inside the worker (`-B` / `--beat`).

## 3. Run database migrations

```bash
make docker-local-migrate
```

## 4. Create API key

```bash
make docker-local-bootstrap
```

Copy the generated API key into `.env`:

```
API_KEY=<generated-key>
```

Then restart the services:

```bash
make docker-local-up
```

## 5. Open Studio

From the local server:

```
http://localhost:5174
```

From another device on the same LAN:

```
http://<server-ip>:5174
```

## 6. Check service status

```bash
make docker-local-status
```

This runs `docker compose -f docker-compose.yml -f docker-compose.local-server.yml ps`
for the whole merged configuration, including Beat. Expect exactly one `beat`
row showing `healthy` after startup. Beat publishes a heartbeat message every 60
seconds and records a receipt each time the broker accepts one; the healthcheck
requires a receipt from within the last 180 seconds. It checks the timestamp inside
the receipt, not a PID or file modification time, and Celery's own schedule
timestamps are deliberately not used because they advance before the publish and
even when the broker rejects it. Missing, unreadable, invalid, future-dated, or
stale receipts fail closed.

Allow the 90-second startup grace period. A missing Beat row means there is no
running scheduler; use the same Compose command with `ps -a` to see stopped
services. An `unhealthy` Beat means it has stopped publishing to the broker:
Beat is stuck or stopped, or Redis is rejecting its publishes. A busy worker does
**not** make Beat unhealthy (the local worker is a solo pool, so a long image or
render task would otherwise do exactly that); worker progress is a separate signal,
so check the worker's own logs and `ps` row. Health proves recent dispatch, not that
a worker consumed the heartbeat or that any maintenance task succeeded. After Beat
stops publishing, the receipt expires in 180 seconds and Docker marks it unhealthy
after three failed 30-second checks. Docker does not automatically restart a
merely unhealthy container. The reverse also holds: a receipt written before a
restart stays valid for up to 180 seconds, so a Beat that was just restarted can
report `healthy` before the new process has published; wait that long, or read the
`beat` logs, before trusting it. While the worker is stopped, accepted heartbeats
keep accumulating in Redis (one per minute, each expiring after 60 seconds); the
worker skips the expired ones when it returns.

To restart the existing scheduler without creating another one:

```bash
docker compose -f docker-compose.yml -f docker-compose.local-server.yml restart beat
make docker-local-status
```

For code/image updates, use `make docker-local-build` instead. Do not start a
second standalone scheduler as a workaround for an unhealthy Beat.

## 7. View logs

```bash
make docker-local-logs
```

This follows API, worker, Beat, and Studio Web logs. Look for Beat's
`Scheduler: Sending due task` entries for `reconcile-stale-dispatches` and
`beat-heartbeat`, and the worker's receipt/execution of the reconciliation task. A
healthy heartbeat alone does not establish that reconciliation completed successfully.

## 8. Stop services

```bash
make docker-local-down
```

This stops the whole merged configuration, including Beat; no service list is
needed for either `docker-local-down` or `docker-local-status`.

## Notes

- Only `studio-web` is exposed to the LAN (bound to `0.0.0.0`).
- API, PostgreSQL, and Redis remain bound to `127.0.0.1` for safety.
- If you need to access the API directly from another device, add your server IP to `CORS_ORIGINS` in `.env`.
- For external (internet) access, use a secure reverse proxy or Cloudflare Tunnel.

## Recommended Models for Local Mode

The default model configuration is optimized for local/free execution:

| Category | Default Model | Why |
|----------|--------------|-----|
| TTS | `edge-tts` | No GPU, no API key, free, Korean voices included |
| LLM | `qwen3-4b` (local) or Groq (cloud) | Local Ollama or free Groq tier |
| STT | `whisper-small` (local) or `groq-whisper-large-v3-turbo` | Local or Groq |
| Image | `placeholder` or `hf-flux-schnell` | Placeholder for testing, HF FLUX for real images |

To override defaults, set these in `.env`:

```env
TTS_DEFAULT_MODEL=edge-tts
SCRIPT_DEFAULT_MODEL=meta-llama/llama-4-scout-17b-16e-instruct
SUBTITLE_DEFAULT_MODEL=groq-whisper-large-v3-turbo
```

### Why edge-tts?

Unlike `qwen3-tts` (which requires a GPU-backed TTS container), `edge-tts` runs
without any local GPU or external API key. It provides high-quality Korean voices
suitable for 썰쇼츠 and demo purposes.

Available Korean voices:

| Voice key | Edge TTS voice |
|-----------|---------------|
| `default` | ko-KR-SunHiNeural |
| `female` | ko-KR-SunHiNeural |
| `male` | ko-KR-InJoonNeural |
