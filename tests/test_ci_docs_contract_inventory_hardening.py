from __future__ import annotations

import pytest

from ci_docs_contract_inventory_support import directly_covered_paths, docs_reference_matches


@pytest.mark.parametrize(
    "command",
    [
        "python3 -m pytest tests/docs_contract.py --deselect=tests/docs_contract.py::test_policy",
        "python3 -m pytest tests/docs_contract.py -k 'not policy'",
        "python3 -m pytest tests/docs_contract.py --co",
        "python3 -m pytest tests/docs_contract.py --collect-only",
        "python3 -m pytest tests/docs_contract.py || true",
        "python3 -m pytest tests/docs_contract.py | cat",
        "python3 -m pytest tests/docs_contract.py |& cat",
        "python3 -m pytest tests/docs_contract.py &",
        "python3 -m pytest tests/docs_contract.py ; true",
        "python3 -m pytest tests/docs_contract.py && true",
        "python3 -m pytest tests/docs_contract.py 2>&1 | cat",
    ],
)
def test_docs_job_coverage_rejects_masked_pytest_invocations(command: str) -> None:
    # Given an exact docs test path whose execution or failure is masked.
    candidates = {"tests/docs_contract.py"}

    # When docs-job coverage is derived from the unsafe command.
    covered = directly_covered_paths(command, candidates)

    # Then a path that is not guaranteed to execute successfully provides no coverage.
    assert not covered


def test_docs_job_coverage_accepts_chained_direct_pytest_invocations() -> None:
    # Given docs tests chained so the second runs only after the first succeeds.
    candidates = {"tests/docs_a.py", "tests/docs_b.py"}
    command = (
        "python3 -m pytest tests/docs_a.py && "
        "python3 -m pytest tests/docs_b.py"
    )

    # When docs-job coverage is derived from the command.
    covered = directly_covered_paths(command, candidates)

    # Then both direct invocations provide coverage.
    assert covered == candidates


@pytest.mark.parametrize(
    ("path", "reviewed_source", "drifted_source", "new_match"),
    [
        (
            "scripts/ci_impact.py",
            'DOCS_GATE = "docs"',
            'DOCS_GATE = "docs"\nDOCS_POLICY = Path("docs/README.md").read_text()',
            'DOCS_POLICY = Path("docs/README.md").read_text()',
        ),
        (
            "docker-compose.yml",
            "# See docs/SECURITY.md for network policy.",
            "# See docs/SECURITY.md for network policy.\nconfigs:\n  cutover_doc:\n    file: ./docs/CUTOVER.md",
            "file: ./docs/CUTOVER.md",
        ),
    ],
)
def test_allowlisted_docs_matches_change_when_consumer_behavior_drifts(
    path: str,
    reviewed_source: str,
    drifted_source: str,
    new_match: str,
) -> None:
    # Given the reviewed matches for an allowlisted path.
    reviewed = docs_reference_matches(((path, reviewed_source),))[path]

    # When the still-matching path gains a new documentation reference.
    current = docs_reference_matches(((path, drifted_source),))[path]

    # Then match-scoped review detects drift and exposes the new line for review.
    assert current != reviewed
    assert new_match in current
