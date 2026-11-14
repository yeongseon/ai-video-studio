"""Scratch placement and fail-closed configuration through public render entry points."""

from __future__ import annotations

import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from PIL import Image

from creator_service import ffmpeg_service, render_segments
from creator_service.bgm_service import BgmService
from creator_service.ffmpeg_service import FFmpegService, RenderInput
from creator_service.render_profile import RenderProfile
from creator_domain.models import RenderPlan
from creator_service.render_scratch import configured_scratch_root
from creator_service.job_capacity import JobCapacityPolicy, ensure_job_capacity
from creator_service.storage_policy import FilesystemLowSpace
from .test_job_capacity import FakeDisk


@pytest.fixture
def inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RenderInput:
    monkeypatch.delenv("RENDER_SCRATCH_DIR", raising=False)
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    image = tmp_path / "scene.png"
    Image.new("RGB", (64, 64), "red").save(image)
    return RenderInput([image], None, None, [0.4])


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("plan", [False, True])
def test_real_render_preserves_layout_and_cleans_scratch(
    inputs: RenderInput, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    configured: bool, plan: bool,
) -> None:
    # Given a real short render and a separate configured scratch volume when enabled.
    scratch = tmp_path / "scratch"
    if configured:
        monkeypatch.setenv("RENDER_SCRATCH_DIR", str(scratch))
    output = tmp_path / "artifacts" / "1" / "render" / "output.mp4"
    profile = RenderProfile(width=64, height=64, preset="ultrafast")
    observed: list[Path] = []
    original = ffmpeg_service._run_ffmpeg

    def observe(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        target = Path(cmd[-1])
        result = original(cmd, timeout)
        observed.append(target)
        if configured and target.suffix == ".ts":
            assert target.parent.stat().st_mode & 0o777 == 0o700
        return result

    monkeypatch.setattr(ffmpeg_service, "_run_ffmpeg", observe)
    monkeypatch.setattr(render_segments, "_run_ffmpeg", observe)
    # When the production renderer encodes and publishes it.
    if plan:
        render_segments.render_plan(RenderPlan.model_validate({
            "segments": [{"kind": "image", "source": str(inputs.image_paths[0]),
                          "timeline_start_seconds": 0, "duration_seconds": 0.4}],
            "output_spec": {"width": 64, "height": 64, "fps": 30},
            "encoding_profile": {"name": "test", "preset": "ultrafast", "crf": 23},
        }), output)
    else:
        FFmpegService(profile).render(inputs, output)
    # Then only segments move; publication keeps the same name and atomic sibling MP4.
    directory = observed[0].parent
    prefix = ".render_plan_" if plan else ".render_tmp_"
    assert directory.parent == (scratch if configured else output.parent)
    assert directory.name.startswith(prefix)
    if not configured:
        assert re.fullmatch(re.escape(prefix) + r"\d+_[0-9a-f]{8}", directory.name)
    assert re.fullmatch(r"output\.tmp\.\d+\.[0-9a-f]{8}\.mp4", observed[-1].name)
    assert observed[-1].parent == output.parent
    assert sorted(p.name for p in output.parent.iterdir()) == ["output.mp4"]
    assert not directory.exists()
    if configured:
        assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("invalid", ["relative", "file", "symlink", "ancestor_symlink", "unwritable", "artifact"])
def test_invalid_configuration_fails_before_render_work(
    inputs: RenderInput, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str,
) -> None:
    # Given an unsafe operator setting and an existing published artifact.
    root = tmp_path / "scratch"
    root.mkdir()
    output = tmp_path / "artifacts" / "output.mp4"
    output.parent.mkdir()
    output.write_bytes(b"published")
    match invalid:
        case "relative":
            configured = Path("relative/scratch")
        case "file":
            configured = tmp_path / "file"
            configured.write_bytes(b"file")
        case "symlink" | "ancestor_symlink":
            configured = tmp_path / "link"
            configured.symlink_to(root, target_is_directory=True)
            if invalid == "ancestor_symlink":
                configured /= "nested"
        case "unwritable":
            root.chmod(0o500)
            configured = root
        case "artifact":
            configured = output.parent / "scratch"
        case _:
            raise AssertionError(invalid)
    monkeypatch.setenv("RENDER_SCRATCH_DIR", str(configured))
    calls: list[list[str]] = []

    def forbidden(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        raise AssertionError("render work started with invalid RENDER_SCRATCH_DIR")

    monkeypatch.setattr(ffmpeg_service, "_run_ffmpeg", forbidden)
    # When rendering is requested, configuration is rejected before FFmpeg or scratch writes.
    try:
        with pytest.raises(ValueError, match="RENDER_SCRATCH_DIR"):
            FFmpegService().render(inputs, output)
    finally:
        root.chmod(0o700)
    # Then the published artifact and unrelated paths are untouched.
    assert calls == []
    assert output.read_bytes() == b"published"
    assert sorted(p.name for p in output.parent.iterdir()) == ["output.mp4"]


def test_concurrent_renders_own_distinct_private_directories_and_cleanup_only_their_own(
    inputs: RenderInput, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given two renders sharing an output directory, with an unrelated outside file.
    scratch = tmp_path / "scratch"
    monkeypatch.setenv("RENDER_SCRATCH_DIR", str(scratch))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"keep")
    barrier = Barrier(2)
    observed: list[Path] = []

    def failing_segment(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        directory = Path(cmd[-1]).parent
        observed.append(directory)
        (directory / "link").symlink_to(outside, target_is_directory=True)
        barrier.wait(timeout=10)
        raise RuntimeError("injected encoder failure")

    monkeypatch.setattr(ffmpeg_service, "_run_ffmpeg", failing_segment)

    def render(index: int) -> None:
        with pytest.raises(RuntimeError, match="injected encoder failure"):
            FFmpegService().render(inputs, tmp_path / "artifacts" / "7" / f"{index}.mp4")

    # When both fail concurrently after acquiring scratch.
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(render, range(2)))
    # Then neither render shared scratch or followed its cleanup symlink outside.
    assert len(set(observed)) == 2
    assert all(path.parent == scratch for path in observed)
    assert list(scratch.iterdir()) == []
    assert (outside / "keep").read_bytes() == b"keep"


def test_scratch_floor_measures_configured_filesystem_without_creating_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a not-yet-created configured root below an existing low-space volume.
    scratch = tmp_path / "volume" / "new" / "scratch"
    scratch.parents[1].mkdir(mode=0o700)
    monkeypatch.setenv("RENDER_SCRATCH_DIR", str(scratch))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    disk = FakeDisk({scratch.parents[1]: 1})
    monkeypatch.setattr("creator_service.job_capacity.shutil.disk_usage", disk)
    # When admission uses the validated configured location.
    with pytest.raises(FilesystemLowSpace):
        ensure_job_capacity("render_video", policy=JobCapacityPolicy(scratch_low_watermark_bytes=2),
                            scratch_root=configured_scratch_root())
    # Then its filesystem was measured with no scratch creation.
    assert disk.queried == [scratch.parents[1]]
    assert list(scratch.parents[1].iterdir()) == []


def test_audio_concat_and_sfx_use_configured_scratch(
    inputs: RenderInput, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given auxiliary audio helpers and a scratch volume outside artifacts.
    scratch = tmp_path / "scratch"
    monkeypatch.setenv("RENDER_SCRATCH_DIR", str(scratch))
    observed: list[Path] = []

    def concat(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        observed.append(Path(cmd[cmd.index("-i") + 1]))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def sfx(cmd: list[str], **kwargs: str | int | bool) -> subprocess.CompletedProcess[str]:
        if "-filter_complex" in cmd:
            observed.append(Path(cmd[cmd.index("-filter_complex") - 1]))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ffmpeg_service, "_run_ffmpeg", concat)
    monkeypatch.setattr("creator_service.bgm_service._run_ffmpeg", sfx)
    # When each helper prepares its transient input.
    FFmpegService().concatenate_audio(["voice.wav"], str(tmp_path / "artifacts" / "audio.wav"))
    BgmService().mix_sfx_at_timestamps("voice.wav", [(0, "ping")], str(tmp_path / "artifacts" / "mixed.mp3"))
    # Then both files were under owned scratch directories, which were cleaned.
    assert len(observed) == 2
    assert all(path.is_relative_to(scratch) for path in observed)
    assert list(scratch.iterdir()) == []
