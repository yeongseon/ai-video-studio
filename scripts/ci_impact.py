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


@dataclass(frozen=True, slots=True)
class GitChange:
    old_mode: str
    new_mode: str
    status: str
    paths: tuple[str, ...]


def _is_documentation(path: str) -> bool:
    if "\\" in path or path.startswith("/"):
        return False
    parsed = PurePosixPath(path)
    if any(part in {".", ".."} for part in parsed.parts) or parsed.as_posix() != path:
        return False
    return (
        path.startswith("docs/") and path.endswith(".md")
    ) or ("/" not in path and path.endswith(".md"))


def classify_paths(paths: list[str]) -> dict[str, bool]:
    result = dict.fromkeys(GATES, False)
    if not paths:
        return dict.fromkeys(GATES, True)  # Do not silently skip a bad/empty diff.
    for raw in paths:
        if _is_documentation(raw):
            result["docs"] = True
            continue
        result.update({gate: True for gate in GATES})
    return result


def _parse_raw_diff(raw: str) -> tuple[GitChange, ...]:
    fields = raw.split("\0")
    if fields[-1] == "":
        fields.pop()
    changes = []
    index = 0
    while index < len(fields):
        metadata = fields[index]
        index += 1
        parts = metadata.split()
        if len(parts) != 5 or not parts[0].startswith(":"):
            raise SystemExit("Malformed git diff --raw metadata")
        status = parts[4]
        status_letter = status[:1]
        if status_letter not in {"A", "C", "D", "M", "R", "T"}:
            raise SystemExit("Malformed git diff --raw status")
        if status_letter in {"R", "C"}:
            score = status[1:]
            if (
                len(score) != 3
                or not score.isdigit()
                or not 1 <= int(score) <= 100
                or index + 1 >= len(fields)
            ):
                raise SystemExit("Malformed git diff --raw status")
            paths = (fields[index], fields[index + 1])
            index += 2
        else:
            if len(status) != 1 or index >= len(fields):
                raise SystemExit("Malformed git diff --raw status")
            paths = (fields[index],)
            index += 1
        if not all(paths):
            raise SystemExit("Malformed git diff --raw path")
        changes.append(GitChange(parts[0][1:], parts[1], status, paths))
    return tuple(changes)


def _has_unsafe_docs_mode(change: GitChange) -> bool:
    return any(mode not in {"000000", "100644"} for mode in (change.old_mode, change.new_mode))


def classify_changes(changes: tuple[GitChange, ...]) -> dict[str, bool]:
    paths = [path for change in changes for path in change.paths]
    gates = classify_paths(paths)
    if any(_has_unsafe_docs_mode(change) for change in changes):
        return dict.fromkeys(GATES, True)
    return gates


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: ci_impact.py BASE_SHA HEAD_SHA")
    raw_diff = subprocess.run(
        [
            "git", "diff", "--raw", "-z", "--find-renames", "--find-copies-harder",
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
