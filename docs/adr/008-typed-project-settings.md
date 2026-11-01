# ADR-008: Typed content settings and the approval-scoped output contract

Status: Accepted design; slice A implemented, slices B/C deferred under #882.

## Context

Content format, target duration, geometry and encoding are independent concepts
([media core architecture](../architecture/SHORT_FIRST_MEDIA_CORE.md)). Landscape
does not imply long-form. Current preview/approval/worker paths use different
legacy defaults; persisting choices alone must not silently change rendering.

## Decision and delivery slices

1. **A: typed persistence and API (this PR).** Domain `ProjectSettings` is frozen,
   forbids extra fields and stores `schema_version=1`, `content_format`,
   `output_preset`, `encoding_profile`, `target_duration_seconds`. Only `short` is
   enabled by `SUPPORTED_CONTENT_FORMATS`; `long` returns “not available yet”.
   Preset resolution delegates geometry to `OutputSpec.preset` and encoding to
   `EncodingProfile.by_name`. Duration is optional, strictly numeric, finite and
   positive, with no hard maximum (180 seconds is product guidance, not a cap).
   Migration 051 adds nullable JSON/JSONB without default or backfill. Memory and
   PostgreSQL preserve the same scoped replacement and timestamp CAS contract.
   See [project API](../PROJECT_SETTINGS_API.md).
2. **B: approval snapshot and worker output (after A and #901).** Preview resolves
   effective settings and returns them plus a canonical versioned settings digest.
   Approval carries the expected digest and Timeline revision. Under the existing
   approval transaction/fences, recompute and compare; mismatch is 409 and requires
   re-preview. Persist the complete resolved output/encoding/duration contract and
   digest in review/dispatch metadata atomically with approval. The worker renders
   exclusively from this immutable approval snapshot, never mutable project
   settings or a legacy-profile argument for new-contract approvals. Validate
   snapshot version and digest before materialization; refuse unknown versions.
   Real ffprobe fixtures prove square/landscape/vertical and encoding selection,
   preserving authorization, cancellation, revision and task ownership fences.
3. **C: generation and Studio.** Carry persisted duration/format into generation
   prompts and recipe planning, including retries. Studio edits this same server
   contract, reloads it, shows supported formats and conflicts, and binds preview
   selection to approval. Target duration remains an intent, not a guarantee of
   achieved duration; render policy must describe its effect explicitly.

## Legacy compatibility

NULL means historical behavior, not an explicit standard preset. Pure resolution
returns settings plus `source=legacy_default`; explicit stored settings return
`source=explicit`. Legacy output is 1080x1920 at 30 fps, H264/AAC, CRF 18, `fast`.
`standard` is CRF 23/`fast`; `high_quality` is CRF 18/`slow`. Neither equals legacy.
The resolver therefore emits read-only `encoding_profile=legacy_default`; the
domain exports its numeric encoding separately. Clients cannot persist that name.
`shorts_default` remains an unchanged render/quality compatibility alias.

Slice A does not read project settings in rendering, dispatch, workers, prompts,
or Studio. Old projects and explicit-settings projects still render through the
existing profile contract until B. Old runs/reviews/tasks/queue messages missing a
snapshot retain the historical adapter/profile path. New snapshot-bearing tasks
must not silently fall back when a snapshot is malformed or unsupported. Settings
edits cannot alter already approved snapshots; re-rendering changed choices needs
new preview/approval. Historical stored profiles are not rewritten or relabeled.

## Versioning, security and deployment

Unknown versions/fields are rejected rather than truncated. Future versions need
explicit parsers/upgraders and deployment ordering: migrate schema first, deploy
readers that understand the version, then enable writers. Old binaries must not be
asked to read future settings. Downgrade removes settings only; export explicit
choices before rollback if operators need to restore them later. Rolling back to
an old application loses the settings API, but not legacy render compatibility.

Settings contain no secrets, URLs, ownership IDs, codec parameters or approval
state. Project access is authenticated and workspace scoped; foreign/missing IDs
return 404. Replacement is atomic against the authorized row's `updated_at`;
concurrent row changes cause 409 rather than overwriting status/title. The flat
PUT body has a 4096-byte receive budget and field-level 422 validation.

## Non-goals

No long-form enablement (#883/#885/#890), per-segment speed, new renderer,
provider calls, geometry duplication, automatic approval/publication, settings
backfill, mutable worker reads, or Slice B/C behavior in Slice A.
