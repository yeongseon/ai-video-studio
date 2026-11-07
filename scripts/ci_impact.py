"""Conservative path-based CI impact classification for pull requests.

The main branch always runs every CI job. Pull requests skip expensive runtime
jobs only for changes outside the relevant risk boundary. Unrecognized paths
fail closed to full validation.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath

GATES = ("docs", "code", "restore", "first_short", "worker", "docker")
HIGH_RISK_WORDS = (
    "auth", "security", "credential", "session", "workspace", "access",
    "quota", "dispatch", "broker", "claim", "reconcile", "retention",
    "storage", "upload", "artifact", "media", "render", "ffmpeg",
    "timeline", "migration", "idempotenc", "spend", "task", "delete",
    "deletion", "owner", "identity", "idor", "fenc", "ingress", "secret",
)
PROTECTED_PREFIXES = (
    "apps/api/src/shorts_api/routes/",
    "apps/api/src/shorts_api/middleware",
    "apps/worker-orchestrator/",
    "apps/api/migrations/",
)
PROTECTED_SERVICE_WORDS = (
    "storage", "dispatch", "quota", "delet", "owner", "broker", "celery",
    "claim", "reconcile", "retention", "artifact", "upload",
)
FULL_GATE_NAMES = (
    "task_dispatch", "release_", "cutover", "rollback", "deploy",
    "build_release_manifest", "nginx",
)
FULL_GATE_FILES = {
    "constraints.txt", "uv.lock", "pyproject.toml", "Makefile",
    "docker-compose.yml", "docker-compose.dev.yml", "docker-compose.e2e.yml",
    "docker-compose.local-server.yml", "docker-compose.scaled-workers.yml",
}


@dataclass(frozen=True, slots=True)
class GitChange:
    old_mode: str
    new_mode: str
    path: str


def _is_documentation(path: str) -> bool:
    if "\\" in path or path.startswith("/"):
        return False
    parsed = PurePosixPath(path)
    if any(part in {".", ".."} for part in parsed.parts) or parsed.as_posix() != path:
        return False
    return (
        path.startswith("docs/") and path.endswith(".md")
    ) or ("/" not in path and path.endswith(".md"))


def _is_full_gate_path(path: str) -> bool:
    path_lower = path.lower()
    name = PurePosixPath(path).name.lower()
    return (
        path_lower.startswith(PROTECTED_PREFIXES)
        or path_lower.startswith("apps/api/src/shorts_api/auth")
        or (
            path_lower.startswith("packages/creator-service/")
            and any(word in path_lower for word in PROTECTED_SERVICE_WORDS)
        )
        or any(word in path_lower for word in FULL_GATE_NAMES)
        or any(
            word in path_lower
            for word in ("secret", "credential", "ingress", "segmentdelete")
        )
        or path.startswith(".github/workflows/")
        or path in FULL_GATE_FILES
        or name.startswith("dockerfile")
        or name == ".dockerignore"
        or (path.startswith("scripts/") and "acceptance" in name)
    )


def classify_paths(paths: list[str]) -> dict[str, bool]:
    result = dict.fromkeys(GATES, False)
    if not paths:
        return dict.fromkeys(GATES, True)  # Do not silently skip a bad/empty diff.
    for raw in paths:
        if _is_documentation(raw):
            result["docs"] = True
            continue
        path = str(PurePosixPath(raw.replace("\\", "/")))
        path_lower = path.lower()
        name = PurePosixPath(path).name.lower()
        result["code"] = True
        if _is_full_gate_path(path):
            result.update({gate: True for gate in GATES})
        elif path.startswith(".github/workflows/") or path in {
            "scripts/ci_impact.py", "tests/test_ci_impact.py",
        } or name in {"dockerfile", ".dockerignore"}:
            result.update({gate: True for gate in GATES})
        elif path.startswith("apps/studio-web/src/"):
            # Most UI edits use frontend tests. API boundary and media preview
            # changes also need the real browser/first-short runtime suite.
            if path.startswith("apps/studio-web/src/api/") or name in {
                "timelinepreview.tsx", "authorizedmedia.tsx",
                "assetdropzone.tsx", "medialibrary.tsx",
                "usemediaactions.ts",
            }:
                result["first_short"] = True
            if any(word in name for word in ("auth", "token", "session", "credential")):
                result.update({gate: True for gate in GATES})
        elif path.startswith("apps/studio-web/"):
            result["docker"] = True
            if "nginx" in name or "entrypoint" in path:
                result.update({gate: True for gate in GATES})
        elif path.startswith("apps/api/src/"):
            if any(word in path_lower for word in HIGH_RISK_WORDS):
                result["first_short"] = True
            if "auth" in path_lower or "security" in path_lower:
                result.update({gate: True for gate in GATES})
        elif path.startswith("apps/api/"):
            result["first_short"] = True
            if name.startswith("requirements"):
                result.update({gate: True for gate in GATES})
        elif path.startswith("packages/creator-service/"):
            if any(word in path_lower for word in HIGH_RISK_WORDS):
                result["first_short"] = True
            if any(word in name for word in ("postgres", "storage", "db", "retention", "upload", "artifact")):
                result["restore"] = True
            if any(word in name for word in ("dispatch", "broker", "quota", "claim", "task", "reconcile")):
                result["worker"] = True
        elif path.startswith("packages/creator-domain/"):
            if any(word in name for word in ("render", "timeline", "asset", "segment", "output", "run", "task")):
                result["first_short"] = True
        elif path.startswith("packages/creator-provider/"):
            if name in {"registry.py", "base.py", "api_keys.py"}:
                result["first_short"] = True
        elif path.startswith("tests/"):
            if name in {"test_ci_impact.py", "test_ci_required.py", "test_ci_required_workflow.py"}:
                result.update({gate: True for gate in GATES})
            elif "integration/" in path or any(word in name for word in HIGH_RISK_WORDS):
                result["first_short"] = True
            if "restore" in name or "backup" in name:
                result["restore"] = True
            if "worker" in name or "topology" in name:
                result["worker"] = True
        elif path.startswith("scripts/"):
            if "restore" in name or "backup" in name:
                result["restore"] = True
            elif "release_manifest" in name:
                # The restore drill builds and verifies the release manifest.
                result["restore"] = result["docker"] = True
            elif "release" in name or "cutover" in name or "quickstart" in name:
                result["docker"] = True
            else:
                result.update({gate: True for gate in GATES})
        elif path.startswith(".github/") or path in {".env.example", ".env.local.example"}:
            result.update({gate: True for gate in GATES})
        else:
            # New directories, top-level configs, and unmatched paths get full CI.
            result.update({gate: True for gate in GATES})
    return result


def _parse_raw_diff(raw: str) -> tuple[GitChange, ...]:
    fields = raw.split("\0")
    if fields[-1] == "":
        fields.pop()
    if len(fields) % 2:
        raise SystemExit("Malformed git diff --raw output")
    changes = []
    for index in range(0, len(fields), 2):
        metadata, path = fields[index:index + 2]
        parts = metadata.split()
        if len(parts) != 5 or not parts[0].startswith(":"):
            raise SystemExit("Malformed git diff --raw metadata")
        changes.append(GitChange(parts[0][1:], parts[1], path))
    return tuple(changes)


def _has_unsafe_docs_mode(change: GitChange) -> bool:
    if "120000" in {change.old_mode, change.new_mode}:
        return True
    old_exec = int(change.old_mode, 8) & 0o111
    new_exec = int(change.new_mode, 8) & 0o111
    return old_exec != new_exec


def classify_changes(changes: tuple[GitChange, ...]) -> dict[str, bool]:
    gates = classify_paths([change.path for change in changes])
    if any(_is_documentation(change.path) and _has_unsafe_docs_mode(change) for change in changes):
        return dict.fromkeys(GATES, True)
    return gates


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: ci_impact.py BASE_SHA HEAD_SHA")
    raw_diff = subprocess.run(
        [
            "git", "diff", "--raw", "-z", "--no-renames",
            "--diff-filter=ACDMRT", sys.argv[1], sys.argv[2],
        ],
        check=True, text=True, capture_output=True,
    ).stdout
    changes = _parse_raw_diff(raw_diff)
    gates = classify_changes(changes)
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        raise SystemExit("GITHUB_OUTPUT is required")
    with open(output_file, "a", encoding="utf-8") as output:
        for name in GATES:
            output.write(f"{name}={str(gates[name]).lower()}\n")
    print(f"Changed files: {len(changes)}; CI gates: {gates}")


if __name__ == "__main__":
    main()
