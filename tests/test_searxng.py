from __future__ import annotations

import httpx
import pytest

from search_gateway import (
    AuthenticationError,
    ConfigurationError,
    ProviderError,
    ProviderUnavailableError,
    SearchQuery,
    SearXNGProvider,
)

from .conftest import Recorder

RESULTS = {
    "query": "q",
    "results": [
        {
            "url": "https://github.com/searxng/searxng",
            "title": "SearXNG",
            "content": "Privacy-respecting metasearch",
            "engine": "duckduckgo",
            "engines": ["duckduckgo", "brave"],
            "score": 2.5,
            "publishedDate": None,
        },
        {"url": "https://github.com/searxng/searxng", "title": "dup", "content": ""},
        {"url": "https://spam.com/", "title": "spam", "content": "", "engine": "bing"},
        {
            "url": "https://docs.searxng.org/",
            "title": "Docs",
            "content": "docs",
            "engine": "google",
        },
    ],
    "unresponsive_engines": [],
}


def provider(rec: Recorder, **kwargs: object) -> SearXNGProvider:
    return SearXNGProvider("http://127.0.0.1:8888/", client=rec.client(), **kwargs)  # type: ignore[arg-type]


def test_validates_base_url() -> None:
    with pytest.raises(ConfigurationError):
        SearXNGProvider("localhost:8888")
    with pytest.raises(ConfigurationError):
        SearXNGProvider("http://127.0.0.1:8888", safesearch=5)


async def test_params_and_parsing() -> None:
    rec = Recorder(httpx.Response(200, json=RESULTS))
    query = SearchQuery(
        "metasearch",
        max_results=5,
        time_range="year",
        language="uk",
        country="ua",
        exclude_domains=["spam.com"],
    )  # type: ignore[arg-type]
    response = await provider(rec, engines=["duckduckgo", " brave "], safesearch=2).search(query)

    assert str(rec.last.url).startswith("http://127.0.0.1:8888/search?")
    assert dict(rec.last.url.params) == {
        "q": "metasearch",
        "format": "json",
        "categories": "general",
        "safesearch": "2",
        "pageno": "1",
        "time_range": "year",
        "language": "uk-UA",
        "engines": "duckduckgo,brave",
    }
    assert [r.url for r in response.results] == [
        "https://github.com/searxng/searxng",
        "https://docs.searxng.org/",
    ]
    assert response.results[0].source_engines == ("duckduckgo", "brave")
    assert response.results[1].source_engines == ("google",)
    assert response.results[0].score == 2.5


async def test_include_domains_become_site_operators() -> None:
    rec = Recorder(httpx.Response(200, json=RESULTS))
    query = SearchQuery("q", include_domains=["github.com", "docs.searxng.org"], topic="news")  # type: ignore[arg-type]
    response = await provider(rec).search(query)
    params = dict(rec.last.url.params)
    assert params["q"] == "q site:github.com OR site:docs.searxng.org"
    assert params["categories"] == "news"
    assert all("spam" not in r.url for r in response.results)


async def test_forbidden_explains_json_format() -> None:
    rec = Recorder(httpx.Response(403, text="Forbidden"))
    with pytest.raises(AuthenticationError, match=r"search\.formats"):
        await provider(rec).search(SearchQuery("q"))


async def test_empty_with_unresponsive_engines_is_unavailable() -> None:
    body = {"results": [], "unresponsive_engines": [["google", "CAPTCHA"], ["bing", "timeout"]]}
    rec = Recorder(httpx.Response(200, json=body))
    with pytest.raises(ProviderUnavailableError, match=r"google \(CAPTCHA\)"):
        await provider(rec).search(SearchQuery("q"))


async def test_genuinely_empty_result() -> None:
    rec = Recorder(httpx.Response(200, json={"results": [], "unresponsive_engines": []}))
    response = await provider(rec).search(SearchQuery("q"))
    assert response.results == []


async def test_redirect_is_reported() -> None:
    rec = Recorder(httpx.Response(302, headers={"Location": "https://login.example/"}))
    with pytest.raises(ProviderError, match="redirect"):
        await provider(rec).search(SearchQuery("q"))


async def test_custom_headers_are_sent() -> None:
    rec = Recorder(httpx.Response(200, json=RESULTS))
    await provider(rec, headers={"Authorization": "Basic abc"}).search(SearchQuery("q"))
    assert rec.last.headers["Authorization"] == "Basic abc"
    assert rec.last.headers["Accept"] == "application/json"
