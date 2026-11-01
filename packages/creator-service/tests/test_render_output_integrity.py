"""Real media rejection at the final render/publication boundary (#901)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from creator_service.ffmpeg_service import FFmpegService, RenderInput
from creator_service.render_profile import RenderProfile


def make_media(path: Path, *, duration: float = 5, size: str = "128x128", fps: int = 30,
               audio: bool = False, codec: str = "libx264") -> Path:
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
           f"testsrc2=size={size}:rate={fps}:duration={duration}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]
    cmd += ["-c:v", codec, "-threads", "1", "-preset", "ultrafast", "-g", str(fps),
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(path)]
    subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
    return path


def copy_render(service: FFmpegService, fixture: Path, output: Path, *, rc: int = 0,
                audio_required: bool = False) -> Path:
    def encode(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        shutil.copyfile(fixture, cmd[-1])
        return subprocess.CompletedProcess(cmd, 0 if cmd[-1].endswith(".ts") else rc, "", "")

    inputs = RenderInput([fixture], fixture if audio_required else None, None, [5.0])
    with patch("creator_service.ffmpeg_service._run_ffmpeg", side_effect=encode):
        return service.render(inputs, output)


@pytest.fixture
def service() -> FFmpegService:
    return FFmpegService(RenderProfile(width=128, height=128, fps=30, preset="ultrafast"))


@pytest.mark.parametrize("rc", [0, 1, -12])
def test_rejects_playable_truncated_output_before_rename(tmp_path: Path, service: FFmpegService,
                                                       rc: int) -> None:
    # Given: playable same-profile output, four seconds short of the plan.
    fixture = make_media(tmp_path / "partial.mp4", duration=1)
    output = tmp_path / "output" / "video.mp4"
    # When / Then: failure cannot publish or leave temporary media.
    with pytest.raises(RuntimeError):
        copy_render(service, fixture, output, rc=rc)
    assert list(output.parent.iterdir()) == []


@pytest.mark.parametrize("case", ["audio", "dimensions", "fps", "codec", "empty"])
def test_rejects_profile_or_stream_mismatch(tmp_path: Path, service: FFmpegService,
                                          case: str) -> None:
    # Given: duration-complete output with a single invalid property.
    fixture = make_media(tmp_path / "fixture.mp4", size="160x128" if case == "dimensions" else "128x128",
                         fps=24 if case == "fps" else 30,
                         codec="libx265" if case == "codec" else "libx264")
    if case == "empty":
        fixture.write_bytes(b"")
    output = tmp_path / "output" / "video.mp4"
    # When / Then
    with pytest.raises(RuntimeError):
        copy_render(service, fixture, output, audio_required=case == "audio")
    assert list(output.parent.iterdir()) == []


@pytest.mark.parametrize("duration", ["NaN", "inf", "0", "-1"])
def test_rejects_invalid_probe_duration(tmp_path: Path, service: FFmpegService,
                                       duration: str) -> None:
    # Given: real good media, but untrusted ffprobe JSON has an invalid duration.
    fixture = make_media(tmp_path / "good.mp4")
    real_run = subprocess.run
    probe = real_run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json",
                      str(fixture)], capture_output=True, text=True, check=True, timeout=15)
    document = json.loads(probe.stdout)
    document["format"]["duration"] = duration

    def run(cmd: list[str], **kwargs):
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, json.dumps(document), "")
        return real_run(cmd, **kwargs)

    # When / Then
    output = tmp_path / "output" / "video.mp4"
    with patch("subprocess.run", side_effect=run), pytest.raises(RuntimeError):
        copy_render(service, fixture, output)
    assert list(output.parent.iterdir()) == []


def test_rejects_corrupt_tail_with_intact_metadata(tmp_path: Path, service: FFmpegService) -> None:
    # Given: faststart keeps the index intact while the last media bytes are damaged.
    fixture = make_media(tmp_path / "tail.mp4")
    data = fixture.read_bytes()
    fixture.write_bytes(data[:-8000] + b"\x00" * 8000)
    output = tmp_path / "output" / "video.mp4"
    # When / Then: a decoder error (even exit zero) is not publication success.
    with pytest.raises(RuntimeError):
        copy_render(service, fixture, output)
    assert list(output.parent.iterdir()) == []


@pytest.mark.parametrize("audio", [False, True])
def test_accepts_complete_real_output(tmp_path: Path, service: FFmpegService, audio: bool) -> None:
    # Given
    fixture = make_media(tmp_path / "good.mp4", audio=audio)
    output = tmp_path / "output" / "video.mp4"
    # When
    result = copy_render(service, fixture, output, audio_required=audio)
    # Then
    assert result == output
    assert output.read_bytes() == fixture.read_bytes()
    assert list(output.parent.iterdir()) == [output]


def test_rejects_complete_output_on_nonzero_exit(tmp_path: Path, service: FFmpegService) -> None:
    # Given: no reproducible benign nonzero case exists in the policy's history.
    fixture = make_media(tmp_path / "complete.mp4")
    output = tmp_path / "output" / "video.mp4"
    # When / Then
    with pytest.raises(RuntimeError):
        copy_render(service, fixture, output, rc=-12)
    assert list(output.parent.iterdir()) == []
