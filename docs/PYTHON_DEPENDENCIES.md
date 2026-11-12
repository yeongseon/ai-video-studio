# Python dependencies

Two files pin Python packages, for two different scopes. Neither is a copy of the other, and they
are not meant to agree.

| | `constraints.txt` | `uv.lock` |
|---|---|---|
| Scope | What the deployable runtime and CI install: the API and Worker requirements, their dev tools, the three workspace packages **without** provider extras, and Playwright | The whole uv workspace (`creator-domain`, `creator-service`, `creator-provider`) with **every** provider extra |
| Installed by | Every `pip install -c constraints.txt`: the API and Worker images, the CI jobs, `make install` | Nothing. CI only runs `uv lock --check` on it |
| Generated from | `constraints.in`, with `make lock-constraints` | The package metadata, with `make lock` |
| Platform | CPython 3.12 on Linux | Universal |

## Why the versions differ

`uv.lock` resolves the workspace with every optional provider SDK, and some of them cap shared
libraries: `stability-sdk` 0.8.6 requires `grpcio<1.64`, `grpcio-tools<1.64` and `protobuf<6`, and
`google-ai-generativelanguage` 0.6.15 requires `protobuf<6` (PyPI metadata, read 2026-11-12). The
lock therefore holds `protobuf` 5.29.6 and `grpcio` 1.63.2, while the deployment closure, which
installs no provider extra, runs `protobuf` 6.33.6 and `grpcio` 1.85.1. The two applications
(`apps/api`, `apps/worker-orchestrator`) are not members of the uv workspace and declare their
own exact pins in their requirements files. Do not force the two files to agree: the deployment
file is what is tested and shipped.

## What `constraints.txt` contains

`constraints.in` lists the inputs: the four requirements files, the three workspace packages and
`playwright`. Each package that those inputs pull in is pinned once, to one exact version, with an
annotation that says why (`# via -r apps/api/requirements.txt`, `# via aiohttp`). Nothing else is
in the file: not a package for another project, not an operating-system package, not an optional
provider SDK that no consumer installs. If a deployment needs a provider extra, add a consumer for
it and its input to `constraints.in` in the same change.

Tools that CI installs without the file (`uv`, `basedpyright`) and build backends are outside it.

## Regenerating and checking

```bash
make lock-constraints         # regenerate constraints.txt from constraints.in
make lock-constraints-check   # fail unless constraints.txt is what that produces
```

Use the `uv` version that the CI lint job pins; another version may format the annotations
differently. Regeneration keeps every existing pin as a preference, so only what a changed input
forces moves, and the diff shows exactly that. To move one package on purpose, add
`--upgrade-package NAME` to the `uv pip compile` line printed by the target, or edit the
requirement, then regenerate and review the diff with the release notes at hand.

The CI lint job runs the check. It fails for a pin that no input needs, a pin that was removed, a
version that conflicts with a requirement, and a requirement that changed without regeneration. It
accepts a hand-edited version that still satisfies every input, because the file is a lock, not a
promise that each version is the newest. `apps/api/tests/test_python_constraints.py` checks the
offline half: pins are exact and unique, every pin carries an annotation that names a requirements file,
a workspace package or another pin, the exact pins of the requirements files agree with it, and `constraints.in` declares exactly what
the `pip install -c constraints.txt` commands in CI, the Makefile and the Dockerfiles use.

## State when this was written (2026-11-12)

The file pins 93 packages, which is exactly what pip installs for every consumer under the previous
file: nothing that was installed changed version. Four of them, `grpcio` and three
`opentelemetry-*` exporter and protocol packages, had been installed without any pin before.
A scan of the pinned versions against the public OSV database found published advisories for
16 of the 93 packages, 11 of which ship in an image; they are tracked in #1023 and were
deliberately left unchanged here.
