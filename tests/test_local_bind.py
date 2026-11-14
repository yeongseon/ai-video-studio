"""Local helper boundary and Make guard contracts; never start Docker."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from local_make_harness import _run_make

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
    tmp_path: Path, bind: str | None, confirmation: str | None, expected: str,
) -> None:
    # Given only operator-provided exposure settings, not a developer .env.
    env = {key: value for key, value in os.environ.items() if not key.startswith("STUDIO_WEB_")}
    if bind is not None:
        env["STUDIO_WEB_BIND"] = bind
    if confirmation is not None:
        env["STUDIO_WEB_LAN_OK"] = confirmation
    # When invoking the dependency-free validator boundary.
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/validate_local_bind.py")], cwd=tmp_path, env=env,
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
def test_bind_is_rejected_when_invalid_or_unconfirmed(tmp_path: Path, bind: str) -> None:
    # Given an invalid literal or an unconfirmed wildcard.
    env = {key: value for key, value in os.environ.items() if not key.startswith("STUDIO_WEB_")}
    env["STUDIO_WEB_BIND"] = bind
    # When validating the operator input.
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/validate_local_bind.py")], cwd=tmp_path, env=env,
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
def test_global_bind_is_always_refused(tmp_path: Path, bind: str, confirmation: str | None) -> None:
    # Given a public address, even with explicit confirmation.
    env = {key: value for key, value in os.environ.items() if not key.startswith("STUDIO_WEB_")}
    env["STUDIO_WEB_BIND"] = bind
    if confirmation is not None:
        env["STUDIO_WEB_LAN_OK"] = confirmation
    # When invoking the validator.
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/validate_local_bind.py")], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=10, check=False,
    )
    # Then public ingress is refused without a Compose literal.
    assert result.returncode == 2
    assert "global" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("target", ["docker-local-up", "docker-local-build"])
@pytest.mark.parametrize(
    ("assignments", "expected"),
    [
        ("STUDIO_WEB_BIND=192.168.1.10", None),
        ("STUDIO_WEB_BIND=8.8.8.8 STUDIO_WEB_LAN_OK=1", None),
        ("STUDIO_WEB_BIND=fd00::10 STUDIO_WEB_LAN_OK=1", "[fd00::10]"),
        ("STUDIO_WEB_BIND=127.0.0.2", "127.0.0.2"),
    ],
    ids=["unconfirmed", "global", "confirmed-ipv6", "loopback"],
)
def test_make_command_line_binding(tmp_path: Path, target: str, assignments: str, expected: str | None) -> None:
    # Given settings passed as Make command-line assignments rather than environment.
    # When executing the real recipe against the recording Docker stand-in.
    result, calls = _run_make(tmp_path, f"{target} {assignments}")
    # Then the same guard policy and normalized output apply.
    assert (result.returncode == 0) is (expected is not None), result.stderr
    assert calls == (f"bind=[{expected}]\n" if expected is not None else "")


@pytest.mark.parametrize("bind", ["192.168.1.10", "fd00::10", "0.0.0.0", "::"])
@pytest.mark.parametrize("confirmation", ["", "true", "yes", "01", " 1", "1 ", "0"])
def test_confirmation_requires_exact_one(tmp_path: Path, bind: str, confirmation: str) -> None:
    # Given a non-loopback address and a noncanonical confirmation.
    env = {**os.environ, "STUDIO_WEB_BIND": bind, "STUDIO_WEB_LAN_OK": confirmation}
    # When invoking the validator.
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/validate_local_bind.py")], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=10, check=False,
    )
    # Then only the exact string 1 could have authorized it.
    assert result.returncode == 2
    assert "STUDIO_WEB_LAN_OK=1" in result.stderr
    assert result.stdout == ""


DENIED_BINDS = (
    "2002:808:808::1", "2002:7f00:1::", "2001:0:4136:e378::1", "64:ff9b::808:808",
    "192.0.2.1", "198.51.100.1", "203.0.113.1", "2001:db8::1", "198.18.0.1",
    "192.0.0.9", "240.0.0.1", "255.255.255.255", "0.1.2.3", "224.0.0.1", "ff02::1",
    "fec0::1", "::8.8.8.8", "::ffff:8.8.8.8", "8.8.8.8", "2606:4700::1",
)
LAN_RANGE_SAMPLES = (
    ("10.0.0.0", "10.12.34.56", "10.255.255.255"),
    ("172.16.0.0", "172.17.0.1", "172.31.255.255"),
    ("192.168.0.0", "192.168.1.10", "192.168.255.255"),
    ("100.64.0.0", "100.100.100.100", "100.127.255.255"),
    ("169.254.0.0", "169.254.1.2", "169.254.255.255"),
    ("fe80::", "fe80::1", "febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff"),
    ("fc00::", "fd00::10", "fdff:ffff:ffff:ffff:ffff:ffff:ffff:ffff"),
)
LAN_BINDS = (
    *(bind for samples in LAN_RANGE_SAMPLES for bind in samples),
    *(f"::ffff:{bind}" for samples in LAN_RANGE_SAMPLES[:5] for bind in samples),
    "0.0.0.0", "::", "::ffff:0.0.0.0",
)


@pytest.mark.parametrize("bind", DENIED_BINDS)
def test_unlisted_bind_is_refused_even_when_confirmed(tmp_path: Path, bind: str) -> None:
    # Given an address outside the explicit local exposure allowlist.
    env = {**os.environ, "STUDIO_WEB_BIND": bind, "STUDIO_WEB_LAN_OK": "1"}
    # When invoking the actual validator from an isolated directory.
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/validate_local_bind.py")], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=10, check=False,
    )
    # Then confirmation cannot authorize it or produce a Compose binding.
    assert result.returncode == 2, result.stdout
    assert result.stdout == ""


@pytest.mark.parametrize("bind", LAN_BINDS)
@pytest.mark.parametrize("confirmation", [None, "1"])
def test_allowlisted_lan_requires_confirmation(tmp_path: Path, bind: str, confirmation: str | None) -> None:
    # Given first, interior and last addresses of every allowed range, including mapped IPv4.
    env = {key: value for key, value in os.environ.items() if not key.startswith("STUDIO_WEB_")}
    env["STUDIO_WEB_BIND"] = bind
    if confirmation is not None:
        env["STUDIO_WEB_LAN_OK"] = confirmation
    # When validating the explicit selection.
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/validate_local_bind.py")], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=10, check=False,
    )
    # Then exactly confirmed local addresses are accepted, without blanket refusal.
    assert result.returncode == (0 if confirmation == "1" else 2), result.stderr


@pytest.mark.parametrize("bind", ["127.0.0.0", "127.255.255.255", "::ffff:127.255.255.255"])
def test_loopback_endpoints_need_no_confirmation(tmp_path: Path, bind: str) -> None:
    # Given loopback range endpoints without exposure confirmation.
    env = {key: value for key, value in os.environ.items() if not key.startswith("STUDIO_WEB_")}
    env["STUDIO_WEB_BIND"] = bind
    # When invoking the validator.
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/validate_local_bind.py")], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=10, check=False,
    )
    # Then the complete loopback range remains usable.
    assert result.returncode == 0, result.stderr
