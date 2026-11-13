"""Jobs that write media are refused before their first write when storage is low (#1009)."""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import os
import tempfile
from collections import namedtuple
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from creator_domain.models.stage import RunStage
from creator_provider.exceptions import ProviderTimeoutError
from creator_service import job_capacity
from creator_service.actionable_errors import failure_summary_from_code
from creator_service.postgres_task_tracking_storage import PostgresTaskTrackingStorage
from creator_service.postgres_usage_storage import PostgresUsageStorage
from creator_service.storage_policy import FilesystemLowSpace
from creator_service.usage_service import InMemoryUsageStorage, UsageService
from tasks import task_runner
from tasks.scene_batch_lifecycle import SAFE_FAILURE_STAGES, SAFE_SUCCESS_STAGES
from worker_loop import run_in_worker_loop

from .provider_boundary_support import GenerationCase, generation_case as generation_case  # noqa: F401
from .terminal_postgres_support import terminal_pool as terminal_pool  # noqa: F401
from .terminal_validation_support import RunnerCase, runner_case as runner_case  # noqa: F401

MIB = 1024 * 1024
_Usage = namedtuple("_Usage", "total used free")

# Tasks that write media (video, image, audio) and therefore need free space before they start.
GUARDED_TASKS = frozenset({
    "render_video", "generate_scene_image", "generate_audio", "generate_paragraph_audio",
})
# Tasks that only write text or tiny diagnostic files; a low watermark must not stop them.
EXEMPT_TASKS = frozenset({
    "generate_script", "generate_visual_plan", "generate_subtitles",
    "generate_paragraph_subtitles", "runtime_topology_probe",
})


class Volumes:
    """The artifact and scratch filesystems of one test, with a controllable amount of free space."""

    def __init__(self, artifacts: Path, scratch: Path) -> None:
        self.artifacts = artifacts
        self.scratch = scratch
        self.free = {artifacts: 9000 * MIB, scratch: 9000 * MIB}

    def set_free(self, *, artifact: int | None = None, scratch: int | None = None) -> None:
        if artifact is not None:
            self.free[self.artifacts] = artifact
        if scratch is not None:
            self.free[self.scratch] = scratch

    def disk_usage(self, path: str | os.PathLike[str]) -> _Usage:
        free = self.free[Path(path)]
        return _Usage(total=1024 * 1024 * MIB, used=1024 * 1024 * MIB - free, free=free)


@pytest.fixture
def volumes(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch,
) -> Volumes:
    # The artifact root is the test's tmp_path (where the generation fixtures write); scratch is separate.
    scratch = tmp_path_factory.mktemp("scratch").resolve()
    volumes = Volumes(tmp_path.resolve(), scratch)
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setattr(job_capacity.shutil, "disk_usage", volumes.disk_usage)
    return volumes


@pytest.fixture
def watermarks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDIA_ARTIFACT_DISK_LOW_WATERMARK_BYTES", str(500 * MIB))
    monkeypatch.setenv("MEDIA_TEMP_DISK_LOW_WATERMARK_BYTES", str(500 * MIB))


@pytest.fixture
def quota_case(runner_case: RunnerCase, monkeypatch: pytest.MonkeyPatch) -> UsageService:
    postgres = isinstance(runner_case.tracking.storage, PostgresTaskTrackingStorage)
    service = UsageService(PostgresUsageStorage() if postgres else InMemoryUsageStorage())
    run_in_worker_loop(service.set_quota(1, monthly_llm_calls=2))
    assert run_in_worker_loop(service.reserve_owned(1, "llm", "validation-task"))
    assert run_in_worker_loop(service.reserve_owned(1, "llm", "other-task"))
    monkeypatch.setattr("creator_service.usage_service.usage_service", service)
    return service


def _snapshot(root: Path) -> dict[str, tuple[str, int]]:
    entries: dict[str, tuple[str, int]] = {}
    for path in sorted(root.rglob("*")):
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "dir"
        entries[str(path.relative_to(root))] = (digest, path.stat().st_mtime_ns)
    return entries


def _guarded(config: task_runner.TaskRunnerConfig) -> task_runner.TaskRunnerConfig:
    return dataclasses.replace(config, requires_free_space=True)


def _delivery(task_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        request=SimpleNamespace(id=task_id, args=(), kwargs={}, retries=0), max_retries=2,
    )


def _executing(
    calls: list[str],
) -> Callable[[task_runner.TaskContext], Awaitable[task_runner.TaskResult]]:
    async def execute(ctx: task_runner.TaskContext) -> task_runner.TaskResult:
        calls.append(ctx.task_id)
        return task_runner.TaskResult()

    return execute


def test_task_is_refused_before_execution_when_the_artifact_filesystem_is_low(
    runner_case: RunnerCase, volumes: Volumes, watermarks: None,
) -> None:
    # Given a guarded task and an artifact filesystem under its floor.
    case = runner_case
    volumes.set_free(artifact=100 * MIB)
    calls: list[str] = []
    # When the worker receives the delivery.
    with pytest.raises(FilesystemLowSpace) as raised:
        task_runner.run_task(case.delivery(), case.run_id, _guarded(case.config), _executing(calls))
    # Then the task never executed, and the refusal is on record, retryable, and names the storage.
    assert calls == []
    assert raised.value.filesystem == "artifact"
    tracked = run_in_worker_loop(case.tracking.list_run_tasks(case.run_id))[0]
    assert (tracked.status, tracked.error_code) == ("failed", "ARTIFACT_STORAGE_LOW")
    summary = failure_summary_from_code(tracked.error_code)
    assert summary is not None and summary["retryable"] is True
    saved = run_in_worker_loop(case.runs.storage.get_run(case.run_id))
    assert (saved["current_stage"], saved["status"]) == ("FAILED", "failed")


def test_task_is_refused_before_execution_when_the_scratch_filesystem_is_low(
    runner_case: RunnerCase, volumes: Volumes, watermarks: None,
) -> None:
    # Given a guarded task and a scratch filesystem under its floor.
    case = runner_case
    volumes.set_free(scratch=100 * MIB)
    calls: list[str] = []
    # When the worker receives the delivery.
    with pytest.raises(FilesystemLowSpace) as raised:
        task_runner.run_task(case.delivery(), case.run_id, _guarded(case.config), _executing(calls))
    # Then it is refused for scratch, which the persisted code distinguishes from artifact storage.
    assert calls == [] and raised.value.filesystem == "scratch"
    tracked = run_in_worker_loop(case.tracking.list_run_tasks(case.run_id))[0]
    assert (tracked.status, tracked.error_code) == ("failed", "SCRATCH_STORAGE_LOW")


def test_task_proceeds_when_both_filesystems_have_room(
    runner_case: RunnerCase, volumes: Volumes, watermarks: None,
) -> None:
    # Given both filesystems above their floors.
    case = runner_case
    calls: list[str] = []
    # When the worker receives the delivery.
    task_runner.run_task(case.delivery(), case.run_id, _guarded(case.config), _executing(calls))
    # Then the task executed and the run advanced as usual.
    assert calls == ["validation-task"]
    saved = run_in_worker_loop(case.runs.storage.get_run(case.run_id))
    assert saved["current_stage"] == "SCRIPT_REVIEW"


def test_task_without_the_guard_ignores_low_storage(
    runner_case: RunnerCase, volumes: Volumes, watermarks: None,
) -> None:
    # Given a task that is not marked as writing media, and both filesystems far under their floors.
    case = runner_case
    volumes.set_free(artifact=1 * MIB, scratch=1 * MIB)
    calls: list[str] = []
    # When the worker receives the delivery.
    task_runner.run_task(case.delivery(), case.run_id, case.config, _executing(calls))
    # Then it runs exactly as before.
    assert calls == ["validation-task"]


def test_nothing_is_refused_unless_a_watermark_is_configured(
    runner_case: RunnerCase, volumes: Volumes, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given no watermark configuration and filesystems that report no free space.
    case = runner_case
    monkeypatch.delenv("MEDIA_ARTIFACT_DISK_LOW_WATERMARK_BYTES", raising=False)
    monkeypatch.delenv("MEDIA_TEMP_DISK_LOW_WATERMARK_BYTES", raising=False)
    volumes.set_free(artifact=0, scratch=0)
    calls: list[str] = []
    # When a guarded task is delivered.
    task_runner.run_task(case.delivery(), case.run_id, _guarded(case.config), _executing(calls))
    # Then existing installations are unaffected.
    assert calls == ["validation-task"]


def test_refusal_returns_the_claimed_tasks_quota_reservation(
    runner_case: RunnerCase, quota_case: UsageService, volumes: Volumes, watermarks: None,
) -> None:
    # Given two reservations and a delivery that is refused before any provider work.
    case = runner_case
    volumes.set_free(artifact=100 * MIB)
    # When the guarded task is refused.
    with pytest.raises(FilesystemLowSpace):
        task_runner.run_task(case.delivery(), case.run_id, _guarded(case.config), _executing([]))
    # Then its capacity is back, the other owner's is untouched, and nothing was billed.
    assert not run_in_worker_loop(quota_case.cancel_owned("validation-task"))
    assert run_in_worker_loop(quota_case.reserve_owned(1, "llm", "new-task"))
    assert not run_in_worker_loop(quota_case.reserve_owned(1, "llm", "overflow"))


def test_a_refused_run_succeeds_after_space_is_freed_and_the_run_is_restarted(
    runner_case: RunnerCase, volumes: Volumes, watermarks: None,
) -> None:
    # Given a run whose first delivery was refused for low artifact space.
    case = runner_case
    config = _guarded(case.config)
    volumes.set_free(artifact=100 * MIB)
    calls: list[str] = []
    with pytest.raises(FilesystemLowSpace):
        task_runner.run_task(case.delivery(), case.run_id, config, _executing(calls))
    assert calls == []
    # When the operator frees space and restarts the run from the failed stage.
    volumes.set_free(artifact=9000 * MIB)
    run_in_worker_loop(case.runs.restart_run(case.run_id, "SCRIPT_GENERATING"))
    task_runner.run_task(_delivery("retry-task"), case.run_id, config, _executing(calls))
    # Then the retry executes and the run advances: the refusal dropped nothing.
    assert calls == ["retry-task"]
    saved = run_in_worker_loop(case.runs.storage.get_run(case.run_id))
    assert (saved["current_stage"], saved["status"]) == ("SCRIPT_REVIEW", "paused")


def test_refusal_creates_no_files_and_leaves_existing_artifacts_untouched(
    runner_case: RunnerCase, volumes: Volumes, watermarks: None,
) -> None:
    # Given a published video under the artifact root.
    case = runner_case
    (volumes.artifacts / "run_7").mkdir()
    (volumes.artifacts / "run_7" / "output.mp4").write_bytes(b"published" * 1000)
    before = (_snapshot(volumes.artifacts), _snapshot(volumes.scratch))
    volumes.set_free(artifact=1 * MIB, scratch=1 * MIB)
    # When a guarded task is refused.
    with pytest.raises(FilesystemLowSpace):
        task_runner.run_task(case.delivery(), case.run_id, _guarded(case.config), _executing([]))
    # Then neither filesystem changed by a byte, a timestamp, or an entry.
    assert (_snapshot(volumes.artifacts), _snapshot(volumes.scratch)) == before


def test_refusal_on_a_retried_delivery_keeps_what_an_earlier_provider_attempt_authorized(
    runner_case: RunnerCase, quota_case: UsageService, volumes: Volumes, watermarks: None,
) -> None:
    # Given a first delivery that authorized and started a provider attempt, lost its answer, and
    # was left reclaimable for Celery's retry (its exposure is therefore still owed), and a cost
    # budget that only has room for the two reservations.
    case = runner_case
    config = _guarded(case.config)
    run_in_worker_loop(quota_case.set_quota(1, monthly_llm_calls=2, monthly_cost_usd=0.2))

    async def first_delivery(ctx: task_runner.TaskContext) -> task_runner.TaskResult:
        assert await quota_case.authorize_owned_attempt(
            ctx.reservation_owner_id or "", "first-provider-attempt", "0.01",
        )
        ctx.mark_provider_started()
        raise ProviderTimeoutError("response lost")

    with pytest.raises(ProviderTimeoutError):
        task_runner.run_task(case.delivery(retries=0), case.run_id, config, first_delivery)
    # When the retried delivery finds the artifact filesystem under its floor.
    volumes.set_free(artifact=100 * MIB)
    with pytest.raises(FilesystemLowSpace):
        task_runner.run_task(case.delivery(retries=1), case.run_id, config, _executing([]))
    # Then the refusal does not pretend nothing was ever started: the earlier attempt's exposure is
    # kept, so no replacement is admitted on the budget it still holds.
    assert not run_in_worker_loop(quota_case.reserve_owned(1, "llm", "replacement"))


def test_a_low_space_error_raised_during_execution_keeps_that_deliverys_own_billing_outcome(
    runner_case: RunnerCase, quota_case: UsageService,
) -> None:
    # Given a task that already started a provider attempt and then raises the refusal error itself
    # (not the admission check, which runs before execution), and a budget with no spare room.
    case = runner_case
    run_in_worker_loop(quota_case.set_quota(1, monthly_llm_calls=2, monthly_cost_usd=0.2))

    async def execute(ctx: task_runner.TaskContext) -> task_runner.TaskResult:
        ctx.mark_provider_started()
        raise FilesystemLowSpace(incoming_bytes=0, free_bytes=1, filesystem="scratch", required_bytes=2)

    # When the delivery fails.
    with pytest.raises(FilesystemLowSpace):
        task_runner.run_task(case.delivery(), case.run_id, case.config, execute)
    # Then the history-based finalization is reserved for refusals before execution: this delivery's
    # provider outcome is unknown, so its whole estimate stays charged like any other failure.
    assert not run_in_worker_loop(quota_case.reserve_owned(1, "llm", "replacement"))


def test_a_refused_scene_image_task_leaves_a_run_that_is_in_review_where_it_is(
    runner_case: RunnerCase, volumes: Volumes, watermarks: None,
) -> None:
    # Given the scene-image task's own stage policy and a run the user is reviewing.
    case = runner_case
    config = task_runner.TaskRunnerConfig(
        task_name="generate_scene_image",
        allowed_stages=frozenset({
            RunStage.VISUAL_PLAN_REVIEW, RunStage.VISUAL_ASSET_GENERATING, RunStage.VISUAL_ASSET_REVIEW,
        }),
        safe_stages=SAFE_SUCCESS_STAGES,
        safe_failure_stages=SAFE_FAILURE_STAGES,
        success_stage=None,
        requires_free_space=True,
    )
    run_in_worker_loop(case.runs.storage.update_run(
        case.run_id, {"current_stage": "VISUAL_ASSET_REVIEW", "status": "paused"},
    ))
    volumes.set_free(artifact=100 * MIB)
    # When regenerating one scene is refused for low storage.
    with pytest.raises(FilesystemLowSpace):
        task_runner.run_task(case.delivery(), case.run_id, config, _executing([]))
    # Then the failure is recorded on the task only: reviewing the other scenes is not interrupted.
    tracked = run_in_worker_loop(case.tracking.list_run_tasks(case.run_id))[0]
    assert (tracked.status, tracked.error_code) == ("failed", "ARTIFACT_STORAGE_LOW")
    saved = run_in_worker_loop(case.runs.storage.get_run(case.run_id))
    assert (saved["current_stage"], saved["status"]) == ("VISUAL_ASSET_REVIEW", "paused")


def test_low_storage_refuses_exactly_the_tasks_that_write_media(
    generation_case: GenerationCase, volumes: Volumes, watermarks: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given each real generation task, and both filesystems under their floors.
    case = generation_case
    volumes.set_free(artifact=100 * MIB, scratch=100 * MIB)
    before = (_snapshot(volumes.artifacts), _snapshot(volumes.scratch))
    retry = Mock(wraps=case.task.retry)
    monkeypatch.setattr(case.task, "retry", retry)
    case.task.push_request(id="low-space-task", args=case.args, kwargs={}, retries=0)
    try:
        # When the real task is delivered.
        if case.task.name in GUARDED_TASKS:
            with pytest.raises(FilesystemLowSpace):
                case.task.run(*case.args)
        else:
            case.task.run(*case.args)
    finally:
        case.task.pop_request()
    tracked = run_in_worker_loop(case.runner.tracking.list_run_tasks(case.runner.run_id))[0]
    saved = run_in_worker_loop(case.runner.runs.storage.get_run(case.runner.run_id))
    if case.task.name in GUARDED_TASKS:
        # Then a media task did nothing: no provider call, no usage, no file, a retryable failure.
        assert case.provider.generate.await_count + case.provider.transcribe.await_count == 0
        case.usage.assert_not_awaited()
        retry.assert_not_called()  # a full disk is not hammered by automatic retries
        assert (_snapshot(volumes.artifacts), _snapshot(volumes.scratch)) == before
        assert (tracked.status, tracked.error_code) == ("failed", "ARTIFACT_STORAGE_LOW")
        assert (saved["current_stage"], saved["status"]) == ("FAILED", "failed")
    else:
        # Then a text-only task is not held back by a low disk.
        assert tracked.status == "success"
        assert saved["current_stage"] != "FAILED"


def _calls_named(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) == name or getattr(node.func, "attr", None) == name)
    ]


def test_every_task_decides_whether_it_needs_free_space() -> None:
    # Given every module under tasks/ that starts a task through run_task.
    tasks_dir = Path(__file__).resolve().parents[1] / "tasks"
    declared: list[tuple[str, bool]] = []
    for module in sorted(tasks_dir.rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        invocations = _calls_named(tree, "run_task")
        configs = _calls_named(tree, "TaskRunnerConfig")
        # Each run_task invocation must be paired with a TaskRunnerConfig written out in the same
        # module, so a task cannot take its configuration from a factory this inventory cannot read.
        assert len(invocations) == len(configs), (
            f"{module.name}: {len(invocations)} run_task calls but {len(configs)} literal TaskRunnerConfig"
        )
        for node in configs:
            keywords = {keyword.arg: keyword.value for keyword in node.keywords}
            name = keywords["task_name"]
            assert isinstance(name, ast.Constant) and isinstance(name.value, str), module.name
            flag = keywords.get("requires_free_space")
            declared.append((name.value, isinstance(flag, ast.Constant) and flag.value is True))
    # When the two reviewed lists are compared with what the code declares.
    names = [name for name, _ in declared]
    expected = {**{task: True for task in GUARDED_TASKS}, **{task: False for task in EXEMPT_TASKS}}
    # Then task names are unique, and a new task cannot ship without someone deciding (and listing)
    # whether it writes media.
    assert len(names) == len(set(names))
    assert dict(declared) == expected
