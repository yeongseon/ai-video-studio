"""Storage exceptions must support the interpreter's mutable traceback protocol."""

import pytest
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
