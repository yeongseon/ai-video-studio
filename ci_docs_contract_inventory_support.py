from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from pathlib import Path
from typing import Final

DOC_TOKEN: Final = re.compile(r"\.md\b|docs/|[\"']docs[\"']|docs\\|README")
EXCLUDED_SUFFIX_REASONS: Final = {
    ".md": "documentation is the input being protected, not an executable consumer",
    ".svg": "declarative vector artwork cannot execute repository documentation reads",
    ".snap": "generated test snapshots are inert expected-output data",
    ".png": "binary image data cannot execute repository documentation reads",
    ".jpg": "binary image data cannot execute repository documentation reads",
    ".jpeg": "binary image data cannot execute repository documentation reads",
    ".gif": "binary image data cannot execute repository documentation reads",
    ".ico": "binary icon data cannot execute repository documentation reads",
    ".woff": "binary font data cannot execute repository documentation reads",
    ".woff2": "binary font data cannot execute repository documentation reads",
    ".ttf": "binary font data cannot execute repository documentation reads",
    ".mp3": "binary media data cannot execute repository documentation reads",
    ".wav": "binary media data cannot execute repository documentation reads",
    ".mp4": "binary media data cannot execute repository documentation reads",
}
EXCLUDED_FILENAMES: Final = {
    "package-lock.json": "generated dependency lock data is not executable",
    "uv.lock": "generated dependency lock data is not executable",
}
SHELL_OPERATORS: Final = frozenset({"&&", ";", "||", "|", "&"})


def is_scanned_code_path(path: str, text: str) -> bool:
    candidate = Path(path)
    if candidate.name in EXCLUDED_FILENAMES or candidate.suffix.lower() in EXCLUDED_SUFFIX_REASONS:
        return False
    return "\0" not in text


def potential_docs_consumers(files: Iterable[tuple[str, str]]) -> set[str]:
    return {
        path
        for path, text in files
        if is_scanned_code_path(path, text) and DOC_TOKEN.search(text)
    }


def directly_covered_paths(command: str, candidates: set[str]) -> set[str]:
    normalized = command.replace("\\\n", " ").replace("\n", " ; ")
    lexer = shlex.shlex(normalized, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    lexer.commenters = "#"
    tokens = tuple(lexer)
    segments: list[tuple[str, ...]] = []
    start = 0
    for index, token in enumerate(tokens):
        if token in SHELL_OPERATORS:
            if start < index:
                segments.append(tokens[start:index])
            start = index + 1
    if start < len(tokens):
        segments.append(tokens[start:])

    covered: set[str] = set()
    for segment in segments:
        pytest_arguments: tuple[str, ...] = ()
        if segment[0] in {"pytest", "py.test"}:
            pytest_arguments = segment[1:]
        elif len(segment) >= 3 and segment[:3] in {
            ("python", "-m", "pytest"),
            ("python3", "-m", "pytest"),
        }:
            pytest_arguments = segment[3:]
        covered.update(
            path
            for path in candidates
            for argument in pytest_arguments
            if argument == path or argument.startswith(f"{path}::")
        )
        if len(segment) >= 2 and segment[0] in {"python", "python3", "bash", "sh"}:
            invoked = segment[1]
            if invoked in candidates:
                covered.add(invoked)
    return covered
