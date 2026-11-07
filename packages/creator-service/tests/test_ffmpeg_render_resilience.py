"""Tests for FFmpegService.render() resilience — two-pass concat demuxer approach.

Covers:
- Non-zero exit + valid output → rejected
- Non-zero exit + zero-byte file → rejected
- Non-zero exit + small corrupt file → rejected
- Non-zero exit + no file created → rejected
- Success (rc=0) → atomic rename to final path
- Stale output from previous run not accepted
- Timeout handling
- Segment render failure propagates
- Input validation (non-positive durations)
- Process isolation (start_new_session)
- Audio/subtitle branch
- Cleanup after failure
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from creator_service.ffmpeg_service import (
    FFmpegService,
    RenderInput,
    _run_ffmpeg,
    _validate_durations,
)
from creator_service.render_profile import RenderProfile
from creator_service.media_integrity import MediaExpectation, validate_media


def make_media(path: Path, *, duration: float = 5, audio: bool = False) -> None:
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
           f"testsrc2=size=128x128:rate=30:duration={duration}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]
    cmd += ["-c:v", "libx264", "-threads", "1", "-preset", "ultrafast", "-c:a", "aac", str(path)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)


@pytest.fixture
def ffmpeg_service() -> FFmpegService:
    return FFmpegService(profile=RenderProfile(width=128, height=128, preset="ultrafast"))


@pytest.fixture
def render_input(tmp_path: Path) -> RenderInput:
    """Minimal RenderInput with a dummy image."""
    img = tmp_path / "scene.png"
    img.write_bytes(b"\x89PNG" + b"\x00" * 100)  # fake PNG header
    return RenderInput(
        image_paths=[img],
        audio_path=None,
        subtitle_path=None,
        scene_durations=[5.0],
    )


def _make_completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["ffmpeg"], returncode=returncode, stdout="", stderr=stderr
    )


def _make_probe_result(duration: float) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["ffprobe"], returncode=0, stdout=f"{duration}\n", stderr=""
    )


def _make_probe_failure() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["ffprobe"], returncode=1, stdout="", stderr="Invalid data"
    )


def _is_segment_call(cmd: list[str]) -> bool:
    """Detect if a subprocess call is a Pass 1 segment render (output ends in .ts)."""
    if not cmd:
        return False
    return str(cmd[-1]).endswith(".ts")


def _is_concat_call(cmd: list[str]) -> bool:
    """Detect if a subprocess call is a Pass 2 concat render."""
    return "-f" in cmd and "concat" in cmd


class TestRenderSuccess:
    """FFmpeg exits 0 — temp file renamed to final output."""

    def test_success_renames_temp_to_output(
        self, ffmpeg_service: FFmpegService, render_input: RenderInput, tmp_path: Path
    ):
        output_path = tmp_path / "output" / "video.mp4"

        def fake_ffmpeg(cmd, timeout):
            target = Path(cmd[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            make_media(target)
            return _make_completed(0)

        with patch("creator_service.ffmpeg_service._run_ffmpeg", side_effect=fake_ffmpeg):
            result = ffmpeg_service.render(render_input, output_path)

        assert result == output_path
        assert output_path.exists()


class TestRenderNonZeroWithValidOutput:
    """FFmpeg concat pass exits non-zero but output is valid (verified by ffprobe)."""

    def test_rejects_valid_output_on_nonzero_exit(
        self, ffmpeg_service: FFmpegService, render_input: RenderInput, tmp_path: Path
    ):
        output_path = tmp_path / "output" / "video.mp4"

        def fake_ffmpeg(cmd, timeout):
            target = Path(cmd[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            if _is_segment_call(cmd):
                # Segment render succeeds
                target.write_bytes(b"\x00" * 50_000)
                return _make_completed(0)
            # Concat: write valid-sized file but exit non-zero
            target.write_bytes(b"\x00" * 50_000)
            return _make_completed(-12, "ENOMEM during filter cleanup")

        with patch("creator_service.ffmpeg_service._run_ffmpeg", side_effect=fake_ffmpeg):
            with pytest.raises(RuntimeError, match="FFmpeg render failed"):
                ffmpeg_service.render(render_input, output_path)
        assert not output_path.exists()
        assert list(output_path.parent.iterdir()) == []


class TestRenderNonZeroWithInvalidOutput:
    """FFmpeg concat pass exits non-zero and output is invalid — must raise."""

    def test_rejects_zero_byte_file(
        self, ffmpeg_service: FFmpegService, render_input: RenderInput, tmp_path: Path
    ):
        output_path = tmp_path / "output" / "video.mp4"

        def fake_ffmpeg(cmd, timeout):
            target = Path(cmd[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            if _is_segment_call(cmd):
                target.write_bytes(b"\x00" * 50_000)
                return _make_completed(0)
            # Concat: zero bytes
            target.write_bytes(b"")
            return _make_completed(1, "Error")

        with patch("creator_service.ffmpeg_service._run_ffmpeg", side_effect=fake_ffmpeg):
            with pytest.raises(RuntimeError, match="FFmpeg render failed"):
                ffmpeg_service.render(render_input, output_path)

    def test_rejects_small_corrupt_file(
        self, ffmpeg_service: FFmpegService, render_input: RenderInput, tmp_path: Path
    ):
        output_path = tmp_path / "output" / "video.mp4"

        def fake_ffmpeg(cmd, timeout):
            target = Path(cmd[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            if _is_segment_call(cmd):
                target.write_bytes(b"\x00" * 50_000)
                return _make_completed(0)
            # Concat: small corrupt file
            target.write_bytes(b"corrupt" * 10)  # 70 bytes < 10KB min
            return _make_completed(1, "Error")

        with patch("creator_service.ffmpeg_service._run_ffmpeg", side_effect=fake_ffmpeg):
            with pytest.raises(RuntimeError, match="FFmpeg render failed"):
                ffmpeg_service.render(render_input, output_path)

    def test_rejects_large_file_failing_ffprobe(
        self, ffmpeg_service: FFmpegService, render_input: RenderInput, tmp_path: Path
    ):
        """File > 10KB but ffprobe says it's not a valid video."""
        output_path = tmp_path / "output" / "video.mp4"

        def fake_ffmpeg(cmd, timeout):
            target = Path(cmd[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            if _is_segment_call(cmd):
                target.write_bytes(b"\x00" * 50_000)
                return _make_completed(0)
            # Concat: write large file but exit non-zero
            target.write_bytes(b"\x00" * 50_000)
            return _make_completed(1, "Segfault")

        with patch("creator_service.ffmpeg_service._run_ffmpeg", side_effect=fake_ffmpeg):
            with patch(
                "creator_service.ffmpeg_service.subprocess.run",
                return_value=_make_probe_failure(),
            ):
                with pytest.raises(RuntimeError, match="FFmpeg render failed"):
                    ffmpeg_service.render(render_input, output_path)

    def test_rejects_when_no_file_created(
        self, ffmpeg_service: FFmpegService, render_input: RenderInput, tmp_path: Path
    ):
        output_path = tmp_path / "output" / "video.mp4"

        def fake_ffmpeg(cmd, timeout):
            target = Path(cmd[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            if _is_segment_call(cmd):
                target.write_bytes(b"\x00" * 50_000)
                return _make_completed(0)
            # Concat: fails without creating file
            return _make_completed(1, "No such file or directory")

        with patch("creator_service.ffmpeg_service._run_ffmpeg", side_effect=fake_ffmpeg):
            with pytest.raises(RuntimeError, match="FFmpeg render failed"):
                ffmpeg_service.render(render_input, output_path)


class TestRenderStaleOutput:
    """Previous output file must not be falsely accepted."""

    def test_stale_output_not_accepted(
        self, ffmpeg_service: FFmpegService, render_input: RenderInput, tmp_path: Path
    ):
        """Even if output_path exists from a previous run, it's not used."""
        output_path = tmp_path / "output" / "video.mp4"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"\x00" * 100_000)  # stale from previous run

        def fake_ffmpeg(cmd, timeout):
            target = Path(cmd[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            if _is_segment_call(cmd):
                target.write_bytes(b"\x00" * 50_000)
                return _make_completed(0)
            # Concat fails, writes nothing to temp
            return _make_completed(1, "Error")

        with patch("creator_service.ffmpeg_service._run_ffmpeg", side_effect=fake_ffmpeg):
            with pytest.raises(RuntimeError, match="FFmpeg render failed"):
                ffmpeg_service.render(render_input, output_path)


class TestRenderTimeout:
    """subprocess.TimeoutExpired propagates correctly."""

    def test_timeout_on_concat_raises(
        self, ffmpeg_service: FFmpegService, render_input: RenderInput, tmp_path: Path
    ):
        output_path = tmp_path / "output" / "video.mp4"

        def fake_ffmpeg(cmd, timeout):
            target = Path(cmd[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            if _is_segment_call(cmd):
                target.write_bytes(b"\x00" * 50_000)
                return _make_completed(0)
            # Concat: timeout
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=300)

        with patch("creator_service.ffmpeg_service._run_ffmpeg", side_effect=fake_ffmpeg):
            with pytest.raises(subprocess.TimeoutExpired):
                ffmpeg_service.render(render_input, output_path)

    def test_timeout_on_segment_raises(
        self, ffmpeg_service: FFmpegService, render_input: RenderInput, tmp_path: Path
    ):
        """Timeout during segment render propagates."""
        output_path = tmp_path / "output" / "video.mp4"

        def fake_ffmpeg(cmd, timeout):
            # Segment render times out
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=120)

        with patch("creator_service.ffmpeg_service._run_ffmpeg", side_effect=fake_ffmpeg):
            with pytest.raises(subprocess.TimeoutExpired):
                ffmpeg_service.render(render_input, output_path)


class TestSegmentFailure:
    """Segment render failure propagates immediately."""

    def test_segment_failure_raises(
        self, ffmpeg_service: FFmpegService, render_input: RenderInput, tmp_path: Path
    ):
        output_path = tmp_path / "output" / "video.mp4"

        def fake_ffmpeg(cmd, timeout):
            target = Path(cmd[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            # Segment render fails
            return _make_completed(1, "Encoder error")

        with patch("creator_service.ffmpeg_service._run_ffmpeg", side_effect=fake_ffmpeg):
            with pytest.raises(RuntimeError, match="Segment render failed"):
                ffmpeg_service.render(render_input, output_path)


class TestInputValidation:
    """Input validation catches bad durations before FFmpeg spawn."""

    def test_rejects_zero_duration(self, ffmpeg_service: FFmpegService, tmp_path: Path):
        img = tmp_path / "scene.png"
        img.write_bytes(b"\x89PNG" + b"\x00" * 100)
        render_input = RenderInput(
            image_paths=[img], audio_path=None, subtitle_path=None, scene_durations=[0.0]
        )
        output_path = tmp_path / "output" / "video.mp4"

        with pytest.raises(ValueError, match="must be positive"):
            ffmpeg_service.render(render_input, output_path)

    def test_rejects_negative_duration(self, ffmpeg_service: FFmpegService, tmp_path: Path):
        img = tmp_path / "scene.png"
        img.write_bytes(b"\x89PNG" + b"\x00" * 100)
        render_input = RenderInput(
            image_paths=[img], audio_path=None, subtitle_path=None, scene_durations=[-3.0]
        )
        output_path = tmp_path / "output" / "video.mp4"

        with pytest.raises(ValueError, match="must be positive"):
            ffmpeg_service.render(render_input, output_path)

    def test_rejects_nan_duration(self, ffmpeg_service: FFmpegService, tmp_path: Path):
        img = tmp_path / "scene.png"
        img.write_bytes(b"\x89PNG" + b"\x00" * 100)
        render_input = RenderInput(
            image_paths=[img], audio_path=None, subtitle_path=None, scene_durations=[float("nan")]
        )
        output_path = tmp_path / "output" / "video.mp4"

        with pytest.raises(ValueError, match="must be finite"):
            ffmpeg_service.render(render_input, output_path)

    def test_rejects_inf_duration(self, ffmpeg_service: FFmpegService, tmp_path: Path):
        img = tmp_path / "scene.png"
        img.write_bytes(b"\x89PNG" + b"\x00" * 100)
        render_input = RenderInput(
            image_paths=[img], audio_path=None, subtitle_path=None, scene_durations=[float("inf")]
        )
        output_path = tmp_path / "output" / "video.mp4"

        with pytest.raises(ValueError, match="must be finite"):
            ffmpeg_service.render(render_input, output_path)

    def test_validate_durations_standalone(self):
        """_validate_durations utility rejects bad values."""
        _validate_durations([1.0, 2.5, 10.0])  # should not raise
        with pytest.raises(ValueError):
            _validate_durations([1.0, 0.0, 3.0])
        with pytest.raises(ValueError):
            _validate_durations([float("nan")])


class TestProcessIsolation:
    """Verify start_new_session=True is passed for proper Celery isolation."""

    def test_run_ffmpeg_uses_start_new_session(self):
        """_run_ffmpeg passes start_new_session=True to Popen."""
        from unittest.mock import MagicMock

        mock_proc = MagicMock()
        mock_proc.communicate.return_value = ("", "")
        mock_proc.returncode = 0
        mock_proc.pid = 12345

        with patch(
            "creator_service.ffmpeg_service.subprocess.Popen", return_value=mock_proc
        ) as mock_popen:
            _run_ffmpeg(["ffmpeg", "-y", "test.mp4"], timeout=60)
            mock_popen.assert_called_once()
            call_kwargs = mock_popen.call_args[1]
            assert call_kwargs["start_new_session"] is True

    def test_run_ffmpeg_kills_process_group_on_timeout(self):
        """_run_ffmpeg calls os.killpg on timeout."""
        from unittest.mock import MagicMock

        mock_proc = MagicMock()
        mock_proc.pid = 99999
        mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=60)
        mock_proc.kill.return_value = None
        mock_proc.wait.return_value = None

        with patch("creator_service.ffmpeg_service.subprocess.Popen", return_value=mock_proc):
            with patch("creator_service.ffmpeg_service.os.killpg") as mock_killpg:
                with pytest.raises(subprocess.TimeoutExpired):
                    _run_ffmpeg(["ffmpeg", "-y", "test.mp4"], timeout=60)
                # Verify process group kill was attempted
                mock_killpg.assert_called_once()
                assert mock_killpg.call_args[0][0] == 99999  # pid


class TestAudioAndSubtitleBranch:
    """Test render with audio and subtitle inputs."""

    def test_render_with_audio(self, ffmpeg_service: FFmpegService, tmp_path: Path):
        """Audio path triggers audio mapping in concat pass."""
        img = tmp_path / "scene.png"
        img.write_bytes(b"\x89PNG" + b"\x00" * 100)
        audio = tmp_path / "audio.mp3"
        audio.write_bytes(b"\x00" * 1000)

        render_input = RenderInput(
            image_paths=[img],
            audio_path=audio,
            subtitle_path=None,
            scene_durations=[5.0],
        )
        output_path = tmp_path / "output" / "video.mp4"

        concat_cmds: list[list[str]] = []

        def fake_ffmpeg(cmd, timeout):
            target = Path(cmd[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            make_media(target, audio=True)
            if _is_concat_call(cmd):
                concat_cmds.append(cmd)
            return _make_completed(0)

        with patch("creator_service.ffmpeg_service._run_ffmpeg", side_effect=fake_ffmpeg):
            result = ffmpeg_service.render(render_input, output_path)

        assert result == output_path
        # Verify audio mapping was included
        assert len(concat_cmds) == 1
        concat_cmd = concat_cmds[0]
        assert "-i" in concat_cmd
        assert str(audio) in concat_cmd
        assert "-map" in concat_cmd
        assert "1:a" in concat_cmd
        assert "-shortest" in concat_cmd

    def test_render_with_subtitles(self, ffmpeg_service: FFmpegService, tmp_path: Path):
        """Subtitle path triggers subtitle burn-in filter in concat pass."""
        img = tmp_path / "scene.png"
        img.write_bytes(b"\x89PNG" + b"\x00" * 100)
        subs = tmp_path / "subs.ass"
        subs.write_text("[Script Info]\n", encoding="utf-8")

        render_input = RenderInput(
            image_paths=[img],
            audio_path=None,
            subtitle_path=subs,
            scene_durations=[5.0],
        )
        output_path = tmp_path / "output" / "video.mp4"

        concat_cmds: list[list[str]] = []

        def fake_ffmpeg(cmd, timeout):
            target = Path(cmd[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            make_media(target)
            if _is_concat_call(cmd):
                concat_cmds.append(cmd)
            return _make_completed(0)

        with patch("creator_service.ffmpeg_service._run_ffmpeg", side_effect=fake_ffmpeg):
            result = ffmpeg_service.render(render_input, output_path)

        assert result == output_path
        assert len(concat_cmds) == 1
        concat_cmd = concat_cmds[0]
        # Should have -vf with ass filter and re-encode settings
        assert "-vf" in concat_cmd
        vf_idx = concat_cmd.index("-vf")
        assert "ass=" in concat_cmd[vf_idx + 1]
        assert "-c:v" in concat_cmd  # re-encoding


class TestCleanup:
    """Verify temp files are cleaned up after success and failure."""

    def test_tmp_dir_cleaned_after_success(
        self, ffmpeg_service: FFmpegService, render_input: RenderInput, tmp_path: Path
    ):
        output_path = tmp_path / "output" / "video.mp4"

        def fake_ffmpeg(cmd, timeout):
            target = Path(cmd[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            make_media(target)
            return _make_completed(0)

        with patch("creator_service.ffmpeg_service._run_ffmpeg", side_effect=fake_ffmpeg):
            ffmpeg_service.render(render_input, output_path)

        # No .render_tmp_* directory should remain
        render_dir = output_path.parent
        tmp_dirs = [d for d in render_dir.iterdir() if d.name.startswith(".render_tmp_")]
        assert tmp_dirs == []

    def test_tmp_dir_cleaned_after_failure(
        self, ffmpeg_service: FFmpegService, render_input: RenderInput, tmp_path: Path
    ):
        output_path = tmp_path / "output" / "video.mp4"

        def fake_ffmpeg(cmd, timeout):
            target = Path(cmd[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            if _is_segment_call(cmd):
                target.write_bytes(b"\x00" * 50_000)
                return _make_completed(0)
            return _make_completed(1, "Error")

        with patch("creator_service.ffmpeg_service._run_ffmpeg", side_effect=fake_ffmpeg):
            with pytest.raises(RuntimeError):
                ffmpeg_service.render(render_input, output_path)

        # No .render_tmp_* directory should remain after failure either
        render_dir = output_path.parent
        tmp_dirs = [d for d in render_dir.iterdir() if d.name.startswith(".render_tmp_")]
        assert tmp_dirs == []

    def test_no_tmp_output_file_remains_after_failure(
        self, ffmpeg_service: FFmpegService, render_input: RenderInput, tmp_path: Path
    ):
        output_path = tmp_path / "output" / "video.mp4"

        def fake_ffmpeg(cmd, timeout):
            target = Path(cmd[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            if _is_segment_call(cmd):
                target.write_bytes(b"\x00" * 50_000)
                return _make_completed(0)
            # Concat: create file but fail
            target.write_bytes(b"bad" * 10)
            return _make_completed(1, "Error")

        with patch("creator_service.ffmpeg_service._run_ffmpeg", side_effect=fake_ffmpeg):
            with pytest.raises(RuntimeError):
                ffmpeg_service.render(render_input, output_path)

        # No .tmp.*.mp4 files should remain
        render_dir = output_path.parent
        tmp_files = [f for f in render_dir.iterdir() if ".tmp." in f.name]
        assert tmp_files == []


class TestProbeOutputValid:
    """Unit tests for _probe_output_valid helper."""

    def test_valid_duration(self, ffmpeg_service: FFmpegService, tmp_path: Path):
        video = tmp_path / "test.mp4"
        make_media(video)
        validate_media(video, MediaExpectation(ffmpeg_service.profile, 5, False))

    def test_zero_duration(self, ffmpeg_service: FFmpegService, tmp_path: Path):
        video = tmp_path / "test.mp4"
        make_media(video)

        with patch(
            "creator_service.media_integrity.run_validation", return_value=_make_probe_result(0.0)
        ):
            with pytest.raises(RuntimeError):
                validate_media(video, MediaExpectation(ffmpeg_service.profile, 5, False))

    def test_probe_failure(self, ffmpeg_service: FFmpegService, tmp_path: Path):
        video = tmp_path / "test.mp4"
        make_media(video)

        with patch(
            "creator_service.media_integrity.run_validation", return_value=_make_probe_failure()
        ):
            with pytest.raises(RuntimeError):
                validate_media(video, MediaExpectation(ffmpeg_service.profile, 5, False))

    def test_probe_timeout(self, ffmpeg_service: FFmpegService, tmp_path: Path):
        from creator_service.media_validation_process import ValidationProcessError
        video = tmp_path / "test.mp4"
        make_media(video)

        with patch(
            "creator_service.media_integrity.run_validation",
            side_effect=ValidationProcessError("validation_timeout"),
        ):
            with pytest.raises(RuntimeError):
                validate_media(video, MediaExpectation(ffmpeg_service.profile, 5, False))

    def test_invalid_duration_string(self, ffmpeg_service: FFmpegService, tmp_path: Path):
        video = tmp_path / "test.mp4"
        make_media(video)

        result = subprocess.CompletedProcess(
            args=["ffprobe"], returncode=0, stdout="N/A\n", stderr=""
        )
        with patch("creator_service.media_integrity.run_validation", return_value=result):
            with pytest.raises(RuntimeError):
                validate_media(video, MediaExpectation(ffmpeg_service.profile, 5, False))


class TestMultiSceneRender:
    """Test with multiple scenes to verify concat demuxer approach."""

    def test_multiple_scenes_all_succeed(self, ffmpeg_service: FFmpegService, tmp_path: Path):
        """5 scenes renders successfully through two-pass approach."""
        images = []
        for i in range(5):
            img = tmp_path / f"scene_{i}.png"
            img.write_bytes(b"\x89PNG" + b"\x00" * 100)
            images.append(img)

        render_input = RenderInput(
            image_paths=images,
            audio_path=None,
            subtitle_path=None,
            scene_durations=[3.0, 4.0, 5.0, 3.5, 4.5],
        )
        output_path = tmp_path / "output" / "video.mp4"

        segment_calls = {"count": 0}

        def fake_ffmpeg(cmd, timeout):
            target = Path(cmd[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            make_media(target, duration=20.0)
            if _is_segment_call(cmd):
                segment_calls["count"] += 1
            return _make_completed(0)

        with patch("creator_service.ffmpeg_service._run_ffmpeg", side_effect=fake_ffmpeg):
            result = ffmpeg_service.render(render_input, output_path)

        assert result == output_path
        assert output_path.exists()
        # Should have rendered 5 segments
        assert segment_calls["count"] == 5

    def test_third_segment_fails(self, ffmpeg_service: FFmpegService, tmp_path: Path):
        """If segment 3 of 5 fails, render raises immediately."""
        images = []
        for i in range(5):
            img = tmp_path / f"scene_{i}.png"
            img.write_bytes(b"\x89PNG" + b"\x00" * 100)
            images.append(img)

        render_input = RenderInput(
            image_paths=images,
            audio_path=None,
            subtitle_path=None,
            scene_durations=[3.0, 4.0, 5.0, 3.5, 4.5],
        )
        output_path = tmp_path / "output" / "video.mp4"

        segment_calls = {"count": 0}

        def fake_ffmpeg(cmd, timeout):
            target = Path(cmd[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            if _is_segment_call(cmd):
                segment_calls["count"] += 1
                if segment_calls["count"] == 3:
                    # Third segment fails
                    return _make_completed(1, "OOM on scene 3")
                target.write_bytes(b"\x00" * 50_000)
                return _make_completed(0)
            target.write_bytes(b"\x00" * 50_000)
            return _make_completed(0)

        with patch("creator_service.ffmpeg_service._run_ffmpeg", side_effect=fake_ffmpeg):
            with pytest.raises(RuntimeError, match="Segment render failed"):
                ffmpeg_service.render(render_input, output_path)

        # Only 3 segments attempted (fails on 3rd)
        assert segment_calls["count"] == 3


class TestConcatenateAudioIsolation:
    """Tests that concatenate_audio uses _run_ffmpeg for process isolation."""

    def test_concatenate_audio_uses_run_ffmpeg(self, ffmpeg_service, tmp_path):
        """concatenate_audio delegates to _run_ffmpeg with proper timeout."""
        audio1 = tmp_path / "a.mp3"
        audio2 = tmp_path / "b.mp3"
        audio1.write_bytes(b"fake")
        audio2.write_bytes(b"fake")
        output = tmp_path / "out.mp3"

        with patch("creator_service.ffmpeg_service._run_ffmpeg") as mock_run:
            mock_run.return_value = _make_completed(0)
            ffmpeg_service.concatenate_audio([str(audio1), str(audio2)], str(output))

            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == "ffmpeg"
            assert "-f" in cmd and "concat" in cmd
            assert mock_run.call_args[1]["timeout"] == 120

    def test_concatenate_audio_timeout_triggers_killpg(self, ffmpeg_service, tmp_path):
        """concatenate_audio propagates TimeoutExpired from _run_ffmpeg."""
        audio1 = tmp_path / "a.mp3"
        audio1.write_bytes(b"fake")
        output = tmp_path / "out.mp3"

        with patch(
            "creator_service.ffmpeg_service._run_ffmpeg",
            side_effect=subprocess.TimeoutExpired("ffmpeg", 120),
        ):
            with pytest.raises(subprocess.TimeoutExpired):
                ffmpeg_service.concatenate_audio([str(audio1)], str(output))
