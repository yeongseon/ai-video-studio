from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator, NoReturn

import pytest
import yaml


ROOT = Path(__file__).parents[3]
WORKER = ROOT / "apps" / "worker-orchestrator"
PYTHONPATH = ":".join((
    str(WORKER), str(ROOT / "apps/api/src"), str(ROOT / "packages/creator-domain"),
    str(ROOT / "packages/creator-service"), str(ROOT / "packages/creator-provider"),
))


def _command(compose: str, service: str) -> list[str]:
    document = yaml.safe_load((ROOT / compose).read_text(encoding="utf-8"))
    return document["services"][service]["command"]


def _wait_for(path: Path, status: str, timeout: float = 15) -> dict[str, str | int | bool]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            state = json.loads(path.read_text(encoding="utf-8"))
            if state.get("status") == status:
                return state
        time.sleep(0.05)
    pytest.fail(f"timed out waiting for {status}: {path.read_text() if path.exists() else 'missing'}")


def _worker_env(broker_url: str, broker_dir: Path) -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": PYTHONPATH,
        "REDIS_URL": broker_url,
        "WORKER_TOPOLOGY_BROKER_DIR": str(broker_dir),
        "ENVIRONMENT": "test",
    }


def _interpreter_command(command: list[str]) -> list[str]:
    """The shipped Celery command, run with the test interpreter instead of whichever ``celery`` PATH finds first."""
    program, *arguments = command
    assert program == "celery", f"expected a celery command, got {command}"
    return [sys.executable, "-m", program, *arguments]


@contextmanager
def _worker(command: list[str], env: dict[str, str]) -> Iterator[subprocess.Popen[str]]:
    process_command = [*_interpreter_command(command), "--include", "tests.worker_topology_tasks"]
    process = subprocess.Popen(
        process_command, cwd=WORKER, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True,
    )
    try:
        time.sleep(2)
        assert process.poll() is None, process.stdout.read() if process.stdout else "worker exited"
        yield process
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def _send(env: dict[str, str], task: str, args: list[str], queue: str = "creator", **options: int) -> str:
    script = (
        "from celery_app import celery_app; "
        "celery_app.loader.import_task_module('tests.worker_topology_tasks'); "
        f"r=celery_app.send_task({task!r}, args={args!r}, queue={queue!r}, **{options!r}); "
        "print(r.id)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], cwd=WORKER, env=env, check=True,
        capture_output=True, text=True, timeout=10,
    )
    return completed.stdout.strip()


class _Provider(BaseHTTPRequestHandler):
    calls = 0

    def do_GET(self) -> None:
        type(self).calls += 1
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *_args: str) -> None:  # noqa: A002
        return


@contextmanager
def _provider() -> Iterator[str]:
    _Provider.calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)


@pytest.fixture
def filesystem_broker(tmp_path: Path) -> tuple[str, Path]:
    broker = tmp_path / "broker"
    for name in ("queue", "control"):
        (broker / name).mkdir(parents=True)
    return "filesystem://", broker


def test_solo_worker_cooperatively_cancels_and_cleans_delivery(
    filesystem_broker: tuple[str, Path], tmp_path: Path,
) -> None:
    # Given the exact default Compose worker running a cross-process filesystem broker.
    broker_url, broker_dir = filesystem_broker
    env = _worker_env(broker_url, broker_dir)
    state, work = tmp_path / "state.json", tmp_path / "work"
    with _provider() as provider_url, _worker(_command("docker-compose.yml", "worker"), env):
        _send(env, "worker_topology.cooperative", [str(state), provider_url, str(work)])
        running = _wait_for(state, "provider_done")

        # When the API-owned state changes to cancelled while work is active.
        state.write_text(json.dumps({**running, "cancel_requested": True}), encoding="utf-8")

        # Then no later provider call occurs and owned process/files/quota are cleaned.
        final = _wait_for(state, "cancelled")
        time.sleep(0.2)
        assert _Provider.calls == 1
        assert not Path(f"/proc/{final['child_pid']}").exists()
        assert list(work.iterdir()) == []


def test_solo_worker_sigterm_finishes_active_cooperative_task(
    filesystem_broker: tuple[str, Path], tmp_path: Path,
) -> None:
    # Given an active task in the exact default solo worker.
    broker_url, broker_dir = filesystem_broker
    env = _worker_env(broker_url, broker_dir)
    state, work = tmp_path / "state.json", tmp_path / "work"
    with _provider() as provider_url, _worker(_command("docker-compose.yml", "worker"), env) as worker:
        _send(env, "worker_topology.cooperative", [str(state), provider_url, str(work)])
        running = _wait_for(state, "provider_done")

        # When SIGTERM requests Celery warm shutdown.
        worker.send_signal(signal.SIGTERM)

        # Then warm shutdown leaves active work running until cooperative cancellation.
        time.sleep(0.2)
        assert json.loads(state.read_text(encoding="utf-8"))["status"] == "provider_done"
        state.write_text(json.dumps({**running, "cancel_requested": True}), encoding="utf-8")
        final = _wait_for(state, "cancelled")
        worker.wait(timeout=10)
        assert not Path(f"/proc/{running['child_pid']}").exists()
        assert final["status"] == "cancelled"
        assert list(work.iterdir()) == []


def test_prefork_scaled_worker_enforces_soft_timeout_and_cleans_delivery(
    filesystem_broker: tuple[str, Path], tmp_path: Path,
) -> None:
    # Given the exact shipped prefork image worker and a synthetic long delivery.
    broker_url, broker_dir = filesystem_broker
    env = _worker_env(broker_url, broker_dir)
    state, work = tmp_path / "state.json", tmp_path / "work"
    command = _command("docker-compose.scaled-workers.yml", "worker-image")
    with _worker(command, env):
        _send(
            env, "worker_topology.deadline", [str(state), str(work)], queue="image",
            soft_time_limit=1, time_limit=3,
        )
        running = _wait_for(state, "running")

        # When the prefork pool applies Celery's soft deadline.
        final = _wait_for(state, "failed_timeout")

        # Then timeout state is terminal and delivery-owned resources are gone.
        assert final["status"] == "failed_timeout"
        assert not Path(f"/proc/{running['child_pid']}").exists()
        assert list(work.iterdir()) == []


def test_recreated_worker_processes_next_attempt_and_fences_late_duplicate(
    filesystem_broker: tuple[str, Path], tmp_path: Path,
) -> None:
    # Given a completed worker lifecycle and a successor attempt owned by a new worker.
    broker_url, broker_dir = filesystem_broker
    env = _worker_env(broker_url, broker_dir)
    command = _command("docker-compose.yml", "worker")
    state = tmp_path / "attempt.json"
    state.write_text(json.dumps({"active_attempt": "first", "status": "pending"}), encoding="utf-8")
    with _worker(command, env):
        _send(env, "worker_topology.attempt", [str(state), "first"])
        _wait_for(state, "success")
    state.write_text(json.dumps({"active_attempt": "successor", "status": "pending"}), encoding="utf-8")

    # When the recreated worker receives the successor followed by a late duplicate.
    with _worker(command, env):
        _send(env, "worker_topology.attempt", [str(state), "successor"])
        _wait_for(state, "success")
        _send(env, "worker_topology.attempt", [str(state), "first"])
        time.sleep(0.5)

    # Then the next task completes and the stale attempt cannot overwrite ownership.
    assert json.loads(state.read_text(encoding="utf-8")) == {
        "active_attempt": "successor", "status": "success",
    }


@pytest.mark.skipif(shutil.which("redis-server") is None, reason="redis-server binary unavailable")
def test_default_worker_starts_against_redis_using_shipped_command(tmp_path: Path) -> None:
    # Given an ephemeral local Redis broker when the binary is available.
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    redis = subprocess.Popen([
        "redis-server", "--bind", "127.0.0.1", "--port", str(port), "--save", "",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        env = _worker_env(f"redis://127.0.0.1:{port}/0", tmp_path)

        # When the exact default worker command starts.
        with _worker(_command("docker-compose.yml", "worker"), env) as worker:
            # Then it remains ready under the Redis transport.
            assert worker.poll() is None
    finally:
        redis.terminate()
        redis.wait(timeout=5)


def test_worker_ignores_another_celery_first_on_path(
    filesystem_broker: tuple[str, Path], tmp_path: Path,
) -> None:
    # Given another interpreter's celery installation first on PATH, here a stand-in that records its use.
    broker_url, broker_dir = filesystem_broker
    decoy = tmp_path / "bin" / "celery"
    decoy.parent.mkdir()
    decoy.write_text('#!/bin/sh\necho decoy celery ran >&2\ntouch "$(dirname "$0")/decoy-used"\nexit 3\n', encoding="utf-8")
    decoy.chmod(0o755)
    env = {**_worker_env(broker_url, broker_dir), "PATH": f"{decoy.parent}{os.pathsep}{os.environ['PATH']}"}

    # When the exact default worker command is started for the tests.
    with _worker(_command("docker-compose.yml", "worker"), env) as worker:
        assert worker.poll() is None

    # Then the test interpreter's Celery ran and the stand-in never did.
    assert not (decoy.parent / "decoy-used").exists()


class _Launched(Exception):
    pass


@pytest.mark.parametrize(("compose", "service"), [
    ("docker-compose.yml", "worker"),
    ("docker-compose.scaled-workers.yml", "worker-image"),
], ids=["solo", "prefork-image"])
def test_worker_command_is_the_shipped_one_run_with_the_test_interpreter(
    compose: str, service: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a shipped Compose worker command and a launcher that records what it is asked to start.
    shipped = _command(compose, service)
    launched: list[list[str]] = []

    def record(command: list[str], **_options: object) -> NoReturn:
        launched.append(command)
        raise _Launched

    monkeypatch.setattr(subprocess, "Popen", record)

    # When the helper starts the worker.
    with pytest.raises(_Launched), _worker(shipped, {}):
        pass

    # Then only the interpreter differs: Celery gets exactly the shipped arguments and the test tasks module.
    assert launched == [[sys.executable, "-m", "celery", *shipped[1:], "--include", "tests.worker_topology_tasks"]]


def test_a_shipped_command_that_is_not_celery_is_refused() -> None:
    # Given a worker command that no longer starts with celery, for example a shell wrapper.
    command = ["sh", "-c", "celery -A celery_app.celery_app worker"]
    # When the helper adapts it for the tests, then it fails loudly instead of running something else.
    with pytest.raises(AssertionError, match="expected a celery command"):
        _interpreter_command(command)


def test_synthetic_terminal_state_is_published_after_cleanup() -> None:
    # Given the legacy synthetic helper retained for fast local signal checks.
    source = (WORKER / "tests" / "worker_topology_tasks.py").read_text(encoding="utf-8")

    # When cancellation and timeout terminal publication are compared with cleanup.
    cancelled = source.index("status=terminal_status")
    timed_out = source.index('status="failed_timeout"')
    cleanup = source.index("_stop_child(child)")

    # Then cleanup happens before either terminal state can be observed.
    assert cleanup < cancelled
    assert cleanup < timed_out
