from pathlib import Path

import pytest
from creator_service.storage_policy import FilesystemLowSpace
from tasks import task_runner
from worker_loop import run_in_worker_loop

from .terminal_validation_support import RunnerCase, runner_case as runner_case  # noqa: F401
from .terminal_postgres_support import terminal_pool as terminal_pool  # noqa: F401
from .test_job_free_space_admission import Volumes, _executing, _guarded, volumes as volumes, watermarks as watermarks  # noqa: F401


def test_configured_render_volume_is_measured_before_execution(
    runner_case: RunnerCase, volumes: Volumes, watermarks: None,
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given healthy system temp but a configured render volume below the scratch floor.
    scratch = tmp_path_factory.mktemp("render-scratch").resolve()
    volumes.free[scratch] = 1
    monkeypatch.setenv("RENDER_SCRATCH_DIR", str(scratch))
    calls: list[str] = []
    # When a media delivery asks to start.
    with pytest.raises(FilesystemLowSpace) as raised:
        task_runner.run_task(runner_case.delivery(), runner_case.run_id, _guarded(runner_case.config), _executing(calls))
    # Then the configured filesystem refuses before execution or scratch creation.
    assert raised.value.filesystem == "scratch"
    assert calls == []
    assert list(scratch.iterdir()) == []
    tracked = run_in_worker_loop(runner_case.tracking.list_run_tasks(runner_case.run_id))[0]
    assert tracked.error_code == "SCRATCH_STORAGE_LOW"
