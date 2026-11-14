# Task Timeout & Retry Policy

This document describes the timeout and retry configuration for each pipeline
stage task in the worker orchestrator.

---

## Overview

All tasks use Celery's built-in retry mechanism with:
- **Exponential backoff** with jitter to avoid thundering herd
- **Auto-retry** on `ProviderTimeoutError` and `RateLimitError`
- **Soft time limit** (raises `SoftTimeLimitExceeded` — task can clean up)
- **Hard time limit** (kills task unconditionally — 60s after soft limit)

These timeout bullets describe task declarations, not guarantees of every pool.
The base, script, audio, and render Compose workers use Celery 5.6.3's `solo`
pool. Its implementation executes inline (`celery/concurrency/solo.py`, lines
11-30) and reports `timeouts: ()`; therefore Celery soft/hard limits and
remote-control terminate cannot interrupt a running task in those workers.
The scaled image worker uses `prefork`, where process tests verify a soft limit.
No worker pool is changed by this policy.

---

## Per-Stage Configuration

| Task | Queue | Soft Limit | Hard Limit | Max Retries | Backoff | Notes |
|------|-------|-----------|-----------|-------------|---------|-------|
| `generate_script` | script | 300s (5m) | 360s (6m) | 5 | 30s × 2^n | LLM call |
| `generate_visual_plan` | script | 300s (5m) | 360s (6m) | 5 | 30s × 2^n | LLM call |
| `generate_scene_image` | image | 600s (10m) | 660s (11m) | 5 | 30s × 2^n | GPU/API image gen |
| `generate_audio` | audio | 300s (5m) | 360s (6m) | 5 | 30s × 2^n | TTS provider |
| `generate_paragraph_audio` | audio | 300s (5m) | 360s (6m) | 3 | exp backoff | Per-paragraph TTS |
| `generate_subtitles` | audio | 300s (5m) | 360s (6m) | 5 | 30s × 2^n | STT provider |
| `generate_paragraph_subtitles` | audio | 300s (5m) | 360s (6m) | 3 | exp backoff | Per-paragraph STT |
| `render_video` | render | 600s (10m) | 660s (11m) | 3 | exp backoff | FFmpeg rendering |
| `reconcile_stale_dispatches` | creator | — | — | 0 | — | Beat task (60s interval) |

---

## Retry Behavior

### Retryable Errors (auto-retry)

| Exception | Behavior |
|-----------|----------|
| `ProviderTimeoutError` | Auto-retry with backoff |
| `RateLimitError` | Auto-retry with backoff |

### Non-Retryable Errors (immediate failure)

| Exception | Behavior |
|-----------|----------|
| `ProviderError` (base) | Task fails; eligible run transitions to `FAILED` |
| Execution `ValueError` / Pydantic or domain `ValidationError` | Task fails; eligible run transitions to `FAILED`, no automatic retry |
| `StageGuardError` | Task rejected; run unchanged |
| `TaskInputError` | Explicit missing prerequisite/invalid argument; task failed, run unchanged |
| `SoftTimeLimitExceeded` | Common runner attempts guarded `FAILED` transition and re-raises; it does not convert to a provider timeout |
| Other unhandled | Task fails, stored in DLQ |

`generate_audio` starts its single-pass fallback only for preparation failures
that happen BEFORE the first per-section provider call (quality-profile
resolution, section planning). Once a section provider call has started,
provider exceptions are mapped exactly like the single-pass path
(`ProviderTimeoutError`, `RateLimitError`, `ProviderError`) and any later local
failure (paragraph artifact, concatenation, sidecar) propagates unchanged, so a
paid section is never followed by a second, paid single-pass call.
`SoftTimeLimitExceeded` always propagates. The single-pass provider
timeout/rate-limit retry policy remains unchanged.

### Backoff Calculation

```
delay = min(retry_backoff × 2^(retry_count), max_backoff)
       + random_jitter(0, delay × 0.5)
```

Default `retry_backoff=30` means:
- 1st retry: ~30-45s
- 2nd retry: ~60-90s
- 3rd retry: ~120-180s
- 4th retry: ~240-360s
- 5th retry: ~480-720s

---

## Queue Configuration

Workers can subscribe to specific queues for independent scaling:

```bash
# All queues (default single-worker setup)
celery -A celery_app worker -Q creator,script,image,audio,render

# Scale image generation independently (GPU worker)
celery -A celery_app worker -Q image --concurrency=2

# Dedicated LLM worker
celery -A celery_app worker -Q script --concurrency=4

# Render worker (CPU-bound, single concurrency recommended)
celery -A celery_app worker -Q render --concurrency=1
```

## Cancellation and shutdown boundaries

Application cancellation is cooperative: the API commits cancellation in
durable run/task state, and a surviving worker observes that state at its next
boundary. After cancellation is visible, the worker must not start another
provider call or persist a new asset, and it must release delivery-owned files,
quota reservations, and child processes. A request already sent to a remote
provider cannot be recalled; its response must be discarded if cancellation
wins before persistence.

Celery 5.6.3 remote control is handled by the worker consumer. In a `solo`
worker, the inline task blocks that consumer, so revoke is not processed until
the task returns. `terminate=True` also has no separate child process to kill.
SIGTERM starts warm shutdown and waits for active work; it does not itself
cancel the running task. Operators must rely on cooperative cancellation and
provider/child-process-specific deadlines for the shipped solo workers.

---

## Failure Handling

### Dead Letter Queue (DLQ)

Tasks that exhaust all retries are stored in Redis list `dlq:creator` with:
- Task name, args, kwargs
- Exception type and traceback
- Timestamp
- Worker hostname

DLQ max size is controlled by `DLQ_MAX_SIZE` env var (default: 10000).
If Redis is unavailable, failures are written to the bounded, durable
`DLQ_FALLBACK_PATH` JSONL file. See [QUEUE_DURABILITY.md](QUEUE_DURABILITY.md) for
the recovery, rotation, and operator collection contract.

### Run Status on Failure

When execution permanently fails in the common runner:
1. General failure finalization records `status = "failed"` and a safe categorized
   error code/message only while the tracking row is still `running`.
   Success finalization uses the same guard; late outcomes preserve revoked,
   rejected, or already successful rows.
   The read surface reconstructs actionable recovery guidance from that code;
   raw exception text and provider output are not persisted in this record.
2. The run transitions to `current_stage = "FAILED"`, `status = "failed"` only
   while still in the task's configured safe failure stages and not cancelled.
   Concurrently advanced review/completed/published stages remain unchanged.
3. Malformed broker input is rejected before claim without modifying a run.
   Claim/context errors before execution and stage rejection do not fail a run.
   Claim/context errors retain the existing best-effort task-failure recording:
   `mark_failed_if_running` updates an existing running tracking row only; it
   never creates a row when the claim failed before insertion. Malformed broker input bypasses even
   that recording, while stage rejection uses `mark_rejected`.
4. Provider timeout/rate-limit failures with retry budget remaining mark the task
   failed so it can be reclaimed, but keep the run generating. Exhausted retries
   use the same guarded terminal-failure transition as other execution errors.

The dedicated soft-timeout handler attempts the guarded run failure transition
and re-raises to Celery, which reports the delivery as `FAILURE` and invokes the
failure/DLQ signal. That handler does not finalize the application task-tracking
row or perform partial-audio/quota cleanup. Propagation does not add those cleanup
guarantees; they require separate lifecycle ownership.
See [#837](https://github.com/yeongseon/short-form-studio/issues/837) for that
lifecycle follow-up and [#836](https://github.com/yeongseon/short-form-studio/issues/836)
for soft-timeout wrapping inside the Edge TTS adapter itself.

Explicit `no_fail_transition_exceptions` overrides remain available for task-specific
nonfatal contracts; the default exempts only `TaskInputError`. Do not exempt all
`ValueError`s: Pydantic validation errors also inherit from `ValueError`.

### Recovery of legacy failed-task/generating-run records

The reconciler does not scan failed task records. After checking that the run is
still eligible and that no active/retried delivery is executing, an operator can
redeliver the original task ID and validated payload through the normal worker.
The existing exclusive claim guards skip running/successful duplicates. A failed
record can be reclaimed: fresh success advances normally, while fresh terminal
validation failure now transitions the eligible run to `FAILED`. Never bulk-fail
runs solely because an old task record is failed; that state is also used between
provider retries. This is explicit recovery, not an automatic historical backfill.

See [TIMEOUT_RETRY_POLICY.md](TIMEOUT_RETRY_POLICY.md) for timeout boundaries.

### Stale Task Reconciliation

The `reconcile_stale_dispatches` beat task runs every 60 seconds and:
1. Finds tasks stuck in `pending` state for > threshold
2. Marks them as failed (cannot re-enqueue without persisted args)
3. Updates the associated run status

---

## Tuning Guidelines

### When to Increase Timeouts

- Image generation with high-resolution models: increase `generate_scene_image` soft_time_limit
- Slow LLM providers (e.g., local Ollama on CPU): increase script/visual_plan limits
- Long videos (> 60s): increase `render_video` limits

### When to Increase Retries

- Unreliable network to external providers: increase max_retries
- Rate-limited APIs with strict quotas: increase max_retries + backoff

### When to Decrease Retries

- Local providers (Ollama, local SD): reduce to 2-3 (failures are likely persistent)
- Development/testing: set `max_retries=0` to fail fast

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DLQ_MAX_SIZE` | 10000 | Maximum entries in Redis DLQ |
| `DLQ_FALLBACK_PATH` | `/tmp/dlq_fallback.jsonl` | File path when Redis DLQ unavailable; Compose overrides this to a mounted volume |
| `DLQ_FALLBACK_MAX_BYTES` | 10485760 | Rotate fallback after this many bytes; one `.1` segment is retained |
| `CELERY_TASK_ROUTES` | — | JSON override for task routing |
