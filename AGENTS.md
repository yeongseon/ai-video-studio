# Agent Rules

## TDD for All Implementation Work

All feature, bug-fix, and refactoring implementation MUST use TDD:

1. Write behavior or regression tests before changing production code.
2. Run them and confirm they fail for the intended reason (Red).
3. Implement the minimum change needed to pass (Green).
4. Refactor while keeping the tests green; do not weaken assertions to pass.
5. Run relevant regression tests, type checks, lint, and builds. Include the
   failing and passing commands/results in the implementation handoff or PR.

For existing behavior, add characterization tests before refactoring and record
the passing baseline; new or changed behavior still requires a failing test first.
Documentation-only changes use content/link checks rather than artificial tests.
The additional security and production-hardening gates below still apply.

## Small, Independently Verifiable Issues and PRs

Default to **S-sized** delivery. Size is an estimate of the *behavioral change*,
not an absolute file-count or line-count limit:

- **S:** one narrow outcome and one focused regression check (typically 1–3 production files).
- **M:** a cohesive change across a few components (typically 4–6 production files).
- **L/XL:** planning/tracking only; split into S/M implementation issues **before coding**.
- **Tracking issues:** describe dependencies, outcomes and child issue links; do not
  implement every child in one PR.

Before coding, inspect the issue for duplicated or already implemented work.
Each S/M implementation issue must state **one outcome**, relevant paths, acceptance
criteria, focused test command(s), and upstream dependencies. Prefer a single
small PR per issue. Do not split inseparable safety invariants into unsafe partial
deployments or create artificial issue/PR chains for trivial file edits.
If an implementation reveals a new cross-cutting dependency, stop and split or
re-scope instead of silently expanding the PR.

## Risk-Based Development Gates

Keep TDD and authorization/data-integrity guarantees. Gate *proportionally*:

| Change tier | Iteration gate | Merge gate |
| --- | --- | --- |
| Documentation/config wording only | Content, link and diff hygiene checks | Lightweight docs checks; no irrelevant media/runtime jobs |
| Standard isolated feature, UI, or bug | Focused Red/Green test, relevant lint/typecheck | Green applicable PR CI at **final head SHA**; review proportionate to impact |
| High-risk: auth, identity/IDOR, secret handling, DB/migrations, artifact access/deletion, workspace quotas, concurrency, queue recovery, media output integrity, public ingress, release/rollback | Focused security/regression tests and relevant integration/runtime suites | Security boundary review, all triggered high-risk CI checks green on final SHA, critical/major findings fixed |
| Release / deployment to a real environment | Validated manifest and environment-specific smoke | **Complete CI on the same SHA**, explicit deployment approval, rollback readiness |

- During development run focused tests and relevant static checks; do not repeatedly
  run the entire repository suite after each small edit.
- A single review of the final change with actionable findings is the default.
  Oracle or another scoring tool may assist but **no arbitrary 100/100 score or
  repeated full re-review is a universal merge requirement**.
- For high-risk changes, document the relevant boundary threat/negative test
  (including 404 anti-enumeration), ownership/fencing expectations and a reviewer
  sign-off. Re-run checks that a follow-up modification could invalidate.
- Do not downgrade a security-sensitive change to standard because a diff is small.
  If scope/classification is uncertain, require the higher gate; paths used to
  skip expensive PR jobs are deliberately conservative.
- On `main` the entire existing CI matrix remains required as a release candidate
  signal; a green PR selection alone does not certify production readiness.
- No skip/xfail, weaker assertions, weakened coverage thresholds or bypassed
  authorization may be introduced just to make a build green.

## CI Failure Handling

This section applies whenever an applicable CI job fails on a pull request or during
implementation. High-risk security boundaries and 404 anti-enumeration must not be weakened.

### Classify the cause first

Read the failing job log and reproduce the failure locally when possible. Then label it:

- **Change-related regression**: the current change caused it.
- **Pre-existing defect**: a real defect that already existed and was exposed by the change or by CI.
- **Infrastructure, external dependency or flaky failure**: runner, registry, network, an external
  service, or a nondeterministic test.
- **Test/environment prerequisite gap**: a test needs something (FFmpeg, an import path, a Compose
  fixture) that CI does not declare.

### Change-related regressions

- Fix them in the same PR. Add or strengthen a regression test that fails for the intended reason
  first (Red), then passes (Green).
- Re-run focused tests, then every CI check applicable to the changed risk boundaries.
- Flow: implement → reproduce → regression test → fix → focused tests → applicable CI →
  exact final head SHA green → review → merge.

### Newly discovered defects (issue-first)

When CI exposes a defect that the change did not cause:

1. Check whether an issue already covers it.
2. If not, open an issue first (or add it explicitly to the current issue's scope), with the evidence.
3. If it affects correctness, security, data integrity, or release readiness, fix it before merging.
4. If it does not block, it may be deferred explicitly: record the evidence and a priority on the
   issue and link it from the PR.
5. The minimal fix needed to finish the current PR safely may ride in the same PR once the issue is
   linked. Never mix in unrelated refactors or cleanup.

### Infrastructure, external and flaky failures

- Do not work around them in application code.
- Confirm whether the failure reproduces and what the external cause is.
- If the CI environment or a fixture is at fault, fix it and add a check that would catch a recurrence.
- A green re-run does not count as a fix for a flaky or infrastructure failure.

### Prerequisite gaps

Declare what the test really needs and keep the test. Example: a real-media render test fails on the
runner because FFmpeg is missing. Install FFmpeg and ffprobe in the CI job, add an executable and
version check, keep the render test, and re-run the full CI. Do not skip the test.

### Never make CI green by

- weakening an assertion
- adding a skip or xfail marker without evidence
- disabling a required check
- lowering a coverage threshold
- suppressing type or lint errors
- deleting a failing test
- removing a security or authorization check
- retrying a failing job until it passes in order to hide the failure

### Merge gate

- Merge only after the **applicable PR CI checks** are green on the exact final
  head SHA. Changes that touch protected boundaries must include their dedicated
  negative/regression tests and triggered runtime gates.
- A green result from an earlier SHA is not evidence for the final head. After each push,
  re-run the checks relevant to changes made since the last green verification.
- Configure `ci-required` as the single required status check. It fails unless the
  impact classifier succeeds and every selected gate succeeds; a selected skipped
  job is a failure. Pull-request gate selections must be explicit `true` or `false`;
  malformed or missing classifier outputs fail closed. On `main`, every gate is selected.
- Only documentation-only changes may use reduced PR gates. Canonical case-sensitive
  `docs/**/*.md` and root-level `*.md` regular files select only the `docs` gate. Every
  other change, including tests, fixtures, frontend, scripts, config, backend,
  infrastructure, and unknown paths, selects the full matrix. This deliberate policy
  narrowing restores pre-#916 behavior outside documentation; widening reduced classes
  later requires evidence and Oracle review.
- Documentation-only changes use content and consistency checks (diff hygiene, links,
  referenced paths and commands) in the dedicated `docs` gate, rather than unrelated
  Python/FFmpeg/Docker gates. Non-canonical paths, symlinks, and executable-mode changes
  cannot use the documentation-only classification.
- The documentation-consumer inventory is a conservative static text heuristic, not a
  proof against contrived dynamic path construction. A PR that adds a documentation
  consumer must add its invoking test to the `docs` job or add a reviewed allowlist entry
  with a true reason; reviewers must verify that boundary when new consumers are added.
- Full end-to-end, container and recovery checks run on `main`; run the same
  complete workflow on the exact release SHA before deployment. Never deploy
  based solely on a fast PR gate.

This is a working rule, not automation: it does not add a CI auto-repair bot or unlimited retries.

## Access Control Pattern

All `/api/creator/` routes MUST use one of:
- `require_run_access(run_id)` — returns `(CurrentUser, PipelineRun)`, verifies workspace ownership
- `require_project_access(project_id)` — returns `(CurrentUser, Project)`, verifies workspace ownership
- `require_workspace_access(workspace_id)` — verifies user membership

Anti-enumeration: always return 404 (never 403) for unauthorized access.

## Test Fixture Pattern

Test files using routes with access control dependencies must add overrides:

```python
from shorts_api.auth import CurrentUser, require_run_access
from shorts_api.main import app

async def _require_run_access(run_id: int) -> tuple[CurrentUser, StubPipelineRun]:
    run = run_svc.runs.get(run_id)
    if run is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Run not found")
    return CurrentUser(user_id=1, workspace_id=1), run

app.dependency_overrides[require_run_access] = _require_run_access
yield ...
app.dependency_overrides.pop(require_run_access, None)
```

## External Access (File Serving)

When the user needs to view generated artifacts (videos, images, audio):

1. Start an HTTP server bound to **loopback only**, scoped to the artifact directory:
   `python3 -m http.server <port> --bind 127.0.0.1 --directory <artifact_dir>`
2. Give the user the loopback URL (e.g. `http://127.0.0.1:<port>/<file>`). This is the default path — no external exposure is performed automatically.
3. **External exposure requires explicit user confirmation.** If (and only if) the user asks to expose an artifact beyond the local machine, offer ONE of:
   - **localtunnel** (no auth): `npx -y localtunnel --port <port>`
   - **ngrok** (needs authtoken in `~/.config/ngrok/ngrok.yml`): `ngrok http <port>`
   - **serveo.net** (unreliable): `ssh -R 80:localhost:<port> serveo.net`
4. (Only if Step 3 was followed) Verify the public URL returns 200 before handing it over: `curl -s -H "Bypass-Tunnel-Reminder: true" <url> -o /dev/null -w "%{http_code}"`
5. Serve ONLY the artifact directory — never the repository root or any directory containing source, secrets, or `.env`.

**IMPORTANT RULES (security):**
- **ALWAYS bind to `127.0.0.1`** (loopback). NEVER use `--bind 0.0.0.0`. This aligns with the loopback policy in `docs/SECURITY.md`.
- **NEVER expose externally without asking the user first.** Do not run localtunnel/ngrok/serveo automatically.
- When exposing, prefer localtunnel (no signup/authtoken required); if it fails, ask the user before trying the next method.
- Always verify a public URL returns 200 before giving it to the user.
- localtunnel requires header `Bypass-Tunnel-Reminder: true` for programmatic access.
