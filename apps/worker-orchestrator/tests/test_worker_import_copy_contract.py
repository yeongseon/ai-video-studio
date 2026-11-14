"""Keep first-party Celery entrypoint imports present in the runtime image."""

import ast
import shlex
from pathlib import Path


def test_celery_first_party_imports_are_copied_into_runtime() -> None:
    # Given the runtime stage's source COPY instructions (not builder installs).
    worker = Path(__file__).parents[1]
    root = worker.parents[1]
    sources: list[Path] = []
    runtime = False
    for line in (worker / "Dockerfile").read_text().replace("\\\n", " ").splitlines():
        parts = shlex.split(line, comments=True)
        if not parts:
            continue
        if parts[0].upper() == "FROM":
            runtime = parts[-1] == "runtime"
        if runtime and parts[0].upper() == "COPY" and not any(p.startswith("--from=") for p in parts):
            sources.extend(root / p for p in parts[1:-1] if not p.startswith("--"))

    # When all imports are visited, including function-local imports.
    imports: set[str] = set()
    for node in ast.walk(ast.parse((worker / "celery_app.py").read_text())):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    missing: list[str] = []
    for module in sorted(imports):
        path = worker.joinpath(*module.split("."))
        candidate = path.with_suffix(".py")
        if not candidate.is_file():
            candidate = path / "__init__.py"
        if candidate.is_file() and not any(
            candidate == source or (source.is_dir() and candidate.is_relative_to(source))
            for source in sources
        ):
            missing.append(module)

    # Then every local import is covered by its exact file or a package COPY.
    assert missing == []
