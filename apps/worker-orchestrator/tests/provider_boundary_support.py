"""Real generation tasks and stores; only external provider output is substituted."""

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from celery import Task
from creator_domain.models.script_draft import ScriptSection
from creator_domain.models.visual_plan import VisualScene
from creator_service.audio_service import AudioService
from creator_service.script_service import ScriptService
from creator_service.subtitle_service import SubtitleService
from creator_service.visual_asset_service import VisualAssetService
from creator_service.visual_plan_service import VisualPlanService
from worker_loop import run_in_worker_loop

from .terminal_postgres_support import terminal_pool as terminal_pool
from .terminal_validation_support import RunnerCase, runner_case as runner_case


@dataclass(frozen=True, slots=True)
class GenerationCase:
    runner: RunnerCase
    module: ModuleType
    task: Task
    args: tuple[int | str, ...]
    stage: str
    provider: AsyncMock
    scripts: ScriptService
    usage: AsyncMock


@pytest.fixture(params=[
    ("generate_script", "SCRIPT_GENERATING", ("An offline idea",)),
    ("generate_visual_plan", "VISUAL_PLAN_GENERATING", ()),
    ("generate_audio", "AUDIO_GENERATING", ()),
    ("generate_audio", "AUDIO_GENERATING", ("edge-tts",)),
    ("generate_paragraph_audio", "AUDIO_GENERATING", ("sec-1",)),
    ("generate_subtitles", "SUBTITLE_GENERATING", ()),
    ("generate_paragraph_subtitles", "SUBTITLE_GENERATING", ("sec-1",)),
    ("generate_scene_image", "VISUAL_ASSET_GENERATING", (None, "groq-svg")),
], ids=["script", "visual", "audio", "sections", "paragraph-audio", "subtitles", "paragraph-subtitles", "scene"])
def generation_case(request: pytest.FixtureRequest, runner_case: RunnerCase, monkeypatch: pytest.MonkeyPatch) -> GenerationCase:
    name, stage, extra_args = request.param
    root = request.getfixturevalue("tmp_path")
    module = import_module(f"tasks.{name}")
    provider = AsyncMock()
    provider.generate.return_value = "An offline generated script."
    entry = SimpleNamespace(
        requires_gpu=False, provider_type="edge_tts" if extra_args == ("edge-tts",) else "offline",
        default_params={}, endpoint="offline",
    )
    if extra_args == ("edge-tts",):
        async def generate_section(_text: str, *, voice: str, params: dict[str, str]) -> None:
            assert voice == "default"
            Path(params["output_path"]).write_bytes(b"synthetic section audio")

        provider.generate.side_effect = generate_section
        monkeypatch.setattr(
            "creator_service.ffmpeg_service.FFmpegService.concatenate_audio",
            lambda _self, _paths, output: Path(output).write_bytes(b"synthetic full audio"),
        )
    registry = SimpleNamespace(resolve=Mock(return_value=entry), get_provider=Mock(return_value=provider))
    scripts, audio, plans = ScriptService(), AudioService(), VisualPlanService()
    run_id = runner_case.run_id
    run_in_worker_loop(runner_case.runs.storage.update_run(run_id, {"current_stage": stage}))
    run_in_worker_loop(scripts.save_draft(
        run_id, "edited_manually", markdown_content="Opening. Ending.", structured_script=[
            ScriptSection(section_id="sec-1", type="hook", text="Opening."),
            ScriptSection(section_id="sec-2", type="conclusion", text="Ending."),
        ],
    ))
    path = Path(root) / "input.wav"
    path.write_bytes(b"offline audio input")
    run_in_worker_loop(audio.create_artifact(run_id, str(path)))
    run_in_worker_loop(audio.create_paragraph_artifact(run_id, "sec-1", str(path)))
    run_in_worker_loop(plans.save_plan(run_id, [
        VisualScene(scene_id=f"scene-{i}", section_id=f"sec-{i}", scene_index=i,
                    section_type="body", original_text="Text", prompt="Offline image")
        for i in (1, 2)
    ]))
    services = {
        "_script_service": scripts, "_audio_service": audio,
        "_visual_plan_service": plans, "_visual_asset_service": VisualAssetService(),
        "_subtitle_service": SubtitleService(), "_run_service": runner_case.runs,
    }
    for attr, service in services.items():
        if hasattr(module, attr):
            monkeypatch.setattr(module, attr, service)
    for attr in ("_ARTIFACT_ROOT", "_ARTIFACTS_BASE"):
        if hasattr(module, attr):
            monkeypatch.setattr(module, attr, str(root))
    usage = AsyncMock()
    monkeypatch.setattr(module, "get_default_registry", lambda: registry)
    monkeypatch.setattr(module, "record_provider_call", usage)
    return GenerationCase(runner_case, module, getattr(module, name), (run_id, *extra_args), stage, provider, scripts, usage)
