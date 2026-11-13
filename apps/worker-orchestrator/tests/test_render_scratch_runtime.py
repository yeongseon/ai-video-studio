from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import replace
from pathlib import Path

import anyio
import pytest
from PIL import Image
from creator_service import ffmpeg_service
from creator_service.artifact_storage_integration import store_artifact_file
from creator_service.recipe_profile import resolve_quality_profile

from tests.render_output_parity_support import render_through_production_adapters
from tasks.render_video import execute_render
from tasks.task_runner import TaskContext
from datetime import datetime, timezone


def _probe(path: Path) -> str:
    result = subprocess.run([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
    ], check=True, capture_output=True, text=True, timeout=30)
    document = json.loads(result.stdout)
    del document["format"]["filename"]
    return json.dumps(document, sort_keys=True)


@pytest.mark.parametrize("configured", [False, True])
def test_legacy_and_timeline_layout_and_published_output_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: bool,
) -> None:
    # Given identical synthetic inputs rendered through both real publication adapters.
    image = tmp_path / "scene.png"
    Image.new("RGB", (96, 160), "white").save(image)
    captions = tmp_path / "captions.srt"
    captions.write_text("1\n00:00:00,000 --> 00:00:00,900\nSCRATCH PARITY\n")
    voice = tmp_path / "voice.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", str(voice)], check=True, capture_output=True, timeout=30)
    scratch = tmp_path / "scratch"
    roots = [tmp_path / "baseline", tmp_path / "comparison"]
    hashes: list[tuple[str, str]] = []
    probes: list[tuple[str, str]] = []
    metadata: list[tuple[dict[str, str], dict[str, str]]] = []
    observed: list[tuple[Path, tuple[Path, ...]]] = []
    timeline_contents: list[set[str]] = []
    original = ffmpeg_service._run_ffmpeg
    monkeypatch.setattr("creator_service.artifact_storage_integration.store_artifact_file", store_artifact_file)
    quality = replace(resolve_quality_profile("shorts_default"), bgm_mood="calm")
    monkeypatch.setattr("tasks.legacy_render_effects.resolve_quality_profile", lambda _: quality)

    def observe(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        result = original(cmd, timeout)
        root = roots[len(hashes)]
        directories = tuple(p for parent in (root, scratch) if parent.exists()
                            for p in parent.rglob("*") if p.is_dir()
                            and (p.name.startswith(".render_tmp_") or p.name.startswith("timeline-")))
        for directory in directories:
            if directory.name.startswith("timeline-"):
                timeline_contents.append({p.name for p in directory.iterdir()})
        observed.append((Path(cmd[-1]), directories))
        return result

    monkeypatch.setattr(ffmpeg_service, "_run_ffmpeg", observe)
    # When each adapter publishes once with the default and once with the selected setting.
    for index, root in enumerate(roots):
        root.mkdir()
        local_image, local_captions = root / image.name, root / captions.name
        local_image.write_bytes(image.read_bytes())
        local_captions.write_bytes(captions.read_bytes())
        local_voice = root / voice.name
        local_voice.write_bytes(voice.read_bytes())
        monkeypatch.delenv("RENDER_SCRATCH_DIR", raising=False)
        if index and configured:
            monkeypatch.setenv("RENDER_SCRATCH_DIR", str(scratch))
        rendered = anyio.run(render_through_production_adapters, root, monkeypatch, local_image, local_voice, local_captions)
        hashes.append(tuple(hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in (rendered.legacy, rendered.timeline)))
        probes.append((_probe(rendered.legacy), _probe(rendered.timeline)))
        metadata.append((rendered.legacy_metadata, rendered.timeline_metadata))
        assert not any(p.name.startswith((".render_tmp_", "timeline-", "output.tmp.")) for p in root.rglob("*"))
    # Then placement changes neither bytes nor stream/format metadata nor persisted storage keys.
    assert hashes[0] == hashes[1]
    assert probes[0] == probes[1]
    assert metadata[0] == metadata[1] == (
        {"render_profile": "shorts_default", "storage_provider": "local", "storage_key": "1/render/output.mp4"},
        {"render_profile": "shorts_default", "storage_provider": "local", "storage_key": "2/render/output.mp4"},
    )
    assert len(observed) == 8
    assert timeline_contents == [{"source_0000", "narration.wav"}] * 4
    for target, directories in observed:
        moved = configured and target.is_relative_to(roots[1]) or target.is_relative_to(scratch)
        expected = scratch if moved else target.parents[1] if target.suffix == ".ts" else target.parent
        assert directories
        for directory in directories:
            assert directory.parent == expected
            if not moved:
                pattern = r"\.render_tmp_\d+_[0-9a-f]{8}" if directory.name.startswith(".render_tmp_") else r"timeline-[a-z0-9_]{8}"
                assert re.fullmatch(pattern, directory.name)
        if target.suffix == ".mp4":
            assert target.parent in [root / str(run) / "render" for root in roots for run in (1, 2)]
    if configured:
        assert list(scratch.iterdir()) == []


def test_invalid_setting_rejects_worker_before_preparing_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given an invalid setting and no prepared render inputs.
    monkeypatch.setenv("RENDER_SCRATCH_DIR", "relative")
    context = TaskContext(1, "scratch-test", {}, 1, 1, datetime.now(timezone.utc))
    # When the production body is entered directly, then validation wins over input preparation.
    with pytest.raises(ValueError, match="RENDER_SCRATCH_DIR"):
        anyio.run(execute_render, context)
