"""Fail-closed redaction at the SDK's log processing and span export boundaries."""

from collections.abc import Mapping, Sequence
from itertools import islice
from typing import Final

from opentelemetry.sdk._logs import LogRecordProcessor, ReadWriteLogRecord
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Status
from opentelemetry.util.types import AnyValue, AttributeValue

from creator_service import error_redaction
from creator_service.telemetry import CORRELATION_ATTRIBUTE

_REDACTED: Final = "<redacted>"
_MAX_ITEMS: Final = 50
_MAX_NODES: Final = 500
_MAX_DEPTH: Final = 6
_MAX_KEY_CHARS: Final = 256


def _text(text: str) -> str:
    try:
        return error_redaction.redact_log_text(text)
    except Exception:
        # This is the final secret-handling boundary; reporting raw input is never safe.
        return _REDACTED


class _LogValues:
    """One shared work budget for a log body and all its SDK attributes."""

    def __init__(self) -> None:
        self.remaining = _MAX_NODES

    def attributes(self, values: Mapping[str, AnyValue], depth: int = 0) -> dict[str, AnyValue]:
        result: dict[str, AnyValue] = {}
        for key, value in islice(values.items(), _MAX_ITEMS):
            if self.remaining <= 0:
                result["<truncated>"] = _REDACTED
                break
            if len(key) > _MAX_KEY_CHARS:
                result[_REDACTED] = _REDACTED
                self.remaining -= 1
                continue
            sensitive = error_redaction.is_sensitive_log_key(key)
            result[_text(key)] = self.visit(_REDACTED if sensitive else value, depth + 1)
        return result

    def visit(self, value: AnyValue, depth: int = 0) -> AnyValue:
        if self.remaining <= 0 or depth >= _MAX_DEPTH:
            return _REDACTED
        self.remaining -= 1
        match value:
            case str():
                return _text(value)
            case None | bool() | int() | float():
                return value
            case bytes():
                return _REDACTED
            case Mapping():
                return self.attributes(value, depth)
            case Sequence():
                return tuple(self.visit(item, depth + 1) for item in islice(value, _MAX_ITEMS))


class RedactingLogRecordProcessor(LogRecordProcessor):
    """Run before every exporting processor, after SDK exception/body translation.

    Only the SDK-owned record is changed. The Python LogRecord shared with the
    JSON stream handler, its exception objects and their chains remain untouched.
    """

    def on_emit(self, log_record: ReadWriteLogRecord) -> None:
        record = log_record.log_record
        try:
            sanitizer = _LogValues()
            body = sanitizer.visit(record.body)
            attributes = sanitizer.attributes(
                {key: value for key, value in (record.attributes or {}).items() if key != CORRELATION_ATTRIBUTE}
            )
            record.body = body
            record.attributes = attributes
            if record.event_name:
                record.event_name = _text(record.event_name)
            if record.severity_text:
                record.severity_text = _text(record.severity_text)
        except Exception:
            # Atomic failure fallback: no partially sanitized field can escape.
            record.body = _REDACTED
            record.attributes = {"redaction.failure": _REDACTED}
            record.event_name = None
            record.severity_text = None

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def _event_value(value: AttributeValue) -> AttributeValue:
    match value:
        case str():
            return _text(value)
        case bool() | int() | float():
            return value
        case Sequence():
            if any(isinstance(item, str) for item in islice(value, _MAX_ITEMS)):
                return tuple(_text(item) for item in islice(value, _MAX_ITEMS)
                             if isinstance(item, str))
            return value


def _exception_event(event: Event) -> Event:
    try:
        attributes = {
            _text(key): _event_value(value)
            for key, value in (event.attributes or {}).items()
        }
    except Exception:
        attributes = {"exception.message": _REDACTED}
    return Event(event.name, attributes=attributes, timestamp=event.timestamp)


def _span(span: ReadableSpan) -> ReadableSpan:
    # Public SDK snapshot construction avoids mutating ended spans/private SDK state.
    return ReadableSpan(
        name=span.name, context=span.context, parent=span.parent, resource=span.resource,
        attributes=span.attributes,
        events=tuple(_exception_event(event) if event.name == "exception" else event
                     for event in span.events),
        links=span.links, kind=span.kind,
        status=Status(span.status.status_code, _text(span.status.description)
                      if span.status.description is not None else None),
        start_time=span.start_time, end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class RedactingSpanExporter(SpanExporter):
    """Sanitize exception events/status for either configured span transport."""

    def __init__(self, exporter: SpanExporter) -> None:
        self.exporter = exporter

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self.exporter.export(tuple(_span(span) for span in spans))

    def shutdown(self) -> None:
        self.exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.exporter.force_flush(timeout_millis)
