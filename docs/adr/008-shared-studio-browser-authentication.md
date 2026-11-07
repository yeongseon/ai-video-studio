# ADR-008: Shared Studio Browser Authentication and Public-Ingress Boundary

**Status**: Proposed

**Date**: 2026-11-08

**Decision makers**: Open for review

**Extends**: [ADR-006](./006-api-key-authentication-model.md)

**Revisits**: [ADR-007](./007-oauth-oidc-integration.md)

## Decision scope

This ADR defines the target authentication boundary for a shared Studio and
splits issues [#894](https://github.com/yeongseon/short-form-studio/issues/894)
and [#575](https://github.com/yeongseon/short-form-studio/issues/575) into
independently verifiable S/M deliveries. It does not implement authentication,
create an identity-provider registration, change ingress, or authorize an Azure
deployment.

The target is a React SPA served by nginx, with same-origin requests proxied to
FastAPI. Browser users authenticate through server-validated OIDC and receive an
opaque application session cookie. Existing DB-backed personal API keys remain
available for explicit machine clients. The static admin key remains a separate
operator trust domain.

## Verified current state

The following statements describe the repository at `origin/main` commit
`3717f3a` and the open records reviewed on 2026-11-08:

- `apps/studio-web/nginx.conf.template` sets `X-API-Key: ${API_KEY}` on the
  general `/api/` proxy and the upload, health, docs, and OpenAPI proxy
  locations. Every browser request through that nginx instance therefore uses
  the configured key's identity.
- `ApiKeyMiddleware` in `apps/api/src/shorts_api/auth.py` accepts
  `X-API-Key` before `Authorization: Bearer`. A browser-supplied Bearer value
  does not override the key injected by nginx.
- Creator keys are already personal identities. Migration 018 binds each key
  to `api_keys.user_id`; migrations 029 and 035 add names, revocation, and
  lookup indexes. Raw keys are not persisted. `scripts/create_api_key.py` is an
  operator issuance path.
- Migration 012 already defines `users`, `workspaces`, and
  `workspace_members`, including unique `(auth_provider, auth_subject)` user
  identity. A key resolves to a user; the selected workspace is derived from
  that user's memberships. `X-Workspace-Id` may select only a membership.
- `require_workspace_access`, `require_run_access`, and
  `require_project_access` enforce workspace/resource access and hide foreign
  resources with 404. The target session identity must produce the same
  `CurrentUser` contract and preserve those guards.
- The creator API currently uses explicit header credentials, not browser
  cookies. `docs/SECURITY.md` therefore identifies cookie-session CSRF as a
  future requirement. nginx currently rejects non-safe API requests whose
  `Origin` is not its computed same-origin value.
- `ADMIN_API_KEY` and `X-Admin-Key` form an independent admin domain with no
  creator `CurrentUser`. This ADR does not make an OIDC user an administrator.
- ADR-007 is proposed, not implemented. Its browser-JWT/middleware design and
  named provider choices are not runtime facts. This ADR replaces its browser
  token-delivery recommendation; ADR-007 remains historical input until it is
  separately superseded or amended.
- Issue #894 records a conditional public-ingress blocker, not evidence of a
  current public exposure or exploit. Issue #575 owns the broader shared-user
  product contract. Open PR #970 is a topology design only: Studio would be the
  browser entry point, API ingress internal, Worker/Beat without ingress, and
  public Studio ingress blocked pending #894/#575 and operator approval.

There is a documentation discrepancy in ADR-006: it says an API key is bound to
both a user and workspace and may be deleted for revocation. Current migrations
and code bind a key to a user, derive workspace context from memberships, and
use `revoked_at`. Implementation work must follow the current schema and tests;
correcting ADR-006 prose is a separate documentation follow-up if desired.

## Threat model for public ingress

### Assets and security properties

- OIDC authorization codes, client credentials, tokens, application sessions,
  personal API keys, and the admin key must remain confidential.
- Each action must be attributable to one application user and one authorized
  workspace. A user must not create in, enumerate, read, approve, cancel, or
  download from a foreign workspace.
- Project and media existence must not be disclosed across workspaces. Foreign
  resources continue returning 404; missing/invalid authentication returns 401.
- Session expiry, logout, membership removal, and revocation must fail closed.
- The API must not trust a browser-supplied user ID, workspace ownership claim,
  ACA identity header, or forwarding header merely because nginx forwarded it.
- Provider, session, API, and admin secrets must not enter the SPA bundle,
  browser storage, URLs, response bodies, or application/access logs.

### Adversaries and failure modes

- An unauthenticated internet client invokes creator or media routes directly
  through the public Studio hostname.
- User A changes object IDs, workspace selectors, media URLs, or range requests
  to access user B's resources.
- A client supplies `X-API-Key`, `X-Workspace-Id`, `X-MS-CLIENT-PRINCIPAL-*`,
  `X-Forwarded-Proto`, `X-Forwarded-For`, or similar headers to impersonate an
  identity or bypass origin/secure-cookie checks.
- A malicious site causes a signed-in browser to submit a state-changing
  request with its automatically attached session cookie.
- A script copied from the SPA, logs, source maps, or runtime configuration
  recovers a shared credential and becomes the proxy-key user.
- A stolen/replayed session remains usable after logout, expiry, membership
  removal, or server-side revocation.
- A deployment accidentally enables a public hostname while still injecting
  the bootstrap key or accepting untrusted proxy identity headers.
- Oversized uploads or unrestricted proxy paths consume resources before
  authorization. Existing route-specific upload limits must remain explicit.

### Trust boundaries

1. The browser and public network are untrusted.
2. The TLS/ingress proxy is trusted only for the forwarding fields that the
   deployment proves it overwrites. nginx must strip identity headers from the
   client before adding any trusted internal values.
3. FastAPI is the application authorization authority. It resolves a session or
   personal key to an application user and derives workspace access from the
   database; neither nginx nor the IdP grants resource ownership.
4. The OIDC provider authenticates the external subject. It does not own local
   workspace membership.
5. Admin credentials remain outside the browser-user flow.

## Browser authentication options

### Option A — API-side OIDC authorization-code flow with PKCE and session cookie

FastAPI exposes login, callback, logout, and current-session endpoints. Login
creates one-time state, nonce, and PKCE values. The callback validates state,
issuer, audience, nonce, redirect URI, signature, and code exchange, then maps
the verified `(issuer/provider, subject)` to an application user. Provider
tokens and any confidential-client credential stay server-side. The browser
receives only an opaque, rotated session identifier in an `HttpOnly`, `Secure`,
host-only cookie with an explicit `SameSite` policy and narrow `Path`.

Microsoft's current identity-platform guidance prefers authorization code with
PKCE over the implicit grant. Confidential web clients redeem the code at the
token endpoint, and the redirect URI must match the authorization request. This
option fits the existing FastAPI authorization boundary and avoids exposing
access/refresh tokens to React or browser storage.

Costs: the application must own session storage, rotation, expiry, revocation,
CSRF, OIDC validation, key rotation, and provider-outage behavior. These are
security-critical implementation and operations responsibilities.

### Option B — browser-held OIDC Bearer tokens

React performs authorization code with PKCE and sends a Bearer access token to
the API. FastAPI validates signature, issuer, audience, expiry, and claims and
maps the subject to the same local user/membership model.

This is standards-based and avoids a server session store, but puts tokens in a
JavaScript-accessible runtime and expands the impact of XSS, frontend telemetry,
browser-extension access, and accidental storage/logging. Refresh and revocation
semantics are more complex, and the current nginx-injected key would still win
until removed. It is not recommended for the Studio browser. Bearer API support
may remain a separate machine/integration concern after its audience and token
contract are explicitly designed.

### Option C — nginx `auth_request`

nginx asks an authentication subrequest endpoint to validate each request and
may pass returned identity headers upstream. This centralizes edge rejection,
but it adds a second authorization integration and header-spoofing boundary.
nginx still cannot decide application workspace membership or resource
ownership, so FastAPI must perform authorization anyway. It also makes local
mode and tests depend on proxy configuration. This option is rejected as the
primary design; nginx remains a transport/security-header boundary, not the
application identity authority.

### Option D — ACA built-in authentication (Easy Auth)

ACA can place an authentication sidecar in front of an external-ingress app,
manage a browser session, and inject claims such as
`X-MS-CLIENT-PRINCIPAL-ID`. Microsoft documents that external requests cannot
set those identity headers at the ACA boundary. It can require authentication
or allow anonymous access and exposes `/.auth` login/logout routes.

This is useful defense in depth for one ACA deployment, but it couples the
application identity contract to an Azure edge, applies access policy to the
whole container app, and still requires FastAPI mapping and workspace
authorization. The proposed topology also has nginx between ACA ingress and the
internal API; accepting injected headers safely would require proving and
pinning that exact hop, stripping spoofed values, and reproducing the behavior
outside ACA. It does not satisfy the project's local/lightweight deployment on
its own. It is not the portable source of application identity. A future ACA
deployment may enable it as an additional gate only after real-proxy tests prove
the header and cookie contract.

## Decision

Adopt **Option A**: server-validated OIDC authorization code with PKCE, backed by
an API-side opaque session. Use Microsoft Entra ID as the first deployment
target only after an operator supplies a registration; keep issuer, audience,
and provider configuration explicit enough that the application contract is
OIDC-based rather than ACA-header-based.

The session resolves to the existing `CurrentUser(user_id, workspace_id,
key_id)` authorization shape; the implementation may make `key_id` optional and
add an authentication-method/session identifier without changing workspace
guards. The verified OIDC subject maps to `users.auth_provider` plus
`users.auth_subject`. Workspace selection remains membership-derived.

No client-provided email, user ID, workspace ID, role, or display name is an
ownership assertion. Email may be profile data only after the provider's claim
contract is decided. First login creates or links a user only under an explicit,
tested provisioning policy. It must not silently join an existing workspace by
matching an unverified email.

### Session contract

- Store only an unguessable session identifier in the cookie; store session
  state server-side. Store provider tokens only if an identified server-side use
  requires them; otherwise discard them after identity validation.
- Set `HttpOnly`, `Secure`, a host-only scope (no `Domain`), an explicit narrow
  `Path`, and a documented `SameSite` value. Rotate the session identifier after
  login and other privilege changes. Bound idle and absolute lifetime.
- Logout and server-side expiry invalidate the session. Membership checks must
  use current authorization data or a short, explicit cache bound so removed
  membership does not survive indefinitely.
- Do not put session IDs, authorization codes, tokens, API keys, client secrets,
  or signed media capabilities in URLs, frontend configuration, browser storage,
  source maps, response payloads, or logs. Log stable database identifiers only
  after authentication, with the existing redaction discipline.

Exact lifetimes, cookie name/path, OIDC scopes, token retention, and session
backend remain open questions and must be fixed by the session child issue
before implementation.

### CSRF contract

Cookie authentication makes CSRF applicable. Every state-changing creator and
session request must pass all of these controls:

1. Same-origin `Origin` validation at a single tested server authority, with a
   tightly defined fallback policy for clients that legitimately omit Origin.
2. A session-bound, unpredictable CSRF token sent in a custom request header by
   `apps/studio-web/src/api/client.ts`; compare it in constant time. Never place
   it in a request URL.
3. `SameSite` cookie defense in depth; do not treat it as the only control.
4. Safe methods remain non-mutating. Login callback state/nonce/PKCE protect the
   OIDC flow; logout's method and CSRF behavior must be explicit.

CORS is not CSRF protection. Production `CORS_ORIGINS` remains an explicit
allowlist, and credentialed cross-origin use is disabled unless a separately
reviewed requirement establishes it.

### Shared bootstrap and admin keys

- Shared browser mode must never inject `API_KEY` into proxied creator or media
  traffic. nginx must strip browser-supplied proxy identity headers before
  forwarding the request.
- The current bootstrap proxy key may remain only for local/lightweight,
  explicitly trusted single-user operation behind an explicit mode flag. It
  must default off in shared/public/production modes; invalid or ambiguous mode
  configuration fails startup or denies creator traffic.
- Existing personal API keys remain explicit machine credentials. The proxy
  must not replace a client credential with a global key. The API must define
  and test conflict behavior when session and API-key credentials coexist.
- `ADMIN_API_KEY` remains separate, is never injected by Studio, and is never
  derived from an OIDC session. Admin-route migration is out of scope.

### Forwarded-header contract

- Trust `X-Forwarded-Proto`, host, and client IP only from configured proxy
  peers/hops. Strip inbound identity headers and overwrite trusted forwarding
  fields at each trusted edge.
- Never use the leftmost unvalidated `X-Forwarded-For` value for security
  decisions. ACA documents that it overwrites `X-Forwarded-Proto` and exposes
  only the rightmost IP while other values require validation; this must still
  be proven for the actual ACA-to-nginx path.
- Secure-cookie, redirect-URI, and Origin calculations use a canonical external
  origin configured by the operator or forwarding fields accepted only from a
  trusted proxy. A client-supplied forwarding header must not downgrade HTTPS or
  change the callback host.
- nginx keeps explicit upload-size limits and secure response headers. Public
  ACA ingress additionally keeps `allowInsecure` disabled.

## Interim control and release boundary

Until every implementation child and end-to-end negative suite is complete:

- Keep the API on internal ingress only.
- Keep Studio private/internal. If an operator authorizes staging access, use a
  narrow IP allowlist in addition to authentication; do not describe an
  allowlist as user authentication.
- Keep deployment/promotion fail closed when the shared bootstrap-key mode is
  enabled or the session/OIDC configuration is incomplete.
- Do not enable a public hostname by merging documentation or code. Public
  ingress requires explicit operator approval, exact-head CI, a security-boundary
  review, and successful real ACA proxy evidence.

The IaC location and mechanism for the allowlist are not present on `main` and
PR #970 is documentation only. The infrastructure child must add a testable hook
rather than claiming a deployed control.

## Local/lightweight migration and compatibility

1. Characterize today's trusted nginx-key path before changing it.
2. Add explicit modes rather than inferring trust from a missing OIDC setting:
   `local-trusted` may inject the bootstrap key; `shared-session` may not.
   Names and configuration shape are open until the proxy child is designed.
3. Preserve current loopback-only API/Postgres/Redis bindings and local Docker
   quickstart. Lightweight mode does not require an IdP unless an operator
   explicitly selects shared-session mode.
4. Preserve personal API-key clients and per-key revocation. Document how a
   client reaches the API without the proxy replacing its credential.
5. Rollout order is deny-first: proxy mode and ingress guard, session/OIDC,
   provisioning, Studio UX, proxy hardening, then real-proxy security evidence.
   There is no compatibility window in which public traffic falls back to the
   bootstrap identity.
6. Rollback from shared-session mode closes external ingress; it does not
   reactivate a global public key.

## Test strategy

All implementation follows TDD and the high-risk gates in `AGENTS.md`.

### Focused and integration tests

- Characterize which identity wins for injected key, explicit API key, Bearer,
  session cookie, and conflicting credentials in each deployment mode.
- OIDC tests cover state, nonce, PKCE, exact redirect URI, issuer, audience,
  signature/key rotation, code replay, expired claims, provider error, and
  callback mix-up. No test uses a real provider credential.
- Session tests cover fixation resistance, rotation, idle/absolute expiry,
  logout/revocation, missing/invalid cookies, membership removal, CSRF missing,
  mismatch and replay, and safe-method behavior.
- Provisioning tests cover first login, repeat login, subject uniqueness,
  workspace creation/invitation policy, concurrent callbacks, and rejection of
  email-based account/workspace takeover.
- Proxy tests cover spoofed identity/API/forwarding headers, HTTPS downgrade,
  Host manipulation, upload limits, secure headers, and secret absence from
  generated nginx config, static bundles, source maps, responses, and logs.
- Authorization tests use users A and B in distinct workspaces. Anonymous and A
  must not create in, enumerate, read, approve, cancel, preview, range-read, or
  download B's projects/runs/assets. Foreign objects return 404; anonymous
  requests return 401. Include HTML video/image/audio and final MP4 paths.
- Admin tests prove creator sessions cannot enter `/api/admin/*` and that no
  admin credential reaches the browser path.

### Real nginx and ACA evidence

Unit tests cannot prove edge header rewriting, TLS forwarding, platform cookies,
IP restrictions, ACA Easy Auth behavior, or live redirect URIs. Before public
ingress, an authorized staging run must exercise the built Studio image through
the real ACA ingress and nginx to the internal API and record:

- client attempts to spoof every trusted/identity header;
- observed external scheme/host/IP and secure-cookie behavior;
- login, callback, logout, expiry, CSRF, membership removal, and key/session
  conflict behavior;
- A/B JSON and media isolation, including ranges and downloads;
- upload limits, logs, browser bundle/source-map inspection, and no secret/token
  disclosure;
- IP allowlist behavior and proof that direct API ingress is unavailable.

This run requires explicit operator approval and real Azure/Entra configuration.
CI and this ADR cannot claim it has occurred.

## Phased implementation split

The linked child issue numbers are added to #894 and #575 after creation. The
dependency order is:

1. **Proxy mode boundary (S):** remove shared nginx identity from shared mode,
   retain explicit local-trusted compatibility, and fail closed.
2. **OIDC login and server session (M):** authorization-code + PKCE endpoints,
   opaque sessions, logout/expiry, and provider validation.
3. **Cookie authentication and CSRF (M):** resolve sessions to `CurrentUser`,
   define credential conflicts, and enforce CSRF.
4. **First-login provisioning (M):** map provider subject to user and apply one
   explicit workspace membership/provisioning policy.
5. **Studio login UX (S):** login/logout/session state without browser-held
   provider tokens or secrets.
6. **Proxy/header/ingress hardening (M):** strip spoofable headers, trust only
   configured proxy hops, retain secure headers/upload limits, and add an
   interim private-ingress/IP-allowlist IaC hook and runbook.
7. **End-to-end negative security suite (M):** real nginx/API tests and an
   operator-run ACA evidence procedure covering identity and media isolation.

Children 2 and the local characterization portion of 1 may proceed in parallel;
3 depends on both. Provisioning depends on the verified session identity. Studio
UX depends on the session API. Proxy hardening depends on the mode boundary and
session contract. End-to-end evidence depends on all prior children and the
separate ACA topology/deployment prerequisites.

## Open questions

1. Which OIDC issuers are supported initially: one Entra tenant only, or a
   provider-neutral allowlist? No tenant, registration, or consent policy has
   been created by this ADR.
2. What are the exact callback/logout URLs and canonical public origin for each
   environment?
3. Which server-side session backend meets expiry, revocation, concurrency,
   availability, and cleanup requirements? Existing Redis/PostgreSQL presence
   does not itself decide this.
4. What idle/absolute lifetimes, cookie name/path/SameSite value, and CSRF-token
   rotation policy are required?
5. Are provider access/refresh tokens needed after login? Default: no; justify
   and encrypt them server-side if a future use requires retention.
6. Is first login invitation-only, does it create a personal workspace, or can
   an administrator pre-provision memberships? Email matching alone is rejected.
7. How are simultaneous session and personal API-key credentials handled? The
   answer must be explicit and must not let a proxy-injected credential mask a
   browser identity.
8. Does ACA Easy Auth add a defense-in-depth gate, and if so, how are its
   injected headers stripped/re-established through nginx and reproduced in
   non-ACA deployments?
9. Which trusted-proxy CIDRs/hop count and external-origin settings apply in
   ACA? This requires runtime evidence, not assumptions from local nginx.
10. Which existing routes intentionally remain anonymous (health, static SPA,
    or login bootstrap), and which response data is safe before authentication?
11. Where will the interim ACA IP allowlist be represented in IaC, given that
    the relevant topology PR is still documentation-only?

## Consequences

**Positive:** browser credentials are short-lived and server-controlled; the
existing user/membership/access model remains the authorization authority;
machine API keys and local mode remain compatible; ACA is not required for
application identity.

**Negative:** the API gains a high-risk session/OIDC/CSRF surface and stateful
session operations. Local and hosted modes require explicit configuration and
broader proxy/browser/integration tests.

**Neutral:** ACA Easy Auth may be defense in depth but does not replace local
user/workspace authorization. Public ingress remains a separate operator action.

## References

- [Security model](../SECURITY.md)
- [ADR-006: API Key Authentication Model](./006-api-key-authentication-model.md)
- [ADR-007: OAuth2/OIDC Integration](./007-oauth-oidc-integration.md)
- [Issue #894](https://github.com/yeongseon/short-form-studio/issues/894)
- [Issue #575](https://github.com/yeongseon/short-form-studio/issues/575)
- [PR #970](https://github.com/yeongseon/short-form-studio/pull/970)
- [Microsoft identity platform authorization-code flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow)
- [Azure Container Apps authentication and authorization](https://learn.microsoft.com/en-us/azure/container-apps/authentication)
- [Azure Container Apps ingress](https://learn.microsoft.com/en-us/azure/container-apps/ingress-overview)
