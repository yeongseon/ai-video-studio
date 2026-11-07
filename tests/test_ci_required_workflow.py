"""Workflow contract for the stable required CI check."""
from copy import deepcopy
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
AGGREGATOR = "ci-required"


def _workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())


def _assert_complete_aggregator(workflow: dict) -> None:
    jobs = workflow["jobs"]
    required = jobs[AGGREGATOR]
    validation_jobs = set(jobs) - {AGGREGATOR}
    commands = "\n".join(step.get("run", "") for step in required["steps"])

    assert required["if"] == "${{ always() }}"
    assert set(required["needs"]) == validation_jobs
    assert "scripts/ci_required.py" in commands
    for job in validation_jobs - {"impact"}:
        assert f'needs.{job}.result' in commands
    for output in ("docs", "code", "restore", "first_short", "worker", "docker"):
        assert f"needs.impact.outputs.{output}" in commands


def _test_commands(workflow: dict) -> str:
    return "\n".join(
        step.get("run", "")
        for step in workflow["jobs"]["test"]["steps"]
    )


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
    assert "-e packages/creator-domain" in commands
    assert "-e packages/creator-provider" in commands
    assert "-e packages/creator-service" in commands
