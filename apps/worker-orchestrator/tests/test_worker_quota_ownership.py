import pytest
from pathlib import Path
from unittest.mock import AsyncMock
from billiard.exceptions import SoftTimeLimitExceeded
from creator_provider.exceptions import ProviderError, ProviderTimeoutError
from creator_service.media_integrity import MediaIntegrityError
from creator_service.postgres_task_tracking_storage import PostgresTaskTrackingStorage
from creator_service.postgres_usage_storage import PostgresUsageStorage
from creator_service.usage_service import InMemoryUsageStorage, UsageService
from tasks import task_runner
from tasks.task_execution import TaskInputError
from worker_loop import run_in_worker_loop

from .terminal_validation_support import RunnerCase, runner_case as runner_case  # noqa: F401
from .terminal_postgres_support import terminal_pool as terminal_pool  # noqa: F401
from .provider_boundary_support import GenerationCase, generation_case as generation_case  # noqa: F401


@pytest.fixture
def quota_case(runner_case: RunnerCase, monkeypatch: pytest.MonkeyPatch) -> UsageService:
    # The terminal finalizer cancels quota in the task-row backend, so reservations must live there.
    postgres = isinstance(runner_case.tracking.storage, PostgresTaskTrackingStorage)
    service = UsageService(PostgresUsageStorage() if postgres else InMemoryUsageStorage())
    run_in_worker_loop(service.set_quota(1, monthly_llm_calls=2))
    assert run_in_worker_loop(service.reserve_owned(1, "llm", "validation-task"))
    assert run_in_worker_loop(service.reserve_owned(1, "llm", "other-task"))
    monkeypatch.setattr("creator_service.usage_service.usage_service", service)
    return service


def test_worker_context_resolves_only_actual_reservation_owner(
    runner_case: RunnerCase, quota_case: UsageService,
) -> None:
    # Given a claimed delivery with a matching owned reservation.
    seen: list[str | None] = []

    async def execute(ctx: task_runner.TaskContext) -> task_runner.TaskResult:
        seen.append(ctx.reservation_owner_id)
        return task_runner.TaskResult()

    # When the worker executes the delivery.
    task_runner.run_task(runner_case.delivery(), runner_case.run_id, runner_case.config, execute)

    # Then accounting receives the real owner, not an inferred idempotency key.
    assert seen == ["validation-task"]
    assert not run_in_worker_loop(quota_case.reserve_owned(1, "llm", "new-task"))


@pytest.mark.parametrize("failure", [SoftTimeLimitExceeded(), RuntimeError("failure"),
    ProviderError("Provider failed video render"), MediaIntegrityError("duration completeness", expected=5.429)])
def test_terminal_failure_cancels_only_claimed_owner(
    runner_case: RunnerCase, quota_case: UsageService, failure: Exception,
) -> None:
    # Given two independent reservations with one claimed delivery.
    async def execute(_ctx: task_runner.TaskContext) -> task_runner.TaskResult:
        raise failure

    # When the claimed delivery fails terminally.
    with pytest.raises(type(failure)):
        task_runner.run_task(runner_case.delivery(), runner_case.run_id, runner_case.config, execute)

    # Then only its capacity is returned, leaving the other owner protected.
    assert not run_in_worker_loop(quota_case.cancel_owned("validation-task"))
    assert run_in_worker_loop(quota_case.reserve_owned(1, "llm", "new-task"))
    assert not run_in_worker_loop(quota_case.reserve_owned(1, "llm", "overflow"))


@pytest.mark.parametrize("failure", [SoftTimeLimitExceeded(), RuntimeError("failure")])
def test_terminal_failure_uses_atomic_claim_run_and_quota_finalizer(
    runner_case: RunnerCase,
    quota_case: UsageService,
    failure: Exception,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a claimed delivery whose terminal cleanup can be observed.
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(runner_case.tracking, "fail_claimed_and_cancel_quota", finalize)

    async def execute(_ctx: task_runner.TaskContext) -> task_runner.TaskResult:
        raise failure

    # When the delivery reaches either terminal exception path.
    with pytest.raises(type(failure)):
        task_runner.run_task(runner_case.delivery(), runner_case.run_id, runner_case.config, execute)

    # Then ownership, run failure, and quota cancellation share one storage operation.
    finalize.assert_awaited_once()
    assert finalize.await_args.args[2] is quota_case.storage
    assert finalize.await_args.args[5] is runner_case.runs.storage
    assert finalize.await_args.args[6] == runner_case.run_id
    assert finalize.await_args.args[7] == frozenset({"SCRIPT_GENERATING"})


def test_lost_timeout_claim_does_not_settle_successor_reservation(
    runner_case: RunnerCase,
    quota_case: UsageService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given atomic timeout finalization rejects a stale delivery claim.
    finalize = AsyncMock(return_value=False)
    settle = AsyncMock()
    monkeypatch.setattr(runner_case.tracking, "fail_claimed_and_cancel_quota", finalize)
    monkeypatch.setattr(quota_case, "settle_owned", settle)

    async def execute(ctx: task_runner.TaskContext) -> task_runner.TaskResult:
        ctx.billing_outcome = task_runner.BillingOutcome.ACCOUNTED
        raise SoftTimeLimitExceeded()

    # When the stale delivery handles its timeout.
    with pytest.raises(SoftTimeLimitExceeded):
        task_runner.run_task(runner_case.delivery(), runner_case.run_id, runner_case.config, execute)

    # Then it cannot settle quota now owned by a successor.
    settle.assert_not_awaited()


@pytest.mark.parametrize("failure", [SoftTimeLimitExceeded(), RuntimeError("failure"),
    ProviderError("Provider failed video render"), MediaIntegrityError("duration completeness", expected=5.429)])
def test_replacement_is_not_admitted_after_unknown_provider_outcome(
    runner_case: RunnerCase,
    quota_case: UsageService,
    failure: Exception,
) -> None:
    # Given the claimed delivery has crossed the provider boundary.
    run_in_worker_loop(quota_case.set_quota(1, monthly_llm_calls=2, monthly_cost_usd=0.2))

    async def execute(ctx: task_runner.TaskContext) -> task_runner.TaskResult:
        ctx.mark_provider_started()
        raise failure

    # When the delivery fails terminally with an unknown provider outcome.
    with pytest.raises(type(failure)):
        task_runner.run_task(
            runner_case.delivery(), runner_case.run_id, runner_case.config, execute,
        )

    # Then its authorized exposure remains charged and cannot admit a replacement.
    assert not run_in_worker_loop(quota_case.reserve_owned(1, "llm", "replacement"))


def test_retryable_failure_keeps_reservation(
    runner_case: RunnerCase, quota_case: UsageService,
) -> None:
    # Given a claimed task with retries remaining and another reserved owner.
    async def execute(_ctx: task_runner.TaskContext) -> task_runner.TaskResult:
        raise ProviderTimeoutError("transient")

    # When a retryable failure escapes execution.
    with pytest.raises(ProviderTimeoutError):
        task_runner.run_task(runner_case.delivery(), runner_case.run_id, runner_case.config, execute)

    # Then this task keeps its reservation for the next delivery attempt.
    assert not run_in_worker_loop(quota_case.reserve_owned(1, "llm", "new-task"))
    assert run_in_worker_loop(quota_case.cancel_owned("validation-task"))


def test_lost_claim_cannot_cancel_successor_reservation(
    runner_case: RunnerCase, quota_case: UsageService,
) -> None:
    # Given a new delivery has reclaimed the original task during execution.
    async def execute(_ctx: task_runner.TaskContext) -> task_runner.TaskResult:
        row = await runner_case.tracking.storage.get_by_celery_id("validation-task")
        assert row is not None
        token = row["claim_token"]
        assert isinstance(token, str)
        assert await runner_case.tracking.mark_failed_if_claimed("validation-task", token, "INTERNAL", "retry")
        assert await runner_case.tracking.record_task_start(
            runner_case.run_id, "generate_script", "validation-task", claim_token="successor",
        )
        raise RuntimeError("stale delivery failed")

    # When the stale delivery handles its exception.
    with pytest.raises(RuntimeError):
        task_runner.run_task(runner_case.delivery(), runner_case.run_id, runner_case.config, execute)

    # Then its failed claim cannot release the reservation for its successor.
    assert not run_in_worker_loop(quota_case.reserve_owned(1, "llm", "replacement"))
    assert run_in_worker_loop(quota_case.cancel_owned("validation-task"))


def test_cancelled_owner_rejects_worker_before_provider_execution(
    runner_case: RunnerCase, quota_case: UsageService,
) -> None:
    # Given a task whose reservation was cancelled before worker delivery.
    assert run_in_worker_loop(quota_case.cancel_owned("validation-task"))
    executed: list[str] = []

    async def execute(_ctx: task_runner.TaskContext) -> task_runner.TaskResult:
        executed.append("provider")
        return task_runner.TaskResult()

    # When the worker receives that task ID.
    with pytest.raises(ValueError, match="Cancelled reservation owner"):
        task_runner.run_task(runner_case.delivery(), runner_case.run_id, runner_case.config, execute)

    # Then no provider work is performed and the other reservation stays reserved.
    assert executed == []
    assert run_in_worker_loop(quota_case.reserve_owned(1, "llm", "replacement"))
    assert not run_in_worker_loop(quota_case.reserve_owned(1, "llm", "overflow"))


def test_stage_guard_releases_not_started_reservation(
    runner_case: RunnerCase, quota_case: UsageService,
) -> None:
    # Given an owned delivery whose run no longer accepts its stage.
    run_in_worker_loop(runner_case.runs.storage.update_run(
        runner_case.run_id, {"current_stage": "SCRIPT_REVIEW"},
    ))

    # When the stage guard rejects the delivery before provider execution.
    with pytest.raises(task_runner.StageGuardError):
        task_runner.run_task(
            runner_case.delivery(), runner_case.run_id, runner_case.config,
            lambda _ctx: None,
        )

    # Then its definitely unbilled reservation is released.
    assert run_in_worker_loop(quota_case.reserve_owned(1, "llm", "replacement"))


def test_input_failure_releases_not_started_reservation(
    runner_case: RunnerCase, quota_case: UsageService,
) -> None:
    # Given an owned delivery whose task input cannot be resolved.
    async def reject_input(_ctx: task_runner.TaskContext) -> task_runner.TaskResult:
        raise TaskInputError("missing task input")

    # When task validation fails inside execute before a provider starts.
    with pytest.raises(TaskInputError):
        task_runner.run_task(
            runner_case.delivery(), runner_case.run_id, runner_case.config, reject_input,
        )

    # Then its definitely unbilled reservation is released.
    assert run_in_worker_loop(quota_case.reserve_owned(1, "llm", "replacement"))


def test_generation_records_verified_owner_separately_from_usage_key(
    generation_case: GenerationCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a real owner record for the current delivery and an offline provider.
    case = generation_case
    service = UsageService(InMemoryUsageStorage())
    monkeypatch.setattr("creator_service.usage_service.usage_service", service)
    operation = {
        "generate_script": "llm",
        "generate_visual_plan": "llm",
        "generate_audio": "tts",
        "generate_paragraph_audio": "tts",
        "generate_subtitles": "stt",
        "generate_paragraph_subtitles": "stt",
        "generate_scene_image": "image_gen",
    }[case.module.__name__.rsplit(".", 1)[-1]]
    assert run_in_worker_loop(
        service.reserve_owned(1, operation, "validation-task", estimated_cost_usd=20)
    )
    case.provider.generate.return_value = "A useful generated script."

    # When the worker reaches its provider accounting call.
    case.task.push_request(id="validation-task", args=case.args, kwargs={}, retries=0)
    try:
        case.task.run(*case.args)
    finally:
        case.task.pop_request()

    # Then it forwards the verified reservation owner independently of the event key.
    case.usage.assert_awaited()
    assert case.usage.await_args.kwargs["reservation_owner_id"] == "validation-task"


def test_owned_generation_does_not_succeed_when_usage_fails(
    generation_case: GenerationCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a verified reservation and a provider that succeeds before accounting fails.
    case = generation_case
    service = UsageService(InMemoryUsageStorage())
    monkeypatch.setattr("creator_service.usage_service.usage_service", service)
    operation = {
        "generate_script": "llm", "generate_visual_plan": "llm",
        "generate_audio": "tts", "generate_paragraph_audio": "tts",
        "generate_subtitles": "stt", "generate_paragraph_subtitles": "stt",
        "generate_scene_image": "image_gen",
    }[case.module.__name__.rsplit(".", 1)[-1]]
    assert run_in_worker_loop(
        service.reserve_owned(1, operation, "validation-task", estimated_cost_usd=20)
    )
    case.provider.generate.return_value = "A useful generated script."
    case.usage.side_effect = OSError("usage database unavailable")

    # When its provider usage event cannot be persisted.
    case.task.push_request(id="validation-task", args=case.args, kwargs={}, retries=0)
    try:
        with pytest.raises(OSError, match="usage database unavailable"):
            case.task.run(*case.args)
    finally:
        case.task.pop_request()

    # Then the worker has not silently reported a successful delivery.
    case.usage.assert_awaited()


def test_audio_generation_authorizes_attempt_before_provider(
    generation_case: GenerationCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an owned audio delivery and an authorization boundary.
    case = generation_case
    name = case.module.__name__.rsplit(".", 1)[-1]
    if name not in {"generate_audio", "generate_paragraph_audio"}:
        pytest.skip("audio generation fixture only")
    authorize = AsyncMock(return_value=True)
    monkeypatch.setattr(case.module, "authorize_owned_provider_attempt", authorize)
    case.provider.generate.return_value = "generated"

    # When the provider is invoked, then approved capacity is consumed first.
    case.task.push_request(id="validation-task", args=case.args, kwargs={}, retries=0)
    try:
        case.task.run(*case.args)
    finally:
        case.task.pop_request()
    authorize.assert_awaited()
    assert authorize.await_count >= case.provider.generate.await_count


def test_each_owned_generation_authorizes_delivery_before_provider(
    generation_case: GenerationCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an owned delivery at a billable provider boundary.
    case = generation_case
    authorize = AsyncMock(return_value=True)
    monkeypatch.setattr(case.module, "authorize_owned_provider_attempt", authorize)
    case.provider.generate.return_value = "generated"

    # When the task invokes its provider.
    case.task.push_request(id="validation-task", args=case.args, kwargs={}, retries=2)
    try:
        case.task.run(*case.args)
    finally:
        case.task.pop_request()

    # Then authorization precedes every invocation and accounting links that attempt.
    authorize.assert_awaited()
    assert authorize.await_count >= case.provider.generate.await_count + case.provider.transcribe.await_count
    assert case.usage.await_args.kwargs["reservation_attempt_id"]


def test_paragraph_audio_timeout_cleans_only_its_delivery(
    generation_case: GenerationCase,
) -> None:
    if generation_case.module.__name__.rsplit(".", 1)[-1] != "generate_paragraph_audio":
        pytest.skip("paragraph audio fixture only")
    # Given another delivery's completed output in the same run.
    case = generation_case
    paths: list[Path] = []

    async def interrupt(*_args: object, **kwargs: object) -> None:
        params = kwargs["params"]
        assert isinstance(params, dict)
        path = Path(params["output_path"])
        paths.append(path)
        path.write_bytes(b"partial")
        raise SoftTimeLimitExceeded()

    case.provider.generate.side_effect = interrupt
    # When the paragraph delivery times out after creating a partial file.
    case.task.push_request(id="paragraph-delivery", args=case.args, kwargs={}, retries=0)
    try:
        with pytest.raises(SoftTimeLimitExceeded):
            case.task.run(*case.args)
    finally:
        case.task.pop_request()

    # Then the partial output belongs to this delivery and has been removed.
    assert len(paths) == 1
    assert paths[0].parent.name == "paragraph-delivery"
    assert not paths[0].exists()
