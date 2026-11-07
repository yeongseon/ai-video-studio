"""Regression tests for conservative PR CI gate selection."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts.ci_impact import GATES, classify_paths


ROOT = Path(__file__).resolve().parents[1]
FULL_GATE_PATHS = (
    "apps/api/src/shorts_api/task_dispatch_adapter.py",
    "packages/creator-domain/creator_domain/task_dispatch.py",
    "packages/creator-service/creator_service/task_dispatch_service.py",
    "scripts/cutover_runtime_acceptance.sh",
    "scripts/release_lifecycle_acceptance.sh",
    "scripts/build_release_manifest.py",
    "apps/api/Dockerfile",
    "apps/studio-web/Dockerfile",
    "apps/worker-orchestrator/Dockerfile",
    "docker-compose.yml",
    "docker-compose.scaled-workers.yml",
    "apps/studio-web/nginx.conf.template",
)


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


def test_docs_only_stays_lightweight():
    assert classify_paths(["README.md", "docs/PRODUCT_DIRECTION.md", "AGENTS.md"]) == {
        "docs": True, "code": False, "restore": False, "first_short": False,
        "worker": False, "docker": False,
    }


def test_frontend_code_uses_standard_test_gate():
    assert classify_paths(["apps/studio-web/src/components/ui/Badge.tsx"]) == {
        "docs": False, "code": True, "restore": False, "first_short": False,
        "worker": False, "docker": False,
    }


def test_render_storage_triggers_media_and_restore():
    gates = classify_paths(["packages/creator-service/creator_service/artifact_storage_integration.py"])
    assert all(gates.values())


def test_worker_task_triggers_worker_runtime():
    gates = classify_paths(["apps/worker-orchestrator/tasks/render_video.py"])
    assert gates["worker"] and gates["first_short"]


def test_auth_or_workflow_changes_are_full_gate():
    for path in [
        "apps/api/src/shorts_api/auth.py",
        ".github/workflows/ci.yml",
        "scripts/ci_required.py",
        "tests/test_ci_required.py",
        "tests/test_ci_required_workflow.py",
    ]:
        assert all(classify_paths([path]).values())


def test_migrations_trigger_restore_and_first_short():
    gates = classify_paths(["apps/api/migrations/versions/051_add_something.py"])
    assert all(gates.values())


def test_unknown_paths_and_empty_diff_fail_closed():
    assert all(classify_paths(["new-service/main.py"]).values())
    assert all(classify_paths([]).values())


def test_mixed_changes_take_union():
    gates = classify_paths([
        "apps/studio-web/src/pages/CreatePage.tsx",
        "apps/worker-orchestrator/tasks/task_runner.py",
    ])
    assert gates["code"] and gates["worker"] and gates["first_short"]


def test_frontend_media_requires_real_runtime():
    gates = classify_paths(["apps/studio-web/src/components/creator/TimelinePreview.tsx"])
    assert gates["code"] and gates["first_short"] and not gates["restore"]


def test_frontend_auth_identity_requires_full_gate():
    assert all(classify_paths(["apps/studio-web/src/api/session.ts"]).values())


def test_release_manifest_scripts_feed_restore_drill_and_docker():
    for path in ["scripts/build_release_manifest.py", "scripts/release_manifest.py"]:
        gates = classify_paths([path])
        assert gates["restore"] and gates["docker"], path


@pytest.mark.parametrize("path", FULL_GATE_PATHS)
def test_real_queue_release_and_container_boundaries_require_every_gate(path: str) -> None:
    # Given a real tracked queue, release, cutover, or container-governance input.
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", path],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    # When classifying the path, then the fixture is real and every gate is required.
    assert tracked.returncode == 0, path
    assert all(classify_paths([path]).values()), path


@pytest.mark.parametrize(
    "path",
    [
        "apps/api/src/shorts_api/routes/release_identity.py",
        "apps/api/src/shorts_api/routes/creator_projects.py",
        "apps/studio-web/src/components/creator/segmentDelete.ts",
        "apps/worker-orchestrator/worker_release_identity.py",
        "apps/api/migrations/versions/051_allow_deleting_project_status.py",
        "packages/creator-service/creator_service/object_storage.py",
        "packages/creator-service/creator_service/project_deletion.py",
        "packages/creator-service/creator_service/broker_recovery.py",
        "apps/worker-orchestrator/celery_app.py",
        "apps/studio-web/nginx.conf.template",
        ".env.example",
    ],
)
def test_protected_governance_boundaries_require_every_gate(path: str) -> None:
    # Given a real path that can change a protected production boundary.
    # When classifying that path for pull-request validation.
    gates = classify_paths([path])

    # Then classification fails closed to every gate.
    assert all(gates.values()), (path, gates)


def test_markdown_is_docs_only_only_in_docs_or_repository_root() -> None:
    # Given markdown in the two reviewed documentation locations and in code.
    root_docs = classify_paths(["REVIEWING.md", "docs/operations/CI.md"])
    code_docs = classify_paths(["apps/api/src/shorts_api/routes/SECURITY.md"])

    # When comparing their classifications, then only the reviewed locations are lightweight.
    assert root_docs["docs"] and sum(root_docs.values()) == 1
    assert code_docs["code"]


@pytest.mark.parametrize("path", ["DOCS/x.md", "docs/../protected.md"])
def test_noncanonical_documentation_paths_fail_closed(path: str) -> None:
    assert all(classify_paths([path]).values())


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "docs/architecture/SYSTEM_TOPOLOGY.md",
        "docs/QUICKSTART.md",
        "docs/FIRST_SHORT_WALKTHROUGH.md",
    ],
)
def test_repository_consumed_markdown_selects_docs_gate(path: str) -> None:
    gates = classify_paths([path])
    assert gates["docs"]


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


def test_gate_names_are_stable() -> None:
    assert GATES == ("docs", "code", "restore", "first_short", "worker", "docker")
