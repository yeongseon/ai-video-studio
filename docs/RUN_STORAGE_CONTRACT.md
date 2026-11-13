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
| Scratch: the temp directory | `MEDIA_TEMP_DISK_LOW_WATERMARK_BYTES` | The same floor for jobs; upload staging also uses it, per upload |

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
appears in the run's task list rather than as an immediate HTTP error. Where
scratch lives and how much one run may write are not changed here.

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
