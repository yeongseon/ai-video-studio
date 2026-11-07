import asyncio
import contextlib
from concurrent.futures import ThreadPoolExecutor
import threading
from collections.abc import AsyncIterator

import asyncpg
import pytest
from creator_service.artifact_download_service import (
    ArtifactDownloadService,
    InMemoryArtifactDownloadStorage,
)

from project_deletion_support import DeletionDatabase

pytest_plugins = ("project_deletion_support",)


class BlockingDeleteBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.first_call_started = threading.Event()
        self.release_first_call = threading.Event()
        self._lock = threading.Lock()

    def delete(self, key: str) -> bool:
        with self._lock:
            self.calls.append(key)
            call_number = len(self.calls)
        if call_number == 1:
            self.first_call_started.set()
            if not self.release_first_call.wait(timeout=10):
                raise TimeoutError("test did not release blocking delete")
        return True


@pytest.mark.asyncio
async def test_retry_workers_hold_each_row_lock_through_storage_deletion(
    deletion_database: DeletionDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given one pending artifact and a backend that blocks the first retry worker.
    await deletion_database.pool.execute("""
        INSERT INTO creator_runs (id, project_id, workspace_id) VALUES (1, 1, 1);
        INSERT INTO creator_artifacts (
            run_id, project_id, artifact_type, file_path, storage_key, delete_requested_at
        ) VALUES (1, 1, 'video', '1/video.mp4', '1/video.mp4', NOW());
    """)
    backend = BlockingDeleteBackend()

    @contextlib.asynccontextmanager
    async def isolated_transaction() -> AsyncIterator[asyncpg.Connection]:
        connection = await asyncpg.connect(
            deletion_database.url,
            server_settings={"search_path": deletion_database.schema},
        )
        try:
            async with connection.transaction():
                yield connection
        finally:
            await connection.close()

    monkeypatch.setattr(
        "creator_service.artifact_download_service.transaction",
        isolated_transaction,
    )
    monkeypatch.setattr(
        "creator_service.artifact_download_service.get_storage_backend",
        lambda: backend,
    )

    def retry_once() -> int:
        service = ArtifactDownloadService(InMemoryArtifactDownloadStorage())
        return asyncio.run(service.retry_failed_deletions())

    # When a second worker retries while the first worker is inside storage deletion.
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(retry_once)
        assert backend.first_call_started.wait(timeout=10)
        second = executor.submit(retry_once)
        try:
            second_result = second.result(timeout=10)
            calls_while_first_is_blocked = list(backend.calls)
        finally:
            backend.release_first_call.set()
        first_result = first.result(timeout=10)

    # Then SKIP LOCKED makes the contender return without invoking deletion.
    assert second_result == 0
    assert calls_while_first_is_blocked == ["1/video.mp4"]
    assert first_result == 1

    assert backend.calls == ["1/video.mp4"]
    assert await deletion_database.pool.fetchval(
        "SELECT count(*) FROM creator_artifacts WHERE run_id=1",
    ) == 0
