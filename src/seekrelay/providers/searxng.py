"""SearXNG (https://docs.searxng.org) - self-hosted metasearch, no per-request fee.

The instance must allow JSON output (``search.formats`` in ``settings.yml``); see
``deploy/searxng`` for a ready configuration.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from typing import Any, NoReturn

import httpx

from ..errors import (
    AuthenticationError,
    ConfigurationError,
    ProviderUnavailableError,
    RateLimitError,
    ResponseFormatError,
)
from ..models import (
    SearchQuery,
    SearchResponse,
    SearchResult,
    Topic,
    is_http_url,
    optional_str,
    validate_url,
)
from .base import SearchProvider

__all__ = ["SEARXNG_SUSPENDED_RETRY_AFTER", "SearXNGProvider"]

SEARXNG_SUSPENDED_RETRY_AFTER = 180.0
"""Seconds to skip an instance that cannot answer until an engine suspension ends.

SearXNG's default ``search.suspended_times`` keeps blocked engines out for 180 s ("too
many requests", "access denied") up to 3600 s (CAPTCHA). The shortest is used, so the
instance is tried again as soon as it may work.
"""

# Reasons in ``unresponsive_engines`` that mean "blocked for minutes", not "slow right now".
_BLOCKED_REASONS = ("suspended", "captcha", "too many requests", "access denied")


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):  # OverflowError: an integer of 400 digits
        return None
    return number if math.isfinite(number) else None  # JSON has no NaN or Infinity


def _instance_blocked(unresponsive: Sequence[tuple[str, str]]) -> bool:
    """True if no engine can answer before a suspension ends: at least one engine is
    suspended, rate-limited or behind a CAPTCHA and every other one crashed (a live
    instance reported ``duckduckgo (unexpected crash)`` next to suspended engines).
    Timeouts and other errors may clear on a retry, so they keep this False.
    """
    reasons = [reason.lower() for _, reason in unresponsive]
    blocked = [any(marker in r for marker in _BLOCKED_REASONS) for r in reasons]
    return any(blocked) and all(b or "crash" in r for b, r in zip(blocked, reasons, strict=True))


class SearXNGProvider(SearchProvider):
    """Search through a SearXNG instance's JSON API.

    Domain filters are applied as ``site:`` operators (honoured by most upstream engines)
    and then strictly on the client side.

    If the instance answers with zero results while some upstream engines were
    unresponsive, the provider raises an error so the relay can fall back, instead of
    returning a misleading empty result: :class:`RateLimitError` with
    ``retry_after=SEARXNG_SUSPENDED_RETRY_AFTER`` when the engines are suspended,
    rate-limited or behind a CAPTCHA (the relay then skips the instance for that long),
    otherwise :class:`ProviderUnavailableError` (timeouts and other transient errors,
    retried).

    Args:
        base_url: Instance root, e.g. ``http://127.0.0.1:8888``.
        engines: Optional list of engine names for web search; they replace the instance's
            default engines. News searches (``topic="news"``) keep using the instance's
            news engines.
        safesearch: 0 (off), 1 (moderate), 2 (strict).
        headers: Extra headers, e.g. ``Authorization`` for an instance behind a reverse
            proxy with basic auth.
        **kwargs: ``timeout``, ``client``, ``user_agent``.
    """

    name = "searxng"

    def __init__(
        self,
        base_url: str,
        *,
        engines: Sequence[str] | None = None,
        safesearch: int = 1,
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            self._base_url = validate_url(base_url).rstrip("/")
        except ValueError:
            # Without the value: a base URL can hold a password.
            raise ConfigurationError("SearXNG base URL is not an absolute http(s) URL") from None
        if safesearch not in (0, 1, 2):
            raise ConfigurationError("safesearch must be 0, 1 or 2")
        super().__init__(**kwargs)
        self._engines = [e.strip() for e in engines or [] if e.strip()]
        self._safesearch = safesearch
        self._headers = {"Accept": "application/json", **(headers or {})}

    def build_params(self, query: SearchQuery) -> dict[str, str]:
        text = query.text
        if query.include_domains:
            text += " " + " OR ".join(f"site:{d}" for d in query.include_domains)
        params: dict[str, str] = {
            "q": text,
            "format": "json",
            "safesearch": str(self._safesearch),
            "pageno": "1",
        }
        if self._engines and query.topic is Topic.GENERAL:
            # Not together with "categories": SearXNG would add every engine of the
            # category to this list, so the pinned engines would not restrict anything.
            params["engines"] = ",".join(self._engines)
        else:
            params["categories"] = "news" if query.topic is Topic.NEWS else "general"
        if query.time_range is not None:
            params["time_range"] = query.time_range.value
        if query.language:
            params["language"] = self.build_language(query.language, query.country)
        return params

    @staticmethod
    def build_language(language: str, country: str | None) -> str:
        """Return a code SearXNG accepts: ``xx`` or ``xx-YY`` (``uk`` + ``ua`` -> ``uk-UA``).

        SearXNG answers 400 for anything else, and a 400 does not fall back, so a region
        that is already part of ``language`` wins over ``country`` and subtags that are
        not two-letter regions (``zh-hans``, ``es-419``) are dropped.
        """
        base, _, region = language.partition("-")
        if len(region) == 2 and region.isalpha():
            return f"{base}-{region.upper()}"
        if country:
            return f"{base}-{country.upper()}"
        return base

    async def search(self, query: SearchQuery) -> SearchResponse:
        started = time.perf_counter()
        response = await self._send(
            "GET",
            f"{self._base_url}/search",
            params=self.build_params(query),
            headers=self._headers,
        )
        data = self._json(response)
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ResponseFormatError(self.name, "'results' list missing in response")

        results: list[SearchResult] = []
        seen: set[str] = set()
        for item in data["results"]:
            if not isinstance(item, dict) or not is_http_url(str(item.get("url") or "")):
                continue
            url = str(item["url"]).strip()
            if url in seen or not query.allows_url(url):
                continue
            seen.add(url)
            engines = item.get("engines")
            if not isinstance(engines, list):
                engines = [item["engine"]] if item.get("engine") else []
            results.append(
                SearchResult(
                    title=optional_str(item.get("title")) or "",
                    url=url,
                    snippet=optional_str(item.get("content")) or "",
                    provider=self.name,
                    score=_as_float(item.get("score")),
                    published_at=optional_str(item.get("publishedDate")),
                    source_engines=tuple(str(e) for e in engines),
                )
            )
            if len(results) >= query.max_results:
                break

        if not results:
            unresponsive = self._unresponsive_engines(data)
            if unresponsive:
                summary = ", ".join(
                    f"{engine} ({reason})" if reason else engine for engine, reason in unresponsive
                )
                if _instance_blocked(unresponsive):
                    # Retrying within the suspension only costs another round trip.
                    raise RateLimitError(
                        self.name,
                        "no results; upstream engines blocked: " + summary,
                        status_code=None,
                        retry_after=SEARXNG_SUSPENDED_RETRY_AFTER,
                    )
                raise ProviderUnavailableError(
                    self.name, "no results; upstream engines unresponsive: " + summary
                )

        return SearchResponse(
            query=query,
            provider=self.name,
            results=results,
            latency_ms=self._elapsed_ms(started),
        )

    @staticmethod
    def _unresponsive_engines(data: dict[str, Any]) -> list[tuple[str, str]]:
        """``(engine, reason)`` pairs; the reason is empty when the instance gives none."""
        engines: list[tuple[str, str]] = []
        for entry in data.get("unresponsive_engines") or []:
            if isinstance(entry, (list, tuple)) and entry:
                engines.append((str(entry[0]), str(entry[1]) if len(entry) > 1 else ""))
            elif entry:
                engines.append((str(entry), ""))
        return engines

    def _raise_for_status(self, response: httpx.Response) -> NoReturn:
        if response.status_code == 403:
            raise AuthenticationError(
                self.name,
                "HTTP 403: JSON output is disabled or the limiter blocked the client - add "
                "'json' to search.formats in settings.yml and check server.limiter",
                status_code=403,
            )
        super()._raise_for_status(response)
