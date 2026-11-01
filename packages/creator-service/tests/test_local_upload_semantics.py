import os
from io import BytesIO

import pytest

from creator_service.object_storage import LocalStorageBackend


@pytest.mark.parametrize("mask", [0o022, 0o002, 0o077])
@pytest.mark.parametrize("stream", [False, True])
def test_upload_preserves_original_file_semantics_when_creating_and_overwriting(tmp_path, mask, stream):
    # Given the original writer's process umask and an existing restricted inode.
    backend = LocalStorageBackend(str(tmp_path))
    existing = tmp_path / "existing"
    existing.write_bytes(b"old")
    existing.chmod(0o640)
    inode = existing.stat().st_ino
    previous = os.umask(mask)
    try:
        # When both new and existing keys receive bytes or stream uploads.
        for key in ("new", "existing"):
            backend.upload(key, BytesIO(b"replacement") if stream else b"replacement")
    finally:
        os.umask(previous)
    # Then creation honors umask, overwrite preserves access policy and identity.
    assert (tmp_path / "new").stat().st_mode & 0o777 == 0o666 & ~mask
    assert existing.stat().st_ino == inode
    assert existing.stat().st_mode & 0o777 == 0o640
    assert {path.name for path in tmp_path.iterdir()} == {"new", "existing"}
