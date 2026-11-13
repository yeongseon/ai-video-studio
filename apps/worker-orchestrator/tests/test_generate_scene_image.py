from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from creator_domain.models.stage import RunStage
from tasks import generate_scene_image as generate_scene_image_module


@dataclass
class FakeEntry:
    provider_type: str = "sd_local"
    endpoint: str = "http://stable-diffusion:7860"
    requires_gpu: bool = True
    default_params: dict[str, object] | None = None


@dataclass
class FakeImageResult:
    """Mimics base.ImageResult returned by ImageProvider.generate()."""

    image_path: str = ""
    width: int = 512
    height: int = 512
    model_key: str = "sd15"
    metadata: dict[str, Any] | None = None


class FakeImageProvider:
    def __init__(
        self,
        result: FakeImageResult | None = None,
        error: Exception | None = None,
        per_scene_errors: dict[str, Exception] | None = None,
    ) -> None:
        self.result = result or FakeImageResult()
        self.error = error
        self.per_scene_errors = per_scene_errors or {}
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    async def generate(
        self, prompt: str, params: dict[str, object] | None = None
    ) -> FakeImageResult:
        self.calls.append((prompt, params))
        # Check per-scene errors based on prompt content
        for scene_key, exc in self.per_scene_errors.items():
            if scene_key in prompt:
                raise exc
        if self.error is not None:
            raise self.error
        return FakeImageResult(
            image_path=f"/tmp/generated_{len(self.calls)}.png",
            model_key="sd15",
        )


class FakeStorage:
    def __init__(
        self,
        error: Exception | None = None,
        runs: dict[int, dict[str, object]] | None = None,
    ) -> None:
        self.error = error
        self.calls: list[tuple[int, dict[str, object]]] = []
        self._runs: dict[int, dict[str, object]] = runs or {}

    async def get_run(self, run_id: int) -> dict[str, object] | None:
        return self._runs.get(run_id)

    async def update_run(self, run_id: int, updates: dict[str, object]) -> dict[str, object]:
        self.calls.append((run_id, updates))
        if self.error is not None:
            raise self.error
        return {"id": run_id, **updates}

    async def conditional_update_run(
        self,
        run_id: int,
        updates: dict[str, object],
        expected_stages: frozenset[str],
    rejected_statuses: frozenset[str] | None = None,
    ) -> tuple[bool, dict[str, object] | None]:
        row = self._runs.get(run_id)
        if row is None:
            return False, None
        if row.get("current_stage") not in expected_stages:
            return False, dict(row)
        self.calls.append((run_id, updates))
        if self.error is not None:
            raise self.error
        row.update(updates)
        self._runs[run_id] = row
        return True, dict(row)


def _make_storage(
    run_id: int = 101, stage: str = "VISUAL_PLAN_REVIEW", **extra: Any
) -> FakeStorage:
    run_row: dict[str, object] = {"id": run_id, "current_stage": stage, **extra}
    return FakeStorage(runs={run_id: run_row})


@dataclass
class FakeVisualScene:
    scene_id: str = "scene-sec-0"
    section_id: str = "sec-0"
    scene_index: int = 0
    section_type: str = "hook"
    original_text: str = "Hook text"
    prompt: str = "A dramatic opening shot"
    prompt_edited: bool = False
    prompt_source: str = "auto_generated"
    style_tags: list[str] = field(default_factory=list)
    mood: str | None = None
    composition: str | None = None
    generation_status: str = "pending"
    latest_asset_id: int | None = None


@dataclass
class FakeVisualPlan:
    id: int = 1
    run_id: int = 101
    version: int = 1
    scenes: list[FakeVisualScene] = field(default_factory=lambda: [FakeVisualScene()])
    created_at: str = "2026-01-01T00:00:00Z"


class FakeVisualPlanService:
    def __init__(
        self,
        plan: FakeVisualPlan | None = None,
        error: Exception | None = None,
    ) -> None:
        self.plan = plan
        self.error = error

    async def get_active_plan(self, run_id: int) -> FakeVisualPlan | None:
        if self.error is not None:
            raise self.error
        return self.plan


@dataclass
class FakeVisualAsset:
    id: int = 1
    run_id: int = 101
    scene_id: str = "scene-sec-0"
    version: int = 1
    asset_path: str = "/tmp/test.png"
    storage_key: str | None = None
    prompt_snapshot: str | None = None
    model_used: str | None = None
    provider_type: str | None = None
    is_active: bool = True
    created_at: str = "2026-01-01T00:00:00Z"


class FakeVisualAssetService:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self._next_id = 1
        self._next_version: dict[str, int] = {}

    async def create_asset(
        self,
        run_id: int,
        scene_id: int,
        asset_path: str,
        *,
        prompt_snapshot: str | None = None,
        model_used: str | None = None,
        provider_type: str | None = None,
        storage_provider: str | None = None,
        storage_key: str | None = None,
        is_active: bool | None = None,
        idempotency_key: str | None = None,
        upload_intent_id: int | None = None,
    ) -> FakeVisualAsset:
        call_data = {
            "run_id": run_id,
            "scene_id": scene_id,
            "asset_path": asset_path,
            "prompt_snapshot": prompt_snapshot,
            "model_used": model_used,
            "provider_type": provider_type,
            "storage_provider": storage_provider,
            "storage_key": storage_key,
            "is_active": is_active,
        }
        self.calls.append(call_data)
        if self.error is not None:
            raise self.error
        version_key = f"{run_id}:{scene_id}"
        version = self._next_version.get(version_key, 0) + 1
        self._next_version[version_key] = version
        asset = FakeVisualAsset(
            id=self._next_id,
            run_id=run_id,
            scene_id=scene_id,
            version=version,
            asset_path=asset_path,
            storage_key=storage_key,
            prompt_snapshot=prompt_snapshot,
            model_used=model_used,
            provider_type=provider_type,
            is_active=is_active,
        )
        self._next_id += 1
        return asset


class FakeRegistry:
    def __init__(
        self,
        entry: FakeEntry,
        provider: FakeImageProvider,
        resolve_error: Exception | None = None,
    ) -> None:
        self.entry = entry
        self.provider = provider
        self.resolve_error = resolve_error

    def resolve(self, _model_key: str) -> FakeEntry:
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.entry

    def get_provider(self, _model_key: str) -> FakeImageProvider:
        return self.provider


def _patch_registry(monkeypatch: pytest.MonkeyPatch, registry: FakeRegistry) -> None:
    monkeypatch.setattr(generate_scene_image_module, "get_default_registry", lambda: registry)


def _patch_redis(monkeypatch: pytest.MonkeyPatch, redis_client: object) -> None:
    redis_stub = SimpleNamespace(Redis=SimpleNamespace(from_url=lambda _: redis_client))
    monkeypatch.setattr("tasks.task_runner.redis", redis_stub)


def _patch_services(
    monkeypatch: pytest.MonkeyPatch,
    visual_plan_service: FakeVisualPlanService,
    visual_asset_service: FakeVisualAssetService,
    storage: FakeStorage,
) -> None:
    monkeypatch.setattr(generate_scene_image_module, "_visual_plan_service", visual_plan_service)
    monkeypatch.setattr(generate_scene_image_module, "_visual_asset_service", visual_asset_service)
    monkeypatch.setattr(
        generate_scene_image_module, "_run_service", SimpleNamespace(storage=storage)
    )
    monkeypatch.setattr("tasks.task_runner._run_service", SimpleNamespace(storage=storage))


def _invoke_task(**kwargs: Any) -> dict[str, object]:
    task = generate_scene_image_module.generate_scene_image
    run_callable = getattr(task, "run", task)
    return run_callable(**kwargs)


# ──────────────────────────────────────────────────────────────────────
# Happy-path tests
# ──────────────────────────────────────────────────────────────────────


def test_happy_path_single_scene_with_gpu_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeImageProvider()
    entry = FakeEntry(requires_gpu=True, default_params={"steps": 20})
    registry = FakeRegistry(entry=entry, provider=provider)
    _patch_registry(monkeypatch, registry)

    redis_client = object()
    _patch_redis(monkeypatch, redis_client)

    lock_calls: list[str] = []
    release_calls: list[str] = []
    monkeypatch.setattr(
        "tasks.task_runner.acquire_gpu_lock",
        lambda client, task_id: (lock_calls.append(task_id) or f"{task_id}:fake-token"),
    )
    monkeypatch.setattr(
        "tasks.task_runner.release_gpu_lock",
        lambda client, token: release_calls.append(token.split(":")[0]) or True,
    )

    plan = FakeVisualPlan(
        run_id=101, scenes=[FakeVisualScene(scene_id="scene-sec-0", prompt="A hook shot")]
    )
    vp_service = FakeVisualPlanService(plan=plan)
    va_service = FakeVisualAssetService()
    storage = _make_storage(run_id=101, stage="VISUAL_PLAN_REVIEW")
    _patch_services(monkeypatch, vp_service, va_service, storage)

    result = _invoke_task(run_id=101, scene_id="scene-sec-0")

    assert result["status"] == "success"
    assert result["total_scenes"] == 1
    assert result["succeeded"] == 1
    assert result["failed"] == 0
    assert len(lock_calls) == 1
    assert len(release_calls) == 1
    assert va_service.calls[0]["scene_id"] == "scene-sec-0"
    assert va_service.calls[0]["prompt_snapshot"] == "A hook shot"
    assert storage.calls == [(101, {"current_stage": RunStage.VISUAL_ASSET_REVIEW, "status": "running"})]
    assert va_service.calls[0]["storage_provider"] == "local"
    assert va_service.calls[0]["storage_key"] is not None


def test_happy_path_all_scenes(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeImageProvider()
    entry = FakeEntry(requires_gpu=False)
    registry = FakeRegistry(entry=entry, provider=provider)
    _patch_registry(monkeypatch, registry)

    scenes = [
        FakeVisualScene(scene_id="scene-sec-0", prompt="Hook shot"),
        FakeVisualScene(scene_id="scene-sec-1", scene_index=1, prompt="Body shot"),
        FakeVisualScene(scene_id="scene-sec-2", scene_index=2, prompt="CTA shot"),
    ]
    plan = FakeVisualPlan(run_id=102, scenes=scenes)
    vp_service = FakeVisualPlanService(plan=plan)
    va_service = FakeVisualAssetService()
    storage = _make_storage(run_id=102, stage="VISUAL_PLAN_REVIEW")
    _patch_services(monkeypatch, vp_service, va_service, storage)

    result = _invoke_task(run_id=102)

    assert result["status"] == "success"
    assert result["total_scenes"] == 3
    assert result["succeeded"] == 3
    assert result["failed"] == 0
    assert result["scene_id"] is None  # All scenes mode
    assert len(va_service.calls) == 3
    assert len(provider.calls) == 3
    assert storage.calls == [(102, {"current_stage": RunStage.VISUAL_ASSET_REVIEW, "status": "running"})]


def test_happy_path_without_gpu_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeImageProvider()
    entry = FakeEntry(
        provider_type="external_image", endpoint="https://api.example.com", requires_gpu=False
    )
    registry = FakeRegistry(entry=entry, provider=provider)
    _patch_registry(monkeypatch, registry)

    # GPU lock should NOT be called
    monkeypatch.setattr(
        "tasks.task_runner.acquire_gpu_lock",
        lambda *_: (_ for _ in ()).throw(RuntimeError("unexpected")),
    )
    monkeypatch.setattr(
        "tasks.task_runner.release_gpu_lock",
        lambda *_: (_ for _ in ()).throw(RuntimeError("unexpected")),
    )

    plan = FakeVisualPlan(run_id=103, scenes=[FakeVisualScene()])
    vp_service = FakeVisualPlanService(plan=plan)
    va_service = FakeVisualAssetService()
    storage = _make_storage(run_id=103, stage="VISUAL_PLAN_REVIEW")
    _patch_services(monkeypatch, vp_service, va_service, storage)

    result = _invoke_task(run_id=103)

    assert result["status"] == "success"
    assert result["provider_type"] == "external_image"


def test_prompt_override_single_scene(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeImageProvider()
    entry = FakeEntry(requires_gpu=False)
    registry = FakeRegistry(entry=entry, provider=provider)
    _patch_registry(monkeypatch, registry)

    plan = FakeVisualPlan(
        run_id=104, scenes=[FakeVisualScene(scene_id="scene-sec-0", prompt="Original prompt")]
    )
    vp_service = FakeVisualPlanService(plan=plan)
    va_service = FakeVisualAssetService()
    storage = _make_storage(run_id=104, stage="VISUAL_PLAN_REVIEW")
    _patch_services(monkeypatch, vp_service, va_service, storage)

    result = _invoke_task(
        run_id=104, scene_id="scene-sec-0", prompt_override="Custom override prompt"
    )

    assert result["status"] == "success"
    assert provider.calls[0][0] == "Custom override prompt"
    assert va_service.calls[0]["prompt_snapshot"] == "Custom override prompt"


def test_prompt_override_ignored_in_all_scene_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """prompt_override is only used when scene_id is specified."""
    provider = FakeImageProvider()
    entry = FakeEntry(requires_gpu=False)
    registry = FakeRegistry(entry=entry, provider=provider)
    _patch_registry(monkeypatch, registry)

    plan = FakeVisualPlan(
        run_id=105, scenes=[FakeVisualScene(scene_id="scene-sec-0", prompt="Original prompt")]
    )
    vp_service = FakeVisualPlanService(plan=plan)
    va_service = FakeVisualAssetService()
    storage = _make_storage(run_id=105, stage="VISUAL_PLAN_REVIEW")
    _patch_services(monkeypatch, vp_service, va_service, storage)

    result = _invoke_task(run_id=105, prompt_override="Should be ignored")

    assert result["status"] == "success"
    # Should use the scene prompt, not the override
    assert provider.calls[0][0] == "Original prompt"


def test_inactive_asset_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeImageProvider()
    entry = FakeEntry(requires_gpu=False)
    registry = FakeRegistry(entry=entry, provider=provider)
    _patch_registry(monkeypatch, registry)

    plan = FakeVisualPlan(run_id=106, scenes=[FakeVisualScene()])
    vp_service = FakeVisualPlanService(plan=plan)
    va_service = FakeVisualAssetService()
    storage = _make_storage(run_id=106, stage="VISUAL_PLAN_REVIEW")
    _patch_services(monkeypatch, vp_service, va_service, storage)

    result = _invoke_task(run_id=106, is_active=False)

    assert result["status"] == "success"
    assert va_service.calls[0]["is_active"] is False


# ──────────────────────────────────────────────────────────────────────
# Partial failure tests
# ──────────────────────────────────────────────────────────────────────


def test_partial_failure_advances_to_review(monkeypatch: pytest.MonkeyPatch) -> None:
    """If some scenes succeed and some fail, still advance to VISUAL_ASSET_REVIEW."""
    provider = FakeImageProvider(per_scene_errors={"Body shot": RuntimeError("GPU OOM")})
    entry = FakeEntry(requires_gpu=False)
    registry = FakeRegistry(entry=entry, provider=provider)
    _patch_registry(monkeypatch, registry)

    scenes = [
        FakeVisualScene(scene_id="scene-sec-0", prompt="Hook shot"),
        FakeVisualScene(scene_id="scene-sec-1", scene_index=1, prompt="Body shot"),
        FakeVisualScene(scene_id="scene-sec-2", scene_index=2, prompt="CTA shot"),
    ]
    plan = FakeVisualPlan(run_id=201, scenes=scenes)
    vp_service = FakeVisualPlanService(plan=plan)
    va_service = FakeVisualAssetService()
    storage = _make_storage(run_id=201, stage="VISUAL_PLAN_REVIEW")
    _patch_services(monkeypatch, vp_service, va_service, storage)

    result = _invoke_task(run_id=201)

    assert result["status"] == "partial"
    assert result["succeeded"] == 2
    assert result["failed"] == 1
    # Should still advance to review
    assert storage.calls == [(201, {"current_stage": RunStage.VISUAL_ASSET_REVIEW, "status": "running"})]
    # Only 2 assets saved (the successful ones)
    assert len(va_service.calls) == 2


def test_all_scenes_fail_marks_run_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """If ALL scenes fail, mark run as FAILED."""
    provider = FakeImageProvider(error=RuntimeError("GPU crashed"))
    entry = FakeEntry(requires_gpu=False)
    registry = FakeRegistry(entry=entry, provider=provider)
    _patch_registry(monkeypatch, registry)

    scenes = [
        FakeVisualScene(scene_id="scene-sec-0", prompt="Hook"),
        FakeVisualScene(scene_id="scene-sec-1", scene_index=1, prompt="Body"),
    ]
    plan = FakeVisualPlan(run_id=202, scenes=scenes)
    vp_service = FakeVisualPlanService(plan=plan)
    va_service = FakeVisualAssetService()
    storage = _make_storage(run_id=202, stage="VISUAL_PLAN_REVIEW")
    _patch_services(monkeypatch, vp_service, va_service, storage)

    result = _invoke_task(run_id=202)

    assert result["status"] == "failed"
    assert result["succeeded"] == 0
    assert result["failed"] == 2
    assert storage.calls == [(202, {"current_stage": "FAILED", "status": "failed"})]
    assert len(va_service.calls) == 0


def test_all_scenes_fail_and_cas_raises_propagates_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """If ALL scenes fail AND the CAS to FAILED itself errors, the exception must propagate.

    Regression: previously the except-block swallowed the storage error and returned
    a 'failed' payload, making Celery mark the task as SUCCESS while the run stayed
    stuck in its original stage.
    """
    provider = FakeImageProvider(error=RuntimeError("GPU crashed"))
    entry = FakeEntry(requires_gpu=False)
    registry = FakeRegistry(entry=entry, provider=provider)
    _patch_registry(monkeypatch, registry)

    scenes = [
        FakeVisualScene(scene_id="scene-sec-0", prompt="Hook"),
    ]
    plan = FakeVisualPlan(run_id=203, scenes=scenes)
    vp_service = FakeVisualPlanService(plan=plan)
    va_service = FakeVisualAssetService()
    storage = _make_storage(run_id=203, stage="VISUAL_PLAN_REVIEW")
    storage.error = ConnectionError("Redis down")  # CAS itself fails
    _patch_services(monkeypatch, vp_service, va_service, storage)

    with pytest.raises(ConnectionError, match="Redis down"):
        _invoke_task(run_id=203)


# ──────────────────────────────────────────────────────────────────────
# Stage guard tests
# ──────────────────────────────────────────────────────────────────────


def test_stage_guard_rejects_wrong_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeImageProvider()
    entry = FakeEntry(requires_gpu=False)
    registry = FakeRegistry(entry=entry, provider=provider)
    _patch_registry(monkeypatch, registry)

    vp_service = FakeVisualPlanService(plan=FakeVisualPlan())
    va_service = FakeVisualAssetService()
    storage = _make_storage(run_id=301, stage="SCRIPT_REVIEW")
    _patch_services(monkeypatch, vp_service, va_service, storage)

    with pytest.raises(ValueError, match="expected one of"):
        _invoke_task(run_id=301)

    assert provider.calls == []
    assert va_service.calls == []
    assert storage.calls == []


def test_stage_guard_accepts_visual_asset_generating(monkeypatch: pytest.MonkeyPatch) -> None:
    """VISUAL_ASSET_GENERATING stage is accepted (retry scenario)."""
    provider = FakeImageProvider()
    entry = FakeEntry(requires_gpu=False)
    registry = FakeRegistry(entry=entry, provider=provider)
    _patch_registry(monkeypatch, registry)

    plan = FakeVisualPlan(run_id=302)
    vp_service = FakeVisualPlanService(plan=plan)
    va_service = FakeVisualAssetService()
    storage = _make_storage(run_id=302, stage="VISUAL_ASSET_GENERATING")
    _patch_services(monkeypatch, vp_service, va_service, storage)

    result = _invoke_task(run_id=302)

    assert result["status"] == "success"
    assert len(va_service.calls) == 1


def test_stage_guard_run_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeImageProvider()
    entry = FakeEntry(requires_gpu=False)
    registry = FakeRegistry(entry=entry, provider=provider)
    _patch_registry(monkeypatch, registry)

    vp_service = FakeVisualPlanService()
    va_service = FakeVisualAssetService()
    storage = FakeStorage()
    _patch_services(monkeypatch, vp_service, va_service, storage)

    with pytest.raises(ValueError, match="Run 999 not found"):
        _invoke_task(run_id=999)

    assert provider.calls == []
    assert storage.calls == []


def test_stage_guard_no_visual_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeImageProvider()
    entry = FakeEntry(requires_gpu=False)
    registry = FakeRegistry(entry=entry, provider=provider)
    _patch_registry(monkeypatch, registry)

    vp_service = FakeVisualPlanService(plan=None)
    va_service = FakeVisualAssetService()
    storage = _make_storage(run_id=303, stage="VISUAL_PLAN_REVIEW")
    _patch_services(monkeypatch, vp_service, va_service, storage)

    with pytest.raises(ValueError, match="No active visual plan"):
        _invoke_task(run_id=303)

    assert provider.calls == []
    assert storage.calls == []


def test_stage_guard_scene_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeImageProvider()
    entry = FakeEntry(requires_gpu=False)
    registry = FakeRegistry(entry=entry, provider=provider)
    _patch_registry(monkeypatch, registry)

    plan = FakeVisualPlan(run_id=304, scenes=[FakeVisualScene(scene_id="scene-sec-0")])
    vp_service = FakeVisualPlanService(plan=plan)
    va_service = FakeVisualAssetService()
    storage = _make_storage(run_id=304, stage="VISUAL_PLAN_REVIEW")
    _patch_services(monkeypatch, vp_service, va_service, storage)

    with pytest.raises(ValueError, match="Scene 'nonexistent' not found"):
        _invoke_task(run_id=304, scene_id="nonexistent")

    assert provider.calls == []
    assert storage.calls == []


def test_stage_guard_blocks_before_provider_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stage guard must reject BEFORE provider resolution."""
    registry = FakeRegistry(
        entry=FakeEntry(),
        provider=FakeImageProvider(),
        resolve_error=KeyError("Model not found"),
    )
    _patch_registry(monkeypatch, registry)

    vp_service = FakeVisualPlanService(plan=FakeVisualPlan())
    va_service = FakeVisualAssetService()
    storage = _make_storage(run_id=305, stage="SCRIPT_REVIEW")
    _patch_services(monkeypatch, vp_service, va_service, storage)

    with pytest.raises(ValueError, match="expected one of"):
        _invoke_task(run_id=305, model_key="bad-model")

    assert storage.calls == []
    assert storage._runs[305]["current_stage"] == "SCRIPT_REVIEW"


# ──────────────────────────────────────────────────────────────────────
# Provider resolution failure
# ──────────────────────────────────────────────────────────────────────


def test_provider_resolution_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = FakeRegistry(
        entry=FakeEntry(),
        provider=FakeImageProvider(),
        resolve_error=KeyError("Model not found"),
    )
    _patch_registry(monkeypatch, registry)

    plan = FakeVisualPlan(run_id=401)
    vp_service = FakeVisualPlanService(plan=plan)
    va_service = FakeVisualAssetService()
    storage = _make_storage(run_id=401, stage="VISUAL_PLAN_REVIEW")
    _patch_services(monkeypatch, vp_service, va_service, storage)

    with pytest.raises(KeyError, match="Model not found"):
        _invoke_task(run_id=401, model_key="missing")

    assert va_service.calls == []
    assert storage.calls == [(401, {"current_stage": "FAILED", "status": "failed"})]


# ──────────────────────────────────────────────────────────────────────
# GPU lock and error propagation tests
# ──────────────────────────────────────────────────────────────────────


def test_gpu_lock_timeout_single_scene(monkeypatch: pytest.MonkeyPatch) -> None:
    """GPU lock timeout for a single scene still creates scene_results."""
    provider = FakeImageProvider()
    entry = FakeEntry(requires_gpu=True)
    registry = FakeRegistry(entry=entry, provider=provider)
    _patch_registry(monkeypatch, registry)

    redis_client = object()
    _patch_redis(monkeypatch, redis_client)

    monkeypatch.setattr(
        "tasks.task_runner.acquire_gpu_lock",
        lambda *_: (_ for _ in ()).throw(TimeoutError("lock timeout")),
    )
    monkeypatch.setattr("tasks.task_runner.release_gpu_lock", lambda *_: None)

    plan = FakeVisualPlan(run_id=501, scenes=[FakeVisualScene()])
    vp_service = FakeVisualPlanService(plan=plan)
    va_service = FakeVisualAssetService()
    storage = _make_storage(run_id=501, stage="VISUAL_PLAN_REVIEW")
    _patch_services(monkeypatch, vp_service, va_service, storage)

    # All scenes fail → task returns with failed status (doesn't raise)
    result = _invoke_task(run_id=501)

    assert result["status"] == "failed"
    assert result["failed"] == 1
    assert storage.calls == [(501, {"current_stage": "FAILED", "status": "failed"})]


def test_releases_gpu_lock_when_provider_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeImageProvider(error=RuntimeError("provider exploded"))
    entry = FakeEntry(requires_gpu=True)
    registry = FakeRegistry(entry=entry, provider=provider)
    _patch_registry(monkeypatch, registry)

    redis_client = object()
    _patch_redis(monkeypatch, redis_client)

    released: list[str] = []
    monkeypatch.setattr("tasks.task_runner.acquire_gpu_lock", lambda *_: True)
    monkeypatch.setattr(
        "tasks.task_runner.release_gpu_lock",
        lambda _, task_id: released.append(task_id) or True,
    )

    plan = FakeVisualPlan(run_id=502, scenes=[FakeVisualScene()])
    vp_service = FakeVisualPlanService(plan=plan)
    va_service = FakeVisualAssetService()
    storage = _make_storage(run_id=502, stage="VISUAL_PLAN_REVIEW")
    _patch_services(monkeypatch, vp_service, va_service, storage)

    result = _invoke_task(run_id=502)

    assert result["status"] == "failed"
    assert len(released) == 1  # GPU lock released even on failure


def test_soft_timeout_removes_unfinished_scene_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    # Given a provider that reaches the scene output boundary before a soft timeout.
    provider = FakeImageProvider(error=SoftTimeLimitExceeded())
    _patch_registry(monkeypatch, FakeRegistry(FakeEntry(requires_gpu=False), provider))
    monkeypatch.setattr(generate_scene_image_module, "_ARTIFACTS_BASE", str(tmp_path))
    plan = FakeVisualPlan(run_id=504, scenes=[FakeVisualScene()])
    _patch_services(
        monkeypatch,
        FakeVisualPlanService(plan=plan),
        FakeVisualAssetService(),
        _make_storage(run_id=504, stage="VISUAL_PLAN_REVIEW"),
    )

    # When the production task receives the cooperative timeout.
    with pytest.raises(SoftTimeLimitExceeded):
        _invoke_task(run_id=504)

    # Then no unfinished scene output remains on local storage.
    assert list(tmp_path.rglob("*.png")) == []


def test_release_gpu_lock_failure_does_not_mask_scene_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If release_gpu_lock raises, the scene is still recorded as failed."""
    provider = FakeImageProvider(error=RuntimeError("generation boom"))
    entry = FakeEntry(requires_gpu=True)
    registry = FakeRegistry(entry=entry, provider=provider)
    _patch_registry(monkeypatch, registry)

    redis_client = object()
    _patch_redis(monkeypatch, redis_client)

    monkeypatch.setattr("tasks.task_runner.acquire_gpu_lock", lambda *_: True)
    monkeypatch.setattr(
        "tasks.task_runner.release_gpu_lock",
        lambda *_: (_ for _ in ()).throw(ConnectionError("redis gone")),
    )

    plan = FakeVisualPlan(run_id=503, scenes=[FakeVisualScene()])
    vp_service = FakeVisualPlanService(plan=plan)
    va_service = FakeVisualAssetService()
    storage = _make_storage(run_id=503, stage="VISUAL_PLAN_REVIEW")
    _patch_services(monkeypatch, vp_service, va_service, storage)

    result = _invoke_task(run_id=503)

    assert result["status"] == "failed"
    assert result["failed"] == 1


# ──────────────────────────────────────────────────────────────────────
# Race condition / CAS tests
# ──────────────────────────────────────────────────────────────────────


class RaceConditionStorage(FakeStorage):
    """Storage that simulates a competing task advancing the run between operations."""

    def __init__(
        self, run_id: int, initial_stage: str, advanced_stage: str, advance_after: int = 1
    ) -> None:
        super().__init__(runs={run_id: {"id": run_id, "current_stage": initial_stage}})
        self._target_id = run_id
        self._advanced_stage = advanced_stage
        self._advance_after = advance_after
        self._op_count = 0

    def _maybe_advance(self, run_id: int) -> None:
        if run_id == self._target_id:
            self._op_count += 1
            if self._op_count > self._advance_after:
                self._runs[run_id] = {"id": run_id, "current_stage": self._advanced_stage}

    async def get_run(self, run_id: int) -> dict[str, object] | None:
        self._maybe_advance(run_id)
        return self._runs.get(run_id)

    async def conditional_update_run(
        self,
        run_id: int,
        updates: dict[str, object],
        expected_stages: frozenset[str],
    rejected_statuses: frozenset[str] | None = None,
    ) -> tuple[bool, dict[str, object] | None]:
        self._maybe_advance(run_id)
        row = self._runs.get(run_id)
        if row is None:
            return False, None
        if row.get("current_stage") not in expected_stages:
            return False, dict(row)
        self.calls.append((run_id, updates))
        row.update(updates)
        self._runs[run_id] = row
        return True, dict(row)


def test_success_transition_skips_when_run_already_advanced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If run has advanced past image generation, skip the VISUAL_ASSET_REVIEW write."""
    provider = FakeImageProvider()
    entry = FakeEntry(requires_gpu=False)
    registry = FakeRegistry(entry=entry, provider=provider)
    _patch_registry(monkeypatch, registry)

    plan = FakeVisualPlan(run_id=601)
    vp_service = FakeVisualPlanService(plan=plan)
    va_service = FakeVisualAssetService()
    storage = RaceConditionStorage(
        run_id=601, initial_stage="VISUAL_PLAN_REVIEW", advanced_stage="AUDIO_GENERATING"
    )
    _patch_services(monkeypatch, vp_service, va_service, storage)

    result = _invoke_task(run_id=601)

    assert result["status"] == "success"
    assert storage.calls == []  # CAS rejected — run already advanced
    assert storage._runs[601]["current_stage"] == "AUDIO_GENERATING"


def test_failure_transition_skips_when_run_already_advanced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If run has advanced and all scenes fail, FAILED write must be skipped."""
    provider = FakeImageProvider(error=RuntimeError("fail"))
    entry = FakeEntry(requires_gpu=False)
    registry = FakeRegistry(entry=entry, provider=provider)
    _patch_registry(monkeypatch, registry)

    plan = FakeVisualPlan(run_id=602)
    vp_service = FakeVisualPlanService(plan=plan)
    va_service = FakeVisualAssetService()
    storage = RaceConditionStorage(
        run_id=602, initial_stage="VISUAL_PLAN_REVIEW", advanced_stage="VISUAL_ASSET_REVIEW"
    )
    _patch_services(monkeypatch, vp_service, va_service, storage)

    result = _invoke_task(run_id=602)

    assert result["status"] == "failed"
    assert storage.calls == []  # CAS rejected
    assert storage._runs[602]["current_stage"] == "VISUAL_ASSET_REVIEW"


# ──────────────────────────────────────────────────────────────────────
# Metadata / audit tests
# ──────────────────────────────────────────────────────────────────────


def test_asset_metadata_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that model_used and provider_type are passed to asset service."""
    provider = FakeImageProvider()
    entry = FakeEntry(provider_type="sd_local", requires_gpu=False)
    registry = FakeRegistry(entry=entry, provider=provider)
    _patch_registry(monkeypatch, registry)

    plan = FakeVisualPlan(run_id=701, scenes=[FakeVisualScene(prompt="Test prompt")])
    vp_service = FakeVisualPlanService(plan=plan)
    va_service = FakeVisualAssetService()
    storage = _make_storage(run_id=701, stage="VISUAL_PLAN_REVIEW")
    _patch_services(monkeypatch, vp_service, va_service, storage)

    result = _invoke_task(run_id=701, model_key="sd15")

    assert result["status"] == "success"
    call = va_service.calls[0]
    assert call["model_used"] == "sd15"
    assert call["provider_type"] == "sd_local"
    assert call["prompt_snapshot"] == "Test prompt"


def test_scene_results_contain_asset_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify scene_results contain asset_id, version, and asset_path."""
    provider = FakeImageProvider()
    entry = FakeEntry(requires_gpu=False)
    registry = FakeRegistry(entry=entry, provider=provider)
    _patch_registry(monkeypatch, registry)

    plan = FakeVisualPlan(run_id=702, scenes=[FakeVisualScene()])
    vp_service = FakeVisualPlanService(plan=plan)
    va_service = FakeVisualAssetService()
    storage = _make_storage(run_id=702, stage="VISUAL_PLAN_REVIEW")
    _patch_services(monkeypatch, vp_service, va_service, storage)

    result = _invoke_task(run_id=702)

    assert result["status"] == "success"
    scene_results = cast(list[dict[str, object]], result["scene_results"])
    assert len(scene_results) == 1
    sr = scene_results[0]
    assert sr["status"] == "success"
    assert sr["asset_id"] == 1
    assert sr["version"] == 1
    assert "asset_path" in sr


def test_regeneration_uses_unique_asset_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeImageProvider()
    entry = FakeEntry(requires_gpu=False)
    registry = FakeRegistry(entry=entry, provider=provider)
    _patch_registry(monkeypatch, registry)

    plan = FakeVisualPlan(run_id=703, scenes=[FakeVisualScene()])
    vp_service = FakeVisualPlanService(plan=plan)
    va_service = FakeVisualAssetService()
    storage = _make_storage(run_id=703, stage="VISUAL_PLAN_REVIEW")
    _patch_services(monkeypatch, vp_service, va_service, storage)

    first = _invoke_task(run_id=703, scene_id="scene-sec-0")
    second = _invoke_task(run_id=703, scene_id="scene-sec-0", prompt_override="New prompt")

    first_results = cast(list[dict[str, object]], first["scene_results"])
    second_results = cast(list[dict[str, object]], second["scene_results"])
    first_path = first_results[0]["asset_path"]
    second_path = second_results[0]["asset_path"]

    assert first_path != second_path
    assert va_service.calls[0]["asset_path"] != va_service.calls[1]["asset_path"]
