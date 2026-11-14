"""A failing hard-timeout finalization must neither skip nor escape the task's cleanup (#1071)."""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import anyio
import pytest
from tasks import generate_scene_image, hard_timeout

HOSTNAME = "celery@custom-node"
WORKER_PID = 42


@dataclass(frozen=True, slots=True)
class _Asset:
    asset_path: str


class _NoPersistedAssets:
    async def list_by_run(self, _run_id: int) -> dict[str, list[_Asset]]:
        return {}


def _run(coroutine):
    async def await_coroutine():
        return await coroutine

    return anyio.run(await_coroutine)


def _request(task: object, run_id: int = 1) -> hard_timeout.ProductionRequest:
    request = object.__new__(hard_timeout.ProductionRequest)
    request.id = "timed-out-delivery"
    request._args = (run_id,)
    request._kwargs = {}
    request.worker_pid = WORKER_PID
    request._hostname = HOSTNAME
    request._task = task
    return request


@pytest.fixture(autouse=True)
def _prefork_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hard_timeout.Request, "on_timeout", lambda *_args: None)
    monkeypatch.setattr(hard_timeout, "gethostname", lambda: HOSTNAME)


def _failing_finalize(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    finalize = MagicMock(side_effect=RuntimeError("database unavailable"))
    monkeypatch.setattr(hard_timeout, "finalize_hard_timeout", finalize)
    return finalize


def test_render_scratch_is_removed_when_finalization_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a killed render delivery's scratch beside a sibling delivery's, and a database outage.
    task = importlib.import_module("tasks.render_video")
    root = tmp_path / "artifacts"
    render = root / "1/render"
    owned = render / f".render_tmp_{WORKER_PID}_deadbeef"
    sibling = render / f".render_tmp_{WORKER_PID + 1}_deadbeef"
    for path in (owned, sibling):
        path.mkdir(parents=True)
        (path / "payload").write_bytes(b"scratch")
    monkeypatch.setattr(task, "_ARTIFACT_ROOT", str(root))
    monkeypatch.setenv("ARTIFACT_ROOT", str(root))
    monkeypatch.delenv("RENDER_SCRATCH_DIR", raising=False)
    finalize = _failing_finalize(monkeypatch)
    # When the prefork parent handles the hard timeout, then nothing propagates out of the handler.
    _request(task.render_video).on_timeout(False, 2.0)
    # Then finalization was attempted, the killed delivery's scratch is gone and the sibling's is kept.
    finalize.assert_called_once()
    assert not owned.exists()
    assert (sibling / "payload").read_bytes() == b"scratch"


def test_scene_image_output_is_removed_when_finalization_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an unfinished scene image of the timed-out delivery and a database outage.
    run_id = 1
    owner = hard_timeout.delivery_owner("timed-out-delivery", HOSTNAME, WORKER_PID)
    scenes = tmp_path / str(run_id) / "scenes"
    scenes.mkdir(parents=True)
    unfinished = scenes / f"scene-1-{owner}-unfinished.png"
    unfinished.touch()
    monkeypatch.setattr(generate_scene_image, "_ARTIFACTS_BASE", str(tmp_path))
    monkeypatch.setattr(generate_scene_image, "_visual_asset_service", _NoPersistedAssets())
    monkeypatch.setattr("worker_loop.run_in_worker_loop", _run)
    finalize = _failing_finalize(monkeypatch)
    # When the prefork parent handles the hard timeout, then nothing propagates out of the handler.
    _request(generate_scene_image.generate_scene_image, run_id).on_timeout(False, 2.0)
    # Then finalization was attempted and the unfinished output is removed.
    finalize.assert_called_once()
    assert not unfinished.exists()


def test_finalization_failure_is_logged_once_with_its_traceback(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a task whose finalization fails and whose cleanup is observable.
    cleanup = MagicMock()
    task = MagicMock(hard_timeout_safe_failure_stages=frozenset({"RENDER_GENERATING"}), hard_timeout_cleanup=cleanup)
    _failing_finalize(monkeypatch)
    # When the prefork parent handles the hard timeout.
    with caplog.at_level(logging.WARNING, logger=hard_timeout.__name__):
        _request(task).on_timeout(False, 2.0)
    # Then the failure is reported once as an error with its traceback and the cleanup still ran.
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert errors[0].exc_info[0] is RuntimeError
    cleanup.assert_called_once_with(1, "timed-out-delivery", HOSTNAME, WORKER_PID)


def test_cleanup_failure_is_logged_and_never_propagates(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a task whose cleanup raises after a successful finalization.
    finalize = MagicMock()
    monkeypatch.setattr(hard_timeout, "finalize_hard_timeout", finalize)
    task = MagicMock(
        hard_timeout_safe_failure_stages=frozenset({"RENDER_GENERATING"}),
        hard_timeout_cleanup=MagicMock(side_effect=OSError("disk gone")),
    )
    # When the prefork parent handles the hard timeout, then nothing propagates out of the handler.
    with caplog.at_level(logging.WARNING, logger=hard_timeout.__name__):
        _request(task).on_timeout(False, 2.0)
    # Then the delivery was finalized and the cleanup failure is reported once with its traceback.
    finalize.assert_called_once()
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].exc_info is not None
    assert warnings[0].exc_info[0] is OSError


def test_cleanup_still_follows_a_successful_finalization(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a task whose finalization succeeds.
    events: list[str] = []
    monkeypatch.setattr(hard_timeout, "finalize_hard_timeout", lambda *_args: events.append("finalized"))

    def cleanup(*_args: object) -> None:
        events.append("cleaned")

    task = MagicMock(hard_timeout_safe_failure_stages=frozenset({"RENDER_GENERATING"}), hard_timeout_cleanup=cleanup)
    # When the prefork parent handles the hard timeout.
    _request(task).on_timeout(False, 2.0)
    # Then the durable failure is recorded before the cleanup can unblock anything.
    assert events == ["finalized", "cleaned"]


def test_keyboard_interrupt_is_not_swallowed_by_the_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a finalization interrupted by the operator.
    monkeypatch.setattr(hard_timeout, "finalize_hard_timeout", MagicMock(side_effect=KeyboardInterrupt))
    task = MagicMock(hard_timeout_safe_failure_stages=frozenset({"RENDER_GENERATING"}))
    # When the prefork parent handles the hard timeout, then the interrupt still escapes the handler.
    with pytest.raises(KeyboardInterrupt):
        _request(task).on_timeout(False, 2.0)
