"""Typed service-layer exceptions with HTTP status code mapping.

These exceptions are raised by the service layer and mapped to HTTP responses
by a central exception handler in the API layer.  This eliminates the need for
service code to import or raise ``fastapi.HTTPException``.

Hierarchy
---------
ServiceError (base)
├── NotFoundError          → 404
├── ValidationError        → 400
├── ConflictError          → 409
│   └── VersionConflictError → 409
├── QuotaExceededError     → 429
├── ServiceUnavailableError → 503
└── DataIntegrityError     → 500
"""

from __future__ import annotations


class ServiceError(Exception):
    """Base exception for all typed service-layer errors.

    Subclasses declare an ``http_status_code`` so the central exception handler
    can translate them to the correct HTTP response without per-route logic.
    """

    http_status_code: int = 500

    def __init__(self, detail: str = "Internal server error") -> None:
        self.detail = detail
        super().__init__(detail)


class NotFoundError(ServiceError):
    """Resource not found."""

    http_status_code: int = 404

    def __init__(self, detail: str = "Not found") -> None:
        super().__init__(detail)


class ValidationError(ServiceError):
    """Invalid input or request."""

    http_status_code: int = 400

    def __init__(self, detail: str = "Bad request") -> None:
        super().__init__(detail)


class ChunkManifestValidationError(ValidationError):
    """The long-form chunk contract violates a timing or policy invariant."""


class ProviderConfigError(ValidationError):
    """A provider credential is missing, invalid, or not configured.

    A ValidationError subclass so it stays a user-fixable 400 (fix the provider
    configuration), but a distinct type so the actionable-error taxonomy can keep
    the auth/config root cause instead of collapsing it into generic VALIDATION.
    """

    def __init__(
        self, detail: str = "Provider is not configured or its credential is invalid"
    ) -> None:
        super().__init__(detail)


class ConflictError(ServiceError):
    """State conflict (e.g. concurrent modification, stage mismatch)."""

    http_status_code: int = 409

    def __init__(self, detail: str = "Conflict") -> None:
        super().__init__(detail)


class VersionConflictError(ConflictError):
    """Optimistic concurrency version mismatch."""

    def __init__(
        self,
        resource_id: int | str,
        expected_version: int,
        actual_version: int,
    ) -> None:
        self.resource_id = resource_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"Version conflict for {resource_id}: "
            f"expected {expected_version}, actual {actual_version}"
        )


class NoHistoryError(ConflictError):
    """No undo/redo history entry is available at this boundary."""

    def __init__(self, detail: str = "No history to apply") -> None:
        super().__init__(detail)


class QuotaExceededError(ServiceError):
    """Usage quota or rate limit exceeded."""

    http_status_code: int = 429

    def __init__(self, detail: str = "Quota exceeded") -> None:
        super().__init__(detail)


class ServiceUnavailableError(ServiceError):
    """Downstream service or infrastructure temporarily unavailable."""

    http_status_code: int = 503

    def __init__(self, detail: str = "Service unavailable") -> None:
        super().__init__(detail)


class DataIntegrityError(ServiceError):
    """Stored data is malformed or corrupted."""

    http_status_code: int = 500

    def __init__(self, detail: str = "Data integrity error") -> None:
        super().__init__(detail)
