import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from creator_service.object_storage import LocalStorageBackend, StorageReadError


@pytest.mark.parametrize("via_alias", [False, True])
def test_changed_ctime_rejects_same_size_rewrite_with_restored_mtime(tmp_path: Path, monkeypatch, via_alias: bool) -> None:
    # Given a retained descriptor and an external in-place writer (unsupported publication).
    target = tmp_path / "object"
    target.write_bytes(b"aaaa")
    alias = tmp_path / "alias"
    os.link(target, alias)
    reader = LocalStorageBackend(str(tmp_path)).open_read("object")
    before = target.stat()
    try:
        # When the same inode is changed while mtime is deliberately restored.
        (alias if via_alias else target).write_bytes(b"bbbb")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        # Then changed ctime is defense-in-depth, never proof against all external writers.
        original = os.fstat

        def observed_change(fd):
            current = original(fd)
            return SimpleNamespace(st_dev=current.st_dev, st_ino=current.st_ino, st_size=current.st_size,
                                   st_mtime_ns=current.st_mtime_ns, st_ctime_ns=before.st_ctime_ns + 1,
                                   st_nlink=current.st_nlink)

        monkeypatch.setattr(os, "fstat", observed_change)
        with pytest.raises(StorageReadError):
            reader.verify()
    finally:
        reader.close()


def test_size_change_is_rejected_even_when_other_stat_fields_match(tmp_path, monkeypatch) -> None:
    # Given a real growth with restored mtime and simulated coarse ctime resolution.
    target = tmp_path / "object"
    target.write_bytes(b"aaaa")
    reader = LocalStorageBackend(str(tmp_path)).open_read("object")
    before = target.stat()
    with target.open("ab") as file:
        file.write(b"growth")
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    original = os.fstat

    def coarse(fd):
        current = original(fd)
        return SimpleNamespace(st_dev=current.st_dev, st_ino=current.st_ino, st_size=current.st_size,
                               st_mtime_ns=before.st_mtime_ns, st_ctime_ns=before.st_ctime_ns, st_nlink=current.st_nlink)

    monkeypatch.setattr(os, "fstat", coarse)
    try:
        # When verification sees only the size difference.
        with pytest.raises(StorageReadError):
            reader.verify()
    finally:
        reader.close()
