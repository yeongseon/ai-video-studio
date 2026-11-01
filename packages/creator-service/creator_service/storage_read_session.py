"""One generation and resource owner for an authorized media transfer."""

import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol

from .object_storage_reads import ObjectInfo, StorageReadError, validate_range


class ObjectReader(Protocol):
    @property
    def info(self) -> ObjectInfo: ...
    def read_range(self, offset: int, length: int) -> bytes: ...
    def verify(self) -> None: ...
    def close(self) -> None: ...


class RemoteReader:
    def __init__(self, info: ObjectInfo, read: Callable[[int, int], bytes]) -> None:
        self.info = info
        self._read = read

    def read_range(self, offset: int, length: int) -> bytes:
        validate_range(offset, length)
        return self._read(offset, length)

    def verify(self) -> None:
        return None

    def close(self) -> None:
        return None


class LocalReader:
    """Own one no-follow descriptor; atomic replacement retains the original inode."""

    def __init__(self, owner: AbstractContextManager[int]) -> None:
        self._owner = owner
        self._descriptor = owner.__enter__()
        self._closed = False
        self._initial = os.fstat(self._descriptor)
        self.info = ObjectInfo(self._initial.st_size)

    def verify(self) -> None:
        current = os.fstat(self._descriptor)
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
            self._initial.st_dev, self._initial.st_ino, self._initial.st_size, self._initial.st_mtime_ns,
        ):
            raise StorageReadError("generation_changed")

    def read_range(self, offset: int, length: int) -> bytes:
        validate_range(offset, length)
        self.verify()
        block = os.pread(self._descriptor, length, offset)
        self.verify()
        return block

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._owner.__exit__(None, None, None)
