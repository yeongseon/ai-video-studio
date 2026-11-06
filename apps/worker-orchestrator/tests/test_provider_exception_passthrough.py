"""Test that ProviderTimeoutError and RateLimitError are re-raised unchanged for all task modules.

Each task module must preserve these provider exceptions without mapping them to
a generic ProviderError, allowing Celery's retry mechanism to handle them.
"""

# pyright: reportCallIssue=false, reportMissingImports=false

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
import asyncio
from typing import Any

import pytest
from creator_provider.exceptions import ProviderTimeoutError, RateLimitError
from tasks import (
    generate_audio as generate_audio_module,
    generate_paragraph_audio as generate_paragraph_audio_module,
    generate_script as generate_script_module,
    generate_visual_plan as generate_visual_plan_module,
    generate_scene_image as generate_scene_image_module,
    generate_subtitles as generate_subtitles_module,
    generate_paragraph_subtitles as generate_paragraph_subtitles_module,
)


async def _async_value(val: Any) -> Any:
    return val


# ============================================================================
# Shared Test Infrastructure
# ============================================================================


@dataclass
class _FakeEntry:
    provider_type: str = "test"
    endpoint: str = "http://test:1234"
    requires_gpu: bool = False
    default_params: dict[str, object] | None = None


class _FakeProvider:
    """Generic fake provider for any task."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[Any] = []

    async def generate(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error

    async def transcribe(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error


class _FakeRegistry:
    def __init__(self, entry: _FakeEntry, provider: _FakeProvider) -> None:
        self.entry = entry
        self.provider = provider

    def resolve(self, _model_key: str) -> _FakeEntry:
        return self.entry

    def get_provider(self, _model_key: str) -> _FakeProvider:
        return self.provider


class _FakeStorage:
    """Minimal storage that allows tasks to proceed."""

    def __init__(
        self,
        run_id: int = 1,
        stage: str = "VISUAL_ASSET_REVIEW",
        extra: dict[str, object] | None = None,
    ) -> None:
        self._run_id = run_id
        self._stage = stage
        self._extra = extra or {}

    async def get_run(self, run_id: int) -> dict[str, object] | None:
        if run_id == self._run_id:
            return {"id": run_id, "current_stage": self._stage, **self._extra}
        return None

    async def conditional_update_run(
        self,
        run_id: int,
        updates: dict[str, object],
        expected_stages: frozenset[str],
    rejected_statuses: frozenset[str] | None = None,
    ) -> tuple[bool, dict[str, object] | None]:
        return False, None

    async def update_run(self, run_id: int, updates: dict[str, object]) -> dict[str, object]:
        return {"id": run_id, **updates}


def _make_storage(
    run_id: int = 1, stage: str = "VISUAL_ASSET_REVIEW", extra: dict[str, object] | None = None
) -> _FakeStorage:
    return _FakeStorage(run_id, stage, extra)


def _patch_registry(monkeypatch: pytest.MonkeyPatch, module: Any, registry: _FakeRegistry) -> None:
    """Patch get_default_registry for a module."""
    monkeypatch.setattr(module, "get_default_registry", lambda: registry)


# ============================================================================
# Tests for generate_audio
# ============================================================================


class _FakeAudioService:
    def __init__(self, artifact_id: int = 1) -> None:
        self.artifact_id = artifact_id
        self.calls: list[dict[str, object]] = []

    async def create_artifact(
        self,
        run_id: int,
        path: str,
        *,
        model_used: str | None = None,
        provider_type: str | None = None,
        voice: str | None = None,
    ) -> Any:
        self.calls.append(
            {
                "run_id": run_id,
                "path": path,
                "model_used": model_used,
                "provider_type": provider_type,
                "voice": voice,
            }
        )
        return SimpleNamespace(id=self.artifact_id, path=path)


class _FakeScriptService:
    def __init__(self, draft: Any = None) -> None:
        self.draft = draft

    async def get_active_draft(self, _run_id: int) -> Any:
        return self.draft


class _FakeScriptDraft:
    def __init__(self, markdown_content: str = "", structured_script: Any = None) -> None:
        self.markdown_content = markdown_content
        self.structured_script = structured_script


def test_generate_audio_preserves_provider_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(generate_audio_module, "_ARTIFACT_ROOT", str(tmp_path))
    provider = _FakeProvider(error=ProviderTimeoutError("timeout"))
    _patch_registry(monkeypatch, generate_audio_module, _FakeRegistry(_FakeEntry(), provider))

    script_service = _FakeScriptService(draft=_FakeScriptDraft(markdown_content="Test"))
    audio_service = _FakeAudioService(artifact_id=1)
    storage = _make_storage(run_id=1, stage="AUDIO_GENERATING")

    monkeypatch.setattr(generate_audio_module, "_script_service", script_service)
    monkeypatch.setattr(generate_audio_module, "_audio_service", audio_service)
    monkeypatch.setattr("tasks.task_runner._run_service", SimpleNamespace(storage=storage))

    task = generate_audio_module.generate_audio
    run_callable = getattr(task, "run", task)

    with pytest.raises(ProviderTimeoutError, match="timeout"):
        run_callable(run_id=1)


def test_generate_audio_preserves_rate_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(generate_audio_module, "_ARTIFACT_ROOT", str(tmp_path))
    provider = _FakeProvider(error=RateLimitError("rate limited"))
    _patch_registry(monkeypatch, generate_audio_module, _FakeRegistry(_FakeEntry(), provider))

    script_service = _FakeScriptService(draft=_FakeScriptDraft(markdown_content="Test"))
    audio_service = _FakeAudioService(artifact_id=1)
    storage = _make_storage(run_id=1, stage="AUDIO_GENERATING")

    monkeypatch.setattr(generate_audio_module, "_script_service", script_service)
    monkeypatch.setattr(generate_audio_module, "_audio_service", audio_service)
    monkeypatch.setattr("tasks.task_runner._run_service", SimpleNamespace(storage=storage))

    task = generate_audio_module.generate_audio
    run_callable = getattr(task, "run", task)

    with pytest.raises(RateLimitError, match="rate limited"):
        run_callable(run_id=1)


# ============================================================================
# Tests for generate_paragraph_audio
# ============================================================================


class _FakeAudioServiceForParagraph:
    def __init__(self, artifact_id: int = 1) -> None:
        self.artifact_id = artifact_id
        self.calls: list[dict[str, object]] = []

    async def create_paragraph_artifact(
        self,
        run_id: int,
        section_id: str,
        path: str,
        *,
        model_used: str | None = None,
        provider_type: str | None = None,
        voice: str | None = None,
    ) -> Any:
        self.calls.append(
            {
                "run_id": run_id,
                "section_id": section_id,
                "path": path,
                "model_used": model_used,
                "provider_type": provider_type,
                "voice": voice,
            }
        )
        return SimpleNamespace(id=self.artifact_id, path=path)


def test_generate_paragraph_audio_preserves_provider_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(generate_paragraph_audio_module, "_ARTIFACT_ROOT", str(tmp_path))
    provider = _FakeProvider(error=ProviderTimeoutError("timeout"))
    _patch_registry(
        monkeypatch, generate_paragraph_audio_module, _FakeRegistry(_FakeEntry(), provider)
    )

    audio_service = _FakeAudioServiceForParagraph(artifact_id=1)
    script_service = _FakeScriptService(
        draft=_FakeScriptDraft(
            structured_script=[SimpleNamespace(section_id="s1", text="Test", display_text=None)]
        )
    )
    storage = _make_storage(run_id=1, stage="AUDIO_GENERATING")

    monkeypatch.setattr(generate_paragraph_audio_module, "_script_service", script_service)
    monkeypatch.setattr(generate_paragraph_audio_module, "_audio_service", audio_service)
    monkeypatch.setattr("tasks.task_runner._run_service", SimpleNamespace(storage=storage))

    task = generate_paragraph_audio_module.generate_paragraph_audio
    run_callable = getattr(task, "run", task)

    with pytest.raises(ProviderTimeoutError, match="timeout"):
        run_callable(run_id=1, section_id="s1")


def test_generate_paragraph_audio_preserves_rate_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(generate_paragraph_audio_module, "_ARTIFACT_ROOT", str(tmp_path))
    provider = _FakeProvider(error=RateLimitError("rate limited"))
    _patch_registry(
        monkeypatch, generate_paragraph_audio_module, _FakeRegistry(_FakeEntry(), provider)
    )

    audio_service = _FakeAudioServiceForParagraph(artifact_id=1)
    script_service = _FakeScriptService(
        draft=_FakeScriptDraft(
            structured_script=[SimpleNamespace(section_id="s1", text="Test", display_text=None)]
        )
    )
    storage = _make_storage(run_id=1, stage="AUDIO_GENERATING")

    monkeypatch.setattr(generate_paragraph_audio_module, "_script_service", script_service)
    monkeypatch.setattr(generate_paragraph_audio_module, "_audio_service", audio_service)
    monkeypatch.setattr("tasks.task_runner._run_service", SimpleNamespace(storage=storage))

    task = generate_paragraph_audio_module.generate_paragraph_audio
    run_callable = getattr(task, "run", task)

    with pytest.raises(RateLimitError, match="rate limited"):
        run_callable(run_id=1, section_id="s1")


# ============================================================================
# Tests for generate_script
# ============================================================================


def test_generate_script_preserves_provider_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _FakeProvider(error=ProviderTimeoutError("timeout"))
    _patch_registry(monkeypatch, generate_script_module, _FakeRegistry(_FakeEntry(), provider))

    storage = _make_storage(run_id=1, stage="SCRIPT_GENERATING")
    monkeypatch.setattr("tasks.task_runner._run_service", SimpleNamespace(storage=storage))
    monkeypatch.setattr(generate_script_module, "_script_service", _FakeScriptService())

    task = generate_script_module.generate_script
    run_callable = getattr(task, "run", task)

    with pytest.raises(ProviderTimeoutError, match="timeout"):
        run_callable(run_id=1, idea_brief="Test idea")


def test_generate_script_preserves_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _FakeProvider(error=RateLimitError("rate limited"))
    _patch_registry(monkeypatch, generate_script_module, _FakeRegistry(_FakeEntry(), provider))

    storage = _make_storage(run_id=1, stage="SCRIPT_GENERATING")
    monkeypatch.setattr("tasks.task_runner._run_service", SimpleNamespace(storage=storage))
    monkeypatch.setattr(generate_script_module, "_script_service", _FakeScriptService())

    task = generate_script_module.generate_script
    run_callable = getattr(task, "run", task)

    with pytest.raises(RateLimitError, match="rate limited"):
        run_callable(run_id=1, idea_brief="Test idea")


# ============================================================================
# Tests for generate_visual_plan
# ============================================================================


class _FakeVisualPlanService:
    async def save_plan(self, **kwargs: Any) -> None:
        pass

    async def get_active_plan(self, _run_id: int) -> Any:
        return None


def test_generate_visual_plan_preserves_provider_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _FakeProvider(error=ProviderTimeoutError("timeout"))
    _patch_registry(monkeypatch, generate_visual_plan_module, _FakeRegistry(_FakeEntry(), provider))

    draft = _FakeScriptDraft(markdown_content="Test script content")
    storage = _make_storage(run_id=1, stage="VISUAL_PLAN_GENERATING")
    monkeypatch.setattr("tasks.task_runner._run_service", SimpleNamespace(storage=storage))
    monkeypatch.setattr(
        generate_visual_plan_module, "_script_service", _FakeScriptService(draft=draft)
    )
    monkeypatch.setattr(
        generate_visual_plan_module, "_visual_plan_service", _FakeVisualPlanService()
    )

    task = generate_visual_plan_module.generate_visual_plan
    run_callable = getattr(task, "run", task)

    with pytest.raises(ProviderTimeoutError, match="timeout"):
        run_callable(run_id=1)


def test_generate_visual_plan_preserves_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _FakeProvider(error=RateLimitError("rate limited"))
    _patch_registry(monkeypatch, generate_visual_plan_module, _FakeRegistry(_FakeEntry(), provider))

    draft = _FakeScriptDraft(markdown_content="Test script content")
    storage = _make_storage(run_id=1, stage="VISUAL_PLAN_GENERATING")
    monkeypatch.setattr("tasks.task_runner._run_service", SimpleNamespace(storage=storage))
    monkeypatch.setattr(
        generate_visual_plan_module, "_script_service", _FakeScriptService(draft=draft)
    )
    monkeypatch.setattr(
        generate_visual_plan_module, "_visual_plan_service", _FakeVisualPlanService()
    )

    task = generate_visual_plan_module.generate_visual_plan
    run_callable = getattr(task, "run", task)

    with pytest.raises(RateLimitError, match="rate limited"):
        run_callable(run_id=1)


# ============================================================================
# Tests for generate_scene_image
# ============================================================================


class _FakeScene:
    def __init__(self, scene_id: str = "scene-1", prompt: str = "test prompt") -> None:
        self.scene_id = scene_id
        self.prompt = prompt


class _FakeVisualPlan:
    def __init__(self) -> None:
        self.scenes = [_FakeScene()]


class _FakeVisualPlanServiceWithPlan:
    def __init__(self, plan: Any = None) -> None:
        self._plan = plan or _FakeVisualPlan()

    async def get_active_plan(self, _run_id: int) -> Any:
        return self._plan

    async def save_plan(self, **kwargs: Any) -> None:
        pass


def test_generate_scene_image_propagates_provider_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """generate_scene_image re-raises ProviderTimeoutError for Celery retry."""
    provider = _FakeProvider(error=ProviderTimeoutError("timeout"))
    _patch_registry(monkeypatch, generate_scene_image_module, _FakeRegistry(_FakeEntry(), provider))

    storage = _make_storage(run_id=1, stage="VISUAL_ASSET_GENERATING")
    monkeypatch.setattr("tasks.task_runner._run_service", SimpleNamespace(storage=storage))
    monkeypatch.setattr(
        generate_scene_image_module, "_run_service", SimpleNamespace(storage=storage)
    )
    monkeypatch.setattr(
        generate_scene_image_module, "_visual_plan_service", _FakeVisualPlanServiceWithPlan()
    )

    task = generate_scene_image_module.generate_scene_image
    run_callable = getattr(task, "run", task)

    with pytest.raises(ProviderTimeoutError, match="timeout"):
        run_callable(run_id=1, scene_id="scene-1")


def test_generate_scene_image_propagates_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """generate_scene_image re-raises RateLimitError for Celery retry."""
    provider = _FakeProvider(error=RateLimitError("rate limited"))
    _patch_registry(monkeypatch, generate_scene_image_module, _FakeRegistry(_FakeEntry(), provider))

    storage = _make_storage(run_id=1, stage="VISUAL_ASSET_GENERATING")
    monkeypatch.setattr("tasks.task_runner._run_service", SimpleNamespace(storage=storage))
    monkeypatch.setattr(
        generate_scene_image_module, "_run_service", SimpleNamespace(storage=storage)
    )
    monkeypatch.setattr(
        generate_scene_image_module, "_visual_plan_service", _FakeVisualPlanServiceWithPlan()
    )

    task = generate_scene_image_module.generate_scene_image
    run_callable = getattr(task, "run", task)

    with pytest.raises(RateLimitError, match="rate limited"):
        run_callable(run_id=1, scene_id="scene-1")


# ============================================================================
# Tests for generate_subtitles
# ============================================================================


class _FakeAudioArtifact:
    def __init__(self, path: str = "/tmp/test_audio.wav") -> None:
        self.path = path


class _FakeAudioServiceForSubtitles:
    def __init__(self, artifact: Any = None) -> None:
        self._artifact = artifact or _FakeAudioArtifact()

    async def get_latest(self, _run_id: int) -> Any:
        return self._artifact


def test_generate_subtitles_preserves_provider_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _FakeProvider(error=ProviderTimeoutError("timeout"))
    _patch_registry(monkeypatch, generate_subtitles_module, _FakeRegistry(_FakeEntry(), provider))

    draft = _FakeScriptDraft(markdown_content="Test script")
    storage = _make_storage(run_id=1, stage="SUBTITLE_GENERATING")
    monkeypatch.setattr("tasks.task_runner._run_service", SimpleNamespace(storage=storage))
    monkeypatch.setattr(
        generate_subtitles_module, "_script_service", _FakeScriptService(draft=draft)
    )
    monkeypatch.setattr(
        generate_subtitles_module, "_audio_service", _FakeAudioServiceForSubtitles()
    )
    monkeypatch.setattr(generate_subtitles_module, "validate_artifact_path", lambda p, r: p)

    task = generate_subtitles_module.generate_subtitles
    run_callable = getattr(task, "run", task)

    with pytest.raises(ProviderTimeoutError, match="timeout"):
        run_callable(run_id=1)


def test_generate_subtitles_preserves_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _FakeProvider(error=RateLimitError("rate limited"))
    _patch_registry(monkeypatch, generate_subtitles_module, _FakeRegistry(_FakeEntry(), provider))

    draft = _FakeScriptDraft(markdown_content="Test script")
    storage = _make_storage(run_id=1, stage="SUBTITLE_GENERATING")
    monkeypatch.setattr("tasks.task_runner._run_service", SimpleNamespace(storage=storage))
    monkeypatch.setattr(
        generate_subtitles_module, "_script_service", _FakeScriptService(draft=draft)
    )
    monkeypatch.setattr(
        generate_subtitles_module, "_audio_service", _FakeAudioServiceForSubtitles()
    )
    monkeypatch.setattr(generate_subtitles_module, "validate_artifact_path", lambda p, r: p)

    task = generate_subtitles_module.generate_subtitles
    run_callable = getattr(task, "run", task)

    with pytest.raises(RateLimitError, match="rate limited"):
        run_callable(run_id=1)


# ============================================================================
# Tests for generate_paragraph_subtitles
# ============================================================================


class _FakeSubtitleService:
    async def create_paragraph_artifact(self, **kwargs: Any) -> Any:
        return SimpleNamespace(id=1, path="/tmp/test.srt")

    async def create_artifact(self, **kwargs: Any) -> Any:
        return SimpleNamespace(id=1, path="/tmp/test.srt")


def test_generate_paragraph_subtitles_preserves_provider_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    provider = _FakeProvider(error=ProviderTimeoutError("timeout"))
    _patch_registry(
        monkeypatch, generate_paragraph_subtitles_module, _FakeRegistry(_FakeEntry(), provider)
    )

    monkeypatch.setattr(
        generate_paragraph_subtitles_module,
        "_audio_service",
        SimpleNamespace(
            get_paragraph_audio=lambda *a, **kw: _async_value(
                SimpleNamespace(path="test_audio.wav")
            )
        ),
    )
    monkeypatch.setattr(
        generate_paragraph_subtitles_module, "validate_artifact_path", lambda p, r: p
    )
    monkeypatch.setattr(
        generate_paragraph_subtitles_module,
        "os",
        SimpleNamespace(path=SimpleNamespace(exists=lambda _: True)),
    )

    storage = _make_storage(run_id=1, stage="SUBTITLE_GENERATING")
    monkeypatch.setattr("tasks.task_runner._run_service", SimpleNamespace(storage=storage))
    monkeypatch.setattr(
        generate_paragraph_subtitles_module, "_subtitle_service", _FakeSubtitleService()
    )

    task = generate_paragraph_subtitles_module.generate_paragraph_subtitles
    run_callable = getattr(task, "run", task)

    with pytest.raises(ProviderTimeoutError, match="timeout"):
        run_callable(run_id=1, section_id="s1")


def test_generate_paragraph_subtitles_preserves_rate_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    provider = _FakeProvider(error=RateLimitError("rate limited"))
    _patch_registry(
        monkeypatch, generate_paragraph_subtitles_module, _FakeRegistry(_FakeEntry(), provider)
    )

    monkeypatch.setattr(
        generate_paragraph_subtitles_module,
        "_audio_service",
        SimpleNamespace(
            get_paragraph_audio=lambda *a, **kw: _async_value(
                SimpleNamespace(path="test_audio.wav")
            )
        ),
    )
    monkeypatch.setattr(
        generate_paragraph_subtitles_module, "validate_artifact_path", lambda p, r: p
    )
    monkeypatch.setattr(
        generate_paragraph_subtitles_module,
        "os",
        SimpleNamespace(path=SimpleNamespace(exists=lambda _: True)),
    )

    storage = _make_storage(run_id=1, stage="SUBTITLE_GENERATING")
    monkeypatch.setattr("tasks.task_runner._run_service", SimpleNamespace(storage=storage))
    monkeypatch.setattr(
        generate_paragraph_subtitles_module, "_subtitle_service", _FakeSubtitleService()
    )

    task = generate_paragraph_subtitles_module.generate_paragraph_subtitles
    run_callable = getattr(task, "run", task)

    with pytest.raises(RateLimitError, match="rate limited"):
        run_callable(run_id=1, section_id="s1")


def test_generate_paragraph_subtitles_rejects_path_traversal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """audio_path outside artifact root must be rejected."""
    provider = _FakeProvider()
    _patch_registry(
        monkeypatch, generate_paragraph_subtitles_module, _FakeRegistry(_FakeEntry(), provider)
    )

    # Audio file exists but is outside artifact root
    audio_file = tmp_path / "outside" / "evil.wav"
    audio_file.parent.mkdir(parents=True, exist_ok=True)
    audio_file.write_bytes(b"fake audio")
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    monkeypatch.setattr(
        generate_paragraph_subtitles_module,
        "_audio_service",
        SimpleNamespace(
            get_paragraph_audio=lambda *a, **kw: _async_value(SimpleNamespace(path=str(audio_file)))
        ),
    )

    storage = _make_storage(run_id=1, stage="SUBTITLE_GENERATING")
    monkeypatch.setattr("tasks.task_runner._run_service", SimpleNamespace(storage=storage))
    monkeypatch.setattr(
        generate_paragraph_subtitles_module, "_subtitle_service", _FakeSubtitleService()
    )

    task = generate_paragraph_subtitles_module.generate_paragraph_subtitles
    run_callable = getattr(task, "run", task)

    with pytest.raises(ValueError, match="outside artifact root"):
        run_callable(run_id=1, section_id="s1")


def test_generate_paragraph_subtitles_rejects_symlink_escape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Symlink pointing outside artifact root must be rejected."""
    provider = _FakeProvider()
    _patch_registry(
        monkeypatch, generate_paragraph_subtitles_module, _FakeRegistry(_FakeEntry(), provider)
    )

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("sensitive data")
    symlink = artifact_root / "evil.wav"
    symlink.symlink_to(secret)
    monkeypatch.setattr(
        generate_paragraph_subtitles_module,
        "_audio_service",
        SimpleNamespace(
            get_paragraph_audio=lambda *a, **kw: _async_value(SimpleNamespace(path=str(symlink)))
        ),
    )

    storage = _make_storage(run_id=1, stage="SUBTITLE_GENERATING")
    monkeypatch.setattr("tasks.task_runner._run_service", SimpleNamespace(storage=storage))
    monkeypatch.setattr(
        generate_paragraph_subtitles_module, "_subtitle_service", _FakeSubtitleService()
    )

    task = generate_paragraph_subtitles_module.generate_paragraph_subtitles
    run_callable = getattr(task, "run", task)

    with pytest.raises(ValueError, match="outside artifact root"):
        run_callable(run_id=1, section_id="s1")
