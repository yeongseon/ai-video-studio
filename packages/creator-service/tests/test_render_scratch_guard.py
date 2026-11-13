import ast
import runpy
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def inventory_matches(pattern: str) -> tuple[Path, ...]:
    matches = tuple(ROOT.glob(pattern))
    assert matches and (any(token in pattern for token in "*?[") or len(matches) == 1), f"inventory: {pattern}"
    return matches


MODULES = tuple(sorted({
    str(path.relative_to(ROOT))
    for pattern in (
        "packages/creator-service/creator_service/ffmpeg_service.py",
        "packages/creator-service/creator_service/render_segments.py",
        "packages/creator-service/creator_service/bgm_service.py",
        "packages/creator-service/creator_service/text_scene_renderer.py",
        "packages/creator-service/creator_service/subtitle_service.py",
        "packages/creator-service/creator_service/media_file_ingestion.py",
        "packages/creator-service/creator_service/media_probe.py",
        "apps/worker-orchestrator/tasks/render_*.py",
        "apps/worker-orchestrator/tasks/legacy_render_*.py",
        "apps/worker-orchestrator/tasks/timeline_*.py",
    ) for path in inventory_matches(pattern)
}))
# Upload byte staging and byte probing are API ingestion, not render scratch.
SYSTEM_TEMP_ALLOWLIST = {
    ("media_file_ingestion.py", "prepare_upload", "tempfile.TemporaryDirectory"): 1,
    ("media_probe.py", "_run_ffprobe", "tempfile.TemporaryDirectory"): 1,
}
# Counted per function: these sites create retained workflow outputs, not scratch.
OUTPUT_PARENTS = {
    **{("ffmpeg_service.py", name, expression): 1 for name, expression in (
        ("render", "output_path.parent"), ("concatenate_audio", "out.parent"),
        ("merge_subtitles", "out.parent"), ("convert_srt_to_ass", "out.parent"),
    )},
    **{("bgm_service.py", name, "out.parent"): 1 for name in (
        "generate_ambient_bgm", "mix_audio_with_bgm", "generate_sfx",
        "mix_sfx_at_timestamps", "normalize_loudness",
    )},
    ("render_segments.py", "render_plan", "output_path.parent"): 1,
    ("timeline_render.py", "render_saved_timeline", "output.parent"): 1,
    ("legacy_render_contract.py", "output", "path.parent"): 1,
    ("text_scene_renderer.py", "render_text_scene", "output_path.parent"): 1,
    ("text_scene_renderer.py", "render_scenes_from_subtitles", "output_dir"): 1,
}


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
    occurrences: Counter[tuple[str, str, str]] = Counter()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Call, ast.Attribute, ast.Name)):
            continue
        parent = parents.get(node)
        if isinstance(node, (ast.Attribute, ast.Name)) and (
            isinstance(parent, ast.Attribute) or isinstance(parent, ast.Call) and parent.func is node
        ):
            continue
        expression = ast.unparse(node.func if isinstance(node, ast.Call) else node)
        head, *rest = expression.split(".")
        canonical = ".".join([aliases.get(head, head), *rest])
        dynamic_temp = isinstance(node, ast.Call) and canonical == "getattr" and bool(node.args) and (
            aliases.get(ast.unparse(node.args[0]), ast.unparse(node.args[0])) == "tempfile"
        )
        owner = parents.get(node)
        while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = parents.get(owner)
        function = owner.name if owner is not None else "<module>"
        key = (filename, function, canonical)
        occurrences[key] += 1
        if occurrences[key] <= SYSTEM_TEMP_ALLOWLIST.get(key, 0):
            continue
        if dynamic_temp or canonical.startswith("tempfile.") or isinstance(node, ast.Call) and canonical.split(".")[-1] in {
            "mkdtemp", "TemporaryDirectory", "NamedTemporaryFile", "SpooledTemporaryFile", "makedirs", "mkdir",
            "copytree",
        }:
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "mkdir":
                output_key = (filename, function, ast.unparse(node.func.value))
                if occurrences[key] <= OUTPUT_PARENTS.get(output_key, 0):
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


@pytest.mark.parametrize("source", [
    "import tempfile\nmake = tempfile.mkdtemp\nmake()",
    "from tempfile import mkdtemp as make\nfactory = make",
    "import tempfile\ndef render(make=tempfile.mkdtemp): make()",
    "import tempfile\ngetattr(tempfile, 'mkdtemp')()",
    "import tempfile as t\ngetattr(t, 'mkdtemp')()",
    "import shutil\nshutil.copytree('source', 'scratch')",
    "out = scratch / 'x'\nout.parent.mkdir()",
])
def test_guard_rejects_reference_and_dynamic_bypasses(source: str) -> None:
    # Given an indirect allocation mutation, when scanned, then it cannot bypass the guard.
    assert unmanaged(source, "ffmpeg_service.py")


def test_guard_counts_allowlisted_occurrences() -> None:
    # Given two sites in a function authorized for exactly one API staging directory.
    source = "import tempfile\ndef prepare_upload():\n tempfile.TemporaryDirectory()\n tempfile.TemporaryDirectory()"
    # When scanned, then only the reviewed occurrence is exempt.
    assert len(unmanaged(source, "media_file_ingestion.py")) == 1


@pytest.mark.parametrize("module", [
    "render_materializer.py", "render_video.py", "legacy_render_effects.py",
    "legacy_render_audio.py", "legacy_render_contract.py", "legacy_render_adapter.py",
    "text_scene_renderer.py", "subtitle_service.py",
])
def test_inventory_includes_render_siblings(module: str) -> None:
    # Given a render sibling, when building the inventory, then new scratch there is guarded.
    assert module in {Path(name).name for name in MODULES}


@pytest.mark.parametrize("missing_pattern", [
    "packages/creator-service/creator_service/ffmpeg_service.py",
    "apps/worker-orchestrator/tasks/render_*.py",
])
def test_inventory_rejects_renamed_or_missing_modules(monkeypatch: pytest.MonkeyPatch, missing_pattern: str) -> None:
    # Given the glob result after a named module is renamed or a wildcard family disappears.
    original = Path.glob

    def renamed(path: Path, pattern: str):
        return iter(()) if pattern == missing_pattern else original(path, pattern)

    monkeypatch.setattr(Path, "glob", renamed)
    # When the guard loads its inventory, then missing coverage fails closed.
    with pytest.raises(AssertionError, match="inventory"):
        runpy.run_path(__file__)
