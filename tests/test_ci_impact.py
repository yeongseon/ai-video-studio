"""Regression tests for conservative PR CI gate selection."""
from scripts.ci_impact import classify_paths


def test_docs_only_stays_lightweight():
    assert classify_paths(["README.md", "docs/PRODUCT_DIRECTION.md", "AGENTS.md"]) == {
        "code": False, "restore": False, "first_short": False,
        "worker": False, "docker": False,
    }


def test_frontend_code_uses_standard_test_gate():
    assert classify_paths(["apps/studio-web/src/components/creator/TimelineRuler.tsx"]) == {
        "code": True, "restore": False, "first_short": False,
        "worker": False, "docker": False,
    }


def test_render_storage_triggers_media_and_restore():
    gates = classify_paths(["packages/creator-service/creator_service/artifact_storage_integration.py"])
    assert gates["code"] and gates["first_short"] and gates["restore"]
    assert not gates["docker"]


def test_worker_task_triggers_worker_runtime():
    gates = classify_paths(["apps/worker-orchestrator/tasks/render_video.py"])
    assert gates["worker"] and gates["first_short"]


def test_auth_or_workflow_changes_are_full_gate():
    for path in ["apps/api/src/shorts_api/auth.py", ".github/workflows/ci.yml"]:
        assert all(classify_paths([path]).values())


def test_migrations_trigger_restore_and_first_short():
    gates = classify_paths(["apps/api/migrations/versions/051_add_something.py"])
    assert gates["code"] and gates["restore"] and gates["first_short"]


def test_unknown_paths_and_empty_diff_fail_closed():
    assert all(classify_paths(["new-service/main.py"]).values())
    assert all(classify_paths([]).values())


def test_mixed_changes_take_union():
    gates = classify_paths([
        "apps/studio-web/src/pages/CreatePage.tsx",
        "apps/worker-orchestrator/tasks/task_runner.py",
    ])
    assert gates["code"] and gates["worker"] and gates["first_short"]
