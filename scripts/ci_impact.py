"""Conservative path-based CI impact classification for pull requests.

The main branch always runs every CI job. Pull requests skip expensive runtime
jobs only for changes outside the relevant risk boundary. Unrecognized paths
fail closed to full validation.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import PurePosixPath

GATES = ("code", "restore", "first_short", "worker", "docker")
DOC_NAMES = {
    "AGENTS.md", "README.md", "CHANGELOG.md", "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md", "SECURITY.md",
}
HIGH_RISK_WORDS = (
    "auth", "security", "credential", "session", "workspace", "access",
    "quota", "dispatch", "broker", "claim", "reconcile", "retention",
    "storage", "upload", "artifact", "media", "render", "ffmpeg",
    "timeline", "migration", "idempotenc", "spend", "task",
)


def classify_paths(paths: list[str]) -> dict[str, bool]:
    result = dict.fromkeys(GATES, False)
    if not paths:
        return dict.fromkeys(GATES, True)  # Do not silently skip a bad/empty diff.
    for raw in paths:
        path = raw.replace("\\", "/")
        name = PurePosixPath(path).name.lower()
        if (
            path in DOC_NAMES
            or path.startswith("docs/")
            or path.startswith(".github/ISSUE_TEMPLATE/")
            or path == ".github/pull_request_template.md"
            or path.endswith(".md")
        ):
            continue
        result["code"] = True
        if path.startswith(".github/workflows/") or path in {
            "scripts/ci_impact.py", "tests/test_ci_impact.py",
            "constraints.txt", "uv.lock", "pyproject.toml", "Makefile",
            "docker-compose.yml", "docker-compose.dev.yml",
            "docker-compose.e2e.yml", "docker-compose.local-server.yml",
            "docker-compose.scaled-workers.yml",
        } or name in {"dockerfile", ".dockerignore"}:
            result.update({gate: True for gate in GATES})
        elif path.startswith("apps/studio-web/src/"):
            # Frontend lint, unit tests, typecheck, and build remain mandatory.
            pass
        elif path.startswith("apps/studio-web/"):
            result["docker"] = True
            if "nginx" in name or "entrypoint" in path:
                result.update({gate: True for gate in GATES})
        elif path.startswith("apps/api/migrations/"):
            result["restore"] = result["first_short"] = True
        elif path.startswith("apps/api/src/"):
            if any(word in name for word in HIGH_RISK_WORDS):
                result["first_short"] = True
            if "auth" in name or "security" in name:
                result.update({gate: True for gate in GATES})
        elif path.startswith("apps/api/"):
            result["first_short"] = True
            if name.startswith("requirements"):
                result.update({gate: True for gate in GATES})
        elif path.startswith("apps/worker-orchestrator/"):
            result["worker"] = True
            if name.startswith("requirements") or name == "dockerfile":
                result.update({gate: True for gate in GATES})
            elif any(word in name for word in HIGH_RISK_WORDS):
                result["first_short"] = True
        elif path.startswith("packages/creator-service/"):
            if any(word in name for word in HIGH_RISK_WORDS):
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
            if "integration/" in path or any(word in name for word in HIGH_RISK_WORDS):
                result["first_short"] = True
            if "restore" in name or "backup" in name:
                result["restore"] = True
            if "worker" in name or "topology" in name:
                result["worker"] = True
        elif path.startswith("scripts/"):
            if "restore" in name or "backup" in name:
                result["restore"] = True
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


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: ci_impact.py BASE_SHA HEAD_SHA")
    changed = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMRT", sys.argv[1], sys.argv[2]],
        check=True, text=True, capture_output=True,
    ).stdout.splitlines()
    gates = classify_paths(changed)
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        raise SystemExit("GITHUB_OUTPUT is required")
    with open(output_file, "a", encoding="utf-8") as output:
        for name in GATES:
            output.write(f"{name}={str(gates[name]).lower()}\n")
    print(f"Changed files: {len(changed)}; CI gates: {gates}")


if __name__ == "__main__":
    main()
