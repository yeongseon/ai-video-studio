"""Cheap local-server singleton and health contracts; never start containers."""

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field


ROOT = Path(__file__).resolve().parents[1]


class Service(BaseModel):
    model_config = ConfigDict(frozen=True)
    command: list[str] | None = None
    healthcheck: dict[str, str | int | list[str]] = Field(default_factory=dict)
    restart: str = ""


class Compose(BaseModel):
    model_config = ConfigDict(frozen=True)
    services: dict[str, Service]


def _compose_tokens(makefile: str, target: str) -> list[str]:
    match = re.search(rf"^{target}:\n((?:\t[^\n]*\n)+)", makefile, re.MULTILINE)
    assert match is not None, f"no recipe for {target}"
    return shlex.split(match[1].replace("\\\n\t", " ").split("&&")[-1])


@pytest.mark.parametrize(
    ("target", "operation", "expected"),
    [
        ("docker-local-up", "up", ["postgres", "redis", "api", "worker", "beat", "studio-web"]),
        ("docker-local-build", "up", ["postgres", "redis", "api", "worker", "beat", "studio-web"]),
        ("docker-local-logs", "logs", ["api", "worker", "beat", "studio-web"]),
    ],
)
def test_local_target_when_selecting_services(target: str, operation: str, expected: list[str]) -> None:
    # Given the supported Make entrypoint's actual recipe.
    tokens = _compose_tokens((ROOT / "Makefile").read_text(), target)

    # When extracting service arguments after the Compose operation.
    services = [token for token in tokens[tokens.index(operation) + 1:] if not token.startswith("-")]

    # Then the original subset includes exactly one dedicated scheduler, never scaling it.
    assert "beat" in services
    assert services == expected
    assert services.count("beat") == 1
    assert "--scale" not in tokens


def test_service_selection_when_the_recipe_has_a_validation_prefix() -> None:
    # Given a recipe that validates its input on a continued line before invoking Compose,
    # the shape the private-by-default bind guard gives these targets.
    makefile = (
        "docker-local-up:\n"
        '\tSTUDIO_WEB_BIND="$$(python3 scripts/validate_local_bind.py)" && export STUDIO_WEB_BIND && \\\n'
        "\tdocker compose -f docker-compose.yml up -d postgres redis api worker beat studio-web\n"
    )

    # When extracting the Compose command's tokens.
    tokens = _compose_tokens(makefile, "docker-local-up")

    # Then the guard prefix does not hide the services Compose receives.
    assert tokens[:2] == ["docker", "compose"]
    assert tokens[tokens.index("up") + 1:] == ["-d", "postgres", "redis", "api", "worker", "beat", "studio-web"]


def test_local_compose_when_resolved_without_containers(tmp_path: Path) -> None:
    # Given a disposable config-only checkout with a non-secret example environment.
    # Compose's service env_file is required even for config; do not mutate a user's .env.
    for name in ("docker-compose.yml", "docker-compose.local-server.yml"):
        (tmp_path / name).write_text((ROOT / name).read_text())
    (tmp_path / ".env").write_text((ROOT / ".env.example").read_text())

    # When resolving the real merged Compose configuration, never contacting the daemon.
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.local-server.yml", "config", "--format", "json"],
        cwd=tmp_path, env={**os.environ, "COMPOSE_PROJECT_NAME": "local-beat-contract"},
        check=True, capture_output=True, text=True,
    )
    config = Compose.model_validate_json(result.stdout)

    # Then Beat has executable health, restart policy, and no embedded worker scheduler.
    beat = config.services["beat"]
    assert beat.healthcheck.get("test") == ["CMD", "python", "-m", "beat_health"]
    assert beat.restart == "unless-stopped"
    assert beat.command is not None and "beat" in beat.command
    worker_command = config.services["worker"].command
    assert worker_command is not None
    assert not {"-B", "--beat"}.intersection(worker_command)


def test_contract_when_ci_selects_local_lifecycle_inputs() -> None:
    # Given the existing fail-closed classifier and code job command.
    from scripts.ci_impact import classify_paths

    workflow = (ROOT / ".github/workflows/ci.yml").read_text()

    # When these runtime/config paths change, then the contract is both selected and invoked.
    for path in ("Makefile", "docker-compose.yml", "docker-compose.local-server.yml", "apps/worker-orchestrator/beat_health.py"):
        assert classify_paths([path])["code"]
    assert "tests/test_local_beat_lifecycle.py" in workflow


@pytest.mark.parametrize("app", ["api", "worker-orchestrator"])
def test_health_module_when_image_ships_celery_app(app: str) -> None:
    # Given both images import the same Celery application and task modules.
    dockerfile = (ROOT / "apps" / app / "Dockerfile").read_text()

    # When assembling either runtime, then its health-module dependency ships too.
    assert re.search(r"^COPY .*apps/worker-orchestrator/beat_health.py ./beat_health.py$", dockerfile, re.MULTILINE)
