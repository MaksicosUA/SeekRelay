"""Brave Search API (https://brave.com/search/api/) - independent web index.

API reference: https://api-dashboard.search.brave.com/documentation
"""

from __future__ import annotations

import html
import logging
import re
import time
from typing import Any, NoReturn

import httpx

from ..errors import (
    ConfigurationError,
    QuotaExceededError,
    RateLimitError,
    ResponseFormatError,
    UnsupportedOperationError,
)
from ..models import (
    MAX_QUERY_LENGTH,
    MAX_RESULTS_LIMIT,
    SearchQuery,
    SearchResponse,
    SearchResult,
    TimeRange,
    Topic,
    is_http_url,
    optional_str,
)
from .base import SearchProvider, parse_retry_after

__all__ = ["BRAVE_BASE_URL", "BRAVE_COUNTRIES", "BRAVE_LANGUAGES", "BraveProvider"]

logger = logging.getLogger("seekrelay")

BRAVE_BASE_URL = "https://api.search.brave.com/res/v1"

_FRESHNESS = {
    TimeRange.DAY: "pd",
    TimeRange.WEEK: "pw",
    TimeRange.MONTH: "pm",
    TimeRange.YEAR: "py",
}
_SAFESEARCH_VALUES = ("off", "moderate", "strict")

# fmt: off
# Values accepted by Brave's `country` parameter (documentation, 2026). Other codes
# (e.g. UA, KZ) are rejected by the API with 422, so they are skipped instead.
BRAVE_COUNTRIES: frozenset[str] = frozenset({
    "ar", "au", "at", "be", "br", "ca", "cl", "dk", "fi", "fr", "de", "hk", "in", "id",
    "it", "jp", "kr", "my", "mx", "nl", "nz", "no", "cn", "pl", "pt", "ph", "ru", "sa",
    "za", "es", "se", "ch", "tw", "tr", "gb", "us",
})
# Brave's own `search_lang` codes (note "jp", "zh-hans", "pt-br").
BRAVE_LANGUAGES: frozenset[str] = frozenset({
    "ar", "eu", "bn", "bg", "ca", "zh-hans", "zh-hant", "hr", "cs", "da", "nl", "en",
    "en-gb", "et", "fi", "fr", "gl", "de", "gu", "he", "hi", "hu", "is", "it", "jp", "kn",
    "ko", "lv", "lt", "ms", "ml", "mr", "nb", "pl", "pt-br", "pt-pt", "pa", "ro", "ru",
    "sr", "sk", "sl", "es", "sv", "ta", "te", "th", "tr", "uk", "vi",
})
# fmt: on
_LANGUAGE_ALIASES = {
    "ja": "jp",
    "zh": "zh-hans",
    "zh-cn": "zh-hans",
    "zh-tw": "zh-hant",
    "pt": "pt-pt",
    "no": "nb",
    "en-us": "en",
}
_TAG_RE = re.compile(r"<[^<>]+>")
_MAX_QUERY_WORDS = 50


def _within_limits(text: str) -> bool:
    return len(text) <= MAX_QUERY_LENGTH and len(text.split()) <= _MAX_QUERY_WORDS


def _clean(text: Any) -> str:
    """Strip highlight markup (``<strong>``) and HTML entities from Brave snippets."""
    return html.unescape(_TAG_RE.sub("", str(text or ""))).strip()


class BraveProvider(SearchProvider):
    """Brave web and news search.

    Brave has no server-side domain filter, so domain filters are applied in two layers:
    ``site:`` (several domains joined with ``OR``) and ``-site:`` operators in the query,
    which are best effort, and a strict client-side filter on the results. When a filter
    is active the provider over-fetches 20 results so that enough survive filtering.

    Brave does not extract page content; use Tavily (or another extractor) for that.

    Args:
        api_key: Subscription token from the Brave Search API dashboard.
        safesearch: ``off`` | ``moderate`` | ``strict``.
        extra_snippets: Request up to 5 additional excerpts per result (appended to the
            snippet). Availability depends on the Brave plan.
        **kwargs: ``timeout``, ``client``, ``user_agent``.
    """

    name = "brave"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BRAVE_BASE_URL,
        safesearch: str = "moderate",
        extra_snippets: bool = True,
        **kwargs: Any,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ConfigurationError("Brave API key is empty")
        if safesearch not in _SAFESEARCH_VALUES:
            raise ConfigurationError(f"safesearch must be one of {', '.join(_SAFESEARCH_VALUES)}")
        super().__init__(**kwargs)
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._safesearch = safesearch
        self._extra_snippets = extra_snippets

    # ------------------------------------------------------------------ request

    @staticmethod
    def build_query_text(query: SearchQuery) -> str:
        excludes = [f"-site:{domain}" for domain in query.exclude_domains]
        without_sites = " ".join([query.text, *excludes])
        if not query.include_domains:
            return without_sites
        sites = " OR ".join(f"site:{domain}" for domain in query.include_domains)
        text = " ".join([query.text, sites, *excludes])
        if len(query.include_domains) > 1 and not _within_limits(text):
            # Too many domains for one query. The client-side filter still applies, so
            # this only costs results, while failing would lose the search altogether.
            return without_sites
        return text

    def build_params(self, query: SearchQuery) -> dict[str, str]:
        text = self.build_query_text(query)
        if not _within_limits(text):
            # Not the caller's fault: another provider can still run this query.
            raise UnsupportedOperationError(
                self.name,
                f"query with domain operators exceeds Brave limits "
                f"({MAX_QUERY_LENGTH} chars / {_MAX_QUERY_WORDS} words)",
            )
        count = MAX_RESULTS_LIMIT if query.has_domain_filter else query.max_results
        params: dict[str, str] = {
            "q": text,
            "count": str(count),
            "safesearch": self._safesearch,
        }
        if query.topic is Topic.GENERAL:
            params["text_decorations"] = "false"
        if query.time_range is not None:
            params["freshness"] = _FRESHNESS[query.time_range]
        if query.country:
            if query.country in BRAVE_COUNTRIES:
                params["country"] = query.country.upper()
            else:
                logger.debug("brave: country %r not supported, parameter skipped", query.country)
        if query.language:
            language = _LANGUAGE_ALIASES.get(query.language, query.language)
            if language in BRAVE_LANGUAGES:
                params["search_lang"] = language
            else:
                logger.debug("brave: language %r not supported, parameter skipped", language)
        if self._extra_snippets:
            params["extra_snippets"] = "true"
        return params

    # ------------------------------------------------------------------ search

    async def search(self, query: SearchQuery) -> SearchResponse:
        endpoint = "news/search" if query.topic is Topic.NEWS else "web/search"
        started = time.perf_counter()
        response = await self._send(
            "GET",
            f"{self._base_url}/{endpoint}",
            params=self.build_params(query),
            headers={"X-Subscription-Token": self._api_key, "Accept": "application/json"},
        )
        data = self._json(response)
        items = self._result_items(data, news=query.topic is Topic.NEWS)

        results: list[SearchResult] = []
        for item in items:
            if not isinstance(item, dict) or not is_http_url(str(item.get("url") or "")):
                continue
            url = str(item["url"]).strip()
            if not query.allows_url(url):
                continue
            snippet = _clean(item.get("description"))
            raw_extras = item.get("extra_snippets")
            extras = [_clean(s) for s in raw_extras if s] if isinstance(raw_extras, list) else []
            if extras:
                snippet = " … ".join([snippet, *extras]) if snippet else " … ".join(extras)
            results.append(
                SearchResult(
                    title=_clean(item.get("title")),
                    url=url,
                    snippet=snippet,
                    provider=self.name,
                    published_at=optional_str(item.get("page_age") or item.get("age")),
                )
            )
            if len(results) >= query.max_results:
                break

        return SearchResponse(
            query=query,
            provider=self.name,
            results=results,
            latency_ms=self._elapsed_ms(started),
            request_id=optional_str(response.headers.get("x-request-id")),
        )

    def _result_items(self, data: Any, *, news: bool) -> list[Any]:
        if not isinstance(data, dict):
            raise ResponseFormatError(self.name, "unexpected response shape")
        if news:
            items = data.get("results")
            if items is None and isinstance(data.get("news"), dict):
                items = data["news"].get("results")
        else:
            web = data.get("web")
            # A response without a "web" section is a valid "no results" answer.
            items = web.get("results") if isinstance(web, dict) else []
        if items is None:
            items = []
        if not isinstance(items, list):
            raise ResponseFormatError(self.name, "'results' is not a list")
        return items

    # ------------------------------------------------------------------ errors

    def _raise_for_status(self, response: httpx.Response) -> NoReturn:
        if response.status_code == 429:
            if self._monthly_quota_exhausted(response):
                raise QuotaExceededError(
                    self.name,
                    f"HTTP 429: plan quota exhausted - {self._error_message(response)}",
                    status_code=429,
                )
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            if retry_after is None:
                # Brave reports per-window resets as "1, 1419704" (seconds); the first
                # value is the shortest window.
                reset = response.headers.get("X-RateLimit-Reset", "").split(",")[0]
                retry_after = parse_retry_after(reset)
            raise RateLimitError(
                self.name,
                f"HTTP 429: {self._error_message(response)}",
                retry_after=retry_after,
            )
        super()._raise_for_status(response)

    @staticmethod
    def _monthly_quota_exhausted(response: httpx.Response) -> bool:
        """Brave signals an exhausted plan as 429 too; tell it apart from a burst limit.

        Either the error code says so (``QUOTA_LIMITED``) or the last (longest) window in
        ``X-RateLimit-Remaining`` (e.g. ``"1, 0"``) has nothing left.
        """
        try:
            error = response.json().get("error", {})
            code = str(error.get("code", "")) if isinstance(error, dict) else ""
        except (ValueError, AttributeError):
            code = ""
        if code.upper() == "QUOTA_LIMITED":
            return True
        windows = [v.strip() for v in response.headers.get("X-RateLimit-Remaining", "").split(",")]
        return len(windows) > 1 and windows[-1] == "0"
