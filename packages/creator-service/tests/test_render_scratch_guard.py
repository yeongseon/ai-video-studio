import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MODULES = (
    "packages/creator-service/creator_service/ffmpeg_service.py",
    "packages/creator-service/creator_service/render_segments.py",
    "apps/worker-orchestrator/tasks/timeline_render.py",
    "packages/creator-service/creator_service/bgm_service.py",
    "packages/creator-service/creator_service/media_file_ingestion.py",
    "packages/creator-service/creator_service/media_probe.py",
)
# Upload byte staging and byte probing are API ingestion, not render scratch.
SYSTEM_TEMP_ALLOWLIST = {
    ("media_file_ingestion.py", "prepare_upload", "tempfile.TemporaryDirectory"),
    ("media_probe.py", "_run_ffprobe", "tempfile.TemporaryDirectory"),
}
# These exact expressions create published-output parents, not transient scratch.
OUTPUT_PARENTS = {"output_path.parent", "out.parent", "output.parent"}


def unmanaged(source: str, filename: str) -> list[str]:
    tree = ast.parse(source)
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name] = alias.name
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        expression = ast.unparse(node.func)
        head, *rest = expression.split(".")
        canonical = ".".join([aliases.get(head, head), *rest])
        owner = parents.get(node)
        while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = parents.get(owner)
        function = owner.name if owner is not None else "<module>"
        if (filename, function, canonical) in SYSTEM_TEMP_ALLOWLIST:
            continue
        if canonical.startswith("tempfile.") or canonical.split(".")[-1] in {
            "mkdtemp", "TemporaryDirectory", "NamedTemporaryFile", "SpooledTemporaryFile", "makedirs", "mkdir",
        }:
            if isinstance(node.func, ast.Attribute) and node.func.attr == "mkdir":
                if ast.unparse(node.func.value) in OUTPUT_PARENTS:
                    continue
            violations.append(f"{filename}:{node.lineno}: {canonical}")
    return violations


def test_render_modules_have_no_unmanaged_scratch() -> None:
    # Given the reviewed inventory, when parsed, then no unmanaged allocation survives.
    violations = [item for name in MODULES for item in unmanaged((ROOT / name).read_text(), Path(name).name)]
    assert violations == []


@pytest.mark.parametrize("source", [
    "import tempfile\ntempfile.mkdtemp()",
    "import tempfile as t\nt.TemporaryDirectory()",
    "from tempfile import NamedTemporaryFile as f\nf()",
    "from tempfile import SpooledTemporaryFile\nSpooledTemporaryFile()",
    "from tempfile import mkdtemp\nmkdtemp()",
    "import os\nos.makedirs('scratch')",
    "from pathlib import Path\nPath('scratch').mkdir()",
    "tmp_dir.mkdir()",
])
def test_guard_rejects_new_unmanaged_site(source: str) -> None:
    # Given a synthetic mutation, when scanned, then its allocation fails the guard.
    assert len(unmanaged(source, "ffmpeg_service.py")) == 1
