"""The local private-by-default claims must state the Docker Engine they depend on.

Loopback-published ports are only private on Docker Engine >= 28.0.0: Docker documents that older
releases let hosts in the same L2 segment reach ports published to localhost. These are content
checks (AGENTS.md: documentation changes use content/link checks) and run in the CI docs job.
"""
from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

ROOT: Final = Path(__file__).resolve().parents[1]
ENGINE_MINIMUM: Final = "Docker Engine >= 28.0.0"
ENGINE_ISSUE: Final = "https://github.com/moby/moby/issues/45610"
# Every guide that tells the operator published ports stay on the host, with the command that
# first starts services. The prerequisite must come before that command.
LOOPBACK_CLAIMS: Final = (
    ("docs/LOCAL_DOCKER.md", "\nmake docker-local-up\n"),
    ("docs/QUICKSTART.md", "docker compose up -d postgres redis"),
    ("README.md", "\ndocker compose up -d\n"),
    ("docs/SECURITY.md", None),
)


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


@pytest.mark.parametrize(("path", "first_start"), LOOPBACK_CLAIMS)
def test_loopback_claims_state_the_docker_engine_minimum(path: str, first_start: str | None) -> None:
    # Given a guide that tells the operator published ports stay on the host.
    text = _text(path)

    # Then it names the Engine minimum and links the upstream reason for it.
    assert ENGINE_MINIMUM in text, f"{path} claims loopback-only ports without the Engine minimum"
    assert ENGINE_ISSUE in text, f"{path} does not link the upstream issue behind the minimum"
    if first_start is not None:
        # And the operator reads it before being told to start anything.
        assert first_start in text, f"{path} lost its startup command"
        assert text.index(ENGINE_MINIMUM) < text.index(first_start), path


def test_engine_and_compose_versions_are_not_conflated() -> None:
    # Given the local server guide, whose overlay needs a recent Compose.
    text = _text("docs/LOCAL_DOCKER.md")

    # Then it says the Engine and Compose are versioned independently, so a new Compose
    # is not read as proof that the Engine is safe.
    assert "versioned independently" in text
    assert "Docker Compose >= 2.24.4" in text


def test_security_policy_keeps_lan_opt_in_studio_only() -> None:
    # Given the security policy describing the local-server override.
    text = _text("docs/SECURITY.md")

    # Then wildcard Studio exposure is not presented as the default and the API gets no LAN path.
    assert "`studio-web` is exposed on `0.0.0.0`" not in text
    assert "If you need LAN access for the API" not in text
    assert "STUDIO_WEB_BIND" in text, "SECURITY.md does not name the explicit Studio opt-in"


def test_legacy_wildcard_warning_precedes_the_first_startup_command() -> None:
    # Given an existing .env copied from the old example, which direct Compose reads itself.
    text = _text("docs/LOCAL_DOCKER.md")

    # When locating the warning about the old wildcard line and the first startup command.
    warning = text.find("STUDIO_WEB_BIND=0.0.0.0")
    first_start = text.find("\nmake docker-local-up\n")

    # Then the operator is told to delete that line before starting anything.
    assert warning != -1, "LOCAL_DOCKER.md does not warn about the old STUDIO_WEB_BIND=0.0.0.0 line"
    assert first_start != -1, "LOCAL_DOCKER.md lost its startup command"
    assert warning < first_start


def test_local_example_does_not_invite_direct_lan_api_access() -> None:
    # Given the example operators copy to .env.
    text = _text(".env.local.example")

    # Then it does not suggest reaching the loopback-only API from another LAN device.
    assert "another device on the LAN" not in text


def test_lan_examples_always_include_confirmation() -> None:
    # Given the operator-facing guide and copied example.
    for path in ("docs/LOCAL_DOCKER.md", ".env.local.example"):
        text = _text(path)
        # When locating non-loopback startup examples.
        commands = [line for line in text.splitlines() if line.lstrip("# ").startswith("STUDIO_WEB_BIND=") and "make docker-local" in line]
        # Then every non-loopback command includes the exact confirmation.
        assert commands
        for line in commands:
            if "STUDIO_WEB_BIND=127." not in line and "STUDIO_WEB_BIND=::1 " not in line:
                assert "STUDIO_WEB_LAN_OK=1" in line, (path, line)


def test_security_names_guard_limits_and_ci_evidence() -> None:
    # Given the ingress policy.
    text = _text("docs/SECURITY.md")
    # Then enforcement is attributed to Make, not direct Compose, with precise CI evidence.
    for token in (
        "Direct Compose invocations bypass", "global addresses", "STUDIO_WEB_LAN_OK=1",
        "tests/test_local_bind.py", "tests/test_local_compose_bindings.py", "`docs` job",
        "network_mode: host",
    ):
        assert token in text


def test_legacy_bind_regressions_run_in_ci() -> None:
    # Given the workflow responsible for the local ingress contracts.
    workflow = _text(".github/workflows/ci.yml")
    # Then the new legacy-file suite is part of the CI pytest command.
    assert "tests/test_local_bind_legacy_env.py" in workflow


@pytest.mark.parametrize("path", [".env.local.example", ".env.example"])
def test_shipped_admin_key_is_disabled(path: str) -> None:
    # Given the example copied by an operator.
    text = _text(path)
    # When reading the effective admin assignment.
    values = [line.partition("=")[2].strip() for line in text.splitlines() if line.startswith("ADMIN_API_KEY=")]
    # Then it cannot enable the admin API with a publicly known credential.
    assert values == [""]


@pytest.mark.parametrize("path", ["docs/LOCAL_DOCKER.md", "docs/SECURITY.md"])
def test_local_policy_documents_the_explicit_allowlist(path: str) -> None:
    # Given the operator and security descriptions of accepted interfaces.
    text = _text(path)
    # When checking the machine-readable CIDRs against the agreed exposure policy.
    for network in (
        "127.0.0.0/8", "::1", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
        "100.64.0.0/10", "169.254.0.0/16", "fe80::/10", "fc00::/7", "0.0.0.0", "::",
    ):
        # Then neither guide omits an accepted range.
        assert f"`{network}`" in text, (path, network)


def test_security_ci_inventory_names_legacy_scan_suite() -> None:
    # Given the security guide's inventory of CI evidence.
    text = _text("docs/SECURITY.md")
    # When checking the named suite for the advisory file boundary.
    # Then readers can find its dedicated regression evidence.
    assert "tests/test_local_bind_legacy_env.py" in text
