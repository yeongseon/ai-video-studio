"""Regression tests for the stable required CI decision."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.ci_required import EXPECTED_JOBS, JobState, required_ci_succeeded


ROOT = Path(__file__).resolve().parents[1]


SELECTED = (
    JobState("docs", True, "success"),
    JobState("hygiene", True, "success"),
    JobState("restore-drill", True, "success"),
    JobState("lint", True, "success"),
    JobState("test", True, "success"),
    JobState("test-worker", True, "success"),
    JobState("first-short-runtime", True, "success"),
    JobState("worker-topology-runtime", True, "success"),
    JobState("docker", True, "success"),
)


def _run_cli(*jobs: str, event: str = "pull_request") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "python3",
            str(ROOT / "scripts/ci_required.py"),
            event,
            "success",
            *jobs,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _complete_jobs(*, selection: str = "true", result: str = "success") -> list[str]:
    return [f"{name}={selection}={result}" for name in EXPECTED_JOBS]


def test_required_ci_succeeds_when_every_selected_job_succeeds() -> None:
    # Given a successful classifier and successful selected jobs.
    # When evaluating the stable required check, then it succeeds.
    assert required_ci_succeeded("success", SELECTED)


@pytest.mark.parametrize("impact_result", ["failure", "cancelled", "skipped"])
def test_required_ci_fails_when_impact_does_not_succeed(impact_result: str) -> None:
    # Given a classifier that did not succeed.
    # When evaluating the stable required check, then it fails closed.
    assert not required_ci_succeeded(impact_result, SELECTED)


@pytest.mark.parametrize("job_result", ["failure", "cancelled", "skipped"])
def test_required_ci_fails_when_selected_job_does_not_succeed(job_result: str) -> None:
    # Given one selected job that did not succeed.
    jobs = (*SELECTED[:-1], JobState("docker", True, job_result))

    # When evaluating the stable required check, then it fails closed.
    assert not required_ci_succeeded("success", jobs)


def test_required_ci_allows_skip_only_when_job_was_not_selected() -> None:
    # Given a successful classifier that explicitly did not select Docker validation.
    jobs = (*SELECTED[:-1], JobState("docker", False, "skipped"))

    # When evaluating the stable required check, then that explicit skip is accepted.
    assert required_ci_succeeded("success", jobs)


def test_required_ci_rejects_unselected_job_that_failed() -> None:
    # Given an unselected job with a failure rather than a GitHub skip.
    jobs = (*SELECTED[:-1], JobState("docker", False, "failure"))

    # When evaluating the stable required check, then the inconsistent state fails closed.
    assert not required_ci_succeeded("success", jobs)


@pytest.mark.parametrize("selection", ["", "maybe", "TRUE"])
def test_pull_request_rejects_noncanonical_selection(selection: str) -> None:
    jobs = _complete_jobs()
    jobs[-1] = f"docker={selection}=skipped"
    assert _run_cli(*jobs).returncode != 0


def test_push_selects_every_job_even_when_impact_output_is_false() -> None:
    jobs = _complete_jobs(selection="false", result="success")
    assert _run_cli(*jobs, event="push").returncode == 0


@pytest.mark.parametrize("result", ["", "maybe", "neutral"])
def test_cli_rejects_unknown_job_result(result: str) -> None:
    jobs = _complete_jobs()
    jobs[-1] = f"docker=true={result}"
    assert _run_cli(*jobs).returncode != 0


def test_cli_rejects_empty_job_name() -> None:
    jobs = _complete_jobs()
    jobs[-1] = "=true=success"
    assert _run_cli(*jobs).returncode != 0


def test_cli_rejects_duplicate_job_name() -> None:
    jobs = _complete_jobs()
    jobs[-1] = jobs[0]
    assert _run_cli(*jobs).returncode != 0


def test_cli_rejects_missing_expected_job() -> None:
    assert _run_cli(*_complete_jobs()[:-1]).returncode != 0


def test_worker_coverage_is_an_expected_job() -> None:
    # Given successful results for the complete validation matrix including Worker.
    jobs = [f"{job.name}=true=success" for job in SELECTED]
    # When the CLI parses the required job set.
    result = _run_cli(*jobs)
    # Then Worker is accepted as part of the exact contract.
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "neutral", ""])
def test_cli_rejects_selected_worker_without_success(result: str) -> None:
    # Given otherwise successful jobs and an unsuccessful or unknown Worker result.
    jobs = [job for job in _complete_jobs() if not job.startswith("test-worker=")]
    jobs.append(f"test-worker=true={result}")
    # When the aggregate is evaluated.
    aggregate = _run_cli(*jobs)
    # Then the selected Worker suite cannot disappear behind a green aggregate.
    assert aggregate.returncode != 0


def test_cli_rejects_missing_worker_coverage() -> None:
    # Given successful results with Worker omitted entirely.
    jobs = [job for job in _complete_jobs() if not job.startswith("test-worker=")]
    # When the CLI checks the exact expected set.
    result = _run_cli(*jobs)
    # Then the missing suite fails closed.
    assert result.returncode != 0


@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_worker_skip_requires_explicit_pr_deselection(event: str) -> None:
    # Given successful jobs with Worker explicitly deselected and skipped.
    jobs = [job for job in _complete_jobs() if not job.startswith("test-worker=")]
    jobs.append("test-worker=false=skipped")
    # When the event-specific selection policy is applied.
    result = _run_cli(*jobs, event=event)
    # Then only a PR may skip Worker; main push still requires success.
    assert (result.returncode == 0) == (event == "pull_request")
