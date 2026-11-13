"""Fail-closed health evidence recorded by Beat itself when it dispatches the heartbeat."""

import importlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        (json.dumps({"last_run_at": (NOW - timedelta(seconds=30)).isoformat()}), True),
        (json.dumps({"last_run_at": (NOW - timedelta(seconds=180)).isoformat()}), True),
        (json.dumps({"last_run_at": (NOW - timedelta(seconds=181)).isoformat()}), False),
        (None, False),
        ("not json", False),
        ("{}", False),
        (json.dumps({"last_run_at": (NOW + timedelta(seconds=1)).isoformat()}), False),
        ('{"last_run_at": null}', False),
        ('{"last_run_at": "2026-11-12T12:00:00"}', False),
    ],
    ids=["fresh", "bound", "stale", "missing", "corrupt", "empty", "future", "never-run", "naive"],
)
def test_health_when_receipt_state_varies(
    tmp_path: Path, contents: str | None, expected: bool,
) -> None:
    # Given an isolated receipt, or no receipt before the first scheduled execution.
    path = tmp_path / "heartbeat.json"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")
    from beat_health import is_healthy

    # When checking freshness against a fixed clock.
    result = is_healthy(path, NOW)

    # Then only a recent, timezone-aware execution timestamp is healthy.
    assert result is expected


def test_health_when_receipt_is_unreadable(tmp_path: Path) -> None:
    # Given a directory where the receipt file should be.
    from beat_health import is_healthy

    # When reading it, then fail closed even when running tests as root.
    assert is_healthy(tmp_path, NOW) is False


@pytest.mark.parametrize("fresh", [True, False], ids=["fresh", "missing"])
def test_cli_exit_when_receipt_is_fresh_or_missing(tmp_path: Path, fresh: bool) -> None:
    # Given the same receipt path used by the task and healthcheck.
    path = tmp_path / "heartbeat.json"
    if fresh:
        path.write_text(json.dumps({"last_run_at": datetime.now(timezone.utc).isoformat()}))

    # When invoking the real module entrypoint (no broker or containers).
    result = subprocess.run(
        [sys.executable, "-m", "beat_health"],
        env={**os.environ, "BEAT_HEARTBEAT_PATH": str(path)},
        text=True, capture_output=True, check=False,
    )

    # Then Docker gets an actionable signal and the corresponding exit status.
    assert result.stdout.strip() == ("healthy" if fresh else "unhealthy")
    assert result.returncode == (0 if fresh else 1)


@pytest.fixture
def beat_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Simulate `celery beat` starting in this process and undo it afterwards."""
    from celery.signals import after_task_publish, beat_init

    path = tmp_path / "heartbeat.json"
    monkeypatch.setenv("BEAT_HEARTBEAT_PATH", str(path))
    importlib.import_module("celery_app")  # registers the beat_init hook

    beat_init.send(sender=None)
    yield path
    after_task_publish.disconnect(dispatch_uid="beat-heartbeat-receipt")


def _publish_heartbeat() -> None:
    import celery_app

    # kombu's in-memory transport is the real publish path with no broker or container.
    with celery_app.celery_app.connection_for_write("memory://") as connection:
        celery_app.celery_app.send_task("beat_heartbeat", connection=connection, ignore_result=True)


def test_schedule_dispatches_a_short_lived_heartbeat() -> None:
    # Given the production schedule.
    import celery_app

    schedule = celery_app.celery_app.conf.beat_schedule["beat-heartbeat"]

    # Then it fires every minute, expires with the interval so a busy worker never accumulates
    # stale copies, and its task is registered for the worker to consume.
    assert schedule["schedule"] == 60.0
    assert schedule["options"]["expires"] == 60.0
    assert "tasks.beat_heartbeat" in celery_app.celery_app.conf.include


def test_a_successful_dispatch_is_recorded_by_the_beat_process(beat_process: Path) -> None:
    # Given a Beat process, and no receipt before the first dispatch.
    assert not beat_process.exists()
    before = datetime.now(timezone.utc)

    # When the heartbeat is published through Celery's real publish path.
    _publish_heartbeat()

    # Then the dispatch itself left a fresh receipt: no worker took part.
    receipt = json.loads(beat_process.read_text(encoding="utf-8"))
    assert before <= datetime.fromisoformat(receipt["last_run_at"]) <= datetime.now(timezone.utc)
    from beat_health import is_healthy

    assert is_healthy(beat_process, datetime.now(timezone.utc)) is True


def test_a_failed_publish_leaves_no_receipt(
    beat_process: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a broker that rejects the publish. Celery advances its own schedule timestamps
    # before publishing and only logs the failure, so those cannot prove dispatch.
    def rejected(*_args: object, **_kwargs: object) -> None:
        raise ConnectionError("broker refused")

    monkeypatch.setattr("kombu.messaging.Producer.publish", rejected)

    # When the heartbeat is dispatched.
    with pytest.raises(ConnectionError):
        _publish_heartbeat()

    # Then nothing was recorded, so the healthcheck stays unhealthy.
    assert not beat_process.exists()


def test_the_real_beat_scheduler_records_only_dispatches_the_broker_accepted(
    beat_process: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given Celery's own Beat scheduler applying the production schedule entry, as `celery beat`
    # does after its loader has imported the task modules.
    from celery.beat import Scheduler

    import celery_app

    app = celery_app.celery_app
    app.loader.import_default_modules()
    monkeypatch.setitem(app.conf, "broker_url", "memory://")
    scheduler = Scheduler(app=app, max_interval=1)
    entry = scheduler.schedule["beat-heartbeat"]

    # When the broker accepts the publish.
    scheduler.apply_entry(entry, producer=scheduler.producer)

    # Then a fresh receipt exists.
    assert beat_process.exists()
    beat_process.unlink()

    # When the broker rejects it: Beat only LOGS this and has already advanced the entry's
    # own timestamps, which is why those cannot prove dispatch.
    def rejected(*_args: object, **_kwargs: object) -> None:
        raise ConnectionError("broker refused")

    monkeypatch.setattr("kombu.messaging.Producer.publish", rejected)
    scheduler.apply_entry(entry, producer=scheduler.producer)

    # Then nothing is recorded.
    assert not beat_process.exists()


def test_only_the_heartbeat_dispatch_counts(beat_process: Path) -> None:
    # Given a Beat process dispatching a different periodic task.
    import celery_app

    # When another task is published.
    with celery_app.celery_app.connection_for_write("memory://") as connection:
        celery_app.celery_app.send_task(
            "sweep_expired_artifacts", connection=connection, ignore_result=True,
        )

    # Then it is not mistaken for the heartbeat.
    assert not beat_process.exists()


def test_processes_that_are_not_beat_record_nothing(tmp_path: Path) -> None:
    # Given a fresh API-or-worker-like process that imported the app but never started Beat.
    # (A fresh interpreter: in this one, earlier tests have connected and disconnected the hook,
    # which would hide a handler wired at import time.)
    root = Path(__file__).resolve().parents[3]
    path = tmp_path / "heartbeat.json"
    script = (
        "import celery_app\n"
        "with celery_app.celery_app.connection_for_write('memory://') as connection:\n"
        "    celery_app.celery_app.send_task('beat_heartbeat', connection=connection, ignore_result=True)\n"
    )
    python_path = os.pathsep.join(
        str(root / part) for part in (
            "apps/worker-orchestrator", "packages/creator-domain",
            "packages/creator-service", "packages/creator-provider",
        )
    )

    # When it publishes the heartbeat task.
    subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "BEAT_HEARTBEAT_PATH": str(path), "PYTHONPATH": python_path},
        check=True, capture_output=True, text=True, timeout=120,
    )

    # Then no receipt appears: only the Beat process vouches for its own liveness.
    assert not path.exists()


def test_executing_the_heartbeat_task_neither_needs_nor_writes_a_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Given a worker executing the periodic message (the local pool is solo, so a long task
    # would otherwise make Beat look dead).
    import celery_app

    path = tmp_path / "heartbeat.json"
    monkeypatch.setenv("BEAT_HEARTBEAT_PATH", str(path))
    celery_app.celery_app.loader.import_task_module("tasks.beat_heartbeat")

    # When the task runs.
    celery_app.celery_app.tasks["beat_heartbeat"].apply(throw=True).get()

    # Then health does not depend on it.
    assert not path.exists()


def test_heartbeat_when_ci_checks_registered_tasks() -> None:
    # Given the existing CI-invoked registration smoke, not a new heavyweight job.
    root = Path(__file__).resolve().parents[3]
    script = (root / "scripts/cutover_runtime_acceptance.sh").read_text()

    # When selecting tasks for inspect registered, then the heartbeat is covered too.
    tasks = re.search(r"for task in ([^;]+); do", script)
    assert tasks is not None
    assert "beat_heartbeat" in tasks[1].split()
