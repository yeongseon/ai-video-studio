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

### Worker-output scope

This change does not make `creator_scene_assets` or `creator_artifacts`
canonical `MediaAsset` records. Worker-generated scene images, paragraph and
run audio, subtitles, and rendered videos therefore remain outside the
workspace byte/asset budgets, temporary-disk admission control, and
`MediaAsset` retention sweeps described here. Their existing artifact lifecycle
continues to apply.

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
