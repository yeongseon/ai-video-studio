"""Build-context contracts using the glob subset used by the root ignore file."""

from pathlib import Path
import re
import shlex
import subprocess
import sys
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
    ".env-cutover-abc123", ".env.example/secret", "nested/.env.production.example/secret",
    ".envsecrets.example", "tls.pem", "nested/tls.pem", "tls.key", "nested/tls.key",
    ".netrc", "nested/.netrc", "secrets/token", "nested/secrets/token",
    ".aws/credentials", "nested/.aws/credentials", ".ssh/id_rsa", "nested/.ssh/id_rsa",
    "operator.env", "nested/operator.env", ".npmrc", "nested/.npmrc",
    "apps/api/data/artifacts/video.mp4", "apps/worker-orchestrator/data/audio.wav",
])
def test_excludes_secrets_and_non_build_content_when_context_is_root(path: str) -> None:
    # Given the root context rules (absence means no filtering).
    ignore = ROOT / ".dockerignore"
    rules = ignore.read_text() if ignore.exists() else ""
    # When selecting a sensitive or generated path, then it never enters the context.
    assert _ignored(path, rules), path


@pytest.mark.parametrize("path", [
    ".env.example", ".env.local.example", ".env.production.example", "apps/api/.env.test.example", "constraints.txt",
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


@pytest.mark.parametrize("pattern", [
    "**/*.pem", "**/*.key", "**/.netrc", "**/secrets/", "**/.aws/", "**/.ssh/",
    "**/*.env", "**/.npmrc", "apps/api/data/artifacts/", "apps/worker-orchestrator/data/",
])
def test_credential_patterns_have_no_tracked_matches(pattern: str) -> None:
    # Given the complete tracked inventory rather than a developer's working tree.
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, env={"GIT_MASTER": "1"},
        capture_output=True, text=True, check=True,
    ).stdout.split("\0")
    # When each new credential/media rule is evaluated independently.
    matches = [path for path in filter(None, tracked) if _ignored(path, pattern)]
    # Then no tracked build or runtime input is removed by that rule.
    assert matches == [], (pattern, matches)


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
    # Then explicit sources and all tracked descendants survive filtering.
    assert "scripts" in sources and "apps/studio-web" in sources and "packages" in sources
    for source in sorted(sources):
        matches = list(ROOT.glob(source))
        assert matches, source
        for match in matches:
            relative = match.relative_to(ROOT).as_posix()
            assert not _ignored(relative, rules), relative
            tracked = subprocess.run(
                ["git", "ls-files", "-z", "--", relative], cwd=ROOT,
                env={"GIT_MASTER": "1"}, capture_output=True, text=True, check=True,
            ).stdout.split("\0")
            assert any(tracked), source
            for path in filter(None, tracked):
                assert not _ignored(path, rules), path


def test_copy_contract_ignores_untracked_local_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given tracked build inputs plus a developer's untracked secret and coverage.
    for source in ("scripts", "packages", "apps/studio-web", "apps/api", "apps/worker-orchestrator"):
        (tmp_path / source).mkdir(parents=True, exist_ok=True)
    (tmp_path / ".dockerignore").write_text((ROOT / ".dockerignore").read_text())
    compose = []
    for app in ("api", "worker-orchestrator", "studio-web"):
        name = f"apps/{app}/Dockerfile"
        (tmp_path / name).write_text("COPY scripts packages apps/studio-web /app/\n")
        compose.append(f"    context: .\n    dockerfile: {name}\n")
    (tmp_path / "docker-compose.yml").write_text("".join(compose))
    (tmp_path / "scripts/input.sh").write_text("true\n")
    (tmp_path / "packages/input.txt").write_text("synthetic\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], env={"GIT_MASTER": "1"}, check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], env={"GIT_MASTER": "1"}, check=True)
    (tmp_path / "apps/studio-web/.env.local").write_text("API_KEY=synthetic\n")
    (tmp_path / "apps/studio-web/coverage").mkdir()
    (tmp_path / "apps/studio-web/coverage/local.html").write_text("local\n")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    # When running the same COPY-survival contract on this checkout.
    test_copy_sources_and_descendants_survive_when_using_root_context()
    # Then only tracked inputs were considered, without altering local files.
    assert (tmp_path / "apps/studio-web/.env.local").read_text() == "API_KEY=synthetic\n"
