"""Abstract provider and the shared HTTP / error-mapping plumbing."""

from __future__ import annotations

import logging
import math
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, ClassVar, NoReturn

import httpx

from .._version import __version__
from ..errors import (
    AuthenticationError,
    InvalidRequestError,
    ProviderError,
    ProviderUnavailableError,
    QuotaExceededError,
    RateLimitError,
    ResponseFormatError,
    UnsupportedOperationError,
)
from ..models import ExtractResponse, SearchQuery, SearchResponse

__all__ = ["DEFAULT_TIMEOUT", "DEFAULT_USER_AGENT", "SearchProvider", "parse_retry_after"]

logger = logging.getLogger("seekrelay")

DEFAULT_TIMEOUT = 20.0
DEFAULT_USER_AGENT = f"seekrelay/{__version__}"

_ERROR_KEYS = ("error", "detail", "message", "msg", "description")


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header (seconds or HTTP-date) into seconds."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        seconds = moment.timestamp() - time.time()
    if not math.isfinite(seconds):
        return None
    return max(seconds, 0.0)


def _find_message(data: Any, depth: int = 0) -> str | None:
    """Dig a human-readable message out of the many error shapes APIs use."""
    if depth > 3:
        return None
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in _ERROR_KEYS:
            if key in data:
                found = _find_message(data[key], depth + 1)
                if found:
                    return found
    if isinstance(data, list) and data:
        return _find_message(data[0], depth + 1)
    return None


class SearchProvider(ABC):
    """Base class for all providers.

    Subclasses implement :meth:`search` (and optionally :meth:`extract`) and may refine
    :meth:`_raise_for_status` for provider-specific status codes.

    A provider owns its ``httpx.AsyncClient`` unless one is injected (handy for tests,
    proxies or connection sharing); an injected client is never closed by the provider.
    """

    name: ClassVar[str] = "base"
    supports_extract: ClassVar[bool] = False

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": user_agent},
            follow_redirects=False,
        )

    # ------------------------------------------------------------------ public API

    @abstractmethod
    async def search(self, query: SearchQuery) -> SearchResponse:
        """Run a search and return normalised results."""

    async def extract(self, urls: Sequence[str], *, advanced: bool = False) -> ExtractResponse:
        """Fetch clean page content. Not every provider supports it."""
        raise UnsupportedOperationError(self.name, "extract is not supported by this provider")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> SearchProvider:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def __repr__(self) -> str:  # never include secrets
        return f"{type(self).__name__}(name={self.name!r})"

    # ------------------------------------------------------------------ helpers

    async def _send(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Send a request and translate transport errors and HTTP errors into our hierarchy."""
        try:
            response = await self._client.request(
                method,
                url,
                params=params,
                json=json,
                headers=headers,
                timeout=self._timeout,
                # Also for injected clients: a redirect could carry the API key header
                # (e.g. X-Subscription-Token) to another host.
                follow_redirects=False,
            )
        except httpx.TimeoutException as exc:
            raise ProviderUnavailableError(
                self.name, f"request timed out after {self._timeout:g}s"
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderUnavailableError(
                self.name, f"network error ({type(exc).__name__})"
            ) from exc
        except httpx.DecodingError as exc:
            raise ResponseFormatError(self.name, "response body could not be decoded") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                self.name, f"HTTP client error ({type(exc).__name__})"
            ) from exc

        if response.status_code >= 400:
            self._raise_for_status(response)
        if response.is_redirect:
            raise ProviderError(
                self.name,
                f"unexpected redirect ({response.status_code}) to "
                f"{response.headers.get('Location', '?')}; check the base URL",
                status_code=response.status_code,
            )
        return response

    def _error_message(self, response: httpx.Response) -> str:
        try:
            message = _find_message(response.json())
        except ValueError:
            message = response.text.strip() or None
        message = message or response.reason_phrase or "error"
        return " ".join(message.split())[:300]

    def _raise_for_status(self, response: httpx.Response) -> NoReturn:
        """Map an HTTP error onto the exception hierarchy. Subclasses extend this."""
        status = response.status_code
        message = f"HTTP {status}: {self._error_message(response)}"
        request_id = response.headers.get("x-request-id")
        kwargs: dict[str, Any] = {"status_code": status, "request_id": request_id}
        if status in (401, 403):
            raise AuthenticationError(self.name, message, **kwargs)
        if status == 402:
            raise QuotaExceededError(self.name, message, **kwargs)
        if status == 429:
            raise RateLimitError(
                self.name,
                message,
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
                **kwargs,
            )
        if status in (400, 413, 414, 422):
            raise InvalidRequestError(self.name, message, **kwargs)
        if status == 408 or status >= 500:
            raise ProviderUnavailableError(self.name, message, **kwargs)
        raise ProviderError(self.name, message, **kwargs)

    def _json(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise ResponseFormatError(
                self.name, "response is not valid JSON", status_code=response.status_code
            ) from exc

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return (time.perf_counter() - started) * 1000.0
