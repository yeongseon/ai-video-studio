# Quick Start — Your First Short (CPU / remote providers)

This is the fastest reproducible path from a clean machine to your first
short-form video, using **remote AI providers** and **no GPU**. The local GPU
services are an optional path documented at the end.

> The project [README](../README.md) also has a Quick Start overview; **this file
> is the reproducible, first-short-focused path** with failure recovery.

## 1. Prerequisites

- Docker and Docker Compose v2+ (`docker compose version`).
- No GPU required. The CPU path uses remote providers for the LLM, image, TTS, and
  speech-to-text (subtitle) stages, because the local model services are gated
  behind the optional `gpu` profile.

## 2. Clone and configure

```bash
git clone https://github.com/yeongseon/short-form-studio.git
cd short-form-studio
cp .env.example .env
```

Edit `.env`:

- **Change the database password** (never ship the placeholder):
  - `POSTGRES_PASSWORD=<a-strong-password>`
  - update the same password inside `DATABASE_URL`.
- **Set a strong admin key**: `ADMIN_API_KEY=<a-strong-random-string>` (min 16
  chars; required in production mode).
- **Set the remote provider keys** for a full CPU pipeline:
  - `OPENAI_API_KEY=<your-openai-api-key>` — powers the script (LLM, `gpt-4o-mini`),
    images (`dall-e-3`), and narration (TTS, `openai-tts-1`).
  - `GROQ_API_KEY=<your-groq-api-key>` — powers remote speech-to-text for
    auto-subtitles (`groq-whisper-large-v3-turbo`). The default `whisper-small`
    STT model is local/GPU, so a remote STT key is what keeps the CPU path
    complete. If you do not need burned-in subtitles yet, you can defer this key
    and set `burn_subtitles` off in your render settings.

> **Security defaults are already safe.** Every published port binds to
> `127.0.0.1` only (Postgres, Redis, the API on `127.0.0.1:8000`, and the studio
> UI on `127.0.0.1:5174`), so nothing is exposed on your LAN. Do not change these
> to `0.0.0.0`. Keep `ENVIRONMENT=development` for local use; production mode
> rejects the default password and enforces `CORS_ORIGINS`.

## 3. Start infrastructure

```bash
docker compose up -d postgres redis
```

Wait for both services to become healthy. The GPU model services (Ollama, Stable
Diffusion, local TTS/STT) stay stopped because they are behind the `gpu` profile.

## 4. Run database migrations

```bash
docker compose run --rm api alembic upgrade head
```

Run this before first use; the API needs the schema in place.

## 5. Bootstrap an API key (first time only)

```bash
docker compose run --rm api python scripts/create_api_key.py \
  --email local@example.com \
  --workspace local \
  --name local-dev
```

This creates a user, a workspace, and an API key for local development; the key is
printed to stdout. Keep it — the studio UI and API use it to authenticate.

**Wire the key into the studio UI so the browser can authenticate.** The studio
UI never holds the key in the browser; instead its server-side proxy injects it
as the `X-API-Key` header. That injection reads the `API_KEY` env var, so you must
put the generated key into `.env` before starting the application:

```bash
# 1. Put the generated key into .env
API_KEY=<paste-the-generated-key>   # set this line in .env (replaces the placeholder)

# 2. Start the complete CPU application once with that key
docker compose up -d api worker beat studio-web
```

Without this step the UI's calls to `/api/creator/*` return **401 Unauthorized**,
because the browser sends no key until the proxy is configured with `API_KEY`.

## 6. Confirm the CPU application

Start one dedicated Beat scheduler with the API, worker, and Studio. Do not add
`-B` to workers, including scaled workers.

The preceding command starts `api`, `worker`, one dedicated `beat`, and
`studio-web` together. Do not start or recreate `studio-web` before `api` on a
clean installation. Use `docker compose up -d --force-recreate --no-deps
studio-web` only after changing `API_KEY` on an already-running installation.

The migration → key bootstrap/configuration → Studio proxy → authenticated smoke
order is also the canonical deployment sequence in [CUTOVER.md](CUTOVER.md).

## 7. Open the studio and make your first Short

Navigate to <http://127.0.0.1:5174>. From there:

For a no-provider smoke path, choose **Try the demo**. This creates synthetic
image media and a saved Timeline locally, not an AI-generated video. Review the
Timeline, explicitly approve its current revision for rendering, then inspect
and download the resulting MP4. The final publication review remains required.
See [First Short Walkthrough](FIRST_SHORT_WALKTHROUGH.md).

1. Start a new project from an idea or script.
2. Approve each stage (script → visual plan → images → audio → subtitles → render).
   With the remote keys set, every stage runs without a GPU.
3. Review the final render and publish.

The Idea form offers 15/30/45/60/90-second presets and Custom. Custom duration is
hard-validated only as a finite number greater than zero; “approximately 3 minutes
or less” is short-form guidance, not a 180-second ceiling. A target is planning
input, not an assertion that the encoded MP4 will have that exact duration. Only
the short content format is currently accepted; long-form remains unvalidated
([#884](https://github.com/yeongseon/short-form-studio/issues/884),
[#885](https://github.com/yeongseon/short-form-studio/issues/885)). See the
[Usage Guide](USAGE.md#duration-output-and-encoding-contracts) for output and
encoding details.

## Optional: local GPU path

If you have an NVIDIA GPU with the Container Toolkit and want fully local
inference (no remote API keys), start the GPU services alongside the core:

```bash
docker compose --profile gpu up -d
# then pull the default local LLM (first time only):
docker compose exec ollama ollama pull qwen3:4b
```

The `gpu` profile adds Ollama, Stable Diffusion, and the local TTS/STT services.
See [Local Docker setup](LOCAL_DOCKER.md) for LAN access and
[Security model](SECURITY.md) for trust boundaries.

## Verify the setup (smoke check)

Before (or instead of) bringing the stack up, run the static smoke check. It
validates the compose config, confirms the CPU core is not GPU-gated, confirms
every published port is loopback-only, and confirms the referenced env vars and
bootstrap/migration entrypoints exist:

```bash
bash scripts/quickstart_smoke.sh
```

This uses an isolated copy of `.env.example`; it neither requires nor reads your
local `.env`. It validates Compose configuration, not video generation. Runtime
acceptance is a separate test with disposable PostgreSQL and Redis, a real API
process, a Celery worker and FFmpeg:

```bash
# These variables must point at disposable test services, never production.
FIRST_SHORT_TEST_DATABASE_URL=postgresql://review:review-local-only@127.0.0.1:55439/first_short \
FIRST_SHORT_TEST_REDIS_URL=redis://127.0.0.1:56389/0 \
python3 -m pytest tests/integration/test_first_short_runtime.py -v
```

That runtime test seeds deterministic synthetic PNG fixtures, uses a real Celery
worker and real FFmpeg, checks an authenticated MP4 download with `ffprobe`, and
does not call an AI provider. The faster API/demo tests use in-memory services and
mock render dispatch; worker orchestration tests also mock FFmpeg. Provider adapter
tests use mocks/fixtures. Configuring a paid-provider key above is an operating
path, **not evidence of a repository-recorded paid-provider end-to-end smoke**.
Relevant evidence is in `tests/integration/test_first_short_runtime.py`,
`packages/creator-service/tests/test_demo_offline.py`,
`apps/api/tests/timeline_demo_support.py`, and
`apps/worker-orchestrator/tests/test_render_video.py`.

## Failure recovery / troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| A stage errors with "provider not configured" / auth error | The relevant provider key is blank in `.env` | Set `OPENAI_API_KEY` (script/image/TTS) and `GROQ_API_KEY` (subtitles), then `docker compose up -d` to reload |
| The studio UI loads but every action fails with 401 Unauthorized | `API_KEY` not wired into studio-web (step 5) | Put the generated key into `.env` as `API_KEY=...`, then `docker compose up -d --force-recreate --no-deps studio-web` |
| API errors about missing tables / relations | Migrations not run | `docker compose run --rm api alembic upgrade head` |
| API/UI won't start; "default password" or CORS error at boot | `ENVIRONMENT=production` with the placeholder password or missing `CORS_ORIGINS` | Keep `ENVIRONMENT=development` locally, or set a real `POSTGRES_PASSWORD`, `ADMIN_API_KEY`, and `CORS_ORIGINS` |
| Port already in use on `127.0.0.1:5174` / `:8000` | Another process holds the port | Stop it, or remap the host port in `docker-compose.yml` (keep the `127.0.0.1:` prefix) |
| Auto-subtitles fail but the rest renders | No remote STT key and local Whisper is GPU-only | Set `GROQ_API_KEY`, or disable burned-in subtitles for now |
| GPU services fail to start | No GPU / missing NVIDIA Container Toolkit, or the local images are not built | Use the CPU/remote path above; the `gpu` profile is optional |

To reset a broken local stack completely:

```bash
docker compose down -v   # WARNING: -v drops the database volume
```
