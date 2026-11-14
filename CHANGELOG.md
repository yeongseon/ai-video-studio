# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Security

- Upgrade FastAPI to 0.133.0, Starlette to 1.10.0 and python-multipart to 0.0.33 past published advisories while preserving JSON parsing without Content-Type and rejection of text/plain JSON bodies (#1023 group 1).
- Upgrade the HTTP client stack to aiohttp 3.14.5, anyio 4.16.0, certifi 2026.10.23, idna 3.20 and multidict 6.9.1 past published advisories, with required yarl 1.25.1 and typing-extensions 4.16.0 dependencies (#1023 group 3).
- Upgrade Pillow 11.3.0 to 13.0.0 and CairoSVG 2.9.0 to 2.9.1 past published media-library advisories (#1023 group 2).

### Added

- Celery Beat scheduler service (`beat`) in `docker-compose.yml` (+ dev and scaled-workers overlays) so scheduled tasks actually run (#585).
- `retry_failed_artifact_deletions` and `sweep_expired_artifacts` registered in `celery_app.include` (#585, #587).
- `ARTIFACT_RETENTION_DAYS` env var (default 90) + `sweep_expired_artifacts` beat task that marks expired artifacts for deletion (#587).
- CI smoke: `celery inspect registered` verifies every beat task is registered on the worker (#585).
- CI smoke: POST through nginx (port 5174) with the browser Origin header to catch CSRF regressions (#586).
- Local runtime acceptance now requires one healthy Beat, preserves it across repeated supported startup, verifies reconciliation dispatch logs, and rejects a stopped scheduler (#990).

### Fixed

- OpenTelemetry Console and OTLP exports now redact log bodies, derived chained exceptions and string extras before exporting, and redact span exception-event attributes and status descriptions; redaction failures fail closed without changing JSON stream logging or metrics (#1058).
- The API server process under default Uvicorn import-string CLI startup now uses the shared redacting JSON stream formatter, including chained exceptions; `uvicorn.access` omits query strings and respects `--no-access-log` (#1035). This applies in every environment, not to `--reload` / `--workers` supervisor lines, custom `--log-config`, app-object startup after import, or OpenTelemetry exporters (see #1058; trace-context placement is tracked in #1059).
- Shared API, DLQ and JSON-log error redaction now detects sensitive name/value assignments (including Python string prefixes, escaped quotes and authorization schemes) and HuggingFace/Replicate token prefixes; admin audit attribution uses `key_fingerprint` instead of `key`, and unsigned Azure storage warnings use `object_name` instead of `key` so non-secret identifiers stay readable (#1005).
- Disabled the shipped local admin key and reject publicly known admin-key examples at API startup in every environment; both admin gates and `/health` also refuse them if startup validation is bypassed. Studio nginx strips client `X-Admin-Key` on every API proxy location without changing creator-key handling (#1036). **Upgrade:** an install whose `.env` still has `local-admin-key-change-me` (or the old guide's literal `<a-strong-random-string>`) now fails at startup with a clear message; unset it to disable local admin access or generate a unique key with `openssl rand -hex 32` (production still requires at least 16 characters). Admin access must use the loopback API or an SSH tunnel, not Studio.
- Non-ASCII `X-Admin-Key` headers now receive the same response as a wrong key without a 500 or traceback; both admin gates and `/health` share a constant-time byte comparison (#1057).
- PostgreSQL project deletion returned 503 because its CHECK rejected the existing `deleting` barrier. Migration 051 admits it; schema downgrade preserves pending rows as `archived` (quiesce writers first). Real-PostgreSQL deletion/retry/cascade and enum-drift guards run in required CI (#947).
- Audio generation only falls back to single-pass TTS before the first section provider call; provider failures retain retry mapping and later local errors propagate without another paid call (#1051).
- Hardened the local Studio bind guard: use a version-independent local-address allowlist with exact LAN confirmation and default denial, diagnose legacy `.env` binds and block unconfirmed wildcards; Compose regression checks cover all profiles and host networking (#1037).
- API key revocation silently bypassed — `_AsyncpgSessionAdapter` discarded the `revoked_at IS NULL` filter. Adapter removed; the resolver now runs the asyncpg query directly (#583).
- Cutover acceptance refuses an existing `.env` unless the checkout is explicitly disposable, publishes its own environment atomically, and excludes `.env*` secrets from root Docker build contexts while retaining examples (#1049).
- Route-preserving log redaction now hides opaque segments and their entire tails, rejects private left-boundary suffixes, checks nested mounts and single-segment parameters for route drift, and visibly escapes controls in API access-log paths (#1047).
- Studio's Vite development proxy strips client `X-Admin-Key` on every proxied prefix without changing creator-key handling (#1056).
- Local artifact downloads always returned 404 — the route read `file_path` (absolute) instead of `storage_key` (root-relative). Legacy rows now normalized via `os.path.relpath` (#584).
- Restore drill child processes use an explicit environment allowlist with Docker-only daemon selectors, pin API/reconciler state into the temporary drill root, refuse existing container names, and use per-run labels to recover containers created before a failed start without deleting foreign containers (#1040).
- nginx CSRF check rejected default compose port — `$host` doesn't include the port, switched to `$http_host` (#586).
- Where Celery timeouts are enforced, render hard timeouts reclaim attributable scratch and partial MP4s; a shipped Worker-start sweep reclaims recognised dead-owner or over-age scratch in numeric run directories and the configured scratch root with bounded candidate selection and independent root budgets while preserving published outputs and younger live-owner scratch (#1030).

## [0.4.0] - 2026-07-23

### Added

- Mermaid architecture diagrams in README (system topology, pipeline flow, package dependencies)
- CI pipeline with lint, test (coverage), Docker build, and smoke test
- Community files: CONTRIBUTING.md, CODE_OF_CONDUCT.md, SECURITY.md
- GitHub issue templates (bug report, feature request) and PR template
- Documentation: Usage Guide (`docs/USAGE.md`), Deployment Guide (`docs/CUTOVER.md`)
- API contract tests for response shapes and CORS policy
- Python constraints file (`constraints.txt`) for reproducible installs
- Provider abstraction layer with pluggable LLM, Image, TTS, and STT adapters
- Support for remote providers: OpenAI, Anthropic, Google Gemini, Stability AI, ElevenLabs
- Support for local GPU providers: Ollama, Stable Diffusion, Qwen TTS, Whisper STT
- Redis-based GPU lock for local model execution
- Docker Compose profiles: `gpu` for AI services, `monitoring` for Flower
- Authenticated artifact route replacing static file mounts
- Paragraph-level generation with per-section skip/retry logic
- Human-in-the-loop review gates at every pipeline stage
- ADR-006 (API key auth) and ADR-007 (OAuth2/OIDC roadmap, Supabase primary)
- `api_keys.name` and `api_keys.revoked_at` columns (migration 029)
- Server-side `expires_at` default on `creator_artifacts` (migration 030)
- Deletion retry metadata on `creator_artifacts` (`delete_requested_at`, `delete_failed_at`, `delete_retry_count`)

### Changed

- Raised basedpyright type checking to `standard` mode (0 errors)
- Bound all Docker services to `127.0.0.1` for security
- Switched Gemini API key transport from URL query string to `x-goog-api-key` header
- Split Docker Compose into base (`docker-compose.yml`) and dev (`docker-compose.dev.yml`)
- Consolidated stage policy into central location (`types/api.ts`)
- Extended Vite dev proxy for same-origin API workflows

### Fixed

- Paragraph-level generation status detection and completed section skipping
- Worker `active_task_id` cleanup on paragraph task completion
- Paragraph-level artifact filtering from run-level audio/subtitle queries
- Duplicate task dispatch blocked when generation already in progress
- Late-finishing worker prevented from advancing cancelled runs
- Hard-coded Alembic password fallback removed
- Run stage conflict returns 409 instead of silent failure
- Model-default validation before task dispatch (audio, subtitle, render profiles)
- Stage guard enforcement on script and visual-plan mutation endpoints
- Optimistic UI state rollback on model-default update failure
- Subtitle format restricted to `srt|vtt` enum
- Nginx proxy health/docs routing for same-origin OpsPage
