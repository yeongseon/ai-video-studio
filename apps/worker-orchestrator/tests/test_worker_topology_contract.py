from __future__ import annotations

import inspect
from pathlib import Path

import celery
import pytest
import yaml
from celery.concurrency.solo import TaskPool


ROOT = Path(__file__).parents[3]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
WORKER_DOCKERFILE = ROOT / "apps" / "worker-orchestrator" / "Dockerfile"


def _worker_command(compose: str, service: str) -> list[str]:
    document = yaml.safe_load((ROOT / compose).read_text(encoding="utf-8"))
    return document["services"][service]["command"]


def test_default_worker_command_tracks_shipped_solo_topology() -> None:
    # Given the base deployment definition.
    command = _worker_command("docker-compose.yml", "worker")

    # When the process-test harness selects its worker arguments.
    worker_args = command[:]

    # Then it uses the exact shipped application, pool, and concurrency.
    assert worker_args == [
        "celery", "-A", "celery_app.celery_app", "worker", "--loglevel=info",
        "--pool=solo", "--concurrency=1",
    ]


def test_scaled_worker_commands_track_every_shipped_pool() -> None:
    # Given the queue-specific deployment definition.
    services = {
        name: _worker_command("docker-compose.scaled-workers.yml", name)
        for name in ("worker-script", "worker-image", "worker-audio", "worker-render")
    }

    # When their executable commands are inspected.
    pools = {name: next(arg for arg in command if arg.startswith("--pool=")) for name, command in services.items()}

    # Then process tests cover both the solo and prefork topologies actually shipped.
    assert pools == {
        "worker-script": "--pool=solo",
        "worker-image": "--pool=prefork",
        "worker-audio": "--pool=solo",
        "worker-render": "--pool=solo",
    }


def test_pinned_celery_solo_pool_declares_no_timeout_support() -> None:
    # Given the Celery version pinned by constraints.txt.
    pinned = next(
        line.removeprefix("celery==")
        for line in (ROOT / "constraints.txt").read_text(encoding="utf-8").splitlines()
        if line.startswith("celery==")
    )

    # When the installed solo implementation reports its capabilities.
    pool = TaskPool()

    # Then source and runtime evidence agree that execution is inline and untimed.
    assert celery.__version__ == pinned
    assert pool.info["timeouts"] == ()
    assert "self.on_apply = apply_target" in inspect.getsource(TaskPool.__init__)


def test_runtime_probe_is_shipped_and_registered_with_the_ci_app(monkeypatch) -> None:
    # Given the production Celery app imported exactly as the image imports it.
    monkeypatch.setenv("WORKER_TOPOLOGY_PROBE_ENABLED", "1")
    monkeypatch.setenv("ENVIRONMENT", "test")
    from celery_app import celery_app
    from celery_app import _worker_task_modules

    # When its normal include list is loaded, without a test-only --include override.
    celery_app.loader.import_task_module("tasks.runtime_topology_probe")

    # Then the shipped probe traverses the production task runner.
    assert "runtime_topology.probe" in celery_app.tasks
    assert "tasks.runtime_topology_probe" in _worker_task_modules()
    assert "tasks.revoked_quota" in _worker_task_modules()


def test_runtime_probe_accepts_nested_evidence_but_rejects_escape(monkeypatch, tmp_path) -> None:
    from tasks.runtime_topology_probe import _evidence_file

    # Given a constrained runtime evidence root.
    monkeypatch.setenv("WORKER_TOPOLOGY_EVIDENCE_DIR", str(tmp_path))

    # When a production-style storage key is resolved below that root.
    nested = _evidence_file("storage/100/artifact.bin")

    # Then nested persistence remains allowed while traversal is rejected.
    assert nested == tmp_path / "storage" / "100" / "artifact.bin"
    with pytest.raises(ValueError, match="Invalid topology evidence path"):
        _evidence_file("../escape.bin")


def test_runtime_probe_is_disabled_without_explicit_ci_enablement(monkeypatch) -> None:
    # Given a normal production worker environment.
    monkeypatch.delenv("WORKER_TOPOLOGY_PROBE_ENABLED", raising=False)

    # When the production app inclusion policy is evaluated.
    from celery_app import _worker_task_modules

    # Then the diagnostic task is not exposed to the broker.
    assert "tasks.runtime_topology_probe" not in _worker_task_modules()


def test_runtime_probe_is_disabled_in_production_even_when_requested(monkeypatch) -> None:
    # Given an accidental production deployment override.
    monkeypatch.setenv("WORKER_TOPOLOGY_PROBE_ENABLED", "1")
    monkeypatch.setenv("ENVIRONMENT", "production")

    # When the worker inclusion policy is evaluated.
    from celery_app import _worker_task_modules

    # Then the diagnostic task remains unavailable to the production broker.
    assert "tasks.runtime_topology_probe" not in _worker_task_modules()


def test_runtime_ci_builds_and_runs_the_shipped_worker_image() -> None:
    # Given the worker topology CI job.
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    # When its runtime setup is inspected.
    dockerfile = str(WORKER_DOCKERFILE.relative_to(ROOT))

    # Then it builds the shipped Dockerfile and runs the image's own entrypoint/CMD.
    assert f"docker build --target runtime -f {dockerfile}" in workflow
    assert "docker run" in workflow
    assert "--entrypoint" not in workflow
    assert "docker image inspect worker-topology-image" in workflow


def test_runtime_ci_uses_postgres_redis_ffmpeg_and_uploads_complete_evidence() -> None:
    # Given the worker topology CI job definition and the images the shipped stack pins.
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]

    # When service and artifact wiring are inspected.
    required = (
        services["postgres"]["image"],
        services["redis"]["image"],
        "ffmpeg -version",
        "manifest.json",
        "worker.log",
        "image-digest.txt",
        "resolved-command.txt",
        "cancellation-final-state.json",
        "redelivery-final-state.json",
        "timeout-final-state.json",
        "127.0.0.1:55441:5432",
        "127.0.0.1:56391:6379",
    )

    # Then the real dependencies and diagnostic evidence are mandatory.
    for marker in required:
        assert marker in workflow


def test_shipped_image_scenario_exercises_authenticated_stop_and_persistence_boundaries() -> None:
    # Given the process-level shipped-image scenario.
    source = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "apps/worker-orchestrator/tests/test_worker_topology_image.py",
            "apps/worker-orchestrator/tests/worker_topology_runtime.py",
        )
    )

    # When its cancellation path is inspected.
    required = (
        "/api/creator/runs/{run_id}/stop",
        '"X-API-Key": API_KEY',
        "workspace_quota_reservation_owners",
        "creator_artifacts",
        "creator_media_assets",
        "provider_calls",
        "storage_entries",
    )

    # Then cancellation traverses auth and records every durable boundary.
    for marker in required:
        assert marker in source


def test_failure_scenario_uses_exact_scaled_command_and_container_process_namespace() -> None:
    # Given the process-level failure scenario and shipped scaled worker command.
    source = (ROOT / "apps/worker-orchestrator/tests/worker_topology_runtime.py").read_text(
        encoding="utf-8"
    )
    scaled_command = _worker_command("docker-compose.scaled-workers.yml", "worker-image")

    # When topology and cleanup evidence are inspected.
    # Then the exact command is used and FFmpeg is observed through Docker's namespace mapping.
    assert all(f'"{argument}"' in source for argument in scaled_command)
    assert '"top"' in source
    assert '"--init"' in source
    assert '"ps", "-p"' not in source
    scenario = (ROOT / "apps/worker-orchestrator/tests/test_worker_topology_image.py").read_text(
        encoding="utf-8"
    )
    assert "active-redelivery" in scenario
    assert "send_scene_image" in scenario
    assert 'scene_id=\'scene-persisted\'' in scenario
    assert 'Path(timeout_assets["asset_path"]).relative_to("/evidence")' in scenario
    assert "persisted_file.is_file()" in scenario
    assert "timeout_files == [persisted_file]" in scenario
    assert "timeout-replacement" in scenario


def test_provider_stub_and_runtime_artifacts_are_isolated_per_phase() -> None:
    # Given the shipped-image runtime harness.
    source = (ROOT / "apps/worker-orchestrator/tests/test_worker_topology_image.py").read_text(
        encoding="utf-8"
    )

    # When network and evidence destinations are inspected.
    # Then the provider avoids a wildcard bind and each phase has durable state evidence.
    assert 'ThreadingHTTPServer(("0.0.0.0", 0)' not in source
    assert "cancellation-final-state.json" in source
    assert "redelivery-final-state.json" in source
    assert "timeout-final-state.json" in source
