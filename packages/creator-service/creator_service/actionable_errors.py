"""SF-78: actionable error mapping, redaction, and async failure summaries.

map_service_error derives a stable, type-based category (never status-based) with
correct retryability and STATIC recovery copy — raw exception detail is never
interpolated into user-facing steps, so paths/secrets cannot leak that way.
map_provider_error wraps a domain ProviderError into the right
creator_domain ServiceError with a safe static detail. redact_error_message strips
filesystem paths, URLs, and secret-shaped tokens. build_run_failure_summary is the
async (worker) surface: a safe, categorized, redacted failure summary for a run
that transitioned to FAILED.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from .error_redaction import redact_error_message as redact_error_message

from creator_domain.exceptions import (
    ConflictError,
    DataIntegrityError,
    NotFoundError,
    ProviderConfigError,
    QuotaExceededError,
    ServiceError,
    ServiceUnavailableError,
    ValidationError,
    VersionConflictError,
)
from .storage_policy import ARTIFACT_FILESYSTEM, FilesystemLowSpace, StorageBudgetExceeded
from creator_domain.provider_errors import (
    ProviderAuthError,
    ProviderError,
    ProviderTimeoutError,
    ProviderValidationError,
    RateLimitError,
)


class ErrorCategory(enum.Enum):
    NOT_FOUND = "NOT_FOUND"
    VALIDATION = "VALIDATION"
    PROVIDER_AUTH = "PROVIDER_AUTH"
    CONFLICT = "CONFLICT"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    QUOTA = "QUOTA"
    UNAVAILABLE = "UNAVAILABLE"
    DATA_INTEGRITY = "DATA_INTEGRITY"
    INTERNAL = "INTERNAL"
    STORAGE_BUDGET = "STORAGE_BUDGET"
    ARTIFACT_STORAGE_LOW = "ARTIFACT_STORAGE_LOW"
    SCRATCH_STORAGE_LOW = "SCRATCH_STORAGE_LOW"


@dataclass(frozen=True)
class ActionableError:
    code: str
    category: ErrorCategory
    retryable: bool
    recovery_steps: tuple[str, ...]
    version_conflict: dict[str, int | str] | None = None


_RECOVERY: dict[ErrorCategory, tuple[str, ...]] = {
    ErrorCategory.NOT_FOUND: (
        "Verify the id is correct",
        "Confirm you have access to this workspace",
    ),
    ErrorCategory.VALIDATION: (
        "Fix the highlighted input",
        "Check the required fields and try again",
    ),
    ErrorCategory.PROVIDER_AUTH: (
        "Check the provider is configured and its credential is valid",
        "Set or update the provider's API key, then retry",
    ),
    ErrorCategory.CONFLICT: (
        "Reload the current state",
        "Reapply your change, then try again",
    ),
    ErrorCategory.VERSION_CONFLICT: (
        "Refresh to load the latest revision",
        "Reapply your change on top of it",
        "Save again",
    ),
    ErrorCategory.QUOTA: (
        "Wait a moment and retry",
        "Check your provider quota or billing",
    ),
    ErrorCategory.UNAVAILABLE: (
        "Retry in a moment",
        "If it persists, check provider status",
    ),
    ErrorCategory.DATA_INTEGRITY: (
        "Retry the operation",
        "If it persists, contact support",
    ),
    ErrorCategory.INTERNAL: (
        "Retry the operation",
        "If it persists, contact support",
    ),
    ErrorCategory.STORAGE_BUDGET: (
        "Remove unused media or increase the workspace storage limit",
        "Retry after in-flight uploads finish",
    ),
    ErrorCategory.ARTIFACT_STORAGE_LOW: (
        "Free up disk space on the server's artifact storage, then retry",
        "If it keeps happening, ask the server operator to review the artifact free-space limit",
    ),
    ErrorCategory.SCRATCH_STORAGE_LOW: (
        "Free up temporary disk space on the server, then retry",
        "If it keeps happening, ask the server operator to review the temporary-storage free-space limit",
    ),
}

_RETRYABLE: frozenset[ErrorCategory] = frozenset(
    {
        ErrorCategory.QUOTA,
        ErrorCategory.UNAVAILABLE,
        ErrorCategory.ARTIFACT_STORAGE_LOW,
        ErrorCategory.SCRATCH_STORAGE_LOW,
    }
)


def _category_for(exc: ServiceError) -> ErrorCategory:
    # Order matters: VersionConflictError is a ConflictError subclass, and
    # ProviderConfigError is a ValidationError subclass — check the narrow types first.
    if isinstance(exc, VersionConflictError):
        return ErrorCategory.VERSION_CONFLICT
    if isinstance(exc, StorageBudgetExceeded):
        return ErrorCategory.STORAGE_BUDGET
    # Before the generic ServiceUnavailableError branch: a refused job is a StorageLowWatermark.
    if isinstance(exc, FilesystemLowSpace):
        return (
            ErrorCategory.ARTIFACT_STORAGE_LOW
            if exc.filesystem == ARTIFACT_FILESYSTEM
            else ErrorCategory.SCRATCH_STORAGE_LOW
        )
    if isinstance(exc, NotFoundError):
        return ErrorCategory.NOT_FOUND
    if isinstance(exc, ProviderConfigError):
        return ErrorCategory.PROVIDER_AUTH
    if isinstance(exc, ValidationError):
        return ErrorCategory.VALIDATION
    if isinstance(exc, QuotaExceededError):
        return ErrorCategory.QUOTA
    if isinstance(exc, ServiceUnavailableError):
        return ErrorCategory.UNAVAILABLE
    if isinstance(exc, DataIntegrityError):
        return ErrorCategory.DATA_INTEGRITY
    if isinstance(exc, ConflictError):
        return ErrorCategory.CONFLICT
    return ErrorCategory.INTERNAL


def map_service_error(exc: ServiceError) -> ActionableError:
    category = _category_for(exc)
    version_conflict: dict[str, int | str] | None = None
    if isinstance(exc, VersionConflictError):
        version_conflict = {
            "resource_id": (
                redact_error_message(exc.resource_id)
                if isinstance(exc.resource_id, str) else exc.resource_id
            ),
            "expected_version": exc.expected_version,
            "actual_version": exc.actual_version,
        }
    return ActionableError(
        code=category.value,
        category=category,
        retryable=category in _RETRYABLE,
        recovery_steps=_RECOVERY[category],
        version_conflict=version_conflict,
    )


def map_provider_error(exc: ProviderError) -> ServiceError:
    # Never carry the provider's raw message (may hold upstream URL/body/token);
    # each branch emits a safe, static, user-facing detail instead.
    if isinstance(exc, ProviderTimeoutError):
        return ServiceUnavailableError("The provider request timed out")
    if isinstance(exc, RateLimitError):
        return QuotaExceededError("The provider rate limit was exceeded")
    if isinstance(exc, ProviderValidationError):
        return ValidationError("The provider rejected the request; check the input")
    if isinstance(exc, ProviderAuthError):
        return ProviderConfigError(
            "The provider is not configured or its credential is invalid"
        )
    return ServiceUnavailableError("The provider is temporarily unavailable")


_PROVIDER_MESSAGE: dict[ErrorCategory, str] = {
    ErrorCategory.UNAVAILABLE: "The provider was temporarily unavailable",
    ErrorCategory.QUOTA: "The provider rate limit was exceeded",
    ErrorCategory.PROVIDER_AUTH: "The provider is not configured or its credential is invalid",
    ErrorCategory.VALIDATION: "The request could not be processed as given",
    ErrorCategory.INTERNAL: "The operation failed unexpectedly",
    ErrorCategory.ARTIFACT_STORAGE_LOW: "Artifact storage is below its free-space limit",
    ErrorCategory.SCRATCH_STORAGE_LOW: "Temporary storage is below its free-space limit",
}


def build_run_failure_summary(exc: BaseException) -> dict[str, object]:
    """Build a safe, categorized failure summary for a run that hit FAILED.

    Provider errors are mapped to the actionable ServiceError taxonomy; any other
    exception becomes a non-retryable INTERNAL summary. The message is static per
    category (never the raw exception text), so no path/secret/upstream body leaks.
    """
    if isinstance(exc, ProviderError):
        service_exc = map_provider_error(exc)
        mapped = map_service_error(service_exc)
    elif isinstance(exc, ServiceError):
        mapped = map_service_error(exc)
    else:
        mapped = ActionableError(
            code=ErrorCategory.INTERNAL.value,
            category=ErrorCategory.INTERNAL,
            retryable=False,
            recovery_steps=_RECOVERY[ErrorCategory.INTERNAL],
        )
    message = _PROVIDER_MESSAGE.get(mapped.category, "The operation failed")
    return {
        "code": mapped.code,
        "category": mapped.category.value,
        "retryable": mapped.retryable,
        "recovery_steps": list(mapped.recovery_steps),
        "message": message,
    }


def failure_summary_from_code(code: str | None) -> dict[str, object] | None:
    """Reconstruct the full actionable summary from a stored failure code.

    The worker persists only the safe category code; the read surface rebuilds
    the category/retryability/recovery steps so clients get actionable guidance
    without the worker having to store (and risk leaking) anything more.
    """
    if code is None:
        return None
    try:
        category = ErrorCategory(code)
    except ValueError:
        return None
    return {
        "code": category.value,
        "category": category.value,
        "retryable": category in _RETRYABLE,
        "recovery_steps": list(_RECOVERY[category]),
        "message": _PROVIDER_MESSAGE.get(category, "The operation failed"),
    }
