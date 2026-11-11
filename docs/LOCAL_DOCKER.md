# Local Docker Compose Setup

This guide explains how to run AI Video Studio on a local server using Docker Compose.

## Prerequisites

- **Docker Engine >= 28.0.0** on the machine that publishes the ports. Check the
  *server* version, not the CLI: `docker version --format '{{.Server.Version}}'`.
  Docker documents that in older releases hosts on the same L2 segment (for example,
  behind the same switch) can reach ports published to localhost, so a `127.0.0.1`
  binding is **not private** there. See
  [moby/moby#45610](https://github.com/moby/moby/issues/45610) and Docker's
  [port-publishing notes](https://docs.docker.com/engine/network/port-publishing/#publishing-ports).
  Do not use an older Engine for this workflow.
- **Docker Compose >= 2.24.4** (`docker compose version`) for the overlay's
  `ports: !override`. The Engine and the Compose plugin are versioned independently:
  a recent Compose does not make an old Engine safe.
- Python 3 on the host: the Make targets validate the Studio bind before invoking Docker.

## 1. Create environment file

```bash
cp .env.local.example .env
```

Edit `.env` and change `POSTGRES_PASSWORD` and `DATABASE_URL` credentials.

## 2. Start services

```bash
make docker-local-up
```

This starts:

- PostgreSQL
- Redis
- API
- Worker
- Studio Web

## 3. Run database migrations

```bash
make docker-local-migrate
```

## 4. Create API key

```bash
make docker-local-bootstrap
```

Copy the generated API key into `.env`:

```
API_KEY=<generated-key>
```

Then restart the services:

```bash
make docker-local-up
```

## 5. Open Studio

From the local server:

```
http://localhost:5174
```

The supported Make workflow is **private by default**: Studio publishes exactly
one binding at `127.0.0.1:5174`; API, PostgreSQL and Redis inherit the base
loopback bindings on ports 8000, 5432 and 6379. Other LAN devices must not be able
to reach any of these ports in this mode, provided the Engine meets the
[prerequisite](#prerequisites).

### Explicit trusted-LAN opt-in

Requires the Compose and Python versions under [Prerequisites](#prerequisites).
The overlay uses [`ports: !override`](https://docs.docker.com/reference/compose-file/merge/)
to replace, rather than append to, the base Studio binding. Older Compose must
be upgraded; do not remove the tag to work around an error.

After reviewing the trust and firewall limits below, replace the example with
the server's actual LAN address:

```bash
STUDIO_WEB_BIND=192.168.1.10 make docker-local-up
```

Use the same prefix with `make docker-local-build` when rebuilding. Both targets
validate the bind **before Docker is invoked**. Supply a literal IPv4/IPv6
address, not a hostname, empty value, port mapping or bracketed address.
For example, `STUDIO_WEB_BIND=fd00::10 make docker-local-up` formats IPv6 safely.
Wildcards additionally require explicit confirmation:

```bash
STUDIO_WEB_BIND=0.0.0.0 STUDIO_WEB_LAN_OK=1 make docker-local-up
```

`::` has the same confirmation requirement. Prefer a specific interface address;
wildcards may expose every interface (including IPv6/dual-stack paths). These
settings must be shell environment variables or Make command-line assignments,
not `.env` entries: the helper deliberately does not load `.env` and exports the
validated result to Compose. Direct Compose invocations bypass this validation
and are not the supported opt-in workflow; they interpolate `.env` themselves, so
delete any `STUDIO_WEB_BIND=0.0.0.0` line that an older `.env.local.example` copied
into your `.env`. To return to private mode:

```bash
STUDIO_WEB_BIND=127.0.0.1 make docker-local-up
```

Only after opt-in, from an allowed trusted client:

```
http://<server-ip>:5174
```

## 6. Check service status

```bash
make docker-local-status
```

## 7. View logs

```bash
make docker-local-logs
```

## 8. Stop services

```bash
make docker-local-down
```

## Network limits and verification

**Trusted single-user access only.** Studio nginx injects a shared server-side
API key: every browser reaching the proxy acts as that key's user. This is not
per-browser authentication. Public, untrusted or multi-user Studio exposure is
**not approved**, even behind a tunnel or reverse proxy. See the
[security trust model](SECURITY.md#personal-api-keys-and-attribution),
[#575](https://github.com/yeongseon/short-form-studio/issues/575) and
[#894](https://github.com/yeongseon/short-form-studio/issues/894).
API/backend ports must stay loopback-only; CORS does not provide network
isolation or authorize clients.

Before opting in, an operator must configure a host-firewall allowlist for only
the intended trusted client CIDR, keep backend ports blocked, and exclude all
other clients. Example for **one** trusted client (`192.168.1.20/32`), assuming
an already reviewed default-deny policy and no broader allow rule:

```bash
sudo ufw allow from 192.168.1.20/32 to 192.168.1.10 port 5174 proto tcp
```

This example was **not applied**. Review existing rules and Docker's packet
forwarding behavior first: Docker-published ports can bypass ordinary ufw rules.
An ufw allow entry alone is not proof of isolation; use an operator-reviewed
Docker-aware firewall policy (including IPv6) and test from both allowed and
denied clients. Do not opt in if that isolation cannot be verified.

Prefer keeping the server loopback-only and using an SSH tunnel from your own
trusted machine:

```bash
ssh -N -L 127.0.0.1:5174:127.0.0.1:5174 user@server
```

Then open `http://127.0.0.1:5174` on the client. A private VPN restricted to the
same trusted operator is another option; it does not add application identity
or approve public/multi-user access.

Inspect the merged declaration (read-only):

```bash
docker compose -f docker-compose.yml -f docker-compose.local-server.yml config --format json
```

Pass the same `STUDIO_WEB_BIND`/`STUDIO_WEB_PORT` used at startup when inspecting
an opt-in declaration (bracket IPv6 in this direct config command). Expect one
Studio mapping, and only loopback backend mappings. **Config inspection alone
is not a network isolation test.** After an authorized operator starts services,
inspect real listeners on the host:

```bash
ss -ltnp | grep -E ':(5174|8000|5432|6379)\b'
```

Adjust the Studio port if customized. Docker may implement publishing through
NAT rather than a visible userspace listener, so also test from a second machine:
in private mode, `curl --connect-timeout 3 http://<server-ip>:5174/` must fail;
in opted-in mode it must work only from the allowed client. Use
`nc -vz -w 3 <server-ip> 8000` (also 5432 and 6379) to confirm backends remain
unreachable. Test an unapproved client and IPv6 paths too. Repeat after clean
starts and mode changes, ensuring no stale/wildcard Studio binding survives.
Existing authorization checks must still reject missing/invalid credentials
with 401 and foreign media with anti-enumeration 404 through the API; the shared
Studio proxy itself supplies credentials and is not an anonymous-access barrier.
These live socket, repeated-start and second-machine checks were **not performed**
by this change and remain manual acceptance requirements.

## Recommended Models for Local Mode

The default model configuration is optimized for local/free execution:

| Category | Default Model | Why |
|----------|--------------|-----|
| TTS | `edge-tts` | No GPU, no API key, free, Korean voices included |
| LLM | `qwen3-4b` (local) or Groq (cloud) | Local Ollama or free Groq tier |
| STT | `whisper-small` (local) or `groq-whisper-large-v3-turbo` | Local or Groq |
| Image | `placeholder` or `hf-flux-schnell` | Placeholder for testing, HF FLUX for real images |

To override defaults, set these in `.env`:

```env
TTS_DEFAULT_MODEL=edge-tts
SCRIPT_DEFAULT_MODEL=meta-llama/llama-4-scout-17b-16e-instruct
SUBTITLE_DEFAULT_MODEL=groq-whisper-large-v3-turbo
```

### Why edge-tts?

Unlike `qwen3-tts` (which requires a GPU-backed TTS container), `edge-tts` runs
without any local GPU or external API key. It provides high-quality Korean voices
suitable for 썰쇼츠 and demo purposes.

Available Korean voices:

| Voice key | Edge TTS voice |
|-----------|---------------|
| `default` | ko-KR-SunHiNeural |
| `female` | ko-KR-SunHiNeural |
| `male` | ko-KR-InJoonNeural |
