import importlib
import importlib.util
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.worker_image_support import load_image_sibling


def _load_api_module(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    sys.modules.pop("shorts_api.main", None)
    return importlib.import_module("shorts_api.main")


def _load_worker_module(monkeypatch: pytest.MonkeyPatch, module_name: str):
    worker_file = Path(__file__).resolve().parents[2] / "worker-orchestrator" / "celery_app.py"
    spec = importlib.util.spec_from_file_location(module_name, worker_file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setenv("ENVIRONMENT", "development")
    sys.modules.pop(module_name, None)
    sys.modules[module_name] = module
    load_image_sibling(monkeypatch, "beat_health")
    spec.loader.exec_module(module)
    return module


def test_worker_does_not_call_setrlimit(monkeypatch: pytest.MonkeyPatch):
    """RLIMIT_AS was replaced by cgroup memory limits (#611).

    The worker's _apply_resource_limits() must NOT call setrlimit, because
    RLIMIT_AS causes MemoryError on CUDA/torch/ffmpeg which reserve large
    virtual address ranges.
    """
    setrlimit_mock = Mock()
    monkeypatch.setenv("MAX_MEMORY_MB", "2048")

    import resource

    monkeypatch.setattr(resource, "setrlimit", setrlimit_mock)
    module = _load_worker_module(monkeypatch, "worker_orchestrator_celery_app_test")

    module._apply_resource_limits()

    # setrlimit must NOT be called — memory is enforced via cgroup.
    setrlimit_mock.assert_not_called()


@pytest.mark.asyncio
async def test_cpu_monitor_triggers_shutdown_flag(monkeypatch: pytest.MonkeyPatch):
    from shorts_api import lifecycle
    lifecycle.shutdown_state.is_shutting_down = False
    kill_mock = Mock(side_effect=SystemExit(1))

    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(lifecycle.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        lifecycle,
        "_cpu_usage_percent",
        lambda _cpu_seconds, _wall_seconds: (float(lifecycle.MAX_CPU_PERCENT) + 1.0, 0.0, 0.0),
    )
    monkeypatch.setattr(lifecycle.os, "kill", kill_mock)

    with pytest.raises(SystemExit, match="1"):
        await lifecycle._monitor_cpu_limit()

    assert lifecycle.shutdown_state.is_shutting_down is True
    # Reset to avoid polluting other tests
    lifecycle.shutdown_state.is_shutting_down = False


@pytest.mark.asyncio
async def test_api_startup_does_not_call_setrlimit(monkeypatch: pytest.MonkeyPatch):
    """RLIMIT_AS was replaced by cgroup memory limits (#611)."""
    from shorts_api import lifecycle

    setrlimit_mock = Mock()
    monkeypatch.setattr(lifecycle.resource, "setrlimit", setrlimit_mock)

    from shorts_api.main import app
    async with lifecycle.lifespan(app):
        pass

    setrlimit_mock.assert_not_called()
