import asyncio
import contextlib
from concurrent.futures import ThreadPoolExecutor
import threading
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
from creator_service.artifact_download_service import (
    ArtifactDownloadService,
    InMemoryArtifactDownloadStorage,
)
from creator_service.rejected_scene_cleanup import SCENE_UPLOAD_LATE_WATCH_MARKER

from project_deletion_support import DeletionDatabase

pytest_plugins = ("project_deletion_support",)


class BlockingDeleteBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.first_call_started = threading.Event()
        self.release_first_call = threading.Event()
        self._lock = threading.Lock()
        self.transaction_backend_pid = 0

    def delete(self, key: str) -> bool:
        with self._lock:
            self.calls.append(key)
            call_number = len(self.calls)
        if call_number == 1:
            self.first_call_started.set()
            if not self.release_first_call.wait(timeout=10):
                raise TimeoutError("test did not release blocking delete")
        return True


async def _wait_for_active_transaction(
    pool: asyncpg.Pool,
    backend_pid: int,
    *,
    timeout: float = 10,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    last_observation: asyncpg.Record | None = None
    while asyncio.get_running_loop().time() < deadline:
        last_observation = await pool.fetchrow(
            "SELECT state, xact_start IS NOT NULL AS in_transaction, "
            "pg_blocking_pids(pid) AS blocking_pids "
            "FROM pg_stat_activity WHERE pid = $1",
            backend_pid,
        )
        if last_observation is not None and last_observation["in_transaction"]:
            return
        await asyncio.sleep(0.01)
    pytest.fail(
        "storage deletion began without the claimed row transaction remaining active; "
        f"backend_pid={backend_pid}, pg_stat_activity={dict(last_observation) if last_observation else None}",
    )


@pytest.mark.asyncio
async def test_retry_workers_hold_each_row_lock_through_storage_deletion(
    deletion_database: DeletionDatabase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given one pending artifact and a backend that blocks the first retry worker.
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    local_path = tmp_path / "fresh.png"
    local_path.write_bytes(b"fresh")
    await deletion_database.pool.execute(
        "INSERT INTO creator_runs (id, project_id, workspace_id) VALUES (1, 1, 1)",
    )
    await deletion_database.pool.execute("""
        INSERT INTO creator_artifacts (
            run_id, project_id, artifact_type, scene_id, file_path, storage_key,
            metadata_json, delete_requested_at
        ) VALUES (1, 1, 'visual_asset', 'fresh', $1, '1/scenes/fresh.png',
                  'scene_upload_cleanup_intent', NOW());
    """, str(local_path))
    await deletion_database.pool.execute(
        "UPDATE creator_artifacts SET created_at = NOW() - INTERVAL '6 minutes' WHERE run_id = 1",
    )
    backend = BlockingDeleteBackend()

    @contextlib.asynccontextmanager
    async def isolated_transaction() -> AsyncIterator[asyncpg.Connection]:
        connection = await asyncpg.connect(
            deletion_database.url,
            server_settings={"search_path": deletion_database.schema},
        )
        try:
            async with connection.transaction():
                backend.transaction_backend_pid = connection.get_server_pid()
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
        assert backend.transaction_backend_pid > 0
        await _wait_for_active_transaction(
            deletion_database.pool,
            backend.transaction_backend_pid,
        )
        second = executor.submit(retry_once)
        try:
            second_result = second.result(timeout=10)
            calls_while_first_is_blocked = list(backend.calls)
        finally:
            backend.release_first_call.set()
        first_result = first.result(timeout=10)

    # Then SKIP LOCKED makes the contender return without invoking deletion.
    assert second_result == 0
    assert calls_while_first_is_blocked == ["1/scenes/fresh.png"]
    assert first_result == 1

    assert backend.calls == ["1/scenes/fresh.png"]
    assert not local_path.exists()
    # An unresolved upload intent outlives its first reclaim as a late-write watch.
    assert await deletion_database.pool.fetchval(
        "SELECT delete_error FROM creator_artifacts WHERE run_id=1",
    ) == SCENE_UPLOAD_LATE_WATCH_MARKER
