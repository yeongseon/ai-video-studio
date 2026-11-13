"""Workflow contract for the stable required CI check."""
import re
import shlex
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from scripts.ci_impact import GATES, classify_paths
from scripts.ci_required import EXPECTED_JOBS

ROOT = Path(__file__).resolve().parents[1]
AGGREGATOR = "ci-required"
DOCS_STEP_NAME = "Validate documentation contracts"


def _workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())


def _assert_complete_aggregator(workflow: dict) -> None:
    jobs = workflow["jobs"]
    required = jobs[AGGREGATOR]
    validation_jobs = set(jobs) - {AGGREGATOR}
    commands = "\n".join(step.get("run", "") for step in required["steps"])

    assert required["if"] == "${{ always() }}"
    assert set(required["needs"]) == validation_jobs
    assert validation_jobs - {"impact"} == set(EXPECTED_JOBS)
    assert "scripts/ci_required.py" in commands
    for job in validation_jobs - {"impact"}:
        assert f'needs.{job}.result' in commands
    for output in ("docs", "code", "restore", "first_short", "worker", "docker"):
        assert f"needs.impact.outputs.{output}" in commands


def _worker_aggregate(workflow: dict, *, selected: str = "true") -> int:
    required = workflow["jobs"][AGGREGATOR]
    command = required["steps"][1]["run"]
    values = {"github.event_name": "pull_request"}
    values.update({f"needs.{name}.result": "success" for name in required["needs"]})
    values.update({
        f"needs.impact.outputs.{gate}": selected
        for gate in GATES
    })
    rendered = re.sub(r"\$\{\{\s*(.*?)\s*\}\}", lambda m: values.get(m[1], ""), command)
    return subprocess.run(shlex.split(rendered), cwd=ROOT, check=False, capture_output=True).returncode


def _test_commands(workflow: dict) -> str:
    return "\n".join(
        step.get("run", "")
        for step in workflow["jobs"]["test"]["steps"]
    )


def _assert_docs_job_has_no_failure_mask(workflow: dict) -> None:
    docs = workflow["jobs"]["docs"]
    matching = [step for step in docs["steps"] if step.get("name") == DOCS_STEP_NAME]
    assert len(matching) == 1
    assert "continue-on-error" not in docs
    assert "continue-on-error" not in matching[0]


def test_ci_required_aggregates_every_validation_job() -> None:
    # Given the repository CI workflow.
    workflow = _workflow()

    # When validating the stable required job, then every validation job is covered.
    _assert_complete_aggregator(workflow)


@pytest.mark.parametrize("mutation", ["needs", "command"])
def test_ci_required_contract_rejects_an_uncovered_job(mutation: str) -> None:
    # Given the repository workflow with a newly added validation job.
    workflow = deepcopy(_workflow())
    workflow["jobs"]["new-validation"] = {"runs-on": "ubuntu-latest", "steps": []}
    if mutation == "needs":
        workflow["jobs"][AGGREGATOR]["needs"].append("new-validation")
    else:
        workflow["jobs"][AGGREGATOR]["steps"][1]["run"] += (
            ' "new-validation=true=${{ needs.new-validation.result }}"'
        )

    # When only one aggregator surface is updated, then the contract fails closed.
    with pytest.raises(AssertionError):
        _assert_complete_aggregator(workflow)


def test_every_root_ci_policy_test_is_invoked_by_workflow() -> None:
    commands = _test_commands(_workflow())
    for test_file in sorted((ROOT / "tests").glob("test_ci_*.py")):
        assert test_file.relative_to(ROOT).as_posix() in commands


def test_docs_job_runs_documentation_contract_checks() -> None:
    workflow = _workflow()
    docs = workflow["jobs"]["docs"]
    commands = "\n".join(step.get("run", "") for step in docs["steps"])

    assert "needs.impact.outputs.docs == 'true'" in docs["if"]
    assert "tests/test_architecture_diagrams.py" in commands
    assert "apps/api/tests/test_quickstart_docs.py" in commands
    assert "apps/api/tests/test_quickstart_execution.py" in commands
    assert "apps/api/tests/test_first_short_walkthrough.py" in commands
    assert "apps/api/tests/test_cutover_doc_contract.py" in commands
    assert "tests/test_ci_docs_contract_inventory.py" in commands
    assert "tests/test_local_compose_bindings.py" in commands
    assert "tests/test_local_bind.py" in commands
    assert "tests/test_local_docs_contract.py" in commands
    assert "tests/test_local_storage_migration.py" in commands
    assert "rsync --version" in commands
    assert "sha256sum --version" in commands
    assert "-e packages/creator-domain" in commands
    assert "-e packages/creator-provider" in commands
    assert "-e packages/creator-service" in commands
    _assert_docs_job_has_no_failure_mask(workflow)


def test_local_storage_compose_checks_are_invoked() -> None:
    # Given the workflow validation jobs.
    workflow = _workflow()
    # When checking storage inspection coverage.
    # Then CI runs real config inspection.
    assert "tests/test_local_storage_compose.py" in _test_commands(workflow)


@pytest.mark.parametrize("scope", ["job", "step"])
@pytest.mark.parametrize(
    "value",
    [True, "${{ true }}", "${{ always() }}", "true", "${{ github.event_name == 'push' }}"],
)
def test_docs_job_rejects_continue_on_error(scope: str, value: bool | str) -> None:
    # Given a docs workflow that masks either the whole job or its validation step.
    workflow = deepcopy(_workflow())
    docs = workflow["jobs"]["docs"]
    if scope == "job":
        docs["continue-on-error"] = value
    else:
        step = next(step for step in docs["steps"] if step.get("name") == DOCS_STEP_NAME)
        step["continue-on-error"] = value

    # When the docs contract is checked, then masked failures are rejected.
    with pytest.raises(AssertionError):
        _assert_docs_job_has_no_failure_mask(workflow)


def test_worker_coverage_moves_without_changing_test_selection() -> None:
    # Given the repository workflow after the parallel extraction.
    workflow = _workflow()
    worker = workflow["jobs"]["test-worker"]
    # When inspecting the only Worker coverage invocation.
    coverage = [step for step in worker["steps"] if "--cov=tasks" in step.get("run", "")]
    # Then the whole suite and original coverage threshold run outside the monolith.
    assert "--cov=tasks" not in _test_commands(workflow)
    assert len(coverage) == 1
    assert coverage[0]["run"] == (
        "cd apps/worker-orchestrator && python3 -m pytest tests/ -v "
        "--cov=tasks --cov-report=term-missing --cov-fail-under=60"
    )
    assert "if" not in coverage[0]
    assert "continue-on-error" not in coverage[0]
    assert "continue-on-error" not in worker


def test_parallel_worker_keeps_database_media_and_python_prerequisites() -> None:
    # Given Worker tests that use PostgreSQL migrations and real media tools.
    jobs = _workflow()["jobs"]
    worker = jobs["test-worker"]
    commands = "\n".join(step.get("run", "") for step in worker["steps"])
    # When inspecting the extracted job's independent setup.
    actions = [step.get("uses", "") for step in worker["steps"]]
    # Then no inherited prerequisite is silently lost or frontend install added.
    assert worker["needs"] == "impact"
    assert worker["services"] == jobs["test"]["services"]
    assert worker["env"] == jobs["test"]["env"]
    assert "actions/checkout@v4" in actions
    assert "actions/setup-python@v5" in actions
    assert not any("setup-node" in action for action in actions)
    assert "npm" not in commands
    assert "ffmpeg -version" in commands and "ffprobe -version" in commands
    for manifest in (
        "apps/api/requirements.txt", "apps/api/requirements-dev.txt",
        "apps/worker-orchestrator/requirements.txt", "apps/worker-orchestrator/requirements-dev.txt",
    ):
        assert f"-r {manifest}" in commands
    for package in ("creator-domain", "creator-provider", "creator-service"):
        assert f"-e packages/{package}" in commands
    assert "-c constraints.txt" in commands


def test_worker_coverage_uses_existing_worker_gate_on_pr_and_always_on_main() -> None:
    # Given the Worker coverage job and required-check command.
    workflow = _workflow()
    worker = workflow["jobs"]["test-worker"]
    command = workflow["jobs"][AGGREGATOR]["steps"][1]["run"]
    # When comparing dispatch selection with aggregate selection.
    expected = "${{ github.event_name != 'pull_request' || needs.impact.outputs.worker == 'true' }}"
    # Then both use the same gate, with unconditional push selection.
    assert worker["if"] == expected
    assert '"test-worker=${{ needs.impact.outputs.worker }}=${{ needs.test-worker.result }}"' in command
    assert _worker_aggregate(workflow) == 0


def test_main_push_keeps_every_validation_job_selected() -> None:
    # Given the complete workflow including the parallel Worker job.
    workflow = _workflow()
    # When inspecting each impact-selected validation job's push dispatch.
    conditions = [
        job["if"] for name, job in workflow["jobs"].items()
        if name not in {"impact", "hygiene", AGGREGATOR}
    ]
    # Then every job bypasses PR-only gate outputs on main push.
    assert all(condition.startswith("${{ github.event_name != 'pull_request' || ") for condition in conditions)


@pytest.mark.parametrize("mutation", ["needs", "incompatible-job"])
def test_worker_aggregate_fails_when_wiring_loses_required_result(mutation: str) -> None:
    # Given a workflow whose Worker edge or result source is broken.
    workflow = deepcopy(_workflow())
    required = workflow["jobs"][AGGREGATOR]
    if mutation == "needs":
        required["needs"].remove("test-worker")
    else:
        required["steps"][1]["run"] = required["steps"][1]["run"].replace(
            "needs.test-worker.result", "needs.incompatible-job.result",
        )
    # When the actual aggregate command is rendered with missing needs as empty.
    result = _worker_aggregate(workflow)
    # Then the CLI rejects the lost required result rather than reporting green.
    assert result != 0


@pytest.mark.parametrize("path", [
    "apps/worker-orchestrator/tasks/render_video.py",
    "apps/api/src/shorts_api/auth.py",
    "packages/creator-service/creator_service/task_dispatch_service.py",
    "packages/creator-domain/creator_domain/task_dispatch.py",
    "apps/api/migrations/versions/051_add_something.py",
    ".github/workflows/ci.yml", "scripts/ci_required.py", "new-service/unknown.py",
])
def test_worker_coverage_selected_for_protected_and_unknown_paths(path: str) -> None:
    # Given a PR touching Worker, shared execution, or a protected/unknown boundary.
    gates = classify_paths([path])
    # When the workflow dispatches Worker coverage using the existing worker gate.
    selection = _workflow()["jobs"]["test-worker"]["if"]
    # Then full validation remains selected and the job consumes that selection.
    assert all(gates.values())
    assert "needs.impact.outputs.worker == 'true'" in selection
