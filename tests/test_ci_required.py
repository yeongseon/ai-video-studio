"""Regression tests for the stable required CI decision."""
from __future__ import annotations

import pytest

from scripts.ci_required import JobState, required_ci_succeeded


SELECTED = (
    JobState("hygiene", True, "success"),
    JobState("restore-drill", True, "success"),
    JobState("lint", True, "success"),
    JobState("test", True, "success"),
    JobState("first-short-runtime", True, "success"),
    JobState("worker-topology-runtime", True, "success"),
    JobState("docker", True, "success"),
)


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
