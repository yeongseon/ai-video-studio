"""Regression tests for conservative PR CI gate selection."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts.ci_impact import (
    GATES,
    _parse_raw_diff,
    classify_changes,
    classify_paths,
)


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
ALL_GATES = dict.fromkeys(GATES, True)
REDUCED_GATE_ALLOWLIST = (
    ("docs", {"docs": True}, "reviewed Markdown with regular file modes"),
    ("api tests", {"code": True, "first_short": True}, "API runtime executes API tests"),
    ("worker tests", {"code": True, "worker": True}, "worker runtime executes worker tests"),
    ("package tests", {"code": True}, "code gate executes package unit tests"),
    ("frontend", {"code": True}, "code gate executes frontend unit and build checks"),
    ("frontend runtime", {"code": True, "first_short": True}, "browser runtime covers media UI"),
)


def _gates(**selected: bool) -> dict[str, bool]:
    return {gate: selected.get(gate, False) for gate in GATES}


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
    assert classify_paths(["apps/studio-web/src/components/ui/Badge.tsx"]) == _gates(code=True)


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


def test_every_tracked_backend_production_file_requires_every_gate() -> None:
    tracked = _git(
        ROOT,
        "ls-files",
        "apps/api/src/**",
        "apps/api/migrations/**",
        "apps/worker-orchestrator/**",
        "packages/*/creator_*/**",
        "scripts/**",
    ).splitlines()
    production = [
        path for path in tracked
        if not path.startswith("apps/worker-orchestrator/tests/")
    ]

    assert len(production) >= 300
    assert all(classify_paths([path]) == ALL_GATES for path in production)


@pytest.mark.parametrize(("category", "expected", "reason"), REDUCED_GATE_ALLOWLIST)
def test_reduced_gate_allowlist_categories_are_explicit(
    category: str,
    expected: dict[str, bool],
    reason: str,
) -> None:
    examples = {
        "docs": "docs/QUICKSTART.md",
        "api tests": "apps/api/tests/test_auth.py",
        "worker tests": "apps/worker-orchestrator/tests/test_worker_loop.py",
        "package tests": "packages/creator-domain/tests/test_stage.py",
        "frontend": "apps/studio-web/src/components/ui/Badge.tsx",
        "frontend runtime": "apps/studio-web/src/components/creator/TimelinePreview.tsx",
    }

    assert reason
    assert classify_paths([examples[category]]) == _gates(**expected)


def test_test_only_reduction_is_revoked_by_a_production_path() -> None:
    paths = [
        "packages/creator-domain/tests/test_stage.py",
        "packages/creator-domain/creator_domain/models/stage.py",
    ]

    assert classify_paths(paths) == ALL_GATES


def test_every_tracked_frontend_source_has_an_explicit_risk_class() -> None:
    tracked = _git(ROOT, "ls-files", "apps/studio-web/src/**").splitlines()
    runtime_names = {
        "assetdropzone.tsx", "authorizedmedia.tsx", "medialibrary.tsx",
        "timelinepreview.tsx", "usemediaactions.ts",
    }
    boundary_fragments = (
        "auth", "token", "credential", "secret", "session", "segmentdelete",
        "delete", "apiclient",
    )

    for path in tracked:
        path_lower = path.lower()
        name = Path(path).name.lower()
        if path_lower.startswith("apps/studio-web/src/api/") or name.startswith("client.") or any(
            fragment in path_lower for fragment in boundary_fragments
        ):
            expected = ALL_GATES
        elif name in runtime_names:
            expected = _gates(code=True, first_short=True)
        else:
            expected = _gates(code=True)
        assert classify_paths([path]) == expected, path


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


def test_gate_names_are_stable() -> None:
    assert GATES == ("docs", "code", "restore", "first_short", "worker", "docker")
