"""Shared structured JSON logging configuration."""

import json
import logging
import math
import traceback
from itertools import islice
from types import TracebackType

from creator_service.error_redaction import (
    JsonValue,
    carries_credential,
    is_sensitive_log_key,
    redact_log_text,
)
from creator_service.uvicorn_logging import UvicornAccessFilter

_REDACTED = "<redacted>"
_UNREPRESENTABLE = "<unrepresentable>"
_TRUNCATED = "<truncated>"
_MAX_KEY_CHARS = 256
_MAX_DEPTH = 6
_MAX_ITEMS = 50
_MAX_NODES = 500
_MAX_EXCEPTIONS = 64
_MAX_FRAMES = 2000
_MAX_INT_BITS = 256
_STANDARD_ATTRIBUTES = frozenset({
    "name", "msg", "args", "created", "filename", "funcName", "levelname", "levelno",
    "lineno", "module", "msecs", "message", "pathname", "process", "processName",
    "relativeCreated", "thread", "threadName", "exc_info", "exc_text", "stack_info",
})


def _safe_log_string(value: object) -> str:
    """Object hooks are untrusted at the final logging boundary."""
    try:
        return redact_log_text(str.__str__(value) if isinstance(value, str) else str(value))
    except Exception:
        return _UNREPRESENTABLE


def _linked(exc: BaseException) -> list[tuple[BaseException, TracebackType | None]]:
    """The exceptions the interpreter prints after ``exc``, read through the base descriptors.

    Same rule as traceback printing: the cause, else a context that is not suppressed, plus the
    members of an exception group. Nothing here stringifies an exception or calls its hooks.
    """
    cause = BaseException.__cause__.__get__(exc)
    context = BaseException.__context__.__get__(exc)
    linked: list[BaseException] = []
    if cause is not None:
        linked.append(cause)
    elif context is not None and not BaseException.__suppress_context__.__get__(exc):
        linked.append(context)
    if isinstance(exc, BaseExceptionGroup):
        linked.extend(islice(BaseExceptionGroup.exceptions.__get__(exc), _MAX_EXCEPTIONS))
    return [(item, BaseException.__traceback__.__get__(item)) for item in linked]


def _frame_headers(exc_info: object) -> frozenset[str]:
    """Frame headers as the interpreter prints them, rebuilt from the live exception chain.

    These come from code metadata, not from message text, so they are the only lines
    redact_log_text keeps verbatim: a path, a line number and a function name. A header whose
    metadata itself looks like a credential (a compiled filename, a function name) is left out
    and is redacted like any other line.
    """
    try:
        if not isinstance(exc_info, tuple) or len(exc_info) != 3:
            return frozenset()
        top, top_traceback = exc_info[1], exc_info[2]
        if not isinstance(top, BaseException):
            return frozenset()
        headers: set[str] = set()
        seen: set[int] = set()
        pending: list[tuple[BaseException, TracebackType | None]] = [(top, top_traceback)]
        while pending and len(seen) < _MAX_EXCEPTIONS:
            current, current_traceback = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            for frame, lineno in islice(traceback.walk_tb(current_traceback), _MAX_FRAMES):
                code = frame.f_code
                header = f'  File "{code.co_filename}", line {lineno}, in {code.co_name}'
                if not carries_credential(header):
                    headers.add(header)
            pending.extend(_linked(current))
        return frozenset(headers)
    except Exception:
        return frozenset()


class _LogSanitizer:
    """Convert extras to JSON-safe values within one shared budget per log record.

    Subclasses of the built-in containers and scalars are read through their base types, as
    json.dumps does, so a hostile ``__iter__``/``__str__`` on them is never invoked. Anything
    else is stringified as before, but guarded.
    """

    def __init__(self) -> None:
        self.remaining = _MAX_NODES
        self.active: set[int] = set()

    def field_name(self, key: object) -> tuple[str, bool]:
        """Return (display name, sensitive), classifying the ORIGINAL key before any redaction.

        Redacting first would hide the credential-looking text the classifier needs, and a key
        that is too long, or of an unexpected type, cannot be classified so it fails closed.
        """
        if isinstance(key, str):
            text = str.__str__(key)
            if len(text) > _MAX_KEY_CHARS:
                return _REDACTED, True
            return redact_log_text(text), is_sensitive_log_key(text)
        if key is None:
            return "null", False
        if isinstance(key, bool):
            return ("true" if key else "false"), False
        if isinstance(key, int):
            if int.bit_length(key) > _MAX_INT_BITS:
                return _UNREPRESENTABLE, True
            return int.__repr__(key), False
        if isinstance(key, float):
            number = float.__float__(key)
            return (float.__repr__(number) if math.isfinite(number) else "<non-finite>"), False
        return _UNREPRESENTABLE, True

    def visit(self, value: object, depth: int = 0) -> JsonValue:
        if self.remaining <= 0:
            return _TRUNCATED
        self.remaining -= 1
        if depth >= _MAX_DEPTH:
            return "<nested>"
        try:
            return self._convert(value, depth)
        except Exception:
            return _UNREPRESENTABLE

    def _convert(self, value: object, depth: int) -> JsonValue:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, str):
            return redact_log_text(str.__str__(value))
        if isinstance(value, int):
            return int.__int__(value) if int.bit_length(value) <= _MAX_INT_BITS else "<unsupported>"
        if isinstance(value, float):
            number = float.__float__(value)
            return number if math.isfinite(number) else "<non-finite>"
        if isinstance(value, (dict, list, tuple, set, frozenset)):
            if id(value) in self.active:
                return "<cycle>"
            self.active.add(id(value))
            try:
                return self._container(value, depth)
            finally:
                self.active.discard(id(value))
        return _safe_log_string(value)

    def _container(self, value: object, depth: int) -> JsonValue:
        if isinstance(value, dict):
            size = dict.__len__(value)
            result: dict[str, JsonValue] = {}
            for index, (key, item) in enumerate(dict.items(value)):
                if self.remaining <= 0 or index >= _MAX_ITEMS - 1 and size > _MAX_ITEMS:
                    result[_TRUNCATED] = _TRUNCATED
                    break
                name, sensitive = self.field_name(key)
                if sensitive:
                    self.remaining -= 1
                result[name] = _REDACTED if sensitive else self.visit(item, depth + 1)
            return result
        if isinstance(value, list):
            iterator, size = list.__iter__(value), list.__len__(value)
        elif isinstance(value, tuple):
            iterator, size = tuple.__iter__(value), tuple.__len__(value)
        elif isinstance(value, frozenset):
            iterator, size = frozenset.__iter__(value), frozenset.__len__(value)
        elif isinstance(value, set):
            iterator, size = set.__iter__(value), set.__len__(value)
        else:
            return _UNREPRESENTABLE
        items: list[JsonValue] = []
        for index, item in enumerate(islice(iterator, _MAX_ITEMS)):
            if self.remaining <= 0 or index >= _MAX_ITEMS - 1 and size > _MAX_ITEMS:
                items.append(_TRUNCATED)
                break
            items.append(self.visit(item, depth + 1))
        return items


class JsonFormatter(logging.Formatter):
    """JSON formatter for structured logging.

    Message, formatted exception chain and extra fields are redacted before they are emitted,
    and formatting never raises an ordinary exception: logging would otherwise report the
    failure with the record's raw arguments.
    """

    def __init__(self, service_name: str) -> None:
        """Initialize the formatter for the named service."""
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON.

        Args:
            record: Log record to format

        Returns:
            JSON-formatted log string
        """
        log_obj: dict[str, JsonValue] = {
            "timestamp": self._timestamp(record),
            "level": _safe_log_string(record.levelname),
            "service": _safe_log_string(self.service_name),
            "logger": _safe_log_string(record.name),
            "message": _UNREPRESENTABLE,
        }
        try:
            log_obj["message"] = redact_log_text(record.getMessage())
        except Exception:
            pass

        # Add exception info if present
        if record.exc_info:
            try:
                log_obj["exc_info"] = redact_log_text(
                    self.formatException(record.exc_info), _frame_headers(record.exc_info),
                )
            except Exception:
                log_obj["exc_info"] = _UNREPRESENTABLE

        log_obj.update(self._extras(record))

        try:
            return json.dumps(log_obj, allow_nan=False)
        except Exception:
            return json.dumps({key: _safe_log_string(value) for key, value in log_obj.items()})

    def _timestamp(self, record: logging.LogRecord) -> str:
        try:
            return self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S")
        except Exception:
            return _UNREPRESENTABLE

    @staticmethod
    def _extras(record: logging.LogRecord) -> dict[str, JsonValue]:
        sanitizer = _LogSanitizer()
        extras: dict[str, JsonValue] = {}
        examined = 0
        try:
            for key, value in record.__dict__.items():
                if key in _STANDARD_ATTRIBUTES:
                    continue
                # Count fields examined, not names emitted: placeholders can collide.
                if examined >= _MAX_ITEMS - 1:
                    extras[_TRUNCATED] = _TRUNCATED
                    break
                examined += 1
                name, sensitive = sanitizer.field_name(key)
                extras[name] = _REDACTED if sensitive else sanitizer.visit(value)
        except Exception:
            extras["extras"] = _UNREPRESENTABLE
        return extras


def setup_json_logging(service_name: str, level: str = "INFO") -> None:
    """Route application and server records through one redacting root handler."""
    root_logger = logging.getLogger()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(service_name=service_name))
    root_logger.handlers = [handler]
    root_logger.setLevel(getattr(logging, level))

    # Requires app import AFTER Uvicorn Config.__init__ (import-string CLI startup).
    # uvicorn.run(app_object) / Config(app=obj) after import reapplies dictConfig.
    # Keep configured levels and deliberately silenced loggers (--no-access-log).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi"):
        server_logger = logging.getLogger(name)
        if server_logger.handlers:
            server_logger.handlers.clear()
            server_logger.propagate = True
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, UvicornAccessFilter) for item in access_logger.filters):
        access_logger.addFilter(UvicornAccessFilter())

    # Suppress noisy loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
