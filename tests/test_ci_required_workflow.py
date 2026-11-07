"""Workflow contract for the stable required CI check."""
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
GATED_JOBS = {
    "impact",
    "hygiene",
    "restore-drill",
    "lint",
    "test",
    "first-short-runtime",
    "worker-topology-runtime",
    "docker",
}


def test_ci_required_aggregates_every_validation_job() -> None:
    # Given the repository CI workflow.
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())

    # When reading the stable required job.
    required = workflow["jobs"]["ci-required"]

    # Then it always evaluates all validation dependencies through the tested decision script.
    assert required["if"] == "${{ always() }}"
    assert set(required["needs"]) == GATED_JOBS
    commands = "\n".join(step.get("run", "") for step in required["steps"])
    assert "scripts/ci_required.py" in commands
