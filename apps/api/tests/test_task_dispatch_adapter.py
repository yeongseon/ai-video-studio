from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

from celery import Celery
from creator_domain.task_dispatch import TaskSubmission
from pydantic import JsonValue
import pytest

from shorts_api.task_dispatch_adapter import ApplicationTaskDispatcher
from tests.worker_image_support import load_image_sibling


def test_celery_submission_preserves_id_arguments_and_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = ApplicationTaskDispatcher()
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:56389/0")
    with patch.object(adapter, "_load_task") as load, patch.object(adapter, "_get_trace_headers", return_value={"traceparent": "trace"}):
        load.return_value.apply_async.return_value.id = "fixed-id"
        result = adapter.dispatch(TaskSubmission("render_video", 8, (8,), {"render_profile": "vertical"}, "fixed-id"))
        load.return_value.apply_async.assert_called_once_with(
            args=[8], kwargs={"render_profile": "vertical"}, headers={"traceparent": "trace"}, task_id="fixed-id",
        )
    assert result == "fixed-id"


def test_cancel_terminates_the_exact_task(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given the real module copied into API Docker, without worker_loop or a worker path.
    source = Path(__file__).resolve().parents[2] / "worker-orchestrator" / "celery_app.py"
    spec = spec_from_file_location("celery_app", source)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "celery_app", module)
    monkeypatch.setitem(sys.modules, "worker_loop", None)
    load_image_sibling(monkeypatch, "beat_health")
    spec.loader.exec_module(module)

    # When cancellation reaches Celery's control boundary.
    with patch("celery_app.celery_app.control.revoke") as revoke:
        ApplicationTaskDispatcher().cancel("queued-id")
    # Then only the requested task is terminated.
    revoke.assert_called_once_with("queued-id", terminate=True)


def test_sync_runner_preserves_synthetic_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    observed: list[tuple[str, int, int, tuple[JsonValue, ...], dict[str, JsonValue]]] = []

    class Task:
        def run(self, context: SimpleNamespace, *args: JsonValue, **kwargs: JsonValue) -> None:
            observed.append((context.request.id, context.request.retries, context.max_retries, args, kwargs))

    adapter = ApplicationTaskDispatcher()
    with patch.object(adapter, "_load_task", return_value=Task()):
        result = adapter.dispatch(TaskSubmission("render_video", 8, (8,), {"render_profile": "vertical"}))
    assert observed == [(result, 0, 0, (8,), {"render_profile": "vertical"})]


def test_lightweight_runner_executes_real_bound_celery_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    app = Celery("dispatch-contract", broker="memory://", backend="cache+memory://")
    observed: list[tuple[str, int, str]] = []

    @app.task(bind=True, autoretry_for=(ConnectionError,))
    def render(self, run_id: int, render_profile: str) -> None:
        observed.append((self.request.id, run_id, render_profile))

    adapter = ApplicationTaskDispatcher()
    with patch.object(adapter, "_load_task", return_value=render):
        result = adapter.dispatch(TaskSubmission("render_video", 8, (8,), {"render_profile": "vertical"}))
    assert observed == [(result, 8, "vertical")]


def test_lightweight_runner_uses_preregistered_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    observed: list[str] = []

    class Task:
        def run(self, context: SimpleNamespace, *args: JsonValue, **kwargs: JsonValue) -> None:
            observed.append(context.request.id)

    adapter = ApplicationTaskDispatcher()
    with patch.object(adapter, "_load_task", return_value=Task()):
        result = adapter.dispatch(TaskSubmission("generate_paragraph_audio", 8, task_id="pending-id"))
    assert observed == ["pending-id"]
    assert result == "pending-id"
