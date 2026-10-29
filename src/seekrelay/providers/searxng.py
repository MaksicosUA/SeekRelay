"""SearXNG (https://docs.searxng.org) - self-hosted metasearch, no per-request fee.

The instance must allow JSON output (``search.formats`` in ``settings.yml``); see
``deploy/searxng`` for a ready configuration.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any, NoReturn

import httpx

from ..errors import (
    AuthenticationError,
    ConfigurationError,
    ProviderUnavailableError,
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

__all__ = ["SearXNGProvider"]


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class SearXNGProvider(SearchProvider):
    """Search through a SearXNG instance's JSON API.

    Domain filters are applied as ``site:`` operators (honoured by most upstream engines)
    and then strictly on the client side.

    If the instance answers with zero results while some upstream engines were
    unresponsive (CAPTCHA, timeouts), the provider raises
    :class:`ProviderUnavailableError` so the relay can fall back, instead of
    returning a misleading empty result.

    Args:
        base_url: Instance root, e.g. ``http://127.0.0.1:8888``.
        engines: Optional list of engine names to query (default: instance defaults).
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
        except ValueError as exc:
            raise ConfigurationError(f"Invalid SearXNG base URL: {base_url!r}") from exc
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
            "categories": "news" if query.topic is Topic.NEWS else "general",
            "safesearch": str(self._safesearch),
            "pageno": "1",
        }
        if query.time_range is not None:
            params["time_range"] = query.time_range.value
        if query.language:
            params["language"] = self.build_language(query.language, query.country)
        if self._engines:
            params["engines"] = ",".join(self._engines)
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
                raise ProviderUnavailableError(
                    self.name,
                    "no results; upstream engines unresponsive: " + ", ".join(unresponsive),
                )

        return SearchResponse(
            query=query,
            provider=self.name,
            results=results,
            latency_ms=self._elapsed_ms(started),
        )

    @staticmethod
    def _unresponsive_engines(data: dict[str, Any]) -> list[str]:
        names: list[str] = []
        for entry in data.get("unresponsive_engines") or []:
            if isinstance(entry, (list, tuple)) and entry:
                names.append(f"{entry[0]} ({entry[1]})" if len(entry) > 1 else str(entry[0]))
            elif entry:
                names.append(str(entry))
        return names

    def _raise_for_status(self, response: httpx.Response) -> NoReturn:
        if response.status_code == 403:
            raise AuthenticationError(
                self.name,
                "HTTP 403: JSON output is disabled or the limiter blocked the client - add "
                "'json' to search.formats in settings.yml and check server.limiter",
                status_code=403,
            )
        super()._raise_for_status(response)
