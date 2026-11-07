from typing import Final

import pytest

from tests.test_ci_media_runtime_gate import CI_WORKFLOW, _edit, violations

GUARDS: Final = (
    "tests/integration/test_postgres_project_deletion.py",
    "tests/integration/test_postgres_project_deletion_cascade.py",
    "tests/integration/test_postgres_project_deletion_barrier.py",
    "tests/integration/test_postgres_project_deletion_fail_closed.py",
    "tests/integration/test_project_status_migration.py",
    "tests/integration/test_postgres_enum_check_drift.py",
    "tests/integration/test_project_deletion_prerequisites.py",
)


@pytest.mark.parametrize("guard", GUARDS)
def test_project_deletion_guards_run_unconditionally_with_postgres(guard: str) -> None:
    assert violations(CI_WORKFLOW.read_text(), guard) == []


@pytest.mark.parametrize("guard", GUARDS)
@pytest.mark.parametrize("option", ["removed", "-k nothing", "--collect-only", "|| true"])
def test_project_deletion_pin_detects_silent_guard_removal(guard: str, option: str) -> None:
    workflow = CI_WORKFLOW.read_text()
    replacement = "" if option == "removed" else f"{guard} {option}"
    broken = _edit(workflow, guard, replacement)
    assert violations(broken, guard)
