"""Domain error hierarchy. API layer maps these to HTTP responses."""

from __future__ import annotations


class AppError(Exception):
    status_code = 400
    code = "app_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class ProviderError(AppError):
    """An external provider (LLM, search, DB) failed.

    ``retryable`` tells the job runner whether a bounded retry makes sense.
    """

    status_code = 502
    code = "provider_error"

    def __init__(self, message: str, *, retryable: bool = True, retry_after: float | None = None,
                 details: dict | None = None) -> None:
        super().__init__(message, details=details)
        self.retryable = retryable
        self.retry_after = retry_after


class ConfigurationError(AppError):
    status_code = 500
    code = "configuration_error"


class UnsafeURLError(AppError):
    status_code = 400
    code = "unsafe_url"
