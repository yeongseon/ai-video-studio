install:
	python3 -m pip install -c constraints.txt -r apps/api/requirements.txt -r apps/worker-orchestrator/requirements.txt
	npm --prefix apps/studio-web install

dev:
	@echo "Run services in separate terminals:"
	@echo "- API: uvicorn shorts_api.main:app --app-dir apps/api/src --reload"
	@echo "- Worker: celery -A celery_app.celery_app worker --workdir apps/worker-orchestrator --loglevel=info"
	@echo "- Web: npm --prefix apps/studio-web run dev"

# Full checks: lint, test, build; check runs all three in order, failing fast.
# Targeted iteration only: lint-python, test-python, lint-web, test-web, build-web.
# Ordered CI-required families; each Python family gets its own pytest process.
TEST_SUITES := api worker creator-domain creator-provider creator-service studio
PYTHON_INSTALL := python3 -m pip install -r apps/api/requirements.txt -r apps/api/requirements-dev.txt -r apps/worker-orchestrator/requirements.txt -r apps/worker-orchestrator/requirements-dev.txt
WEB_INSTALL := npm --prefix apps/studio-web ci

.PHONY: lint test build check lint-python test-python lint-web test-web build-web \
	_check-web _check-python _check-lint-python _check-test-python \
	$(addprefix _test-,$(TEST_SUITES))

_check-web:
	@command -v npm >/dev/null && command -v node >/dev/null && command -v npx >/dev/null && \
		test -d apps/studio-web/node_modules || \
		{ echo "Studio toolchain missing: activate Node/npm, then run $(WEB_INSTALL)" >&2; exit 1; }

_check-python:
	@command -v python3 >/dev/null || \
		{ echo "python3 missing: activate Python 3.12+, then run $(PYTHON_INSTALL)" >&2; exit 1; }

_check-lint-python: _check-python
	@for tool in ruff basedpyright; do \
		command -v $$tool >/dev/null || \
		{ echo "$$tool missing: activate the Backend environment; run $(PYTHON_INSTALL); install basedpyright as in CI" >&2; exit 1; }; \
	done

_check-test-python: _check-python
	@python3 -c 'import pytest' 2>/dev/null || \
		{ echo "pytest missing: activate the Backend environment; run $(PYTHON_INSTALL)" >&2; exit 1; }

define LINT_PYTHON
	ruff check apps packages
	python3 -m compileall apps packages
	basedpyright apps packages
endef

define LINT_WEB
	npm --prefix apps/studio-web run lint
	cd apps/studio-web && npx --no-install tsc --noEmit
endef

lint: _check-web _check-lint-python
	$(LINT_PYTHON)
	$(LINT_WEB)

lint-python: _check-lint-python
	$(LINT_PYTHON)

lint-web: _check-web
	$(LINT_WEB)

format:
	ruff format apps packages
	@if [ -d apps/studio-web/node_modules ]; then npm --prefix apps/studio-web run format; else echo "Skipping studio-web format (run make install first)"; fi

test: _check-web _check-test-python
	@set -e; for suite in $(TEST_SUITES); do $(MAKE) _test-$$suite; done

test-python: _check-test-python
	@set -e; for suite in $(filter-out studio,$(TEST_SUITES)); do $(MAKE) _test-$$suite; done

test-web: _check-web
	npm --prefix apps/studio-web run test

_test-api:
	cd apps/api && python3 -m pytest tests/ -v

_test-worker:
	cd apps/worker-orchestrator && python3 -m pytest tests/ -v

_test-creator-domain _test-creator-provider _test-creator-service:
	python3 -m pytest packages/$(patsubst _test-%,%,$@)/tests/ -v

_test-studio:
	npm --prefix apps/studio-web run test

build: _check-web _check-python
	python3 -m compileall apps packages
	npm --prefix apps/studio-web run build

build-web: _check-web
	npm --prefix apps/studio-web run build

check: _check-web _check-lint-python _check-test-python
	$(MAKE) lint
	$(MAKE) test
	$(MAKE) build

docker-up:
	docker compose up -d

docker-down:
	docker compose down

docker-up-all:
	docker compose --profile monitoring up -d

docker-logs:
	docker compose logs -f

# --- Local Server (private by default, LAN opt-in) ---
# STUDIO_WEB_BIND / STUDIO_WEB_LAN_OK reach the recipes through make's own environment and
# command-line passing. Do not `export` them here: exporting an unset variable hands the recipe
# an EMPTY value, which the validator (rightly) rejects, breaking the private default.

docker-local-up:
	STUDIO_WEB_BIND="$$(python3 scripts/validate_local_bind.py)" && export STUDIO_WEB_BIND && \
	docker compose -f docker-compose.yml -f docker-compose.local-server.yml up -d postgres redis api worker beat studio-web

docker-local-build:
	STUDIO_WEB_BIND="$$(python3 scripts/validate_local_bind.py)" && export STUDIO_WEB_BIND && \
	docker compose -f docker-compose.yml -f docker-compose.local-server.yml up -d --build postgres redis api worker beat studio-web

docker-local-migrate:
	docker compose run --rm api alembic upgrade head

docker-local-bootstrap:
	docker compose run --rm api python scripts/create_api_key.py --email local@example.com --workspace local --name local-server
	@echo ""
	@echo "Copy the generated API key into .env as API_KEY, then run:"
	@echo "  make docker-local-up"

docker-local-logs:
	docker compose -f docker-compose.yml -f docker-compose.local-server.yml logs -f api worker beat studio-web

docker-local-down:
	docker compose -f docker-compose.yml -f docker-compose.local-server.yml down

docker-local-status:
	docker compose -f docker-compose.yml -f docker-compose.local-server.yml ps

# --- Dependency locking ---
# Two lockfiles serve different purposes:
#   uv.lock           — the uv workspace: the three packages with every provider extra
#   constraints.txt   — what the API and Worker images, CI and `make install` install,
#                       generated from constraints.in (see the Python dependencies guide)
#
# lock:                    regenerate uv.lock.
# lock-constraints:        regenerate constraints.txt from constraints.in. Existing pins are
#                          kept as preferences, so only what a changed input forces moves;
#                          review the diff. Use the uv version that CI pins.
# lock-constraints-check:  fail unless constraints.txt is what that regeneration produces.
UV_COMPILE_CONSTRAINTS = uv pip compile constraints.in --python-version 3.12 \
	--python-platform linux --no-header --no-emit-package creator-domain \
	--no-emit-package creator-provider --no-emit-package creator-service

lock:
	uv lock
	@echo "uv.lock regenerated"

lock-constraints:
	$(UV_COMPILE_CONSTRAINTS) --output-file constraints.txt
	@echo "constraints.txt regenerated from constraints.in: review the diff"

lock-constraints-check:
	@tmp="$$(mktemp)" && trap 'rm -f "$$tmp"' EXIT && cp constraints.txt "$$tmp" \
		&& $(UV_COMPILE_CONSTRAINTS) --output-file "$$tmp" --quiet \
		&& diff -u constraints.txt "$$tmp" \
		&& echo "constraints.txt matches constraints.in"

lock-check:
	uv lock --check

# Bump package version; optional tag-only suffix: make release VERSION=0.5.0 TAG_SUFFIX=-e2e
release:
	@test -n "$(VERSION)" || { echo "Usage: make release VERSION=0.5.0 [TAG_SUFFIX=-e2e]"; exit 1; }
	python3 -c "import re; s=open('pyproject.toml').read(); s=re.sub(r'^version = \".*\"', 'version = \"$(VERSION)\"', s, flags=re.M); open('pyproject.toml','w').write(s)"
	@echo "Bumped pyproject.toml to $(VERSION). Review, commit, then: git tag v$(VERSION)$(TAG_SUFFIX) && git push --tags"
