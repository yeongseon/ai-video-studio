# Contributing to AI Video Studio

Thank you for your interest in contributing! This guide will help you get started.

## Getting Started

1. **Fork** the repository
2. **Clone** your fork: `git clone https://github.com/<your-username>/short-form-studio.git`
3. **Create a branch**: `git checkout -b feat/your-feature`
4. **Make changes** and commit
5. **Push** and open a Pull Request

## Development Setup

### Prerequisites

- Python 3.12+
- Node.js 24 LTS (the exact release is pinned in `.node-version`; Node 20 is end of life)
- Docker and Docker Compose v2+

### Backend

```bash
# Install dependencies
pip install -r apps/api/requirements.txt -r apps/api/requirements-dev.txt
pip install -r apps/worker-orchestrator/requirements.txt -r apps/worker-orchestrator/requirements-dev.txt
# Install local packages
pip install -e packages/creator-domain -e packages/creator-service -e packages/creator-provider

# Run lint
python3 -m ruff check apps packages

# Run tests
PYTHONPATH=apps/api/src:packages/creator-domain:packages/creator-service:packages/creator-provider \
  python3 -m pytest apps/api/tests/ -x -v
```

### Frontend

```bash
cd apps/studio-web
npm ci
npm run dev        # Dev server
npm run build      # Production build
npx tsc --noEmit   # Type check
npm test -- --run  # Run tests
```

### Quick versus full checks

Run checks from the repository root with the Backend environment activated and
the local packages installed as above:

| Full target | Required work |
| --- | --- |
| `make lint` | Python ruff, compileall and basedpyright; Studio ESLint and TypeScript typecheck |
| `make test` | API, Worker, creator-domain, creator-provider, creator-service, then Studio Vitest |
| `make build` | Python compileall and the Studio production build |
| `make check` | Full lint, test, then build, sequentially even with parallel make |

Full targets fail rather than silently omit a family. Missing Studio dependencies
or Node/npm tools produce an error pointing to `npm --prefix apps/studio-web ci`.
Missing Python tools produce Backend installation guidance; install the runtime
and development requirements above (including pytest and ruff), and install
basedpyright as CI does (`python3 -m pip install basedpyright==1.29.2`). These
preflight failures happen before any check or suite runs.

The ordered family inventory is `TEST_SUITES` in the Makefile. Each Python family
uses a separate pytest invocation; the full test target **fails fast** on the first
failed family, without running later families. Existing suite-specific prerequisites
(for example FFmpeg or a disposable database for applicable tests) still apply.
Local checks are not a replacement for applicable CI checks on the final PR SHA,
including coverage and runtime gates.

For quick, **targeted** iteration, explicitly choose `make lint-python` or
`make test-python` (all five Python families, no frontend dependency required), or
`make lint-web`, `make test-web`, or `make build-web` (Studio only, no Python
toolchain required). These narrow targets are not a full-check pass. To run fewer
Python tests, invoke pytest with the desired file paths, separately per area.

`make install` retains its existing `npm install` behavior; CI uses `npm ci`.
Install/toolchain pin alignment is tracked separately in #1000.

## Code Guidelines

### General

- Follow existing code patterns and conventions
- Keep changes focused — one feature or fix per PR
- Add tests for new functionality
- Ensure all tests pass before submitting

### Python (Backend)

- Use type hints for all function signatures
- Follow existing Pydantic model patterns
- Use `ruff` for linting (config in `pyproject.toml`)
- Keep route handlers thin — business logic belongs in services

### TypeScript (Frontend)

- Use TypeScript strict mode — no `any` types
- Follow existing component patterns (hooks, pages, components)
- Use the shared types in `src/types/api.ts`
- Ensure `tsc --noEmit` passes with no errors

## Pull Request Process

1. **Describe your changes** clearly in the PR description
2. **Link related issues** (e.g., "Fixes #123")
3. **Ensure CI passes** — all tests, lint, build, and type checks
4. **Keep PRs small** — large PRs are harder to review
5. **Respond to feedback** promptly

## Reporting Bugs

- Use GitHub Issues with the bug report template
- Include steps to reproduce, expected vs actual behavior
- Include browser/OS/Docker version if relevant

## Requesting Features

- Open a GitHub Issue to discuss before implementing
- Describe the use case and why it's valuable
- Wait for maintainer feedback before starting work

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
