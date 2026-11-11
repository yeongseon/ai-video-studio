"""Retry deletion safety at the transaction and filesystem boundaries."""

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from creator_service.artifact_deletion_retry import retry_failed_deletions
from creator_service.object_storage import LocalStorageBackend
from creator_service.rejected_scene_cleanup import SCENE_UPLOAD_CLEANUP_INTENT_MARKER


class RecordingConnection:
    """Mutable fake records committed state independently of active transactions."""

    def __init__(self, path: Path) -> None:
        self.row = {
            "id": 956, "run_id": 956, "storage_key": "956/scenes/object.png",
            "storage_provider": "local", "file_path": str(path),
            "metadata_json": SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
            "delete_retry_count": 0, "delete_failed_at": None,
        }
        self.active = 0
        self.entries = 0
        self.committed_fence = 0
        self.deleted = False
        self.failures = 0
        self.fence_transaction = 0

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator["RecordingConnection"]:
        self.entries += 1
        self.active = self.entries
        previous = self.row.copy()
        try:
            yield self
        except BaseException:
            self.row = previous
            raise
        else:
            if self.fence_transaction == self.active:
                self.committed_fence = self.active
        finally:
            self.active = 0

    async def fetchrow(self, query: str, *args):
        if self.deleted:
            return None
        if "ANY(" in query and self.row["id"] in args[1]:
            return None
        return self.row.copy()

    async def fetchval(self, query: str, *args) -> int | None:
        return None if "creator_scene_assets" in query else 0

    async def execute(self, query: str, *args) -> str:
        if "clock_timestamp()" in query:
            self.row["delete_failed_at"] = "fenced"
            self.fence_transaction = self.active
        elif "delete_retry_count = COALESCE" in query:
            self.failures += 1
            self.row["delete_retry_count"] = self.failures
        elif "DELETE FROM" in query:
            self.deleted = True
        return "OK"


class RecordingBackend(LocalStorageBackend):
    def __init__(self, connection: RecordingConnection, root: Path) -> None:
        super().__init__(str(root))
        self.connection = connection
        self.observations: list[tuple[int, int]] = []
        self.failure = False

    def delete(self, key: str) -> bool:
        self.observations.append((self.connection.committed_fence, self.connection.active))
        if self.failure:
            raise TimeoutError("ambiguous delete")
        return True


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "artifacts"
    root.mkdir()
    monkeypatch.setenv("ARTIFACT_ROOT", str(root))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    return root


async def test_retry_uses_recorded_local_provider(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given local bytes but a newly configured cloud backend.
    backend = LocalStorageBackend(str(root))
    backend.upload("956/scenes/object.png", b"object")
    connection = RecordingConnection(root / "source.png")
    connection.row["metadata_json"] = None
    configured = RecordingBackend(connection, root)
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    # When retry processes the row.
    result = await retry_failed_deletions(5, connection.transaction, lambda: configured)
    # Then only the persisted provider loses bytes.
    assert not backend.exists("956/scenes/object.png")
    assert configured.observations == []
    assert result == 1


@pytest.mark.parametrize("kind", ["outside", "traversal", "intermediate_symlink"])
async def test_retry_refuses_unsafe_source(root: Path, kind: str) -> None:
    # Given a recorded source that escapes the configured root.
    outside = root.parent / "outside"
    outside.mkdir()
    victim = outside / "victim.png"
    victim.write_bytes(b"keep")
    (root / "956").mkdir()
    path = victim
    if kind == "traversal":
        (root / "956/scenes").mkdir()
        path = root / "956/scenes/../../../outside/victim.png"
    if kind == "intermediate_symlink":
        (root / "956/scenes").symlink_to(outside, target_is_directory=True)
        path = root / "956/scenes/victim.png"
    connection = RecordingConnection(path)
    backend = RecordingBackend(connection, root)
    # When retry deletes the object then attempts source cleanup.
    result = await retry_failed_deletions(5, connection.transaction, lambda: backend)
    # Then the victim survives and the intent remains retryable.
    assert victim.exists(), "outside victim was unlinked"
    assert result == 0
    assert connection.failures == 1
    assert not connection.deleted


@pytest.mark.parametrize("kind", ["final_symlink", "missing_file", "missing_parent"])
async def test_retry_safe_source_cleanup_is_idempotent(root: Path, kind: str) -> None:
    # Given a safe missing source or a final symlink to outside bytes.
    victim = root.parent / "victim.png"
    victim.write_bytes(b"keep")
    path = root / "source.png"
    if kind == "final_symlink":
        path.symlink_to(victim)
    if kind == "missing_parent":
        path = root / "absent/source.png"
    connection = RecordingConnection(path)
    backend = RecordingBackend(connection, root)
    # When retry cleans the source.
    result = await retry_failed_deletions(5, connection.transaction, lambda: backend)
    # Then the source entry is gone, never its target.
    assert result == 1
    assert connection.deleted
    assert not path.is_symlink()
    assert victim.read_bytes() == b"keep"


async def test_retry_commits_fence_before_storage_io(root: Path) -> None:
    # Given an unfenced upload intent.
    connection = RecordingConnection(root / "missing.png")
    backend = RecordingBackend(connection, root)
    # When it is reclaimed.
    assert await retry_failed_deletions(5, connection.transaction, lambda: backend) == 1
    # Then storage runs in T2 after the T1 fence commit.
    assert backend.observations == [(1, 2)], "storage ran before committed fence / outside T2"


async def test_retry_failure_preserves_committed_fence(root: Path) -> None:
    # Given a backend that fails after the reclamation decision.
    connection = RecordingConnection(root / "missing.png")
    backend = RecordingBackend(connection, root)
    backend.failure = True
    # When retry records the failure.
    assert await retry_failed_deletions(5, connection.transaction, lambda: backend) == 0
    # Then the independent fence survives with exactly one retry increment.
    assert connection.committed_fence == 1, "fence was not committed before failing I/O"
    assert connection.row["delete_failed_at"] == "fenced"
    assert connection.row["delete_retry_count"] == 1
    assert not connection.deleted
