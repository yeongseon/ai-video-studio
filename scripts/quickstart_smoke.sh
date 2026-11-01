#!/usr/bin/env bash
# SF-71: static clean-environment smoke check for the Docker Quick Start.
#
# Verifies the reproducible CPU/remote-provider path documented in
# docs/QUICKSTART.md WITHOUT bringing the stack up (no docker-in-docker required
# beyond `docker compose config`): the compose config is valid, the CPU core is
# present and not GPU-gated, the GPU services are behind the `gpu` profile, every
# published port is bound to 127.0.0.1, the required env vars exist in
# .env.example, and the referenced bootstrap/migration entrypoints exist.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
pass() { echo "ok: $*"; }

# --- referenced entrypoints exist ---
[ -f .env.example ] || fail ".env.example missing"
[ -f docker-compose.yml ] || fail "docker-compose.yml missing"
[ -f scripts/create_api_key.py ] || fail "scripts/create_api_key.py missing"
[ -f apps/api/alembic.ini ] || fail "apps/api/alembic.ini missing"
[ -f docs/QUICKSTART.md ] || fail "docs/QUICKSTART.md missing"
pass "referenced files present"

# --- required env vars are documented in .env.example ---
for var in POSTGRES_PASSWORD DATABASE_URL REDIS_URL CORS_ORIGINS ADMIN_API_KEY \
           API_KEY OPENAI_API_KEY GROQ_API_KEY; do
  grep -qE "^${var}=" .env.example || fail "${var} not declared in .env.example"
done
pass "required env vars declared"

# --- studio-web receives API_KEY so its proxy can authenticate the browser ---
grep -qE 'API_KEY:\s*\$\{API_KEY' docker-compose.yml \
  || fail "studio-web must receive API_KEY from env (browser auth wiring)"
grep -qF 'proxy_set_header X-API-Key "${API_KEY}"' apps/studio-web/nginx.conf.template \
  || fail "nginx must inject X-API-Key from API_KEY"
pass "studio-web browser-auth wiring present"

# Service env_file paths are resolved independently of --env-file interpolation.
# Validate an isolated copy so neither a missing nor a private .env is consulted.
SMOKE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/quickstart-smoke.XXXXXX")"
trap 'rm -rf "$SMOKE_DIR"' EXIT
cp docker-compose.yml "$SMOKE_DIR/compose.yml"
cp .env.example "$SMOKE_DIR/.env"
compose() {
  env -i PATH="$PATH" HOME="${HOME:-/tmp}" docker compose \
    --project-name quickstart-smoke --project-directory "$SMOKE_DIR" \
    --env-file "$SMOKE_DIR/.env" -f "$SMOKE_DIR/compose.yml" "$@"
}
compose config --quiet || fail "docker compose config invalid"
pass "docker compose config valid"

# --- CPU core services are defined and NOT gpu-profile-gated ---
CORE_SERVICES="$(compose config --services)" || fail "docker compose core service listing failed"
# Here-strings avoid a producer receiving SIGPIPE when grep exits after a match.
for svc in postgres redis api worker studio-web; do
  grep -qx "$svc" <<< "$CORE_SERVICES" || fail "CPU core service '${svc}' not defined"
done
pass "CPU core services defined"

# ollama (local LLM) must be gpu-gated so the default `up` is CPU-only.
GPU_SERVICES="$(compose --profile gpu config --services)" || fail "docker compose gpu service listing failed"
grep -qx "ollama" <<< "$GPU_SERVICES" || fail "ollama should exist under the gpu profile"
grep -qx "ollama" <<< "$CORE_SERVICES" && fail "ollama must NOT start without --profile gpu"
pass "gpu services gated behind the gpu profile"

# --- every published port binds to 127.0.0.1 only (loopback) ---
compose config --format json > "$SMOKE_DIR/compose.json"
python3 - "$SMOKE_DIR/compose.json" <<'PY'
import json
import sys

with open(sys.argv[1]) as handle:
    compose = json.load(handle)

bad = []
for name, service in (compose.get("services") or {}).items():
    for port in service.get("ports") or []:
        host_ip = port.get("host_ip") if isinstance(port, dict) else None
        published = port.get("published") if isinstance(port, dict) else None
        if published is not None and host_ip != "127.0.0.1":
            bad.append(f"{name}: published {published} host_ip={host_ip!r}")

if bad:
    print("SMOKE FAIL: non-loopback published ports:\n  " + "\n  ".join(bad), file=sys.stderr)
    sys.exit(1)
print("ok: all published ports bound to 127.0.0.1")
PY

echo "Quick Start smoke check passed."
