"""The relay: one entry point over an ordered list of providers.

Request flow (see docs/ARCHITECTURE.md):

1. Build and validate a :class:`SearchQuery`.
2. PII guard: reject queries containing personal data before any network call.
3. Cache lookup (identical query within the TTL).
4. Try providers in order. Each provider gets ``RetryPolicy.max_attempts`` attempts for
   retryable errors (timeouts, 5xx, 429). Quota/auth failures put the provider into
   cooldown and the relay falls back to the next provider.
5. De-duplicate results, store in cache, return the response together with the list
   of failed attempts (so callers can see that a fallback happened).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import random
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import TracebackType
from typing import Any, TypeVar

from ._text import one_line
from .cache import TTLCache
from .errors import (
    AllProvidersFailedError,
    AuthenticationError,
    ConfigurationError,
    ProviderError,
    ProviderUnavailableError,
    QuotaExceededError,
    RateLimitError,
)
from .models import (
    ExtractResponse,
    ProviderAttempt,
    SearchQuery,
    SearchResponse,
    SearchResult,
    canonical_url,
    validate_url,
)
from .pii import PIIGuard
from .providers.base import SearchProvider

__all__ = ["RetryPolicy", "SeekRelay", "dedupe_results"]

logger = logging.getLogger("seekrelay")

T = TypeVar("T")

DEFAULT_PII_GUARD = PIIGuard()
"""Shared default guard (PIIGuard is immutable, so sharing is safe)."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Per-provider retry settings for retryable errors (timeouts, 5xx, 429).

    Attributes:
        max_attempts: Attempts per provider, including the first one (1 = no retries).
        base_delay: First back-off delay in seconds; doubles on each retry.
        max_delay: Upper bound for a single delay. If a provider asks us to wait longer
            (``Retry-After``), the relay does not wait and falls back instead.
        jitter: Random +/- fraction applied to computed delays.
    """

    max_attempts: int = 2
    base_delay: float = 0.5
    max_delay: float = 8.0
    jitter: float = 0.2

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay < 0 or self.max_delay < 0:
            raise ValueError("delays must not be negative")
        if not 0 <= self.jitter <= 1:
            raise ValueError("jitter must be between 0 and 1")

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        """Seconds to wait after the ``attempt``-th failed attempt (1-based)."""
        if retry_after is not None:
            return min(max(retry_after, 0.0), self.max_delay)
        delay = self.base_delay * 2.0 ** (attempt - 1)
        if self.jitter:
            delay *= 1 + random.uniform(-self.jitter, self.jitter)
        return min(max(delay, 0.0), self.max_delay)


def dedupe_results(results: Iterable[SearchResult]) -> list[SearchResult]:
    """Drop results whose canonical URL was already seen (first occurrence wins)."""
    seen: set[str] = set()
    unique: list[SearchResult] = []
    for result in results:
        key = canonical_url(result.url)
        if key in seen:
            continue
        seen.add(key)
        unique.append(result)
    return unique


class SeekRelay:
    """Search through an ordered list of providers with fallback.

    Args:
        providers: Providers in priority order. Names must be unique.
        retry: Retry policy applied to each provider. Default: 2 attempts.
        cache_ttl: Seconds to cache non-empty search responses. ``None`` or ``0``
            disables the cache.
        cache_size: Maximum number of cached queries (LRU eviction).
        pii_guard: Guard applied to queries, domain filters and URLs before sending.
            ``None`` disables it.
        fallback_on_invalid_request: Also fall back on HTTP 400/422. Off by default: a
            provider may have rejected the query deliberately, and sending it to the
            next provider would defeat that.
        fallback_on_empty: Try the next provider when one returns zero results.
        cooldown_seconds: How long to skip a provider after an auth or quota error. A
            rate limit whose ``retry_after`` exceeds ``retry.max_delay`` skips the provider
            for ``retry_after`` seconds instead. ``0`` disables both.
        time_budget: Upper bound in seconds for one ``search``/``extract`` call across all
            providers, retries and back-off. When it runs out, the running request is
            cancelled and the remaining providers are skipped, so the call ends with
            :class:`AllProvidersFailedError`. ``None`` (default) means no limit.
        sleep, clock: Injection points for tests.

    Use either the async API (``await search(...)``) or the sync API
    (``search_sync(...)``) on one instance, not both.
    """

    def __init__(
        self,
        providers: Sequence[SearchProvider],
        *,
        retry: RetryPolicy | None = None,
        cache_ttl: float | None = 600.0,
        cache_size: int = 256,
        pii_guard: PIIGuard | None = DEFAULT_PII_GUARD,
        fallback_on_invalid_request: bool = False,
        fallback_on_empty: bool = False,
        cooldown_seconds: float = 300.0,
        time_budget: float | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        providers = list(providers)
        if not providers:
            raise ConfigurationError("At least one provider is required")
        names = [p.name for p in providers]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ConfigurationError(f"Duplicate providers: {', '.join(duplicates)}")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must not be negative")
        if time_budget is not None and time_budget <= 0:
            raise ValueError("time_budget must be positive (or None for no limit)")

        self._providers = providers
        self._retry = retry or RetryPolicy()
        self._cache: TTLCache[SearchResponse] | None = (
            TTLCache(cache_ttl, cache_size, clock=clock) if cache_ttl else None
        )
        self._pii_guard = pii_guard
        self._fallback_on_invalid_request = fallback_on_invalid_request
        self._fallback_on_empty = fallback_on_empty
        self._cooldown_seconds = cooldown_seconds
        self._time_budget = time_budget
        self._sleep = sleep
        self._clock = clock
        self._cooldown_until: dict[str, float] = {}
        self._sync_loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        env_file: str | os.PathLike[str] | None = None,
        **overrides: Any,
    ) -> SeekRelay:
        """Build a relay from environment variables (see docs/CONFIGURATION.md).

        ``env_file`` names a ``.env`` file to read as well; real environment variables win.
        """
        from .config import build_relay_from_env

        return build_relay_from_env(env, env_file=env_file, **overrides)

    # ------------------------------------------------------------------ introspection

    @property
    def providers(self) -> tuple[SearchProvider, ...]:
        return tuple(self._providers)

    @property
    def provider_names(self) -> list[str]:
        return [p.name for p in self._providers]

    def cooldown_remaining(self, provider_name: str) -> float:
        """Seconds until ``provider_name`` is tried again (0 if it is available)."""
        until = self._cooldown_until.get(provider_name)
        if until is None:
            return 0.0
        remaining = until - self._clock()
        if remaining <= 0:
            del self._cooldown_until[provider_name]
            return 0.0
        return remaining

    def reset_cooldowns(self) -> None:
        self._cooldown_until.clear()

    def clear_cache(self) -> None:
        if self._cache is not None:
            self._cache.clear()

    def __repr__(self) -> str:
        return f"SeekRelay(providers={self.provider_names!r})"

    # ------------------------------------------------------------------ search

    async def search(self, query: str | SearchQuery, **options: Any) -> SearchResponse:
        """Search the web.

        Args:
            query: Query text, or a ready :class:`SearchQuery`.
            **options: :class:`SearchQuery` fields (``max_results``, ``topic``,
                ``time_range``, ``include_domains`` ...) when ``query`` is a string.

        Raises:
            QueryRejectedError: the query or a domain filter contains personal data or
                a secret.
            InvalidRequestError: a provider rejected the request (no fallback by default).
            AllProvidersFailedError: every provider failed or was in cooldown.
        """
        self._ensure_open()
        if isinstance(query, SearchQuery):
            if options:
                raise TypeError("Pass options inside SearchQuery or as keywords, not both")
            search_query = query
        else:
            search_query = SearchQuery(text=query, **options)

        if self._pii_guard is not None:
            self._pii_guard.check(search_query.text)
            for domain in (*search_query.include_domains, *search_query.exclude_domains):
                self._pii_guard.check(domain, what="domain filter")

        if self._cache is not None:
            cached = self._cache.get(search_query)
            if cached is not None:
                logger.debug("cache hit for %r", search_query.text)
                # A copy, without the original call's fallback history.
                return replace(cached, results=list(cached.results), from_cache=True, attempts=())

        attempts: list[ProviderAttempt] = []
        empty_response: SearchResponse | None = None
        deadline = self._deadline()
        for provider in self._providers:
            if self._skip_if_cooling_down(provider, attempts) or self._skip_if_out_of_time(
                provider, deadline, attempts
            ):
                continue
            try:
                response = await self._within_budget(
                    provider, functools.partial(provider.search, search_query), deadline
                )
            except ProviderError as exc:
                self._record_failure(provider, exc, attempts)
                if not exc.fallback and not self._fallback_on_invalid_request:
                    raise
                continue

            response.results = dedupe_results(response.results)
            if not response.results and self._fallback_on_empty:
                attempts.append(
                    ProviderAttempt(provider.name, "EmptyResult", "provider returned no results")
                )
                empty_response = empty_response or response
                continue

            response.attempts = tuple(attempts)
            if self._cache is not None and response.results:
                # Store a private copy: callers may mutate the response they receive.
                self._cache.set(
                    search_query, replace(response, results=list(response.results), attempts=())
                )
            if attempts:
                logger.info("search served by %s after fallback", provider.name)
            return response

        if empty_response is not None:
            empty_response.attempts = tuple(attempts)
            return empty_response
        raise AllProvidersFailedError(attempts)

    # ------------------------------------------------------------------ extract

    async def extract(
        self, urls: str | Sequence[str], *, advanced: bool = False
    ) -> ExtractResponse:
        """Fetch clean page content (Markdown) for one or more URLs.

        Only providers with ``supports_extract`` take part (so far only Tavily).

        Raises:
            ValueError: a URL is not an absolute http(s) URL, or is longer than
                :data:`~seekrelay.models.MAX_URL_LENGTH`.
            QueryRejectedError: a URL contains personal data or a secret.
            ConfigurationError: no configured provider supports extraction.
            AllProvidersFailedError: every capable provider failed.
        """
        self._ensure_open()
        url_list = [urls] if isinstance(urls, str) else list(urls)
        cleaned = list(dict.fromkeys(validate_url(u) for u in url_list))
        if not cleaned:
            raise ValueError("At least one URL is required")
        deadline = self._deadline()
        if self._pii_guard is not None:
            for url in cleaned:
                self._pii_guard.check_url(url)
                await asyncio.sleep(0)  # a long URL list must not stall the loop in one piece

        capable = [p for p in self._providers if p.supports_extract]
        if not capable:
            raise ConfigurationError(
                "None of the configured providers supports extract "
                f"(configured: {', '.join(self.provider_names)})"
            )

        attempts: list[ProviderAttempt] = []
        for provider in capable:
            if self._skip_if_cooling_down(provider, attempts) or self._skip_if_out_of_time(
                provider, deadline, attempts
            ):
                continue
            try:
                response = await self._within_budget(
                    provider,
                    functools.partial(provider.extract, cleaned, advanced=advanced),
                    deadline,
                )
            except ProviderError as exc:
                self._record_failure(provider, exc, attempts)
                if not exc.fallback and not self._fallback_on_invalid_request:
                    raise
                continue
            response.attempts = tuple(attempts)
            return response
        raise AllProvidersFailedError(attempts)

    # ------------------------------------------------------------------ internals

    def _deadline(self) -> float | None:
        """Event-loop time by which the current call must finish (``None``: no budget)."""
        if self._time_budget is None:
            return None
        return asyncio.get_running_loop().time() + self._time_budget

    async def _within_budget(
        self, provider: SearchProvider, call: Callable[[], Awaitable[T]], deadline: float | None
    ) -> T:
        """Run ``call`` with retries; cancel it when the call's time budget runs out."""
        if deadline is None:
            return await self._with_retry(provider, call)
        remaining = deadline - asyncio.get_running_loop().time()
        try:
            return await asyncio.wait_for(self._with_retry(provider, call), remaining)
        except asyncio.TimeoutError:
            # _with_retry turns every exception from a provider into ProviderError, so a
            # TimeoutError here can only come from wait_for itself.
            raise ProviderUnavailableError(
                provider.name, f"cancelled: the {self._time_budget:g}s time budget ran out"
            ) from None

    def _skip_if_out_of_time(
        self, provider: SearchProvider, deadline: float | None, attempts: list[ProviderAttempt]
    ) -> bool:
        if deadline is None or asyncio.get_running_loop().time() < deadline:
            return False
        attempts.append(
            ProviderAttempt(
                provider.name,
                "TimeBudget",
                f"skipped: the {self._time_budget:g}s time budget is used up",
            )
        )
        return True

    async def _with_retry(self, provider: SearchProvider, call: Callable[[], Awaitable[T]]) -> T:
        attempt = 1
        while True:
            try:
                return await call()
            except ProviderError as exc:
                if not exc.retryable or attempt >= self._retry.max_attempts:
                    raise
                retry_after = exc.retry_after if isinstance(exc, RateLimitError) else None
                if retry_after is not None and retry_after > self._retry.max_delay:
                    logger.info(
                        "%s asks to wait %.0fs; falling back instead of waiting",
                        provider.name,
                        retry_after,
                    )
                    raise
                delay = self._retry.delay(attempt, retry_after)
                logger.info(
                    "%s: %s - retry %d/%d in %.2fs",
                    provider.name,
                    one_line(exc.message),
                    attempt + 1,
                    self._retry.max_attempts,
                    delay,
                )
                await self._sleep(delay)
                attempt += 1
            except Exception as exc:
                # A bug or an unexpected payload in one provider must not take the whole
                # search down: log it and let the relay fall back. The traceback holds local
                # paths and whatever the exception quotes, so it stays at DEBUG.
                logger.warning(
                    "provider %s raised an unexpected %s", provider.name, type(exc).__name__
                )
                logger.debug("traceback of that error", exc_info=True)
                raise ProviderError(
                    provider.name, f"unexpected {type(exc).__name__}: {str(exc)[:200]}"
                ) from exc

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("SeekRelay is closed; create a new instance")

    def _skip_if_cooling_down(
        self, provider: SearchProvider, attempts: list[ProviderAttempt]
    ) -> bool:
        remaining = self.cooldown_remaining(provider.name)
        if remaining <= 0:
            return False
        attempts.append(
            ProviderAttempt(
                provider.name,
                "Cooldown",
                f"skipped for another {remaining:.0f}s after an auth, quota or rate-limit error",
            )
        )
        return True

    def _record_failure(
        self, provider: SearchProvider, exc: ProviderError, attempts: list[ProviderAttempt]
    ) -> None:
        attempts.append(
            ProviderAttempt(
                provider=provider.name,
                error=type(exc).__name__,
                message=exc.message,
                status_code=exc.status_code,
            )
        )
        if self._cooldown_seconds:
            if isinstance(exc, (AuthenticationError, QuotaExceededError)):
                self._cooldown_until[provider.name] = self._clock() + self._cooldown_seconds
            elif (
                isinstance(exc, RateLimitError)
                and exc.retry_after is not None
                and exc.retry_after > self._retry.max_delay
            ):
                # The provider said when to come back; asking earlier only costs a round
                # trip per call (e.g. SearXNG with all upstream engines suspended).
                self._cooldown_until[provider.name] = self._clock() + exc.retry_after
        # Class and status only: the message is the provider's text, which may even fake a
        # closing tag of the LLM block when an agent reads stderr together with stdout.
        status = f" (HTTP {exc.status_code})" if exc.status_code else ""
        logger.warning("provider %s failed: %s%s", provider.name, type(exc).__name__, status)
        logger.debug("provider %s said: %s", provider.name, one_line(exc.message))

    # ------------------------------------------------------------------ sync API

    def search_sync(self, query: str | SearchQuery, **options: Any) -> SearchResponse:
        """Blocking version of :meth:`search` for scripts and notebooks without asyncio."""
        return self._run_sync(self.search(query, **options))

    def extract_sync(self, urls: str | Sequence[str], *, advanced: bool = False) -> ExtractResponse:
        """Blocking version of :meth:`extract`."""
        return self._run_sync(self.extract(urls, advanced=advanced))

    def _run_sync(self, coro: Coroutine[Any, Any, T]) -> T:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            coro.close()
            raise RuntimeError(
                "The sync API cannot run inside an event loop; use 'await relay.search(...)'"
            )
        # One private loop for the whole lifetime of the instance, so the HTTP connection
        # pool (bound to the loop that created it) stays valid between calls.
        if self._sync_loop is None or self._sync_loop.is_closed():
            self._sync_loop = asyncio.new_event_loop()
        return self._sync_loop.run_until_complete(coro)

    # ------------------------------------------------------------------ lifecycle

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.gather(*(p.aclose() for p in self._providers), return_exceptions=True)

    def close(self) -> None:
        """Close providers when the sync API was used."""
        if self._sync_loop is not None and not self._sync_loop.is_closed():
            try:
                self._sync_loop.run_until_complete(self.aclose())
            finally:
                self._sync_loop.close()
                self._sync_loop = None
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.aclose())
        else:
            raise RuntimeError("Inside an event loop use 'await relay.aclose()'")

    async def __aenter__(self) -> SeekRelay:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def __enter__(self) -> SeekRelay:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
