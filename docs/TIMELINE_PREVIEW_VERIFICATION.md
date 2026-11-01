# Timeline provider-neutral preview verification (#900)

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
