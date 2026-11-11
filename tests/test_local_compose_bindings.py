"""Inspect real Compose merges without resolving secrets or touching Docker state."""
from __future__ import annotations

from ipaddress import ip_address
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Final

from pydantic import BaseModel, ConfigDict, Field
import pytest

ROOT: Final = Path(__file__).resolve().parents[1]
RUNTIME_MINIMUM: Final = (2, 24, 4)  # `ports: !override`, needed by the supported workflow
INSPECTION_MINIMUM: Final = (2, 35, 0)  # `config --no-env-resolution`, needed by these tests


def compose_version_problem(version_text: str) -> str | None:
    """Explain why this Compose cannot run the checks, or None when it can."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", version_text.strip())
    if match is None:
        return f"Cannot verify the Docker Compose version: {version_text!r}"
    version = tuple(map(int, match.groups()))
    if version < INSPECTION_MINIMUM:
        return (
            f"Docker Compose >= 2.35.0 is required to run these checks (`config --no-env-resolution`); "
            f"found {version_text.strip()}. The workflow itself needs >= 2.24.4 for `ports: !override`."
        )
    return None


class Port(BaseModel):
    model_config = ConfigDict(frozen=True)
    host_ip: str = ""
    target: int
    published: str


class Service(BaseModel):
    model_config = ConfigDict(frozen=True)
    ports: tuple[Port, ...] = ()


class ComposeConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    services: dict[str, Service] = Field(default_factory=dict)


@pytest.fixture(scope="module")
def compose() -> str:
    docker = shutil.which("docker")
    if docker is None:
        message = "Docker Compose >= 2.35.0 is required to verify local port bindings"
        if "GITHUB_ACTIONS" in os.environ:
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    version = subprocess.run(
        [docker, "compose", "version", "--short"],
        check=False, capture_output=True, text=True, timeout=15,
    )
    assert version.returncode == 0, f"Cannot run docker compose: {version.stderr}"
    problem = compose_version_problem(version.stdout)
    assert problem is None, problem
    return docker


@pytest.mark.parametrize("bind", [None, "192.168.1.10"])
def test_local_merge_publishes_only_selected_studio_binding(
    compose: str, tmp_path: Path, bind: str | None,
) -> None:
    # Given a clean interpolation environment, explicitly excluding bind/port overrides.
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    env_file = tmp_path / "compose-test.env"
    env_file.write_text(
        "POSTGRES_DB=bindings_test\nPOSTGRES_USER=bindings_test\n"
        "POSTGRES_PASSWORD=synthetic-only\nAPI_KEY=synthetic-only\n", encoding="utf-8",
    )
    command = [compose, "compose", "--env-file", str(env_file), "-f", "docker-compose.yml"]
    base = subprocess.run(
        [*command, "config", "--no-env-resolution", "--format", "json"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=True, timeout=30,
    )
    base_config = ComposeConfig.model_validate_json(base.stdout)
    for service in base_config.services.values():
        for port in service.ports:
            assert ip_address(port.host_ip).is_loopback
    assert len(base_config.services["studio-web"].ports) == 1
    assert base_config.services["studio-web"].ports[0].host_ip == "127.0.0.1"
    if bind is not None:
        env["STUDIO_WEB_BIND"] = bind
    # When merging the actual local-server overlay, without reading any service .env.
    result = subprocess.run(
        [*command, "-f", "docker-compose.local-server.yml", "config",
         "--no-env-resolution", "--format", "json"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=True, timeout=30,
    )
    config = ComposeConfig.model_validate_json(result.stdout)
    # Then exactly one Studio socket is declared, and every other socket stays private.
    studio = config.services["studio-web"].ports
    assert len(studio) == 1, studio
    assert (studio[0].host_ip, studio[0].published, studio[0].target) == (
        bind or "127.0.0.1", "5174", 8080,
    )
    for name in ("api", "postgres", "redis"):
        assert config.services[name].ports == base_config.services[name].ports
        assert config.services[name].ports
    for name, service in config.services.items():
        if name != "studio-web":
            for port in service.ports:
                assert ip_address(port.host_ip).is_loopback, (name, port)


def test_shipped_local_example_keeps_a_direct_compose_run_private(compose: str, tmp_path: Path) -> None:
    # Given an operator who copied the shipped example to .env and runs Compose directly,
    # which skips the Make guard, so Compose itself interpolates every value in that file.
    env_file = tmp_path / "copied-example.env"
    shutil.copyfile(ROOT / ".env.local.example", env_file)

    # When merging the real local-server overlay under that environment alone.
    result = subprocess.run(
        [compose, "compose", "--env-file", str(env_file), "-f", "docker-compose.yml",
         "-f", "docker-compose.local-server.yml", "config", "--no-env-resolution", "--format", "json"],
        cwd=ROOT, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        capture_output=True, text=True, check=True, timeout=30,
    )
    config = ComposeConfig.model_validate_json(result.stdout)

    # Then Studio is still published on loopback only: LAN exposure needs an explicit opt-in.
    studio = config.services["studio-web"].ports
    assert [(p.host_ip, p.published, p.target) for p in studio] == [("127.0.0.1", "5174", 8080)]


@pytest.mark.parametrize(
    ("version_text", "acceptable"),
    [
        ("v2.38.2\n", True),
        ("2.35.0", True),
        ("v5.0.2", True),
        ("v2.34.0", False),
        ("2.24.4", False),
        ("v2.24.4-desktop.1", False),
        ("not-a-version", False),
        ("", False),
    ],
)
def test_the_checks_state_their_own_compose_minimum(version_text: str, acceptable: bool) -> None:
    # Given a Compose that supports the workflow (2.24.4) but not `config --no-env-resolution`.
    # When deciding whether the checks can run on it.
    problem = compose_version_problem(version_text)

    # Then only a Compose with the flag is accepted, and the refusal names both minimums.
    assert (problem is None) is acceptable
    if problem is not None and "Cannot verify" not in problem:
        assert "2.35.0" in problem and "2.24.4" in problem
