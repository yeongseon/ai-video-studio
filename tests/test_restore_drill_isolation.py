from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import restore_drill
from restore_drill_runtime import DrillError
from tests.restore_drill_fake import DrillSandbox


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DrillSandbox:
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LANG": "C.UTF-8"})
    fake = DrillSandbox()
    release = tmp_path / "release.json"
    release.write_text(json.dumps({name: f"registry/{name}@sha256:" + "0123456789abcdef" * 4 for name in ("api", "worker", "web")}))
    monkeypatch.setenv("RESTORE_RELEASE_MANIFEST", str(release))
    monkeypatch.delenv("RESTORE_CORRUPT_AUDIO_METADATA", raising=False)
    monkeypatch.setattr("restore_drill.shutil.which", lambda name: f"/synthetic/{name}")
    monkeypatch.setattr("restore_drill.subprocess.run", fake.run)
    monkeypatch.setattr("restore_drill_runtime.subprocess.Popen", fake.popen)
    monkeypatch.setattr("restore_drill.wait_for_api", lambda *args: None)
    monkeypatch.setattr("restore_drill.verify_http", lambda *args: None)
    return fake


@pytest.mark.parametrize("child", ["docker", "migration", "verifier", "git", "render", "api", "guard", "reconciler", "readiness", "cleanup"])
def test_every_child_uses_isolated_environment_when_parent_is_poisoned(
    sandbox: DrillSandbox, monkeypatch: pytest.MonkeyPatch, child: str
) -> None:
    # Given an operator shell with synthetic production credentials and destinations.
    poison = {name: f"poison-{name}" for name in (
        "REDIS_URL", "DATABASE_URL", "OPENAI_API_KEY", "CELERY_BROKER_URL",
        "CUSTOM_API_KEY", "CUSTOM_TOKEN", "CUSTOM_SECRET", "OTEL_EXPORTER_OTLP_ENDPOINT",
        "AWS_ACCESS_KEY_ID", "AZURE_STORAGE_CONNECTION_STRING", "GOOGLE_APPLICATION_CREDENTIALS",
        "STORAGE_BACKEND", "PGSERVICE", "PG_RESTORE", "PYTHONPATH",
    )}
    for name, value in poison.items():
        monkeypatch.setenv(name, value)
    selectors = {name: f"synthetic-{name}" for name in (
        "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH",
        "DOCKER_TLS_VERIFY", "XDG_RUNTIME_DIR",
    )}
    for name, value in selectors.items():
        monkeypatch.setenv(name, value)

    # When running every drill phase against injected external processes.
    assert restore_drill.main() == 0

    # Then each subprocess gets an explicit environment, never ambient configuration.
    selections = {
        "docker": lambda c: c[0] == "docker",
        "migration": lambda c: "alembic" in c,
        "verifier": lambda c: any(value.endswith("restore_verify.py") for value in c),
        "git": lambda c: c[0] == "git",
        "render": lambda c: any("render_saved_timeline" in value for value in c),
        "api": lambda c: "uvicorn" in c,
        "guard": lambda c: any("HTTPServer" in value for value in c),
        "reconciler": lambda c: any("reconcile_stale_dispatches" in value for value in c),
        "readiness": lambda c: "pg_isready" in c,
        "cleanup": lambda c: c[:2] == ("docker", "rm"),
    }
    calls = [(command, env) for command, env in sandbox.calls if selections[child](command)]
    assert calls, child
    for index, (command, environment) in enumerate(calls):
        assert environment is not None, command
        assert not set(poison.values()).intersection(environment.values()), command
        assert environment["REDIS_URL"] == "", command
        for name, value in selectors.items():
            if command[0] == "docker":
                assert environment.get(name) == value, command
            else:
                assert name not in environment, command
        if child in {"migration", "render", "api", "reconciler"}:
            expected_port = "15432" if child == "migration" and index < 2 else "25432"
            assert environment["DATABASE_URL"] == f"postgresql://restore:synthetic-local-only@127.0.0.1:{expected_port}/restore"
        else:
            assert "DATABASE_URL" not in environment


@pytest.mark.parametrize("existing", ["restore-source-", "restore-target-"])
def test_drill_refuses_existing_name_before_starting_any_container(
    sandbox: DrillSandbox, existing: str
) -> None:
    # Given either planned PostgreSQL container name is already owned elsewhere.
    sandbox.existing = existing

    # When / Then the typed failure propagates to a nonzero CLI exit before creation/deletion.
    with pytest.raises(DrillError, match="container already exists") as error:
        restore_drill.main()
    assert type(error.value).__name__ == "ContainerNameCollisionError"
    assert sandbox.docker_calls("run") == []
    assert sandbox.docker_calls("rm") == []


@pytest.mark.parametrize("failed_start", ["restore-source-", "restore-target-"])
def test_cleanup_removes_only_successfully_created_containers_when_start_fails(
    sandbox: DrillSandbox, failed_start: str
) -> None:
    # Given a failed docker run, including a race after the preflight check.
    sandbox.failed_start = failed_start

    # When
    with pytest.raises(DrillError, match="synthetic start failure"):
        restore_drill.main()

    # Then the failed name is never removed, even after the first container succeeded.
    removed = [name for call in sandbox.docker_calls("rm") for name in call[3:]]
    assert all(not name.startswith(failed_start) for name in removed)
    assert len(removed) == int(failed_start == "restore-target-")
    assert all(name.startswith("restore-source-") for name in removed)


def test_happy_path_removes_both_containers_it_created(sandbox: DrillSandbox) -> None:
    # Given an empty disposable sandbox.
    # When
    assert restore_drill.main() == 0

    # Then both created names are checked before any container starts and cleaned up.
    created = [call[call.index("--name") + 1] for call in sandbox.docker_calls("run") if "--name" in call]
    checked = [call[call.index("--filter") + 1] for call in sandbox.docker_calls("ps")]
    assert checked == [f"name=^/{name}$" for name in created]
    operations = [command[1] for command, _ in sandbox.calls if command[0] == "docker"]
    assert operations[:2] == ["ps", "ps"]
    removed = [name for call in sandbox.docker_calls("rm") for name in call[3:]]
    assert removed == created
    assert len(created) == 2


@pytest.mark.parametrize("failed_start", ["restore-source-", "restore-target-"])
@pytest.mark.parametrize("failure", ["exit", "timeout", "interrupt"])
def test_cleanup_removes_labelled_container_when_run_creates_then_fails(
    sandbox: DrillSandbox, failed_start: str, failure: str
) -> None:
    # Given docker created a container before reporting failure or interruption.
    sandbox.created_then_failed = failed_start
    error: type[DrillError] | type[subprocess.TimeoutExpired] | type[KeyboardInterrupt] = DrillError
    if failure == "timeout":
        sandbox.start_failure = subprocess.TimeoutExpired("docker run", 120)
        error = subprocess.TimeoutExpired
    elif failure == "interrupt":
        sandbox.start_failure = KeyboardInterrupt()
        error = KeyboardInterrupt

    # When
    with pytest.raises(error):
        restore_drill.main()

    # Then only the exact name carrying this run's nonce proves failed-start ownership.
    assert sandbox.containers == {}
    started = [call for call in sandbox.docker_calls("run") if "--name" in call]
    labels = [call[call.index("--label") + 1] for call in started]
    assert len(set(labels)) == 1
    nonce = labels[0].removeprefix("restore-drill.run=")
    assert len(bytes.fromhex(nonce)) == 16
    failed_name = started[-1][started[-1].index("--name") + 1]
    queries = [call for call in sandbox.docker_calls("ps") if "-aq" in call]
    assert ("docker", "ps", "-aq", "--filter", f"name=^/{failed_name}$", "--filter", f"label={labels[0]}") in queries
    removed = [name for call in sandbox.docker_calls("rm") for name in call[3:]]
    assert f"id-{failed_name}" in removed


def test_cleanup_preserves_foreign_container_when_start_loses_creation_race(
    sandbox: DrillSandbox,
) -> None:
    # Given another actor creates an unlabelled container after preflight.
    sandbox.failed_start = "restore-target-"

    # When
    with pytest.raises(DrillError, match="synthetic start failure"):
        restore_drill.main()

    # Then the foreign container survives while this run's source is removed.
    assert len(sandbox.containers) == 1
    name, (_, labels) = next(iter(sandbox.containers.items()))
    assert name.startswith("restore-target-")
    assert labels == set()


def test_cleanup_query_failure_preserves_original_start_error(sandbox: DrillSandbox) -> None:
    # Given an owned failed start followed by an unavailable cleanup query.
    sandbox.created_then_failed = "restore-source-"
    sandbox.cleanup_failure = OSError("synthetic cleanup unavailable")

    # When / Then the start error remains the primary failure.
    with pytest.raises(DrillError, match="synthetic start failure"):
        restore_drill.main()


@pytest.mark.parametrize("child", ["api", "reconciler"])
def test_worker_state_paths_are_inside_drill_root_when_child_imports_worker(
    sandbox: DrillSandbox, monkeypatch: pytest.MonkeyPatch, child: str
) -> None:
    # Given an operator's shared-host state destinations.
    names = ("BROKER_EPOCH_STATE_PATH", "DLQ_FALLBACK_PATH", "BEAT_HEARTBEAT_PATH")
    for name in names:
        monkeypatch.setenv(name, f"/synthetic-production/{name}")

    # When
    assert restore_drill.main() == 0

    # Then both worker-importing children explicitly select disposable state paths.
    environments = [env for command, env in sandbox.calls if
                    ("uvicorn" in command if child == "api" else any("reconcile_stale_dispatches" in value for value in command))]
    assert len(environments) == 1
    environment = environments[0]
    assert environment is not None
    assert sandbox.media is not None
    root = sandbox.media.parent.parent
    for name in names:
        path = Path(environment[name])
        assert path.is_absolute()
        assert path.is_relative_to(root)
