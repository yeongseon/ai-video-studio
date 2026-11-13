# --- build stage ---
FROM python:3.12-slim AS builder
WORKDIR /build
RUN pip install --no-cache-dir --upgrade pip
COPY packages ./packages
COPY apps/worker-orchestrator/requirements.txt ./requirements.txt
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
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid appuser --create-home appuser \
    && install -d -o appuser -g appuser /var/lib/short-form-studio/dlq
WORKDIR /app
COPY --from=builder /install /usr/local
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 fonts-nanum && rm -rf /var/lib/apt/lists/* && fc-cache -f
COPY --chown=appuser:appuser apps/worker-orchestrator/tasks ./tasks
COPY --chown=appuser:appuser apps/worker-orchestrator/celery_app.py ./celery_app.py
COPY --chown=appuser:appuser apps/worker-orchestrator/beat_health.py ./beat_health.py
COPY --chown=appuser:appuser apps/worker-orchestrator/worker_loop.py ./worker_loop.py
COPY --chown=appuser:appuser apps/worker-orchestrator/worker_release_identity.py ./worker_release_identity.py
USER appuser
CMD ["celery", "-A", "celery_app.celery_app", "worker", "--loglevel=info", "--pool=solo", "--concurrency=1"]

# --- dev stage ---
FROM runtime AS dev
USER root
RUN pip install --no-cache-dir watchfiles
USER appuser
CMD ["sh", "-c", "watchfiles 'celery -A celery_app.celery_app worker --loglevel=info --pool=solo' /app"]
