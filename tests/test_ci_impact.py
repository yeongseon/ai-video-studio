"""Regression tests for conservative PR CI gate selection."""
import pytest

from scripts.ci_impact import classify_paths


def test_docs_only_stays_lightweight():
    assert classify_paths(["README.md", "docs/PRODUCT_DIRECTION.md", "AGENTS.md"]) == {
        "code": False, "restore": False, "first_short": False,
        "worker": False, "docker": False,
    }


def test_frontend_code_uses_standard_test_gate():
    assert classify_paths(["apps/studio-web/src/components/ui/Badge.tsx"]) == {
        "code": True, "restore": False, "first_short": False,
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
    assert not any(root_docs.values())
    assert code_docs["code"]
