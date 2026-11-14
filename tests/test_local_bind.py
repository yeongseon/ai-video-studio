"""Local helper boundary and Make guard contracts; never start Docker."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("bind", "confirmation", "expected"),
    [
        (None, None, "127.0.0.1"),
        ("127.0.0.1", None, "127.0.0.1"),
        ("192.168.1.10", "1", "192.168.1.10"),
        ("::1", None, "[::1]"),
        ("fd00::10", "1", "[fd00::10]"),
        ("127.12.34.56", None, "127.12.34.56"),
        ("::ffff:127.0.0.2", None, "127.0.0.2"),
        ("10.0.0.1", "1", "10.0.0.1"),
        ("172.17.0.1", "1", "172.17.0.1"),
        ("fe80::1", "1", "[fe80::1]"),
        ("0.0.0.0", "1", "0.0.0.0"),
        ("::", "1", "[::]"),
        ("::ffff:0.0.0.0", "1", "0.0.0.0"),
    ],
)
def test_bind_is_rendered_when_explicitly_safe(
    bind: str | None, confirmation: str | None, expected: str,
) -> None:
    # Given only operator-provided exposure settings, not a developer .env.
    env = {key: value for key, value in os.environ.items() if not key.startswith("STUDIO_WEB_")}
    if bind is not None:
        env["STUDIO_WEB_BIND"] = bind
    if confirmation is not None:
        env["STUDIO_WEB_LAN_OK"] = confirmation
    # When invoking the dependency-free validator boundary.
    result = subprocess.run(
        [sys.executable, "scripts/validate_local_bind.py"], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=10, check=False,
    )
    # Then only a safe Compose host literal is emitted.
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize(
    "bind",
    [
        "", "localhost", "0.0.0.0", "::", "::ffff:0.0.0.0", "::ffff:0:0",
        "127.0.0.1:1:2", "[::1]", "fe80::1%eth0", "$(id)",
        "192.168.1.10", "fd00::10", "10.0.0.1", "172.17.0.1", "fe80::1",
    ],
)
def test_bind_is_rejected_when_invalid_or_unconfirmed(bind: str) -> None:
    # Given an invalid literal or an unconfirmed wildcard.
    env = {key: value for key, value in os.environ.items() if not key.startswith("STUDIO_WEB_")}
    env["STUDIO_WEB_BIND"] = bind
    # When validating the operator input.
    result = subprocess.run(
        [sys.executable, "scripts/validate_local_bind.py"], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=10, check=False,
    )
    # Then fail closed with no binding available for Compose.
    assert result.returncode == 2
    assert "STUDIO_WEB_BIND" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("target", ["docker-local-up", "docker-local-build"])
def test_make_guards_binding_before_docker(target: str) -> None:
    # Given either supported start target, inspect it without executing recipes.
    # When expanding the Make recipe (no Docker invocation).
    result = subprocess.run(
        ["make", "--dry-run", target], cwd=ROOT,
        capture_output=True, text=True, timeout=10, check=True,
    )
    # Then validation must succeed before its output can reach Docker.
    command = result.stdout
    assert command.index("scripts/validate_local_bind.py") < command.index("docker compose")
    assert 'STUDIO_WEB_BIND="$(python3 scripts/validate_local_bind.py)" &&' in command
    assert "export STUDIO_WEB_BIND &&" in command


FAKE_DOCKER = '#!/bin/sh\necho "bind=[$STUDIO_WEB_BIND]" >> "$FAKE_DOCKER_LOG"\n'


def _run_make(
    tmp_path: Path, target: str, **settings: str,
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Execute the real recipe with a recording stand-in for docker: nothing is ever started."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    log = tmp_path / "docker-calls.log"
    log.write_text("", encoding="utf-8")
    env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "FAKE_DOCKER_LOG": str(log), **settings}
    result = subprocess.run(
        ["make", "-s", target], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=30, check=False,
    )
    return result, log.read_text(encoding="utf-8")


@pytest.mark.parametrize("target", ["docker-local-up", "docker-local-build"])
def test_default_start_reaches_docker_with_the_private_bind(tmp_path: Path, target: str) -> None:
    # Given an operator who set nothing: the common, supported case.
    # When the real recipe runs.
    result, calls = _run_make(tmp_path, target)
    # Then it is not blocked by its own guard, and Compose is handed loopback.
    assert result.returncode == 0, result.stderr
    assert calls == "bind=[127.0.0.1]\n"


@pytest.mark.parametrize("target", ["docker-local-up", "docker-local-build"])
@pytest.mark.parametrize(
    "settings",
    [
        {"STUDIO_WEB_BIND": "localhost"},
        {"STUDIO_WEB_BIND": ""},
        {"STUDIO_WEB_BIND": "127.0.0.1:1:2"},
        {"STUDIO_WEB_BIND": "0.0.0.0"},
        {"STUDIO_WEB_BIND": "::"},
        {"STUDIO_WEB_BIND": "::ffff:0.0.0.0"},
        {"STUDIO_WEB_BIND": "::ffff:0:0"},
        {"STUDIO_WEB_BIND": "192.168.1.10"},
        {"STUDIO_WEB_BIND": "fd00::10"},
        {"STUDIO_WEB_BIND": "8.8.8.8", "STUDIO_WEB_LAN_OK": "1"},
    ],
    ids=["hostname", "empty", "injection", "wildcard", "v6-wildcard", "mapped-wildcard", "mapped-hex", "lan-address", "ula", "global"],
)
def test_invalid_or_unconfirmed_bind_never_reaches_docker(
    tmp_path: Path, target: str, settings: dict[str, str],
) -> None:
    # Given an invalid literal or an unconfirmed wildcard, in any spelling.
    # When the real recipe runs.
    result, calls = _run_make(tmp_path, target, **settings)
    # Then make fails and docker was never invoked.
    assert result.returncode != 0
    assert calls == ""


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({"STUDIO_WEB_BIND": "192.168.1.10", "STUDIO_WEB_LAN_OK": "1"}, "192.168.1.10"),
        ({"STUDIO_WEB_BIND": "0.0.0.0", "STUDIO_WEB_LAN_OK": "1"}, "0.0.0.0"),
        ({"STUDIO_WEB_BIND": "::ffff:0.0.0.0", "STUDIO_WEB_LAN_OK": "1"}, "0.0.0.0"),
        ({"STUDIO_WEB_BIND": "::1"}, "[::1]"),
    ],
    ids=["lan-address", "confirmed-wildcard", "confirmed-mapped-wildcard", "ipv6-loopback"],
)
def test_validated_bind_is_what_compose_receives(
    tmp_path: Path, settings: dict[str, str], expected: str,
) -> None:
    # Given an explicit, valid opt-in.
    # When the real recipe runs.
    result, calls = _run_make(tmp_path, "docker-local-up", **settings)
    # Then Compose receives the normalized literal, not the raw operator text.
    assert result.returncode == 0, result.stderr
    assert calls == f"bind=[{expected}]\n"


@pytest.mark.parametrize("bind", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
@pytest.mark.parametrize("confirmation", [None, "1"])
def test_global_bind_is_always_refused(bind: str, confirmation: str | None) -> None:
    # Given a public address, even with explicit confirmation.
    env = {key: value for key, value in os.environ.items() if not key.startswith("STUDIO_WEB_")}
    env["STUDIO_WEB_BIND"] = bind
    if confirmation is not None:
        env["STUDIO_WEB_LAN_OK"] = confirmation
    # When invoking the validator.
    result = subprocess.run(
        [sys.executable, "scripts/validate_local_bind.py"], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=10, check=False,
    )
    # Then public ingress is refused without a Compose literal.
    assert result.returncode == 2
    assert "global" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("bind", ["192.168.1.10", "fd00::10", "0.0.0.0", "::"])
@pytest.mark.parametrize("confirmation", ["", "true", "yes", "01", " 1", "1 ", "0"])
def test_confirmation_requires_exact_one(bind: str, confirmation: str) -> None:
    # Given a non-loopback address and a noncanonical confirmation.
    env = {**os.environ, "STUDIO_WEB_BIND": bind, "STUDIO_WEB_LAN_OK": confirmation}
    # When invoking the validator.
    result = subprocess.run(
        [sys.executable, "scripts/validate_local_bind.py"], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=10, check=False,
    )
    # Then only the exact string 1 could have authorized it.
    assert result.returncode == 2
    assert "STUDIO_WEB_LAN_OK=1" in result.stderr
    assert result.stdout == ""
