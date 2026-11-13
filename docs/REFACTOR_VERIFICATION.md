# Canonical first-Short refactor verification

This records local verification of the refactor based on commit `0700ace`.
It is not a claim that these changes are merged or deployed, nor a measured
time-to-first-video KPI.

## Architecture

- Media metadata uses PostgreSQL when `DATABASE_URL` is configured. Migration
  `034` adds the media table; `035` adds a non-unique personal-key user index.
  Existing visual assets and historical migrations are preserved.
- Timeline-backed runs require `TIMELINE_REVIEW`, explicit approval of the saved
  revision, and a matching persisted approval record in the worker. Generic run
  metadata cannot forge the routing/approval fields.
- Preview and Timeline rendering use the same compiler. Storage keys are
  materialized only after workspace/project ownership and path validation.
- The old generated workflow remains supported by an isolated legacy render
  adapter, including paragraph audio, BGM, SFX, loudness, and styled subtitles.
- Demo seeding creates real generated and uploaded-example PNGs with synthetic
  CC0 provenance, then a saved Timeline, then the review-paused run. It makes no
  paid provider calls or automatic approval/publication.
- Provider status derives from shared facts: configured credentials are not
  verified health; offline placeholders are available; keyless remote providers
  remain unknown until checked; unsupported private providers are explicit.
- Provider contracts and exceptions live in domain. Application adapters own
  worker imports, broker dispatch, and cancellation. ADR-001 has no exemptions.

## Runtime evidence

`tests/integration/test_first_short_runtime.py` starts separate real API and
Celery processes against disposable PostgreSQL and Redis, without replacing
seed, authentication, storage, or FFmpeg with mock implementations. It checks:

1. Database-backed authentication and unauthorized 401.
2. Offline demo creation and persistent media/Timeline state.
3. API process restart, then successful reload and authenticated media access.
4. Stale revision 409 and foreign workspace preview/approval/download 404.
5. Explicit approval creates one review; worker renders the approved revision.
6. Downloaded MP4 has a positive duration according to ffprobe.
7. Explicit final approval moves the run to `PUBLISHED`.

Each run uses its own database schema and Celery queue. The test does not use a
wall-clock threshold as a product KPI. The broker/API/worker run completed in
approximately 11–14 seconds on the development host, excluding installation.

Additional execution coverage:

- `test_postgres_media_asset_storage.py`: separate-process reads, JSON metadata,
  ownership, listing/pagination, and legacy-data preservation against PostgreSQL.
- `test_studio_proxy_runtime.py`: actual nginx template injects the runtime key,
  does not expose it in the served page, and blocks foreign-origin mutations.
- `test_authorized_media_runtime.py`: actual nginx template, API and PostgreSQL in
  development and production mode serve scene, artifact, workspace and project media
  through authorized identity routes with exact bytes and MIME types, return 404 for
  absent and foreign objects, and never serve raw artifact paths. CI runs it in the
  `first-short-runtime` job and `test_ci_media_runtime_gate.py` pins that it does.
- `test_quickstart_execution.py`: actual Compose configuration smoke in a fresh
  temporary checkout without a local `.env`.
- Worker tests render real image/video segments, narration and subtitles.
- `test_output_preset_ffmpeg.py`: actual square and landscape MP4s across all
  three encoding profiles, checked with ffprobe.

## Browser verification

The production frontend build was exercised against the real local API/worker.
The browser driver forwarded same-origin API traffic with a synthetic proxy key;
the nginx key-injection behavior was independently checked by the proxy test.

The browser displayed the offline plan, created a demo, loaded protected image
media, approved the shown Timeline revision, played the rendered MP4
(`readyState=4`, duration approximately 3.97 seconds), requested a download, and
explicitly published. Settings displayed real provider status responses.
Create, project, review, and settings surfaces were checked at 375/768/1280px;
no horizontal overflow remained. Two independent visual reviewers approved the
changed surfaces. This does not claim a complete accessibility/performance audit.

## Remaining issue disposition

Implementation in this branch is not the same as closing an issue on GitHub.
Issues should be closed only after the changes land and their acceptance scope
is confirmed.

| Issue | Local implementation / verification | Remaining limitation |
|---|---|---|
| #619 | One membership query; failing-then-passing regression | Requires merge |
| #620 | Auth database failures consistently return 503 | Requires merge |
| #575 | Multiple personal keys and user/key/workspace audit attribution | OAuth/OIDC runtime remains a roadmap; a shared proxy key still identifies its owner |
| #621 | AST dependency gate, zero violations, CI wiring | Requires merge |
| #581 | Release suffix, Vite healthcheck, user-key index, historical-test clarification | Already-correct documentation was not rewritten |
| #622 | Media, dispatch, render and frontend modules split by responsibility | Ongoing maintenance; not every historical large module was rewritten |
| #634/#635 | Real square/landscape render profile matrix | Requires merge |
| #696/#697 | Provider Settings consumes truthful states, refresh/retry, read-only server-key guidance | No browser key editor or request-time secret writes by design |
| #698/#699 | Real mixed-provenance offline sample and explicit review/render/download | This is not AI generation or a live paid-provider benchmark |
| #700 | Duration presets + Custom, observable onboarding milestone, Timeline continuation | No claim of exhaustive usability validation |
| #701 | Safe error parsing, actionable failures, bounded HTTP/DLQ serialization | Redaction is not universal detection of every possible secret |
| #702 | Walkthrough corrected; actual automated and browser execution documented | Fresh-user under-10-minute KPI has not been measured |
| #576 | Opt-in `script-timestamps` path uses Edge TTS provider-native word boundaries and exact approved-script text | Limited to newly generated Edge TTS audio; no cross-provider aligner or Korean timing benchmark |

For #576, the default remains `whisper-small`. Selecting `script-timestamps`
fails closed when provider-native word boundaries are absent or do not match the
approved script; it never invents proportional timings. The remaining real-audio
Korean benchmark and non-Edge provider support keep the issue open.

## Reproduce

Latest local regression results: package suites **1,701 passed / 8 optional DB
tests skipped**, API **823 passed / 1 optional test skipped**, worker **294 passed**,
frontend **728 passed**. The optional PostgreSQL tests were then run with the
disposable DB together with full runtime and nginx checks: **10 passed, zero
skipped**. Architecture/doc/bootstrap checks: **79 passed**. Application/package
Ruff, basedpyright, TypeScript and Vite passed; ESLint retains 13 pre-existing hook
warnings. Independent approval, persistence/security, authentication, dispatch,
provider-contract, and visual reviews passed in their stated scopes.

Use the repository Python environment and its package paths. Point the variables
below at disposable services only, never production:

```bash
python3 scripts/check_package_dependencies.py
python3 -m pytest packages/creator-domain/tests packages/creator-provider/tests packages/creator-service/tests -q
(cd apps/api && python3 -m pytest tests -q)
(cd apps/worker-orchestrator && python3 -m pytest tests -q)
npm --prefix apps/studio-web test
npm --prefix apps/studio-web run build
python3 -m ruff check apps packages tests/integration
basedpyright apps packages tests/integration

MEDIA_ASSET_TEST_DATABASE_URL="$TEST_DATABASE_URL" \
FIRST_SHORT_TEST_DATABASE_URL="$TEST_DATABASE_URL" \
FIRST_SHORT_TEST_REDIS_URL="$TEST_REDIS_URL" \
python3 -m pytest packages/creator-service/tests/test_postgres_media_asset_storage.py tests/integration -v
```

Do not report optional integration-test skips as successful runtime checks. API
and worker test trees should be run separately to avoid their existing conftest
package-name collision. LSP access to the external worktree was unavailable in
the agent harness; standalone basedpyright and TypeScript builds supplied type
diagnostics instead.
