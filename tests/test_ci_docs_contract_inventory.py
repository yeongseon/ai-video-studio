"""Verify docs-only markdown consumers are covered by the docs CI job.

A docs-only markdown file (docs/*.md or root *.md per test_ci_impact.py) must have
every test that consumes it invoked by the docs CI job. This prevents docs-only PRs
from breaking contract suites silently while the reduced docs job stays green.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent


def _docs_only_paths() -> set[str]:
    """Return the set of docs-only markdown file patterns."""
    # From test_ci_impact.py: .md files in docs/ or at repository root (no /)
    # This matches the canonical docs-only allowlist.
    return {"docs/", "root_md"}


def _parse_markdown_path_references(text: str) -> set[str]:
    """Extract markdown file paths referenced in Python source.

    Finds:
    - Variable assignments like: _CUTOVER = _ROOT / "docs" / "CUTOVER.md"
    - Direct path references like: _CUTOVER.read_text()

    Filters out docstring examples and false positives.
    """
    refs = set()

    # Look for path variable assignments: _VAR = _ROOT / "docs" / "NAME.md" or similar
    # This pattern matches chains like: _ROOT / "docs" / "CUTOVER.md"
    # and captures each path component separately, then rejoins them
    var_assignments = re.findall(
        r'_[A-Z_]+ = (?:_ROOT|_REPO_ROOT|Path\([^)]*\))(?:\s*/\s*["\']([^\'"]+)["\'])+',
        text
    )

    for path in var_assignments:
        if path.endswith('.md'):
            refs.add(path)

    # Capture full paths from chained / operations
    # E.g., _ROOT / "docs" / "CUTOVER.md" -> "docs/CUTOVER.md"
    path_chains = re.findall(
        r'(?:_ROOT|_REPO_ROOT|Path\([^)]*\))(?:\s*/\s*["\']([^\'"]+)["\'])+',
        text
    )

    # Reconstruct paths by looking at complete expressions
    for match in re.finditer(
        r'(?:_ROOT|_REPO_ROOT|Path\([^)]*\))(?:(?:\s*/\s*["\']([^\'"]+)["\'])+)',
        text
    ):
        # Skip docstrings and comments by checking context
        if '"""' in text[:match.start()] or "'''" in text[:match.start()]:
            continue

        full_expr = match.group(0)
        # Extract all path components
        components = re.findall(r'["\']([^\'"]+)["\']', full_expr)
        if components and components[-1].endswith('.md'):
            # Join path components with /
            full_path = '/'.join(components)
            # Clean up: remove duplicate // and trailing /
            full_path = re.sub(r'/+', '/', full_path)
            if full_path.endswith('.md'):
                refs.add(full_path)

    return refs


def _is_docs_only(path: str) -> bool:
    """Return True if path matches the docs-only allowlist.

    Docs-only paths:
    - Start with "docs/" (but not "docs/../")
    - Are in repo root (no "/" in path name)
    """
    if "/" in path:
        # Must be docs/ subdir (not docs/../ traversal)
        return path.startswith("docs/") and not ".." in path
    else:
        # Root markdown
        return path.endswith(".md")


def _test_files_in_repo() -> list[Path]:
    """Return all test files in tests/, apps/*/tests/, packages/*/tests/, scripts/."""
    test_files = []
    for pattern in [
        "tests/test_*.py",
        "apps/*/tests/test_*.py",
        "packages/*/tests/test_*.py",
        "scripts/test_*.py",
    ]:
        test_files.extend(ROOT.glob(pattern))
    return sorted(test_files)


def _consumers_of_docs_file(doc_path: str) -> set[str]:
    """Return test file paths that consume the given docs file.

    A test file consumes a doc if it references it by string literal or Path.
    """
    consumers = set()

    for test_file in _test_files_in_repo():
        try:
            text = test_file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue

        refs = _parse_markdown_path_references(text)

        # Normalize: docs/CUTOVER.md can be referenced as:
        # - "docs/CUTOVER.md"
        # - _ROOT / "docs" / "CUTOVER.md" (parsed as "docs/CUTOVER.md")
        # - CUTOVER (just the name, less reliable)

        # Direct match
        if doc_path in refs:
            consumers.add(test_file.relative_to(ROOT).as_posix())
        # Also match just the basename for robustness
        elif doc_path.split("/")[-1] in refs:
            consumers.add(test_file.relative_to(ROOT).as_posix())

    return consumers


def _docs_job_commands() -> str:
    """Return the concatenated run: commands from the docs CI job."""
    workflow_path = ROOT / ".github" / "workflows" / "ci.yml"
    workflow = yaml.safe_load(workflow_path.read_text())
    docs_job = workflow["jobs"]["docs"]

    commands = "\n".join(step.get("run", "") for step in docs_job["steps"])
    return commands


def _validate_markdown_utf8(path: Path) -> list[str]:
    """Check markdown file for UTF-8 validity and absence of NUL bytes.

    Returns list of issues (empty if valid).
    """
    issues = []
    try:
        text = path.read_bytes()
        # Check for NUL bytes
        if b'\x00' in text:
            issues.append(f"{path}: contains NUL byte")
        # Try to decode as UTF-8
        try:
            text.decode('utf-8')
        except UnicodeDecodeError as e:
            issues.append(f"{path}: invalid UTF-8 at offset {e.start}: {e.reason}")
    except FileNotFoundError:
        pass  # File doesn't exist, other tests will catch this

    return issues


def test_every_docs_only_consumer_is_run_by_docs_job() -> None:
    """Test: every test consuming a docs-only markdown file is invoked by the docs CI job.

    Given: a tracked repository with docs-only markdown files (docs/*.md, root *.md).
    When:  checking which tests reference these files,
    Then:  every test file is invoked by the docs job (or is in the reviewed allowlist).

    The docs job runs a subset of tests for docs-only PRs. This test ensures
    contracts pinned to docs-only files are not silently broken by such PRs.
    """
    docs_commands = _docs_job_commands()

    # Scan for actual docs consumers (skip this inventory test itself)
    docs_only_files = set()
    for test_file in _test_files_in_repo():
        if test_file.name == "test_ci_docs_contract_inventory.py":
            continue  # Skip this inventory test itself

        try:
            text = test_file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue

        refs = _parse_markdown_path_references(text)
        for ref in refs:
            if _is_docs_only(ref):
                docs_only_files.add(ref)

    # Allowlist: tests that consume docs but are covered elsewhere or intentionally excluded
    # Tests in this allowlist are validated by other (non-docs-only-gated) jobs
    allowlist = {
        # test_ci_impact.py: runs on every commit via 'impact' job (ci-required)
        # not docs-only-gated, so files are always validated
        "tests/test_ci_impact.py": "covered by 'impact' job (runs on every commit)",
        # test_ci_impact_git.py: git tests, validation not docs-only-specific
        "tests/test_ci_impact_git.py": "covered by 'impact' job (runs on every commit)",
    }

    missed = {}
    for doc_file in sorted(docs_only_files):
        consumers = _consumers_of_docs_file(doc_file)
        for consumer in sorted(consumers):
            if consumer.endswith("test_ci_docs_contract_inventory.py"):
                continue  # Skip self-reference from this test
            # Check if test is invoked by docs job or explicitly allowlisted
            if consumer not in allowlist and consumer not in docs_commands:
                if doc_file not in missed:
                    missed[doc_file] = []
                missed[doc_file].append(consumer)

    assert not missed, (
        f"docs-only contract violation: the following test files consume docs-only "
        f"markdown but are not invoked by the docs CI job:\n"
        + "\n".join(
            f"  {doc}: {', '.join(tests)}"
            for doc, tests in sorted(missed.items())
        )
    )


def test_all_tracked_markdown_in_docs_is_valid_utf8() -> None:
    """Test: every tracked markdown file in docs-only locations is valid UTF-8.

    Given: markdown files in docs/ and repository root,
    When:  checking their encoding,
    Then:  all decode as UTF-8 and contain no NUL bytes.

    This prevents a malformed markdown file (e.g., with non-UTF-8 or NUL bytes)
    from passing the docs-only gate.
    """
    issues = []

    # Check docs/ tree
    if (ROOT / "docs").exists():
        for md_file in (ROOT / "docs").rglob("*.md"):
            issues.extend(_validate_markdown_utf8(md_file))

    # Check root markdown files
    for root_md in ROOT.glob("*.md"):
        if root_md.is_file():
            issues.extend(_validate_markdown_utf8(root_md))

    assert not issues, (
        f"invalid markdown encoding in docs-only files:\n"
        + "\n".join(f"  {issue}" for issue in issues)
    )
