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
