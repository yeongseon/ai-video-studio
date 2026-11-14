"""Inspect real Compose merges in a throwaway project directory; never starts containers."""
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

from tests import test_local_bind

ROOT: Final = Path(__file__).resolve().parents[1]
MINIMUM: Final = (2, 24, 4)  # `ports: !override`, the minimum the local server guide documents
BASE_FILE: Final = "docker-compose.yml"
LOCAL_FILES: Final = (BASE_FILE, "docker-compose.local-server.yml")
SYNTHETIC_ENV: Final = (
    "POSTGRES_DB=bindings_test\nPOSTGRES_USER=bindings_test\n"
    "POSTGRES_PASSWORD=synthetic-only\nAPI_KEY=synthetic-only\n"
)


def compose_version_problem(version_text: str) -> str | None:
    """Explain why this Compose cannot run the checks, or None when it can."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", version_text.strip())
    if match is None:
        return f"Cannot verify the Docker Compose version: {version_text!r}"
    if tuple(map(int, match.groups())) < MINIMUM:
        return f"Docker Compose >= 2.24.4 is required for `ports: !override`; found {version_text.strip()}"
    return None


class Port(BaseModel):
    model_config = ConfigDict(frozen=True)
    host_ip: str = ""
    target: int
    published: str


class Service(BaseModel):
    model_config = ConfigDict(frozen=True)
    ports: tuple[Port, ...] = ()
    network_mode: str | None = None


class ComposeConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    services: dict[str, Service] = Field(default_factory=dict)


def assert_private_services(config: ComposeConfig, studio_bind: str = "127.0.0.1") -> None:
    """Check every socket, allowing only the selected Studio interface."""
    for name, service in config.services.items():
        assert service.network_mode != "host", name
        for port in service.ports:
            if name == "studio-web":
                assert port.host_ip == studio_bind, (name, port)
            else:
                assert ip_address(port.host_ip).is_loopback, (name, port)


@pytest.fixture(scope="module")
def compose() -> str:
    docker = shutil.which("docker")
    if docker is None:
        message = "Docker Compose >= 2.24.4 is required to verify local port bindings"
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


def merged_config(
    compose: str, project: Path, files: tuple[str, ...], env_text: str, bind: str | None = None,
) -> ComposeConfig:
    """Merge `files` in an isolated project directory whose `.env` is exactly `env_text`.

    Some Compose releases (2.38.2 on GitHub's runners) require every service `env_file` to
    exist even for `config`, and an operator's own `.env` must never be read, so nothing is
    resolved from the repository checkout.
    """
    project.mkdir()
    for name in files:
        shutil.copyfile(ROOT / name, project / name)
    (project / ".env").write_text(env_text, encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "COMPOSE_PROJECT_NAME": "local-bindings-contract"}
    if bind is not None:
        env["STUDIO_WEB_BIND"] = bind
    command = [compose, "compose", "--profile", "*", *(arg for name in files for arg in ("-f", name)), "config", "--format", "json"]
    result = subprocess.run(command, cwd=project, env=env, capture_output=True, text=True, check=False, timeout=30)
    assert result.returncode == 0, result.stderr
    return ComposeConfig.model_validate_json(result.stdout)


@pytest.mark.parametrize("bind", [None, "192.168.1.10", "[fd00::10]", "[::1]"])
def test_local_merge_publishes_only_selected_studio_binding(
    compose: str, tmp_path: Path, bind: str | None,
) -> None:
    # Given the base file alone, in a clean environment without bind or port overrides.
    base_config = merged_config(compose, tmp_path / "base", (BASE_FILE,), SYNTHETIC_ENV)
    for service in base_config.services.values():
        for port in service.ports:
            assert ip_address(port.host_ip).is_loopback
    assert len(base_config.services["studio-web"].ports) == 1
    assert base_config.services["studio-web"].ports[0].host_ip == "127.0.0.1"

    # When merging the actual local-server overlay, with the operator's explicit choice if any.
    config = merged_config(compose, tmp_path / "local", LOCAL_FILES, SYNTHETIC_ENV, bind)

    # Then exactly one Studio socket is declared, and every other socket stays private.
    studio = config.services["studio-web"].ports
    assert len(studio) == 1, studio
    assert (studio[0].host_ip, studio[0].published, studio[0].target) == (
        (bind or "127.0.0.1").strip("[]"), "5174", 8080,
    )
    for name in ("api", "postgres", "redis"):
        assert config.services[name].ports == base_config.services[name].ports
        assert config.services[name].ports
    for name, service in config.services.items():
        if name != "studio-web":
            for port in service.ports:
                assert ip_address(port.host_ip).is_loopback, (name, port)
    assert_private_services(base_config)
    assert_private_services(config, (bind or "127.0.0.1").strip("[]"))


@pytest.mark.parametrize("files", [(BASE_FILE,), LOCAL_FILES], ids=["base", "local"])
def test_all_profiled_services_are_checked(compose: str, tmp_path: Path, files: tuple[str, ...]) -> None:
    # Given the real configurations, including optional monitoring and GPU services.
    config = merged_config(compose, tmp_path / "profiles", files, SYNTHETIC_ENV)
    # When checking the merged service inventory.
    assert {"flower", "ollama", "stable-diffusion", "tts-qwen3", "stt-whisper", "tts-cosyvoice"} <= config.services.keys()
    # Then every service uses the same private-ingress checker.
    assert_private_services(config)


@pytest.mark.parametrize("files", [(BASE_FILE,), LOCAL_FILES], ids=["base", "local"])
@pytest.mark.parametrize(
    ("name", "mutation"),
    [
        ("flower", Service(ports=(Port(host_ip="0.0.0.0", published="5555", target=5555),))),
        ("worker", Service(network_mode="host")),
        ("ollama", Service(ports=(Port(host_ip="::", published="11434", target=11434),))),
    ],
    ids=["flower-wildcard", "host-network", "gpu-ipv6-wildcard"],
)
def test_checker_rejects_ingress_mutations(
    compose: str, tmp_path: Path, files: tuple[str, ...], name: str, mutation: Service,
) -> None:
    # Given an in-memory copy of the real merge with one unsafe service.
    config = merged_config(compose, tmp_path / "mutation", files, SYNTHETIC_ENV)
    assert name in config.services
    mutated = config.model_copy(deep=True)
    mutated.services[name] = mutation
    # When checking the mutated inventory, then it must fail for that service.
    with pytest.raises(AssertionError, match=name):
        assert_private_services(mutated)


def test_shipped_local_example_keeps_a_direct_compose_run_private(compose: str, tmp_path: Path) -> None:
    # Given an operator who copied the shipped example to .env and runs Compose directly,
    # which skips the Make guard, so Compose itself interpolates every value in that file.
    example = (ROOT / ".env.local.example").read_text(encoding="utf-8")

    # When merging the real local-server overlay under that environment alone.
    config = merged_config(compose, tmp_path / "copied-example", LOCAL_FILES, example)

    # Then Studio is still published on loopback only: LAN exposure needs an explicit opt-in.
    studio = config.services["studio-web"].ports
    assert [(p.host_ip, p.published, p.target) for p in studio] == [("127.0.0.1", "5174", 8080)]


def test_make_export_wins_over_legacy_env_in_real_compose(
    compose: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given legacy wildcard text and a confirmed, distinct IPv6 shell selection.
    for name in LOCAL_FILES:
        shutil.copyfile(ROOT / name, tmp_path / name)
    (tmp_path / ".env").write_text(SYNTHETIC_ENV + "STUDIO_WEB_BIND=0.0.0.0\n", encoding="utf-8")
    monkeypatch.setattr(test_local_bind, "FAKE_DOCKER", (
        f'#!/bin/sh\n"{compose}" compose --profile "*" -f docker-compose.yml '
        '-f docker-compose.local-server.yml config --format json > "$FAKE_DOCKER_LOG"\n'
    ))
    # When the real Make recipe hands its export to a config-only Compose stand-in.
    result, rendered = test_local_bind._run_make(
        tmp_path, "docker-local-up STUDIO_WEB_BIND=fd00::10 STUDIO_WEB_LAN_OK=1",
    )
    # Then real interpolation uses the guard's bracketed IPv6 output, not the file.
    assert result.returncode == 0, result.stderr
    assert "WARNING" in result.stderr and ".env" in result.stderr
    config = ComposeConfig.model_validate_json(rendered)
    assert_private_services(config, "fd00::10")
    assert len(config.services["studio-web"].ports) == 1


@pytest.mark.parametrize(
    ("version_text", "acceptable"),
    [
        ("v2.38.2\n", True),
        ("2.24.4", True),
        ("v2.24.4-desktop.1", True),
        ("v5.0.2", True),
        ("2.24.3", False),
        ("v2.23.9", False),
        ("not-a-version", False),
        ("", False),
    ],
)
def test_the_checks_state_the_documented_compose_minimum(version_text: str, acceptable: bool) -> None:
    # Given a version string as printed by `docker compose version --short`.
    # When deciding whether `ports: !override` can be verified with it.
    problem = compose_version_problem(version_text)

    # Then only the documented minimum or newer is accepted, and a refusal names it.
    assert (problem is None) is acceptable
    if problem is not None and "Cannot verify" not in problem:
        assert "2.24.4" in problem
