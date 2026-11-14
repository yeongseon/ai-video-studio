# Run storage update contract

The in-memory and PostgreSQL run adapters share these boundaries:

- `update_run` accepts only columns in `UPDATABLE_RUN_COLUMNS`. Storage-owned
  columns (`id`, `version`, `project_id`, `workspace_id`) and unknown columns
  are rejected with `ValueError`. An empty patch is a workspace-scoped read that
  checks `expected_version` when provided; it never advances the version. A
  stale version returns `None`.
- `conditional_update_run` also rejects storage-owned and unknown columns. An
  empty patch reads the scoped run, accepts only an expected stage that is not
  in `rejected_statuses`, and does not advance the version. A missing or
  out-of-workspace run returns `(False, None)`.
- Nonempty accepted patches advance the version once. Callers that require a
  version change must supply an actual field update; an empty patch does not
  acquire an exclusive claim or atomically reserve a later update.

`RunService` stage transitions, dispatch CAS, and worker outcome transitions
submit nonempty patches. These contracts are exercised against both adapters,
including a disposable migrated PostgreSQL schema, in
`packages/creator-service/tests/test_run_adapter_parity.py`.

## Project deletion lifecycle (migration 051)

`DELETE /api/creator/projects/{id}` first persists `status='deleting'` in the
owning workspace. Migration 051 relaxes `ck_creator_projects_status` to admit
that existing domain value; no route, response, or storage retention policy is
changed. Missing or foreign projects remain 404; missing/invalid credentials
remain 401. Run creation and script imports reject deleting projects with 409.
PostgreSQL run creation locks the project and checks the barrier in the same
statement as the INSERT, including requests with a stale pre-barrier access check.

Cleanup cancels runs before task revocation, then removes artifact objects before
removing their metadata. Cancellation/revocation failure aborts cleanup; object
deletion failure returns 503, retains failed artifact rows with retry metadata,
and leaves the project deleting and its runs cancelled. Retrying DELETE remains
possible: marking deleting is idempotent, and the existing owner get/list/title
update paths do **not** filter this status. This visibility is characterized,
not changed to 404 (which would prevent retry). Error responses do not contain
provider exception text. Completed deletions are not idempotent: a later DELETE
returns 404 because the project no longer exists.

The final project DELETE cascades runs, run tasks, script drafts, visual plans,
legacy scene assets, stage reviews, Timeline rows, and normalized approval asset
mapping/resolution rows. Artifact rows have a run cascade and a project FK;
normal cleanup removes them first. Canonical workspace media is reusable:
`creator_media_assets.project_id` and `run_id` use **SET NULL**, not CASCADE, so
metadata and its object remain workspace-owned. Usage events have no project/run
FK and remain billing history; quota owner/attempt ledgers belong to workspace
and task identities, not project/run FKs, and are retained. Project deletion is
not a workspace-wide erasure or accounting-history deletion operation.

For rolling deploys/app-only rollback, retain schema 051: the CHECK relaxation
is compatible with the previous release's queries and domain decoder. The CI
release-lifecycle docker job runs origin/main against the upgraded schema and
verifies project/media reads. A **schema** downgrade to 050 maps deleting rows to
`archived` before restoring the old CHECK; this preserves rows but loses the
barrier (archived is not a write prohibition). Quiesce writers before schema
downgrade, and finish pending cleanup after upgrading again. Neither downgrade
nor app rollback restores deleted objects or completed project deletions.

The real-PostgreSQL deletion, retry, stale-request and rollback proofs live in
`tests/integration/test_postgres_project_deletion*.py` and
`tests/integration/test_project_status_migration.py`; they run unconditionally
in the required `first-short-runtime` CI job, fail rather than skip on GitHub
Actions when PostgreSQL is absent, and have mutation-sensitive CI wiring guards.

### Persisted enum / CHECK audit

`tests/integration/test_postgres_enum_check_drift.py` compares the migrated
PostgreSQL catalog with runtime model definitions, with exact sets in both
directions. New unmapped enum CHECKs also fail the guard. At 050 the only
constrained-domain drift was Project.status missing deleting; 051 resolves it.

| Persisted column | Domain owner / values | CHECK |
|---|---|---|
| creator_projects.status | Project.status: draft, active, completed, archived, deleting | ck_creator_projects_status |
| creator_projects.source_type | Project.source_type: idea, markdown, url, pasted_json | ck_creator_projects_source_type |
| creator_runs.status | PipelineRun.status: pending, running, paused, completed, failed, cancelled | ck_creator_runs_status |
| creator_run_tasks.status | RunTask.status: queued, pending, running, success, failed, revoked, rejected | ck_creator_run_tasks_status |
| creator_media_assets.media_type | MediaType: IMAGE, VIDEO, AUDIO, LOGO, GRAPHIC | ck_media_assets_type |
| creator_media_assets.origin | MediaOrigin: UPLOADED, IMPORTED, EXTERNAL_URL, STOCK, GENERATED | ck_media_assets_origin |
| usage_events.operation_type | UsageEvent.operation_type: llm, image_gen, tts, stt, render | ck_usage_events_operation_type |
| workspace_quota_reservation_owners.operation_type | UsageEvent.operation_type (same operations) | ck_quota_owner_operation |
| creator_media_upload_intents.state | UploadIntentState: pending, uploaded, cleanup_pending, reclaiming, late_upload_watch, completed, reclaimed, exhausted | ck_media_upload_intents_state |
| creator_stage_reviews.review_status | Storage-only strings: pending, approved, rejected, skipped | ck_creator_stage_reviews_review_status |
| creator_artifacts.artifact_type | Storage-only strings: idea, script, visual_plan, visual_asset, audio, subtitle, video, render_manifest | ck_creator_artifacts_artifact_type |
| workspace_quota_reservation_owners.status | Storage-only strings: reserved, consumed, settled, cancelled, billable_unknown | ck_quota_owner_status |
| workspace_quota_reservation_attempts.status | Storage-only strings: authorized, accounted, unknown | ck_quota_attempt_status |

The following persisted types have **no enum-valued CHECK**, so there is no
existing allowed DB set to compare. The audit does not silently claim otherwise
or add restrictive constraints during a rollback-compatible deletion fix:

| Persisted representation | Domain type | Existing boundary |
|---|---|---|
| creator_runs.current_stage, review_stage, restart_from | RunStage (nullable) | Service/domain parsing; unconstrained text columns |
| creator_stage_reviews.stage_name | RunStage values used by services | Unconstrained text column, no typed review model |
| creator_script_drafts.source_type | ScriptDraft.source_type | Domain parsing; unconstrained text column |
| workspace_members.role | WorkspaceMember.role | Domain parsing; unconstrained text column |
| creator_visual_plans.scenes_json | VisualScene.prompt_source, generation_status | Nested JSON domain parsing |
| creator_script_drafts.structured_script_json | VisualOverride.type | Nested JSON domain parsing |
| creator_artifacts.metadata_json | SubtitleArtifact.format | Nested artifact metadata parsing |
| creator_timelines.segments_json | MediaSegment (no Literal/StrEnum fields) | Nested Timeline domain parsing |

Numeric/shape/conditional CHECKs (dimensions, revisions, owned object key,
visual_asset scene ID, JSON object shape, singleton and nonnegative budgets)
are not enum allowed-set constraints. Editor command discriminators, deletion
policies and output presets are request commands, not dedicated persisted SQL
enum columns. No further constrained-domain mismatch remains at head 051.
BillingOutcome is a computed service result, not a persisted column.
RenderSegmentKind, AspectRatio, VideoCodec, AudioCodec and TrackRole belong to
compiled render/output models, not the saved Timeline table at this head.

## Media upload ownership and reconciliation

Every workspace media upload has a durable `creator_media_upload_intents` row
before object I/O. Its `owner_id` identifies one attempt; `workspace_id` and the
workspace-prefixed `storage_key` bind that attempt to exactly one object. The
upload-intent lifecycle is also the authoritative byte and asset-slot
reservation ledger used by workspace admission and reconciliation.

States and terminal policy:

- `pending` owns a key before upload; process death leaves it for reclamation
  only after its lease expires.
- `uploaded` means object I/O completed while metadata is not yet confirmed.
- metadata success, including a registered key found after an indeterminate
  commit acknowledgement, transitions to terminal `completed`; it is never
  deleted by reconciliation. This also preserves Timeline-referenced media
  because Timeline references resolve through persisted `MediaAsset` rows.
- validation failure occurs before intent/object creation and leaves no durable
  upload state. Cancellation before object I/O immediately compensates the
  intent and releases its byte and asset-slot reservation. Cancellation after
  upload is shielded through metadata completion. A hard process death at either
  point converges through the expired lease.
- a definite metadata rollback triggers compensation. Successful deletion ends
  in terminal `reclaimed`; a delete failure remains `cleanup_pending`.
- cleanup retries are bounded. Exhausted records end in terminal `exhausted`
  for operator action and remain durable rather than losing ownership evidence.

Sweepers claim expired rows with `FOR UPDATE SKIP LOCKED`, verify a same-workspace
`creator_media_assets.storage_key` reference before deleting, and update terminal
state idempotently. Ambiguous outcomes favor preservation: no object is deleted
until the lease expires and registration has been checked by exact key.

### Authorized Timeline reads

Project asset previews resolve persisted project/workspace ownership before
storage I/O. The recorded provider must match the configured singleton; mismatch
is 409, not automatic migration or local fallback. Local/S3/Azure adapters expose
`stat(key) -> ObjectInfo(size, etag?, version_id?)` and bounded ranged reads;
`open_read(key)` acquires an owned generation-bound session for an HTTP transfer,
with each read capped at 64 KiB and its response released before returning.
The API supplies same-origin GET/HEAD and single byte-range delivery; it never
uses whole-object `download_bytes` or exposes cloud URLs. Scene/workspace routes
and final-artifact downloads retain their existing contracts. See
[SECURITY.md](SECURITY.md#timeline-project-previews) for Range/error/cancellation
semantics and the required manual cloud acceptance checklist, and
[TIMELINE_PREVIEW_VERIFICATION.md](TIMELINE_PREVIEW_VERIFICATION.md) for Red and
mutation evidence.

## Workspace media capacity

Media uploads reserve their validated byte size and one asset slot on the durable
upload intent before object I/O. Admission checks `stored + reserved + incoming`
under a workspace lock. Success converts the reservation to stored metadata;
definite failures and cancellations before object I/O release bytes and the
asset slot immediately. Ambiguous process death retains capacity through a
bounded late-upload verification window. Once that window observes no object,
both byte and slot capacity are released while a zero-reservation
`late_upload_watch` tombstone remains durable to detect and remove a delayed
write. Repeated sweeps release capacity exactly once.

`MEDIA_MAX_STORED_BYTES`, `MEDIA_MAX_ASSET_COUNT`, and
`MEDIA_MAX_RESERVED_BYTES` default to `0` (unlimited). The authenticated
`GET /api/creator/workspaces/{workspace_id}/assets/storage-usage` response shows
stored and in-flight usage plus configured limits. Budget rejection is HTTP 413
with error code `STORAGE_BUDGET`. `MEDIA_TEMP_DISK_LOW_WATERMARK_BYTES` defaults
to `0`; when positive it blocks new upload staging without blocking reads,
downloads, or cleanup.

### Job free-space admission

Render, scene-image and audio jobs write media under `ARTIFACT_ROOT` and, while
they run, into the temp (scratch) directory. Before such a job's first write the
Worker compares the free space of each of those two filesystems with its
watermark and refuses the job if either is below it. Each check is off when its
setting is `0` or negative, which is the default.

| Filesystem | Setting | Floor it enforces |
| --- | --- | --- |
| Artifact: the filesystem that holds `ARTIFACT_ROOT` | `MEDIA_ARTIFACT_DISK_LOW_WATERMARK_BYTES` | Free bytes it must keep before a job may start |
| Scratch: system temp **and** `RENDER_SCRATCH_DIR` when set | `MEDIA_TEMP_DISK_LOW_WATERMARK_BYTES` | Each location must meet the same floor; upload staging still checks system temp per upload |

`.env.local.example` enables both for the local server. A floor above a
filesystem's free space refuses every job, so lower it on a small disk or a small
`tmpfs` temp directory. Only these two settings are read for this check. A
malformed value (for example `2GiB`) fails the job with an internal error that
names the setting; it is never read as zero.

The check itself never writes. A disabled check resolves nothing, the artifact
filesystem is checked before the temp directory is looked up, the temp directory
is resolved without `tempfile`'s probe file, and a directory that does not exist
yet is measured through its nearest existing parent without creating it. The
artifact floor is measured at `ARTIFACT_ROOT` itself; a different filesystem
mounted below it is not measured separately. Once the process has used
`tempfile`, its cached directory is measured exactly. On the process's first
admission the directory is chosen in `tempfile`'s order by existence and
permissions rather than by `tempfile`'s write probe, so a full or
inode-exhausted first candidate (`TMPDIR`) can make the check measure a
different filesystem than `tempfile` then settles on.

What a refusal does:

- **The job.** No directory, file or provider call is made, existing artifacts
  are never purged, truncated or rewritten to make room, and Celery does not
  retry the task.
- **Quota.** The reservation owned by the task is released when this was the
  task's first delivery. A retried delivery that is refused keeps whatever an
  earlier provider attempt already authorized, so that exposure stays charged.
- **Task and run.** The task is recorded as failed with the code
  `ARTIFACT_STORAGE_LOW` or `SCRATCH_STORAGE_LOW`, both retryable. The run moves
  to `FAILED` when it is in a stage this task may fail; a scene image
  regenerated while the run is in review leaves the run where it is, with only
  that task failed. Studio shows "Output storage is low" or "Temporary storage is
  low" with the recovery steps, not the shortfall.
- **Recovery.** Free space, then recover as after any other failed media step:
  the API can restart a `FAILED` run from a generating stage
  (`POST /api/creator/runs/{id}/restart`), and the step is started again. That
  creates a new task with a new quota reservation; a refused task is never
  resumed.
- **Log.** The admission check logs one warning per refusal with `filesystem`,
  `free_bytes`, `required_bytes`, `shortfall_bytes`, `task_name`, `run_id` and the
  measured `path`; failure handling and the dead-letter bookkeeping log
  separately. The exception text names the storage and the shortfall in MiB and
  no path.
- **Metric.** The counter `worker.job.low_space_refusals`, labelled with
  `filesystem` (`artifact` or `scratch`) and `celery.task_name`.
- **Unreadable disk.** If free space cannot be measured, the job is admitted and
  a warning with `measured=false` is logged; the write itself reports a real
  error.

Limits. The floor is an admission check, not a reservation: a job admitted just
above it can still run out of space, and jobs admitted together each see the
same free space (keep Worker concurrency low and the floor generous). It does not
coordinate with the upload byte lease that shares the temp setting. Text-only
tasks (script, visual plan and subtitle generation) and upload staging's artifact
side are not checked. The API does not check at enqueue time, so a refusal
appears in the run's task list rather than as an immediate HTTP error. How much
one run may write is not bounded by this check.

### Render scratch placement (#1029)

`RENDER_SCRATCH_DIR` optionally separates render scratch from published artifacts.
It must be an absolute writable/searchable directory with no symlink components,
`..`, newlines or NUL. Neither it nor `ARTIFACT_ROOT`/the published output directory
may contain the other; existing inode identities are compared to reject aliases.
The root must be a real directory owned by the worker uid, with **no group/world
write permission**. Every existing ancestor must be owned by root or the worker
uid, and must not be group/world writable unless sticky (so `/tmp` and root-owned 0755 mount
points remain valid). Missing components are created deliberately with mode 0700;
existing unsafe roots are rejected, never silently chmodded or adopted. Each run
directory is checked as a real worker-owned 0700 directory after allocation.
Invalid configuration fails closed before scratch use with an error naming the
setting; permission/path errors are configuration errors, while capacity/hardware
errors such as ENOSPC, EDQUOT and EIO remain operational OSErrors.
The admission check validates without writing and
measures its nearest existing ancestor if it has not been created yet. A mount
must already exist before admission to measure the intended filesystem. The
scratch floor checks **both** this setting and default system temp for media jobs.
Remote S3/Azure publication still spools uploads larger than 1 MiB to system temp;
it is not relocated by this helper. API upload/probe staging also stays on system
temp with its existing upload floor. Render and combined-audio generation validate
the setting because they allocate scratch. Jobs that do not use the helper (scene
images and individual paragraph audio) ignore an invalid render setting, but still
check default system temp and artifact capacity. A valid configured volume is
measured conservatively for all guarded media jobs.

Legacy segments (`.render_tmp_*`), segment-plan renders (`.render_plan_*`),
Timeline materialized sources/narration (`timeline-*`), audio concat lists and
SFX clips all allocate through `creator_service.render_scratch`. New directories
are private (0700); configured names include hashed host and run/output-directory
identity, PID and randomness. With the setting unset the three directory names
and locations remain unchanged beside the output; the auxiliary audio concat
file and SFX directory keep their existing system-temp placement. Successful
and ordinary failed renders retain their existing cleanup behavior. Retained
legacy `bgm.mp3`, mixed/normalized audio and styled subtitles are artifacts of
the existing workflow, not transient scratch, and do not move.

The temporary `output.tmp.<pid>.<random>.mp4` **always stays beside the output**
for atomic rename on the same filesystem. Published names, storage keys, codecs
and FFmpeg arguments do not change. API `media-upload-` staging and byte-probe
temporary files are explicitly allow-listed by the AST guard; render source and
output probes operate on paths without that staging.

An SSD reduces segment/materialization I/O latency while artifacts can stay on
an HDD; scratch on the HDD saves SSD capacity but shares its seek/I/O load. A
`tmpfs` avoids disk I/O but consumes RAM and must be sized with room above the
scratch floor (a full or undersized tmpfs can still fail mid-render). For example,
an **optional operator-owned Compose override**, not a change to the shipped stack:

```yaml
services:
  worker:
    environment:
      RENDER_SCRATCH_DIR: /render-scratch
      MEDIA_TEMP_DISK_LOW_WATERMARK_BYTES: "1073741824"
    volumes:
      - /srv/ssd/ai-video-render:/render-scratch
    # Alternative to the bind above (do not use both at the same target):
    # tmpfs:
    #   - /render-scratch:size=4g,mode=0700,uid=1000,gid=1000
```

For a host bind, create the host directory with mode **0700** and chown it to the
worker UID/GID (**1000:1000 in the shipped images**); adjust these IDs for a custom
image. Do not use a shared group/world-writable bind source. Configure/mount the same path in the API process
when using lightweight synchronous rendering. Do not serve or upload this root.
The configured root and its ancestors are operator-controlled: do not let other
users rename/replace them during rendering. Per-run caps (#1031), concurrency
changes and live-host measurements remain separate work.

### Render crash cleanup (#1030)

Where the pool enforces time limits, the render task's existing `production_task` hard-timeout hook runs in Celery's
main process after timeout finalization. It removes recognised scratch belonging
to the killed pid, and that pid's `output.tmp.<pid>.<hex8>.mp4`, immediately in
both layouts. It examines only the run's `<ARTIFACT_ROOT>/<run_id>/render/`
direct children and, when configured and trust-valid, the scratch-root direct
children. Configured names must also match this machine's hashed
`socket.gethostname()` and the run/output-directory hash. Celery node aliases
are not machine identity. It leaves another pid, host or run's scratch alone.
Published `output.mp4`, retained audio/subtitles and unrelated names are never
candidates. A missing directory or concurrent removal is harmless, and failures
are logged without escaping the timeout callback.

The shipped base worker and scaled render worker use `--pool=solo`. Celery time
limits are **not enforced** there, so the hard-timeout callable does not fire;
the Worker-start sweep is the primary reclaim path after a crash. See
[pool support in the shipped topology](TIMEOUT_RETRY_POLICY.md#pool-support-in-the-shipped-topology).
The maximum-age floor of twice the render hard limit assumes enforced limits.
Keep the configured age well above the longest legitimate render (default 3600
seconds); do not set it near 1320 seconds on a solo pool.

Pid-only names include legacy scratch directories AND the partial
`output.tmp.<pid>.<hex>.mp4` in EITHER layout, inside one run's render directory.
Their timeout attribution uses the pid only. A sibling container retrying the same run with a colliding pid
in the timeout-finalization-to-cleanup window could lose its scratch or partial
output. This requires unlikely timing but is not excluded by pid-only names.

Legacy unset-mode `timeline-<random>` has no owner information: the timeout hook
**never removes it**. It waits for the next Worker start and the maximum-age
rule. This intentionally leaves the unconditional "no scratch after hard timeout"
acceptance wording only partially met. Combined-audio timeout cleanup is not
wired here; its configured scratch can be reclaimed at Worker start, while its
system-temp concat list and `sfx_` directory remain outside this cleanup scope.

At `worker_ready` (main process, once per Worker start), an injectable sweep
inspects the immediate `<ARTIFACT_ROOT>/<numeric run id>/render/` children and the top level of
a valid configured scratch root. It never consults DB state or descends into
published directories. Run names must contain only ASCII decimal digits and be
at most 20 characters long; arbitrary top-level names such as `workspaces` are
excluded entirely. An invalid configured root is skipped with one warning;
the artifact-tree sweep can still proceed. This is **startup only**, not a
periodic/Beat sweep and not an API/lightweight startup hook.

Eligibility requires an exact recognised name and the expected real directory
type (regular file only for a temporary MP4). Symlinks at the entry, run, or
`render` level are skipped and logged once per sweep. The operator artifact root
itself may be a symlink/mount; all levels below it are descriptor-relative and
no-follow. Directory deletion uses fd-based `rmtree`, and MP4 unlink uses the
confined deletion primitive. Neither operation traverses a symlink target.

Age is `max(0, now - max(lstat.mtime, lstat.ctime))`, using one wall-time snapshot
per sweep. A future timestamp keeps an entry fresh; mtime alone never proves
owner death. Either of these conditions permits reclamation:

- **Owner gone:** only a configured name with this host hash, a valid pid
  (1 through 4194304), a failed liveness probe proving absence in this pid
  namespace, and age at least `RENDER_SCRATCH_SWEEP_MIN_GRACE_SECONDS` (60).
  Permission and other probe errors conservatively count as alive.
- **Maximum age:** every recognised pattern qualifies at
  `RENDER_SCRATCH_MAX_AGE_SECONDS` (3600). `0` disables this rule entirely.
  A positive value below twice the real render task hard limit is clamped up
  with one warning (currently 1320 seconds = 2 × 660). The worker computes this
  floor; creator-service has no worker import.

**The owner-gone rule assumes ONE pid namespace per `socket.gethostname()`.**
Containers sharing a hostname but not a pid namespace are NOT supported for
this rule: host networking, `--uts=host`, or replicas with an identical explicit
`hostname:` can create this topology. A probe in one container can then report
a live sibling's pid absent and delete its scratch after the 60-second grace.
The shipped Compose files set neither host networking/UTS sharing nor an explicit
hostname. Keep hostnames unique across independent pid namespaces. Otherwise,
set the grace above every legitimate render lifetime and choose the age rule
accordingly; age zero alone does not disable owner-gone reclamation. Disable
the startup sweep if those lifetime bounds cannot be guaranteed.

Other-host entries and legacy pid-only names (including partial MP4s) use **only
the age rule**: a shared artifact volume may belong to another container with
the same pid. Ownerless legacy Timeline scratch uses age only too. Thus a crash
right before a restart is immediately reclaimable only for configured same-host
scratch whose owner is gone **after the grace**. Fresh legacy or sibling-host
leftovers wait for age and a subsequent restart. Pid reuse is conservatively
alive for owner-gone checks, but maximum age is a bound on legitimate lifetime,
not a liveness inference: an over-age entry can be removed even when its pid is
alive. A live Worker's scratch is protected **while younger than the effective
maximum age**. Setting age to zero preserves unknown/remote owners indefinitely.
After restarting the same container, its pid namespace starts again and the new
Worker usually reuses the crashed Worker's pid. The owner-gone rule rarely helps
in that case; the age rule is the usual reclaim path.

| Setting | Default | Scope |
| --- | --- | --- |
| `RENDER_SCRATCH_SWEEP_ENABLED` | `true` | Case-insensitive `1/true/yes` enables; `0/false/no` disables startup cleanup, not the timeout hook |
| `RENDER_SCRATCH_MAX_AGE_SECONDS` | `3600` | Last-change lifetime; `0` disables age reclamation |
| `RENDER_SCRATCH_SWEEP_MIN_GRACE_SECONDS` | `60` | Minimum age for a proven same-host dead owner |
| `RENDER_SCRATCH_SWEEP_MAX_RUN_DIRECTORIES` | `1000` | Newest numeric run directories inspected per boot; nonnumeric names are excluded |
| `RENDER_SCRATCH_SWEEP_MAX_ENTRIES` | `10000` | Independent recognised direct-child candidate budget for each root; unrelated names consume no entry slots |
| `RENDER_SCRATCH_SWEEP_MAX_REMOVALS` | `500` | Successful entry removals |
| `RENDER_SCRATCH_SWEEP_BUDGET_SECONDS` | `20` | Cooperative monotonic wall-time budget |

Each integer setting is parsed independently: ASCII decimal digits, surrounding
whitespace ignored. Signs, underscores, non-ASCII digits and other invalid values
(including values too large for integer conversion) fall back to that setting's
default with one warning naming the setting.
Zero is valid only for maximum age; all other integer limits must be positive.
An invalid enabled value falls back to enabled with one warning.

The configured scratch root is swept **first**, then the artifact tree, each with
its own entry budget. Only removals and wall time are global stop conditions.
Artifact-root discovery selects the newest `max_run_directories` numeric ids
while scanning within the time budget, retaining only a bounded heap of names.
Older runs' leftovers wait for retention deletion of their run directory while
they remain outside that newest-run window. Child discovery filters exact
recognised patterns without syscalls and retains only the first 10000 candidates
in sorted order per directory. Unrecognised names are counted, not retained, and
cannot starve scratch candidates. Candidate overflow is reported before continuing to the next
run. Discovery or entry limits in one root never stop the other root.
Distinct bounds are logged once and local bounds identify their root, such as
`scratch:max_entries` and `artifact:max_run_directories`.
Entry exhaustion is reported only when an unexamined candidate remains.
Skip counters and warn-once state are also root-qualified, for example
`scratch:root_error` and `artifact:root_absent`.
Timeout cleanup has a
shared 10000-entry budget and a two-second cooperative deadline across its two
locations. These budgets stop **between** filesystem operations: a blocked
filesystem syscall or a recursive delete already in progress cannot be
preempted. They are not hard realtime guarantees on a broken filesystem.
An INFO summary reports examined/removed/skipped counts and elapsed time, with
one INFO per removal containing only the scratch entry name and run id, never
media payload names. No recursive size scan is performed to count freed bytes.
Concurrent disappearance counts as vanished, not failed deletion. A second
sweep is idempotent absent newly eligible entries. Bounds can leave eligible
files for another Worker start; no storage-wide deletion guarantee is claimed.

### Worker-output scope

This change does not make `creator_scene_assets` or `creator_artifacts`
canonical `MediaAsset` records. Worker-generated scene images, paragraph and
run audio, subtitles, and rendered videos therefore remain outside the
workspace byte/asset budgets, upload-staging admission control, and
`MediaAsset` retention sweeps described here; their media-writing jobs are
admitted by the free-space check above instead. Their existing artifact
lifecycle continues to apply.

Canonical worker publication requires one durable operation that couples an
idempotent task-delivery/output-role identity, the canonical object and
`MediaAsset`, and the workflow alias. Cancellation, alias-write failure,
run/project deletion, and a crash between object and metadata deletion must all
converge without double charging or deleting an object still owned by canonical
metadata. That lifecycle is intentionally deferred to a follow-up issue rather
than partially implemented in this change.

### Scene upload cleanup intents

Before each worker scene-image upload, a durable cleanup intent is written to
`creator_artifacts` (`artifact_type='visual_asset'`,
`metadata_json='scene_upload_cleanup_intent'`, the exact `<run_id>/scenes/<name>.png`
key, the recorded provider and the local source path). No migration is involved.
Intent rows are bookkeeping, not artifacts: download-by-id answers 404 for them and the
admin stage inventory ignores them. The persisted `scene_id` is the tail of the generated
filename stem, bounded to the `VARCHAR(100)` column.

- **Registration** takes `FOR SHARE` on the project and run and is refused once the project
  is `deleting` or the run `cancelled`; the batch then cancels quietly before any storage
  I/O, so no upload can start behind project deletion's artifact enumeration.
- **Acceptance** (the scene-asset INSERT) uses one lock order for the intent-id and the
  key-only paths: project, run, intent `FOR UPDATE NOWAIT`, then the scene advisory lock.
  The idempotency lookup runs under the scene lock, so a replay returns the winner untouched
  and a losing delivery keeps its own intent (its object stays reclaimable). The intent is
  consumed in the same transaction as the INSERT. A fenced or busy intent rejects acceptance
  rather than queueing behind storage I/O while parent share locks are held; a missing intent
  rejects an acceptance that names one.
- **Reclamation** (the artifact retry task) makes an intent eligible five minutes after
  creation. A first transaction commits a fence (`delete_failed_at` set,
  `delete_error='scene_upload_reclaim_started'`); a second deletes the object through its
  recorded provider and the local source, so a crash after remote deletion can never leave a
  claimable row whose object is gone. Ordinary rows, including ones with `NULL` metadata,
  are eligible immediately.
- **Late-write watch.** A reclaimed, unresolved intent is kept fenced for
  `LATE_UPLOAD_WATCH_RETENTION` (the media pipeline's policy) and swept once more before it
  is erased. Acceptance of that key is rejected meanwhile, whether or not it names the
  intent. An intent whose object was accepted, or that a finishing worker compensates itself,
  is simply removed.

Local-file deletion is root-anchored. Recorded source paths must lie strictly beneath
`ARTIFACT_ROOT` and are unlinked descriptor-relative with `O_NOFOLLOW`, so a directory
swapped for a symlink after validation cannot redirect the delete; `LocalStorageBackend.delete`
uses the same primitive and rejects keys containing `..` or starting with `/` instead of
re-rooting them (callers translate legacy absolute keys to root-relative ones first).
`ARTIFACT_ROOT` must therefore be identical for the API and the worker: a mismatch fails
closed, leaving the row to be retried until `max_retries`.
Symlinked intermediate directories under `ARTIFACT_ROOT` are unsupported for deletion: local deletion fails closed with `StorageDeletionError` and is retried, then abandoned; only `ARTIFACT_ROOT` itself may be a symlink.

Residual risk, tracked in #1004: the watch bounds a late write, it does not rule one out. A
remote-object-store upload that outlives the watch **and** whose worker dies before
compensating can leave an object with no record, and project deletion erases unresolved
intents immediately. The local filesystem backend cannot write late. Closing this fully
needs an uploader acknowledgement and a fail-closed deletion path; until then it gates any
remote object-store deployment.

## Media retention safety

New workspace asset preview generations rely on `_store_asset` creating fresh UUID
workspace asset keys: reserve upload intent → upload → persist metadata. They
do not rely on atomic local publication. Local uploads and STT/run-artifact
writers retain origin/main's in-place inode/permission semantics; regeneratable
run paths are outside the new UUID namespace; authorized legacy project rows may
still reference non-workspace keys with weaker provenance and no write-once promise.
The retained local descriptor
survives atomic replace and verifies detectable dev/ino/size/mtime/ctime changes.
Filesystem write access means full control of served bytes; timestamp-restored
or timestamp-granularity in-place rewrites are outside this threat model. See
[writer-contract audit](TIMELINE_PREVIEW_VERIFICATION.md#writer-contract-audit).

MediaAsset retention is separate from artifact expiration. It defaults to
disabled (`MEDIA_RETENTION_ENABLED=false`) and dry-run
(`MEDIA_RETENTION_DRY_RUN=true`); all original, generated-intermediate, and final
video windows default to `0`, so no arbitrary age policy is activated. Positive
windows are configured independently with `MEDIA_RETENTION_ORIGINAL_DAYS`,
`MEDIA_RETENTION_INTERMEDIATE_DAYS`, and `MEDIA_RETENTION_FINAL_VIDEO_DAYS`.

Candidates must also pass `MEDIA_RETENTION_GRACE_DAYS` (default 7), exact
workspace-key ownership, current Timeline asset references, and normalized
asset mappings for approved Timeline revisions. A workspace with any unresolved
historical Timeline approval remains report-only until repaired. Dry-run reports
candidates without deleting. In destructive mode,
object deletion happens before metadata deletion; a storage deletion failure
therefore leaves both metadata and accounted capacity intact. Deletion is
irreversible once the object backend confirms removal, so operators should review
dry-run output and backups before setting `MEDIA_RETENTION_DRY_RUN=false`.

## Project settings (slice A of #882)

`creator_project_settings` stores explicit settings as required JSONB (JSON on
SQLite), with project_id primary key/cascading FK, schema_version
and timestamps. No row preserves historical rendering; there is no backfill.
`creator_projects` gains no columns: old SELECT-star/forbid-extra readers continue
to work after migration and app-only rollback. Effective API resolution is
read-only and does not write defaults. Both project adapters persist explicit
typed settings and atomically replace them under workspace scope plus the
authorized project row's `updated_at` compare-and-swap, preserving status/source/title.
PostgreSQL advances that row and upserts settings in one transaction; failed CAS
writes neither. Project deletion cascades settings; memory deletion mirrors it.
Scope derives exclusively from the project's workspace_id. Settings has no duplicate
ownership column, so project workspace moves cannot leave contradictory ownership.
See [the API contract](PROJECT_SETTINGS_API.md) and
[ADR-008](adr/008-typed-project-settings.md) for the three-slice delivery.
Approval snapshots/digests and worker consumption are slice B, not implemented
by this storage change; old run/dispatch/profile contracts remain unchanged.
