"""Bookkeeping rows must fit creator_artifacts columns for every name the worker generates."""

import contextlib
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from creator_service.rejected_scene_cleanup import RejectedSceneCleanupService

_SCENE_ID_COLUMN_WIDTH = 100


class RecordingConnection:
    def __init__(self) -> None:
        self.inserted: list[tuple[object, ...]] = []

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        if "INSERT INTO creator_artifacts" in query:
            self.inserted.append(args)
            return {"id": 1}
        if "creator_projects" in query or "creator_runs" in query:
            return {"status": "active"}
        return None

    async def execute(self, query: str, *args: object) -> str:
        return "OK"


@pytest.fixture
def connection(monkeypatch: pytest.MonkeyPatch) -> RecordingConnection:
    recorder = RecordingConnection()

    @contextlib.asynccontextmanager
    async def transaction() -> AsyncIterator[RecordingConnection]:
        yield recorder

    monkeypatch.setattr("creator_service.rejected_scene_cleanup.transaction", transaction)
    return recorder


def _worker_filename(scene_id: str, task_id: str) -> str:
    owner = f"{task_id}-0123456789abcdef-12345"
    return f"{scene_id}-{owner}-{uuid4().hex}.png"


@pytest.mark.parametrize("scene_id", ["scene-1", "scene-10", "s" * 100])
@pytest.mark.parametrize(
    "task_id", [str(uuid4()), f"sync-generate_scene_image-7-{uuid4().hex[:12]}"],
    ids=["celery", "lightweight"],
)
async def test_intent_registration_bounds_the_bookkeeping_scene_id(
    connection: RecordingConnection, scene_id: str, task_id: str,
) -> None:
    # Given the filename the worker really generates for this scene and delivery.
    name = _worker_filename(scene_id, task_id)

    # When the cleanup intent is registered before the upload.
    await RejectedSceneCleanupService().record_intent(
        run_id=7, workspace_id=1, project_id=1,
        storage_key=f"7/scenes/{name}", local_path=f"/data/artifacts/7/scenes/{name}",
        storage_provider="local", content_type="image/png",
    )

    # Then the persisted scene_id fits its column and still ends with the per-attempt UUID.
    scene_column = connection.inserted[0][3]
    assert isinstance(scene_column, str)
    assert len(scene_column) <= _SCENE_ID_COLUMN_WIDTH
    assert name.removesuffix(".png").endswith(scene_column)


async def test_rejected_record_bounds_the_bookkeeping_scene_id_too(
    connection: RecordingConnection,
) -> None:
    name = _worker_filename("s" * 100, str(uuid4()))

    await RejectedSceneCleanupService().record(
        run_id=7, workspace_id=1, project_id=1,
        storage_key=f"7/scenes/{name}", local_path=f"/data/artifacts/7/scenes/{name}",
        storage_provider="local", content_type="image/png", size_bytes=1, checksum="abc",
    )

    scene_column = connection.inserted[0][3]
    assert isinstance(scene_column, str)
    assert len(scene_column) <= _SCENE_ID_COLUMN_WIDTH
