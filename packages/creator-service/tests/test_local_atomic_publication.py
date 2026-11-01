import hashlib
import os
from io import BytesIO
from pathlib import Path

import pytest

from creator_service.object_storage import LocalStorageBackend


@pytest.mark.parametrize("stream", [False, True])
def test_upload_overwrite_preserves_open_inode_and_hard_link(tmp_path: Path, stream: bool) -> None:
    # Given a published inode also held by a reader and another hard-link name.
    backend = LocalStorageBackend(str(tmp_path))
    target = tmp_path / "object"
    old = b"a" * 131072
    target.write_bytes(old)
    alias = tmp_path / "alias"
    os.link(target, alias)
    with target.open("rb") as reader:
        before = os.fstat(reader.fileno())
        # When overwriting the logical key with new bytes (not that inode).
        data = b"b" * len(old)
        result = backend.upload("object", BytesIO(data) if stream else data, "video/mp4")
        after = os.fstat(reader.fileno())
        # Then published A remains immutable and B atomically replaces its name.
        assert reader.read() == old
        assert alias.read_bytes() == old
        assert before.st_ino == after.st_ino
        assert before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns
        assert target.stat().st_ino != before.st_ino
        assert target.read_bytes() == data
        assert result.checksum == hashlib.md5(data, usedforsecurity=False).hexdigest()
        assert result.size_bytes == len(data)


def test_upload_fsyncs_completed_temp_before_atomic_replace(tmp_path: Path, monkeypatch) -> None:
    # Given a preexisting object and observed publication syscalls.
    backend = LocalStorageBackend(str(tmp_path))
    (tmp_path / "object").write_bytes(b"old")
    events = []
    fsync, replace = os.fsync, os.replace

    def sync(fd):
        events.append("fsync")
        fsync(fd)

    def publish(source, target):
        assert Path(source).parent == Path(target).parent
        assert Path(source).read_bytes() == b"new"
        events.append("replace")
        replace(source, target)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "replace", publish)
    # When the adapter publishes the replacement.
    backend.upload("object", b"new")
    # Then durability precedes visibility and no staging file survives.
    assert "fsync" in events
    assert events.index("fsync") < events.index("replace")
    assert set(path.name for path in tmp_path.iterdir()) == {"object"}


@pytest.mark.parametrize("failure", ["source", "fsync", "replace"])
def test_upload_failure_preserves_old_generation_and_cleans_temp(tmp_path: Path, monkeypatch, failure: str) -> None:
    # Given a failure during staging/durability/publication of a replacement.
    backend = LocalStorageBackend(str(tmp_path))
    target = tmp_path / "object"
    target.write_bytes(b"old")

    class BrokenStream(BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            raise OSError("source failed")

    def fail(*args):
        raise OSError("publication failed")

    if failure != "source":
        monkeypatch.setattr(os, failure, fail)
    # When the upload cannot publish fully.
    with pytest.raises(OSError):
        backend.upload("object", BrokenStream() if failure == "source" else b"new")
    # Then a failed write cannot destroy A or leave private temporary content.
    assert target.read_bytes() == b"old"
    assert set(path.name for path in tmp_path.iterdir()) == {"object"}
