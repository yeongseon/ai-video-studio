"""Build-context contracts using the glob subset used by the root ignore file."""

from pathlib import Path
import re
import shlex
from typing import Final

import pytest

ROOT: Final = Path(__file__).resolve().parents[1]


def _ignored(path: str, rules: str) -> bool:
    """Docker-style root-relative *, **, ?, ancestor and last-match-wins rules.

    Unlike gitignore, a slashless pattern is rooted, not matched at every depth.
    **/ explicitly matches zero or more directories; trailing slashes are ignored.
    Unsupported character classes fail closed rather than give a false proof.
    """
    ignored = False
    parts = path.strip("/").split("/")
    ancestors = ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]
    for raw in rules.splitlines():
        rule = raw.strip()
        if not rule or rule.startswith("#") or rule == ".":
            continue
        include = rule.startswith("!")
        pattern = rule.removeprefix("!").strip("/")
        assert not any(char in pattern for char in "[]\\"), pattern
        regex = "".join(
            {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}.get(token, re.escape(token))
            for token in re.findall(r"\*\*/|\*\*|\*|\?|[^*?]+", pattern)
        )
        if any(re.fullmatch(regex, ancestor) for ancestor in ancestors):
            ignored = not include
    return ignored


@pytest.mark.parametrize("path", [
    ".env", ".env.local", ".env.production", ".env-storage-abc123", "apps/api/.env",
    "apps/studio-web/.env.production", "data/x", ".git/config", ".git", ".venv/bin/python",
    "apps/studio-web/node_modules/x", "node_modules/x", "packages/x/__pycache__/x.pyc",
    "packages/x/x.pyc", ".pytest_cache/x", ".mypy_cache/x", ".ruff_cache/x",
    ".coverage", "coverage/index.html", "htmlcov/index.html", ".omc/state/x", ".vscode/settings.json",
])
def test_excludes_secrets_and_non_build_content_when_context_is_root(path: str) -> None:
    # Given the root context rules (absence means no filtering).
    ignore = ROOT / ".dockerignore"
    rules = ignore.read_text() if ignore.exists() else ""
    # When selecting a sensitive or generated path, then it never enters the context.
    assert _ignored(path, rules), path


@pytest.mark.parametrize("path", [
    ".env.example", ".env.local.example", "apps/api/.env.test.example", "constraints.txt",
    "apps/api/src/shorts_api/main.py", "packages/creator-service/pyproject.toml",
])
def test_keeps_examples_and_build_inputs_when_filtering_context(path: str) -> None:
    # Given a configured filter; examples are intentional non-secret inputs.
    rules = (ROOT / ".dockerignore").read_text()
    # When checking retained paths, then neither source nor example gets dropped.
    assert not _ignored(path, rules), path


@pytest.mark.parametrize(("rules", "path", "expected"), [
    ("*.txt", "nested/x.txt", False), ("*.txt", "x.txt", True),
    ("**/*.txt", "x.txt", True), ("**/*.txt", "a/b/x.txt", True),
    ("cache/", "cache/x", True), ("/cache/", "nested/cache/x", False),
    ("temp?", "temp1", True), ("temp?", "temp12", False),
    ("**/.env*\n!**/.env*.example", "nested/.env.local.example", False),
    ("*.txt\n!keep*\nkeep-secret.txt", "keep-secret.txt", True),
])
def test_matcher_when_applying_supported_docker_semantics(rules: str, path: str, expected: bool) -> None:
    # Given / When / Then: distinguish root globs, recursive globs and ordered exceptions.
    assert _ignored(path, rules) == expected


def test_copy_sources_and_descendants_survive_when_using_root_context() -> None:
    # Given every Compose root-context Dockerfile, including overlay references.
    rules = (ROOT / ".dockerignore").read_text()
    dockerfiles: set[str] = set()
    for compose in ROOT.glob("docker-compose*.yml"):
        dockerfiles.update(re.findall(r"context: \.\s+dockerfile: (\S+)", compose.read_text()))
    assert dockerfiles == {"apps/api/Dockerfile", "apps/worker-orchestrator/Dockerfile", "apps/studio-web/Dockerfile"}
    # When parsing all local COPY sources (stage copies are outside the context).
    sources: set[str] = set()
    for name in sorted(dockerfiles):
        dockerfile = ROOT / name
        assert not dockerfile.with_name(dockerfile.name + ".dockerignore").exists()
        for line in dockerfile.read_text().replace("\\\n", " ").splitlines():
            tokens = shlex.split(line, comments=True)
            if not tokens or tokens[0].upper() != "COPY" or any(token.startswith("--from=") for token in tokens):
                continue
            sources.update(token for token in tokens[1:-1] if not token.startswith("--"))
    # Then explicit sources and all shipped descendants survive, except reinstallable caches.
    assert "scripts" in sources and "apps/studio-web" in sources and "packages" in sources
    for source in sorted(sources):
        matches = list(ROOT.glob(source))
        assert matches, source
        for match in matches:
            paths = [match, *match.rglob("*")] if match.is_dir() else [match]
            for path in paths:
                relative = path.relative_to(ROOT)
                if {"node_modules", "__pycache__"}.intersection(relative.parts) or path.suffix == ".pyc":
                    continue
                assert not _ignored(relative.as_posix(), rules), relative
