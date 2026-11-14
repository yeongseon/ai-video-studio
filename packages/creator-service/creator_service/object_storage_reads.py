"""Bounded storage reads: each call owns and releases one provider response."""

import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from collections.abc import Callable, Iterator
from typing import Final, Protocol, TypedDict
from typing_extensions import NotRequired
from .exception_pickling import Reduced, field_values, rebuild_exception
from .storage_sdk_logging import private_sdk_operation

READ_CHUNK_SIZE: Final = 64 * 1024


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    size: int
    etag: str | None = None
    version_id: str | None = None


class StorageObjectMissing(RuntimeError):
    def __str__(self) -> str:
        return "Object not found"


@dataclass(slots=True)
class StorageReadError(RuntimeError):
    """Exception state remains mutable so Python can attach traceback and cause."""

    kind: str = "unavailable"

    def __str__(self) -> str:
        return "Storage read failed"

    def __reduce__(self) -> Reduced:
        return rebuild_exception, (type(self), field_values(self))


def validate_range(offset: int, length: int) -> None:
    if offset < 0 or not 0 < length <= READ_CHUNK_SIZE:
        raise ValueError("Read must use a nonnegative offset and 1..65536 bytes")


def validate_key(key: str) -> list[str]:
    """Reject traversal, not prove ownership of literal legacy non-workspace keys."""
    path = PurePosixPath(key)
    if not key or path.is_absolute() or ".." in path.parts or "\\" in key or "\x00" in key:
        raise StorageObjectMissing()
    parts = list(path.parts)
    if not parts:
        raise StorageObjectMissing()
    return parts


@contextmanager
def local_descriptor(root: Path, key: str) -> Iterator[int]:
    """Open relative to trusted directory descriptors, rejecting symlink races."""
    parts = validate_key(key)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    directory = os.open(root.resolve(), flags | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            child = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise StorageObjectMissing()
            yield descriptor
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def local_stat(root: Path, key: str) -> ObjectInfo:
    try:
        with local_descriptor(root, key) as descriptor:
            return ObjectInfo(os.fstat(descriptor).st_size)
    except OSError:
        raise StorageObjectMissing() from None


def local_read(root: Path, key: str, offset: int, length: int) -> bytes:
    validate_range(offset, length)
    try:
        with local_descriptor(root, key) as descriptor:
            return os.pread(descriptor, length, offset)
    except OSError:
        raise StorageObjectMissing() from None


class S3Body(Protocol):
    def read(self, amt: int) -> bytes: ...
    def close(self) -> None: ...


class S3Head(TypedDict):
    ContentLength: int
    ETag: NotRequired[str]
    VersionId: NotRequired[str]


class S3Object(TypedDict):
    Body: S3Body


def s3_stat(operation: Callable[[], S3Head]) -> ObjectInfo:
    from botocore.exceptions import BotoCoreError, ClientError, ReadTimeoutError

    try:
        with private_sdk_operation():
            result = operation()
            return ObjectInfo(result["ContentLength"], result.get("ETag"), result.get("VersionId"))
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {"PreconditionFailed", "412"}:
            raise StorageReadError("generation_changed") from None
        if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
            raise StorageObjectMissing() from None
        raise StorageReadError() from None
    except (ReadTimeoutError, TimeoutError):
        raise StorageReadError("timeout") from None
    except (BotoCoreError, OSError):
        raise StorageReadError() from None


def s3_read(operation: Callable[[str], S3Object], offset: int, length: int) -> bytes:
    from botocore.exceptions import BotoCoreError, ClientError, ReadTimeoutError

    validate_range(offset, length)
    try:
        with private_sdk_operation():
            response = operation(f"bytes={offset}-{offset + length - 1}")
            body = response["Body"]
            try:
                return body.read(length)
            finally:
                body.close()
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {"PreconditionFailed", "412"}:
            raise StorageReadError("generation_changed") from None
        if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
            raise StorageObjectMissing() from None
        raise StorageReadError() from None
    except (ReadTimeoutError, TimeoutError):
        raise StorageReadError("timeout") from None
    except (BotoCoreError, OSError):
        raise StorageReadError() from None


class BlobProperties(Protocol):
    @property
    def size(self) -> int: ...

    @property
    def etag(self) -> str: ...


def azure_stat(operation: Callable[[], BlobProperties]) -> ObjectInfo:
    from azure.core.exceptions import AzureError, ResourceNotFoundError

    try:
        with private_sdk_operation():
            result = operation()
            return ObjectInfo(result.size, result.etag)
    except ResourceNotFoundError:
        raise StorageObjectMissing() from None
    except TimeoutError:
        raise StorageReadError("timeout") from None
    except (AzureError, OSError):
        raise StorageReadError() from None


def azure_read(operation: Callable[[int, int], bytes], offset: int, length: int) -> bytes:
    from azure.core.exceptions import AzureError, HttpResponseError, ResourceNotFoundError

    validate_range(offset, length)
    try:
        # readall consumes at most this bounded range, not the whole blob. The
        # synchronous SDK consumes/releases its HTTP response during this call.
        with private_sdk_operation():
            return operation(offset, length)
    except ResourceNotFoundError:
        raise StorageObjectMissing() from None
    except HttpResponseError as error:
        raise StorageReadError("generation_changed" if error.status_code == 412 else "unavailable") from None
    except TimeoutError:
        raise StorageReadError("timeout") from None
    except (AzureError, OSError):
        raise StorageReadError() from None
