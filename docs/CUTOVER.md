# Cutover Checklist & Deployment Notes

Operator procedure for a clean Docker Compose cutover. The default path uses
remote providers and requires no GPU or pre-built local-model image. Run it from
a fresh checkout with a newly created `.env`; do not reuse personal credentials,
containers, volumes, or provider keys from another installation.

This guide does not claim that a clean-environment cutover has been executed for
this revision. Record runtime evidence from the target environment before
declaring a deployment successful.

## Prerequisites

- Docker Engine and Docker Compose v2.
- Remote provider credentials for each generation stage you intend to run.
- A changed `POSTGRES_PASSWORD` and matching `DATABASE_URL`, a strong
  `ADMIN_API_KEY`, and deployment-specific `CORS_ORIGINS` in `.env`.
- FFmpeg in the worker image for the final render. A GPU is not a prerequisite
  for the remote-provider path.

Create the environment from the tracked template and configure only credentials
approved for this deployment:

```bash
cp .env.example .env
docker compose pull postgres redis
docker compose build api worker studio-web
docker compose up -d postgres redis
docker compose ps postgres redis
```

Wait until Postgres and Redis report healthy before continuing.

## Remote-provider cutover (default)

This is the canonical no-GPU order. Do not bring up application services before
the migration and authentication configuration are ready.

### 1. Migrate the database

```bash
docker compose run --rm api alembic upgrade head
docker compose run --rm api alembic current
docker compose run --rm api alembic heads
```

`current` must match the single reported head.

### 2. Bootstrap or configure authentication

For a synthetic/local operator key, follow the README's canonical
[Bootstrap an API key](../README.md#35-bootstrap-an-api-key-first-time-only)
procedure. For a team deployment, use an operator-approved DB-backed creator key
whose user belongs only to the intended workspaces. Set the resulting server-side
key as `API_KEY` in this deployment's `.env`; never put it in browser storage or
client JavaScript.

Load creator and admin credentials into the operator shell without adding them to
shell history. These exports are for the smoke commands below; Compose still
reads the same values from `.env`:

```bash
read -rsp "Creator API key: " API_KEY; printf '\n'; export API_KEY
read -rsp "Admin API key: " ADMIN_API_KEY; printf '\n'; export ADMIN_API_KEY
```

On shared hosts, replace these prompts with the approved secret-manager export
step. Do not use a literal `export API_KEY=...` command that records the value in
history.

Also set the remote provider keys required by the chosen pipeline. Provider keys
authorize paid generation; `API_KEY` authenticates creator API requests. They are
different trust domains.

### 3. Start the application and Studio proxy

```bash
docker compose up -d api worker beat studio-web
docker compose ps api worker beat studio-web
```

Run exactly one dedicated `beat` service per environment. Do not add Celery's
embedded scheduler flag to workers, including scaled workers; multiple schedulers
can enqueue the same periodic work more than once. Recreate `studio-web` whenever
`API_KEY` changes so its same-origin nginx proxy receives the new value:

```bash
docker compose up -d --force-recreate --no-deps studio-web
```

### 4. Run authenticated smoke checks

First prove the creator boundary rejects a missing key, then prove the approved
key works through both the API and the Studio same-origin proxy:

```bash
curl -i http://127.0.0.1:8000/api/creator/workspaces
curl -fsS -H "X-API-Key: ${API_KEY}" http://127.0.0.1:8000/api/creator/workspaces
curl -fsS http://127.0.0.1:5174/api/creator/workspaces
```

The first request must return 401. The latter two must succeed and list only
workspaces authorized for that key. Exercise a same-origin Studio mutation and
an image, audio, or video upload before accepting a team cutover; confirm another
workspace's identifier remains unavailable (404, not 403). Do not paste keys into
shell history on shared hosts; use an approved secret injection mechanism there.

An **offline demo** success proves authentication, sample media, Timeline review,
and local rendering. It does not call or validate a real provider. A **real
provider** success requires completing the generated workflow with the configured
remote LLM, image, TTS, and STT providers and downloading its first MP4. See the
[First Short Walkthrough](FIRST_SHORT_WALKTHROUGH.md) for the two flows.

## Verification ladder

Pass each layer independently. A lower layer never implies a higher one.

The automated `scripts/cutover_runtime_acceptance.sh` harness is separate from
this operator procedure. Run it only in a fresh clone or `git worktree` with no
`.env`, or move/rename the real `.env` first: it refuses an existing `.env`
before touching it. CI's `docker` job sets `CUTOVER_ACCEPTANCE_DISPOSABLE=1`,
meaning **this checkout is disposable: its `.env` will be replaced**. Never set
that opt-in on a working deployment. Replacement prints a loud WARNING to stderr
naming the checkout directory; no warning is printed if `.env` was absent.
Without the opt-in, `ln -T --` publishes the complete private temporary file without
overwriting: an entry of any kind (file, directory or symlink) appearing at `.env`
after the guard causes atomic refusal, and the hard link needs a checkout filesystem
that supports hard links. Only the
explicit opt-in uses `mv -T`. The later API-key rewrite uses `os.replace` on the
harness's own file. INT/TERM handling records publication success before exiting;
cleanup removes the temporary name in either case and `.env` only after successful
publication, with Compose project volume teardown only for a published environment.
Compose services use a relative `env_file: .env`; `--env-file` alone cannot
isolate them. The root `.dockerignore` excludes `.env*` secrets (including
interrupted `.env-storage-*` and `.env-cutover-*` copies) at every depth. It retains
only `.env.example` and `.env.*.example` files, not their directory contents.
All `*.example` files must stay synthetic, never contain real credentials.
An uncatchable termination may leave disposable harness files; keep that checkout
private and remove them before reuse. These checks do not replace deployment smoke.

### Liveness

```bash
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:5174/
```

This only proves that the API and Studio processes answer.

### Readiness

```bash
curl -fsS -H "X-Admin-Key: ${ADMIN_API_KEY}" http://127.0.0.1:8000/health
```

In production or staging this checks database, Redis, and shutdown readiness.
Keep the admin key separate from the creator key.

### Worker registration

```bash
docker compose exec worker celery -A celery_app.celery_app inspect ping
docker compose exec worker celery -A celery_app.celery_app inspect registered
```

Require a worker reply and registrations for `reconcile_stale_dispatches`,
`retry_failed_artifact_deletions`, and `sweep_expired_artifacts`. Registration
does not prove Beat has dispatched a task.

### Scheduler execution

Create a disposable due housekeeping record in the cutover workspace, then watch
the dedicated scheduler and worker for longer than the relevant schedule interval:

```bash
docker compose logs beat worker
```

For example, a stale pending dispatch older than the reconciler threshold must be
processed by `reconcile_stale_dispatches`, and its expected task/run DB transition
must be observed. Use only disposable records owned by this deployment. A log line
or registered task alone is insufficient; verify the expected database state.

### First MP4

Complete either the explicitly labeled offline demo or a real-provider workflow,
approve the required reviews, preview the result, and download the MP4. Record
which path ran, artifact identifier, timestamps, provider set, and any failures.
Only the real-provider path proves authenticated provider operation.

## Optional local GPU path

This path is an optional alternative after the CPU core is working. It requires
an NVIDIA GPU, NVIDIA Container Toolkit, and local service images that are not
shipped or reproducible from this repository. It is not a hidden prerequisite of
the remote-provider cutover.

```bash
docker compose --profile gpu up -d ollama stable-diffusion tts-qwen3 stt-whisper
docker compose exec ollama ollama pull qwen3:4b
```

Keep the same migration, authentication, Studio proxy, scheduler, and verification
steps. Starting GPU services does not replace Beat.

## Authentication prerequisites by deployment model

| Model | Allowed authentication assumption | Required action |
|---|---|---|
| **Local-only** | One operator on loopback may use one bootstrapped creator key. | Keep all published ports on `127.0.0.1`; inject `API_KEY` only into `studio-web`. |
| **Trusted team** | A shared Studio proxy key attributes every browser action to one user; all holders must be trusted. | Put Studio behind TLS and an authenticated reverse proxy, restrict network access, and prefer individually issued keys when attribution matters. |
| **Per-user hosted** | The shared proxy-key model is insufficient for untrusted users. | Do not launch until OAuth2/OIDC human login, per-user identity-to-workspace mapping, authorization, rate limits, and durable audit controls are implemented. |

See the [Security model](SECURITY.md) for trust boundaries and network policy.
Never expose the API, database, or Redis directly, and never change their Compose
bindings from loopback as a shortcut.

## Optional monitoring

```bash
docker compose --profile monitoring up -d flower
curl -fsS http://127.0.0.1:5555/
```

Flower is observability, not evidence of scheduler execution or pipeline success.

## Rollback

Before rollback, follow the coordinated database/media restore and verification procedure in [BACKUP_RESTORE.md](BACKUP_RESTORE.md). Application rollback uses the approved release manifest from #858; database downgrade is a separate approved data-change procedure.

### Application Rollback

```bash
docker compose down
git checkout <previous-tag-or-commit>
docker compose build api worker studio-web
docker compose up -d api worker beat studio-web
```

Prefer backward-compatible migrations. A database downgrade can destroy data;
back up and assess the migration before running `alembic downgrade`.

## Troubleshooting

| Symptom | Check |
|---|---|
| Creator requests return 401 | Bootstrap/approve the creator key, set `API_KEY`, then recreate `studio-web`. |
| Authenticated key sees the wrong workspace | Stop cutover; inspect key ownership and workspace membership. Do not broaden membership to hide the defect. |
| Worker receives no tasks | Check `REDIS_URL`, worker ping, queue routing, and worker logs. |
| Periodic work never runs | Confirm exactly one `beat` container is running, inspect Beat and worker logs, then verify a due record's DB transition. |
| Provider stage fails | Check only that stage's approved remote credential and provider configuration; do not expose the secret in logs. |
| GPU service fails | Return to the default remote-provider path unless all optional GPU prerequisites and local images exist. |
