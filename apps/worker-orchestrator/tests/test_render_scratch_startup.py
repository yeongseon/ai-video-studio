import importlib
import logging
from pathlib import Path

import pytest
from celery.signals import worker_ready


def test_ready_sweeps_once_through_signal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given an old legacy entry and a published sibling.
    import celery_app
    path = tmp_path / "1/render/.render_tmp_42_deadbeef"
    path.mkdir(parents=True)
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    module = importlib.import_module("creator_service.render_scratch_sweeper")
    original = module.sweep_render_scratch
    calls = []

    def observe(root, policy):
        calls.append(policy)
        return original(root, policy, module.SweepRuntime(now=lambda: path.stat().st_ctime + 7200))

    monkeypatch.setattr(module, "sweep_render_scratch", observe)
    # When Celery emits its main-process ready signal.
    responses = worker_ready.send(sender=None)
    # Then exactly one receiver invokes the sweep and removes stale scratch.
    assert len(calls) == 1
    assert not path.exists()
    assert (celery_app.cleanup_render_scratch_at_start, None) in responses


def test_ready_contains_failures(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    # Given an unexpected sweep failure.
    import celery_app
    module = importlib.import_module("creator_service.render_scratch_sweeper")

    def broken(*args):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(module, "sweep_render_scratch", broken)
    # When Celery emits ready; then the receiver succeeds and logs the failed maintenance.
    responses = worker_ready.send(sender=None)
    assert (celery_app.cleanup_render_scratch_at_start, None) in responses
    assert any(r.levelno == logging.WARNING and "sweep failed" in r.message for r in caplog.records)


def test_ready_respects_disabled_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given startup reclamation disabled and a sweep that must not run.
    import celery_app
    module = importlib.import_module("creator_service.render_scratch_sweeper")
    monkeypatch.setenv("RENDER_SCRATCH_SWEEP_ENABLED", "false")

    def forbidden(*args):
        raise AssertionError("disabled sweep ran")

    monkeypatch.setattr(module, "sweep_render_scratch", forbidden)
    # When the signal runs; then the callback returns normally without sweeping.
    assert (celery_app.cleanup_render_scratch_at_start, None) in worker_ready.send(sender=None)


def test_default_age_covers_real_task_hard_limit() -> None:
    # Given real registered task limits; when choosing defaults; then the age floor fits.
    from creator_service.render_scratch_sweeper import SweepPolicy
    from tasks.render_video import render_video
    assert SweepPolicy().max_age_seconds >= 2 * render_video.time_limit
