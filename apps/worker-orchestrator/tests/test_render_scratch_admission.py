from pathlib import Path
from dataclasses import replace

import pytest
from creator_service.storage_policy import FilesystemLowSpace
from creator_service.render_scratch import RenderScratchConfigurationError
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


@pytest.mark.parametrize("low_temp", [True, False])
def test_configured_volume_does_not_replace_system_temp_floor(
    runner_case: RunnerCase, volumes: Volumes, watermarks: None,
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch, low_temp: bool,
) -> None:
    # Given healthy render scratch but a separately sized remote-upload spool volume.
    scratch = tmp_path_factory.mktemp("render-scratch").resolve()
    volumes.free[scratch] = 9000 * 1024 * 1024
    if low_temp:
        volumes.set_free(scratch=1)
    monkeypatch.setenv("RENDER_SCRATCH_DIR", str(scratch))
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    calls: list[str] = []
    # When admission runs, then either filesystem can refuse before execution.
    if low_temp:
        with pytest.raises(FilesystemLowSpace) as raised:
            task_runner.run_task(runner_case.delivery(), runner_case.run_id, _guarded(runner_case.config), _executing(calls))
        assert raised.value.filesystem == "scratch"
        assert calls == []
        tracked = run_in_worker_loop(runner_case.tracking.list_run_tasks(runner_case.run_id))[0]
        assert tracked.error_code == "SCRATCH_STORAGE_LOW"
    else:
        task_runner.run_task(runner_case.delivery(), runner_case.run_id, _guarded(runner_case.config), _executing(calls))
        assert calls == ["validation-task"]
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("task_name", ["generate_scene_image", "generate_paragraph_audio"])
def test_non_scratch_jobs_ignore_invalid_render_setting(
    runner_case: RunnerCase, volumes: Volumes, watermarks: None,
    monkeypatch: pytest.MonkeyPatch, task_name: str,
) -> None:
    # Given jobs that never call the helper and an invalid render-only setting.
    monkeypatch.setenv("RENDER_SCRATCH_DIR", "relative")
    config = replace(_guarded(runner_case.config), task_name=task_name)
    calls: list[str] = []
    # When admitted, then healthy image/paragraph jobs can still execute.
    task_runner.run_task(runner_case.delivery(), runner_case.run_id, config, _executing(calls))
    assert calls == ["validation-task"]


@pytest.mark.parametrize("task_name", ["render_video", "generate_audio"])
def test_scratch_jobs_reject_invalid_setting_before_execution(
    runner_case: RunnerCase, volumes: Volumes, watermarks: None,
    monkeypatch: pytest.MonkeyPatch, task_name: str,
) -> None:
    # Given an invalid setting for a scratch-using media job.
    monkeypatch.setenv("RENDER_SCRATCH_DIR", "relative")
    config = replace(_guarded(runner_case.config), task_name=task_name, uses_render_scratch=True)
    calls: list[str] = []
    # When admission runs, then execution (including paid provider work) never starts.
    with pytest.raises(RenderScratchConfigurationError):
        task_runner.run_task(runner_case.delivery(), runner_case.run_id, config, _executing(calls))
    assert calls == []
