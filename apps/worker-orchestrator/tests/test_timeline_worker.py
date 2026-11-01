"""Exercise the real Celery task body, compiler, storage IO and FFmpeg."""
from __future__ import annotations

import importlib
import io
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import anyio
import pytest
from PIL import Image
from creator_domain.models import MediaSegment, Timeline
from creator_service import artifact_storage_integration
from creator_service.audio_service import AudioService
from creator_service.media_asset_service import InMemoryMediaAssetStorage, MediaAssetService
from creator_service.object_storage import LocalStorageBackend
from creator_service.render_service import RenderService
from creator_service.run_service import InMemoryRunStorage, RunService
from creator_service.stage_review_service import InMemoryStageReviewStorage, StageReviewService
from creator_service.subtitle_service import SubtitleService
from creator_service.task_tracking_service import InMemoryTaskTrackingStorage, TaskTrackingService
from creator_service.timeline_service import TimelineService
from tasks import render_video, task_runner

REAL_STORE = artifact_storage_integration.store_artifact_file


@dataclass(frozen=True, slots=True)
class OwnerLookup:
    assets: MediaAssetService

    async def get_asset_owner(self, asset_id: int, workspace_id: int) -> int | None:
        asset = await self.assets.get_asset(asset_id, workspace_id)
        return asset.project_id if asset else None


@dataclass(frozen=True, slots=True)
class WorkerFixture:
    root: Path
    assets: MediaAssetService
    timelines: TimelineService
    runs: InMemoryRunStorage
    renders: RenderService
    backend: LocalStorageBackend
    reviews: StageReviewService

    async def seed(self, *, mixed: bool = False, revision: int = 1, approved: bool = True) -> int:
        image = io.BytesIO()
        Image.new("RGB", (96, 160), "red").save(image, format="PNG")
        asset = await self.assets.create_generated_image_asset(
            workspace_id=7, project_id=11, filename="still.png", data=image.getvalue(),
        )
        segments = [MediaSegment(
            id="still", scene_id="scene", asset_id=asset.id,
            timeline_start_seconds=0, duration_seconds=0.4, transition="cut",
        )]
        if mixed:
            source = self.root / "source.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                "color=c=blue:s=96x160:r=15:d=1", "-c:v", "libx264", str(source),
            ], check=True, capture_output=True, timeout=30)
            video = await self.assets.create_video_asset(
                workspace_id=7, project_id=11, filename="clip.mp4",
                data=source.read_bytes(), content_type="video/mp4",
            )
            segments.append(MediaSegment(
                id="video", scene_id="scene", asset_id=video.id,
                timeline_start_seconds=0.4, duration_seconds=0.4,
                trim_start_seconds=0.2, trim_end_seconds=0.6, transition="cut",
            ))
        await self.timelines.save_timeline(
            project_id=11, workspace_id=7, expected_revision=0,
            timeline=Timeline(id="worker-test", project_id=11, segments=segments),
        )
        row = await self.runs.create_run({
            "project_id": 11, "current_stage": "RENDER_GENERATING", "status": "running",
            "metadata_json": json.dumps({
                "render_source": "timeline", "render_timeline_revision": revision,
            }),
        })
        run_id = int(row["id"])
        if approved:
            await self.reviews.record_approval(
                run_id, "TIMELINE_REVIEW", reviewer="7",
                notes=json.dumps({"timeline_id": "worker-test", "revision": revision}),
            )
        return run_id


@pytest.fixture
def worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WorkerFixture:
    backend = LocalStorageBackend(str(tmp_path))
    assets = MediaAssetService(backend, InMemoryMediaAssetStorage())
    timelines = TimelineService(asset_owner=OwnerLookup(assets))
    runs = InMemoryRunStorage()
    renders = RenderService()
    reviews = StageReviewService(InMemoryStageReviewStorage())
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setattr(render_video, "_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(render_video, "_render_service", renders)
    monkeypatch.setattr(task_runner, "_run_service", RunService(runs))
    monkeypatch.setattr(task_runner, "_task_tracking_service", TaskTrackingService(InMemoryTaskTrackingStorage()))
    monkeypatch.setattr("creator_service.media_asset_service.media_asset_service", assets)
    monkeypatch.setattr("creator_service.timeline_service.timeline_service", timelines)
    monkeypatch.setattr("creator_service.stage_review_service.stage_review_service", reviews)
    monkeypatch.setattr("creator_service.object_storage._storage_backend", backend)
    monkeypatch.setattr(artifact_storage_integration, "_ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(artifact_storage_integration, "store_artifact_file", REAL_STORE)

    async def workspace_id(run_id: int) -> int:
        return 7

    monkeypatch.setattr(task_runner, "resolve_workspace_id_from_run", workspace_id)
    return WorkerFixture(tmp_path, assets, timelines, runs, renders, backend, reviews)


@pytest.mark.parametrize("mixed", [False, True])
def test_worker_renders_saved_timeline_to_real_mp4(worker: WorkerFixture, mixed: bool) -> None:
    # Given persisted assets and an approved saved timeline, with no legacy visuals.
    async def seed() -> int:
        return await worker.seed(mixed=mixed)

    run_id = anyio.run(seed)
    # When the production worker task runs (no compiler/renderer/manifest mocks).
    result = render_video.render_video.run(run_id=run_id, render_profile="fast_preview")
    # Then a stored, decodable MP4 reaches the existing final-review transition.
    assert result["status"] == "success"
    artifact = anyio.run(worker.renders.get_latest, run_id)
    assert artifact is not None
    output = Path(artifact.path)
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height:format=duration",
        "-of", "json", str(output),
    ], check=True, capture_output=True, text=True, timeout=30)
    metadata = json.loads(probe.stdout)
    assert metadata["streams"][0]["codec_name"] == "h264"
    assert metadata["streams"][0]["width"] == 540
    assert float(metadata["format"]["duration"]) == pytest.approx(0.8 if mixed else 0.4, abs=0.15)
    assert worker.backend.download_bytes(str(output.relative_to(worker.root))) == output.read_bytes()
    row = anyio.run(worker.runs.get_run, run_id)
    assert row is not None and row["current_stage"] == "FINAL_REVIEW"
    if mixed:
        pixels = subprocess.run([
            "ffmpeg", "-v", "error", "-ss", "0.6", "-i", str(output),
            "-frames:v", "1", "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ], check=True, capture_output=True, timeout=30).stdout
        assert pixels[2] > pixels[0] + 100, "the VIDEO segment must actually appear"


def test_worker_rejects_unapproved_revision(worker: WorkerFixture) -> None:
    # Given approval for a revision other than the saved revision.
    async def seed() -> int:
        return await worker.seed(revision=2)

    run_id = anyio.run(seed)
    # When the real task runs, then it fails before rendering or persisting output.
    with pytest.raises(RuntimeError, match="revision"):
        render_video.render_video.run(run_id=run_id, render_profile="fast_preview")
    assert anyio.run(worker.renders.get_latest, run_id) is None


def test_imports_are_from_worktree() -> None:
    root = Path(__file__).resolve().parents[3]
    for name in ("creator_domain", "creator_service", "creator_provider", "tasks.render_video"):
        module = importlib.import_module(name)
        assert module.__file__ is not None
        assert Path(module.__file__).resolve().is_relative_to(root), module.__file__


def test_approved_timeline_missing_narration_fails_terminal(worker: WorkerFixture,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock
    import shutil
    from creator_provider.exceptions import ProviderError
    # Given: approved Timeline with requested narration and matching video-only output.
    audio = AudioService()
    monkeypatch.setattr(render_video, "_audio_service", audio)
    voice = worker.root / "voice.wav"
    video = worker.root / "video.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=0.4",
                    str(voice)], check=True, capture_output=True, timeout=30)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=size=540x960:rate=15:duration=0.4", "-c:v", "libx264",
                    "-threads", "1", "-preset", "ultrafast", str(video)],
                   check=True, capture_output=True, timeout=30)

    async def seed() -> int:
        run_id = await worker.seed()
        await audio.create_artifact(run_id, str(voice))
        return run_id

    run_id = anyio.run(seed)
    def encode(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        shutil.copyfile(video, cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("creator_service.ffmpeg_service._run_ffmpeg", encode)
    upload, usage, retry = Mock(), Mock(), Mock()
    monkeypatch.setattr(artifact_storage_integration, "store_artifact_file", upload)
    monkeypatch.setattr(render_video, "record_provider_call", usage)
    monkeypatch.setattr(render_video.render_video, "retry", retry)
    # When / Then: typed integrity rejection is terminal on the Timeline path too.
    with pytest.raises(ProviderError) as error:
        render_video.render_video.run(run_id=run_id, render_profile="fast_preview")
    assert type(error.value) is ProviderError
    retry.assert_not_called()
    upload.assert_not_called()
    usage.assert_not_called()
    assert anyio.run(worker.renders.get_latest, run_id) is None
    row = anyio.run(worker.runs.get_run, run_id)
    assert row is not None and row["current_stage"] == "FAILED"


def test_worker_keeps_narration_and_burns_subtitles(worker: WorkerFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = AudioService()
    subtitles = SubtitleService()
    monkeypatch.setattr(render_video, "_audio_service", audio)
    monkeypatch.setattr(render_video, "_subtitle_service", subtitles)
    voice = worker.root / "voice.wav"
    subtitle = worker.root / "captions.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:00,400\nVISIBLE CAPTION\n")
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.4", str(voice),
    ], check=True, capture_output=True, timeout=30)

    async def seed() -> int:
        run_id = await worker.seed(mixed=True)
        await audio.create_artifact(run_id=run_id, path=str(voice))
        await subtitles.create_artifact(run_id=run_id, path=str(subtitle))
        return run_id

    run_id = anyio.run(seed)
    result = render_video.render_video.run(run_id=run_id, render_profile="fast_preview")
    output = result["video_path"]
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_name",
        "-of", "csv=p=0", str(output),
    ], check=True, capture_output=True, text=True, timeout=30)
    assert "aac" in probe.stdout
    duration = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", str(output),
    ], check=True, capture_output=True, text=True, timeout=30).stdout
    assert float(duration) == pytest.approx(0.8, abs=0.15)
    pixels = subprocess.run([
        "ffmpeg", "-v", "error", "-ss", "0.2", "-i", str(output), "-frames:v", "1",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ], check=True, capture_output=True, timeout=30).stdout
    assert any(r > 200 and g > 200 and b > 200 for r, g, b in zip(pixels[::3], pixels[1::3], pixels[2::3])), "subtitle pixels must be burned"
