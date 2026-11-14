"""Exercise the real shell entrypoint with synthetic files and command stand-ins."""

from pathlib import Path
import shutil
import subprocess
import sys
from typing import Final

import pytest

ROOT: Final = Path(__file__).resolve().parents[1]
SCRIPT: Final = "scripts/cutover_runtime_acceptance.sh"
SENTINEL: Final = b"API_KEY=operator-synthetic\r\nARTIFACTS_HOST_DIR=/operator/media\n"


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(ROOT / SCRIPT, tmp_path / SCRIPT)
    (tmp_path / ".env.example").write_text("API_KEY=example\n", encoding="utf-8")
    (tmp_path / "bin").mkdir()
    (tmp_path / "state").mkdir()
    commands = {
        "docker": '''printf '%s\\n' "$*" >> calls
if [ "$*" = 'compose up -d postgres redis' ]; then
  stat -c %i .env > initial-inode
fi
if [ "$*" = 'compose up -d api worker beat studio-web' ]; then
  /bin/cp .env observed-env
  stat -c %i .env > rewritten-inode
  exit 73
fi
case "$*" in
  *create_api_key.py*) printf '%s\\n' '{"api_key":"synthetic-created","workspace_id":1}' ;;
  *'exec -T postgres'*) printf '1\\n' ;;
esac
''',
        "cp": '''printf 'cp %s\\n' "$*" >> file-calls
exec /bin/cp "$@"
''',
        "mv": '''[ ! -e .env ] || printf 'existing\\n' >> publication
printf '%s\\n' "$*" >> publication
exec /bin/mv "$@"
''',
        "ln": '''printf '%s\\n' "$*" >> publication
exec /bin/ln "$@"
''',
        "python3": f'exec "{sys.executable}" "$@"\n',
        "timeout": "exit 0\n",
        "curl": "exit 91\n",
        "make": "exit 92\n",
    }
    for name, body in commands.items():
        executable = tmp_path / "bin" / name
        executable.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        executable.chmod(0o755)
    return tmp_path


def _run(checkout: Path, disposable: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", SCRIPT], cwd=checkout,
        env={"PATH": f"{checkout}/bin:/usr/bin:/bin", "TMPDIR": str(checkout / "state"),
             "CUTOVER_ACCEPTANCE_DISPOSABLE": disposable},
        capture_output=True, text=True, timeout=10, check=False,
    )


@pytest.mark.parametrize("opt_in", ["", "0", "true"])
def test_refuses_before_any_side_effect_when_operator_env_exists(checkout: Path, opt_in: str) -> None:
    # Given an operator's working environment and no exact disposable opt-in.
    dotenv = checkout / ".env"
    dotenv.write_bytes(SENTINEL)
    inode = dotenv.stat().st_ino
    # When invoking the real harness through fake external commands.
    result = _run(checkout, opt_in)
    # Then refusal happens before copying secrets, Compose, or state allocation.
    assert result.returncode != 0
    assert dotenv.read_bytes() == SENTINEL
    assert dotenv.stat().st_ino == inode
    assert not (checkout / "calls").exists()
    assert not (checkout / "file-calls").exists()
    assert list((checkout / "state").iterdir()) == []
    assert sorted(path.name for path in checkout.glob(".env*")) == [".env", ".env.example"]
    guidance = result.stderr.lower()
    assert all(term in guidance for term in (".env", "fresh clone", "git worktree", "move", "rename"))


@pytest.mark.parametrize("entry", ["symlink", "dangling", "directory"])
def test_refuses_when_env_is_an_existing_filesystem_entry(checkout: Path, entry: str) -> None:
    # Given an existing entry, including a symlink whose target is absent.
    dotenv = checkout / ".env"
    if entry == "directory":
        dotenv.mkdir()
    else:
        target = checkout / "operator-env"
        if entry == "symlink":
            target.write_bytes(SENTINEL)
        dotenv.symlink_to(target)
    # When starting without permission to replace it.
    result = _run(checkout)
    # Then it is refused before external operations and the entry survives intact.
    assert result.returncode != 0
    assert not (checkout / "calls").exists()
    assert dotenv.is_dir() if entry == "directory" else dotenv.is_symlink()
    if entry == "symlink":
        assert dotenv.read_bytes() == SENTINEL


@pytest.mark.parametrize(("existing", "opt_in"), [(False, ""), (False, "1"), (True, "1")])
def test_publishes_complete_env_atomically_and_cleans_up_when_disposable(
    checkout: Path, existing: bool, opt_in: str,
) -> None:
    # Given a disposable checkout, with optional explicitly replaceable configuration.
    if existing:
        (checkout / ".env").write_bytes(SENTINEL)
    (checkout / "unrelated").write_text("keep", encoding="utf-8")
    # When fake Docker stops the harness immediately after the real API-key rewrite.
    result = _run(checkout, opt_in)
    # Then both publications use a new inode; only harness-owned temporary files disappear.
    assert result.returncode == 73, result.stderr
    assert (checkout / "observed-env").read_text() == (
        "API_KEY=synthetic-created\n\nARTIFACT_ROOT=/tmp/cutover-artifacts\n"
    )
    publication = (checkout / "publication").read_text().splitlines()
    assert publication[-1].endswith(" .env")
    assert publication[-1].startswith("-T " if opt_in else "-- ")
    assert ("existing" in publication) == existing
    assert (checkout / "initial-inode").read_text() != (checkout / "rewritten-inode").read_text()
    file_calls = (checkout / "file-calls").read_text() if (checkout / "file-calls").exists() else ""
    assert "cp .env " not in file_calls
    assert not (checkout / ".env").exists()
    assert sorted(path.name for path in checkout.glob(".env*")) == [".env.example"]
    assert list((checkout / "state").iterdir()) == []
    assert (checkout / "unrelated").read_text() == "keep"


def test_refuses_when_env_appears_during_publication(checkout: Path) -> None:
    # Given another writer arriving after the guard, at either publication command.
    for command in ("ln", "mv"):
        (checkout / "bin" / command).write_text(
            f"#!/bin/sh\nprintf 'API_KEY=racing-synthetic\\n' > .env\n"
            f"stat -c %i .env > racing-inode\nexec /bin/{command} \"$@\"\n",
        )
    # When the harness attempts to publish its complete environment.
    result = _run(checkout)
    # Then the competing environment survives and no Compose operation runs.
    assert result.returncode != 0
    assert (checkout / ".env").read_text() == "API_KEY=racing-synthetic\n"
    assert str((checkout / ".env").stat().st_ino) == (checkout / "racing-inode").read_text().strip()
    assert "refusing: .env appeared" in result.stderr.lower()
    assert all(term in result.stderr for term in ("fresh clone", "git worktree", "move/rename"))
    assert not (checkout / "calls").exists()
    assert list(checkout.glob(".env-cutover-*")) == []
    assert list((checkout / "state").iterdir()) == []
    calls = (checkout / "calls").read_text() if (checkout / "calls").exists() else ""
    assert "compose up" not in calls


@pytest.mark.parametrize("opt_in", ["", "1"])
@pytest.mark.parametrize("signal", ["INT", "TERM"])
@pytest.mark.parametrize("published", [False, True])
def test_cleans_owned_files_when_publication_is_interrupted(
    checkout: Path, opt_in: str, signal: str, published: bool,
) -> None:
    # Given a signal arriving inside the external publication operation.
    command = "mv" if opt_in else "ln"
    operation = f'/bin/{command} "$@"\n' if published else ""
    (checkout / "bin" / command).write_text(
        f'#!/bin/sh\n{operation}kill -{signal} "$PPID"\n'
        f'exit {0 if published else 1}\n',
    )
    # When Bash receives the signal before recording command completion.
    result = _run(checkout, opt_in)
    # Then interruption fails and neither a published env nor a temp secret remains.
    assert result.returncode != 0
    assert not (checkout / ".env").exists()
    assert list(checkout.glob(".env-cutover-*")) == []
    assert list((checkout / "state").iterdir()) == []
    calls = (checkout / "calls").read_text() if (checkout / "calls").exists() else ""
    assert "compose up" not in calls


def test_ci_declares_disposable_checkout_when_running_acceptance() -> None:
    # Given the docker job, which already creates .env during config checks.
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    # When selecting the step that executes this harness.
    step = workflow.split("      - name: Runtime cutover acceptance\n", 1)[1].split("\n\n", 1)[0]
    # Then only this disposable CI invocation opts into replacement.
    assert 'CUTOVER_ACCEPTANCE_DISPOSABLE: "1"' in step
    assert f"run: {SCRIPT}" in step
    assert "tests/test_local_acceptance_env.py tests/test_local_dockerignore.py" in workflow
