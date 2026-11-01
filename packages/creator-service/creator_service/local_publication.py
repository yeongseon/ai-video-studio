"""Publish an immutable local generation only after complete, durable staging."""

import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO


def publish_local(path: Path, data: bytes | BinaryIO) -> tuple[int, str]:
    md5 = hashlib.md5(usedforsecurity=False)
    size = 0
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".publish-", delete=False) as staged:
            temporary = Path(staged.name)
            if isinstance(data, bytes):
                staged.write(data)
                md5.update(data)
                size = len(data)
            else:
                while chunk := data.read(8192):
                    staged.write(chunk)
                    md5.update(chunk)
                    size += len(chunk)
            staged.flush()
            os.fsync(staged.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return size, md5.hexdigest()
