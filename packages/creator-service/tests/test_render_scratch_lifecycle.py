from __future__ import annotations

import shutil
import errno
import logging
import os
import subprocess
import tempfile
from pathlib import Path

import pytest
from creator_domain.models import RenderPlan
from creator_service import ffmpeg_service, render_segments
from creator_service.bgm_service import BgmService
from creator_service.ffmpeg_service import FFmpegService, RenderInput
from creator_service.render_scratch import render_scratch


@pytest.mark.parametrize("renderer", ["context", "plan"])
def test_cleanup_warns_once_without_exposing_child_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, renderer: str,
) -> None:
    # Given a child whose deletion fails, followed by a nonempty-directory failure.
    monkeypatch.setenv("RENDER_SCRATCH_DIR", str(tmp_path / "scratch"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    original = os.unlink
    observed: list[Path] = []

    def blocked(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> None:
        if Path(path).name == "private-source.wav":
            raise PermissionError(errno.EACCES, "secret path", str(path))
        original(path, dir_fd=dir_fd)

    def fail_segment(segment: RenderInput, output: Path, **kwargs: int) -> Path:
        observed.append(output.parent)
        (output.parent / "private-source.wav").write_bytes(b"media")
        raise RuntimeError("encoder failure")

    monkeypatch.setattr(os, "unlink", blocked)
    monkeypatch.setattr(render_segments, "render_image_segment", fail_segment)
    # When cleanup handles the failed deletion, then it never replaces the render outcome.
    with caplog.at_level(logging.WARNING):
        if renderer == "context":
            with render_scratch(tmp_path / "artifacts" / "output.mp4", "timeline-") as directory:
                observed.append(directory)
                (directory / "private-source.wav").write_bytes(b"media")
        else:
            plan = RenderPlan.model_validate({
                "segments": [{"kind": "image", "source": "image.png", "duration_seconds": 1,
                              "timeline_start_seconds": 0}],
                "output_spec": {"width": 64, "height": 64, "fps": 30},
                "encoding_profile": {"name": "test", "preset": "ultrafast", "crf": 23},
            })
            with pytest.raises(RuntimeError, match="encoder failure"):
                render_segments.render_plan(plan, tmp_path / "artifacts" / "output.mp4")
    records = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(records) == 1
    record = records[0]
    assert record.name == ("creator_service.render_scratch" if renderer == "context" else "creator_service.render_segments")
    assert record.__dict__["scratch_directory"] == observed[0].name
    assert record.__dict__["errno"] == errno.EACCES
    assert record.exc_info is None
    assert "private-source.wav" not in caplog.text
    assert str(tmp_path) not in caplog.text


@pytest.mark.parametrize("renderer", ["legacy", "plan", "sfx", "unset_sfx"])
def test_output_parent_failure_cleans_allocated_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, renderer: str,
) -> None:
    # Given an output parent that is a file, and isolated configured/system scratch.
    root = tmp_path / "scratch"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("RENDER_SCRATCH_DIR", str(root))
    if renderer == "unset_sfx":
        monkeypatch.delenv("RENDER_SCRATCH_DIR")
        monkeypatch.setattr(tempfile, "tempdir", str(root))
    parent = tmp_path / "blocked"
    parent.write_bytes(b"not a directory")
    output = parent / "output.mp4"
    # When the public entry point fails to create its output parent.
    with pytest.raises(OSError):
        match renderer:
            case "legacy":
                FFmpegService().render(RenderInput([tmp_path / "image.png"], None, None, [1]), output)
            case "plan":
                render_segments.render_plan(RenderPlan.model_validate({
                    "segments": [{"kind": "image", "source": "image.png", "duration_seconds": 1,
                                  "timeline_start_seconds": 0}],
                    "output_spec": {"width": 64, "height": 64, "fps": 30},
                    "encoding_profile": {"name": "test", "preset": "ultrafast", "crf": 23},
                }), output)
            case "sfx" | "unset_sfx":
                BgmService().mix_sfx_at_timestamps("voice.wav", [(0, "ping")], str(output))
            case _:
                raise AssertionError(renderer)
    # Then neither configured scratch nor the old system-temp SFX directory leaks.
    assert list(root.iterdir()) == []


def test_plan_cleanup_does_not_follow_swapped_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a victim outside the render scratch tree.
    root, victim = tmp_path / "scratch", tmp_path / "victim"
    victim.mkdir()
    keep = victim / "output.mp4"
    keep.write_bytes(b"published")
    monkeypatch.setenv("RENDER_SCRATCH_DIR", str(root))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))

    def swap(segment: RenderInput, output: Path, **kwargs: int) -> Path:
        shutil.rmtree(output.parent)
        output.parent.symlink_to(victim, target_is_directory=True)
        raise RuntimeError("encoder failure after swap")

    monkeypatch.setattr(render_segments, "render_image_segment", swap)
    plan = RenderPlan.model_validate({
        "segments": [{"kind": "image", "source": "image.png", "duration_seconds": 1,
                      "timeline_start_seconds": 0}],
        "output_spec": {"width": 64, "height": 64, "fps": 30},
        "encoding_profile": {"name": "test", "preset": "ultrafast", "crf": 23},
    })
    # When failure unwinds the segment-plan renderer.
    with pytest.raises(RuntimeError, match="encoder failure after swap"):
        render_segments.render_plan(plan, tmp_path / "artifacts" / "output.mp4")
    # Then cleanup never traverses the replacement into published files.
    assert keep.read_bytes() == b"published"


@pytest.mark.parametrize("replacement", ["missing", "symlink"])
def test_timeline_context_cleanup_preserves_outside_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str,
) -> None:
    # Given the same context used by render_saved_timeline and an outside publication.
    monkeypatch.setenv("RENDER_SCRATCH_DIR", str(tmp_path / "scratch"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    victim = tmp_path / "victim"
    victim.mkdir()
    keep = victim / "output.mp4"
    keep.write_bytes(b"published")
    # When another actor removes or replaces scratch after successful publication.
    with render_scratch(tmp_path / "artifacts" / "output.mp4", "timeline-") as directory:
        shutil.rmtree(directory)
        if replacement == "symlink":
            directory.symlink_to(victim, target_is_directory=True)
    # Then publication remains successful and no outside file was deleted.
    assert keep.read_bytes() == b"published"


@pytest.mark.parametrize("configured", [False, True])
def test_audio_concat_writes_through_creating_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: bool,
) -> None:
    # Given a concat invocation and a trap for reopening the temporary list by path.
    monkeypatch.delenv("RENDER_SCRATCH_DIR", raising=False)
    if configured:
        monkeypatch.setenv("RENDER_SCRATCH_DIR", str(tmp_path / "scratch"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    original = Path.open

    def guarded_open(path: Path, mode: str = "r", *, encoding: str | None = None, errors: str | None = None):
        if mode == "w" and (path.name == "concat.txt" or path.suffix == ".txt"):
            raise AssertionError("concat list reopened by path")
        return original(path, mode, encoding=encoding, errors=errors)

    observed: list[str] = []

    def ffmpeg(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        observed.append(Path(cmd[cmd.index("-i") + 1]).read_text())
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(ffmpeg_service, "_run_ffmpeg", ffmpeg)
    # When concatenation prepares FFmpeg's input.
    FFmpegService().concatenate_audio([str(tmp_path / "voice.wav")], str(tmp_path / "artifacts" / "audio.wav"))
    # Then flushed contents reach FFmpeg without a close/reopen window.
    assert observed == [f"file '{tmp_path / 'voice.wav'}'\n"]
