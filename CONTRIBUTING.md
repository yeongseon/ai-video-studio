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
