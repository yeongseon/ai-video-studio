"""Storage exceptions must support the interpreter's mutable traceback protocol."""

import pytest
from creator_service.object_storage_reads import StorageReadError
from creator_service.storage_policy import (
    FilesystemLowSpace,
    StorageBudgetExceeded,
    StorageLowWatermark,
)


@pytest.mark.parametrize("error", [
    StorageBudgetExceeded(1, 10, 90, 10, 100),
    StorageLowWatermark(10, 0),
    FilesystemLowSpace(0, 0, "scratch", 100),
])
def test_handled_storage_exception_allows_traceback_cleanup(
    error: StorageBudgetExceeded | StorageLowWatermark,
) -> None:
    # Given an exception raised by storage admission.
    try:
        raise error
    except (StorageBudgetExceeded, StorageLowWatermark) as handled:
        assert handled.__traceback__ is not None

        # When Starlette 1.10 clears a handled exception's retained request frames.
        handled.__traceback__ = None

        # Then the standard exception protocol succeeds without masking the error.
        assert handled.__traceback__ is None


@pytest.mark.parametrize("make", [
    lambda: StorageBudgetExceeded(1, 10, 90, 10, 100),
    lambda: StorageLowWatermark(10, 0),
    lambda: FilesystemLowSpace(0, 0, "scratch", 100),
], ids=["budget", "watermark", "filesystem"])
def test_storage_exception_keeps_identity_hash_when_made_mutable(make) -> None:
    # Given two equal-looking instances of a mutable (non-frozen) exception dataclass.
    first, second = make(), make()
    # When they are used as set members or dict keys, as exceptions commonly are.
    members = {first, second}
    # Then they stay hashable and compare by identity like any other exception.
    assert len(members) == 2
    assert first != second


def test_storage_read_error_keeps_identity_hash() -> None:
    # Given two equal-looking instances of the storage read failure.
    first, second = StorageReadError(kind="timeout"), StorageReadError(kind="timeout")
    # When they are used as set members or dict keys, as exceptions commonly are.
    members = {first, second}
    # Then they stay hashable and compare by identity like any other exception.
    assert len(members) == 2
    assert first != second
