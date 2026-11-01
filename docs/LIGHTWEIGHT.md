# Lightweight Mode

Lightweight mode lets you run API task dispatch without a Celery worker or Redis
broker; the Celery Python library and copied worker task modules are still required.
When `REDIS_URL` is not set, task dispatch runs worker task functions synchronously in-process.

## When To Use

- Local development where you only run the API service.
- Debugging task behavior without queue infrastructure.
- Small single-user environments where async queueing is not required.

## How It Works

- `REDIS_URL` set: API dispatches tasks with Celery `apply_async(...)` (existing behavior).
- `REDIS_URL` unset or empty: API calls the same worker task entrypoints directly.
- Synchronous failures are caught and surfaced as errors. The adapter attempts to
  mark the run `FAILED`; this is best-effort if the failure update itself fails.

## Enable Lightweight Mode

1. Unset `REDIS_URL` in your environment.
2. Start API normally.
3. Trigger pipeline stages from the UI or API.

Example from the repository root (Python 3.12 with API and shared-package
dependencies installed). The source checkout needs the worker modules on its
import path; the runtime image already copies them:

```bash
unset REDIS_URL
cd apps/api
PYTHONPATH=../worker-orchestrator uvicorn shorts_api.main:app --app-dir src --reload
```

## Production Warning

Lightweight mode is **not suitable for production use**. The triggering request
awaits task execution in a bounded worker thread within the API process; it does
not block the API event loop. Temporary loops and DB pools are invocation-local,
but there is no separate-process isolation, Celery worker retry scheduling, or
independent worker scaling. Always use Celery + Redis in production.

- Tasks execute during the request lifecycle, so requests can take longer.
- A supplied task ID (including an owned reservation ID) is preserved; otherwise
  lightweight dispatch generates a synthetic `sync-...` ID.
- No worker process is required for dispatch execution in this mode.

## API Response Semantics

In normal (Celery) mode, successful generation trigger endpoints return
`202 Accepted` after queue submission, not worker completion. In lightweight
mode, successful triggers also return `202`, but only after local task execution
returns. Execution failures are surfaced as errors, not successful `202` responses.
The trigger response contains task identity and dispatch-stage information; it is
not a guarantee that the body contains the final persisted run state.

This means:
- Requests may take significantly longer (seconds to minutes) depending on the AI provider.
- Clients should read the run and task APIs for persisted results and review state;
  the trigger response is not the final artifact payload.
- Timeouts at the HTTP/proxy layer may need adjustment for long-running tasks.

## Async Request Boundaries

Generation requests still await lightweight task completion and return the same
task identity or error. Blocking execution runs in a bounded AnyIO worker thread;
health and cancellation requests can continue on the API event loop. Each
lightweight invocation owns a temporary worker loop and DB pool, closed before
the invocation returns. Celery workers retain their persistent process loop.

API offload limits per event loop are eight dispatch operations, four control
operations (independent so saturated generation cannot starve revocation), and
one serialized admin rate-limit operation. Media services allow four simultaneous
validation/probe/storage operations per service instance.

Thread cancellation does not stop Python threads. These boundaries use AnyIO's
non-abandoning wait. Dispatch ownership is shielded through reservation,
publication, tracking, and post-dispatch cancellation compensation. An accepted
media upload is shielded through its asset-row save. A disconnected client does
not make an in-progress publish safe to retry; use the existing run/task state.
Shutdown must allow these owned operations to finish; downstream timeouts still
govern their duration. Cancellation before entering an owned operation can stop it.

Image/audio/video upload routes pass file streams to bounded staging: reads are
at most 64 KiB, followed by file-based probing and streamed storage. Existing
per-file limits remain 25 MiB for images, 100 MiB for audio, and 500 MiB for video.
This avoids allocating a whole accepted payload in application memory, but still
requires temporary disk capacity and does not mean the multipart HTTP stack never
buffers or spools input. See
[`media_file_ingestion.py`](../packages/creator-service/creator_service/media_file_ingestion.py)
and [`test_upload_large_rss.py`](../apps/api/tests/test_upload_large_rss.py)
for bounded-read and local large-video RSS evidence. Cloud-storage outage latency
and production throughput are not measured by the local boundary tests.

## Supported API-Image Contract and Evidence Limits

The API runtime image supports both Redis-backed queue publication and lightweight
in-process execution. Its copied task modules include `celery_app.py`,
`worker_loop.py`, and `worker_release_identity.py`; no separate worker checkout is
needed inside the image. Queue-only imports and dispatch must not register OS
signal handlers or replace the API process's SIGINT/SIGTERM handlers. The
fresh-process [`test_queue_import_signals.py`](../apps/api/tests/test_queue_import_signals.py)
guards this with `worker_loop` both present and absent (the compatibility layout,
not the current image's execution layout).

The built-image
[`test_api_lightweight_image.py`](../tests/integration/test_api_lightweight_image.py)
runs [`lightweight_image_probe.py`](../tests/integration/lightweight_image_probe.py)
with a local bound stub task and real disposable PostgreSQL/Redis. It proves:

- Lightweight execution reaches the actual worker-loop helper and returns the
  expected stub result without paid providers.
- Concurrent `/healthz` responds while that task is blocked; the API's DB pool
  stays usable and separate from the invocation's pool.
- Switching to queued mode publishes to Redis and returns the requested task ID.

CI's `first-short-runtime` job in
[`ci.yml`](../.github/workflows/ci.yml), step
**Verify API image lightweight execution and concurrent health**, builds the
runtime target and runs this test with all required image/database/Redis settings.
This is execution evidence, not an import-only smoke test. It does **not** prove
full video rendering or FFmpeg availability in the API image, forced-shutdown
behavior, or consumption by a separate Celery worker. The probe is scoped to
dispatch and concurrent ASGI health/DB access, not production throughput or a
deployed HTTP server. Loop/pool closure for overlapping lightweight invocations
is additionally guarded by
[`test_lightweight_loop_ownership.py`](../apps/api/tests/test_lightweight_loop_ownership.py).
