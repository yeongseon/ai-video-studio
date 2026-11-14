"""Regression tests for conservative PR CI gate selection."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.ci_impact import (
    GATES,
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
ORACLE_FULL_GATE_PATHS = (
    "tests/test_restore_drill.py",
    "tests/test_restore_drill_isolation.py",
    "tests/test_restore_database_negative.py",
    "tests/test_restore_secret_fingerprint.py",
    "tests/test_restore_verify.py",
    "tests/test_restore_verify_symlinks.py",
    "tests/test_restore_verify_target.py",
    "tests/fixtures/approved-release-manifest.json",
    "apps/studio-web/src/pages/project/useRunActions.ts",
    "apps/studio-web/src/pages/RunsPage.tsx",
    "apps/studio-web/src/pages/create/useCreateProject.ts",
    "apps/studio-web/src/pages/project/ProjectHeader.tsx",
)


def _gates(**selected: bool) -> dict[str, bool]:
    return {gate: selected.get(gate, False) for gate in GATES}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_docs_only_stays_lightweight():
    assert classify_paths(["README.md", "docs/PRODUCT_DIRECTION.md", "AGENTS.md"]) == {
        "docs": True, "code": False, "restore": False, "first_short": False,
        "worker": False, "docker": False,
    }


def test_frontend_code_requires_every_gate():
    assert classify_paths(["apps/studio-web/src/components/ui/Badge.tsx"]) == ALL_GATES


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


@pytest.mark.parametrize("path", ORACLE_FULL_GATE_PATHS)
def test_restore_and_frontend_boundary_inputs_require_every_gate(path: str) -> None:
    # Given a tracked restore input or state-changing frontend module.
    assert _git(ROOT, "ls-files", "--error-unmatch", path) == path

    # When it is the only changed path, then no filename heuristic reduces validation.
    assert classify_paths([path]) == ALL_GATES


def test_fixtures_and_conftest_files_require_every_gate() -> None:
    # Given every tracked shared fixture and pytest support file.
    paths = _git(ROOT, "ls-files", "tests/fixtures/**", "**/conftest.py").splitlines()

    # When each path is classified, then all six gates are required.
    assert paths
    assert all(classify_paths([path]) == ALL_GATES for path in paths)


def test_mixed_changes_take_union():
    gates = classify_paths([
        "apps/studio-web/src/pages/CreatePage.tsx",
        "apps/worker-orchestrator/tasks/task_runner.py",
    ])
    assert gates["code"] and gates["worker"] and gates["first_short"]


def test_frontend_media_requires_real_runtime():
    gates = classify_paths(["apps/studio-web/src/components/creator/TimelinePreview.tsx"])
    assert gates == ALL_GATES


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


def test_every_tracked_non_documentation_path_requires_every_gate() -> None:
    # Given the complete tracked repository tree outside the canonical docs-only allowlist.
    tracked = _git(ROOT, "ls-files").splitlines()
    non_documentation = [
        path for path in tracked
        if not (
            path.endswith(".md")
            and (path.startswith("docs/") or "/" not in path)
        )
    ]

    # When each path is classified, then every one selects the full six-gate matrix.
    reduced = [path for path in non_documentation if classify_paths([path]) != ALL_GATES]
    assert not reduced, (
        f"{len(non_documentation)} tracked non-documentation paths must select all "
        f"{len(GATES)} gates; reduced={reduced}"
    )


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
    assert gates == _gates(docs=True)


def test_gate_names_are_stable() -> None:
    assert GATES == ("docs", "code", "restore", "first_short", "worker", "docker")
