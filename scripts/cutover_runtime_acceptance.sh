#!/usr/bin/env bash
# allow: SIZE_OK - One ordered runtime acceptance scenario; keep its assertions together.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if { [ -e .env ] || [ -L .env ]; } && [ "${CUTOVER_ACCEPTANCE_DISPOSABLE:-}" != 1 ]; then
  printf '%s\n' 'Refusing to replace existing .env. Run from a fresh clone or git worktree, or move/rename the real .env first. CUTOVER_ACCEPTANCE_DISPOSABLE=1 means this checkout is disposable: its .env will be replaced.' >&2
  exit 1
fi
export COMPOSE_PROJECT_NAME="cutover-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"
STATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cutover-acceptance.XXXXXX")"
ENV_TEMP=""
ENV_CREATED=0
ENV_PUBLISHING=0
ENV_INTERRUPTED=0

fail() { printf 'ACCEPTANCE FAIL: %s\n' "$*" >&2; exit 1; }
code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
psql_scalar() {
  docker compose exec -T postgres sh -c \
    'psql -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -Atc "$1"' _ "$1"
}
beat_is_healthy() {
  local containers
  containers="$(docker compose ps -a -q beat)" || return 1
  [ -n "$containers" ] && [ "$(printf '%s\n' "$containers" | wc -l)" = 1 ] || return 1
  docker inspect --format '{{.State.Running}}:{{.State.Health.Status}}' "$containers" \
    | grep -Fx 'true:healthy'
}
assert_dlq_write() {
  local service="$1"
  shift
  docker compose "$@" run --rm --no-deps "$service" python -c \
    'from pathlib import Path; from creator_service.celery_dlq_fallback import append_fallback_entry; append_fallback_entry(Path("/var/lib/short-form-studio/dlq/failures.jsonl"), {"source": "container-acceptance"}, max_bytes=4096)'
}
assert_dlq_fallback_recovery() {
  docker compose run --rm --no-deps worker python -c \
    'import celery_app; celery_app.redis.Redis.from_url = lambda _url: (_ for _ in ()).throw(ConnectionError("forced Redis failure")); celery_app._record_failed_task_to_dlq("fallback-acceptance", "acceptance.marker", (), {"password": "secret-value"}, RuntimeError("marker failure"))'
  docker compose up -d --force-recreate --no-deps worker
  timeout 60 bash -c 'until docker compose exec -T worker celery -A celery_app.celery_app inspect ping 2>/dev/null | grep -q pong; do sleep 2; done'
  docker compose exec -T worker python -c \
    'from celery_app import read_dlq_fallback; entries = read_dlq_fallback(); entry = next(item for item in entries if item.get("task_id") == "fallback-acceptance"); assert entry["kwargs"] == {"password": "<redacted>"}'
}
enqueue_lifecycle_marker() {
  local scenario="$1"
  docker compose exec -T beat python -c \
    "from celery_app import celery_app; celery_app.send_task('acceptance.marker', args=['${scenario}-durability-marker'], queue='durability-marker')"
}
assert_lifecycle_marker() {
  local scenario="$1"
  [ "$(docker compose exec -T redis redis-cli LLEN durability-marker | tr -d '\r')" = 1 ] \
    || fail "${scenario} did not preserve routed durability marker"
}
wait_for_redis() {
  timeout 60 bash -c 'until docker compose exec -T redis redis-cli ping 2>/dev/null | grep -q PONG; do sleep 1; done'
}
run_recovery_now() {
  docker compose exec -T beat python -c \
    'from tasks.reconcile_stale_dispatches import reconcile_stale_dispatches; print(reconcile_stale_dispatches())'
}
seed_lifecycle_rows() {
  local scenario="$1"
  local run_id
  local broker_generation
  broker_generation="$(docker compose exec -T redis redis-cli GET queue:recovery_epoch | tr -d '\r' | cut -d'|' -f1)"
  [ -n "$broker_generation" ] || fail "broker generation unavailable before ${scenario} seed"
  run_id="$(psql_scalar "INSERT INTO creator_runs (project_id,workspace_id,current_stage,status,style_preset) SELECT id,${WORKSPACE_ID},'AUDIO_GENERATING','pending','default' FROM creator_projects WHERE workspace_id=${WORKSPACE_ID} ORDER BY id LIMIT 1 RETURNING id")"
  for status in pending queued running success; do
    psql_scalar "INSERT INTO creator_run_tasks (run_id,task_type,celery_task_id,status,attempt,created_at,started_at,finished_at,claim_token,broker_generation) VALUES (${run_id},CASE '${status}' WHEN 'pending' THEN 'generate_script' WHEN 'queued' THEN 'generate_visual_plan' WHEN 'running' THEN 'generate_audio' ELSE 'render_video' END,'${scenario}-lifecycle-${status}','${status}',1,NOW(),CASE WHEN '${status}'='running' THEN NOW() END,CASE WHEN '${status}'='success' THEN NOW() END,CASE WHEN '${status}'='running' THEN 'late-worker' END,'${broker_generation}');"
  done
  for status in pending queued running; do
    psql_scalar "INSERT INTO workspace_quota_reservation_owners (owner_id,workspace_id,period_start,operation_type,status) VALUES ('${scenario}-lifecycle-${status}',${WORKSPACE_ID},date_trunc('month',NOW()),'tts','reserved');"
  done
  printf '%s' "$run_id"
}
assert_late_worker_fenced() {
  local task_id="$1"
  local claimed
  claimed="$(docker compose exec -T worker python -c "from worker_loop import run_in_worker_loop; from creator_service.task_tracking_service import task_tracking_service; print(run_in_worker_loop(task_tracking_service.record_task_start(1, 'generate_audio', '${task_id}', claim_token='late-worker')))" )"
  [ "$claimed" = "None" ] || fail "late worker reclaimed ${task_id}: ${claimed}"
}
run_redis_lifecycle() {
  local scenario="$1"
  local run_id
  run_id="$(seed_lifecycle_rows "$scenario")"
  if [ "$scenario" != redis_fresh_volume_replacement ]; then
    enqueue_lifecycle_marker "$scenario"
  fi
  case "$scenario" in
    redis_process_restart)
      docker compose restart redis
      ;;
    redis_force_recreate_retained_volume)
      docker compose up -d --force-recreate redis
      ;;
    redis_fresh_volume_replacement)
      docker compose stop redis
      docker compose rm -f redis
      docker volume rm "${COMPOSE_PROJECT_NAME}_redis_data"
      docker compose up -d redis
      ;;
    *) fail "unknown Redis lifecycle scenario: ${scenario}" ;;
  esac
  wait_for_redis
  if [ "$scenario" != redis_fresh_volume_replacement ]; then
    assert_lifecycle_marker "$scenario"
    docker compose exec -T redis redis-cli DEL durability-marker >/dev/null
  fi
  timeout 60 bash -c 'until docker compose exec -T worker celery -A celery_app.celery_app inspect ping 2>/dev/null | grep -q pong; do sleep 2; done'
  run_recovery_now
  if [ "$scenario" = redis_fresh_volume_replacement ]; then
    [ "$(psql_scalar "SELECT string_agg(status,',' ORDER BY celery_task_id) FROM creator_run_tasks WHERE celery_task_id LIKE '${scenario}-lifecycle-%'")" = "failed,failed,failed,success" ] || fail "fresh broker did not fail only active lifecycle rows"
    [ "$(psql_scalar "SELECT count(*) FROM workspace_quota_reservation_owners WHERE owner_id LIKE '${scenario}-lifecycle-%' AND status='cancelled'")" = 3 ] || fail "fresh broker did not release active attempt quota"
    [ "$(psql_scalar "SELECT status FROM creator_runs WHERE id=${run_id}")" = failed ] || fail "fresh broker did not fail the run"
    assert_late_worker_fenced "${scenario}-lifecycle-running"
  else
    [ "$(psql_scalar "SELECT string_agg(status,',' ORDER BY celery_task_id) FROM creator_run_tasks WHERE celery_task_id LIKE '${scenario}-lifecycle-%'")" = "pending,queued,running,success" ] || fail "retained Redis lifecycle changed task outcomes"
    [ "$(psql_scalar "SELECT count(*) FROM workspace_quota_reservation_owners WHERE owner_id LIKE '${scenario}-lifecycle-%' AND status='reserved'")" = 3 ] || fail "retained Redis lifecycle changed quota outcomes"
    [ "$(psql_scalar "SELECT status FROM creator_runs WHERE id=${run_id}")" = pending ] || fail "retained Redis lifecycle changed run outcome"
  fi
}
cleanup() {
  if [ "$ENV_CREATED" = 1 ]; then
    docker compose down --volumes --remove-orphans || true
    rm -f .env
  fi
  if [ -n "$ENV_TEMP" ]; then rm -f "$ENV_TEMP"; fi
  rm -rf "$STATE_DIR"
}
trap cleanup EXIT
trap 'if [ "$ENV_PUBLISHING" = 1 ]; then ENV_INTERRUPTED=1; else exit 130; fi' INT
trap 'if [ "$ENV_PUBLISHING" = 1 ]; then ENV_INTERRUPTED=1; else exit 143; fi' TERM

ENV_TEMP="$(mktemp ./.env-cutover-XXXXXX)"
cat .env.example > "$ENV_TEMP"
printf '\nARTIFACT_ROOT=/tmp/cutover-artifacts\n' >> "$ENV_TEMP"
ENV_PUBLISHING=1
if [ "${CUTOVER_ACCEPTANCE_DISPOSABLE:-}" = 1 ]; then
  if [ -e .env ] || [ -L .env ]; then
    printf 'WARNING: CUTOVER_ACCEPTANCE_DISPOSABLE=1: replacing existing .env in checkout %s\n' "$ROOT" >&2
  fi
  if mv -T "$ENV_TEMP" .env; then ENV_CREATED=1; else exit 1; fi
else
  if ln -T -- "$ENV_TEMP" .env; then
    ENV_CREATED=1
  elif [ -e .env ] || [ -L .env ]; then
    fail 'Refusing: .env appeared during publication. Run from a fresh clone or git worktree, or move/rename the real .env first. CUTOVER_ACCEPTANCE_DISPOSABLE=1 means this checkout is disposable: its .env will be replaced.'
  else
    fail 'Refusing: could not publish .env with a hard link (the checkout filesystem may not support hard links). Use a checkout on a local filesystem, or set CUTOVER_ACCEPTANCE_DISPOSABLE=1 only when this checkout is disposable: its .env will be replaced.'
  fi
fi
ENV_PUBLISHING=0
[ "$ENV_INTERRUPTED" = 0 ] || exit 1
rm -f "$ENV_TEMP"
docker compose up -d postgres redis
docker compose run --rm api alembic upgrade 037
LEGACY_USER_ID="$(psql_scalar "INSERT INTO users (email,auth_provider,auth_subject) VALUES ('legacy-upgrade@example.com','api_key','legacy-upgrade') RETURNING id")"
LEGACY_WORKSPACE_ID="$(psql_scalar "INSERT INTO workspaces (name,slug,owner_id) VALUES ('Legacy upgrade','legacy-upgrade-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}',${LEGACY_USER_ID}) RETURNING id")"
LEGACY_PROJECT_ID="$(psql_scalar "INSERT INTO creator_projects (workspace_id,title) VALUES (${LEGACY_WORKSPACE_ID},'Legacy upgrade') RETURNING id")"
LEGACY_RUN_ID="$(psql_scalar "INSERT INTO creator_runs (project_id,workspace_id,current_stage,status,style_preset) VALUES (${LEGACY_PROJECT_ID},${LEGACY_WORKSPACE_ID},'AUDIO_GENERATING','pending','default') RETURNING id")"
for status in pending queued running success; do
  psql_scalar "INSERT INTO creator_run_tasks (run_id,task_type,celery_task_id,status,attempt,started_at,finished_at) VALUES (${LEGACY_RUN_ID},'generate_audio','legacy-upgrade-${status}','${status}',1,CASE WHEN '${status}'='running' THEN NOW() END,CASE WHEN '${status}'='success' THEN NOW() END);"
done
docker compose run --rm api alembic upgrade head
docker compose run --rm --no-deps beat sh -c \
  'printf %s beat > /var/lib/short-form-studio/dlq/observed-broker-epoch'
docker compose run --rm --no-deps beat sh -c \
  'test "$(cat /var/lib/short-form-studio/dlq/observed-broker-epoch)" = beat'
assert_dlq_write "worker"
assert_dlq_write "worker-script" -f docker-compose.yml -f docker-compose.scaled-workers.yml
assert_dlq_write "worker-image" -f docker-compose.yml -f docker-compose.scaled-workers.yml
assert_dlq_write "worker-audio" -f docker-compose.yml -f docker-compose.scaled-workers.yml
assert_dlq_write "worker-render" -f docker-compose.yml -f docker-compose.scaled-workers.yml
assert_dlq_fallback_recovery

docker compose run --rm api python scripts/create_api_key.py \
  --email cutover@example.com --workspace cutover --name cutover-ci --json > "$STATE_DIR/cutover-key.json"
API_KEY="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["api_key"])' < "$STATE_DIR/cutover-key.json")"
WORKSPACE_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["workspace_id"])' < "$STATE_DIR/cutover-key.json")"
[ -n "$API_KEY" ] || fail "creator key bootstrap returned no key"
export API_KEY

docker compose run --rm api python scripts/create_api_key.py \
  --email foreign@example.com --workspace foreign --name foreign-ci --json > "$STATE_DIR/foreign-key.json"
FOREIGN_WORKSPACE_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["workspace_id"])' < "$STATE_DIR/foreign-key.json")"
[ -n "$FOREIGN_WORKSPACE_ID" ] || fail "foreign workspace bootstrap returned no id"

python3 - "$API_KEY" <<'PY'
from pathlib import Path
import os
import sys
from tempfile import NamedTemporaryFile

path = Path(".env")
lines = path.read_text(encoding="utf-8").splitlines()
with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".env-cutover-", delete=False) as handle:
    temporary = Path(handle.name)
    try:
        handle.write("\n".join(f"API_KEY={sys.argv[1]}" if line.startswith("API_KEY=") else line for line in lines) + "\n")
        handle.flush()
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
PY

docker compose up -d api worker beat studio-web
timeout 90 bash -c 'until curl -sf http://127.0.0.1:8000/healthz >/dev/null; do sleep 2; done'
timeout 90 bash -c 'until curl -sf http://127.0.0.1:5174/ >/dev/null; do sleep 2; done'
DEADLINE=$((SECONDS + 150))
until beat_is_healthy; do
  if [ "$SECONDS" -ge "$DEADLINE" ]; then
    docker compose logs beat
    fail "expected exactly one running healthy Beat within 150 seconds"
  fi
  sleep 3
done
for attempt in 1 2; do
  make docker-local-up
  beat_is_healthy || fail "local up ${attempt} did not preserve one healthy Beat"
done

docker compose stop redis
docker compose rm -f redis
docker volume rm "${COMPOSE_PROJECT_NAME}_redis_data"
docker compose up -d redis
wait_for_redis
timeout 60 bash -c 'until docker compose exec -T worker celery -A celery_app.celery_app inspect ping 2>/dev/null | grep -q pong; do sleep 2; done'
docker compose run --rm --no-deps beat sh -c \
  'printf %s fallback-replacement > /var/lib/short-form-studio/dlq/observed-broker-epoch'
run_recovery_now
[ "$(psql_scalar "SELECT string_agg(status,',' ORDER BY celery_task_id) FROM creator_run_tasks WHERE celery_task_id LIKE 'legacy-upgrade-%'")" = "failed,failed,failed,success" ] \
  || fail "legacy active rows did not converge after broker replacement"

[ "$(code http://127.0.0.1:8000/api/creator/users/me)" = 401 ] \
  || fail "creator endpoint did not return 401 without a key"
[ "$(code -H "X-API-Key: ${API_KEY}" http://127.0.0.1:8000/api/creator/users/me)" = 200 ] \
  || fail "creator endpoint did not return 200 with the exported key"

for task in reconcile_stale_dispatches retry_failed_artifact_deletions sweep_expired_artifacts beat_heartbeat; do
  timeout 60 bash -c "until docker compose exec -T worker celery -A celery_app.celery_app inspect registered 2>/dev/null | grep -q '$task'; do sleep 2; done"
  printf 'registered: %s\n' "$task"
done

PROJECT="$(curl -fsS -H "X-API-Key: ${API_KEY}" -H 'Content-Type: application/json' \
  -d '{"title":"Beat acceptance","source_type":"idea","idea_brief":"disposable"}' \
  http://127.0.0.1:8000/api/creator/projects)"
PROJECT_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"$PROJECT")"
RUN="$(curl -fsS -H "X-API-Key: ${API_KEY}" -H 'Content-Type: application/json' \
  -d '{"style_preset":"default"}' \
  "http://127.0.0.1:8000/api/creator/projects/${PROJECT_ID}/runs")"
RUN_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"$RUN")"
run_recovery_now
run_redis_lifecycle "redis_process_restart"
run_redis_lifecycle "redis_force_recreate_retained_volume"
run_redis_lifecycle "redis_fresh_volume_replacement"

psql_scalar "INSERT INTO creator_run_tasks (run_id,task_type,celery_task_id,status,attempt,created_at) VALUES (${RUN_ID},'generate_script','cutover-stale-${RUN_ID}','pending',1,NOW()-INTERVAL '5 minutes');"

DEADLINE=$((SECONDS + 150))
while [ "$(psql_scalar "SELECT status FROM creator_run_tasks WHERE celery_task_id='cutover-stale-${RUN_ID}'")" != failed ] && [ "$SECONDS" -lt "$DEADLINE" ]; do
  sleep 3
done
if [ "$SECONDS" -ge "$DEADLINE" ]; then
  docker compose logs beat worker
  fail "Beat did not reconcile the stale task within 150 seconds"
fi
RUN_STATE="$(psql_scalar "SELECT status||':'||current_stage FROM creator_runs WHERE id=${RUN_ID}")"
[ "$RUN_STATE" = "failed:FAILED" ] || fail "Beat housekeeping produced unexpected run state: $RUN_STATE"
docker compose logs beat | grep -F 'Scheduler: Sending due task reconcile-stale-dispatches (reconcile_stale_dispatches)' \
  || fail "Beat log has no reconcile-stale-dispatches dispatch evidence"
beat_is_healthy || fail "Beat lost singleton health during reconciliation"
docker compose stop -t 10 beat
if beat_is_healthy; then
  fail "stopped Beat silently passed the singleton health acceptance"
fi
printf 'Beat missing-scheduler signal verified after stop.\n'

ORIGIN='Origin: http://localhost:5174'
MUTATION_CODE="$(code -H "$ORIGIN" -H 'Content-Type: application/json' \
  -d '{"title":"nginx mutation","source_type":"idea","idea_brief":"same origin"}' \
  http://localhost:5174/api/creator/projects)"
[ "$MUTATION_CODE" = 201 ] || fail "same-origin nginx mutation returned $MUTATION_CODE"

FOREIGN_CODE="$(code "http://localhost:5174/api/creator/workspaces/${FOREIGN_WORKSPACE_ID}/assets")"
[ "$FOREIGN_CODE" = 404 ] || fail "foreign workspace returned $FOREIGN_CODE instead of 404"

PNG="$STATE_DIR/upload.png"
printf '%s' 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=' | base64 -d > "$PNG"
UPLOAD_CODE="$(code -H "$ORIGIN" -H 'Accept: application/json' \
  -F "file=@${PNG};type=image/png" \
  "http://localhost:5174/api/creator/workspaces/${WORKSPACE_ID}/assets")"
if [ "$UPLOAD_CODE" != 201 ]; then
  docker compose logs api
  fail "same-origin nginx upload returned $UPLOAD_CODE"
fi

printf 'Cutover runtime acceptance passed: auth 401/200, workspace 404, mutation/upload, Beat DB transition.\n'
