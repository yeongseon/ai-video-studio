"""Real SDK spans crossing the shared JSON correlation and redaction boundary."""

import io
import json
import logging
from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Tracer

from creator_service import telemetry
from creator_service.error_redaction import redact_log_text
from creator_service.logging_config import JsonFormatter, setup_json_logging

TRACE = "0123456789abcdef0123456789abcdef"
SPAN = "0123456789abcdef"


@pytest.fixture
def pipeline(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Tracer, io.StringIO]]:
    # Given an isolated logger graph and a local SDK provider (no exporters/network).
    root = logging.RootLogger(logging.INFO)
    monkeypatch.setattr(logging, "root", root)
    monkeypatch.setattr(logging.Logger, "root", root)
    monkeypatch.setattr(logging.Logger, "manager", logging.Manager(root))
    monkeypatch.setattr(telemetry, "_STATE", telemetry._TelemetryState())
    monkeypatch.setenv("OTEL_ENABLED", "false")
    telemetry.init_telemetry("probe")
    monkeypatch.setattr(telemetry._STATE, "enabled", True)
    setup_json_logging("probe")
    stream = io.StringIO()
    handler = root.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(stream)
    provider = TracerProvider()
    try:
        yield provider.get_tracer("correlation-probe"), stream
    finally:
        provider.shutdown()
        handler.close()


@pytest.mark.parametrize("name", ["creator.test.named", "uvicorn.error", ""])
def test_json_carries_active_context_when_record_reaches_root(pipeline, name: str) -> None:
    # Given the real JSON handler and an active SDK span.
    tracer, stream = pipeline
    with tracer.start_as_current_span("operation") as span:
        context = span.get_span_context()
        # When a named/server/root logger emits a record.
        logging.getLogger(name).warning("operation complete")
    # Then exactly one copy of each real correlation identifier reaches JSON.
    raw = stream.getvalue()
    output = json.loads(raw)
    assert output["trace_id"] == format(context.trace_id, "032x")
    assert output["span_id"] == format(context.span_id, "016x")
    assert raw.count('"trace_id":') == raw.count('"span_id":') == 1
    assert "_otel_correlation" not in output


def test_user_trace_is_redacted_when_there_is_no_active_span(pipeline) -> None:
    # Given user-supplied correlation-looking fields without an active span.
    _, stream = pipeline
    # When a named logger emits the extras.
    logging.getLogger("creator.test.named").warning("probe", extra={"trace_id": TRACE, "span_id": SPAN})
    # Then the field name alone never grants an exemption.
    output = json.loads(stream.getvalue())
    assert output["trace_id"] == "<redacted>"
    assert output["span_id"] == SPAN


def test_real_context_wins_when_user_supplies_correlation_fields(pipeline) -> None:
    # Given correlation-looking user extras and a different active SDK span.
    tracer, stream = pipeline
    with tracer.start_as_current_span("operation") as span:
        context = span.get_span_context()
        assert format(context.trace_id, "032x") != TRACE
        assert format(context.span_id, "016x") != SPAN
        # When logging user extras inside the span.
        logging.getLogger("creator.test.named").warning(
            "probe", extra={"trace_id": TRACE, "span_id": SPAN},
        )
    # Then the SDK's real context overrides both supplied identifiers.
    output = json.loads(stream.getvalue())
    assert output["trace_id"] == format(context.trace_id, "032x")
    assert output["span_id"] == format(context.span_id, "016x")


def test_other_hex_values_stay_redacted_when_context_is_valid(pipeline) -> None:
    # Given synthetic hex credentials in ordinary, nested and exception fields.
    tracer, stream = pipeline
    error = ValueError(TRACE)
    with tracer.start_as_current_span("operation") as span:
        context = span.get_span_context()
        # When the same record also carries genuine correlation identifiers.
        logging.getLogger("creator.test.named").error(
            "value %s", TRACE, exc_info=(ValueError, error, None),
            extra={"api_key": TRACE, "token": TRACE, "request_id": TRACE,
                   "nested": {"trace_id": TRACE, "items": [TRACE]}},
        )
    # Then only the top-level, filter-produced correlation is exempt.
    output = json.loads(stream.getvalue())
    assert output["message"] == "value <redacted>"
    assert output["exc_info"] == "ValueError: <redacted>"
    assert [output[key] for key in ("api_key", "token", "request_id")] == ["<redacted>"] * 3
    assert output["nested"] == {"trace_id": "<redacted>", "items": ["<redacted>"]}
    assert output["trace_id"] == format(context.trace_id, "032x")


def test_each_handler_has_one_filter_when_configuration_repeats(pipeline) -> None:
    # Given startup repeats with an extra existing root handler.
    tracer, stream = pipeline
    setup_json_logging("probe")
    stream_handler = logging.getLogger().handlers[0]
    assert isinstance(stream_handler, logging.StreamHandler)
    stream_handler.setStream(stream)
    handler = logging.NullHandler()
    logging.getLogger().addHandler(handler)
    # When telemetry initialization repeats without recreating providers.
    telemetry.init_telemetry("probe")
    telemetry.init_telemetry("probe")
    # Then every handler has exactly one filter, with none on the root logger.
    for installed in logging.getLogger().handlers:
        assert sum(isinstance(item, telemetry.TraceContextFilter) for item in installed.filters) == 1
    assert not any(isinstance(item, telemetry.TraceContextFilter) for item in logging.getLogger().filters)
    with tracer.start_as_current_span("operation") as span:
        logging.getLogger().warning("probe")
    raw = stream.getvalue()
    assert json.loads(raw)["trace_id"] == format(span.get_span_context().trace_id, "032x")
    assert raw.count('"trace_id":') == raw.count('"span_id":') == 1


def test_existing_handlers_receive_context_when_telemetry_starts(pipeline, monkeypatch) -> None:
    # Given root handlers installed before telemetry initialization.
    tracer, _ = pipeline
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("probe"))
    logging.getLogger().handlers = [handler]
    monkeypatch.setattr(telemetry._STATE, "initialized", False)
    monkeypatch.setenv("OTEL_ENABLED", "true")
    monkeypatch.setattr(telemetry, "_init_otel_providers", lambda *_args: None)
    # When telemetry starts, then a named logger emits inside a real span.
    telemetry.init_telemetry("probe")
    with tracer.start_as_current_span("operation") as span:
        logging.getLogger("creator.test.named").warning("probe")
    # Then pre-existing handlers also enrich propagated records.
    assert json.loads(stream.getvalue())["trace_id"] == format(span.get_span_context().trace_id, "032x")


def test_formatter_preserves_trace_when_filter_produced_provenance(pipeline) -> None:
    # Given a real filter-enriched record independently of handler placement.
    tracer, _ = pipeline
    record = logging.makeLogRecord({"msg": "probe"})
    with tracer.start_as_current_span("operation") as span:
        expected = format(span.get_span_context().trace_id, "032x")
        telemetry.TraceContextFilter().filter(record)
    # When the production formatter serializes it after the span exits.
    output = json.loads(JsonFormatter("probe").format(record))
    # Then valid filter provenance, not ambient context, grants the exemption.
    assert output["trace_id"] == expected
    assert "_otel_correlation" not in output


@pytest.mark.parametrize("field,value", [
    ("trace_id", "0" * 32), ("span_id", "0" * 16),
    ("trace_id", TRACE.upper()), ("span_id", SPAN.upper()),
    ("trace_id", TRACE + "a"), ("span_id", SPAN + "a"),
    ("trace_id", TRACE + "\n"), ("span_id", SPAN + "\n"),
    ("trace_id", TRACE), ("span_id", SPAN),
])
def test_formatter_rejects_exemption_when_ids_are_invalid_or_changed(pipeline, field, value) -> None:
    # Given real provenance, then a changed field (including valid-shaped spoofing).
    tracer, _ = pipeline
    record = logging.makeLogRecord({"msg": "probe"})
    with tracer.start_as_current_span("operation"):
        telemetry.TraceContextFilter().filter(record)
    setattr(record, field, value)
    # When the formatter sees a mismatch or invalid identifier pair.
    output = json.loads(JsonFormatter("probe").format(record))
    # Then neither field receives the pair exemption, and provenance stays private.
    assert output["trace_id"] == redact_log_text(record.__dict__["trace_id"])
    assert "_otel_correlation" not in output


@pytest.mark.parametrize("trace_id,span_id", [
    ("0" * 32, SPAN), (TRACE, "0" * 16),
    (TRACE.upper(), SPAN), (TRACE, SPAN.upper()),
    (TRACE + "a", SPAN), (TRACE, SPAN + "a"),
    (TRACE + "\n", SPAN), (TRACE, SPAN + "\n"),
])
def test_formatter_requires_valid_pair_even_when_provenance_matches(trace_id: str, span_id: str) -> None:
    # Given malformed ids with matching but invalid provenance at the formatter boundary.
    record = logging.makeLogRecord({"msg": "probe", "trace_id": trace_id, "span_id": span_id,
                                    "_otel_correlation": (trace_id, span_id)})
    # When the formatter serializes the record.
    output = json.loads(JsonFormatter("probe").format(record))
    # Then neither field bypasses ordinary redaction and provenance is never exported.
    assert output["trace_id"] == redact_log_text(trace_id)
    assert output["span_id"] == redact_log_text(span_id)
    assert "_otel_correlation" not in output


def test_trace_context_filter_never_raises_into_the_log_call_site(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given an enrichment helper that fails while a record is being logged.
    def failing_enrichment(record: dict) -> dict:
        raise RuntimeError("synthetic enrichment failure")

    monkeypatch.setattr(telemetry, "add_trace_context_to_log", failing_enrichment)
    record = logging.LogRecord("creator.named", logging.INFO, __file__, 1, "message", (), None)
    # When the handler-level filter runs.
    kept = telemetry.TraceContextFilter().filter(record)
    # Then logging continues without correlation fields or provenance.
    assert kept is True
    assert not hasattr(record, "trace_id")
    assert not hasattr(record, "_otel_correlation")
