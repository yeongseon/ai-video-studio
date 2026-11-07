"""Git-boundary regression tests for conservative PR CI gate selection."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts.ci_impact import _parse_raw_diff, classify_changes


ROOT = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "ci@example.invalid")
    _git(repo, "config", "user.name", "CI Test")
    (repo / "apps/api/src/shorts_api").mkdir(parents=True)
    (repo / "apps/api/src/shorts_api/auth.py").write_text("AUTH = True\n")
    (repo / "README.md").write_text("# Repository\n")
    (repo / "docs").mkdir()
    (repo / "docs/guide.md").write_text("# Guide\n")
    return repo, _commit(repo, "baseline")


def _classify_git(repo: Path, base: str, head: str, tmp_path: Path) -> dict[str, bool]:
    output = tmp_path / "github-output"
    env = {**os.environ, "GITHUB_OUTPUT": str(output)}
    subprocess.run(
        ["python3", str(ROOT / "scripts/ci_impact.py"), base, head],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return {
        name: value == "true"
        for name, value in (
            line.split("=", maxsplit=1) for line in output.read_text().splitlines()
        )
    }


def test_deleted_protected_path_is_not_hidden_by_docs_edit(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    (repo / "apps/api/src/shorts_api/auth.py").unlink()
    (repo / "README.md").write_text("# Updated\n")
    head = _commit(repo, "delete protected file")

    assert all(_classify_git(repo, base, head, tmp_path).values())


def test_protected_path_renamed_to_docs_keeps_old_boundary(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    (repo / "apps/api/src/shorts_api/auth.py").rename(repo / "docs/auth.md")
    head = _commit(repo, "rename protected file")

    assert all(_classify_git(repo, base, head, tmp_path).values())


def test_ordinary_docs_rename_remains_docs_only(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    (repo / "docs/guide.md").rename(repo / "docs/renamed.md")
    head = _commit(repo, "rename docs")

    gates = _classify_git(repo, base, head, tmp_path)
    assert gates["docs"] and sum(gates.values()) == 1


def test_docs_symlink_fails_closed(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    (repo / "docs/link.md").symlink_to("guide.md")
    head = _commit(repo, "add docs symlink")

    assert all(_classify_git(repo, base, head, tmp_path).values())


def test_docs_executable_mode_change_fails_closed(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    guide = repo / "docs/guide.md"
    guide.chmod(guide.stat().st_mode | 0o111)
    head = _commit(repo, "make docs executable")

    assert all(_classify_git(repo, base, head, tmp_path).values())


def test_docs_gitlink_add_fails_closed(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    _git(repo, "update-index", "--add", "--cacheinfo", "160000", base, "docs/module.md")
    head = _git(repo, "commit", "-m", "add docs gitlink") and _git(repo, "rev-parse", "HEAD")

    assert all(_classify_git(repo, base, head, tmp_path).values())


def test_regular_docs_file_replaced_by_gitlink_fails_closed(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    _git(repo, "rm", "--cached", "docs/guide.md")
    (repo / "docs/guide.md").unlink()
    _git(repo, "update-index", "--add", "--cacheinfo", "160000", base, "docs/guide.md")
    head = _git(repo, "commit", "-m", "replace docs file with gitlink") and _git(repo, "rev-parse", "HEAD")

    assert all(_classify_git(repo, base, head, tmp_path).values())


def test_protected_source_copy_to_docs_keeps_source_boundary(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    (repo / "docs/auth-copy.md").write_text(
        (repo / "apps/api/src/shorts_api/auth.py").read_text(),
    )
    head = _commit(repo, "copy protected source into docs")

    assert all(_classify_git(repo, base, head, tmp_path).values())


def test_docs_copy_with_regular_modes_remains_docs_only(tmp_path: Path) -> None:
    repo, base = _repository(tmp_path)
    (repo / "docs/guide-copy.md").write_text((repo / "docs/guide.md").read_text())
    head = _commit(repo, "copy docs")

    gates = _classify_git(repo, base, head, tmp_path)
    assert gates["docs"] and sum(gates.values()) == 1


@pytest.mark.parametrize("name", ["space name.md", "한글.md", "line\nbreak.md"])
def test_docs_names_with_special_characters_remain_docs_only(
    tmp_path: Path,
    name: str,
) -> None:
    repo, base = _repository(tmp_path)
    (repo / "docs" / name).write_text("# Safe name\n")
    head = _commit(repo, "add specially named docs")

    gates = _classify_git(repo, base, head, tmp_path)
    assert gates["docs"] and sum(gates.values()) == 1


@pytest.mark.parametrize("status", ["X", "R", "C100extra"])
def test_malformed_or_unknown_raw_status_fails_closed(status: str) -> None:
    raw = f":100644 100644 {'0' * 40} {'1' * 40} {status}\0docs/guide.md\0"

    with pytest.raises(SystemExit, match="Malformed git diff --raw"):
        classify_changes(_parse_raw_diff(raw))


@pytest.mark.parametrize("status", ["C101", "C999", "R000", "C1", "C0100", "Cxx"])
def test_invalid_copy_or_rename_similarity_score_fails_closed(status: str) -> None:
    raw = (
        f":100644 100644 {'0' * 40} {'1' * 40} {status}\0"
        "docs/guide.md\0docs/copied.md\0"
    )

    with pytest.raises(SystemExit, match="Malformed git diff --raw status"):
        _parse_raw_diff(raw)
