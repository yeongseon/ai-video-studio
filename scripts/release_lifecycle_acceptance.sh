#!/usr/bin/env bash
set -euo pipefail
umask 077

NETWORK="release-lifecycle-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"
POSTGRES="${NETWORK}-postgres"
API="${NETWORK}-api"
STATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/release-lifecycle.XXXXXX")"
EVIDENCE_DIR="${RELEASE_LIFECYCLE_EVIDENCE_DIR:-$STATE_DIR/evidence}"
DATABASE_URL="postgresql://review:review-local-only@${POSTGRES}:5432/release_lifecycle"
LIFECYCLE_N_HEAD="${LIFECYCLE_N_HEAD:?required}"
LIFECYCLE_N_PLUS_ONE_HEAD="${LIFECYCLE_N_PLUS_ONE_HEAD:?required}"

cleanup() {
  docker rm -f "$API" "$POSTGRES" >/dev/null 2>&1 || true
  docker run --rm -v "$STATE_DIR:/state" alpine:3.21 rm -rf /state/artifacts >/dev/null 2>&1 || true
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
  rm -rf "$STATE_DIR"
}
trap cleanup EXIT

docker network create "$NETWORK" >/dev/null
mkdir -p "$EVIDENCE_DIR"
docker run -d --name "$POSTGRES" --network "$NETWORK" \
  -e POSTGRES_USER=review -e POSTGRES_PASSWORD=review-local-only \
  -e POSTGRES_DB=release_lifecycle postgres:16.16-alpine@sha256:0adbf25a9e96d38823bb813f3ac0ed41fadb665c88f1df7fe6dbfb060cbdc106 >/dev/null
timeout 60 bash -c "until docker exec '$POSTGRES' pg_isready -U review; do sleep 1; done"

docker run --rm --network "$NETWORK" -e DATABASE_URL="$DATABASE_URL" release-lifecycle-n alembic upgrade head
docker run --rm --network "$NETWORK" -e DATABASE_URL="$DATABASE_URL" release-lifecycle-n \
  python scripts/create_api_key.py --email lifecycle@example.com --workspace lifecycle --name lifecycle-ci --json \
  > "$STATE_DIR/key.json"
API_KEY="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["api_key"])' < "$STATE_DIR/key.json")"
WORKSPACE_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["workspace_id"])' < "$STATE_DIR/key.json")"

start_api() {
  local image="$1"
  docker rm -f "$API" >/dev/null 2>&1 || true
  docker run -d --name "$API" --network "$NETWORK" -p 127.0.0.1:58080:8000 \
    -e DATABASE_URL="$DATABASE_URL" -e ARTIFACT_ROOT=/tmp/release-artifacts \
    -v "$STATE_DIR/artifacts:/tmp/release-artifacts" "$image" >/dev/null
  timeout 60 bash -c 'until curl -sf http://127.0.0.1:58080/healthz >/dev/null; do sleep 1; done'
}

mkdir -p "$STATE_DIR/artifacts"
chmod 777 "$STATE_DIR/artifacts"
start_api release-lifecycle-n
printf '%s\n' 'release_n_started=true' > "$EVIDENCE_DIR/result.txt"
SEED="$(curl -fsS -X POST -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:58080/api/creator/workspaces/${WORKSPACE_ID}/demo-short/runs")"
PROJECT_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["seeded_project_id"])' <<<"$SEED")"
PREVIEW="$(curl -fsS -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:58080/api/creator/projects/${PROJECT_ID}/timeline/preview")"
MEDIA_URL="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["segments"][0]["media_url"])' <<<"$PREVIEW")"

docker rm -f "$API" >/dev/null
docker run --rm --network "$NETWORK" -e DATABASE_URL="$DATABASE_URL" release-lifecycle-n-plus-one alembic upgrade head
CURRENT="$(docker run --rm --network "$NETWORK" -e DATABASE_URL="$DATABASE_URL" release-lifecycle-n \
  python -c 'import os; from alembic.runtime.migration import MigrationContext; from sqlalchemy import create_engine; engine = create_engine(os.environ["DATABASE_URL"]); connection = engine.connect(); print(" ".join(MigrationContext.configure(connection).get_current_heads())); connection.close(); engine.dispose()')"
test "$CURRENT" = "$LIFECYCLE_N_PLUS_ONE_HEAD"
python3 scripts/release_manifest.py verify-db-compatible \
  <(printf '{"release_tag":"2026.10.17.1","source_sha":"%040d","images":{"api":{"repository":"ghcr.io/review/api","digest":"sha256:%064d"},"worker":{"repository":"ghcr.io/review/worker","digest":"sha256:%064d"},"studio":{"repository":"ghcr.io/review/studio-web","digest":"sha256:%064d"}},"db_revision":"%s","schema_contract":"1","queue_contract":"1"}\n' 0 0 0 0 "$LIFECYCLE_N_HEAD") \
  "$CURRENT" "$LIFECYCLE_N_HEAD,$LIFECYCLE_N_PLUS_ONE_HEAD"
start_api release-lifecycle-n-plus-one
curl -fsS -H "X-API-Key: $API_KEY" "http://127.0.0.1:58080/api/creator/projects/${PROJECT_ID}" >/dev/null
curl -fsS -H "X-API-Key: $API_KEY" "http://127.0.0.1:58080${MEDIA_URL}" -o /dev/null
printf '%s\n' 'release_n_plus_one_project_media=true' >> "$EVIDENCE_DIR/result.txt"

start_api release-lifecycle-n
curl -fsS -H "X-API-Key: $API_KEY" "http://127.0.0.1:58080/api/creator/projects/${PROJECT_ID}" >/dev/null
curl -fsS -H "X-API-Key: $API_KEY" "http://127.0.0.1:58080${MEDIA_URL}" -o /dev/null
printf '%s\n' 'release_n_rollback_project_media=true' >> "$EVIDENCE_DIR/result.txt"

printf 'Release lifecycle acceptance passed: N seed, N+1 upgrade, N app rollback preserved project and media.\n'
