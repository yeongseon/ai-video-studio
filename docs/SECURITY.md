# Security Model

This document describes the authentication architecture, trust boundaries,
and deployment constraints for AI Video Studio.

---

## Authentication Domains

The system uses **two separate authentication domains**:

| Domain | Path Prefix | Credential | Header |
|--------|-------------|------------|--------|
| Creator API | `/api/creator/*` | DB-backed API key (SHA-256 hashed) | `X-API-Key` or `Authorization: Bearer` |
| Admin API | `/api/admin/*` | Environment variable (`ADMIN_API_KEY`) | `X-Admin-Key` |

These credentials are **not interchangeable**. A valid creator API key cannot
access admin endpoints, and vice versa.

### Defense-in-Depth

Admin authentication is enforced at **two layers**:

1. **Middleware layer** (`ApiKeyMiddleware`): Validates `X-Admin-Key` against
   `ADMIN_API_KEY` before the request reaches any route handler. Returns 401
   if key is missing or env var is unset; returns 403 if key doesn't match.

2. **Router dependency layer** (`Depends(require_admin)`): All admin routes
   include this dependency as a secondary check with additional production
   safeguards (minimum key length enforcement, timing-safe comparison).

This ensures that even if a new admin sub-route is added without the proper
dependency, the middleware still blocks unauthenticated access.

---

## Personal API Keys and Attribution

> **Warning**: The current API key model is designed for **single-user or
> trusted-team deployments only**. It is NOT suitable for multi-user public
> deployments.

### Current Model

- Each user can hold multiple named personal API keys. The `api_keys` table
  binds each key to a `user_id`; only the SHA-256 hash is stored.
- Operators issue keys with `scripts/create_api_key.py --email <user-email>
  --workspace <workspace-slug> --name <key-label>`. Repeating issuance for the
  same email creates another key for the same user. The raw key is displayed
  once for delivery to that user; keep this output out of application logs.
- Clients continue using `X-API-Key` or `Authorization: Bearer`. Workspace
  access comes from that user's memberships; `X-Workspace-Id` selects one of
  those workspaces. Missing/invalid/revoked credentials return 401; unavailable
  or unauthorized workspace selections return 404. Auth database failures
  return 503, including the dependency fallback lookup.
- Request audit entries from `shorts_api.app_factory` include `user_id`,
  `key_id` (the database primary key), and `workspace_id`, alongside method,
  path, response status, and elapsed time. These identify authenticated actions
  reaching request logging, including route-level errors. Raw credentials,
  key hashes, authorization headers, and query strings are not audit fields.
- Authentication middleware rejections occur before request logging; these
  entries are not a comprehensive authentication-attempt ledger. Admin audit
  logging remains a separate authentication domain, described below.
- Revocation is per key: operators set `api_keys.revoked_at` for its `id`.
  Revoked keys cannot authenticate; other keys belonging to the user remain valid.
- Sharing a personal key still makes all holders appear as its owner. In
  particular, a Studio proxy configured with one `API_KEY` attributes all
  browser actions to that key's user, not to distinct humans. Use individually
  provisioned credentials for per-user attribution.
- Keys cannot be scoped to specific operations (read-only, write-only, etc.)

### Implications for Multi-User Deployments

If you need to support untrusted multi-user access:

| Requirement | Current Support | Recommendation |
|-------------|-----------------|----------------|
| Per-user identity | Yes (personal keys) | OAuth2/OIDC for human login |
| Role-based access | No | Implement RBAC layer |
| Audit trail per user | Request logs carry user/key/workspace IDs | Durable audit storage and auth-attempt coverage |
| Key revocation per user | Individual key revocation via DB | Self-service key lifecycle management |
| Rate limiting per user | No | Token-based rate limiting |

### OAuth2/OIDC Roadmap (Not Implemented)

For production deployments with multiple untrusted users:

1. Adopt an **identity provider** (e.g. Authelia or Keycloak)
2. Map identity tokens to workspace membership
3. Issue **per-user, short-lived tokens** instead of long-lived API keys
4. Preserve user attribution in a durable audit trail
5. Retain personal/service API keys for machine-to-machine clients

Until these are implemented, treat the current system as a **team-internal
tool** where all key holders are trusted.

---

## Network Exposure Policy

> **Critical**: The API server (port 8000) must **NEVER** be directly exposed
> to the public internet.

### Required Architecture

```
Internet → Reverse Proxy (nginx/Caddy/Traefik) → localhost:8000 (API)
                    ↓
              TLS termination
              Rate limiting
              IP allowlisting
              Request size limits
```

### Default Port Bindings

The `docker-compose.yml` binds all backend services to `127.0.0.1` only:

```yaml
api:
  ports:
    - "127.0.0.1:8000:8000"   # API — loopback only

postgres:
  ports:
    - "127.0.0.1:5432:5432"   # DB — loopback only

redis:
  ports:
    - "127.0.0.1:6379:6379"   # Cache — loopback only
```

The `docker-compose.local-server.yml` override maintains this policy:
- `studio-web` is exposed on `0.0.0.0` for LAN access (UI only)
- API, Postgres, and Redis remain on `127.0.0.1`

### Why Direct Exposure is Dangerous

1. **No TLS**: The API speaks plain HTTP. Direct exposure means credentials
   travel in cleartext.
2. **No rate limiting**: Without a reverse proxy, the API has no protection
   against brute-force or denial-of-service attacks.
3. **No request filtering**: Large payloads, malformed requests, and slow
   clients hit the API directly.
4. **Credential compromise**: A leaked personal key grants its owner's workspace
   access; a shared Studio proxy key exposes that same identity to every holder.

### Deployment Checklist

Before exposing the system to any network:

- [ ] API port bound to `127.0.0.1` (never `0.0.0.0`)
- [ ] Reverse proxy with TLS configured
- [ ] `CORS_ORIGINS` set to specific allowed origins (not `*`)
- [ ] `ADMIN_API_KEY` set to a strong value (16+ characters)
- [ ] `POSTGRES_PASSWORD` changed from default
- [ ] `ENVIRONMENT=production` set (enables startup safety checks)
- [ ] Firewall rules block direct access to ports 8000, 5432, 6379

### CI Validation

The CI workflow validates that `docker-compose.yml` does not expose the API
on `0.0.0.0`. If you need LAN access for the API during development, use the
`docker-compose.local-server.yml` override with explicit acknowledgment of
the security implications.

---

## Admin API Security Controls

### ADMIN_API_KEY Requirements

| Environment | Minimum Length | Behavior if Unset |
|-------------|---------------|-------------------|
| Development | None | Returns 401 on admin requests |
| Production | 16 characters | API refuses to start (fail-fast) |

### Rate Limiting

Destructive admin operations (unstick run, clear cache) are rate-limited to
**10 operations per minute** per admin key. Rate limiting uses Redis when
available, with an in-memory fallback.

### Audit Logging

All admin mutations are logged to the `admin.audit` logger with:
- Action type
- Target resource
- Key fingerprint (first 8 chars of SHA-256)
- Request ID
- Source IP
- Unique audit ID (UUID)

### Safety Guards

- Cache clear operations must match an allowlist of key prefixes
- Dangerous patterns (`*`, `*:*`, empty) are rejected
- Destructive operations require `X-Confirm-Action: yes` header

---

## Provider API Key Configuration

### Supported model: server-side environment variables, read-only status surface

Provider API keys (OpenAI, Anthropic, Google, Stability, ElevenLabs, Groq) are
configured **exclusively through server environment variables** and are resolved
at runtime with `os.getenv` only. This is a deliberate security decision, not a
missing feature.

| Property | Behavior |
|----------|----------|
| Source of truth | Server environment variables (`OPENAI_API_KEY`, etc.) |
| Request-time writes | **None** — the API never writes `.env`, a DB, or any secret store from a request |
| Settings surface | `GET /api/creator/settings/api-keys` returns `{provider, label, configured}` only |
| Key values in responses | **Never** — not raw, not masked, not truncated (presence boolean only) |
| Key values in logs | **Never** — no key material is passed to any logger |
| Browser storage | **Never** — the UI holds no provider keys in `localStorage`/`sessionStorage` |
| Rotation | Change the env var and restart: `docker compose restart api worker` |

Write verbs (`POST`/`PUT`/`PATCH`/`DELETE`) on the settings path return `405
Method Not Allowed` and have no side effects. A presence-only contract is
preferred over masked values because a mask still leaks length, prefix, suffix,
or format of the secret.

### CSRF applicability

CSRF protection is **not applicable** to the current creator API because
authentication uses explicit request headers (`X-API-Key` /
`Authorization: Bearer`), not cookies or any browser-auto-attached credential.
A cross-origin page cannot forge those headers, and there are no mutating
settings endpoints. If creator authentication ever moves to cookie/session
credentials, or request-time key-mutation endpoints are introduced, CSRF
protection (or per-request CSRF tokens at the `apps/studio-web/src/api/client.ts`
choke point) becomes required before enabling them.

### Trust model note

The presence booleans reveal which providers are configured to any authenticated
operator. This is intentional and acceptable under the documented single-user /
internal-team trust model; it is not a public endpoint. Self-service key
management from the UI (encrypted server-side storage, admin-scoped rotation,
audit) is explicitly **out of scope** and would be tracked as a separate,
larger secret-management feature.

---

## Artifact Access

Generated artifacts (videos, images, audio) are stored on the local filesystem
under `ARTIFACT_ROOT` or the configured S3/Azure Blob backend. Access is gated
by the creator API — artifacts are never
served directly from the filesystem to external clients.

### Timeline project previews

`GET`/`HEAD /api/creator/projects/{project_id}/assets/{asset_id}/content`
uses project access and a workspace-scoped asset lookup before any object I/O.
Foreign projects, missing assets, wrong project associations, missing objects,
unsafe keys and foreign workspace-prefixed keys return 404. Anonymous requests
return 401. Only stored `image/png`, `image/jpeg`, `image/webp`, `video/mp4` and
`video/webm` MIME types can be previewed; caller or cloud MIME metadata cannot
override the allowlist (415). Previews retain inline behavior without a filename
or storage key in Content-Disposition, declare `nosniff`, and are
`Cache-Control: private, no-store`.

The asset's recorded `storage_provider` must match both `STORAGE_BACKEND` and
the initialized singleton's provider. Mismatch returns 409 without reading any
object; historical assets require an operator-controlled migration/configuration,
not caller-selected accounts, buckets or keys. GCS/unknown providers return an
explicit unsupported-provider 409. Local reads use no-follow directory
descriptors, including every path component, to prevent symlink/traversal races.
Cloud reads stay under the adapter's configured bucket/container and prefix.

Media streams through the same-origin API, never a signed redirect, SAS, public
cloud URL or unsigned development fallback. CSP and nginx raw-file policy remain
unchanged. Each provider read requests at most 64 KiB and releases its response
before returning. S3 uses `GetObject Range`; Azure uses `download_blob(offset,
length, max_concurrency=1)` and consumes only that bounded range. Blocking I/O
runs on the capacity-limited executor. Disconnect/cancellation stops further
reads; an already-running synchronous SDK call finishes and releases its
response before cancellation propagates (existing SDK connection/read timeouts
and retries apply). This is bounded memory, not an instantaneous cancellation
guarantee for an in-flight network call. A large full GET can incur many cloud
range requests; cloud cost/latency must be assessed during manual acceptance.

Full GET returns 200; single bounded, open-ended and suffix byte ranges return
206 with exact Content-Range and Content-Length. Malformed, multi-range and
unsatisfiable ranges return 416 with `Content-Range: bytes */size`. HEAD ignores
Range and returns full headers without content reads. If-Range is treated as
nonmatching (full 200); no ETag/Last-Modified validator support is advertised.
Missing objects return the same generic 404 as unavailable identities. Provider
auth/config/network failures return generic 502, recognized read timeouts 503,
without logging keys or SDK exception/cause text. During adapter stat/conditional
read operations, context-scoped filters on the SDK's record-producing loggers
suppress Azure/botocore diagnostics before any handlers receive them, including
globally enabled DEBUG and explicitly enabled Azure HTTP logging for the pinned
Azure HTTP/storage policies and botocore endpoint/signing/parser emitters.
This is not a guarantee for future/lazy/unlisted emitters, SDK root loggers,
third-party instrumentation or arbitrary warnings. Offline real
SDK pipelines test stat, range and transport/auth failures at INFO and DEBUG,
including fresh-interpreter first operations captured through root handlers,
lastResort, warnings and stderr and formatted with the repository JSON formatter. This guarantee
is scoped to those adapter read operations, not arbitrary external SDK clients
or operator instrumentation. Failure after headers terminates
the response; Content-Length exposes truncation, never JSON embedded in media.

Scene/workspace preview routes remain local-only (remote 409). Run-artifact
download/expiration behavior is unchanged. Timeline preview JSON still includes
its authorized internal `source` and identity `media_url`, and preserves revision
gates. The browser's `source` fallback is not used for responses from this route,
which always emits `media_url`; no concrete cross-tenant or cloud-capability leak
was demonstrated. Do not treat internal source keys as public delivery URLs.

Literal legacy keys (including encoded or Unicode dots and uppercase prefixes)
are not normalized into new namespaces. Key validation is a traversal boundary,
not a universal ownership proof for non-workspace legacy keys: persisted project
and workspace identity controls still apply.

Transfers acquire an owned read session. Local reads retain one no-follow
descriptor and serve its original inode across atomic rename/replacement. Each
read verifies descriptor size/mtime/ctime/identity before and after I/O as defense
in depth (atomic unlink changes ctime/link count without mutating retained data).
The consistency guarantee is for storage writers that publish atomically:
LocalStorageBackend uploads stage in the same directory, fsync and os.replace,
never mutate an existing published inode. External in-place writers (including
timestamp-preserving hard-link writes) and network filesystems are unsupported;
metadata checks alone cannot make them safe. Observed changes abort. See
[local writer audit](TIMELINE_PREVIEW_VERIFICATION.md#local-writer-audit).
Cloud sessions capture ETag (S3 VersionId when
present) and condition every range with S3 IfMatch/VersionId or Azure
IfNotModified. A failed condition is generic 502 before bytes; after start it
aborts the incomplete transfer. Short/over-reads also abort and never append JSON.
HEAD describes the single acquired generation without fetching content.

#### Manual cloud acceptance (not proved by offline tests)

Offline S3 SDK Stubber, injected Azure client and real nginx/API/PostgreSQL tests
prove request shape, bytes, auth boundaries and range/header behavior, not live
IAM/RBAC permissions, credential rotation/expiry, cloud transport cleanup or
provider latency/cost. Before enabling cloud previews, an authorized operator
must use a private configured bucket/container and verify PNG/MP4 full GET, HEAD,
seek/suffix ranges, foreign/missing 404, credential-revocation fail-closed behavior,
disconnect cleanup and memory under a large file, through the normal Studio
proxy. Confirm no cloud URL/SAS/key appears in browser responses or logs. These
checks require real credentials and are not performed by CI or this change.

Production cloud enablement remains conditional: shipped API/worker images do
not currently install boto3/azure-storage-blob. Cloud configuration therefore
returns generic 502 until SDK provisioning in #895/#898 lands. Offline test
dependencies do not change shipped runtime images.

For external preview access during development, use the documented loopback
HTTP server pattern (see `AGENTS.md`). External tunneling requires explicit
user confirmation.
