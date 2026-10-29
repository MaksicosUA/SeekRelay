# Adding a provider

A provider is a subclass of `SearchProvider` that translates `SearchQuery` into one API
call and maps the answer to `SearchResponse`. The base class already handles the HTTP
client, timeouts, transport errors and common status codes.

## Checklist

1. Create `src/search_gateway/providers/<name>.py`.
2. Set the class attributes `name` (unique, lower-case) and `supports_extract`.
3. Validate configuration in `__init__` and raise `ConfigurationError` for missing keys.
4. Implement `search()`; implement `extract()` if the API can fetch page content.
5. Map every `SearchQuery` field the API supports. For fields it cannot express:
   apply them client-side (`query.allows_url(url)` for domain filters) or log at DEBUG
   and skip. Never silently widen a filter.
6. Override `_raise_for_status()` only for non-standard status codes, then call
   `super()._raise_for_status(response)`.
7. Export it in `providers/__init__.py` and `search_gateway/__init__.py`; add env support
   in `config.py` if needed (and to `DEFAULT_PROVIDER_ORDER`).
8. Add tests with `httpx.MockTransport` (see `tests/test_brave.py`): request mapping,
   response mapping, each error code, empty results.
9. Document parameter mapping, status codes and cost in `docs/PROVIDERS.md`.

## Skeleton: Exa (semantic search)

The endpoint and fields below are illustrative; check the provider's current API
reference before shipping.

```python
from __future__ import annotations

import time
from typing import Any

from ..errors import ConfigurationError, ResponseFormatError
from ..models import SearchQuery, SearchResponse, SearchResult
from .base import SearchProvider


class ExaProvider(SearchProvider):
    name = "exa"

    def __init__(
        self, api_key: str, *, base_url: str = "https://api.exa.ai", **kwargs: Any
    ) -> None:
        if not api_key:
            raise ConfigurationError("Exa API key is empty")
        super().__init__(**kwargs)
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    async def search(self, query: SearchQuery) -> SearchResponse:
        started = time.perf_counter()
        payload: dict[str, Any] = {"query": query.text, "numResults": query.max_results}
        if query.include_domains:
            payload["includeDomains"] = list(query.include_domains)
        if query.exclude_domains:
            payload["excludeDomains"] = list(query.exclude_domains)

        response = await self._send(
            "POST", f"{self._base_url}/search", json=payload, headers={"x-api-key": self._api_key}
        )
        data = self._json(response)
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ResponseFormatError(self.name, "'results' missing")

        results = [
            SearchResult(
                title=str(item.get("title") or ""),
                url=str(item["url"]),
                snippet=str(item.get("text") or item.get("summary") or ""),
                provider=self.name,
                score=item.get("score"),
                published_at=item.get("publishedDate"),
            )
            for item in data["results"]
            if isinstance(item, dict) and item.get("url") and query.allows_url(str(item["url"]))
        ]
        return SearchResponse(
            query=query,
            provider=self.name,
            results=results[: query.max_results],
            latency_ms=self._elapsed_ms(started),
        )
```

## Extract-only providers

Jina Reader, Firecrawl or a self-hosted Crawl4AI can be added as providers that only
implement `extract()`. Give them a `search()` that raises `UnsupportedOperationError`;
the gateway then uses them for `extract` and falls through them for `search`.

When fetching pages yourself (Crawl4AI, Playwright), validate the target before the
request: resolve the host and reject loopback, private, link-local and cloud metadata
addresses, and limit response size and redirects. See SECURITY.md (T10).

## Design rules

* Keep the result contract small: put nothing provider-specific into `SearchResult`
  without adding it to the model and all docs.
* Raise the most specific exception: the gateway's retry, fallback and cooldown
  behaviour depends on it.
* No new runtime dependencies without a strong reason; `httpx` is enough for REST APIs.
