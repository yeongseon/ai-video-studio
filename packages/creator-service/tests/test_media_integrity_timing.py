"""Aggregate timing bounds independent of segmentation (#901 Oracle r1)."""
import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image
from creator_service.media_integrity import MediaExpectation, MediaIntegrityError, validate_media
from creator_service.ffmpeg_service import FFmpegService, RenderInput
from creator_service.render_profile import RenderProfile


def media(path: Path, fps: int = 30, duration: float = 5) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size=128x128:rate={fps}:duration={duration}", "-c:v", "libx264",
                    "-threads", "1", "-preset", "ultrafast", str(path)],
                   check=True, capture_output=True, timeout=30)


def test_many_segments_do_not_license_one_second_truncation(tmp_path: Path) -> None:
    # Given / When / Then: 30 segments do not permit 20 percent media loss.
    path = tmp_path / "four.mp4"
    media(path, duration=4)
    with pytest.raises(MediaIntegrityError):
        validate_media(path, MediaExpectation(RenderProfile(width=128, height=128), 5, False, 30))


def test_numeric_diagnostics_do_not_change_rule_message(tmp_path: Path) -> None:
    # Given / When / Then: numeric render diagnostics are attributes, never classifier input.
    path = tmp_path / "four.mp4"
    media(path, duration=4)
    with pytest.raises(MediaIntegrityError) as error:
        validate_media(path, MediaExpectation(RenderProfile(width=128, height=128), 5.429, False))
    assert str(error.value) == "FFmpeg output integrity failed: duration completeness"
    assert error.value.expected == 5.429
    assert error.value.durations == (4.0, 4.0)


@pytest.mark.parametrize("fps", [15, 24, 25, 30, 60])
@pytest.mark.parametrize("segments", [1, 5, 30])
@pytest.mark.parametrize("outside", [False, True])
def test_aggregate_duration_boundary(tmp_path: Path, fps: int, segments: int, outside: bool) -> None:
    # Given: real media with bounded-container metadata at / beyond the timing boundary.
    path = tmp_path / "five.mp4"
    media(path, fps)
    ceiling = max(0.1, 2 / fps)
    expected = 5 + ceiling + (0.001 if outside else 0)
    expectation = MediaExpectation(RenderProfile(width=128, height=128, fps=fps), expected, False, segments)
    # When / Then
    if outside:
        with pytest.raises(MediaIntegrityError):
            validate_media(path, expectation)
    else:
        validate_media(path, expectation)


def test_thirty_tiny_segments_render_complete(tmp_path: Path) -> None:
    # Given: 30 independently encoded exact-CFR segments.
    image = tmp_path / "image.png"
    Image.new("RGB", (128, 128), "red").save(image)
    profile = RenderProfile(width=128, height=128, preset="ultrafast")
    # When
    output = FFmpegService(profile).render(RenderInput([image] * 30, None, None, [1 / 6] * 30),
                                          tmp_path / "out.mp4")
    # Then
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "json", str(output)], check=True, capture_output=True, text=True, timeout=15)
    assert abs(float(json.loads(probe.stdout)["format"]["duration"]) - 5) <= 0.1
