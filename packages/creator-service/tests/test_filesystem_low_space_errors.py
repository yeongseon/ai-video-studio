"""Actionable error taxonomy for jobs refused by the free-space admission check (#1009)."""

from __future__ import annotations

import pickle
from typing import cast

import pytest
from creator_domain.exceptions import ServiceUnavailableError

from creator_service.actionable_errors import (
    ErrorCategory,
    build_run_failure_summary,
    failure_summary_from_code,
    map_service_error,
)
from creator_service.storage_policy import FilesystemLowSpace, StorageLowWatermark

MIB = 1024 * 1024


def _refusal(filesystem: str) -> FilesystemLowSpace:
    return FilesystemLowSpace(
        incoming_bytes=0, free_bytes=412 * MIB, filesystem=filesystem, required_bytes=500 * MIB,
    )


def test_a_refused_job_is_a_retryable_low_watermark_and_a_503() -> None:
    # Given the error a refused job raises.
    refusal = _refusal("artifact")
    # Then it extends the existing upload-staging error and keeps its HTTP mapping.
    assert isinstance(refusal, StorageLowWatermark)
    assert isinstance(refusal, ServiceUnavailableError)
    assert refusal.http_status_code == 503


@pytest.mark.parametrize("filesystem,category", [
    ("artifact", ErrorCategory.ARTIFACT_STORAGE_LOW),
    ("scratch", ErrorCategory.SCRATCH_STORAGE_LOW),
])
def test_each_filesystem_maps_to_its_own_retryable_category(filesystem: str, category: ErrorCategory) -> None:
    # Given a refusal for one filesystem.
    # When it is mapped to the actionable taxonomy.
    actionable = map_service_error(_refusal(filesystem))
    # Then the user is told which storage is low and that retrying after freeing space is the fix.
    assert actionable.category is category
    assert actionable.code == category.value
    assert actionable.retryable is True


@pytest.mark.parametrize("category,words", [
    (ErrorCategory.ARTIFACT_STORAGE_LOW, ("artifact", "free")),
    (ErrorCategory.SCRATCH_STORAGE_LOW, ("temporary", "free")),
])
def test_recovery_steps_tell_the_operator_to_free_that_storage(category: ErrorCategory, words: tuple[str, ...]) -> None:
    # Given the failure summary rebuilt from a stored code.
    summary = failure_summary_from_code(category.value)
    # Then the steps name the storage and the action, and never send the operator to a provider.
    assert summary is not None
    steps = " ".join(cast("list[str]", summary["recovery_steps"])).lower()
    assert all(word in steps for word in words)
    assert "provider" not in steps


def test_upload_staging_low_watermark_still_maps_to_unavailable() -> None:
    # Given the pre-existing upload-staging refusal.
    # When it is mapped.
    actionable = map_service_error(StorageLowWatermark(incoming_bytes=10, free_bytes=5))
    # Then its category and retryability are unchanged by the new job refusals.
    assert actionable.category is ErrorCategory.UNAVAILABLE
    assert actionable.retryable is True


@pytest.mark.parametrize("filesystem", ["artifact", "scratch"])
def test_failure_summary_is_static_and_rebuilt_from_the_stored_code(filesystem: str) -> None:
    # Given a refusal whose exception text carries measured numbers.
    refusal = _refusal(filesystem)
    # When the worker builds the summary it persists and the API rebuilds it from the code.
    built = build_run_failure_summary(refusal)
    rebuilt = failure_summary_from_code(str(built["code"]))
    # Then both agree, are retryable, and carry no measured value or path in the message.
    assert rebuilt == built
    assert built["retryable"] is True
    message = str(built["message"])
    assert "MiB" not in message and "412" not in message and "/" not in message


def test_the_two_storage_low_codes_are_distinct_and_stable() -> None:
    # Given the persisted code is the only thing the read side keeps.
    # Then the two storages are distinguishable by code, and the strings are a stable contract.
    assert ErrorCategory.ARTIFACT_STORAGE_LOW.value == "ARTIFACT_STORAGE_LOW"
    assert ErrorCategory.SCRATCH_STORAGE_LOW.value == "SCRATCH_STORAGE_LOW"


@pytest.mark.parametrize("filesystem", ["artifact", "scratch"])
def test_refusal_survives_a_pickle_round_trip(filesystem: str) -> None:
    # Given an exception that Celery's prefork pool may have to pickle.
    refusal = _refusal(filesystem)
    # When it crosses a process boundary.
    restored = pickle.loads(pickle.dumps(refusal))
    # Then it is the same typed error, with the same facts.
    assert isinstance(restored, FilesystemLowSpace)
    assert (restored.filesystem, restored.free_bytes, restored.required_bytes) == (filesystem, 412 * MIB, 500 * MIB)
    assert str(restored) == str(refusal)
