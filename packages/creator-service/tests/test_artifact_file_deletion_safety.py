"""Root-anchored file deletion, including all cleanup entry points."""

from pathlib import Path

import pytest
from creator_service import confined_directory_deletion as deletion
from creator_service.object_storage import LocalStorageBackend
from creator_service.rejected_scene_cleanup import RejectedSceneCleanupService


@pytest.mark.parametrize("kind", ["outside", "traversal", "intermediate_symlink", "root"])
def test_delete_artifact_file_refuses_escape(tmp_path: Path, kind: str) -> None:
    # Given bytes outside the configured root.
    root = tmp_path / "artifacts"
    root.mkdir()
    victim = tmp_path / "victim.png"
    victim.write_bytes(b"keep")
    path = victim
    if kind == "traversal":
        path = root / "../victim.png"
    if kind == "intermediate_symlink":
        (root / "link").symlink_to(tmp_path, target_is_directory=True)
        path = root / "link/victim.png"
    if kind == "root":
        path = root
    # When deletion is attempted.
    with pytest.raises(OSError):
        deletion.delete_artifact_file(path, root)
    # Then no outside bytes were touched.
    assert victim.read_bytes() == b"keep"


@pytest.mark.parametrize("kind", ["final_symlink", "directory", "missing", "missing_parent", "relative", "symlink_root"])
def test_delete_artifact_file_safe_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    # Given a root and a safe entry (the root itself may be a symlink).
    root = tmp_path / "artifacts"
    root.mkdir()
    victim = tmp_path / "victim.png"
    victim.write_bytes(b"keep")
    path = root / "source.png"
    if kind == "final_symlink":
        path.symlink_to(victim)
    if kind == "directory":
        path.mkdir()
    if kind == "missing_parent":
        path = root / "absent/source.png"
    if kind == "relative":
        monkeypatch.chdir(tmp_path)
        path.write_bytes(b"remove")
        root = Path("artifacts")
        path = root / "source.png"
    if kind == "symlink_root":
        alias = tmp_path / "alias"
        alias.symlink_to(root, target_is_directory=True)
        root = alias
        path = root / "source.png"
        path.write_bytes(b"remove")
    # When deleting through the anchored helper.
    if kind == "directory":
        with pytest.raises(IsADirectoryError):
            deletion.delete_artifact_file(path, root)
    else:
        deletion.delete_artifact_file(path, root)
        # Then the safe entry is removed without following a final link.
        assert not path.exists()
        assert not path.is_symlink()
    assert victim.read_bytes() == b"keep"


async def test_rejected_cleanup_refuses_outside_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a pending rejected cleanup whose source is outside the root.
    root = tmp_path / "artifacts"
    root.mkdir()
    monkeypatch.setenv("ARTIFACT_ROOT", str(root))
    victim = tmp_path / "victim.png"
    victim.write_bytes(b"keep")
    events: list[str] = []

    class Backend(LocalStorageBackend):
        def delete(self, key: str) -> bool:
            events.append("object")
            return True

    async def execute(query: str, *args) -> None:
        events.append("row")

    monkeypatch.setattr("creator_service.rejected_scene_cleanup.get_storage_backend", Backend)
    monkeypatch.setattr("creator_service.rejected_scene_cleanup.execute", execute)
    # When immediate cleanup reaches local deletion.
    with pytest.raises(OSError):
        await RejectedSceneCleanupService().cleanup(956, "956/scenes/object.png", str(victim))
    # Then object deletion precedes refusal and the durable row remains.
    assert events == ["object"]
    assert victim.read_bytes() == b"keep"
