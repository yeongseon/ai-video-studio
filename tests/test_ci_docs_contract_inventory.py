from __future__ import annotations

import subprocess
from hashlib import sha256
from pathlib import Path
from typing import Final

import pytest
import yaml

import ci_docs_contract_inventory_support as inventory_support
from ci_docs_contract_inventory_support import (
    EXCLUDED_FILENAMES,
    EXCLUDED_SUFFIX_REASONS,
    directly_covered_paths,
    is_scanned_code_path,
    potential_docs_consumers,
)

ROOT: Final = Path(__file__).resolve().parent.parent
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
    "apps/api/tests/test_log_route_contract.py": (
        "asserts that the FastAPI interactive-documentation route stays a preserved log path; never reads repository docs"
    ),
    "packages/creator-service/creator_service/error_redaction.py": (
        "names the FastAPI interactive-documentation route as a preserved log path; never reads repository docs"
    ),
    "packages/creator-service/tests/test_log_route_redaction.py": (
        "asserts that the FastAPI interactive-documentation route survives log redaction; never reads repository docs"
    ),
}
HISTORICAL_REFERENCE_ALLOWLIST: Final = {
    "apps/api/migrations/versions/005_expand_artifact_typing_and_indexes.py": (
        "migration comments cite historical index design notes; runtime never reads documentation"
    ),
    "apps/api/migrations/versions/031_artifact_retention_env.py": (
        "migration prose records documentation updated with the schema; runtime never reads it"
    ),
    "apps/worker-orchestrator/celery_app.py": (
        "module prose points operators to a runbook; worker startup never reads documentation"
    ),
    "docker-compose.yml": (
        "comment points operators to network policy; Compose never loads documentation"
    ),
}
PACKAGE_METADATA_ALLOWLIST: Final = {
    "pyproject.toml": "package metadata names README.md for build-time display, not runtime behavior",
}
INVENTORY_ALLOWLIST: Final = {
    "ci_docs_contract_inventory_support.py": "implements conservative scanning and shell tokenization",
    "tests/test_ci_docs_contract_inventory.py": "implements this inventory and its regression fixtures",
}
ALLOWLIST_BY_CATEGORY: Final = {
    "CI policy classifiers": CI_POLICY_ALLOWLIST,
    "API route identifiers": API_ROUTE_ALLOWLIST,
    "Historical prose references": HISTORICAL_REFERENCE_ALLOWLIST,
    "Package metadata": PACKAGE_METADATA_ALLOWLIST,
    "Inventory implementation": INVENTORY_ALLOWLIST,
}
ALLOWLIST: Final = {
    path: reason
    for entries in ALLOWLIST_BY_CATEGORY.values()
    for path, reason in entries.items()
}
REVIEWED_ALLOWLIST_MATCH_DIGESTS: Final = {
    "scripts/ci_impact.py": "3299bdfcdee12b427c01dfc66f33446f4dbde2196b3c2cc243ed07b7130f5158",
    "scripts/ci_required.py": "5330a60518537724a8dd459e39eb32b8e324219aba9e09b0bdbbc0c3b8bb21d7",
    "tests/test_ci_impact.py": "4da6968714f1ac4c76389deb94dfd736129d3528d7d0fc427036a107f2f44184",
    "tests/test_ci_impact_git.py": "8d558fd7293ed4ec6446908ce0d99518dcccffe63449e5405486e396f68eddb5",
    "tests/test_ci_required.py": "8a646781d6f7de504ee035e6ceffdec5bac7b2cdacae4894035fdd3a48d1c0dd",
    "tests/test_ci_required_workflow.py": "b6c3ec7e30f9cf8dd7ccacccd09d831d5919b2f6c93cde792380b75927eb2ac1",
    "apps/api/tests/test_auth.py": "ba7d93317b9d1feef4301eb6c07d607c8394fc095b1f67398a96c363d64002b6",
    "apps/studio-web/src/pages/OpsPage.tsx": "da2a9442fafa0e32885e0fb4f4fa6dcc698e957eb2e6842d87d924a534068d36",
    "apps/api/tests/test_log_route_contract.py": (
        "46f3363d135594a5c8e32246ca5c46028bf1dd1a18cf4eef91f30498d7031aa6"
    ),
    "packages/creator-service/creator_service/error_redaction.py": (
        "b18db262874ce055dca954e08cc4811193b7386b8f5460f707837d29e34d630d"
    ),
    "packages/creator-service/tests/test_log_route_redaction.py": (
        "f70e33109eec162c48ab5d1a85b9c9d3253056679a609dca314f33c6a5b9655e"
    ),
    "apps/api/migrations/versions/005_expand_artifact_typing_and_indexes.py": (
        "ed958eb3fd883a2443b55a52def834d1828aee54e93eaefab2ca5aa5bc7432b9"
    ),
    "apps/api/migrations/versions/031_artifact_retention_env.py": (
        "9889871d9c983afbecd6727491df0f7e5b4b620f2ce8be032d1778e8f439fcf5"
    ),
    "apps/worker-orchestrator/celery_app.py": (
        "5079680b007699cd723fe0df708ae3afed3841787eaa91cc84c6ea8faea71765"
    ),
    "docker-compose.yml": "2f9e4c09cae2cea192b34b2cbcd23988f6a8f7ef8b00db5daf3bf3b44b87bee2",
    "pyproject.toml": "72c22b6c12c1c2bac72839744f462529bbc3bcda0ac0176e5f6aade00ea6bd89",
    "ci_docs_contract_inventory_support.py": (
        "c4b95259cfcf3155b7c329c24cec3a9309c110c956cd73e70a9d4bf42273a377"
    ),
    "tests/test_ci_docs_contract_inventory.py": (
        "873688636e0064317136d4eb49e194c2cec642225d26b31595df6ac2fcaa70c6"
    ),
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
    texts: list[tuple[str, str]] = []
    for path in _tracked_files():
        content = (ROOT / path).read_bytes()
        if b"\0" in content:
            continue
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if is_scanned_code_path(path, text):
            texts.append((path, text))
    return tuple(texts)


def _docs_job_command() -> str:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["docs"]["steps"]
    matching = [step["run"] for step in steps if step.get("name") == DOCS_STEP_NAME]
    assert len(matching) == 1
    return matching[0]


def _docs_job_covered_paths(command: str, candidates: set[str]) -> set[str]:
    covered = directly_covered_paths(command, candidates)
    covered.update(
        script
        for script, invoking_test in DOCS_JOB_EXECUTED_SCRIPTS.items()
        if script in candidates and invoking_test in covered
    )
    return covered


def test_every_excluded_file_type_has_a_reviewed_reason() -> None:
    # Given the complete explicit set of file types excluded from text scanning.
    reasons = (*EXCLUDED_SUFFIX_REASONS.values(), *EXCLUDED_FILENAMES.values())

    # When the exclusion contract is reviewed.
    reviewed = tuple(reason for reason in reasons if reason.strip())

    # Then every exclusion explains why that file type cannot consume documentation.
    assert reviewed == reasons


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
    flagged = potential_docs_consumers(files)

    # Then syntax shape and earlier docstrings cannot hide the candidate.
    assert flagged == {path}


@pytest.mark.parametrize(
    "path",
    [
        "apps/worker-orchestrator/tasks/docs_runtime_contract.py",
        "root_docs_consumer.py",
        "apps/api/migrations/docs_contract.py",
        "apps/studio-web/vite.config.ts",
        "docker-compose.yml",
        "bin/docs-check",
    ],
)
def test_conservative_detector_scans_code_like_files_regardless_of_location(path: str) -> None:
    # Given a code-like tracked file outside the formerly recognized directory shapes.
    files = ((path, 'Path("docs/CUTOVER.md").read_text(encoding="utf-8")'),)

    # When the conservative detector scans its path and text.
    flagged = potential_docs_consumers(files)

    # Then location cannot hide a potential documentation consumer.
    assert flagged == {path}


@pytest.mark.parametrize(
    "command",
    [
        "python3 -m pytest tests/real.py\n# tests/comment_only.py",
        'echo "tests/quoted.py"',
        "python3 -m pytest tests/real.py  # noqa: tests/trailing.py",
        "python3 -m pytest tests/test_x.py_backup",
        "echo pytest tests/quoted_as_command_data.py",
    ],
)
def test_docs_job_coverage_rejects_non_command_path_mentions(command: str) -> None:
    # Given a docs-job command that only mentions a candidate outside an executable token.
    candidates = {
        "tests/comment_only.py",
        "tests/quoted.py",
        "tests/trailing.py",
        "tests/test_x.py",
        "tests/quoted_as_command_data.py",
    }

    # When docs-job coverage is derived from shell commands.
    covered = _docs_job_covered_paths(command, candidates)

    # Then comments, data arguments, and longer tokens provide no coverage.
    assert not covered


def test_docs_job_coverage_accepts_direct_pytest_paths() -> None:
    # Given direct pytest paths with the supported quiet option.
    command = "python3 -m pytest tests/a.py::test_case tests/b.py -q"
    candidates = {"tests/a.py", "tests/b.py"}

    # When docs-job coverage is derived from shell commands.
    covered = _docs_job_covered_paths(command, candidates)

    # Then each directly invoked test path is covered.
    assert covered == candidates


def test_docs_job_coverage_ignores_path_invoked_only_by_another_job() -> None:
    # Given separate docs and non-docs job commands.
    docs_command = "python3 -m pytest tests/docs_contract.py"
    jobs = {
        "docs": docs_command,
        "test": "python3 -m pytest tests/other_job_only.py",
    }

    # When coverage is derived only from the docs-validation command.
    covered = _docs_job_covered_paths(
        jobs["docs"],
        {"tests/docs_contract.py", "tests/other_job_only.py"},
    )

    # Then a direct invocation in another job does not count.
    assert covered == {"tests/docs_contract.py"}


def test_every_potential_docs_consumer_is_reviewed_or_run_by_docs_job() -> None:
    tracked_texts = _tracked_code_texts()
    candidates = potential_docs_consumers(tracked_texts)
    covered = _docs_job_covered_paths(_docs_job_command(), candidates)
    current_matches = inventory_support.docs_reference_matches(tracked_texts)

    assert candidates == covered | ALLOWLIST.keys(), (
        f"unclassified potential docs consumers: {sorted(candidates - covered - ALLOWLIST.keys())}; "
        f"stale allowlist entries: {sorted(ALLOWLIST.keys() - candidates)}"
    )
    assert all(reason.strip() for reason in ALLOWLIST.values())
    changed = {
        path: current_matches[path]
        for path in ALLOWLIST
        if path in current_matches
        and sha256("\n".join(current_matches[path]).encode()).hexdigest()
        != REVIEWED_ALLOWLIST_MATCH_DIGESTS.get(path)
    }
    assert not changed, f"allowlisted documentation matches changed; review current matched lines: {changed}"


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
