# Timeline provider-neutral preview verification (#900)

## Writer-contract audit

| Writer | Published-key concurrency / policy |
|---|---|
| `object_storage.py::LocalStorageBackend.upload` | Original in-place bytes/stream writing; new modes honor umask and overwrite retains inode/mode/group/ACL. Same-source artifact registration only hashes. No anonymous publication staging added. |
| `media_asset_service.py::_store_asset` (196-197) | The only production workspace-media backend upload caller: fresh `workspaces/<id>/assets/<uuid>-<filename>` each time. Reserve upload intent → upload → mark uploaded → persist metadata; never rewrites an existing served asset key. `create_image_asset`, generated image, video, audio and file ingestion all converge here. |
| `artifact_storage_integration.py::store_artifact_file` (20) | The other production `backend.upload` caller: local path relative to ARTIFACT_ROOT, commonly run/artifact paths, not freshly created UUID workspace asset keys. Regeneratable run keys retain original writer semantics; authorized legacy project rows can reference these with weaker provenance. |
| `generate_scene_image.py:161,223`, `scene_batch_lifecycle.py::upload` | UUID/owner-specific run output names before artifact registration; completed files use original adapter upload. |
| `generate_audio.py:168-171,209`, `generate_paragraph_audio.py:101` | Delivery-specific directories; active output is unpublished, immutable completed files are registered after provider result. No previously published delivery key is reused by a successor. |
| `generate_subtitles.py:94`, `generate_paragraph_subtitles.py:85` | Fixed run subtitle paths may be rewritten. Whisper/Groq retain their original write_text semantics; these are not workspace-media project asset keys. |
| `ffmpeg_service.py::render` (319-478), `render_video.py`, `timeline_render.py` | Existing FFmpeg temporary final output/rename unchanged; completed run outputs registered through original adapter upload. No render integrity code changed (#901 lane). |
| FFmpeg segments/concat lists, merged SRT/ASS, narration mixing | Private unique render temporary directory or preparatory render inputs, never registered as downloadable output identities during write. Final video only registered after atomic render success. |
| `render_segments.py::render_plan` | Direct-output helper has no production publication caller; repository callers are render unit tests. Not a workspace-media asset writer. |
| `render_materializer.py:39`, probe/upload staging | Private TemporaryDirectory/materialization/probe files, never registered as public artifact identity; scope ends with task. |
| `scripts/generate_ssul_shorts.py`, `scripts/image-gen/*` | Offline generation/gallery tools; no canonical MediaAsset or run-artifact registration. Do not run offline writers against live registered keys. Restore tooling runs in isolated/maintenance restore targets, not concurrent service publication. |

Audit search: `.upload(` across `apps/`, `packages/`, `scripts/`. Production
backend calls are the two `_store_asset` branches and `store_artifact_file`
above; worker `batch.upload(target_path)` delegates artifact registration, not a
new backend call. No script backend upload calls exist. Tests intentionally write
fixture/corrupt keys: API `test_project_media_content`, worker
`test_scene_upload_cancellation`, `test_scene_upload_sdk_errors`,
`test_timeline_worker_boundaries`; creator-service storage adapter/asset tests
exercise local/S3/Azure uploads, not production writers. Review future backend
callers for rewriting the workspace asset namespace before claiming this contract.

Newly created workspace assets are written once under fresh UUID keys by `_store_asset`:
reserve/upload/persist order makes asset identities undiscoverable until complete.
The retained no-follow descriptor serves A across an atomic replace by any writer.
It checks dev/ino/size/mtime_ns/ctime_ns around every read and aborts detectable
in-place changes. An actor with artifact-root write access has full control of
served bytes; same-inode rewrites restoring timestamps or fitting timestamp
granularity are out of scope, not proved prevented. Network filesystems are not
validated. Authorized legacy non-workspace keys remain accepted, with weaker
provenance and no write-once guarantee. Local writers were deliberately restored
to origin/main semantics. The post-pread verify is redundant with the streaming
verify before delivery; removing that individual check is not independently
mutation-sensitive and no such claim is made.
ctime checks are defense in depth, with coarse-timestamp simulations proving
independent size/ctime comparisons. Tests retain readers/responses and assert
EBADF immediately on normal completion, failure, in-flight disconnect, HEAD,
empty GET and constructor failure; garbage collection cannot mask leaks.

## Oracle round-3 remediation (round 4)

## Oracle round-4 remediation (round 5)

Red `4f99a4f`:5 failures/1 pass: two subprocess shutdown watchdog timeouts,
cancelled waiter leaking later ValueError, worker KeyboardInterrupt/SystemExit
terminating the interpreter. `bdda72a` replaces loop-task ownership with a local
four-worker ThreadPoolExecutor and concurrent future callback; lock-protected
claim/abandon ensures one resource owner. Worker exceptions are completion data,
cancellation takes precedence and repeated cancellation waits for thread completion.
Completion accounting is released by the thread future callback, including discard.
Shared BlockingIO and all writers remain unchanged.

Acquisition Red `4c2dd08`:3 operational errors falsely404 and2 retained-owner
constructor leaks. `1e8dcf1`:only ENOENT/ELOOP/ENOTDIR and nonregular refusal are
missing; timeout503, other operational failures502. Constructors close on every
post-acquisition BaseException then preserve the original failure.
`1749ea4`:missing ETag adapter tests plus GET/HEAD HTTP tests. Removing both
guards fails3 adapter tests and8 HTTP cases (200 vs502); no ranged read permitted.

Round5 mutations rerun in fresh interpreters: all prior membership/workspace/
earlyIO/MIME/nosniff/cache/buffering/exact-length/OSError/close/ctime/size/IfMatch/
Azurecondition/SDK/UUID probes fail. VersionId removal1; callback discard removal1;
claim-lock removal1; awaiter token release6; acquisition misclassification2;
constructor OSError-only cleanup2; absent ETag HTTP8. No tracked mutation remains.
Full local API1232, packages2316, worker1165/27 existing skips; whole mandatory
multi-file media invocation160 (authorized-media file alone101), all DB env enabled.
Ruff/typecheck/LSP and architecture/dependency validators clean.

Writer reversal: Red `d473ee9` had six inode-preservation failures; `2d9ec49`
and `4ac7ca7` restore original uploads and STT providers, six characterization
cases pass (bytes/stream × umask022/002/077, retained inode/mode, no temp files).
The former atomic-publisher tests are replaced, not retained as false contracts.
Native cancellation Red `554778b`: six failures (five retained-FD phases and
capacity). Fix `5bffeca` retains completion and limiter ownership through repeated
Task.cancel; five ×500 acquisitions/prefetch/midstream/HEAD/close transfers and
12 competing cancellations pass. Syscall Red `3bb6b6c`: twelve real-HTTP failures
with private-marker ASGI logs; normalized pread/fstat now yield generic502/503 or
incomplete midstream transfers with immediate retained-reader closure.

Round4 mutation reruns (fresh interpreter, in-memory edits, no tracked edits):
membership1, workspace1, early-I/O1, MIME3, nosniff4, cache4, whole-read1,
exact-length3, native acquisition done-callback2, early limiter release1
(peak6 >4), OSError passthrough8, owner exit→pass8, ctime2, size1,
S3 IfMatch1, Azure match_condition1, SDK suppression9, reused UUID key1
all fail intended assertions. An initial unshielded-future mutation hung and was
terminated; the explicit early-token-release mutation is the bounded capacity
proof, not that timeout. Swallowed midstream errors remain incomplete by
Content-Length; no mutation-killed claim for harmless swallowing.

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
