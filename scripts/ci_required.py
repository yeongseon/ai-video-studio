"""Fail-closed decision for the repository's stable required CI check."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Final


SUCCESS: Final = "success"
SKIPPED: Final = "skipped"


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


def _parse_job(raw: str) -> JobState:
    name, selected, result = raw.split("=", maxsplit=2)
    return JobState(name=name, selected=selected == "true", result=result)


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: ci_required.py IMPACT_RESULT NAME=SELECTED=RESULT [...]", file=sys.stderr)
        return 2
    jobs = tuple(_parse_job(raw) for raw in sys.argv[2:])
    if required_ci_succeeded(sys.argv[1], jobs):
        return 0
    print(f"required CI failed: impact={sys.argv[1]}, jobs={jobs}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
