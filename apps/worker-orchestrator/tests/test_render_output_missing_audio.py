from __future__ import annotations

import os
from pathlib import Path

import anyio
import pytest
from creator_service.media_integrity import MediaExpectation, validate_media
from tests.render_output_parity_support import render_through_production_adapters
from tests.render_output_probe_support import probe as _probe
from tests.render_output_probe_support import run_ffmpeg as _run


def _portable_validate_media(path: Path, expectation: MediaExpectation) -> None:
    # macOS lacks waitid; CI/Linux still executes the complete #941 validator.
    if hasattr(os, "waitid"):
        validate_media(path, expectation)


def test_generated_script_and_saved_timeline_persist_silent_output_without_audio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: both production adapters have the same image and subtitles, but no audio artifact.
    monkeypatch.setattr(
        "creator_service.ffmpeg_service.validate_media",
        _portable_validate_media,
    )
    root = tmp_path / "artifacts"
    root.mkdir()
    image = root / "scene.png"
    subtitles = root / "captions.ass"
    _run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=white:size=96x160",
            "-frames:v",
            "1",
            str(image),
        ]
    )
    subtitles.write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,96,&H00000000,&H000000FF,&H00FFFFFF,&H00000000,"
        "0,0,0,0,100,100,0,0,1,2,0,2,10,10,300,1\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:00.00,0:00:00.90,Default,,0,0,0,,SILENT\n"
    )

    # When: execute_render runs through each real adapter composition without narration.
    rendered = anyio.run(
        render_through_production_adapters,
        root,
        monkeypatch,
        image,
        None,
        subtitles,
    )

    # Then: both persisted MP4 artifacts are intentionally video-only.
    assert all(stream.codec_type != "audio" for stream in _probe(rendered.legacy).streams)
    assert all(stream.codec_type != "audio" for stream in _probe(rendered.timeline).streams)
    assert rendered.legacy.is_file()
    assert rendered.timeline.is_file()
    assert rendered.legacy_metadata["storage_key"].endswith("1/render/output.mp4")
    assert rendered.timeline_metadata["storage_key"].endswith("2/render/output.mp4")
    assert rendered.legacy_metadata["storage_provider"] == "local"
    assert rendered.timeline_metadata["storage_provider"] == "local"
