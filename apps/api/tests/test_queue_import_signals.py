"""Queue dispatch must leave process signal ownership with the API server."""

import os
from pathlib import Path
import subprocess
import sys
from textwrap import dedent

import pytest


@pytest.mark.parametrize("worker_loop_available", [True, False], ids=["worker-loop-present", "worker-loop-absent"])
def test_queue_dispatch_preserves_signals_when_worker_layout_varies(worker_loop_available: bool) -> None:
    # Given an isolated interpreter, independent of pytest's cached application imports.
    root = Path(__file__).resolve().parents[3]
    scenario = dedent("""
        import signal
        import sys
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch

        root = Path(sys.argv[1])
        worker_loop_available = sys.argv[2] == "True"
        sys.path[:0] = [str(root / "apps/api/src"), str(root / "apps/worker-orchestrator")]
        assert all(name not in sys.modules for name in (
            "celery_app", "tasks.task_runner", "tasks.render_video", "shorts_api.app_factory",
        ))
        if not worker_loop_available:
            # Same absence seam as test_task_dispatch_adapter: no cached worker module.
            sys.modules["worker_loop"] = None
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

        # When the API imports its real queue modules and dispatches a queued task.
        with patch("signal.signal", wraps=signal.signal) as register:
            from shorts_api.app_factory import create_app
            from shorts_api.task_dispatch_adapter import ApplicationTaskDispatcher
            from creator_domain.task_dispatch import TaskSubmission
            import celery_app
            import tasks.task_runner
            from tasks.render_video import render_video

            create_app()  # Configure dispatch, but do not enter the API lifespan.
            assert (celery_app._worker_loop_module is not None) == worker_loop_available
            adapter = ApplicationTaskDispatcher()
            assert adapter.uses_queue()
            # Stub only the broker boundary; keep the adapter's real task import seam.
            with patch.object(render_video, "apply_async", return_value=SimpleNamespace(id="queue-id")) as publish:
                result = adapter.dispatch(TaskSubmission(
                    "render_video", 8, (8,), {"render_profile": "vertical"}, "queue-id",
                ))
            assert result == "queue-id"
            publish.assert_called_once_with(
                args=[8], kwargs={"render_profile": "vertical"},
                headers=adapter._get_trace_headers(), task_id="queue-id",
            )

            # Then neither imports nor queued publication register any OS handler.
            register.assert_not_called()
            assert all(signal.getsignal(sig) is handler for sig, handler in handlers.items())
    """)
    environment = {
        **os.environ,
        "ENVIRONMENT": "test",
        "REDIS_URL": "redis://127.0.0.1:1/0",
        "OTEL_ENABLED": "false",
    }

    # When the fresh-process scenario runs (no broker, Docker or provider required).
    result = subprocess.run(
        [sys.executable, "-I", "-c", scenario, str(root), str(worker_loop_available)],
        env=environment, capture_output=True, text=True, timeout=30, check=False,
    )

    # Then expose the child assertion/traceback if the signal contract is violated.
    assert result.returncode == 0, result.stdout + result.stderr
