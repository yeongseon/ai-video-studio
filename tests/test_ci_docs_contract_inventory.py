from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Final

import pytest
import yaml

ROOT: Final = Path(__file__).resolve().parent.parent
CODE_SUFFIXES: Final = frozenset({".py", ".sh", ".js", ".ts", ".tsx", ".yml", ".yaml"})
DOC_TOKEN: Final = re.compile(r"\.md\b|docs/|[\"']docs[\"']|docs\\|README")
DOCS_STEP_NAME: Final = "Validate documentation contracts"

DOCS_JOB_EXECUTED_SCRIPTS: Final = {
    "scripts/check_architecture_diagrams.py": "tests/test_architecture_diagrams.py",
    "scripts/quickstart_smoke.sh": "apps/api/tests/test_quickstart_execution.py",
}

CI_POLICY_ALLOWLIST: Final = {
    "scripts/ci_impact.py": "classifies .md paths and the docs gate; never reads repository docs",
    "scripts/ci_required.py": "uses docs only as a required CI job name",
    "tests/test_ci_impact.py": "uses .md paths only as classifier inputs; never reads doc content",
    "tests/test_ci_impact_git.py": "creates .md git fixtures only to test impact classification",
    "tests/test_ci_required.py": "uses docs only as a required CI job name",
    "tests/test_ci_required_workflow.py": "inspects docs-job wiring; never reads doc content",
}
API_ROUTE_ALLOWLIST: Final = {
    "apps/api/tests/test_auth.py": "tests the FastAPI /docs route; never reads repository docs",
    "apps/studio-web/src/pages/OpsPage.tsx": "links to the FastAPI /docs route; never reads repository docs",
}
INVENTORY_ALLOWLIST: Final = {
    "tests/test_ci_docs_contract_inventory.py": "implements this inventory and its regression fixtures",
}
ALLOWLIST_BY_CATEGORY: Final = {
    "CI policy classifiers": CI_POLICY_ALLOWLIST,
    "API route identifiers": API_ROUTE_ALLOWLIST,
    "Inventory implementation": INVENTORY_ALLOWLIST,
}
ALLOWLIST: Final = {
    path: reason
    for entries in ALLOWLIST_BY_CATEGORY.values()
    for path, reason in entries.items()
}


def _is_scanned_code_path(path: str) -> bool:
    candidate = PurePosixPath(path)
    if candidate.suffix not in CODE_SUFFIXES:
        return False
    parts = candidate.parts
    if parts[0] in {"tests", "scripts"}:
        return True
    if len(parts) >= 3 and parts[0] in {"apps", "packages"} and parts[2] == "tests":
        return True
    if len(parts) >= 3 and parts[0] == "apps" and parts[2] == "src":
        return True
    if len(parts) >= 2 and parts[0] == "packages" and "tests" not in parts[2:]:
        return True
    return len(parts) == 3 and parts[:2] == (".github", "workflows")


def _potential_docs_consumers(files: Iterable[tuple[str, str]]) -> set[str]:
    return {
        path
        for path, text in files
        if _is_scanned_code_path(path) and DOC_TOKEN.search(text)
    }


def _tracked_files() -> tuple[str, ...]:
    output = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    return tuple(path.decode("utf-8") for path in output.split(b"\0") if path)


def _tracked_code_texts() -> tuple[tuple[str, str], ...]:
    return tuple(
        (path, (ROOT / path).read_text(encoding="utf-8"))
        for path in _tracked_files()
        if _is_scanned_code_path(path)
    )


def _docs_job_command() -> str:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["docs"]["steps"]
    matching = [step["run"] for step in steps if step.get("name") == DOCS_STEP_NAME]
    assert len(matching) == 1
    return matching[0]


def _docs_job_covered_paths(command: str, candidates: set[str]) -> set[str]:
    covered = {path for path in candidates if path in command}
    covered.update(
        script
        for script, invoking_test in DOCS_JOB_EXECUTED_SCRIPTS.items()
        if script in candidates and invoking_test in command
    )
    return covered


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("scripts/joinpath.py", 'ROOT.joinpath("docs", "NEW.md").read_text()'),
        ("scripts/formatted.py", 'Path(f"docs/{NAME}.md").read_text()'),
        ("scripts/globbed.py", 'next(Path("docs").glob("*.md")).read_text()'),
        ("scripts/direct.py", 'Path("docs/NEW.md").read_text()'),
        ("scripts/hypothetical_docs_consumer.py", 'Path("README.md").read_text()'),
        ("scripts/hypothetical_docs_consumer.sh", 'grep -q README README.md'),
        ("scripts/after_docstring.py", '\"\"\"Module docs.\"\"\"\nPath("docs/LATER.md").read_text()'),
    ],
)
def test_conservative_detector_flags_every_docs_reference_form(path: str, source: str) -> None:
    # Given one code-like tracked file containing a potential repository-doc reference.
    files = ((path, source),)

    # When the conservative detector scans its path and complete text.
    flagged = _potential_docs_consumers(files)

    # Then syntax shape and earlier docstrings cannot hide the candidate.
    assert flagged == {path}


def test_every_potential_docs_consumer_is_reviewed_or_run_by_docs_job() -> None:
    candidates = _potential_docs_consumers(_tracked_code_texts())
    covered = _docs_job_covered_paths(_docs_job_command(), candidates)

    assert candidates == covered | ALLOWLIST.keys(), (
        f"unclassified potential docs consumers: {sorted(candidates - covered - ALLOWLIST.keys())}; "
        f"stale allowlist entries: {sorted(ALLOWLIST.keys() - candidates)}"
    )
    assert all(reason.strip() for reason in ALLOWLIST.values())


def test_all_tracked_markdown_in_docs_is_valid_utf8() -> None:
    issues: list[str] = []
    docs_files = (
        path
        for path in _tracked_files()
        if path.endswith(".md") and (path.startswith("docs/") or "/" not in path)
    )
    for path in docs_files:
        content = (ROOT / path).read_bytes()
        if b"\0" in content:
            issues.append(f"{path}: contains NUL byte")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as error:
            issues.append(f"{path}: invalid UTF-8 at offset {error.start}: {error.reason}")

    assert not issues, "invalid tracked docs-only markdown:\n  " + "\n  ".join(issues)
