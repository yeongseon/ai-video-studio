from __future__ import annotations

import shutil
from pathlib import Path

import anyio
import pytest
from tasks import timeline_render
from tasks.render_video import render_video

from .test_timeline_worker import WorkerFixture, worker as worker  # noqa: F401


@pytest.mark.parametrize("replacement", ["missing", "symlink"])
def test_published_timeline_survives_scratch_replacement(
    worker: WorkerFixture, tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch, replacement: str,
) -> None:
    # Given an approved Timeline, separate scratch, and an unrelated publication.
    root = tmp_path_factory.mktemp("timeline-scratch")
    monkeypatch.setenv("RENDER_SCRATCH_DIR", str(root))
    victim = tmp_path_factory.mktemp("victim")
    keep = victim / "output.mp4"
    keep.write_bytes(b"outside publication")
    run_id = anyio.run(worker.seed)
    original = timeline_render.TimelineFFmpegService.render

    def publish_then_replace(
        renderer: timeline_render.TimelineFFmpegService,
        inputs: timeline_render.RenderInput, output: Path,
    ) -> Path:
        result = original(renderer, inputs, output)
        directory = next(path for path in root.iterdir() if path.name.startswith("timeline-"))
        shutil.rmtree(directory)
        if replacement == "symlink":
            directory.symlink_to(victim, target_is_directory=True)
        return result

    monkeypatch.setattr(timeline_render.TimelineFFmpegService, "render", publish_then_replace)
    # When the real worker publishes and unwinds the Timeline context.
    result = render_video.run(run_id=run_id, render_profile="fast_preview")
    # Then cleanup neither fails the task nor deletes the outside publication.
    assert result["status"] == "success"
    assert keep.read_bytes() == b"outside publication"
    artifact = anyio.run(worker.renders.get_latest, run_id)
    assert artifact is not None and Path(artifact.path).is_file()
