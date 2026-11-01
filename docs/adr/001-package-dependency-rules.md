# ADR-001: Package Dependency Rules

**Status**: Accepted  
**Date**: 2026-05-12  
**Decision makers**: @yeongseon

## Context

The monorepo has four internal packages and two application layers. As the project grows, unconstrained cross-dependencies would create circular imports, tight coupling, and difficult-to-test code.

## Decision

The dependency graph flows **strictly downward**:

```
apps/api  ─┬─→ creator-service ──→ creator-domain
            └─→ creator-provider ──→ creator-domain

apps/worker-orchestrator ─┬─→ creator-service ──→ creator-domain
             └─→ creator-provider ──→ creator-domain
```

The [canonical static import graph](../architecture/SYSTEM_TOPOLOGY.md#static-import-dependencies)
labels every arrow as an import, separate from runtime requests and queue traffic.
The application paths above are composition roots, not packages scanned by the
package-only AST gate. Its policy is reused by the diagram checker for package
edges; application diagram edges follow the accepted rules below.

### Rules

1. **`creator-domain`** is the leaf package. It has **zero** internal dependencies. It defines entities, value objects, enums, and port interfaces.

2. **`creator-service`** depends on `creator-domain` only. It implements use cases, storage adapters, and business logic.

3. **`creator-provider`** depends on `creator-domain` only. It implements LLM, Image, TTS, and STT provider adapters. It does **NOT** depend on `creator-service`.

4. **`apps/api`** and **`apps/worker-orchestrator`** depend on both `creator-service` and `creator-provider`. They are the composition root where dependencies are wired together.

5. **No lateral dependencies**: `creator-service` and `creator-provider` must never import from each other.

6. **No upward dependencies**: packages must never import from apps.

### Why provider does NOT depend on service

Providers are I/O adapters (HTTP calls to external APIs). Services are business logic orchestrators. Keeping them independent allows:
- Testing providers without mocking business logic
- Swapping providers without touching service code
- Deploying provider updates independently

## Verification

Run the AST-based gate from the repository root (Python 3.12+, no installed
application packages or third-party dependencies needed):

```bash
python3 scripts/check_package_dependencies.py
```

An optional positional repository root supports checking another checkout. Without
it, the script locates the repository relative to itself, independent of the
working directory. Exit codes: **0** for no diagnostics, **1** for violations or
incomplete analysis, **2** for invalid command-line usage. Diagnostics contain
repository-relative file paths, source lines, and import targets.

CI entry for the workflow owner to add after Python setup:

```yaml
- name: Enforce ADR001 package dependencies
  run: python3 scripts/check_package_dependencies.py
```

Checker regression suite (also add to CI's test job):

```bash
python3 -m pytest tests/test_package_dependency_cli.py tests/test_package_dependency_parser.py -q
```

The gate parses `.py` files recursively inside the three production import roots:
`packages/creator-domain/creator_domain`,
`packages/creator-service/creator_service`, and
`packages/creator-provider/creator_provider`. Sibling package `tests/` directories
are outside those roots. There are no per-file ignores, baselines, or exemptions.
Missing/empty roots, unreadable files, syntax errors, and unresolved recognized
dynamic imports fail the check.

It checks `import`, `from ... import`, aliases, relative imports (including package
initializers), and literal `importlib.import_module` / `__import__` calls, including
direct import aliases. Lazy and `TYPE_CHECKING` imports are included. Comments,
docstrings, and ordinary strings are not imports. Application roots include
`shorts_api`, `tasks`, `celery_app`, `worker_loop`, and the repository-qualified
`apps` namespace. Same-package and third-party imports remain allowed.

This is a bounded static analysis, not a proof about arbitrary Python execution.
Recognized loaders with computed targets fail closed rather than being skipped.
Loader aliases are conservatively accumulated across scopes, so shadowing a
loader name may need clarification in source. Assignment-based loader forwarding,
`getattr`-constructed loaders, `exec`, custom plugin loaders, and manipulation of
`sys.path` are not resolved. New application import roots must be added to the
checker's `APP_ROOTS` and tests. Imported application modules are never executed.

## Consequences

- New shared types/interfaces must go in `creator-domain`, not copied across packages
- If a service needs provider functionality, it must go through a port interface defined in domain
- Apps are the only place where service + provider can be combined

## Issue #621 implementation audit (2026-09-22)

**The issue's assertion that production already complies is incorrect.** The rules
above remain the intended architecture; the gate deliberately fails on the
following observed debt. This inventory explains failures; it is not an allowlist.
At the audited checkout there were **seven blocking diagnostics**:

| Service source | Dependency / diagnostic | Required boundary correction |
| --- | --- | --- |
| `actionable_errors.py` | `creator_provider.exceptions` | Translate provider exceptions into domain errors at the provider/app boundary; service error presentation consumes domain errors. Coordinate `build_run_failure_summary` and worker `tasks/task_runner.py` callers. |
| `provider_facts.py` | `creator_provider.registry.ModelCatalogEntry` | Put the provider-neutral catalog contract in domain and adapt/re-export it from the provider registry. Service facts and catalog/readiness/config consumers should depend on that contract. |
| `voice_preset.py` | `creator_provider.api_keys` | Inject a credential-presence port from the composition root instead of resolving provider keys inside service. |
| `voice_preset.py` | `creator_provider.registry` | Consume a domain registry/catalog port and provider-neutral category contract. Preserve unsupported-model and missing-credential behavior with regression tests. |
| `task_dispatch_service.py` | `import_module(module_name)` (unresolved) | Move worker loading and synchronous/Celery execution behind an injected domain dispatch port implemented and wired at the app boundary. |
| `task_dispatch_service.py` | `__import__("celery_app")`, two revoke sites | Move cancellation into the same app-owned dispatch adapter. |

The computed dispatch loader is an actual upward dependency, not just a theoretical
unknown. Its current `_dispatch_task` callers pass these worker module names:

- `tasks.generate_script`
- `tasks.generate_visual_plan`
- `tasks.generate_audio`
- `tasks.generate_subtitles`
- `tasks.render_video`
- `tasks.generate_scene_image`
- `tasks.generate_paragraph_audio`
- `tasks.generate_paragraph_subtitles`

That bridge supports both Celery and lightweight synchronous mode. Moving it
requires coordinated API/worker wiring and dispatch, cancellation, atomicity,
telemetry, and lightweight-mode regression coverage. It is **not exempted** by this
change. Likewise, the lateral seams are not grandfathered merely because they
already exist. Resolve these boundaries in coordinated changes rather than
moving imports into function bodies, dynamic loaders, or type-only branches.

The checker implementation is restricted to the gate, its tests, and this ADR
addendum while other work owns application/media/render files. Workflow wiring is
owned separately. Until the listed boundaries are corrected, wiring the exact CI
command above will correctly produce a red gate; do not use `continue-on-error`
or an all-files baseline to represent it as compliant.

### Boundary implementation (2026-09-22)

The seven diagnostics above are now resolved in production source:

- `creator_domain.provider_errors` owns the neutral failure hierarchy.
  `creator_provider.exceptions` re-exports those exact classes and retains HTTP
  error translation. Service error presentation consumes the domain classes.
- `creator_domain.provider_catalog` owns `ModelCatalogEntry`, `ProviderCategory`,
  and the catalog read protocols. The provider registry re-exports the values;
  service facts and voice selection depend only on domain contracts.
- Voice credential presence is an injected boolean callback, defaulting to a
  stripped environment-variable presence check. Credentials never enter a result.
- `creator_domain.task_dispatch.TaskDispatcher` is the submission/cancellation
  port. Service dispatch is split into call wrappers, CAS coordination, quota
  compensation, and runtime contracts. The existing service API remains available.
- `shorts_api.task_dispatch_adapter` owns worker task loading, Celery enqueue and
  termination, trace headers, and lightweight execution. `create_app()` explicitly
  configures the service singleton with that adapter. Standalone service users
  must inject a dispatcher; no package imports an application to discover one.

The unchanged AST gate reports **zero diagnostics**, with no exemptions. The
production-tree regression is `tests/test_package_dependency_compliance.py`.
Dispatch argument, pending/queued idempotency, rollback/revocation, provider import
identity, and real-bound-task lightweight tests cover the corrected seams. The
real first-short acceptance test exercises application initialization, PostgreSQL,
Redis, a separate Celery worker, and downloadable FFmpeg output.
