"""Exercise real Celery trace logging with chained synthetic credentials."""

import io
import json
import logging
from pathlib import Path

import celery_app as worker
import pytest
from creator_service.logging_config import JsonFormatter


@pytest.mark.parametrize("configured_propagates", [False, True])
def test_eager_trace_redacts_chained_provider_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, configured_propagates: bool,
) -> None:
    # Given: actual app/trace machinery, offline DLQ, isolated logger state.
    monkeypatch.setattr(worker, "redis", None)
    monkeypatch.setattr(worker, "dlq_fallback_path", str(tmp_path / "dlq.jsonl"))
    monkeypatch.setitem(worker.celery_app.conf, "task_eager_propagates", configured_propagates)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("worker"))
    logger = logging.getLogger("celery.app.trace")
    monkeypatch.setattr(logger, "handlers", [handler])
    monkeypatch.setattr(logger, "level", logging.ERROR)
    monkeypatch.setattr(logger, "propagate", False)

    @worker.celery_app.task(name=f"test.chained_provider_failure.{configured_propagates}", ignore_result=True)
    def chained_provider_failure() -> None:
        raise RuntimeError("outer") from ValueError(
            "token=sk-EXAMPLEEXAMPLE https://user:pass@host.example/x?api_key=SECRETVALUE12345"
        )

    try:
        # When: throw=False preserves the result even if eager propagation is configured.
        result = chained_provider_failure.apply(throw=False)
        # Then: credentials are absent from EVERY raw JSON line, not just exc_info.
        lines = stream.getvalue().splitlines()
        assert lines
        payloads = [json.loads(line) for line in lines]
        assert result.state == "FAILURE"
        assert isinstance(result.result, RuntimeError) and str(result.result) == "outer"
        assert isinstance(result.result.__cause__, ValueError)
        for line in lines:
            for secret in ("sk-EXAMPLEEXAMPLE", "user:pass", "SECRETVALUE12345"):
                assert secret not in line
        trace = "\n".join(payload["exc_info"] for payload in payloads)
        assert "RuntimeError" in trace and "ValueError" in trace
        assert "chained_provider_failure" in trace
    finally:
        worker.celery_app.tasks.pop(chained_provider_failure.name, None)
