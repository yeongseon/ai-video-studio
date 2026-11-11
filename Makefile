install:
	python3 -m pip install -c constraints.txt -r apps/api/requirements.txt -r apps/worker-orchestrator/requirements.txt
	npm --prefix apps/studio-web install

dev:
	@echo "Run services in separate terminals:"
	@echo "- API: uvicorn shorts_api.main:app --app-dir apps/api/src --reload"
	@echo "- Worker: celery -A celery_app.celery_app worker --workdir apps/worker-orchestrator --loglevel=info"
	@echo "- Web: npm --prefix apps/studio-web run dev"

lint:
	ruff check apps packages
	python3 -m compileall apps packages
	@if [ -d apps/studio-web/node_modules ]; then npm --prefix apps/studio-web run lint; else echo "Skipping studio-web lint (run make install first)"; fi

format:
	ruff format apps packages
	@if [ -d apps/studio-web/node_modules ]; then npm --prefix apps/studio-web run format; else echo "Skipping studio-web format (run make install first)"; fi

test:
	cd apps/api && python3 -m pytest tests/ -v
	cd apps/worker-orchestrator && python3 -m pytest tests/ -v
	@if [ -d apps/studio-web/node_modules ]; then npm --prefix apps/studio-web run test; else echo "Skipping studio-web tests (run make install first)"; fi

build:
	python3 -m compileall apps packages
	@if [ -d apps/studio-web/node_modules ]; then npm --prefix apps/studio-web run build; else echo "Skipping studio-web build (run make install first)"; fi

docker-up:
	docker compose up -d

docker-down:
	docker compose down

docker-up-all:
	docker compose --profile monitoring up -d

docker-logs:
	docker compose logs -f

# --- Local Server (LAN-accessible) ---

docker-local-up:
	docker compose -f docker-compose.yml -f docker-compose.local-server.yml up -d postgres redis api worker beat studio-web

docker-local-build:
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
#   uv.lock           — workspace packages (creator-domain/service/provider)
#   constraints.txt   — full deployment pin (api + worker + packages + transitive)
#
# lock:       regenerate uv.lock for workspace packages.
#             constraints.txt must be regenerated separately when api/worker
#             requirements change (see lock-constraints below).
lock:
	uv lock
	@echo "uv.lock regenerated"

lock-constraints:
	uv pip compile apps/api/requirements.txt apps/api/requirements-dev.txt \
		apps/worker-orchestrator/requirements.txt \
		apps/worker-orchestrator/requirements-dev.txt \
		--output-file constraints.txt
	@echo "constraints.txt regenerated"

lock-check:
	uv lock --check

# Bump package version; optional tag-only suffix: make release VERSION=0.5.0 TAG_SUFFIX=-e2e
release:
	@test -n "$(VERSION)" || { echo "Usage: make release VERSION=0.5.0 [TAG_SUFFIX=-e2e]"; exit 1; }
	python3 -c "import re; s=open('pyproject.toml').read(); s=re.sub(r'^version = \".*\"', 'version = \"$(VERSION)\"', s, flags=re.M); open('pyproject.toml','w').write(s)"
	@echo "Bumped pyproject.toml to $(VERSION). Review, commit, then: git tag v$(VERSION)$(TAG_SUFFIX) && git push --tags"
