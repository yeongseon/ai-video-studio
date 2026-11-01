"""Publish an immutable local generation only after complete, durable staging."""

import hashlib
import os
from uuid import uuid4
from pathlib import Path
from typing import BinaryIO


def publish_local(path: Path, data: bytes | BinaryIO) -> tuple[int, str]:
    md5 = hashlib.md5(usedforsecurity=False)
    size = 0
    temporary: Path | None = None
    try:
        temporary = path.parent / f".publish-{uuid4().hex}"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(descriptor, "wb") as staged:
            if path.exists():
                os.fchmod(staged.fileno(), path.stat().st_mode & 0o777)
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
