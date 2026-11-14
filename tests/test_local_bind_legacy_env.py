"""Legacy environment diagnostics through the CLI and real Make recipes."""
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
    "line",
    [
        "STUDIO_WEB_BIND=0.0.0.0", "export STUDIO_WEB_BIND='::' # old setting",
        ' STUDIO_WEB_BIND = "::ffff:0.0.0.0" ',
    ],
    ids=["ipv4", "export-ipv6", "quoted-mapped"],
)
@pytest.mark.parametrize("target", ["docker-local-up", "docker-local-build"])
def test_legacy_wildcard_blocks_make_without_confirmation(tmp_path: Path, line: str, target: str) -> None:
    # Given a legacy wildcard in the working directory, but a safe shell override.
    (tmp_path / ".env").write_text(line + "\n", encoding="utf-8")
    # When the real Make guard runs.
    result, calls = _run_make(tmp_path, target, STUDIO_WEB_BIND="127.0.0.2")
    # Then it diagnoses the legacy line and refuses before Docker.
    assert result.returncode != 0
    assert calls == ""
    assert "WARNING" in result.stderr
    assert ".env" in result.stderr
    assert "STUDIO_WEB_LAN_OK=1" in result.stderr


@pytest.mark.parametrize(
    ("line", "warning"),
    [
        ("# STUDIO_WEB_BIND=0.0.0.0", False),
        ("export STUDIO_WEB_BIND='127.0.0.2' # safe", False),
        ('STUDIO_WEB_BIND="::1"', False),
        ("STUDIO_WEB_BIND=::ffff:127.0.0.1", False),
        ("STUDIO_WEB_BIND=192.168.1.10", True),
        ("STUDIO_WEB_BIND=8.8.8.8", True),
        ("STUDIO_WEB_BIND=0.0.0.0", True),
        ("STUDIO_WEB_BIND=localhost", True),
        ("STUDIO_WEB_BIND=$(touch sentinel)", True),
        ("STUDIO_WEB_BIND=0.0.0.0\nSTUDIO_WEB_BIND=127.0.0.1", True),
    ],
    ids=["comment", "ipv4-loopback", "ipv6-loopback", "mapped-loopback", "lan", "global", "wildcard", "hostname", "shell-text", "duplicate"],
)
def test_legacy_line_is_diagnosed_without_loading_it(tmp_path: Path, line: str, warning: bool) -> None:
    # Given text-only legacy configuration and exact confirmation in the shell.
    (tmp_path / ".env").write_text(line + "\n", encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("STUDIO_WEB_")}
    env["STUDIO_WEB_LAN_OK"] = "1"
    # When invoking the CLI from that directory.
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/validate_local_bind.py")], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=10, check=False,
    )
    # Then the shell default wins and legacy non-loopback lines warn without execution.
    assert result.returncode == 0, result.stderr
    assert result.stdout == "127.0.0.1\n"
    assert ("WARNING" in result.stderr) is warning
    if not warning:
        assert result.stderr == ""
    assert not (tmp_path / "sentinel").exists()


@pytest.mark.parametrize("confirmation", ["", "true", "yes", "01", " 1"])
def test_legacy_wildcard_requires_exact_shell_confirmation(tmp_path: Path, confirmation: str) -> None:
    # Given a legacy file which cannot itself authorize exposure.
    (tmp_path / ".env").write_text("STUDIO_WEB_BIND=0.0.0.0\nSTUDIO_WEB_LAN_OK=1\n", encoding="utf-8")
    # When the shell has a noncanonical confirmation.
    result, calls = _run_make(tmp_path, "docker-local-up", STUDIO_WEB_LAN_OK=confirmation)
    # Then startup is refused before Docker.
    assert result.returncode != 0
    assert calls == ""


@pytest.mark.parametrize("wildcard", ["[::]", "[::ffff:0.0.0.0]", "0.0.0.0", "::"])
@pytest.mark.parametrize("quote", ["", "'", '"'])
@pytest.mark.parametrize("prefix", ["", "export ", "\ufeff"])
@pytest.mark.parametrize("separator", ["=", ": "])
def test_legacy_wildcard_syntax_is_refused(
    tmp_path: Path, wildcard: str, quote: str, prefix: str, separator: str,
) -> None:
    # Given a Compose dotenv spelling of an unconfirmed legacy wildcard.
    line = f"{prefix}STUDIO_WEB_BIND{separator}{quote}{wildcard}{quote}\n"
    (tmp_path / ".env").write_text(line, encoding="utf-8")
    # When the supported Make entry point inspects it.
    result, calls = _run_make(tmp_path, "docker-local-up")
    # Then quoting, brackets, colon or BOM cannot hide the wildcard.
    assert result.returncode != 0
    assert calls == ""
    assert "STUDIO_WEB_LAN_OK=1" in result.stderr


@pytest.mark.parametrize("line", ["STUDIO_WEB_BIND=", "STUDIO_WEB_BIND=''", 'STUDIO_WEB_BIND: ""'])
def test_empty_legacy_value_is_silent(tmp_path: Path, line: str) -> None:
    # Given an empty value that Compose defaults to loopback.
    (tmp_path / ".env").write_text(line + "\n", encoding="utf-8")
    # When Make validates its independent default.
    result, calls = _run_make(tmp_path, "docker-local-up")
    # Then no false exposure warning is emitted.
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert calls == "bind=[127.0.0.1]\n"


@pytest.mark.parametrize("directory", [False, True], ids=["invalid-byte", "directory"])
@pytest.mark.parametrize("bind", ["127.0.0.1", "192.168.1.10"])
def test_uninspectable_env_preserves_bind_validation(tmp_path: Path, directory: bool, bind: str) -> None:
    # Given an advisory file that is not UTF-8 text or cannot be read as a file.
    path = tmp_path / ".env"
    if directory:
        path.mkdir()
    else:
        path.write_bytes(b"X=\xff\n")
    env = {key: value for key, value in os.environ.items() if not key.startswith("STUDIO_WEB_")}
    env["STUDIO_WEB_BIND"] = bind
    # When validating the actual shell bind independently of the scan.
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/validate_local_bind.py")], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=10, check=False,
    )
    # Then the ordinary loopback/LAN decision survives without a traceback.
    assert result.returncode == (0 if bind == "127.0.0.1" else 2), result.stderr
    assert "Traceback" not in result.stderr
    if directory:
        warnings = [line for line in result.stderr.splitlines() if line.startswith("WARNING:")]
        assert len(warnings) == 1
        assert "cannot inspect .env" in warnings[0]
        assert "legacy STUDIO_WEB_BIND check skipped" in warnings[0]


def test_dynamic_legacy_value_warns_without_expansion(tmp_path: Path) -> None:
    # Given an unresolved dotenv expression that is not a literal wildcard.
    (tmp_path / ".env").write_text("STUDIO_WEB_BIND=${H:-0.0.0.0}\n", encoding="utf-8")
    # When Make exports its independently validated loopback.
    result, calls = _run_make(tmp_path, "docker-local-up")
    # Then the advisory scan warns but neither expands nor authorizes the expression.
    assert result.returncode == 0, result.stderr
    assert "WARNING" in result.stderr
    assert calls == "bind=[127.0.0.1]\n"
