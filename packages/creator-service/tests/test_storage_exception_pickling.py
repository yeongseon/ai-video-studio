"""Storage exceptions cross process boundaries by pickle, however they were constructed (#1087)."""

from __future__ import annotations

import pickle
from collections.abc import Callable
from dataclasses import dataclass, fields

import pytest
from creator_service.actionable_errors import map_service_error
from creator_service.storage_policy import (
    FilesystemLowSpace,
    StorageBudgetExceeded,
    StorageLowWatermark,
)


@dataclass(slots=True, eq=False)
class TaggedBudgetExceeded(StorageBudgetExceeded):
    tag: str = "default"


MAKERS: dict[str, Callable[[], StorageBudgetExceeded | StorageLowWatermark]] = {
    "budget-keywords": lambda: StorageBudgetExceeded(
        workspace_id=7, incoming_bytes=10, stored_bytes=90, reserved_bytes=5,
        max_stored_bytes=100, max_asset_count=3, max_reserved_bytes=50,
    ),
    "budget-positional": lambda: StorageBudgetExceeded(7, 10, 90, 5, 100),
    "budget-positional-and-keyword": lambda: StorageBudgetExceeded(7, 10, 90, 5, 100, max_asset_count=3),
    "budget-subclass-with-extra-field": lambda: TaggedBudgetExceeded(
        workspace_id=7, incoming_bytes=10, stored_bytes=90, reserved_bytes=5,
        max_stored_bytes=100, max_asset_count=3, tag="custom",
    ),
    "watermark-keywords": lambda: StorageLowWatermark(incoming_bytes=10, free_bytes=5),
    "filesystem-subclass": lambda: FilesystemLowSpace(
        incoming_bytes=0, free_bytes=412, filesystem="scratch", required_bytes=500,
    ),
}


@pytest.mark.parametrize("protocol", [pickle.DEFAULT_PROTOCOL, pickle.HIGHEST_PROTOCOL])
@pytest.mark.parametrize("make", MAKERS.values(), ids=MAKERS.keys())
def test_storage_exception_survives_a_pickle_round_trip(
    make: Callable[[], StorageBudgetExceeded | StorageLowWatermark], protocol: int,
) -> None:
    # Given an exception built the way admission code builds it.
    error = make()
    # When it crosses a process boundary, as a Celery result or worker boundary may require.
    restored = pickle.loads(pickle.dumps(error, protocol=protocol))
    # Then it is the same typed error with every field, its message, detail and HTTP mapping.
    assert type(restored) is type(error)
    assert restored is not error
    assert {f.name: getattr(restored, f.name) for f in fields(restored)} == {
        f.name: getattr(error, f.name) for f in fields(error)
    }
    assert (str(restored), restored.detail, restored.http_status_code) == (
        str(error), error.detail, error.http_status_code,
    )
    assert map_service_error(restored) == map_service_error(error)
