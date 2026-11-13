import logging
from datetime import datetime, timezone
from pathlib import Path

import anyio
import pytest
from celery.exceptions import Ignore, SoftTimeLimitExceeded
from creator_service import artifact_storage_integration as integration
from creator_service.object_storage import LocalStorageBackend, StorageResult
from creator_service.run_service import InMemoryRunStorage, RunService
from tasks.scene_batch_lifecycle import SceneBatch
from tasks.task_runner import TaskContext
from worker_loop import run_in_worker_loop

from .scene_cancellation_support import SceneCase, scene_case as scene_case
from .terminal_postgres_support import terminal_pool as terminal_pool
from .terminal_validation_support import runner_case as runner_case
from .test_scene_upload_cancellation import REAL_UPLOAD, upload_backend as upload_backend


class StorageSDKError(Exception):
    """SDK deletion contract surrogate: Exception, not OSError, carrying unsafe text."""


def test_sdk_delete_failure_preserves_cancelled_result(
    scene_case: SceneCase, upload_backend: LocalStorageBackend,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    # Given an accepted prior asset and a new upload whose cleanup SDK fails.
    case = scene_case
    case.provider.released.set()
    prior_key = f"{case.runner.run_id}/scenes/prior.png"
    upload_backend.upload(prior_key, b"accepted")
    prior = run_in_worker_loop(case.assets.create_asset(
        case.runner.run_id, "scene-0", "prior.png", storage_key=prior_key,
    ))
    uploaded: list[StorageResult] = []
    deleted: list[str] = []
    original = SceneBatch.checkpoint

    def upload(run_id: int, path: str, content_type: str) -> StorageResult:
        result = REAL_UPLOAD(run_id, path, content_type)
        uploaded.append(result)
        return result

    async def checkpoint(batch: SceneBatch) -> None:
        if uploaded:
            await case.cancel()
        await original(batch)

    def delete(key: str) -> bool:
        deleted.append(key)
        raise StorageSDKError("https://storage.invalid/?token=secret-cleanup-token")

    monkeypatch.setattr(integration, "store_artifact_file", upload)
    monkeypatch.setattr(SceneBatch, "checkpoint", checkpoint)
    monkeypatch.setattr(upload_backend, "delete", delete)
    caplog.set_level(logging.WARNING)
    # When cancellation is known before the failing delete operation.
    result = case.invoke()
    # Then cleanup failure cannot become scene failure or leak SDK response text.
    assert (result["status"], result["succeeded"], result["failed"]) == ("cancelled", 0, 0)
    assert result["scene_results"] == []
    assert deleted == [uploaded[0].key]
    assert prior_key not in deleted
    assert upload_backend.download_bytes(prior_key) == b"accepted"
    assert not case.provider.paths[0].exists()
    assets = run_in_worker_loop(case.assets.storage.list_assets_by_run(case.runner.run_id))
    assert [asset["id"] for asset in assets] == [prior.id]
    warnings = [record for record in caplog.records if record.name == "tasks.scene_batch_lifecycle"]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert warnings[0].getMessage() == "Scene upload cleanup failed"
    assert warnings[0].exc_info is None
    assert "secret-cleanup-token" not in caplog.text


@pytest.mark.parametrize("signal_type", [SoftTimeLimitExceeded, KeyboardInterrupt, SystemExit])
def test_cleanup_propagates_process_control(
    upload_backend: LocalStorageBackend, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, signal_type: type[BaseException],
) -> None:
    # Given known cancellation followed by a process-control signal in delete.
    path = tmp_path / "1" / "scenes" / "owned.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"generated")
    batch = SceneBatch(
        TaskContext(1, "task", {}, 1, 1, datetime.now(timezone.utc)),
        RunService(InMemoryRunStorage()),
    )
    batch.local_outputs.add(path)
    checkpoints = 0

    async def checkpoint(self: SceneBatch) -> None:
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 2:
            raise Ignore()

    signal = signal_type()

    def delete(key: str) -> bool:
        raise signal

    monkeypatch.setattr(SceneBatch, "checkpoint", checkpoint)
    monkeypatch.setattr(upload_backend, "delete", delete)
    # When upload cleanup receives process control rather than an SDK failure.
    with pytest.raises(signal_type) as caught:
        anyio.run(batch.upload, str(path))
    # Then the original signal propagates, including Exception-based soft timeouts.
    assert caught.value is signal
