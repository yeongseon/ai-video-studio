from __future__ import annotations

import functools
import logging
import os
from contextlib import nullcontext
from dataclasses import dataclass
from importlib import import_module
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry.context import get_current
from opentelemetry.propagate import extract, inject


class _NoOpInstrument:
    def add(self, _value: int | float, _attributes: dict[str, str] | None = None) -> None:
        return None

    def record(self, _value: int | float, _attributes: dict[str, str] | None = None) -> None:
        return None


class _NoOpMeter:
    def create_counter(self, _name: str, **_kwargs: Any) -> _NoOpInstrument:
        return _NoOpInstrument()

    def create_histogram(self, _name: str, **_kwargs: Any) -> _NoOpInstrument:
        return _NoOpInstrument()


class _NoOpTracer:
    def start_as_current_span(self, _name: str, **_kwargs: Any):
        return nullcontext()


@dataclass
class _TelemetryState:
    initialized: bool = False
    enabled: bool = False


_STATE = _TelemetryState()


class TraceContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        enriched = add_trace_context_to_log({})
        for key, value in enriched.items():
            setattr(record, key, value)
        return True


def _is_enabled() -> bool:
    value = os.getenv("OTEL_ENABLED", "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _load_otel() -> dict[str, Any] | None:
    try:
        from opentelemetry import metrics, trace
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs import LoggingHandler
        from opentelemetry.sdk._logs.export import (
            BatchLogRecordProcessor,
            ConsoleLogExporter,
            SimpleLogRecordProcessor,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import (
            ConsoleMetricExporter,
            PeriodicExportingMetricReader,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            ConsoleSpanExporter,
            SimpleSpanProcessor,
        )
        from opentelemetry.trace.span import INVALID_SPAN_CONTEXT

        otlp_log_exporter = None
        otlp_metric_exporter = None
        otlp_span_exporter = None
        try:
            otlp_log_exporter = import_module(
                "opentelemetry.exporter.otlp.proto.grpc._log_exporter"
            ).OTLPLogExporter
            otlp_metric_exporter = import_module(
                "opentelemetry.exporter.otlp.proto.grpc.metric_exporter"
            ).OTLPMetricExporter
            otlp_span_exporter = import_module(
                "opentelemetry.exporter.otlp.proto.grpc.trace_exporter"
            ).OTLPSpanExporter
        except ImportError:
            pass

        return {
            "metrics": metrics,
            "trace": trace,
            "LoggerProvider": LoggerProvider,
            "BatchLogRecordProcessor": BatchLogRecordProcessor,
            "ConsoleLogExporter": ConsoleLogExporter,
            "SimpleLogRecordProcessor": SimpleLogRecordProcessor,
            "MeterProvider": MeterProvider,
            "ConsoleMetricExporter": ConsoleMetricExporter,
            "PeriodicExportingMetricReader": PeriodicExportingMetricReader,
            "Resource": Resource,
            "TracerProvider": TracerProvider,
            "BatchSpanProcessor": BatchSpanProcessor,
            "ConsoleSpanExporter": ConsoleSpanExporter,
            "SimpleSpanProcessor": SimpleSpanProcessor,
            "INVALID_SPAN_CONTEXT": INVALID_SPAN_CONTEXT,
            "OTLPLogExporter": otlp_log_exporter,
            "OTLPMetricExporter": otlp_metric_exporter,
            "OTLPSpanExporter": otlp_span_exporter,
            "set_logger_provider": set_logger_provider,
            "LoggingHandler": LoggingHandler,
        }
    except ImportError:
        return None


def init_telemetry(service_name: str) -> None:
    """
    Initialize OpenTelemetry tracing, metrics, and logging (idempotent).

    Safe to call multiple times and across forked processes. Second and
    subsequent calls are no-ops. Each forked worker (e.g., Celery worker)
    will call this independently via signal handlers.

    Production Configuration:
    - OTEL_ENABLED: Set to 'true' to enable instrumentation (default: 'false')
    - OTEL_EXPORTER_OTLP_ENDPOINT: gRPC endpoint for span/metric/log export
      Example: 'http://localhost:4317' (default gRPC port)

    Sampling Configuration (OpenTelemetry SDK):
    - OTEL_TRACES_SAMPLER: Sampler strategy for production
      Recommended: 'parentbased_traceidratio' for distributed tracing
      Other options: 'always_on', 'always_off', 'traceidratio'
    - OTEL_TRACES_SAMPLER_ARG: Sampling probability (0.0-1.0)
      Example: '0.1' for 10% sampling on high-throughput services
      Example: '1.0' for 100% sampling during development/debugging
    - OTEL_EXPORTER_OTLP_PROTOCOL: Protocol for export
      Options: 'grpc' (default), 'http/protobuf'

    Environment Configuration:
    - OTEL_ENVIRONMENT: Deployment environment (default: 'development')
    - OTEL_SERVICE_NAME: Service identifier in traces (default: service_name arg)
    - OTEL_SERVICE_VERSION: Service version in resource attributes

    Fork Safety:
    Each forked Celery worker calls init_telemetry independently via
    the worker_process_init signal. The idempotency guard prevents
    reinitializing the SDK if called multiple times within the same process.
    """
    logger = logging.getLogger(__name__)

    if _STATE.initialized:
        return

    _STATE.initialized = True
    root_logger = logging.getLogger()
    if not any(isinstance(existing, TraceContextFilter) for existing in root_logger.filters):
        root_logger.addFilter(TraceContextFilter())

    if not _is_enabled():
        _STATE.enabled = False
        return

    otel = _load_otel()
    if otel is None:
        _STATE.enabled = False
        return

    try:
        _init_otel_providers(otel, service_name, logger, root_logger)
    except Exception:
        logger.exception("OpenTelemetry initialization failed; telemetry disabled")
        _STATE.enabled = False
        return

    _STATE.enabled = True


def _init_otel_providers(otel: dict, service_name: str, logger: logging.Logger, root_logger: logging.Logger) -> None:
    from creator_service.telemetry_redaction import (
        RedactingLogRecordProcessor,
        RedactingSpanExporter,
    )

    environment = os.getenv("OTEL_ENVIRONMENT", "development")
    configured_service_name = os.getenv("OTEL_SERVICE_NAME", service_name)
    service_version = os.getenv("OTEL_SERVICE_VERSION", "unknown")
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    is_otlp = bool(endpoint)

    resource = otel["Resource"].create(
        {
            "service.name": configured_service_name,
            "service.version": service_version,
            "deployment.environment": environment,
        }
    )

    tracer_provider = otel["TracerProvider"](resource=resource)
    otlp_span_exporter = otel.get("OTLPSpanExporter")
    if is_otlp and otlp_span_exporter is not None:
        span_exporter = RedactingSpanExporter(otlp_span_exporter(endpoint=endpoint))
        tracer_provider.add_span_processor(otel["BatchSpanProcessor"](span_exporter))
    else:
        if not is_otlp:
            logger.warning(
                "OTEL_EXPORTER_OTLP_ENDPOINT is not configured; falling back to ConsoleSpanExporter"
            )
        elif otlp_span_exporter is None:
            logger.warning("OTLP span exporter is unavailable; falling back to ConsoleSpanExporter")
        tracer_provider.add_span_processor(
            otel["SimpleSpanProcessor"](RedactingSpanExporter(otel["ConsoleSpanExporter"]()))
        )
    otel["trace"].set_tracer_provider(tracer_provider)

    metric_readers = []
    otlp_metric_exporter = otel.get("OTLPMetricExporter")
    if is_otlp and otlp_metric_exporter is not None:
        metric_readers.append(
            otel["PeriodicExportingMetricReader"](otlp_metric_exporter(endpoint=endpoint))
        )
    else:
        if not is_otlp:
            logger.warning(
                "OTEL_EXPORTER_OTLP_ENDPOINT is not configured; falling back to ConsoleMetricExporter"
            )
        elif otlp_metric_exporter is None:
            logger.warning(
                "OTLP metric exporter is unavailable; falling back to ConsoleMetricExporter"
            )
        metric_readers.append(
            otel["PeriodicExportingMetricReader"](otel["ConsoleMetricExporter"]())
        )
    meter_provider = otel["MeterProvider"](resource=resource, metric_readers=metric_readers)
    otel["metrics"].set_meter_provider(meter_provider)

    logger_provider = otel["LoggerProvider"](resource=resource)
    logger_provider.add_log_record_processor(RedactingLogRecordProcessor())
    otlp_log_exporter = otel.get("OTLPLogExporter")
    if is_otlp and otlp_log_exporter is not None:
        logger_provider.add_log_record_processor(
            otel["BatchLogRecordProcessor"](otlp_log_exporter(endpoint=endpoint))
        )
    else:
        if not is_otlp:
            logger.warning(
                "OTEL_EXPORTER_OTLP_ENDPOINT is not configured; falling back to ConsoleLogExporter"
            )
        elif otlp_log_exporter is None:
            logger.warning("OTLP log exporter is unavailable; falling back to ConsoleLogExporter")
        logger_provider.add_log_record_processor(
            otel["SimpleLogRecordProcessor"](otel["ConsoleLogExporter"]())
        )

    set_logger_provider = otel.get("set_logger_provider")
    logging_handler_type = otel.get("LoggingHandler")

    if set_logger_provider is not None:
        set_logger_provider(logger_provider)
    if logging_handler_type is not None and not any(
        isinstance(handler, logging_handler_type) for handler in root_logger.handlers
    ):
        root_logger.addHandler(
            logging_handler_type(level=logging.NOTSET, logger_provider=logger_provider)
        )



def get_tracer(name: str):
    if not _STATE.enabled:
        return _NoOpTracer()
    otel = _load_otel()
    if otel is None:
        return _NoOpTracer()
    return otel["trace"].get_tracer(name)


def get_meter(name: str):
    if not _STATE.enabled:
        return _NoOpMeter()
    otel = _load_otel()
    if otel is None:
        return _NoOpMeter()
    return otel["metrics"].get_meter(name)


def add_trace_context_to_log(record: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(record)
    if not _STATE.enabled:
        return enriched

    otel = _load_otel()
    if otel is None:
        return enriched

    span = otel["trace"].get_current_span()
    if span is None:
        return enriched

    span_context = span.get_span_context()
    if span_context is None or span_context == otel["INVALID_SPAN_CONTEXT"]:
        return enriched

    if getattr(span_context, "is_valid", False):
        enriched["trace_id"] = format(span_context.trace_id, "032x")
        enriched["span_id"] = format(span_context.span_id, "016x")
    return enriched


def trace_task(task_name: str):
    """Decorator to add OTEL tracing to Celery tasks."""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            request = getattr(args[0], "request", None) if args else None
            headers = getattr(request, "headers", None) or {}
            parent_context = extract(headers)
            token = otel_context.attach(parent_context)
            tracer = get_tracer(__name__)
            try:
                with tracer.start_as_current_span(
                    f"celery.task.{task_name}",
                    attributes={"celery.task_name": task_name},
                ):
                    return func(*args, **kwargs)
            finally:
                otel_context.detach(token)

        return wrapper

    return decorator


def get_trace_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    inject(headers, context=get_current())
    return headers
