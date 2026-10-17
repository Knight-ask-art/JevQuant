"""Application error contract used by the API and the workers."""

from __future__ import annotations

from typing import Any

from .enums import ErrorCode

_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.PURPOSE_NOT_ALLOWED: 403,
    ErrorCode.MODE_NOT_ARMED: 409,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.POLICY_EXPIRED: 409,
    ErrorCode.POLICY_INVALID: 422,
    ErrorCode.STALE_MARKET_DATA: 409,
    ErrorCode.RISK_REJECTED: 422,
    ErrorCode.LOCAL_RATE_LIMITED: 429,
    ErrorCode.PROVIDER_RATE_LIMITED: 429,
    ErrorCode.DEPENDENCY_BAD_RESPONSE: 502,
    ErrorCode.DEPENDENCY_UNAVAILABLE: 503,
    ErrorCode.SAFE_STATE_ACTIVE: 409,
    ErrorCode.RECONCILIATION_REQUIRED: 409,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.KILL_SWITCH_ACTIVE: 409,
}


class AppError(Exception):
    """A typed error that is safe to return to clients.

    ``details`` must never contain credentials or raw provider payloads.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        retry_after_ms: int | None = None,
        details: dict[str, Any] | None = None,
        trace_id: str | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms
        self.details = details or {}
        self.trace_id = trace_id
        self.http_status = http_status or _HTTP_STATUS.get(code, 400)

    def to_contract(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "retryable": self.retryable,
                "retry_after_ms": self.retry_after_ms,
                "trace_id": self.trace_id,
                "details": self.details,
            }
        }


class ValidationAppError(AppError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(ErrorCode.VALIDATION_ERROR, message, **kwargs)


class PolicyExpiredError(AppError):
    def __init__(self, message: str = "Strategy policy is expired.", **kwargs: Any) -> None:
        super().__init__(ErrorCode.POLICY_EXPIRED, message, **kwargs)


class PolicyInvalidError(AppError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(ErrorCode.POLICY_INVALID, message, **kwargs)


class DependencyUnavailableError(AppError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(
            ErrorCode.DEPENDENCY_UNAVAILABLE, message, retryable=True, **kwargs
        )


class DependencyBadResponseError(AppError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(ErrorCode.DEPENDENCY_BAD_RESPONSE, message, **kwargs)


class ProviderRateLimitedError(AppError):
    def __init__(self, message: str, retry_after_ms: int | None = None, **kwargs: Any) -> None:
        super().__init__(
            ErrorCode.PROVIDER_RATE_LIMITED,
            message,
            retryable=True,
            retry_after_ms=retry_after_ms,
            **kwargs,
        )


class PurposeNotAllowedError(AppError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(ErrorCode.PURPOSE_NOT_ALLOWED, message, **kwargs)


class ModeNotArmedError(AppError):
    def __init__(self, message: str = "Live mode is not armed.", **kwargs: Any) -> None:
        super().__init__(ErrorCode.MODE_NOT_ARMED, message, **kwargs)


class StaleMarketDataError(AppError):
    def __init__(self, message: str = "Market data is stale.", **kwargs: Any) -> None:
        super().__init__(ErrorCode.STALE_MARKET_DATA, message, **kwargs)


class RiskRejectedError(AppError):
    def __init__(self, message: str, failed_rules: list[dict[str, Any]], **kwargs: Any) -> None:
        details = dict(kwargs.pop("details", {}))
        details["rules"] = failed_rules
        super().__init__(ErrorCode.RISK_REJECTED, message, details=details, **kwargs)


class ReconciliationRequiredError(AppError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(ErrorCode.RECONCILIATION_REQUIRED, message, **kwargs)
