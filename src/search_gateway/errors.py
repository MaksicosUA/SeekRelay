"""Exception hierarchy.

Every error raised by the library derives from :class:`SearchGatewayError`, so callers
can catch a single base class.

Provider errors carry two class-level flags that drive the gateway's behaviour:

* ``retryable`` - the *same* provider may succeed if we try again shortly
  (timeouts, 5xx, rate limits).
* ``fallback``  - *another* provider may succeed with the same request
  (quota exhausted, bad key, provider down). Request-level problems such as
  HTTP 400/422 do not fall back by default: the same query would usually fail
  everywhere, and it could be a query that one provider refused on purpose
  (for example because it contained personal data).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ProviderAttempt

__all__ = [
    "AllProvidersFailedError",
    "AuthenticationError",
    "ConfigurationError",
    "InvalidRequestError",
    "ProviderError",
    "ProviderUnavailableError",
    "QueryRejectedError",
    "QuotaExceededError",
    "RateLimitError",
    "ResponseFormatError",
    "SearchGatewayError",
    "UnsupportedOperationError",
]


class SearchGatewayError(Exception):
    """Base class for every error raised by search-gateway."""


class ConfigurationError(SearchGatewayError):
    """Missing or invalid configuration (no providers, empty API key, bad URL, ...)."""


class QueryRejectedError(SearchGatewayError):
    """The request was blocked locally, before any network call (e.g. by the PII guard).

    ``findings`` lists the *kinds* of data that were detected (``"email"``,
    ``"payment_card"``...). The matched values themselves are never stored, so the
    exception is safe to log.
    """

    def __init__(self, message: str, *, findings: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.findings: tuple[str, ...] = tuple(findings)


class ProviderError(SearchGatewayError):
    """A provider failed to serve a request."""

    retryable: bool = False
    fallback: bool = True

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.message = message
        self.status_code = status_code
        self.request_id = request_id


class AuthenticationError(ProviderError):
    """HTTP 401/403: the API key is missing, wrong or revoked, or access is denied.

    The gateway puts the provider into cooldown, because retrying will not help.
    """


class QuotaExceededError(ProviderError):
    """Plan, credit or pay-as-you-go limit reached (Tavily 432/433, HTTP 402).

    The gateway puts the provider into cooldown and falls back to the next one.
    """


class RateLimitError(ProviderError):
    """HTTP 429: too many requests in a short window."""

    retryable = True

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status_code: int | None = 429,
        request_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(provider, message, status_code=status_code, request_id=request_id)
        self.retry_after = retry_after


class ProviderUnavailableError(ProviderError):
    """Timeouts, connection errors, HTTP 5xx or a temporarily degraded provider."""

    retryable = True


class InvalidRequestError(ProviderError):
    """HTTP 400/422 or a request that cannot be expressed for this provider."""

    fallback = False


class ResponseFormatError(ProviderError):
    """The provider answered 2xx but the payload had an unexpected shape."""


class UnsupportedOperationError(ProviderError):
    """The provider cannot do this: an operation it lacks (``extract`` on Brave) or a
    request it cannot express (e.g. a query too long for Brave once domain operators
    are added). Other providers may still succeed, so the gateway falls back.
    """


class AllProvidersFailedError(SearchGatewayError):
    """Every configured provider failed or was skipped.

    ``attempts`` records what happened with each provider, in order.
    """

    def __init__(self, attempts: Sequence[ProviderAttempt]) -> None:
        self.attempts: tuple[ProviderAttempt, ...] = tuple(attempts)
        if self.attempts:
            summary = "; ".join(f"{a.provider}: {a.error}" for a in self.attempts)
        else:
            summary = "no providers available"
        super().__init__(f"All providers failed ({summary})")
