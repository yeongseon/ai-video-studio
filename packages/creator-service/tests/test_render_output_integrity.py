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
from creator_service.render_segments import render_plan
from creator_domain.models import EncodingProfile, OutputSpec, RenderPlan, RenderSegment, RenderSegmentKind
from creator_service.media_integrity import MediaExpectation, MediaIntegrityError, validate_media


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

    def run(cmd: list[str], timeout: int):
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, json.dumps(document), "")
        return real_run(cmd, capture_output=True, text=True, timeout=timeout)

    # When / Then
    output = tmp_path / "output" / "video.mp4"
    with patch("creator_service.media_integrity.run_validation", side_effect=run), pytest.raises(RuntimeError):
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


def test_plan_rejects_partial_final_output(tmp_path: Path) -> None:
    # Given: the alternative public renderer returns final media, not intermediates.
    fixture = make_media(tmp_path / "partial.mp4", duration=1)
    plan = RenderPlan(segments=[RenderSegment(kind=RenderSegmentKind.IMAGE,
        source=str(fixture), timeline_start_seconds=0, duration_seconds=5)],
        output_spec=OutputSpec(width=128, height=128, fps=30),
        encoding_profile=EncodingProfile.from_render_profile(RenderProfile()))
    output = tmp_path / "output" / "video.mp4"

    def encode(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        shutil.copyfile(fixture, cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    # When / Then
    with patch("creator_service.render_segments._run_ffmpeg", side_effect=encode), pytest.raises(RuntimeError):
        render_plan(plan, output)
    assert list(output.parent.iterdir()) == []


def test_image_segment_encodes_actual_profile_cadence(tmp_path: Path) -> None:
    from PIL import Image
    from creator_service.render_segments import render_image_segment
    # Given: still-image demuxing defaults to 25 fps, profile requires 30.
    image = tmp_path / "image.png"
    Image.new("RGB", (128, 128), "red").save(image)
    segment = RenderSegment(kind=RenderSegmentKind.IMAGE, source=str(image),
                            timeline_start_seconds=0, duration_seconds=2)
    # When
    output = render_image_segment(segment, tmp_path / "segment.ts",
                                  profile=RenderProfile(width=128, height=128, fps=30))
    # Then: count decoded frames, not just the nominal r_frame_rate label.
    probe = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_entries",
                            "stream=nb_read_frames", "-of", "json", str(output)],
                           capture_output=True, text=True, check=True, timeout=15)
    assert int(json.loads(probe.stdout)["streams"][0]["nb_read_frames"]) == 60


@pytest.mark.parametrize("rule", ["video", "audio_codec", "audio_duration", "video_duration",
                                 "size", "average_fps", "malformed", "probe_error", "tail_error",
                                 "tail_empty", "timeout"])
def test_validator_rejects_untrusted_probe_or_decoder_results(tmp_path: Path, rule: str) -> None:
    # Given: real media with exactly one corrupted tool result.
    fixture = make_media(tmp_path / "good.mp4", audio=True)
    result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of",
                             "json", str(fixture)], capture_output=True, text=True, check=True, timeout=15)
    document = json.loads(result.stdout)
    if rule == "video":
        document["streams"] = document["streams"][1:]
    if rule == "audio_codec":
        document["streams"][1]["codec_name"] = "mp3"
    if rule == "audio_duration":
        document["streams"][1]["duration"] = "1"
    if rule == "video_duration":
        document["streams"][0]["duration"] = "1"
    if rule == "size":
        document["format"]["size"] = "1000"
    if rule == "average_fps":
        document["streams"][0]["avg_frame_rate"] = "24/1"
    real_run = subprocess.run

    def run(cmd: list[str], timeout: int):
        if rule == "timeout":
            from creator_service.media_validation_process import ValidationProcessError
            raise ValidationProcessError("validation_timeout")
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 1 if rule == "probe_error" else 0,
                "{}" if rule == "malformed" else json.dumps(document), "")
        if rule in ("tail_error", "tail_empty"):
            return subprocess.CompletedProcess(cmd, 0, "", "decoder error" if rule == "tail_error" else "")
        return real_run(cmd, capture_output=True, text=True, timeout=timeout)

    # When / Then
    with patch("creator_service.media_integrity.run_validation", side_effect=run), pytest.raises(MediaIntegrityError):
        validate_media(fixture, MediaExpectation(RenderProfile(width=128, height=128), 5, True))


@pytest.mark.parametrize("validation_call", ["ffprobe", "ffmpeg"])
@pytest.mark.parametrize("error_type", [FileNotFoundError, PermissionError, OSError])
def test_spawn_failure_rejects_render_without_publication_or_residue(
    tmp_path: Path,
    service: FFmpegService,
    validation_call: str,
    error_type: type[OSError],
) -> None:
    # Given: encoding completed, but a validation executable cannot be spawned.
    fixture = tmp_path / "encoded.mp4"
    fixture.write_bytes(b"x" * 2048)
    output = tmp_path / "output" / "video.mp4"

    def run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        if cmd[0] == validation_call:
            raise error_type("validation executable unavailable")
        if cmd[0] == "ffprobe":
            document = {
                "streams": [{
                    "codec_type": "video", "codec_name": "h264", "width": 128,
                    "height": 128, "avg_frame_rate": "30/1", "r_frame_rate": "30/1",
                    "duration": 5, "nb_frames": 150,
                }],
                "format": {"duration": 5, "size": 2048},
            }
            return subprocess.CompletedProcess(cmd, 0, json.dumps(document), "")
        return subprocess.CompletedProcess(cmd, 0, "0, frame\n", "")

    # When
    with patch("creator_service.media_integrity.run_validation", side_effect=run), pytest.raises(
        MediaIntegrityError,
    ) as error:
        copy_render(service, fixture, output)

    # Then
    assert error.value.rule == "validation_unavailable"
    assert not output.exists()
    assert list(output.parent.iterdir()) == []


@pytest.mark.parametrize("duration", [0.4, 5.03])
def test_validator_accepts_frame_and_audio_rounding(tmp_path: Path, duration: float) -> None:
    # Given: real output rounded by the encoder, within documented plan tolerance.
    fixture = make_media(tmp_path / "good.mp4", duration=duration, audio=True)
    # When / Then
    validate_media(fixture, MediaExpectation(RenderProfile(width=128, height=128),
                                            0.4 if duration < 1 else 5, True))


def test_real_audio_render_respects_plan_end(tmp_path: Path) -> None:
    from PIL import Image
    # Given: muxer -shortest buffering must not extend a one-second plan.
    image = tmp_path / "scene.png"
    Image.new("RGB", (128, 128), "red").save(image)
    voice = tmp_path / "voice.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=duration=1", str(voice)], check=True, capture_output=True, timeout=15)
    subtitles = tmp_path / "captions.srt"
    subtitles.write_text("1\n00:00:00,000 --> 00:00:01,000\nCAPTION\n")
    # When
    from creator_service.render_profile import TransitionStyle
    output = FFmpegService(RenderProfile(width=128, height=128, fps=15, preset="ultrafast",
                                         transition_style=TransitionStyle.KEN_BURNS_LITE)).render(
        RenderInput([image], voice, subtitles, [1]), tmp_path / "out.mp4")
    # Then
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=noprint_wrappers=1:nokey=1", str(output)],
                           check=True, capture_output=True, text=True, timeout=15)
    assert abs(float(probe.stdout) - 1) < 0.1
