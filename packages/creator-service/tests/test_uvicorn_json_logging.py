"""Uvicorn's default handlers must not bypass the shared redaction boundary."""

import io
import json
import logging
import logging.config
import weakref
from collections.abc import Iterator

import pytest
from uvicorn.config import LOGGING_CONFIG, Config

from creator_service.logging_config import JsonFormatter, setup_json_logging

UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")


@pytest.fixture(autouse=True)
def isolated_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # dictConfig closes registered handlers: isolate its registry as well as the
    # logger graph so pytest's capture/file handlers are never closed or changed.
    root = logging.RootLogger(logging.WARNING)
    monkeypatch.setattr(logging, "root", root)
    monkeypatch.setattr(logging.Logger, "root", root)
    monkeypatch.setattr(logging.Logger, "manager", logging.Manager(root))
    monkeypatch.setattr(logging, "_handlers", weakref.WeakValueDictionary())
    monkeypatch.setattr(logging, "_handlerList", [])
    yield
    logging.shutdown()


@pytest.mark.parametrize("access_log", [False, True])
def test_access_logging_preserves_operator_setting_when_setup_runs(access_log: bool) -> None:
    # Given the real Config constructor, which applies defaults and the operator flag.
    Config(app="unused:app", access_log=access_log)
    # When application import configures logging, including repeated setup.
    setup_json_logging("api")
    setup_json_logging("api")
    # Then the protocol's access-log decision still honors the operator setting.
    assert logging.getLogger("uvicorn.access").hasHandlers() is access_log
    assert len(logging.getLogger("uvicorn.access").filters) == 1


def test_uvicorn_defaults_flow_to_one_json_handler_when_setup_runs() -> None:
    # Given the real server defaults, including its non-propagating handlers.
    logging.config.dictConfig(LOGGING_CONFIG)
    levels = {name: logging.getLogger(name).level for name in UVICORN_LOGGERS}
    # When application import configures logging after Uvicorn.
    setup_json_logging("api")
    # Then only the root owns a handler; server log levels remain unchanged.
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    for name in UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        assert logger.handlers == [], name
        assert logger.propagate is True, name
        assert logger.level == levels[name], name
    assert len(logging.getLogger("uvicorn.access").filters) == 1


@pytest.mark.parametrize("service", ["api", "worker"])
def test_records_are_emitted_once_when_setup_is_repeated(service: str) -> None:
    # Given either server defaults or the worker's unconfigured logger graph.
    if service == "api":
        logging.config.dictConfig(LOGGING_CONFIG)
    stream = io.StringIO()
    setup_json_logging(service)
    # When startup repeats and each server logger emits a record.
    setup_json_logging(service)
    handler = logging.getLogger().handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(stream)
    for name in UVICORN_LOGGERS:
        logging.getLogger(name).warning("probe %s", name)
    # Then each record crosses the JSON boundary exactly once, with one filter.
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [record["logger"] for record in records] == list(UVICORN_LOGGERS)
    assert all(record["service"] == service for record in records)
    assert len(logging.getLogger().handlers) == 1
    assert len(logging.getLogger("uvicorn.access").filters) == 1


@pytest.mark.parametrize("target, expected", [
    ("/healthz?token=synthetic&api_key=other", "/healthz"),
    ("/healthz?ordinary=also-private", "/healthz"),
    ("/healthz", "/healthz"),
    ("/healthz%3Fpart?token=synthetic", "/healthz%3Fpart"),
])
def test_access_target_keeps_only_path_when_query_is_present(target: str, expected: str) -> None:
    # Given Uvicorn's five positional access arguments, shared with the caller.
    setup_json_logging("api")
    args = ("127.0.0.1:1234", "GET", target, "1.1", 200)
    record = logging.makeLogRecord({"args": args})
    # When the logger applies its filters.
    assert logging.getLogger("uvicorn.access").filter(record)
    # Then only the request target changed, without modifying the original tuple.
    assert record.args == (args[0], args[1], expected, args[3], args[4])
    assert args[2] == target


@pytest.mark.parametrize("args", [None, (), ("short",), (1, 2, 3, 4, 5), {"path": "/healthz?x=y"}])
def test_access_filter_accepts_unexpected_argument_shapes(args) -> None:
    # Given a non-Uvicorn call on the same logger.
    setup_json_logging("worker")
    record = logging.makeLogRecord({"args": args})
    # When filtering an unexpected shape.
    accepted = logging.getLogger("uvicorn.access").filter(record)
    # Then logging continues and the caller's data is untouched.
    assert accepted
    assert record.args is args


def test_worker_logging_redacts_when_uvicorn_was_never_configured() -> None:
    # Given worker startup with no Uvicorn configuration or imports required by setup.
    stream = io.StringIO()
    setup_json_logging("worker")
    handler = logging.getLogger().handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(stream)
    # When a worker task emits a synthetic provider credential.
    logging.getLogger("tasks.probe").error("provider token=%s", "sk-EXAMPLE1234567890abcdef")
    # Then the original worker boundary still emits one redacted JSON record.
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(records) == 1
    assert records[0]["service"] == "worker"
    assert records[0]["message"] == "provider token=<redacted>"
