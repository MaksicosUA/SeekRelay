"""The relay: one entry point over an ordered list of providers.

Request flow (see docs/ARCHITECTURE.md):

1. Build and validate a :class:`SearchQuery`.
2. PII guard: reject queries containing personal data before any network call.
3. Cache lookup (identical query within the TTL).
4. Rate limit, if one is set: refuse the call without sending anything.
5. Try providers in order. Each provider gets ``RetryPolicy.max_attempts`` attempts for
   retryable errors (5xx, 429, connection errors; not timeouts). Quota/auth failures put the
   provider into cooldown and the relay falls back to the next provider. The whole call
   stays within ``time_budget``.
6. De-duplicate results, store in cache, return the response together with the list
   of failed attempts (so callers can see that a fallback happened).

Along the way the relay counts what happened (:meth:`SeekRelay.stats`, :meth:`SeekRelay.status`)
and, with ``on_event``, reports each step as a :class:`~seekrelay.events.RelayEvent`.
"""

from __future__ import annotations

import asyncio
import enum
import functools
import logging
import os
import random
import sys
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
    InvalidRequestError,
    LimitExceededError,
    ProviderError,
    QueryRejectedError,
    QuotaExceededError,
    RateLimitError,
    TimeBudgetExceededError,
    UnsupportedOperationError,
)
from .events import (
    SEVERITY,
    EventHandler,
    EventKind,
    Monitor,
    Operation,
    ProviderStatus,
    RelayEvent,
    RelayStats,
)
from .models import (
    MAX_EXTRACT_URLS,
    ExtractResponse,
    ProviderAttempt,
    SearchQuery,
    SearchResponse,
    SearchResult,
    canonical_url,
    validate_public_url,
)
from .pii import PIIGuard
from .providers.base import CALL_DEADLINE, SearchProvider

__all__ = [
    "DEFAULT_CACHE_MAX_BYTES",
    "DEFAULT_TIME_BUDGET",
    "RetryPolicy",
    "SeekRelay",
    "dedupe_results",
]

logger = logging.getLogger("seekrelay")

T = TypeVar("T")

MAX_RETRY_AFTER_COOLDOWN = 3600.0
DEFAULT_TIME_BUDGET = 30.0
DEFAULT_CACHE_MAX_BYTES = 64 * 1024 * 1024
SLOW_HANDLER_SECONDS = 0.01

# Errors that say nothing about the provider's health: they never take it down.
_NOT_HEALTH = (InvalidRequestError, UnsupportedOperationError, TimeBudgetExceededError)

DEFAULT_PII_GUARD = PIIGuard()
"""Shared default guard (PIIGuard is immutable, so sharing is safe)."""


class _Unset(enum.Enum):
    """Marks an argument that was not passed, where ``None`` has a meaning of its own."""

    TOKEN = 0


_UNSET = _Unset.TOKEN


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Per-provider retry settings for retryable errors (5xx, 429, connection errors).

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


def _response_size(response: SearchResponse) -> int:
    """A rough size of a cached response in bytes: its strings plus a fixed part per result."""
    size = 1000 + sys.getsizeof(response.answer or "")
    for result in response.results:
        size += 400 + sys.getsizeof(result.title) + sys.getsizeof(result.url)
        size += sys.getsizeof(result.snippet) + sys.getsizeof(result.raw_content or "")
    return size


class _RateLimiter:
    """A token bucket: ``per_minute`` calls, refilled evenly. Two numbers of state."""

    __slots__ = ("_capacity", "_clock", "_rate", "_tokens", "_updated")

    def __init__(self, per_minute: int, clock: Callable[[], float]) -> None:
        self._capacity = float(per_minute)
        self._rate = per_minute / 60.0
        self._tokens = self._capacity
        self._clock = clock
        self._updated = clock()

    def take(self) -> float:
        """Take one call. Returns 0, or the seconds to wait when the bucket is empty."""
        now = self._clock()
        self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
        self._updated = now
        if self._tokens >= 1:
            self._tokens -= 1
            return 0.0
        return (1 - self._tokens) / self._rate


class SeekRelay:
    """Search through an ordered list of providers with fallback.

    Args:
        providers: Providers in priority order. Names must be unique.
        retry: Retry policy applied to each provider. Default: 2 attempts.
        cache_ttl: Seconds to cache non-empty search responses. ``None`` or ``0``
            disables the cache.
        cache_size: Maximum number of cached queries (LRU eviction).
        cache_max_bytes: Maximum estimated size of the cache (default 64 MiB); ``None``
            limits only the number of entries.
        pii_guard: Guard applied to queries, domain filters and URLs before sending.
            ``None`` disables it.
        fallback_on_invalid_request: Also fall back on HTTP 400/422. Off by default: a
            provider may have rejected the query deliberately, and sending it to the
            next provider would defeat that.
        fallback_on_empty: Try the next provider when one returns zero results.
        cooldown_seconds: How long to skip a provider after an auth or quota error. A
            rate limit whose ``retry_after`` exceeds ``retry.max_delay`` skips the provider
            for ``retry_after`` seconds instead, up to an hour. ``0`` disables both.
        time_budget: Upper bound in seconds for one ``search``/``extract`` call across all
            providers, retries and back-off (default 30). When it runs out, the running
            request is cancelled and the remaining providers are skipped, so the call ends
            with :class:`AllProvidersFailedError`. ``None`` means no limit. A call can pass
            its own value.
        max_requests_per_minute: Calls per minute that may reach the providers (cache hits
            are free). Over it, a call raises :class:`LimitExceededError` at once and sends
            nothing. ``None`` (default) means no limit.
        on_event: A plain function that receives a :class:`~seekrelay.events.RelayEvent`
            for each step (see docs/MONITORING.md). It runs inside the call, so it must
            return quickly; an error in it is logged once and never breaks the call.
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
        cache_max_bytes: int | None = DEFAULT_CACHE_MAX_BYTES,
        pii_guard: PIIGuard | None = DEFAULT_PII_GUARD,
        fallback_on_invalid_request: bool = False,
        fallback_on_empty: bool = False,
        cooldown_seconds: float = 300.0,
        time_budget: float | None = DEFAULT_TIME_BUDGET,
        max_requests_per_minute: int | None = None,
        on_event: EventHandler | None = None,
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
        if max_requests_per_minute is not None and max_requests_per_minute < 1:
            raise ValueError("max_requests_per_minute must be at least 1 (or None for no limit)")
        if cache_max_bytes is not None and cache_max_bytes < 1:
            raise ValueError("cache_max_bytes must be positive (or None for no limit)")

        self._providers = providers
        self._retry = retry or RetryPolicy()
        self._cache: TTLCache[SearchResponse] | None = (
            TTLCache(
                cache_ttl,
                cache_size,
                clock=clock,
                max_bytes=cache_max_bytes,
                sizeof=_response_size,
            )
            if cache_ttl
            else None
        )
        self._pii_guard = pii_guard
        self._fallback_on_invalid_request = fallback_on_invalid_request
        self._fallback_on_empty = fallback_on_empty
        self._cooldown_seconds = cooldown_seconds
        self._time_budget = time_budget
        self._limiter = (
            _RateLimiter(max_requests_per_minute, clock) if max_requests_per_minute else None
        )
        self._max_requests_per_minute = max_requests_per_minute
        self._on_event = on_event
        self._handler_error_logged = False
        self._slow_handler_logged = False
        self._sleep = sleep
        self._clock = clock
        self._cooldown_until: dict[str, float] = {}
        self._monitor = Monitor(names, clock)
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

    def stats(self) -> RelayStats:
        """A snapshot of the counters since the relay was created (see docs/MONITORING.md)."""
        return self._monitor.stats()

    def status(self) -> dict[str, ProviderStatus]:
        """The state of each provider right now, in priority order."""
        return {
            p.name: self._monitor.status(p.name, self.cooldown_remaining(p.name))
            for p in self._providers
        }

    def __repr__(self) -> str:
        return f"SeekRelay(providers={self.provider_names!r})"

    # ------------------------------------------------------------------ search

    async def search(
        self,
        query: str | SearchQuery,
        *,
        time_budget: float | _Unset | None = _UNSET,
        **options: Any,
    ) -> SearchResponse:
        """Search the web.

        Args:
            query: Query text, or a ready :class:`SearchQuery`.
            time_budget: This call's own limit in seconds instead of the relay's
                ``time_budget``; ``None`` means no limit.
            **options: :class:`SearchQuery` fields (``max_results``, ``topic``,
                ``time_range``, ``include_domains`` ...) when ``query`` is a string.

        Raises:
            QueryRejectedError: the query or a domain filter contains personal data or
                a secret.
            LimitExceededError: ``max_requests_per_minute`` is used up.
            InvalidRequestError: a provider rejected the request (no fallback by default).
            AllProvidersFailedError: every provider failed or was in cooldown.
        """
        self._ensure_open()
        budget = self._resolve_budget(time_budget)
        if isinstance(query, SearchQuery):
            if options:
                raise TypeError("Pass options inside SearchQuery or as keywords, not both")
            search_query = query
        else:
            search_query = SearchQuery(text=query, **options)
        self._monitor.searches += 1

        if self._pii_guard is not None:
            try:
                self._pii_guard.check(search_query.text)
                for domain in (*search_query.include_domains, *search_query.exclude_domains):
                    self._pii_guard.check(domain, what="domain filter")
            except QueryRejectedError:
                self._blocked("search")
                raise

        if self._cache is not None:
            cached = self._cache.get(search_query)
            if cached is not None:
                logger.debug("cache hit for %r", search_query.text)
                self._monitor.cache_hits += 1
                # A copy, without the original call's fallback history or latency.
                hit = replace(
                    cached,
                    results=list(cached.results),
                    from_cache=True,
                    attempts=(),
                    latency_ms=0.0,
                )
                self._emit(
                    "served",
                    "search",
                    provider=hit.provider,
                    results=len(hit.results),
                    latency_ms=0.0,
                    from_cache=True,
                )
                return hit

        self._take_rate_limit("search")
        attempts: list[ProviderAttempt] = []
        empty_response: SearchResponse | None = None
        deadline = self._deadline(budget)
        for provider in self._providers:
            if self._skip_if_cooling_down(provider, attempts) or self._skip_if_out_of_time(
                provider, deadline, budget, attempts
            ):
                continue
            response = await self._call_provider(
                provider,
                "search",
                functools.partial(provider.search, search_query),
                deadline,
                budget,
                attempts,
            )
            if response is None:
                continue

            response.results = dedupe_results(response.results)
            self._succeeded(provider, "search", response.latency_ms, backup=bool(attempts))
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
                self._emit("fallback", "search", provider=provider.name)
            self._emit(
                "served",
                "search",
                provider=provider.name,
                results=len(response.results),
                latency_ms=response.latency_ms,
            )
            return response

        if empty_response is not None:
            empty_response.attempts = tuple(attempts)
            self._emit(
                "served",
                "search",
                provider=empty_response.provider,
                results=0,
                latency_ms=empty_response.latency_ms,
            )
            return empty_response
        raise self._all_failed("search", attempts)

    # ------------------------------------------------------------------ extract

    async def extract(
        self,
        urls: str | Sequence[str],
        *,
        advanced: bool = False,
        time_budget: float | _Unset | None = _UNSET,
    ) -> ExtractResponse:
        """Fetch clean page content (Markdown) for one or more URLs.

        Only providers with ``supports_extract`` take part (so far only Tavily).
        ``time_budget`` works as in :meth:`search`.

        Raises:
            ValueError: a URL is not an absolute public http(s) URL or is longer than
                :data:`~seekrelay.models.MAX_URL_LENGTH`, or there are more than
                :data:`~seekrelay.models.MAX_EXTRACT_URLS` URLs.
            QueryRejectedError: a URL contains personal data or a secret.
            LimitExceededError: ``max_requests_per_minute`` is used up.
            ConfigurationError: no configured provider supports extraction.
            AllProvidersFailedError: every capable provider failed.
        """
        self._ensure_open()
        budget = self._resolve_budget(time_budget)
        url_list = [urls] if isinstance(urls, str) else list(urls)
        cleaned = list(dict.fromkeys(validate_public_url(u) for u in url_list))
        if not cleaned:
            raise ValueError("At least one URL is required")
        if len(cleaned) > MAX_EXTRACT_URLS:
            raise ValueError(f"At most {MAX_EXTRACT_URLS} URLs per extract call")
        self._monitor.extracts += 1
        deadline = self._deadline(budget)
        if self._pii_guard is not None:
            try:
                for url in cleaned:
                    self._pii_guard.check_url(url)
                    await asyncio.sleep(0)  # a long URL list must not stall the loop in one piece
            except QueryRejectedError:
                self._blocked("extract")
                raise

        capable = [p for p in self._providers if p.supports_extract]
        if not capable:
            raise ConfigurationError(
                "None of the configured providers supports extract "
                f"(configured: {', '.join(self.provider_names)})"
            )

        self._take_rate_limit("extract")
        attempts: list[ProviderAttempt] = []
        for provider in capable:
            if self._skip_if_cooling_down(provider, attempts) or self._skip_if_out_of_time(
                provider, deadline, budget, attempts
            ):
                continue
            response = await self._call_provider(
                provider,
                "extract",
                functools.partial(provider.extract, cleaned, advanced=advanced),
                deadline,
                budget,
                attempts,
            )
            if response is None:
                continue
            self._succeeded(provider, "extract", response.latency_ms, backup=bool(attempts))
            if response.batch_error is not None:
                # Some batches failed; an exhausted quota there must still pause the provider.
                self._batch_failed(provider, response.batch_error)
            response.attempts = tuple(attempts)
            if attempts:
                self._emit("fallback", "extract", provider=provider.name)
            self._emit(
                "served",
                "extract",
                provider=provider.name,
                results=len(response.pages),
                failed=len(response.failed),
                latency_ms=response.latency_ms,
            )
            return response
        raise self._all_failed("extract", attempts)

    # ------------------------------------------------------------------ internals

    def _resolve_budget(self, value: float | _Unset | None) -> float | None:
        if isinstance(value, _Unset):
            return self._time_budget
        if value is not None and value <= 0:
            raise ValueError("time_budget must be positive (or None for no limit)")
        return value

    @staticmethod
    def _deadline(budget: float | None) -> float | None:
        """Event-loop time by which the current call must finish (``None``: no budget)."""
        if budget is None:
            return None
        return asyncio.get_running_loop().time() + budget

    def _take_rate_limit(self, operation: Operation) -> None:
        if self._limiter is None:
            return
        wait = self._limiter.take()
        if not wait:
            return
        self._monitor.limit_exceeded += 1
        self._emit("limit_exceeded", operation, error="LimitExceededError", seconds=wait)
        raise LimitExceededError(
            f"more than {self._max_requests_per_minute} calls per minute; "
            f"the next one can go in {wait:.1f}s",
            retry_after=wait,
        )

    async def _call_provider(
        self,
        provider: SearchProvider,
        operation: Operation,
        call: Callable[[], Awaitable[T]],
        deadline: float | None,
        budget: float | None,
        attempts: list[ProviderAttempt],
    ) -> T | None:
        """Run one provider. ``None`` means it failed and the next one may try."""
        self._monitor.called(provider.name)
        try:
            return await self._within_budget(provider, operation, call, deadline, budget)
        except ProviderError as exc:
            self._provider_failed(provider, operation, exc, attempts)
            if not exc.fallback and not self._fallback_on_invalid_request:
                raise
            return None

    async def _within_budget(
        self,
        provider: SearchProvider,
        operation: Operation,
        call: Callable[[], Awaitable[T]],
        deadline: float | None,
        budget: float | None,
    ) -> T:
        """Run ``call`` with retries; cancel it when the call's time budget runs out."""
        if deadline is None:
            return await self._with_retry(provider, operation, call)
        # Lets a provider return finished work just before the deadline (Tavily's batches).
        token = CALL_DEADLINE.set(deadline)
        try:
            remaining = deadline - asyncio.get_running_loop().time()
            return await asyncio.wait_for(self._with_retry(provider, operation, call), remaining)
        except asyncio.TimeoutError:
            # _with_retry turns every exception from a provider into ProviderError, so a
            # TimeoutError here can only come from wait_for itself.
            raise TimeBudgetExceededError(
                provider.name, f"cancelled: the {budget:g}s time budget ran out"
            ) from None
        finally:
            CALL_DEADLINE.reset(token)

    def _skip_if_out_of_time(
        self,
        provider: SearchProvider,
        deadline: float | None,
        budget: float | None,
        attempts: list[ProviderAttempt],
    ) -> bool:
        if deadline is None or asyncio.get_running_loop().time() < deadline:
            return False
        attempts.append(
            ProviderAttempt(
                provider.name,
                "TimeBudget",
                f"skipped: the {budget:g}s time budget is used up",
            )
        )
        return True

    async def _with_retry(
        self, provider: SearchProvider, operation: Operation, call: Callable[[], Awaitable[T]]
    ) -> T:
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
                self._monitor.retried(provider.name)
                self._emit(
                    "retry",
                    operation,
                    provider=provider.name,
                    error=type(exc).__name__,
                    status_code=exc.status_code,
                    attempt=attempt + 1,
                    seconds=delay,
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

    def _cooldown_for(self, exc: ProviderError) -> float | None:
        """Seconds to skip a provider after ``exc``, or ``None`` for no cooldown."""
        if not self._cooldown_seconds:
            return None
        if isinstance(exc, (AuthenticationError, QuotaExceededError)):
            return self._cooldown_seconds
        if (
            isinstance(exc, RateLimitError)
            and exc.retry_after is not None
            and exc.retry_after > self._retry.max_delay
        ):
            # The provider said when to come back; asking earlier only costs a round
            # trip per call (e.g. SearXNG with all upstream engines suspended).
            # The ceiling is for a value that would switch the provider off for good.
            return min(exc.retry_after, MAX_RETRY_AFTER_COOLDOWN)
        return None

    def _provider_failed(
        self,
        provider: SearchProvider,
        operation: Operation,
        exc: ProviderError,
        attempts: list[ProviderAttempt],
    ) -> None:
        error = type(exc).__name__
        attempts.append(
            ProviderAttempt(
                provider=provider.name,
                error=error,
                message=exc.message,
                status_code=exc.status_code,
            )
        )
        went_down = self._monitor.failed(
            provider.name, error, health=not isinstance(exc, _NOT_HEALTH)
        )
        # Class and status only: the message is the provider's text, which may even fake a
        # closing tag of the LLM block when an agent reads stderr together with stdout.
        status = f" (HTTP {exc.status_code})" if exc.status_code else ""
        logger.warning("provider %s failed: %s%s", provider.name, error, status)
        logger.debug("provider %s said: %s", provider.name, one_line(exc.message))
        self._emit(
            "failure", operation, provider=provider.name, error=error, status_code=exc.status_code
        )
        went_down = self._pause(provider, operation, exc) or went_down
        if went_down:
            self._emit(
                "provider_down",
                operation,
                provider=provider.name,
                error=error,
                status_code=exc.status_code,
            )

    def _batch_failed(self, provider: SearchProvider, exc: ProviderError) -> None:
        """Part of an extract failed while the rest succeeded: not a failed call."""
        error = type(exc).__name__
        self._monitor.error_seen(provider.name, error)
        status = f" (HTTP {exc.status_code})" if exc.status_code else ""
        logger.warning("provider %s failed on some URLs: %s%s", provider.name, error, status)
        self._emit(
            "failure", "extract", provider=provider.name, error=error, status_code=exc.status_code
        )
        if self._pause(provider, "extract", exc):
            self._emit(
                "provider_down",
                "extract",
                provider=provider.name,
                error=error,
                status_code=exc.status_code,
            )

    def _pause(self, provider: SearchProvider, operation: Operation, exc: ProviderError) -> bool:
        """Start a cooldown if ``exc`` calls for one. True if the provider has just gone down."""
        seconds = self._cooldown_for(exc)
        if seconds is None:
            return False
        self._cooldown_until[provider.name] = self._clock() + seconds
        self._emit(
            "cooldown", operation, provider=provider.name, error=type(exc).__name__, seconds=seconds
        )
        return self._monitor.paused(provider.name)

    def _succeeded(
        self, provider: SearchProvider, operation: Operation, latency_ms: float, *, backup: bool
    ) -> None:
        outage = self._monitor.succeeded(provider.name, latency_ms, backup=backup)
        if outage is not None:
            self._emit("provider_up", operation, provider=provider.name, seconds=outage)

    def _blocked(self, operation: Operation) -> None:
        self._monitor.blocked += 1
        self._emit("blocked", operation, error="QueryRejectedError")

    def _all_failed(
        self, operation: Operation, attempts: list[ProviderAttempt]
    ) -> AllProvidersFailedError:
        self._monitor.all_failed += 1
        self._emit("all_failed", operation, attempt=len(attempts))
        return AllProvidersFailedError(attempts)

    def _emit(self, kind: EventKind, operation: Operation, **fields: Any) -> None:
        """Hand an event to ``on_event``. Without a handler no event is even created."""
        handler = self._on_event
        if handler is None:
            return
        event = RelayEvent(kind, SEVERITY[kind], operation, at=time.time(), **fields)
        started = time.perf_counter()
        try:
            handler(event)
        except Exception as exc:
            self._monitor.handler_errors += 1
            if not self._handler_error_logged:
                self._handler_error_logged = True
                logger.warning(
                    "on_event handler raised %s; the call goes on (logged once)",
                    type(exc).__name__,
                )
            logger.debug("traceback of that error", exc_info=True)
            return
        elapsed = time.perf_counter() - started
        if elapsed > SLOW_HANDLER_SECONDS and not self._slow_handler_logged:
            self._slow_handler_logged = True
            logger.warning(
                "on_event handler took %.0f ms; it runs inside each call, so hand slow work "
                "to a queue (logged once)",
                elapsed * 1000,
            )

    # ------------------------------------------------------------------ sync API

    def search_sync(
        self,
        query: str | SearchQuery,
        *,
        time_budget: float | _Unset | None = _UNSET,
        **options: Any,
    ) -> SearchResponse:
        """Blocking version of :meth:`search` for scripts and notebooks without asyncio."""
        return self._run_sync(self.search(query, time_budget=time_budget, **options))

    def extract_sync(
        self,
        urls: str | Sequence[str],
        *,
        advanced: bool = False,
        time_budget: float | _Unset | None = _UNSET,
    ) -> ExtractResponse:
        """Blocking version of :meth:`extract`."""
        return self._run_sync(self.extract(urls, advanced=advanced, time_budget=time_budget))

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
