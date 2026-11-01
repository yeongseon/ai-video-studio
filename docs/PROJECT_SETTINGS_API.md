# Project settings API (slice A of #882)

Creator routes require the existing API-key authentication. Ownership is checked
with `require_project_access`; foreign/missing project IDs return 404, missing
authentication returns 401. Workspace IDs are server-owned, never settings input.

## Creation and reads

`POST /api/creator/projects` accepts optional `settings` alongside existing fields.
Omitted/null settings create no settings row, with no backfill. Creation, GET detail/list,
PATCH title and PUT settings responses add effective `settings` and
`settings_source` (`explicit` or `legacy_default`) while retaining existing fields.

```json
{
  "schema_version": 1,
  "content_format": "short",
  "output_preset": "short_square",
  "encoding_profile": "high_quality",
  "target_duration_seconds": 240
}
```

Explicit defaults: version 1, short, short_vertical, standard, null duration.
Supported output names: short_vertical, short_square, short_landscape. Encoding:
preview, standard, high_quality. Unknown names return field-level 422 listing
supported values. `long` is defined but not available yet. Duration is a positive
finite JSON number or null; booleans/strings, NaN and infinity are rejected.
240 and 3600 seconds are valid; there is no 180-second hard cap.

## Replacement

`PUT /api/creator/projects/{project_id}/settings` takes the settings object directly
(not a `settings` wrapper) and replaces the whole value; omitted fields resolve
to explicit defaults. It returns the updated project, not a render result.
The body must be a flat object of the five documented fields, at most 4096 bytes.
Creation is bounded at3,157,056 bytes before parsing, covering worst-case escaped
astral source/title/URL characters:12*(5000+50000+200000+200+2000)+4096 settings
+1024 JSON framing+65536 whitespace. Non-string whitespace is separately capped
at65536 bytes; whitespace inside source strings is content under character limits.
Both budgets count streamed chunks without Content-Length; oversized requests
return field-level 422 before writes. An explicit quote/escape-aware scanner rejects
JSON nesting above 16 containers before parsing with a field-level 422.
Extra/reserved/nested fields and oversized bodies return 422. There is no reset-to-
legacy PUT: null is not a settings object, and `legacy_default` cannot be written.

An atomic workspace-scoped comparison to the authorized row's `updated_at`
prevents a concurrent project mutation from being overwritten. A race returns
409 (reload and retry); a deleted/foreign row returns 404. Settings replacement
does not change title/source/status, Timeline revision, run state or approvals.

Legacy reads emit vertical output and read-only `encoding_profile=legacy_default`
(H264/AAC, CRF18/fast at30fps), not standard (CRF23/fast). Source provenance is
explicit so clients can distinguish historical compatibility from saved choices.

**No render behavior changes in slice A.** Selection is persisted but not yet
consumed by preview/approval/worker or generation/Studio. The [ADR](adr/008-typed-project-settings.md)
defines B (digest-bound approval snapshot and worker matrix) and C (generation/UX).

Version is strict: only JSON integer1 is accepted; true,1.0 and "1" are rejected.
Python Decimal duration input normalizes to float; JSON durations remain numbers.
Archived/deleting projects accept settings replacement (no run/approval transition).
Deploy migration before new readers: schema050 lacks the table and returns a
sanitized500 without SQL text; do not start new readers against unmigrated databases.
Settings ownership is derived only from creator_projects.workspace_id; moves follow
the project, and settings disappear with project deletion. Existing workspace
deletion constraints are unchanged (occupied-workspace deletion can be blocked).
