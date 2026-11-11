"""Inspect storage and memory contracts without contacting the Docker daemon."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Final

from pydantic import BaseModel, ConfigDict, Field
import pytest

ROOT: Final = Path(__file__).resolve().parents[1]
TARGET: Final = "/app/data/artifacts"
OVERLAYS: Final = (None, "dev", "e2e", "scaled-workers", "local-server")
SYNTHETIC_ENV: Final = (
    "POSTGRES_DB=storage_test\nPOSTGRES_USER=storage_test\n"
    "POSTGRES_PASSWORD=synthetic-only\nAPI_KEY=synthetic-only\n"
)


class Volume(BaseModel):
    model_config = ConfigDict(frozen=True)
    type: str
    source: str = ""
    target: str
    read_only: bool = False


class Limits(BaseModel):
    model_config = ConfigDict(frozen=True)
    memory: int = 0


class Resources(BaseModel):
    model_config = ConfigDict(frozen=True)
    limits: Limits = Field(default_factory=Limits)


class Deploy(BaseModel):
    model_config = ConfigDict(frozen=True)
    resources: Resources = Field(default_factory=Resources)


class Service(BaseModel):
    model_config = ConfigDict(frozen=True)
    volumes: tuple[Volume, ...] = ()
    environment: dict[str, str | None] = Field(default_factory=dict)
    deploy: Deploy = Field(default_factory=Deploy)


class ComposeConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    services: dict[str, Service]


@dataclass(frozen=True, slots=True)
class Project:
    """The real compose files in an isolated project directory with its own synthetic `.env`.

    Some Compose releases (2.38.2 on GitHub's runners) require every service `env_file` to
    exist even for `config`, and an operator's own `.env` must never be read. The variables
    under test travel in the process environment, which Compose interpolates but which never
    becomes part of a service's `environment`.
    """

    docker: str
    directory: Path
    files: tuple[str, ...]

    def config(self, values: tuple[str, ...] = ()) -> ComposeConfig:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "COMPOSE_PROJECT_NAME": "local-storage-contract"}
        env.update(value.partition("=")[::2] for value in values)
        command = [self.docker, "compose", "--profile", "*",
                   *(arg for name in self.files for arg in ("-f", name)), "config", "--format", "json"]
        result = subprocess.run(
            command, cwd=self.directory, env=env, capture_output=True, text=True, check=False, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return ComposeConfig.model_validate_json(result.stdout)


@dataclass(frozen=True, slots=True)
class Inspection:
    docker: str
    root: Path

    def project(self, overlay: str | None) -> Project:
        directory = Path(tempfile.mkdtemp(dir=self.root))
        files = ("docker-compose.yml", *((f"docker-compose.{overlay}.yml",) if overlay else ()))
        for name in files:
            shutil.copyfile(ROOT / name, directory / name)
        (directory / ".env").write_text(SYNTHETIC_ENV, encoding="utf-8")
        return Project(self.docker, directory, files)


@pytest.fixture
def inspection(tmp_path: Path) -> Inspection:
    docker = shutil.which("docker")
    if docker is None:
        message = "Docker Compose is required for storage config inspection"
        if "GITHUB_ACTIONS" in os.environ:
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    return Inspection(docker, tmp_path)


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_artifact_mount_when_default_or_overridden(inspection: Inspection, overlay: str | None) -> None:
    # Given the real base or merged overlay with no operator environment leaking in.
    project = inspection.project(overlay)
    baseline = project.config()
    destination = inspection.root / "hdd media"
    consumers = {"api", "worker"}
    if overlay == "scaled-workers":
        consumers.update({"worker-script", "worker-image", "worker-audio", "worker-render"})
    for name in consumers:
        assert [v.source for v in baseline.services[name].volumes if v.target == TARGET] == [
            str(project.directory / "data/artifacts"),
        ]

    # When selecting a different durable host directory.
    configured = project.config((f"ARTIFACTS_HOST_DIR={destination}",))

    # Then only the artifact bind sources change, including every scaled worker.
    assert configured.services.keys() == baseline.services.keys()
    for name, service in configured.services.items():
        original = baseline.services[name]
        artifacts = [v for v in service.volumes if v.target == TARGET]
        assert len(artifacts) == int(name in consumers), name
        if name in consumers:
            assert artifacts == [Volume(type="bind", source=str(destination), target=TARGET)]
        assert [v for v in service.volumes if v.target != TARGET] == [
            v for v in original.volumes if v.target != TARGET
        ]
        assert service.environment == original.environment
        assert service.deploy == original.deploy
    assert [v.source for v in configured.services["worker"].volumes
            if v.target == "/var/lib/short-form-studio/dlq"] == ["dlq_data"]


@pytest.mark.parametrize("overlay", OVERLAYS)
@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ((), (1024, 4096)),
        (("API_MAX_MEMORY_MB=1536",), (1536, 4096)),
        (("WORKER_MAX_MEMORY_MB=5120",), (1024, 5120)),
        (("MAX_MEMORY_MB=2048",), (2048, 2048)),
        (("MAX_MEMORY_MB=2048", "API_MAX_MEMORY_MB=1536"), (1536, 2048)),
        (("MAX_MEMORY_MB=2048", "WORKER_MAX_MEMORY_MB=5120"), (2048, 5120)),
        (("MAX_MEMORY_MB=2048", "API_MAX_MEMORY_MB=1536", "WORKER_MAX_MEMORY_MB=5120"),
         (1536, 5120)),
        (("MAX_MEMORY_MB=", "API_MAX_MEMORY_MB=", "WORKER_MAX_MEMORY_MB="), (1024, 4096)),
        (("MAX_MEMORY_MB=2048", "API_MAX_MEMORY_MB=", "WORKER_MAX_MEMORY_MB="), (2048, 2048)),
    ],
)
def test_memory_when_resolving_precedence(
    inspection: Inspection, overlay: str | None, values: tuple[str, ...], expected: tuple[int, int],
) -> None:
    # Given distinct defaults, legacy fallback and independent overrides.
    # When resolving the real merged container configuration.
    config = inspection.project(overlay).config(values)

    # Then each application sees exactly its own declared cgroup limit in MiB.
    for name, memory_mb in zip(("api", "worker"), expected, strict=True):
        service = config.services[name]
        assert service.deploy.resources.limits.memory == memory_mb * 1024 * 1024, name
        assert service.environment.get("MAX_MEMORY_MB") == str(memory_mb), name
