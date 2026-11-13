"""Fail-closed decision for the repository's stable required CI check."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Final


SUCCESS: Final = "success"
SKIPPED: Final = "skipped"
EXPECTED_JOBS: Final = (
    "docs",
    "hygiene",
    "restore-drill",
    "lint",
    "test",
    "test-worker",
    "first-short-runtime",
    "worker-topology-runtime",
    "docker",
)
EVENTS: Final = {"pull_request", "push"}
RESULTS: Final = {SUCCESS, "failure", "cancelled", SKIPPED}


@dataclass(frozen=True, slots=True)
class JobState:
    name: str
    selected: bool
    result: str


def required_ci_succeeded(impact_result: str, jobs: tuple[JobState, ...]) -> bool:
    return impact_result == SUCCESS and all(
        job.result == SUCCESS if job.selected else job.result == SKIPPED
        for job in jobs
    )


def _parse_job(raw: str, event: str) -> JobState:
    name, selected, result = raw.split("=", maxsplit=2)
    if not name or result not in RESULTS:
        raise ValueError(raw)
    if event == "pull_request" and selected not in {"true", "false"}:
        raise ValueError(raw)
    return JobState(name=name, selected=event == "push" or selected == "true", result=result)


def main() -> int:
    if len(sys.argv) < 4:
        print(
            "usage: ci_required.py EVENT IMPACT_RESULT NAME=SELECTED=RESULT [...]",
            file=sys.stderr,
        )
        return 2
    event = sys.argv[1]
    try:
        if event not in EVENTS:
            raise ValueError(event)
        jobs = tuple(_parse_job(raw, event) for raw in sys.argv[3:])
    except ValueError as error:
        print(f"invalid required CI input: {error}", file=sys.stderr)
        return 2
    names = tuple(job.name for job in jobs)
    if len(names) != len(set(names)) or set(names) != set(EXPECTED_JOBS):
        print(f"required CI job set mismatch: {names}", file=sys.stderr)
        return 2
    if required_ci_succeeded(sys.argv[2], jobs):
        return 0
    print(f"required CI failed: impact={sys.argv[2]}, jobs={jobs}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
