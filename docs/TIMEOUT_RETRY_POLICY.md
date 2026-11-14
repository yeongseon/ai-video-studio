# Timeout & Retry Policy

This document describes the timeout and retry configuration for all pipeline tasks.

---

## Task Timeout Matrix

| Task | Soft Limit | Hard Limit | Max Retries | Backoff | Queue |
|------|-----------|-----------|-------------|---------|-------|
| `generate_script` | 300s (5m) | 360s (6m) | 5 | 30s exponential | script |
| `generate_visual_plan` | 300s (5m) | 360s (6m) | 5 | 30s exponential | script |
| `generate_scene_image` | 600s (10m) | 660s (11m) | 5 | 30s exponential | image |
| `generate_audio` | 300s (5m) | 360s (6m) | 5 | 30s exponential | audio |
| `generate_paragraph_audio` | 300s (5m) | 360s (6m) | 3 | exponential | audio |
| `generate_subtitles` | 300s (5m) | 360s (6m) | 5 | 30s exponential | audio |
| `generate_paragraph_subtitles` | 300s (5m) | 360s (6m) | 3 | exponential | audio |
| `render_video` | 600s (10m) | 660s (11m) | 3 | exponential | render |
| `reconcile_stale_dispatches` | — | — | 0 | — | creator |

---

## How Timeouts Work

### Media Upload Object-I/O and Reconciliation

S3 and Azure clients use a 30-second connection timeout and a 10-minute read
timeout. The upload service's operation deadline is 30 minutes. If reconciliation
claims an upload that was still `pending`, successful deletion is not proof that
the blocking provider call has returned: S3 deletion is idempotent, and an
in-flight local or Azure upload can rewrite the key. Such intents therefore stay
in `late_upload_watch` and are retried hourly even after successful deletion.

The local write and stream-spooling paths cannot enforce the service deadline
while blocking I/O is active. A `pending` or `late_upload_watch` intent therefore
cannot become terminal based on elapsed wall time alone. Its sweepable tombstone
remains until the uploader durably records completion or compensation, so a key
that materializes after an arbitrarily delayed blocking call is still reclaimed.

### Soft Time Limit (`soft_time_limit`)
Raises `SoftTimeLimitExceeded` inside the task. The task can catch this and
perform cleanup (save partial results, update status to FAILED).

### Hard Time Limit (`time_limit`)
Kills the worker process if the task exceeds this. Always set 60s above the
soft limit to allow cleanup time. Where the pool delivers it (`prefork`), the
parent first records the durable `TIMEOUT` failure and then runs the task's
output cleanup; a failure in either step is logged by the handler itself and
does not skip the other step.

### Pool support in the shipped topology

The declarations above are enforced only by pools that implement timeouts.
With the pinned Celery 5.6.3, `celery/concurrency/solo.py` defines a blocking,
inline `TaskPool` and reports an empty timeout capability tuple. Consequently,
the base Compose worker and scaled script/audio/render workers do **not**
deliver Celery soft or hard time limits while a task is running. The scaled
image worker uses `prefork`; the process-level CI test verifies its soft-timeout
failure state and cleanup of the synthetic child, temporary file, and quota
marker. Provider HTTP timeouts and explicit subprocess termination remain
required boundaries regardless of pool.

Remote-control revoke has the same solo limitation: the consumer cannot handle
the command until inline execution returns, and terminate has no child worker
process to signal. SIGTERM is warm shutdown and waits for active work. These
limits are evidence for operational caution, not authorization to change pools;
prefork migration would separately require event-loop, DB-pool, GPU-lock, and
file-ownership regression coverage.

### Why Image/Render Get Longer Limits
- **Image generation** may involve external API calls with queuing (DALL-E, Stability)
- **Video render** runs FFmpeg which is CPU-bound and scales with video length

---

## Retry Strategy

### Exponential Backoff
Tasks use `retry_backoff=30` (or `retry_backoff=True`), meaning:
- 1st retry: ~30s delay
- 2nd retry: ~60s delay
- 3rd retry: ~120s delay
- 4th retry: ~240s delay
- 5th retry: ~480s delay

With jitter applied to prevent thundering herd.

### Retryable Exceptions
Tasks retry on:
- `ProviderTimeoutError` — upstream API timed out
- `RateLimitError` — upstream rate limit hit
- Network failures retry when the task translates them to `ProviderTimeoutError`.
  Bare `ConnectionError` is not part of the common runner's retry whitelist.

The common runner re-raises `SoftTimeLimitExceeded` after attempting a guarded
`FAILED` transition. It does not convert that signal to `ProviderTimeoutError`.
Task-specific catches may differ; per-section audio timeout propagation is tracked
separately in #819.

### Non-Retryable Exceptions
Tasks fail immediately on:
- Execution-time Pydantic/domain `ValidationError` — invalid generated or stored data
- `ProviderAuthError` — bad API credentials
- Execution-time `ValueError` — invalid task data or task logic

These terminal execution errors mark the task failed and transition the run to
`FAILED` only through the existing safe-stage/cancellation guard. They are distinct
from malformed broker messages (rejected before claim, no run mutation) and
`StageGuardError` (rejected stale/illegal-stage delivery, no run failure).
Errors before execution begins do not fail the run. Explicitly rejected
prerequisites/arguments (`TaskInputError`) retain the existing nonfatal behavior.
Running/successful duplicate deliveries skip execution; timeout/rate-limit retries
retain generating state until
their retry budget is exhausted. Safe error records and explicit recovery of legacy
failed-task/generating-run records are described in [TASK_POLICY.md](TASK_POLICY.md).

---

## Dead Letter Queue (DLQ)

Tasks that exhaust all retries are recorded to the DLQ:
- **Primary**: Redis list `dlq:creator` (max 10,000 entries, configurable via `DLQ_MAX_SIZE`)
- **Fallback**: File at `DLQ_FALLBACK_PATH` (default: `/tmp/dlq_fallback.jsonl`)

Sensitive data is redacted before DLQ storage. See `DLQ_MONITORING.md` for
operational procedures.

---

## Worker Configuration

### Global Settings (celery_app.py)
```python
worker_prefetch_multiplier = 1      # Only fetch 1 task at a time
task_acks_late = True               # Acknowledge AFTER execution (at-least-once)
task_reject_on_worker_lost = True   # Return to queue if worker dies
```

### Resource Limits
- `MAX_MEMORY_MB`: Worker memory limit (default: 4096 MB)
- Workers are killed if memory limit is exceeded (RLIMIT_AS)

---

## Tuning Recommendations

### Increasing Timeout for Slow Providers
If using a slow provider (e.g., large local model), override via environment:
```bash
# These are Celery-level settings, not per-task
CELERY_TASK_SOFT_TIME_LIMIT=900
CELERY_TASK_TIME_LIMIT=960
```

### Reducing Retries for Fast Failure
For development where you want immediate failure feedback:
```python
# In task decorator
@celery_app.task(max_retries=0, ...)
```

### Queue-Specific Scaling
Use `docker-compose.scaled-workers.yml` to scale specific queues:
```bash
docker compose -f docker-compose.yml -f docker-compose.scaled-workers.yml up -d --scale worker-image=3
```
