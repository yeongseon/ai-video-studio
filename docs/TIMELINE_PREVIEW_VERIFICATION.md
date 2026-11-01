# Timeline provider-neutral preview verification (#900)

## Local writer audit

| Writer | Published-key concurrency / policy |
|---|---|
| `object_storage.py::LocalStorageBackend.upload` | All canonical MediaAsset writes, including same-source artifact uploads, stage complete bytes in same-directory temporary file, fsync, atomic replace; collisions preserve old reader inode. Failures clean temporary files and preserve old data. |
| `media_asset_service.py::_store_asset`, `media_file_ingestion.py` | UUID workspace keys and private upload staging; metadata registration follows adapter publication. No existing published key is written in place. |
| `generate_scene_image.py:161,223`, `scene_batch_lifecycle.py::upload` | UUID/owner-specific output names before asset registration; provider retries select new names. Completed files use adapter publication. |
| `generate_audio.py:168-171,209`, `generate_paragraph_audio.py:101` | Delivery-specific directories; active output is unpublished, immutable completed files are registered after provider result. No previously published delivery key is reused by a successor. |
| `generate_subtitles.py:94`, `generate_paragraph_subtitles.py:85` | Fixed subtitle keys can be served during regeneration. Whisper/Groq STT writers now use `stt.atomic_file.publish_text`: same-directory temporary + fsync + replace. Adapter upload likewise atomically republishes. |
| `ffmpeg_service.py::render` (319-478), `render_video.py`, `timeline_render.py` | FFmpeg encodes unique temporary final output and atomically renames at success; published `<run>/render/output.mp4` is never FFmpeg's in-place encoding target. Completed outputs go through adapter publication. No render integrity code changed (#901 lane). |
| FFmpeg segments/concat lists, merged SRT/ASS, narration mixing | Private unique render temporary directory or preparatory render inputs, never registered as downloadable output identities during write. Final video only registered after atomic render success. |
| `render_segments.py::render_plan` | Direct-output helper has no production publication caller; repository callers are render unit tests. Not an authorized runtime artifact publisher; callers must use atomic publication if added. |
| `render_materializer.py:39`, probe/upload staging | Private TemporaryDirectory/materialization/probe files, never registered as public artifact identity; scope ends with task. |
| `scripts/generate_ssul_shorts.py`, `scripts/image-gen/*` | Offline generation/gallery tools; no canonical MediaAsset or run-artifact registration. Do not run offline writers against live registered keys. Restore tooling runs in isolated/maintenance restore targets, not concurrent service publication. |

Immutable local publication is an operator/runtime contract, not protection
against arbitrary external in-place writes or network filesystem semantics.
ctime checks are defense in depth, with coarse-timestamp simulations proving
independent size/ctime comparisons. Tests retain readers/responses and assert
EBADF immediately on normal completion, failure, in-flight disconnect, HEAD,
empty GET and constructor failure; garbage collection cannot mask leaks.

## Oracle round-2 remediation

Red `77533c2`:9 failures/5 pass (in-place publisher, constructor leak, HEAD/empty
500). Descriptor-close mutation:5 retained-reader cases fail (GC independent).
Red `371a866`:cloud conditions2 failures; changed-ctime mutation2 failures.
Fixes `ddaf195`/`ff1425d`/`6cf26db`:atomic publisher, constructor close, no-body
mapping, condition classification. Fixed-key subtitle audit Red `de5854a`:4
failures; Green `699409e`:4 passes. Fresh-interpreter pipeline logging12 passes
with root/lastResort/warning/stderr capture; guaranteed emitters explicitly scoped.

Rerun mutations: close→pass, in-place upload, HEAD verify unmapped, drop ctime,
drop size with restored timestamps, short/over-read rejection, bounded read,
SDK suppression, S3 IfMatch, Azure match_condition, project access/workspace
lookup, early backend call, MIME, nosniff and private/no-store all fail targeted
assertions. Exact-length alone still gives3 intended transfer failures. All
mutations restored. Swallowed midstream storage error remains incomplete by
Content-Length: the real-HTTP invariant is tested, without claiming this harmless
implementation mutation must fail. Post-pread verification remains in production.

## Oracle round-1 remediation

Oracle rejected c73e172 at 70/100 for SDK logging, unbound generations and missing
short-read sensitivity. Logging Red `75b64d1`: 9 failed/3 passed (Azure INFO/DEBUG
and botocore DEBUG leak); Green `7d33b38` plus precise offline response fixture
`9fb6c4b`: 12 real-pipeline tests pass. Suppression-disabled mutation repeats
9 failures. Per-operation SDK filters protect raw records and JSON formatting.

Generation Red `e8698db`: 9 failed/8 passed, reproducing real local mixed bytes
and growth success. Green `61f8093`/`bd9d2dc`: owned read sessions retain the
original local inode and verify fstat size/mtime around reads; cloud ranges carry
captured S3 IfMatch/VersionId and Azure IfNotModified ETag. Real HTTP generation
matrix covers replacement before/between chunks, same-size replacement, growth,
shrink, full/ranged responses and HEAD. Removing S3 IfMatch fails Stubber's exact
conditional request; removing Azure match_condition fails its captured contract.

First-block short/over-read returns generic 502 without media bytes; later short
read over real uvicorn/httpx yields RemoteProtocolError and transmits only the
first block. An in-flight cancellation test holds the worker-thread read until
release and confirms no active read remains. Disabling the exact-length check
now causes **3 intended failures** (2 initial-length cases and real HTTP later
truncation), whereas it survived the original tests. All mutations restored.

Post-remediation local suites: API1175, service1612 + domain/provider677 (2289
combined), worker1165/27 existing skips, mandatory real-media runtime160.
Ruff/basedpyright and architecture diagram validator: zero errors/blocking
diagnostics. Frontend source is untouched. Oracle re-review remains required.

## Red-first evidence

At `a837e56`, local API characterization: 43 passed; real nginx/API guard: 97
passed in development/production. Red commits: `6b56210` (API), `99cb6c1`
(storage), `17e0e45` (runtime). API matrix: 26 failed, 11 passed (remote GET
returned 409; HEAD 405; typed read contract absent). SDK/local contracts: 27
failed (stat/read_range absent). Remote nginx matrix: 4 failed on remote 409.
Streaming: 2 failed because the route did not stream.

Additional Red/Green cases: stale singleton provider and SDK initialization
failure (2 failed); foreign/unsafe namespace (4 failed); storage timeout mapping
(3 failed, 1 already passed). Assertions were retained.

## Mutation evidence

Each temporary production edit was applied separately, the listed test executed
with Python 3.12 and `-q --tb=short -p no:cacheprovider`, then the edit restored.
Paths below are relative to `apps/api/tests/` unless otherwise stated.

| Claim | Production mutation | Test detecting it | Observed failure |
|---|---|---|---|
| Project membership before storage | Remove `require_project_access` membership refusal | `test_project_cloud_content.py::test_real_project_dependency_rejects_revoked_membership_before_storage` | 200 instead of 404 |
| Workspace-scoped lookup | Substitute foreign workspace 8 in `get_asset` | `test_project_cloud_content.py::test_project_preview_denies_before_storage[foreign-asset]` | 200 instead of 404 |
| No backend before authorization | Insert backend stat before asset association refusal | `test_project_cloud_content.py::test_project_preview_denies_before_storage[missing]` | call count 1 instead of 0 |
| MIME allowlist | Disable MIME refusal | `test_project_cloud_content.py::test_preview_mime_allowlist_precedes_storage` | 3 failures: 200 instead of 415 |
| Route nosniff | Replace nosniff with unsafe | `test_project_cloud_content.py::test_project_cloud_content_serves_owned_bytes_without_local_fallback` | 4 header failures |
| Private/no-store | Replace cache control with public | Same remote-byte matrix | 4 cache-control failures |
| Bounded memory | Request remaining whole object instead of min(65536, remaining) | `test_project_content_streaming.py::test_huge_preview_is_lazy_bounded_and_stops_on_cancellation` | whole-object buffering assertion failed |

The first project-access mutation survived the foreign-caller runtime check due
to the independent workspace-scoped asset lookup. A revoked-membership test
using the real dependency was added, passed on the original, then failed under
that mutation. This documents defense in depth rather than claiming the first
test proved every guard. No mutation remains in production.

## Runtime and limitations

`tests/integration/test_authorized_media_runtime.py` is already mandatory in
`first-short-runtime`. Its added S3/Azure-labelled fake-backed matrix runs the
real shipped nginx template, FastAPI auth and PostgreSQL in both modes. Distinct
remote PNG/MP4 and local collision bytes prove no local fallback; full GET,
HEAD, Range, anonymous 401 and foreign/missing 404 are asserted. Existing remote
scene 409 stays covered. No CSP or static-serving change was made.

The fake runtime proves same-origin contract, not live cloud access. Botocore
Stubber proves bucket/prefix/Range request shape, missing/auth mappings and S3
body close on success/failure. Injected Azure clients prove prefix/offset/length,
bounded concurrency and ResourceNotFound/auth mapping. ASGI disconnect stops
further reads; synchronous in-flight calls complete under existing SDK timeouts
before cancellation. Large files use many 64 KiB requests, a deliberate memory/
resource-lifetime tradeoff that needs cloud latency/cost acceptance.

Live IAM/RBAC, credential rotation/expiry, transport cleanup and real cloud
latency are **not proved**. Follow the manual acceptance checklist in
[SECURITY.md](SECURITY.md#manual-cloud-acceptance-not-proved-by-offline-tests).
No paid/real provider was called. Oracle 100/100 remains an independent merge
gate, not implementer self-approval.
