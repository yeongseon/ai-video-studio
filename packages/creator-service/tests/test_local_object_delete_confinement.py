"""Local object deletion must stay beneath its root even if a directory is swapped mid-delete."""

from pathlib import Path

import pytest
from creator_service.confined_directory_deletion import unlink_beneath
from creator_service.object_storage import LocalStorageBackend, StorageDeletionError

_KEY = "956/scenes/object.png"


@pytest.fixture
def layout(tmp_path: Path) -> tuple[LocalStorageBackend, Path, Path]:
    root = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "object.png"
    victim.write_bytes(b"keep")
    return LocalStorageBackend(str(root)), root, victim


def test_delete_never_follows_a_directory_swapped_in_after_validation(
    layout: tuple[LocalStorageBackend, Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a real object whose parent directory is replaced by a symlink after the key check.
    backend, root, victim = layout
    backend.upload(_KEY, b"object", "image/png")
    original_safe_key = LocalStorageBackend._safe_key

    def validate_then_swap(self: LocalStorageBackend, key: str) -> str:
        validated = original_safe_key(self, key)
        scenes = root / "956" / "scenes"
        for child in scenes.iterdir():
            child.unlink()
        scenes.rmdir()
        scenes.symlink_to(victim.parent, target_is_directory=True)
        return validated

    monkeypatch.setattr(LocalStorageBackend, "_safe_key", validate_then_swap)

    # When the object is deleted through the backend.
    with pytest.raises(StorageDeletionError):
        backend.delete(_KEY)

    # Then the file outside the root is untouched.
    assert victim.read_bytes() == b"keep"


def test_delete_refuses_a_symlinked_intermediate_directory(
    layout: tuple[LocalStorageBackend, Path, Path],
) -> None:
    # Given a scene directory that is a symlink, even one resolving inside the root.
    backend, root, _victim = layout
    elsewhere = root / "elsewhere"
    elsewhere.mkdir(parents=True)
    (elsewhere / "object.png").write_bytes(b"inside")
    (root / "956").mkdir()
    (root / "956" / "scenes").symlink_to(elsewhere, target_is_directory=True)

    # When the object is deleted through the link.
    with pytest.raises(StorageDeletionError):
        backend.delete(_KEY)

    # Then nothing was removed.
    assert (elsewhere / "object.png").read_bytes() == b"inside"


def test_delete_still_reports_presence_and_absence(
    layout: tuple[LocalStorageBackend, Path, Path],
) -> None:
    backend, _root, _victim = layout
    backend.upload(_KEY, b"object", "image/png")

    assert backend.delete(_KEY) is True
    assert backend.delete(_KEY) is False
    assert backend.delete("956/missing/object.png") is False


def test_delete_reports_absent_when_a_regular_file_occupies_a_directory_position(
    layout: tuple[LocalStorageBackend, Path, Path],
) -> None:
    # Given a regular file where the key's directory would have to be.
    backend, root, _victim = layout
    root.mkdir(parents=True)
    (root / "956").write_bytes(b"not a directory")

    # When an object beneath it is deleted.
    deleted = backend.delete(_KEY)

    # Then the object is simply absent, as before; the stray file is untouched.
    assert deleted is False
    assert (root / "956").read_bytes() == b"not a directory"


def test_delete_rejects_a_key_whose_lexical_and_resolved_meanings_differ(
    tmp_path: Path,
) -> None:
    # Given a symlinked alias inside the root and a victim file one level above the root.
    root = tmp_path / "artifacts"
    (root / "a" / "b").mkdir(parents=True)
    (root / "alias").symlink_to(root / "a" / "b", target_is_directory=True)
    victim = tmp_path / "victim.png"
    victim.write_bytes(b"keep")
    backend = LocalStorageBackend(str(root))

    # When a key whose '..' components resolve inside the root is deleted: the kernel
    # resolves it to <root>/victim.png, a lexical rewrite would name <root>/../victim.png.
    with pytest.raises(ValueError, match="path traversal"):
        backend.delete("alias/../../victim.png")

    # Then the file outside the root is untouched.
    assert victim.read_bytes() == b"keep"


def test_delete_rejects_absolute_keys_instead_of_re_rooting_them(tmp_path: Path) -> None:
    # Given an object at <root>/object.png and a decoy where its absolute path re-rooted
    # beneath the root would land.
    root = tmp_path / "artifacts"
    root.mkdir()
    real = root / "object.png"
    real.write_bytes(b"real")
    decoy = root.joinpath(*real.parts[1:])
    decoy.parent.mkdir(parents=True)
    decoy.write_bytes(b"decoy")
    backend = LocalStorageBackend(str(root))

    # When the absolute path is passed as a key.
    with pytest.raises(ValueError, match="path traversal"):
        backend.delete(str(real))

    # Then neither file was touched; callers must translate to a root-relative key first.
    assert real.read_bytes() == b"real"
    assert decoy.read_bytes() == b"decoy"


@pytest.mark.parametrize("parts", [("..", "victim.png"), ("a", "..", "victim.png"), ("", "victim.png")])
def test_unlink_beneath_refuses_components_that_are_not_plain_names(
    tmp_path: Path, parts: tuple[str, ...],
) -> None:
    # Given a victim beside the root, reachable only by a '..' step.
    root = tmp_path / "artifacts"
    (root / "a").mkdir(parents=True)
    victim = tmp_path / "victim.png"
    victim.write_bytes(b"keep")

    # When the primitive is handed a component that is not a plain directory entry name.
    with pytest.raises(ValueError, match="unsafe path component"):
        unlink_beneath(root, parts)

    # Then nothing outside the root was removed.
    assert victim.read_bytes() == b"keep"


def test_delete_still_ignores_harmless_dot_and_empty_components(tmp_path: Path) -> None:
    # Given an object reached through '.' and doubled separators, which never change meaning.
    backend = LocalStorageBackend(str(tmp_path / "artifacts"))
    backend.upload(_KEY, b"object", "image/png")

    # When it is deleted through such a spelling.
    deleted = backend.delete("./956//scenes/./object.png")

    # Then it is deleted exactly as before.
    assert deleted is True
    assert backend.exists(_KEY) is False
