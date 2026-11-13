"""Synthetic credential coverage at the shared logging boundary."""

import io
import json
import logging
import re
import tracemalloc
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from collections.abc import Callable
from typing import NoReturn
from uuid import UUID

import pytest
from creator_service import error_redaction
from creator_service.logging_config import JsonFormatter

SECRET = "sk-EXAMPLEEXAMPLE"
DENIED = [
    "password", "passwd", "api_key", "apiKey", "X-API-Key", "apikey", "secret",
    "client_secret", "token", "access_token", "authorization", "credential",
    "credentials", "private_key", "key", "cookie", "signing.key", "session key",
    "accessKey", "secret-key", "privateKey", "clientAuthorization",
    "passphrase", "pwd", "bearer", "bearerToken", "my_apikey",
]
ALLOWED = [
    "key_id", "key_fingerprint", "storage_key", "idempotency_key", "cache_key",
    "user_id", "run_id", "status", "tokens_used", "max_tokens", "token_count",
]


def encode(extras: dict[str, object] | None = None) -> str:
    record = logging.makeLogRecord({"msg": "terminal state", **(extras or {})})
    return JsonFormatter("api").format(record)


@pytest.mark.parametrize("credential", [
    "https://user:pass@host.example/x?api_key=SECRETVALUE12345",
    "Bearer synthetic-private-value", SECRET,
])
def test_message_redacts_percent_arguments(credential: str) -> None:
    # Given
    record = logging.makeLogRecord({"msg": "provider failed: %s", "args": (credential,)})
    # When
    raw = JsonFormatter("worker").format(record)
    # Then
    assert credential not in raw
    assert json.loads(raw)["message"] == "provider failed: <redacted>"


@pytest.mark.parametrize("explicit", [True, False], ids=["cause", "context"])
def test_exception_chain_preserves_frames_not_credentials(explicit: bool) -> None:
    # Given
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("api"))
    logger = logging.Logger("chain-probe")
    logger.addHandler(handler)
    # When
    try:
        try:
            raise ValueError(SECRET)
        except ValueError as inner:
            if explicit:
                raise RuntimeError("outer") from inner
            raise RuntimeError("outer")
    except RuntimeError:
        logger.exception("provider failed")
    # Then
    raw = stream.getvalue()
    assert SECRET not in raw
    trace = json.loads(raw)["exc_info"]
    assert "ValueError: <redacted>" in trace and "RuntimeError: outer" in trace
    connector = "direct cause" if explicit else "During handling"
    assert connector in trace and "Traceback (most recent call last):" in trace
    frames = re.findall(r'  File "[^"]+", line \d+, in [^\n]+', trace)
    assert frames and all(__file__ in frame for frame in frames)
    assert "test_exception_chain_preserves_frames_not_credentials" in trace


def test_long_traceback_redacts_each_line_without_losing_frames() -> None:
    # Given
    safe_lines = "\n".join("safe diagnostic " + "x" * 100 for _ in range(90))
    try:
        raise ValueError(safe_lines + "\n" + SECRET + "\n" + "x" * 9000 + SECRET)
    except ValueError as error:
        record = logging.makeLogRecord({"msg": "failed", "exc_info": (type(error), error, error.__traceback__)})
    # When
    raw = JsonFormatter("api").format(record)
    # Then
    assert SECRET not in raw
    trace = json.loads(raw)["exc_info"]
    assert len(trace) > 8192 and __file__ in trace
    assert "ValueError" in trace and "safe diagnostic" in trace
    assert trace.endswith("<redacted>\n<redacted>")


@pytest.mark.parametrize("line", ["safe", "x" * 1024, "é" * 1024], ids=["lines", "size", "utf8"])
def test_log_text_has_global_cap(line: str) -> None:
    # Given
    text = "\n".join([line] * 1000 + [SECRET])
    # When
    raw = encode({"detail": text})
    # Then
    detail = json.loads(raw)["detail"]
    assert SECRET not in raw
    assert detail.endswith("<output truncated>")
    assert len(detail.encode()) <= 65536 and len(detail.splitlines()) <= 401


@pytest.mark.parametrize("name", DENIED)
def test_sensitive_keys_are_replaced(name: str) -> None:
    # Given / When
    raw = encode({name: "synthetic-private-value"})
    # Then
    assert json.loads(raw)[name] == "<redacted>"
    assert getattr(error_redaction, "is_sensitive_log_key")(name) is True


@pytest.mark.parametrize("name", ALLOWED)
def test_diagnostic_keys_remain_visible(name: str) -> None:
    # Given / When
    raw = encode({name: 3})
    # Then
    assert json.loads(raw)[name] == 3
    assert getattr(error_redaction, "is_sensitive_log_key")(name) is False


def test_nested_containers_redact_keys_and_free_text() -> None:
    # Given
    value = {"details": [{"apiKey": "private-value", "key_id": 7}, (SECRET,)], "set": {SECRET}}
    # When
    raw = encode({"data": value})
    # Then
    assert SECRET not in raw and "private-value" not in raw
    assert json.loads(raw)["data"] == {
        "details": [{"apiKey": "<redacted>", "key_id": 7}, ["<redacted>"]], "set": ["<redacted>"],
    }
    assert value["details"][0]["apiKey"] == "private-value"


class BrokenText:
    def __str__(self) -> str:
        raise ValueError(SECRET)


class Provider(Enum):
    SYNTHETIC = SECRET

    def __str__(self) -> str:
        return self.value


@pytest.mark.parametrize("value", [
    BrokenText(), {BrokenText(): SECRET}, b"sk-EXAMPLEEXAMPLE", Path("/tmp/private/file"),
    Provider.SYNTHETIC, UUID("550e8400-e29b-41d4-a716-446655440000"),
    datetime(2026, 1, 1), date(2026, 1, 1), Decimal("1.25"),
], ids=["broken", "broken-key", "bytes", "path", "enum", "uuid", "datetime", "date", "decimal"])
def test_arbitrary_values_are_safe_json(value: object) -> None:
    # Given / When
    raw = encode({"detail": value, "token": "private-value"})
    # Then
    assert SECRET not in raw and "private-value" not in raw
    assert json.loads(raw)["token"] == "<redacted>"
    if isinstance(value, BrokenText):
        assert json.loads(raw)["detail"] == "<unrepresentable>"
    elif isinstance(value, (UUID, datetime, date, Decimal)):
        assert json.loads(raw)["detail"] == str(value)
    elif isinstance(value, (Path, Provider)):
        assert json.loads(raw)["detail"] == "<redacted>"


@pytest.mark.parametrize("shape", ["cycle", "depth", "width", "nodes"])
def test_containers_are_bounded(shape: str) -> None:
    # Given
    value: dict[str, object] = {"detail": SECRET}
    if shape == "cycle":
        value["self"] = value
    elif shape == "depth":
        for _ in range(20):
            value = {"child": value}
    elif shape == "width":
        value = {str(i): SECRET for i in range(100)}
    else:
        for _ in range(3):
            value = {str(i): value for i in range(10)}
    # When
    raw = encode({"data": value})
    # Then
    assert SECRET not in raw
    assert len(raw) < 20000
    assert any(marker in raw for marker in ("<truncated>", "<nested>", "<cycle>"))
    assert isinstance(json.loads(raw)["data"], dict)
    assert raw.count("<redacted>") <= 500
    if shape == "width":
        assert len(json.loads(raw)["data"]) <= 50
    if shape == "depth":
        assert raw.count('"child"') <= 6


def test_cancellation_content_and_field_order_are_preserved() -> None:
    # Given
    record = logging.makeLogRecord({"msg": "Run %d cancelled", "args": (3,), "levelname": "INFO",
                                    "name": "tasks.task_runner", "run_id": 3, "status": "cancelled", "key_id": None})
    formatter = JsonFormatter("worker")
    expected = {"timestamp": formatter.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S"),
                "level": "INFO", "service": "worker", "logger": "tasks.task_runner",
                "message": "Run 3 cancelled", "taskName": None,
                "run_id": 3, "status": "cancelled", "key_id": None}
    # When
    raw = formatter.format(record)
    # Then: characterization, including Python 3.12's ordinary taskName field.
    assert json.loads(raw) == expected
    assert raw == json.dumps(expected)


@pytest.mark.parametrize("forged", [
    f'  File "{SECRET}", line 1, in run',
    '  File "https://user:pass@host.example/x", line 1, in run',
    f'  File "/app/tasks/x.py", line 1, in token={SECRET}',
    '  File "/app/tasks/x.py", line 1, in Bearer synthetic-private-value',
    '  File "/app/0123456789abcdef0123456789abcdef.py", line 1, in run',
], ids=["token-path", "credential-url", "token-in-function", "bearer-in-function", "hex-path"])
def test_message_text_shaped_like_a_frame_header_cannot_smuggle_credentials(forged: str) -> None:
    # Given an exception message (provider error bodies echo request content, newlines included)
    # whose later line imitates a traceback frame header carrying a credential.
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("api"))
    logger = logging.Logger("forged-frame-probe")
    logger.addHandler(handler)

    # When it is logged with its traceback.
    try:
        raise RuntimeError(f"upstream said:\n{forged}")
    except RuntimeError:
        logger.exception("provider failed")

    # Then the credential is redacted even though the line has a frame header's shape.
    raw = stream.getvalue()
    for needle in (SECRET, "user:pass", "synthetic-private-value",
                   "0123456789abcdef0123456789abcdef"):
        assert needle not in raw
    json.loads(raw)


def test_real_frame_headers_keep_their_path_line_and_function() -> None:
    # Given genuine frame headers, which operators need and which carry no credentials.
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("api"))
    logger = logging.Logger("real-frame-probe")
    logger.addHandler(handler)

    # When an exception raised from a named function is logged.
    def failing_provider_call() -> None:
        raise ValueError(SECRET)

    try:
        failing_provider_call()
    except ValueError:
        logger.exception("provider failed")

    # Then the header still names this file, a line number and the function.
    trace = json.loads(stream.getvalue())["exc_info"]
    assert SECRET not in trace
    assert any(
        __file__ in frame and "in failing_provider_call" in frame
        for frame in re.findall(r'  File "[^"]+", line \d+, in [^\n]+', trace)
    )


def _logged(message: str, *args: object, exc: BaseException | None = None, **extra: object) -> str:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("api"))
    logger = logging.Logger("review-probe")
    logger.addHandler(handler)
    if exc is None:
        logger.error(message, *args, extra=extra or None)
    else:
        try:
            raise exc
        except BaseException:
            logger.exception(message, *args, extra=extra or None)
    return stream.getvalue()


HEADER_FORGERIES = {
    "underscore-path": ('  File "/tmp/_sk-EXAMPLEEXAMPLE", line 1, in run', "sk-EXAMPLEEXAMPLE"),
    "carriage-return": (
        '  File "/tmp/app.py", line 1, in run\rworkspaces/synthetic-private-value',
        "synthetic-private-value",
    ),
    "line-separator": (
        '  File "/tmp/app.py", line 1, in run\u2028workspaces/synthetic-private-value',
        "synthetic-private-value",
    ),
    "vertical-tab": (
        '  File "/tmp/app.py", line 1, in run\x0bworkspaces/synthetic-private-value',
        "synthetic-private-value",
    ),
}


@pytest.mark.parametrize("carrier", ["exception", "message", "extra"])
@pytest.mark.parametrize("forged, needle", HEADER_FORGERIES.values(), ids=HEADER_FORGERIES.keys())
def test_header_shaped_text_is_only_trusted_when_it_is_a_real_frame(
    carrier: str, forged: str, needle: str,
) -> None:
    # Given text shaped like a traceback frame header that carries a credential, delivered
    # through an exception message, a log message argument or a string extra.
    text = f"upstream said:\n{forged}"

    # When it is logged.
    if carrier == "exception":
        raw = _logged("provider failed", exc=RuntimeError(text))
    elif carrier == "message":
        raw = _logged("provider failed: %s", text)
    else:
        raw = _logged("provider failed", detail=text)

    # Then no frame-metadata exemption applies: provenance, not shape, decides.
    assert needle not in raw
    json.loads(raw)


def test_nested_key_longer_than_the_redaction_limit_still_hides_its_value() -> None:
    # Given a credential-named key too long for the per-text redactor to keep.
    key = "password." + "x" * 8192

    # When it is nested in an extra, and again as a top-level extra.
    nested = _logged("failed", detail={key: "synthetic-private-value"})
    top_level = _logged("failed", **{key: "synthetic-private-value"})

    # Then classification used the original key, and the value stays hidden.
    assert "synthetic-private-value" not in nested
    assert "synthetic-private-value" not in top_level
    assert "x" * 300 not in nested + top_level


def test_non_string_extra_keys_never_raise_or_reach_the_error_handler() -> None:
    # Given a handler that records logging's own error reporting, which prints raw arguments.
    reported: list[object] = []

    class Handler(logging.StreamHandler[io.StringIO]):
        def handleError(self, record: logging.LogRecord) -> None:
            reported.append(record.args)

    stream = io.StringIO()
    handler = Handler(stream)
    handler.setFormatter(JsonFormatter("api"))
    logger = logging.Logger("non-string-keys")
    logger.addHandler(handler)

    # When extras use keys that are not strings.
    logger.error("provider failed: %s", SECRET, extra={1: "diagnostic", (1, 2): "tuple-key"})

    # Then formatting succeeded, nothing was reported with the raw arguments, and no secret leaked.
    assert reported == []
    raw = stream.getvalue()
    assert SECRET not in raw
    assert json.loads(raw)["1"] == "diagnostic"


class HostileDict(dict[str, object]):
    def __str__(self) -> NoReturn:
        raise SystemExit(7)

    __repr__ = __str__

    def items(self) -> NoReturn:
        raise SystemExit(8)

    def __iter__(self) -> NoReturn:
        raise SystemExit(9)

    def __len__(self) -> NoReturn:
        raise SystemExit(10)


class HostileList(list[object]):
    def __str__(self) -> NoReturn:
        raise SystemExit(7)

    def __iter__(self) -> NoReturn:
        raise SystemExit(9)

    def __len__(self) -> NoReturn:
        raise SystemExit(10)


class HostileTuple(tuple[object, ...]):
    def __str__(self) -> NoReturn:
        raise SystemExit(7)

    def __iter__(self) -> NoReturn:
        raise SystemExit(9)


class HostileText(str):
    def __str__(self) -> str:
        raise SystemExit(7)


class HostileInt(int):
    def __int__(self) -> int:
        raise SystemExit(7)

    def __str__(self) -> str:
        raise SystemExit(7)


@pytest.mark.parametrize("value, expected", [
    (HostileDict(ok=True, token="private-value"), {"ok": True, "token": "<redacted>"}),
    (HostileList([1, SECRET]), [1, "<redacted>"]),
    (HostileTuple((1, SECRET)), [1, "<redacted>"]),
    (HostileText(SECRET), "<redacted>"),
    (HostileInt(5), 5),
], ids=["dict", "list", "tuple", "str", "int"])
def test_builtin_container_and_scalar_subclasses_are_read_without_calling_their_hooks(
    value: object, expected: object,
) -> None:
    # Given subclasses whose overridable hooks would terminate the process if invoked.
    # When they are logged as extras, as json.dumps would have serialized them natively.
    raw = _logged("failed", detail=value)

    # Then their contents are read through the base types and sanitized.
    assert json.loads(raw)["detail"] == expected
    assert "private-value" not in raw and SECRET not in raw


def test_input_processing_is_bounded_in_memory() -> None:
    # Given a huge message after the retained lines, and a huge extra key.
    huge_message = "ok\n" * 400 + "x" * 50_000_000
    huge_key = "x" * 20_000_000
    record = logging.makeLogRecord({"msg": huge_message, huge_key: 1})
    formatter = JsonFormatter("api")

    # When formatting, measured by peak allocation.
    tracemalloc.start()
    try:
        raw = formatter.format(record)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    # Then neither input was copied wholesale.
    assert peak < 8 * 1024 * 1024, f"peak allocation {peak} bytes"
    assert json.loads(raw)["message"].endswith("<output truncated>")


def test_non_finite_floats_keep_the_output_strict_json() -> None:
    # Given non-finite floats, which Python would emit as bare NaN / Infinity.
    raw = _logged("failed", duration=float("nan"), rate=float("inf"), series=[float("-inf")])

    # When parsed by a strict JSON parser.
    def reject(constant: str) -> object:
        raise AssertionError(f"non-standard JSON constant {constant}")

    payload = json.loads(raw, parse_constant=reject)

    # Then they were replaced by a marker, not dropped silently.
    assert payload["duration"] == payload["rate"] == payload["series"][0] == "<non-finite>"


def test_frame_metadata_that_carries_a_credential_is_not_trusted() -> None:
    # Given real frames whose code metadata IS a credential: a compiled filename and a def name.
    namespace: dict[str, Callable[[], None]] = {}
    exec("def AKIA1234567890123456():\n    raise RuntimeError('failed')\n", namespace)
    outcomes = []
    for run in (
        lambda: exec(compile("raise RuntimeError('failed')", SECRET, "exec")),
        namespace["AKIA1234567890123456"],
    ):
        try:
            run()
        except RuntimeError as error:
            outcomes.append(error)

    # When each is logged with its traceback.
    for error in outcomes:
        raw = _logged("provider failed", exc=error)

        # Then the genuine header does not get a verbatim exemption: it is redacted like any line.
        assert SECRET not in raw and "AKIA1234567890123456" not in raw


class ExitKey(int):
    def __str__(self) -> NoReturn:
        raise SystemExit(7)

    __repr__ = __str__


class ExitFloatKey(float):
    def __str__(self) -> NoReturn:
        raise SystemExit(7)

    __repr__ = __str__


def test_numeric_subclass_keys_are_read_without_calling_their_hooks() -> None:
    # Given integer and float keys whose overridable hooks would terminate the process.
    # When they are used as extras, as json.dumps would have encoded them natively.
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("api"))
    logger = logging.Logger("numeric-keys")
    logger.addHandler(handler)
    logger.error("failed", extra={ExitKey(1): "diagnostic", ExitFloatKey(2.5): "ratio"})

    # Then the keys keep their JSON spelling.
    payload = json.loads(stream.getvalue())
    assert payload["1"] == "diagnostic" and payload["2.5"] == "ratio"


def test_non_string_keys_keep_their_json_spelling() -> None:
    # Given None, bool, int and float dictionary keys, which json.dumps spells null/true/7/2.5.
    raw = _logged("failed", detail={None: "n", True: "t", 7: "i", 2.5: "f"})

    # When parsed, then the spellings are JSON's, not Python's repr.
    assert json.loads(raw)["detail"] == {"null": "n", "true": "t", "7": "i", "2.5": "f"}


def test_a_suppressed_context_is_never_stringified() -> None:
    # Given a chain whose SUPPRESSED context has a __str__ that would never return. The
    # interpreter's own traceback printing skips suppressed contexts, so the formatter must
    # not visit them either; the stdlib swallows failures of str() with a bare except, so the
    # observable invariant is that the hook is not invoked at all.
    called: list[str] = []

    class WatchedError(Exception):
        def __str__(self) -> str:
            called.append("__str__")
            return "watched"

    outer = RuntimeError("outer")
    outer.__context__ = WatchedError()
    outer.__suppress_context__ = True
    record = logging.makeLogRecord({"msg": "failed", "exc_info": (RuntimeError, outer, None)})

    # When formatted.
    raw = JsonFormatter("api").format(record)

    # Then the suppressed context was never rendered, and the printable part is kept.
    assert called == []
    assert "RuntimeError: outer" in json.loads(raw)["exc_info"]


def test_exception_group_breadth_adds_no_work_beyond_the_stdlib_formatter() -> None:
    # Given a wide exception group whose members count their own stringification.
    called: list[int] = []

    class CountedError(ValueError):
        def __str__(self) -> str:
            called.append(1)
            return "member"

    def wide_group() -> ExceptionGroup:
        return ExceptionGroup("wide", [CountedError() for _ in range(20_000)])

    group = wide_group()
    logging.Formatter().formatException((ExceptionGroup, group, None))
    baseline = len(called)
    called.clear()

    # When the JSON formatter handles an identical group.
    group = wide_group()
    record = logging.makeLogRecord({"msg": "failed", "exc_info": (ExceptionGroup, group, None)})
    json.loads(JsonFormatter("api").format(record))

    # Then it did exactly the stdlib's work: its own header collection stringified nothing.
    assert baseline > 0 and len(called) == baseline, f"{len(called)} calls, stdlib needs {baseline}"


def test_cause_cycles_terminate() -> None:
    # Given two exceptions that are each other's cause.
    first, second = RuntimeError("first"), RuntimeError("second")
    first.__cause__, second.__cause__ = second, first
    record = logging.makeLogRecord({"msg": "failed", "exc_info": (RuntimeError, first, None)})

    # When formatted, then it finishes with valid JSON.
    assert "RuntimeError" in json.loads(JsonFormatter("api").format(record))["exc_info"]


def test_extras_are_not_copied_before_the_cap_applies() -> None:
    # Given a record carrying two million extra fields.
    record = logging.makeLogRecord({f"field_{index}": index for index in range(2_000_000)})
    formatter = JsonFormatter("api")

    # When formatting, measured by peak allocation.
    tracemalloc.start()
    try:
        raw = formatter.format(record)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    # Then only the retained extras were materialized.
    assert peak < 8 * 1024 * 1024, f"peak allocation {peak} bytes"
    assert len(json.loads(raw)) <= 60


def test_timestamp_failure_cannot_reach_the_error_handler_with_raw_arguments() -> None:
    # Given a record whose creation time cannot be converted, and a handler that reports
    # logging's own errors (it prints the raw message arguments).
    reported: list[object] = []

    class Handler(logging.StreamHandler[io.StringIO]):
        def handleError(self, record: logging.LogRecord) -> None:
            reported.append(record.args)

    stream = io.StringIO()
    handler = Handler(stream)
    handler.setFormatter(JsonFormatter("api"))
    record = logging.makeLogRecord({"msg": "failed: %s", "args": (SECRET,), "created": float("nan")})

    # When it is handled.
    handler.handle(record)

    # Then nothing was reported and no secret leaked.
    assert reported == []
    raw = stream.getvalue()
    assert SECRET not in raw
    assert json.loads(raw)["message"] == "failed: <redacted>"


def test_envelope_fields_are_redacted_too() -> None:
    # Given a logger and a service named like a credential.
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(SECRET))
    logger = logging.Logger(SECRET)
    logger.addHandler(handler)

    # When it logs.
    logger.error("failed")

    # Then neither envelope field carries it.
    raw = stream.getvalue()
    assert SECRET not in raw
    assert json.loads(raw)["logger"] == json.loads(raw)["service"] == "<redacted>"


def test_real_headers_survive_for_chained_exceptions_and_explicit_tracebacks() -> None:
    # Given a chained exception and an exc_info tuple that carries its own traceback.
    def inner() -> None:
        raise ValueError(SECRET)

    def outer() -> None:
        try:
            inner()
        except ValueError as cause:
            raise RuntimeError("outer") from cause

    try:
        outer()
    except RuntimeError as error:
        record = logging.makeLogRecord(
            {"msg": "failed", "exc_info": (type(error), error, error.__traceback__)},
        )

    # When formatted.
    trace = json.loads(JsonFormatter("api").format(record))["exc_info"]

    # Then every frame of both exceptions keeps its path, line and function.
    headers = re.findall(r'  File "[^"]+", line \d+, in [^\n]+', trace)
    assert SECRET not in trace
    assert {name for header in headers for name in ("inner", "outer") if f"in {name}" in header} == {
        "inner", "outer",
    }
    assert all(__file__ in header for header in headers)


def test_oversized_integers_do_not_collapse_the_other_fields() -> None:
    # Given an integer past json's 4300-digit conversion limit, as a value and as a key,
    # next to ordinary diagnostics.
    huge = 10**5000

    # When it is logged.
    raw = _logged("failed", count=huge, detail={huge: "x", "ok": 1}, run_id=3, status="cancelled")

    # Then only the oversized numbers are replaced; the rest keeps its JSON types.
    payload = json.loads(raw)
    assert payload["count"] == "<unsupported>"
    assert payload["run_id"] == 3 and payload["status"] == "cancelled"
    assert payload["detail"]["ok"] == 1 and "<unrepresentable>" in payload["detail"]


def test_the_extras_cap_counts_input_fields_not_output_names() -> None:
    # Given many extras whose names all collapse to one placeholder (oversized keys fail closed),
    # so the number of OUTPUT names never grows.
    extras = {("x" * 257) + str(index): index for index in range(200)}

    # When logged.
    payload = json.loads(_logged("failed", **extras))

    # Then processing stopped at the cap and said so, instead of examining every field.
    assert payload["<truncated>"] == "<truncated>"
