"""Exercise the production provider wiring with real SDK records and exporters."""

import json
import logging
from io import StringIO
from types import SimpleNamespace

import pytest
from opentelemetry.trace import get_current_span
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from creator_service import error_redaction, telemetry
from creator_service.logging_config import JsonFormatter

SECRET = "sk-EXAMPLE1234567890abcdef"
DIAGNOSTIC = f"token={SECRET} https://user:pass@example.invalid/x"


@pytest.fixture(params=[False, True], ids=["console", "otlp"])
def pipeline(request, monkeypatch):
    # Given the SAME factories used by init_telemetry, replacing only transports/setters.
    sdk = telemetry._load_otel()
    assert sdk is not None
    spans = InMemorySpanExporter()
    logs = InMemoryLogRecordExporter()
    providers = SimpleNamespace()
    sdk["trace"] = SimpleNamespace(
        get_current_span=get_current_span,
        set_tracer_provider=lambda provider: setattr(providers, "traces", provider),
        get_tracer=lambda name: providers.traces.get_tracer(name),
    )
    sdk["metrics"] = SimpleNamespace(
        set_meter_provider=lambda provider: setattr(providers, "metrics", provider),
    )
    sdk["set_logger_provider"] = lambda provider: setattr(providers, "logs", provider)
    for key in ("ConsoleSpanExporter", "OTLPSpanExporter"):
        sdk[key] = lambda **_kwargs: spans
    for key in ("ConsoleLogExporter", "OTLPLogExporter"):
        sdk[key] = lambda **_kwargs: logs
    monkeypatch.setattr(telemetry, "_load_otel", lambda: sdk)
    monkeypatch.setattr(telemetry, "_STATE", telemetry._TelemetryState())
    monkeypatch.setenv("OTEL_ENABLED", "true")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    if request.param:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid:4317")
    # No metric reader thread or metric output is needed for this signal-boundary test.
    sdk["PeriodicExportingMetricReader"] = lambda *_args: None
    sdk["MeterProvider"] = lambda **_kwargs: SimpleNamespace(shutdown=lambda: None)
    stream = StringIO()
    stream_handler = logging.StreamHandler(stream)
    stream_handler.setFormatter(JsonFormatter("probe"))
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [stream_handler])
    monkeypatch.setattr(root, "filters", [])
    telemetry.init_telemetry("probe")
    assert telemetry._STATE.enabled
    handler = root.handlers[-1]
    stream.seek(0)
    stream.truncate()
    try:
        yield SimpleNamespace(spans=spans, logs=logs, providers=providers,
                              handler=handler, stream=stream)
    finally:
        providers.logs.shutdown()
        providers.traces.shutdown()
        providers.metrics.shutdown()


def assert_safe(text: str) -> None:
    assert SECRET not in text
    assert "user:pass@" not in text
    assert "<redacted>" in text


@pytest.mark.parametrize("chain", ["cause", "context"])
def test_log_export_redacts_entire_record_when_exception_is_chained(pipeline, chain):
    # Given body arguments, cached exception text and credential-labelled/plain extras.
    try:
        try:
            raise ValueError(DIAGNOSTIC)
        except ValueError as cause:
            if chain == "cause":
                raise RuntimeError("outer") from cause
            raise RuntimeError("outer")
    except RuntimeError as error:
        record = logging.LogRecord("probe", logging.ERROR, __file__, 1,
                                   "ordinary %s", (DIAGNOSTIC,),
                                   (type(error), error, error.__traceback__))
    record.exc_text = DIAGNOSTIC
    record.api_key = "opaque-secret-value"
    record.detail = DIAGNOSTIC
    record.token_count = 7
    before = JsonFormatter("probe").format(record)
    # When all root handlers receive the shared Python record.
    for handler in logging.getLogger().handlers:
        handler.handle(record)
    pipeline.providers.logs.force_flush()
    # Then inspect EVERY serialized exported field, not just the message body.
    exported, = pipeline.logs.get_finished_logs()
    assert_safe(exported.to_json())
    assert "opaque-secret-value" not in exported.to_json()
    assert exported.log_record.body.startswith("ordinary ")
    assert exported.log_record.attributes["exception.type"] == "RuntimeError"
    assert "ValueError" in exported.log_record.attributes["exception.stacktrace"]
    assert exported.log_record.attributes["token_count"] == 7
    assert JsonFormatter("probe").format(record) == before
    assert json.loads(pipeline.stream.getvalue()) == json.loads(before)
    assert record.args == (DIAGNOSTIC,) and record.exc_text == DIAGNOSTIC


def test_span_export_redacts_exception_attributes_when_span_raises(pipeline):
    # Given a normal span, including a custom string/sequence exception attribute.
    tracer = telemetry.get_tracer("probe")
    # When SDK exception recording also generates the span status description.
    with pytest.raises(ValueError), tracer.start_as_current_span("operation") as span:
        assert span is not None
        span.add_event("exception", {"detail": DIAGNOSTIC, "items": [DIAGNOSTIC],
                                     "ordinary": "useful", "exception.type": "ValueError"})
        raise ValueError(DIAGNOSTIC)
    pipeline.providers.traces.force_flush()
    # Then all exported exception fields/status are safe, types and ordinary text survive.
    exported, = pipeline.spans.get_finished_spans()
    assert_safe(exported.to_json())
    assert exported.events[0].attributes["ordinary"] == "useful"
    assert exported.events[-1].attributes["exception.type"] == "ValueError"


@pytest.mark.parametrize("signal", ["log", "span"])
def test_export_fails_closed_when_redactor_raises(pipeline, monkeypatch, signal):
    # Given an unexpected failure inside the shared redactor.
    def broken(*_args, **_kwargs):
        raise LookupError("synthetic redactor failure")

    monkeypatch.setattr(error_redaction, "redact_log_text", broken)
    # When exporting must neither raise nor fall back to raw input.
    if signal == "log":
        record = logging.LogRecord("probe", logging.ERROR, __file__, 1, DIAGNOSTIC, (), None)
        record.detail = DIAGNOSTIC
        pipeline.handler.handle(record)
        pipeline.providers.logs.force_flush()
        exported, = pipeline.logs.get_finished_logs()
    else:
        with telemetry.get_tracer("probe").start_as_current_span("operation") as span:
            assert span is not None
            span.add_event("exception", {"exception.message": DIAGNOSTIC})
        pipeline.providers.traces.force_flush()
        exported, = pipeline.spans.get_finished_spans()
    # Then a fixed safe placeholder reaches the exporter.
    assert_safe(exported.to_json())


def test_task_span_is_redacted_when_worker_decorator_propagates_exception(pipeline):
    # Given the worker's shared decorator using the production-configured tracer.
    @telemetry.trace_task("probe")
    def task():
        raise ValueError(DIAGNOSTIC)

    # When a worker task fails.
    with pytest.raises(ValueError):
        task()
    pipeline.providers.traces.force_flush()
    # Then the worker is covered without any middleware participation.
    exported, = pipeline.spans.get_finished_spans()
    assert exported.name == "celery.task.probe"
    assert_safe(exported.to_json())


def test_initialization_is_idempotent_when_called_twice(pipeline):
    # Given an initialized production pipeline.
    handlers = tuple(logging.getLogger().handlers)
    # When startup invokes initialization again.
    telemetry.init_telemetry("probe")
    # Then exactly one record/span reaches each exporter.
    assert tuple(logging.getLogger().handlers) == handlers
    pipeline.handler.handle(logging.LogRecord("probe", logging.INFO, __file__, 1, "ok", (), None))
    with telemetry.get_tracer("probe").start_as_current_span("once"):
        pass
    pipeline.providers.logs.force_flush()
    pipeline.providers.traces.force_flush()
    assert len(pipeline.logs.get_finished_logs()) == len(pipeline.spans.get_finished_spans()) == 1
