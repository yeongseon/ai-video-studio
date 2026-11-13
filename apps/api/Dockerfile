# --- build stage ---
FROM python:3.12-slim AS builder
WORKDIR /build
RUN pip install --no-cache-dir --upgrade pip
COPY packages ./packages
COPY apps/api/requirements.txt ./requirements.txt
COPY constraints.txt ./constraints.txt
RUN pip install --no-cache-dir --prefix=/install -c constraints.txt -r requirements.txt \
    ./packages/creator-domain \
    ./packages/creator-service \
    ./packages/creator-provider

# --- runtime stage ---
FROM python:3.12-slim AS runtime
ARG SOURCE_SHA
LABEL org.opencontainers.image.revision="${SOURCE_SHA}"
ENV SOURCE_SHA="${SOURCE_SHA}"
RUN groupadd --gid 1000 appuser && useradd --uid 1000 --gid appuser --create-home appuser
WORKDIR /app
COPY --from=builder /install /usr/local
COPY --chown=appuser:appuser apps/api/src ./src
COPY --chown=appuser:appuser apps/api/alembic.ini ./alembic.ini
COPY --chown=appuser:appuser apps/api/migrations ./migrations
RUN bash -euo pipefail -c 'mapfile -t revisions < <(DATABASE_URL=sqlite:///identity.db python -m alembic heads | awk "NF {print \$1}"); test "${#revisions[@]}" -eq 1; printf "%s" "${revisions[0]}" > /app/alembic-revision'
# Worker task modules needed by API for Celery task dispatch (import_module)
COPY --chown=appuser:appuser apps/worker-orchestrator/tasks ./tasks
COPY --chown=appuser:appuser apps/worker-orchestrator/celery_app.py ./celery_app.py
COPY --chown=appuser:appuser apps/worker-orchestrator/beat_health.py ./beat_health.py
COPY --chown=appuser:appuser apps/worker-orchestrator/worker_loop.py ./worker_loop.py
COPY --chown=appuser:appuser apps/worker-orchestrator/worker_release_identity.py ./worker_release_identity.py
COPY --chown=appuser:appuser scripts ./scripts
ENV PYTHONPATH=/app/src:/app
USER appuser
EXPOSE 8000
CMD ["uvicorn", "shorts_api.main:app", "--host", "0.0.0.0", "--port", "8000"]

# --- dev stage ---
FROM runtime AS dev
USER root
RUN pip install --no-cache-dir watchfiles
USER appuser
CMD ["uvicorn", "shorts_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
