from __future__ import annotations

import json
import tempfile
from collections import namedtuple
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import shutil
import subprocess
from unittest.mock import Mock
from creator_service import job_capacity
from creator_service.ffmpeg_service import FFmpegService
from creator_service.media_integrity import MediaIntegrityError
from creator_service.render_profile import RenderProfile
from creator_service.storage_policy import FilesystemLowSpace
from creator_service.quality_profile import get_quality_profile
from tasks import render_video as render_video_module


@pytest.fixture
def no_pacing_split(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the pacing sub-beat split so scene counts stay 1:1.

    render_video splits scenes longer than the profile's max_scene_duration
    into two sub-beats. These orchestration tests predate that feature and
    assert one image path per scene, so raise the threshold above any test
    duration to keep the mapping 1:1.
    """

    def _profile(name: str = "ssul_v2"):
        return replace(get_quality_profile(name), max_scene_duration=1_000_000.0)

    monkeypatch.setattr(
        "creator_service.quality_profile.get_quality_profile", _profile
    )
    monkeypatch.setattr(
        "creator_service.recipe_profile.get_quality_profile", _profile
    )


class FakeStorage:
    def __init__(self, runs: dict[int, dict[str, object]] | None = None) -> None:
        self._runs: dict[int, dict[str, object]] = runs or {}
        self.calls: list[tuple[int, dict[str, object]]] = []
        self.cas_calls: list[tuple[int, dict[str, object], frozenset[str]]] = []

    async def get_run(self, run_id: int) -> dict[str, object] | None:
        return self._runs.get(run_id)

    async def conditional_update_run(
        self,
        run_id: int,
        updates: dict[str, object],
        expected_stages: frozenset[str],
    rejected_statuses: frozenset[str] | None = None,
    ) -> tuple[bool, dict[str, object] | None]:
        self.cas_calls.append((run_id, updates, expected_stages))
        row = self._runs.get(run_id)
        if row is None:
            return False, None
        if row.get("current_stage") not in expected_stages:
            return False, dict(row)
        self.calls.append((run_id, updates))
        row.update(updates)
        self._runs[run_id] = row
        return True, dict(row)


def _make_storage(run_id: int = 201, stage: str = "RENDER_GENERATING") -> FakeStorage:
    run_row: dict[str, object] = {"id": run_id, "current_stage": stage}
    return FakeStorage(runs={run_id: run_row})


@dataclass
class FakeVisualAsset:
    scene_id: str
    asset_path: str
    prompt_snapshot: str
    is_active: bool


class FakeVisualAssetService:
    def __init__(self, grouped_assets: dict[str, list[FakeVisualAsset]] | None = None) -> None:
        self.grouped_assets = grouped_assets or {}

    async def list_by_run(self, _run_id: int) -> dict[str, list[FakeVisualAsset]]:
        return self.grouped_assets


@dataclass
class FakeAudioArtifact:
    path: str
    section_id: str | None = None


class FakeAudioService:
    def __init__(
        self,
        artifact: FakeAudioArtifact | None = None,
        paragraph_artifacts: list[FakeAudioArtifact] | None = None,
    ) -> None:
        self.artifact = artifact
        self.paragraph_artifacts = paragraph_artifacts or []

    async def get_latest(self, _run_id: int) -> FakeAudioArtifact | None:
        return self.artifact

    async def list_paragraph_audio(self, _run_id: int) -> list[FakeAudioArtifact]:
        return self.paragraph_artifacts


@dataclass
class FakeSubtitleArtifact:
    path: str
    section_id: str | None = None


class FakeSubtitleService:
    def __init__(
        self,
        artifact: FakeSubtitleArtifact | None = None,
        paragraph_artifacts: list[FakeSubtitleArtifact] | None = None,
    ) -> None:
        self.artifact = artifact
        self.paragraph_artifacts = paragraph_artifacts or []

    async def get_latest(self, _run_id: int) -> FakeSubtitleArtifact | None:
        return self.artifact

    async def list_paragraph_subtitles(self, _run_id: int) -> list[FakeSubtitleArtifact]:
        return self.paragraph_artifacts


@dataclass
class FakePlanScene:
    scene_id: str
    section_id: str


@dataclass
class FakeVisualPlan:
    scenes: list[FakePlanScene]


class FakeVisualPlanService:
    def __init__(self, plan: FakeVisualPlan | None = None) -> None:
        self.plan = plan

    async def get_active_plan(self, _run_id: int) -> FakeVisualPlan | None:
        return self.plan


@dataclass
class FakeScriptSection:
    section_id: str


@dataclass
class FakeScriptDraft:
    structured_script: list[FakeScriptSection] | None


class FakeScriptService:
    def __init__(self, draft: FakeScriptDraft | None = None) -> None:
        self.draft = draft

    async def get_active_draft(self, _run_id: int) -> FakeScriptDraft | None:
        return self.draft


@dataclass
class FakeVideoArtifact:
    id: int
    path: str


class FakeRenderService:
    def __init__(
        self,
        manifest: dict[str, Any],
        artifact_id: int = 1,
        artifact_path: str = "data/artifacts/201/render/output.mp4",
    ) -> None:
        self.manifest = manifest
        self.artifact = FakeVideoArtifact(id=artifact_id, path=artifact_path)
        self.manifest_calls: list[dict[str, object]] = []
        self.create_calls: list[dict[str, object]] = []

    async def build_render_manifest(
        self,
        run_id: int,
        visual_asset_service: Any,
        audio_service: Any,
        subtitle_service: Any,
        render_profile_name: str = "shorts_default",
    ) -> dict[str, Any]:
        self.manifest_calls.append(
            {
                "run_id": run_id,
                "visual_asset_service": visual_asset_service,
                "audio_service": audio_service,
                "subtitle_service": subtitle_service,
                "render_profile_name": render_profile_name,
            }
        )
        return self.manifest

    async def create_artifact(
        self,
        run_id: int,
        path: str,
        *,
        render_profile: str | None = None,
        storage_provider: str | None = None,
        storage_key: str | None = None,
        idempotency_key: str | None = None,
    ) -> FakeVideoArtifact:
        self.create_calls.append(
            {
                "run_id": run_id,
                "path": path,
                "render_profile": render_profile,
                "storage_provider": storage_provider,
                "storage_key": storage_key,
            }
        )
        return self.artifact


class FakeFFmpegService:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[Any, Path]] = []
        self.concatenate_calls: list[tuple[list[str], str]] = []
        self.merge_calls: list[tuple[list[str], list[float], str]] = []
        self.duration_map: dict[str, float] = {}

    def render(self, render_input: Any, output_path: Path) -> Path:
        self.calls.append((render_input, output_path))
        if self.error is not None:
            raise self.error
        return output_path

    def get_audio_duration(self, path: str) -> float:
        return self.duration_map[path]

    def concatenate_audio(self, input_paths: list[str], output_path: str) -> str:
        self.concatenate_calls.append((input_paths, output_path))
        return output_path

    def merge_subtitles(
        self,
        input_paths: list[str],
        durations: list[float],
        output_path: str,
    ) -> str:
        self.merge_calls.append((input_paths, durations, output_path))
        return output_path


def _manifest(
    run_id: int,
    scenes: list[dict[str, object]] | None = None,
    audio_path: str | None = "data/artifacts/201/audio/audio.wav",
    subtitle_path: str | None = "data/artifacts/201/subtitles/subtitles.srt",
    max_duration_seconds: float = 30.0,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "scenes": scenes
        if scenes is not None
        else [
            {"scene_id": "scene-1", "asset_path": "data/artifacts/201/visual/scene-1.png"},
            {"scene_id": "scene-2", "asset_path": "data/artifacts/201/visual/scene-2.png"},
        ],
        "audio_path": audio_path,
        "subtitle_path": subtitle_path,
        "render_profile": {
            "name": "shorts_default",
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "video_codec": "libx264",
            "audio_codec": "aac",
            "transition_style": "none",
            "min_duration_seconds": 10.0,
            "max_duration_seconds": max_duration_seconds,
            "crf": 23,
            "preset": "medium",
            "burn_subtitles": True,
            "subtitle_font_size": 48,
        },
    }


def _patch_services(
    monkeypatch: pytest.MonkeyPatch,
    *,
    storage: FakeStorage,
    fake_render_service: FakeRenderService,
    fake_vas: FakeVisualAssetService,
    fake_audio: FakeAudioService,
    fake_subtitle: FakeSubtitleService,
    fake_ffmpeg: FakeFFmpegService,
    fake_visual_plan: FakeVisualPlanService | None = None,
    fake_script: FakeScriptService | None = None,
) -> None:
    monkeypatch.setattr("tasks.task_runner._run_service", SimpleNamespace(storage=storage))
    monkeypatch.setattr(render_video_module, "_render_service", fake_render_service)
    monkeypatch.setattr(render_video_module, "_visual_asset_service", fake_vas)
    monkeypatch.setattr(render_video_module, "_audio_service", fake_audio)
    monkeypatch.setattr(render_video_module, "_subtitle_service", fake_subtitle)
    monkeypatch.setattr(
        render_video_module,
        "_visual_plan_service",
        fake_visual_plan or FakeVisualPlanService(),
    )
    monkeypatch.setattr(
        render_video_module,
        "_script_service",
        fake_script or FakeScriptService(),
    )
    monkeypatch.setattr(render_video_module, "FFmpegService", lambda profile=None: fake_ffmpeg)


def _invoke_task(**kwargs: Any) -> dict[str, object]:
    task = render_video_module.render_video
    run_callable = getattr(task, "run", task)
    return run_callable(**kwargs)


def test_render_video_success(
    monkeypatch: pytest.MonkeyPatch, no_pacing_split: None
) -> None:
    run_id = 201
    storage = _make_storage(run_id=run_id, stage="RENDER_GENERATING")
    fake_render_service = FakeRenderService(manifest=_manifest(run_id=run_id), artifact_id=77)
    fake_vas = FakeVisualAssetService()
    fake_audio = FakeAudioService()
    fake_subtitle = FakeSubtitleService()
    fake_ffmpeg = FakeFFmpegService()
    _patch_services(
        monkeypatch,
        storage=storage,
        fake_render_service=fake_render_service,
        fake_vas=fake_vas,
        fake_audio=fake_audio,
        fake_subtitle=fake_subtitle,
        fake_ffmpeg=fake_ffmpeg,
    )

    result = _invoke_task(run_id=run_id, render_profile="high_quality")

    assert result["status"] == "success"
    assert result["run_id"] == run_id
    assert result["render_profile"] == "high_quality"
    assert result["video_artifact_id"] == 77
    assert result["video_path"] == "data/artifacts/201/render/output.mp4"
    assert result["scene_count"] == 2
    assert result["audio_path"] == "data/artifacts/201/audio/audio.wav"
    assert result["subtitle_path"] == "data/artifacts/201/subtitles/subtitles.srt"

    assert fake_render_service.manifest_calls[0]["render_profile_name"] == "high_quality"
    assert len(fake_render_service.create_calls) == 1
    call = fake_render_service.create_calls[0]
    assert call["run_id"] == 201
    assert call["path"] == "data/artifacts/201/render/output.mp4"
    assert call["render_profile"] == "high_quality"
    assert call["storage_provider"] == "local"
    assert "storage_key" in call

    render_input, output = fake_ffmpeg.calls[0]
    assert output == Path("data/artifacts/201/render/output.mp4")
    assert render_input.image_paths == [
        Path("data/artifacts/201/visual/scene-1.png"),
        Path("data/artifacts/201/visual/scene-2.png"),
    ]
    assert render_input.audio_path == Path("data/artifacts/201/audio/audio.wav")
    assert render_input.subtitle_path == Path("data/artifacts/201/subtitles/subtitles.srt")
    assert render_input.scene_durations == [15.0, 15.0]

    assert storage.calls == [(201, {"current_stage": "FINAL_REVIEW", "status": "paused"})]
    assert storage.cas_calls[0][2] == frozenset({"RENDER_GENERATING"})


def test_render_video_routes_final_inputs_through_render_plan_adapter(
    monkeypatch: pytest.MonkeyPatch, no_pacing_split: None
) -> None:
    run_id = 213
    storage = _make_storage(run_id=run_id, stage="RENDER_GENERATING")
    fake_render_service = FakeRenderService(
        manifest=_manifest(run_id=run_id),
        artifact_path="data/artifacts/213/render/output.mp4",
    )
    fake_ffmpeg = FakeFFmpegService()
    _patch_services(
        monkeypatch,
        storage=storage,
        fake_render_service=fake_render_service,
        fake_vas=FakeVisualAssetService(),
        fake_audio=FakeAudioService(),
        fake_subtitle=FakeSubtitleService(),
        fake_ffmpeg=fake_ffmpeg,
    )

    adapter_calls: list[dict[str, Any]] = []
    real_adapter = render_video_module.render_input_from_plan

    def spy_adapter(plan: Any, **kwargs: Any) -> Any:
        adapter_calls.append({"plan": plan, "kwargs": kwargs})
        return real_adapter(plan, **kwargs)

    monkeypatch.setattr(render_video_module, "render_input_from_plan", spy_adapter)

    result = _invoke_task(run_id=run_id)

    assert result["status"] == "success"
    assert len(adapter_calls) == 1
    plan = adapter_calls[0]["plan"]
    assert [seg.source for seg in plan.segments] == [
        "data/artifacts/201/visual/scene-1.png",
        "data/artifacts/201/visual/scene-2.png",
    ]
    assert [seg.duration_seconds for seg in plan.segments] == [15.0, 15.0]
    assert [seg.timeline_start_seconds for seg in plan.segments] == [0.0, 15.0]
    assert adapter_calls[0]["kwargs"]["audio_path"] == Path(
        "data/artifacts/201/audio/audio.wav"
    )
    render_input, _ = fake_ffmpeg.calls[0]
    assert render_input.image_paths == [
        Path("data/artifacts/201/visual/scene-1.png"),
        Path("data/artifacts/201/visual/scene-2.png"),
    ]


def test_build_render_plan_rejects_length_mismatch() -> None:
    from creator_service.render_profile import RenderProfile

    with pytest.raises(ValueError, match="must have equal length"):
        render_video_module._build_render_plan(
            [Path("a.png"), Path("b.png")],
            [1.0],
            None,
            RenderProfile.default(),
        )


def test_render_video_run_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = FakeStorage()
    fake_render_service = FakeRenderService(manifest=_manifest(run_id=999))
    fake_vas = FakeVisualAssetService()
    fake_audio = FakeAudioService()
    fake_subtitle = FakeSubtitleService()
    fake_ffmpeg = FakeFFmpegService()
    _patch_services(
        monkeypatch,
        storage=storage,
        fake_render_service=fake_render_service,
        fake_vas=fake_vas,
        fake_audio=fake_audio,
        fake_subtitle=fake_subtitle,
        fake_ffmpeg=fake_ffmpeg,
    )

    with pytest.raises(ValueError, match="Run 999 not found"):
        _invoke_task(run_id=999)

    assert fake_render_service.manifest_calls == []
    assert fake_ffmpeg.calls == []
    assert storage.calls == []


def test_render_video_wrong_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = _make_storage(run_id=202, stage="SCRIPT_REVIEW")
    fake_render_service = FakeRenderService(manifest=_manifest(run_id=202))
    fake_vas = FakeVisualAssetService()
    fake_audio = FakeAudioService()
    fake_subtitle = FakeSubtitleService()
    fake_ffmpeg = FakeFFmpegService()
    _patch_services(
        monkeypatch,
        storage=storage,
        fake_render_service=fake_render_service,
        fake_vas=fake_vas,
        fake_audio=fake_audio,
        fake_subtitle=fake_subtitle,
        fake_ffmpeg=fake_ffmpeg,
    )

    with pytest.raises(ValueError, match="expected one of"):
        _invoke_task(run_id=202)

    assert fake_render_service.manifest_calls == []
    assert fake_ffmpeg.calls == []
    assert storage.calls == []


def test_render_video_no_scenes(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = _make_storage(run_id=203, stage="RENDER_GENERATING")
    fake_render_service = FakeRenderService(
        manifest=_manifest(run_id=203, scenes=[]),
        artifact_path="data/artifacts/203/render/output.mp4",
    )
    fake_vas = FakeVisualAssetService()
    fake_audio = FakeAudioService()
    fake_subtitle = FakeSubtitleService()
    fake_ffmpeg = FakeFFmpegService()
    _patch_services(
        monkeypatch,
        storage=storage,
        fake_render_service=fake_render_service,
        fake_vas=fake_vas,
        fake_audio=fake_audio,
        fake_subtitle=fake_subtitle,
        fake_ffmpeg=fake_ffmpeg,
    )

    with pytest.raises(RuntimeError, match="No scenes found for render"):
        _invoke_task(run_id=203)

    assert fake_ffmpeg.calls == []
    assert fake_render_service.create_calls == []
    assert storage.calls == [(203, {"current_stage": "FAILED", "status": "failed"})]


def test_render_video_ffmpeg_failure_marks_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = _make_storage(run_id=204, stage="RENDER_GENERATING")
    fake_render_service = FakeRenderService(
        manifest=_manifest(run_id=204),
        artifact_path="data/artifacts/204/render/output.mp4",
    )
    fake_vas = FakeVisualAssetService()
    fake_audio = FakeAudioService()
    fake_subtitle = FakeSubtitleService()
    fake_ffmpeg = FakeFFmpegService(error=RuntimeError("ffmpeg crashed"))
    _patch_services(
        monkeypatch,
        storage=storage,
        fake_render_service=fake_render_service,
        fake_vas=fake_vas,
        fake_audio=fake_audio,
        fake_subtitle=fake_subtitle,
        fake_ffmpeg=fake_ffmpeg,
    )

    with pytest.raises(render_video_module.ProviderError, match="Provider failed video render"):
        _invoke_task(run_id=204)

    assert len(fake_ffmpeg.calls) == 1
    assert fake_render_service.create_calls == []
    assert storage.calls == [(204, {"current_stage": "FAILED", "status": "failed"})]
    assert storage.cas_calls[0][2] == frozenset({"RENDER_GENERATING"})


@pytest.mark.parametrize("expected_duration", [5.0, 5.429])
@pytest.mark.parametrize("classifier_poison", [False, True])
def test_render_video_partial_output_never_published(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_pacing_split: None,
    expected_duration: float,
    classifier_poison: bool,
) -> None:
    # Given: real same-profile 1s output for a 5s worker plan.
    fixture = tmp_path / "partial.mp4"
    if classifier_poison:
        from creator_service.media_integrity import MediaIntegrityError
        monkeypatch.setattr(MediaIntegrityError, "__str__", lambda self: "429 rate limit")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=128x128:rate=30:duration=1", "-c:v", "libx264",
                    "-threads", "1", "-preset", "ultrafast", str(fixture)],
                   check=True, capture_output=True, timeout=30)
    storage = _make_storage(run_id=204)
    manifest = _manifest(204, scenes=[{"scene_id": "scene-1", "asset_path": str(fixture)}],
                         audio_path=None, subtitle_path=None, max_duration_seconds=expected_duration)
    renders = FakeRenderService(manifest=manifest)
    _patch_services(monkeypatch, storage=storage, fake_render_service=renders,
                    fake_vas=FakeVisualAssetService(), fake_audio=FakeAudioService(),
                    fake_subtitle=FakeSubtitleService(), fake_ffmpeg=FakeFFmpegService())
    monkeypatch.setattr(render_video_module, "_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(render_video_module, "FFmpegService",
                        lambda profile=None: FFmpegService(RenderProfile(width=128, height=128)))

    def encode(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        shutil.copyfile(fixture, cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("creator_service.ffmpeg_service._run_ffmpeg", encode)
    upload = Mock()
    usage = Mock()
    monkeypatch.setattr("creator_service.artifact_storage_integration.store_artifact_file", upload)
    monkeypatch.setattr(render_video_module, "record_provider_call", usage)
    retry = Mock()
    monkeypatch.setattr(render_video_module.render_video, "retry", retry)
    # When / Then: existing ProviderError mapping and FAILED transition remain.
    with pytest.raises(render_video_module.ProviderError, match="Provider failed video render") as error:
        _invoke_task(run_id=204)
    assert type(error.value) is render_video_module.ProviderError
    retry.assert_not_called()
    upload.assert_not_called()
    usage.assert_not_called()
    assert renders.create_calls == []
    assert storage.calls == [(204, {"current_stage": "FAILED", "status": "failed"})]
    assert list((tmp_path / "204" / "render").iterdir()) == []


@pytest.mark.parametrize("validation_call", ["ffprobe", "ffmpeg"])
@pytest.mark.parametrize("error_type", [FileNotFoundError, PermissionError, OSError])
def test_render_video_validation_spawn_failure_never_records_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    no_pacing_split: None,
    validation_call: str,
    error_type: type[OSError],
) -> None:
    # Given: encoding succeeds without real media, but validation cannot spawn its tool.
    fixture = tmp_path / "encoded.mp4"
    fixture.write_bytes(b"x" * 2048)
    storage = _make_storage(run_id=204)
    manifest = _manifest(
        204,
        scenes=[{"scene_id": "scene-1", "asset_path": str(fixture)}],
        audio_path=None,
        subtitle_path=None,
        max_duration_seconds=5,
    )
    renders = FakeRenderService(manifest=manifest)
    _patch_services(
        monkeypatch,
        storage=storage,
        fake_render_service=renders,
        fake_vas=FakeVisualAssetService(),
        fake_audio=FakeAudioService(),
        fake_subtitle=FakeSubtitleService(),
        fake_ffmpeg=FakeFFmpegService(),
    )
    monkeypatch.setattr(render_video_module, "_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(
        render_video_module,
        "FFmpegService",
        lambda profile=None: FFmpegService(RenderProfile(width=128, height=128)),
    )

    def encode(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        shutil.copyfile(fixture, cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def validate(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        if cmd[0] == validation_call:
            raise error_type("validation executable unavailable")
        if cmd[0] == "ffprobe":
            document: dict[str, object] = {
                "streams": [{
                    "codec_type": "video", "codec_name": "h264", "width": 128,
                    "height": 128, "avg_frame_rate": "30/1", "r_frame_rate": "30/1",
                    "duration": 5, "nb_frames": 150,
                }],
                "format": {"duration": 5, "size": 2048},
            }
            return subprocess.CompletedProcess(cmd, 0, json.dumps(document), "")
        return subprocess.CompletedProcess(cmd, 0, "0, frame\n", "")

    monkeypatch.setattr("creator_service.ffmpeg_service._run_ffmpeg", encode)
    monkeypatch.setattr("creator_service.media_integrity.run_validation", validate)
    upload = Mock()
    usage = Mock()
    monkeypatch.setattr("creator_service.artifact_storage_integration.store_artifact_file", upload)
    monkeypatch.setattr(render_video_module, "record_provider_call", usage)

    # When
    with pytest.raises(render_video_module.ProviderError, match="Provider failed video render") as error:
        _invoke_task(run_id=204)

    # Then
    assert type(error.value.__cause__) is MediaIntegrityError
    assert error.value.__cause__.rule == "validation_unavailable"
    upload.assert_not_called()
    usage.assert_not_called()
    assert renders.create_calls == []
    assert list((tmp_path / "204" / "render").iterdir()) == []
    assert storage.calls == [(204, {"current_stage": "FAILED", "status": "failed"})]


def test_render_video_without_audio_subtitles(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = _make_storage(run_id=205, stage="RENDER_GENERATING")
    fake_render_service = FakeRenderService(
        manifest=_manifest(run_id=205, audio_path=None, subtitle_path=None),
        artifact_path="data/artifacts/205/render/output.mp4",
    )
    fake_vas = FakeVisualAssetService()
    fake_audio = FakeAudioService()
    fake_subtitle = FakeSubtitleService()
    fake_ffmpeg = FakeFFmpegService()
    _patch_services(
        monkeypatch,
        storage=storage,
        fake_render_service=fake_render_service,
        fake_vas=fake_vas,
        fake_audio=fake_audio,
        fake_subtitle=fake_subtitle,
        fake_ffmpeg=fake_ffmpeg,
    )

    result = _invoke_task(run_id=205, render_profile="fast_preview")

    assert result["status"] == "success"
    assert result["audio_path"] is None
    assert result["subtitle_path"] is None
    render_input, _ = fake_ffmpeg.calls[0]
    assert render_input.audio_path is None
    assert render_input.subtitle_path is None


class _CASSkipStorage(FakeStorage):
    async def conditional_update_run(
        self,
        run_id: int,
        updates: dict[str, object],
        expected_stages: frozenset[str],
    rejected_statuses: frozenset[str] | None = None,
    ) -> tuple[bool, dict[str, object] | None]:
        self.cas_calls.append((run_id, updates, expected_stages))
        row = self._runs.get(run_id)
        return False, dict(row) if row else None


def test_render_video_cas_skip_on_stage_change(monkeypatch: pytest.MonkeyPatch) -> None:
    run_row: dict[str, object] = {"id": 206, "current_stage": "RENDER_GENERATING"}
    storage = _CASSkipStorage(runs={206: run_row})
    fake_render_service = FakeRenderService(
        manifest=_manifest(run_id=206),
        artifact_id=90,
        artifact_path="data/artifacts/206/render/output.mp4",
    )
    fake_vas = FakeVisualAssetService()
    fake_audio = FakeAudioService()
    fake_subtitle = FakeSubtitleService()
    fake_ffmpeg = FakeFFmpegService()
    _patch_services(
        monkeypatch,
        storage=storage,
        fake_render_service=fake_render_service,
        fake_vas=fake_vas,
        fake_audio=fake_audio,
        fake_subtitle=fake_subtitle,
        fake_ffmpeg=fake_ffmpeg,
    )

    result = _invoke_task(run_id=206)

    assert result["status"] == "success"
    assert result["video_artifact_id"] == 90
    assert len(fake_ffmpeg.calls) == 1
    assert len(fake_render_service.create_calls) == 1
    assert len(storage.cas_calls) == 1
    assert storage.cas_calls[0][1] == {"current_stage": "FINAL_REVIEW", "status": "paused"}
    assert storage.calls == []


def test_render_video_default_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = _make_storage(run_id=207, stage="RENDER_GENERATING")
    fake_render_service = FakeRenderService(
        manifest=_manifest(run_id=207),
        artifact_path="data/artifacts/207/render/output.mp4",
    )
    fake_vas = FakeVisualAssetService()
    fake_audio = FakeAudioService()
    fake_subtitle = FakeSubtitleService()
    fake_ffmpeg = FakeFFmpegService()
    _patch_services(
        monkeypatch,
        storage=storage,
        fake_render_service=fake_render_service,
        fake_vas=fake_vas,
        fake_audio=fake_audio,
        fake_subtitle=fake_subtitle,
        fake_ffmpeg=fake_ffmpeg,
    )

    result = _invoke_task(run_id=207)

    assert result["status"] == "success"
    assert result["render_profile"] == "shorts_default"
    assert fake_render_service.manifest_calls[0]["render_profile_name"] == "shorts_default"
    assert fake_render_service.create_calls[0]["render_profile"] == "shorts_default"


def test_render_video_profile_propagated_to_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify non-default render_profile is actually passed to FFmpegService."""
    run_id = 208
    storage = _make_storage(run_id=run_id, stage="RENDER_GENERATING")
    fake_render_service = FakeRenderService(
        manifest=_manifest(run_id=run_id),
        artifact_path="data/artifacts/208/render/output.mp4",
    )
    fake_vas = FakeVisualAssetService()
    fake_audio = FakeAudioService()
    fake_subtitle = FakeSubtitleService()
    fake_ffmpeg = FakeFFmpegService()

    # Capture the profile argument passed to FFmpegService constructor
    captured_profiles: list[Any] = []

    def ffmpeg_factory(profile: Any = None) -> FakeFFmpegService:
        captured_profiles.append(profile)
        return fake_ffmpeg

    monkeypatch.setattr("tasks.task_runner._run_service", SimpleNamespace(storage=storage))
    monkeypatch.setattr(render_video_module, "_render_service", fake_render_service)
    monkeypatch.setattr(render_video_module, "_visual_asset_service", fake_vas)
    monkeypatch.setattr(render_video_module, "_audio_service", fake_audio)
    monkeypatch.setattr(render_video_module, "_subtitle_service", fake_subtitle)
    monkeypatch.setattr(render_video_module, "FFmpegService", ffmpeg_factory)

    result = _invoke_task(run_id=run_id, render_profile="high_quality")

    assert result["status"] == "success"
    assert result["render_profile"] == "high_quality"
    assert len(captured_profiles) == 1
    profile = captured_profiles[0]
    assert profile.name == "high_quality"
    assert profile.crf == 18
    assert profile.preset == "slow"


def test_render_video_reorders_scenes_from_active_visual_plan(
    monkeypatch: pytest.MonkeyPatch, no_pacing_split: None
) -> None:
    run_id = 209
    storage = _make_storage(run_id=run_id, stage="RENDER_GENERATING")
    fake_render_service = FakeRenderService(
        manifest=_manifest(
            run_id=run_id,
            scenes=[
                {"scene_id": "scene-10", "asset_path": "data/artifacts/209/visual/scene-10.png"},
                {"scene_id": "scene-2", "asset_path": "data/artifacts/209/visual/scene-2.png"},
            ],
        ),
        artifact_path="data/artifacts/209/render/output.mp4",
    )
    fake_vas = FakeVisualAssetService()
    fake_audio = FakeAudioService()
    fake_subtitle = FakeSubtitleService()
    fake_ffmpeg = FakeFFmpegService()
    fake_visual_plan = FakeVisualPlanService(
        FakeVisualPlan(
            scenes=[
                FakePlanScene(scene_id="scene-2", section_id="sec-2"),
                FakePlanScene(scene_id="scene-10", section_id="sec-10"),
            ]
        )
    )
    _patch_services(
        monkeypatch,
        storage=storage,
        fake_render_service=fake_render_service,
        fake_vas=fake_vas,
        fake_audio=fake_audio,
        fake_subtitle=fake_subtitle,
        fake_ffmpeg=fake_ffmpeg,
        fake_visual_plan=fake_visual_plan,
    )

    result = _invoke_task(run_id=run_id)

    assert result["status"] == "success"
    render_input, _ = fake_ffmpeg.calls[0]
    assert render_input.image_paths == [
        Path("data/artifacts/209/visual/scene-2.png"),
        Path("data/artifacts/209/visual/scene-10.png"),
    ]


def test_render_video_handles_sparse_paragraph_artifacts(
    monkeypatch: pytest.MonkeyPatch, no_pacing_split: None
) -> None:
    run_id = 210
    storage = _make_storage(run_id=run_id, stage="RENDER_GENERATING")
    fake_render_service = FakeRenderService(
        manifest=_manifest(
            run_id=run_id,
            scenes=[
                {"scene_id": "scene-1", "asset_path": "data/artifacts/210/visual/scene-1.png"},
                {"scene_id": "scene-2", "asset_path": "data/artifacts/210/visual/scene-2.png"},
            ],
        ),
        artifact_path="data/artifacts/210/render/output.mp4",
    )
    fake_vas = FakeVisualAssetService()
    fake_audio = FakeAudioService(
        paragraph_artifacts=[
            FakeAudioArtifact(path="data/artifacts/210/audio/sec-2.wav", section_id="sec-2")
        ]
    )
    fake_subtitle = FakeSubtitleService(
        paragraph_artifacts=[
            FakeSubtitleArtifact(path="data/artifacts/210/subtitles/sec-2.srt", section_id="sec-2")
        ]
    )
    fake_ffmpeg = FakeFFmpegService()
    fake_ffmpeg.duration_map["data/artifacts/210/audio/sec-2.wav"] = 7.5
    fake_visual_plan = FakeVisualPlanService(
        FakeVisualPlan(
            scenes=[
                FakePlanScene(scene_id="scene-1", section_id="sec-1"),
                FakePlanScene(scene_id="scene-2", section_id="sec-2"),
            ]
        )
    )
    fake_script = FakeScriptService(
        FakeScriptDraft(
            structured_script=[
                FakeScriptSection(section_id="sec-1"),
                FakeScriptSection(section_id="sec-2"),
            ]
        )
    )
    _patch_services(
        monkeypatch,
        storage=storage,
        fake_render_service=fake_render_service,
        fake_vas=fake_vas,
        fake_audio=fake_audio,
        fake_subtitle=fake_subtitle,
        fake_ffmpeg=fake_ffmpeg,
        fake_visual_plan=fake_visual_plan,
        fake_script=fake_script,
    )

    result = _invoke_task(run_id=run_id)

    assert result["status"] == "success"
    render_input, _ = fake_ffmpeg.calls[0]
    assert render_input.scene_durations == [15.0, 15.0]
    assert fake_ffmpeg.concatenate_calls == []
    assert fake_ffmpeg.merge_calls == []


def test_render_video_rejects_non_dict_manifest_render_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = 211
    storage = _make_storage(run_id=run_id, stage="RENDER_GENERATING")
    manifest = _manifest(run_id=run_id)
    manifest["render_profile"] = "shorts_default"
    fake_render_service = FakeRenderService(
        manifest=manifest,
        artifact_path="data/artifacts/211/render/output.mp4",
    )
    fake_vas = FakeVisualAssetService()
    fake_audio = FakeAudioService()
    fake_subtitle = FakeSubtitleService()
    fake_ffmpeg = FakeFFmpegService()
    _patch_services(
        monkeypatch,
        storage=storage,
        fake_render_service=fake_render_service,
        fake_vas=fake_vas,
        fake_audio=fake_audio,
        fake_subtitle=fake_subtitle,
        fake_ffmpeg=fake_ffmpeg,
    )

    with pytest.raises(RuntimeError, match="Invalid render_profile"):
        _invoke_task(run_id=run_id)


def test_render_video_rejects_unknown_manifest_render_profile_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = 212
    storage = _make_storage(run_id=run_id, stage="RENDER_GENERATING")
    manifest = _manifest(run_id=run_id)
    profile = dict(manifest["render_profile"])
    profile["unexpected_key"] = True
    manifest["render_profile"] = profile
    fake_render_service = FakeRenderService(
        manifest=manifest,
        artifact_path="data/artifacts/212/render/output.mp4",
    )
    fake_vas = FakeVisualAssetService()
    fake_audio = FakeAudioService()
    fake_subtitle = FakeSubtitleService()
    fake_ffmpeg = FakeFFmpegService()
    _patch_services(
        monkeypatch,
        storage=storage,
        fake_render_service=fake_render_service,
        fake_vas=fake_vas,
        fake_audio=fake_audio,
        fake_subtitle=fake_subtitle,
        fake_ffmpeg=fake_ffmpeg,
    )

    with pytest.raises(RuntimeError, match="Invalid render_profile"):
        _invoke_task(run_id=run_id)


_MIB = 1024 * 1024


def _volumes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, artifact_free_mib: int,
) -> Path:
    """Point the artifact root and scratch directory at tmp dirs with a controllable amount of free space."""
    artifacts = tmp_path.resolve() / "artifacts"
    scratch = tmp_path.resolve() / "scratch"
    artifacts.mkdir()
    scratch.mkdir()
    free = {artifacts: artifact_free_mib * _MIB, scratch: 9000 * _MIB}
    usage = namedtuple("usage", "total used free")
    monkeypatch.setenv("ARTIFACT_ROOT", str(artifacts))
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("MEDIA_ARTIFACT_DISK_LOW_WATERMARK_BYTES", str(500 * _MIB))
    monkeypatch.setenv("MEDIA_TEMP_DISK_LOW_WATERMARK_BYTES", str(500 * _MIB))

    def disk_usage(path: str) -> object:
        return usage(total=1 << 40, used=0, free=free[Path(path)])

    monkeypatch.setattr(job_capacity.shutil, "disk_usage", disk_usage)
    return artifacts


def test_render_video_is_refused_before_any_render_work_when_the_artifact_filesystem_is_low(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_pacing_split: None,
) -> None:
    # Given a render whose artifact filesystem is under its configured floor.
    run_id = 214
    storage = _make_storage(run_id=run_id, stage="RENDER_GENERATING")
    fake_render_service = FakeRenderService(manifest=_manifest(run_id=run_id))
    fake_ffmpeg = FakeFFmpegService()
    _patch_services(
        monkeypatch, storage=storage, fake_render_service=fake_render_service,
        fake_vas=FakeVisualAssetService(), fake_audio=FakeAudioService(),
        fake_subtitle=FakeSubtitleService(), fake_ffmpeg=fake_ffmpeg,
    )
    artifacts = _volumes(monkeypatch, tmp_path, artifact_free_mib=100)

    # When the real render task is delivered.
    with pytest.raises(FilesystemLowSpace) as raised:
        _invoke_task(run_id=run_id)

    # Then it is refused for the artifact filesystem before a manifest, FFmpeg run, artifact row or file exists.
    assert raised.value.filesystem == "artifact"
    assert fake_ffmpeg.calls == []
    assert fake_render_service.manifest_calls == []
    assert fake_render_service.create_calls == []
    assert list(artifacts.rglob("*")) == []
    assert storage.calls == [(run_id, {"current_stage": "FAILED", "status": "failed"})]


def test_render_video_runs_normally_when_the_artifact_filesystem_has_room(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_pacing_split: None,
) -> None:
    # Given the same render with the artifact filesystem above its floor.
    run_id = 215
    storage = _make_storage(run_id=run_id, stage="RENDER_GENERATING")
    fake_render_service = FakeRenderService(manifest=_manifest(run_id=run_id))
    fake_ffmpeg = FakeFFmpegService()
    _patch_services(
        monkeypatch, storage=storage, fake_render_service=fake_render_service,
        fake_vas=FakeVisualAssetService(), fake_audio=FakeAudioService(),
        fake_subtitle=FakeSubtitleService(), fake_ffmpeg=fake_ffmpeg,
    )
    _volumes(monkeypatch, tmp_path, artifact_free_mib=9000)

    # When the real render task is delivered.
    result = _invoke_task(run_id=run_id)

    # Then the admission check is invisible: the render ran and the run advanced.
    assert result["status"] == "success"
    assert len(fake_ffmpeg.calls) == 1
    assert storage.calls == [(run_id, {"current_stage": "FINAL_REVIEW", "status": "paused"})]
