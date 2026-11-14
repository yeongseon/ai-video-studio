# Observability Guide

This document covers the OpenTelemetry integration and structured logging
setup for AI Video Studio.

---

## Overview

The system supports optional OpenTelemetry (OTEL) instrumentation for:
- **Distributed tracing** — follow a request across API → worker → provider
- **Metrics** — task duration histograms, error counters
- **Structured logging** — JSON logs with trace context correlation

OTEL is disabled by default and degrades gracefully if the collector is
unreachable.

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_ENABLED` | `false` | Enable OpenTelemetry instrumentation |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | OTLP gRPC collector endpoint |
| `OTEL_SERVICE_NAME` | `short-form-studio` | Service name in traces |
| `OTEL_ENVIRONMENT` | `development` | Environment tag |

### Enabling in Docker Compose

```yaml
# Add to .env
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317

# Add collector service to docker-compose.yml (or use separate override)
services:
  otel-collector:
    image: otel/opentelemetry-collector-contrib:latest
    ports:
      - "127.0.0.1:4317:4317"   # gRPC
      - "127.0.0.1:4318:4318"   # HTTP
    volumes:
      - ./otel-config.yaml:/etc/otelcol-contrib/config.yaml
```

---

## Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────────┐
│  API Server  │────▶│    Worker    │────▶│   AI Provider    │
│  (FastAPI)   │     │  (Celery)   │     │ (Ollama/OpenAI)  │
└──────┬───────┘     └──────┬───────┘     └──────────────────┘
       │                     │
       │  OTLP gRPC          │  OTLP gRPC
       ▼                     ▼
┌──────────────────────────────────────┐
│       OpenTelemetry Collector        │
└──────────────┬───────────────────────┘
               │
       ┌───────┴───────┐
       ▼               ▼
┌────────────┐  ┌────────────┐
│   Jaeger   │  │ Prometheus │
│  (Traces)  │  │ (Metrics)  │
└────────────┘  └────────────┘
```

---

## Trace Propagation

### API → Worker

When the API dispatches a Celery task, trace context is propagated via
task headers. The worker's `trace_task` decorator extracts this context and runs
the task inside a `celery.task.<task_name>` span, which continues the trace.

```python
# In shorts_api/task_dispatch_adapter.py — the current context becomes the Celery task headers
from creator_service.telemetry import get_trace_headers

headers = get_trace_headers()
task.apply_async(args=args, kwargs=kwargs, headers=headers)
```

### Spans

Two places create spans today: the API request middleware (one span per request,
from the tracer named `shorts_api.middleware.telemetry`) and `trace_task` (one
`celery.task.<task_name>` span per task delivery). Provider calls do not create
spans of their own. To add a span, take a named tracer:

```python
from creator_service.telemetry import get_tracer

tracer = get_tracer(__name__)
with tracer.start_as_current_span("my.operation"):
    ...
```

---

## Structured Logging

Application stream handlers use JSON-formatted logging in every environment.
Trace correlation fields, when available, look like:

```json
{
  "timestamp": "2025-01-15T12:30:45.123Z",
  "level": "INFO",
  "logger": "shorts_api.routes.creator_runs_core",
  "message": "Run created",
  "trace_id": "0123456789abcdef0123456789abcdef",
  "span_id": "0123456789abcdef",
  "run_id": 42,
  "project_id": 7,
  "workspace_id": 1
}
```

### Log Configuration

Structured logging is configured in `creator_service/logging_config.py`:
- The API server process under default Uvicorn startup (import-string CLI form)
  uses the shared redacting JSON stream formatter, including chained exceptions.
  `uvicorn.access` retains the request path but omits the entire query string;
  `--no-access-log` remains respected.
- JSON logging is configured unconditionally at application import, in development
  as well as production.
- Log text preserves exact route shapes under `/api/creator` and `/api/admin`
  (route-shaped paths whose segments are filtered as described below), `/release/identity`,
  `/release/worker-identity`, and `/docs/oauth2-redirect`. Single-segment paths
  such as `/health`, `/healthz`, `/docs`, `/redoc`, and `/openapi.json` also remain
  readable. A route is independent only at a left boundary not preceded by a
  word character, `.`, `:`, `@`, or `-`: a relative file or authority prefix that
  touches the route (`uploads/api/admin/...`, `user:pass@host/api/creator/...`) is
  hidden together with it. Other separators in front of a route (`//`, `\`, `~`, `+`)
  are not part of that prefix, so text before them is not hidden by this rule.
  Within a route, preserve a segment only if it is `[0-9]{1,20}`, or
  `[a-z][a-z_-]{0,23}` without a `[0-9a-f]{12,}` run or a case-insensitive
  credential prefix anywhere (`eyJ`, `hf_`, `r8_`, `sk-`, `sk_`, `gsk`, `ghp`,
  `xoxb`, `AKIA`, `AIza`, `pplx-`); because a prefix is matched anywhere in a
  segment, an ordinary word that contains one (`task-list`, `disk_usage`) is
  redacted too, and a new route whose static segment is redacted must be renamed or
  reviewed into the exception set. The explicit frozen framework exception set
  is `{"openapi.json", "oauth2-redirect"}`. The first unsafe segment and its
  entire remaining tail, including a trailing slash, become one `<redacted>`;
  non-route-shaped multi-segment paths are hidden whole. This is a conservative
  heuristic, not a guarantee against arbitrary credentials: a short lowercase
  word or numeric value can still look like a safe route segment. Never put
  secrets in routes. The live-route drift guard checks static segments and
  parameters, accumulates nested mount prefixes, and refuses uninspectable
  file-serving mounts under preserved prefixes; it is a regression aid, not
  runtime authorization. The API access-log path argument visibly escapes
  U+0000–U+001F and U+007F as `\xNN`; U+0080–U+009F (including U+0085),
  U+2028/U+2029 and the bidirectional overrides and isolates U+202A–U+202E and
  U+2066–U+2069 as `\uNNNN`; and doubles a literal backslash so that it cannot pass
  for an escape. The decoded ASGI path is used because URL parsing can discard
  controls. Normal paths are unchanged; the stream formatter and Uvicorn query
  filter remain unchanged.
- The shared error-text redactor used by API error responses, DLQ values and JSON
  logs hides URLs, credential URIs, Bearer tokens, 20+ hexadecimal characters and
  provider-shaped tokens (`sk-`/`sk_`, `gsk-`/`gsk_`, `ghp-`/`ghp_`, `xoxb-`/`xoxb_`,
  `hf_`, `r8_`, `AKIA`, `AIza`, each with at least six body characters from ASCII
  letters, digits, `_` and `-`). Sensitive `name=value` and `name:value` assignments,
  including quoted JSON/dict keys, use the same key predicate as log extras; only
   their values are replaced with `<redacted>`. Unquoted values end at whitespace,
   comma, semicolon, ampersand, quotes or brackets (`[]{}()`); quoted values end at
   the first unescaped matching quote (or input end if unterminated). A backslash
   skips the following character without decoding it. Python string prefixes
   (`b`, `r`, `u`, case-insensitive, up to two characters) are accepted on quoted
   names and values; a value prefix is included in the redacted span. Unquoted
   values of names ending in `authorization` hide the rest of that line, including
   Basic, Token and AWS4 signature schemes. `token_count`, `key_id` and `max_tokens` remain readable;
  `password_policy` is sensitive under that predicate. Detection is heuristic,
   not a guarantee for arbitrary opaque credentials or tokens split across lines.
   Sensitive names in prose, SQL and traceback source lines also fail closed:
   `Invalid token: expired`, `claim_token = $3` and `token = settings.api_token`
   lose their apparent values. For non-secret data use non-sensitive field names
   such as `key_fingerprint` (admin attribution) or `object_name` (storage), and
   verify them with `is_sensitive_log_key`; do not weaken the predicate.
   Unquoted passphrases containing spaces (for example `password=correct horse
   battery staple`) hide only the first word. Nested container values such as
   `{"password": ["a","b"]}` are not parsed by the free-text scanner. Never
   log these secrets; prefer credential-labelled structured fields, whose entire
   values are hidden by the JSON formatter. Ordinary quoted delimiters are
   preserved; prefixed values and any contiguous unquoted suffix are hidden
   together to keep repeated redaction stable.
- Supervisor lines from `--reload` / `--workers` are not covered: those processes
  never import the application and may retain plain-text logging.
- Custom `--log-config` handlers are not preserved or covered. Passing an already
  imported app object to `uvicorn.run(app)` / `Config(app=obj)` reapplies Uvicorn's
  logging configuration after application setup and is outside this guarantee.
- With `OTEL_ENABLED=true`, the application-configured LoggerProvider redacts SDK
  records before either the Console or OTLP exporting processor receives them.
  This covers the formatted body (`msg`/`args`), derived exception message and
  complete chained stacktrace, cached exception text when exported as an attribute,
  and string extras (including credential-labelled keys). Nested log values share
  a bounded traversal budget; credential detection reuses `error_redaction.py`.
  The original Python LogRecord and JSON stream formatter are unchanged, regardless
  of root handler order (normally the stream handler precedes the OTel handler).
- The application-configured span exporter redacts all string attributes on
  `exception` events, including messages, chained stacktraces and string sequences,
  plus span status descriptions, for both Console and OTLP. This also covers worker
  `trace_task` spans. Redaction failures produce a fixed `<redacted>` placeholder
  rather than raw text and do not fail a logging call or request.
- This is not a general telemetry data-loss-prevention guarantee. Ordinary span
  attributes/names, resource and instrumentation metadata, links and non-exception
  events are outside this boundary; never put secrets there. Third-party
  instrumentation using these providers receives the same boundary, but separately
  installed providers/exporters/handlers or direct output are not intercepted.
  Metrics are unchanged and are not redacted. No new configuration is required.
- `TraceContextFilter` runs on the root handlers and adds the correlation fields
  only when telemetry is enabled and a valid span is active; it covers direct-root
  records and propagated application and Uvicorn records. Logging setup filters each
  installed handler; telemetry setup also filters existing root handlers and the SDK
  log handler, idempotently. A handler added to the root logger later by other code
  gets no filter of its own and sees the fields only when a filtered handler handled
  the same record first.
- JSON exempts only top-level `trace_id` and `span_id` from hex redaction when
  both match the filter's private provenance pair and are nonzero lowercase hex
  strings of exactly 32 and 16 characters, respectively. These SDK correlation
  identifiers enable trace joins; the private provenance is never emitted, in
  JSON or as an exported OTLP log attribute (the redacting log processor drops it).
  Active-span values override user-supplied correlation fields. Without valid
  provenance, user fields follow ordinary redaction; the provenance is an
  in-process convention that only code controlling record attributes can forge.
  The filter never raises into the logging call site. Messages, exception text,
  nested extras and every other field retain the existing redaction rules.

### Correlation Fields

| Field | Source | Description |
|-------|--------|-------------|
| `trace_id` | OTEL context | Distributed trace identifier |
| `span_id` | OTEL context | Current span identifier |
| `run_id` | Application | Pipeline run being processed |
| `workspace_id` | Auth context | Workspace of the requesting user |
| `task_name` | Celery | Worker task function name |

---

## Metrics

### Available Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `task.duration` | Histogram | Task execution time by type |
| `task.errors` | Counter | Task failures by type and error class |
| `provider.calls` | Counter | External provider API calls |
| `provider.latency` | Histogram | Provider response time |

### Prometheus Export

With the OTEL collector configured for Prometheus export:

```yaml
# otel-config.yaml (example)
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317

exporters:
  prometheus:
    endpoint: 0.0.0.0:8889

service:
  pipelines:
    metrics:
      receivers: [otlp]
      exporters: [prometheus]
    traces:
      receivers: [otlp]
      exporters: [jaeger]
```

---

## Graceful Degradation

The telemetry system is designed to never crash the application:

1. **OTEL_ENABLED=false** (default): No-op implementations used for tracer/meter.
   Zero overhead.
2. **Collector unreachable**: SDK buffers and drops silently. Logs a warning
   once, then suppresses further connection errors.
3. **Import failure**: If `opentelemetry` packages are missing, the system
   falls back to no-op stubs automatically.

```python
# From telemetry.py — graceful init
try:
    _init_provider()
except Exception:
    logger.warning("OTEL init failed; telemetry disabled (non-fatal)")
    _STATE.enabled = False
```

---

## Development Setup (Quick Start)

For local development with Jaeger:

```bash
# Start Jaeger all-in-one (UI on port 16686)
docker run -d --name jaeger \
  -p 16686:16686 \
  -p 4317:4317 \
  jaegertracing/all-in-one:latest

# Enable OTEL in .env
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317

# Restart services
docker compose restart api worker
```

Then open http://localhost:16686 to view traces.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| No traces in Jaeger | `OTEL_ENABLED=false` | Set to `true` and restart |
| Traces missing worker spans | Context not propagated | Check task dispatch headers |
| High memory usage | Batch export buffer full | Reduce `OTEL_BSP_MAX_QUEUE_SIZE` |
| Logs missing trace_id | Filter not attached | Check `TraceContextFilter` in logging config |
| Startup warning about OTEL | Collector unreachable | Start collector or disable OTEL |
