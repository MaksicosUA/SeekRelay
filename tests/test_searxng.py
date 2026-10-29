from __future__ import annotations

import httpx
import pytest

from seekrelay import (
    AuthenticationError,
    ConfigurationError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    SearchQuery,
    SearXNGProvider,
    SeekRelay,
)
from seekrelay.providers.searxng import SEARXNG_SUSPENDED_RETRY_AFTER

from .conftest import FakeClock, FakeProvider, FakeSleep, Recorder

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
        "safesearch": "2",
        "pageno": "1",
        "time_range": "year",
        "language": "uk-UA",
        # No "categories": SearXNG would add the category's engines to this list.
        "engines": "duckduckgo,brave",
    }
    assert [r.url for r in response.results] == [
        "https://github.com/searxng/searxng",
        "https://docs.searxng.org/",
    ]
    assert response.results[0].source_engines == ("duckduckgo", "brave")
    assert response.results[1].source_engines == ("google",)
    assert response.results[0].score == 2.5


@pytest.mark.parametrize(
    ("language", "country", "expected"),
    [
        ("uk", None, "uk"),
        ("uk", "ua", "uk-UA"),
        ("en-us", None, "en-US"),
        ("en-gb", "us", "en-GB"),
        ("pt-br", "br", "pt-BR"),
        ("zh-hans", None, "zh"),
        ("zh-hans", "cn", "zh-CN"),
        ("es-419", None, "es"),
    ],
)
def test_language_param_is_valid_for_searxng(
    language: str, country: str | None, expected: str
) -> None:
    # SearXNG accepts only ^[a-z]{2,3}(-[a-zA-Z]{2})?$ and answers 400 otherwise.
    query = SearchQuery("q", language=language, country=country)
    assert provider(Recorder(httpx.Response(200))).build_params(query)["language"] == expected


def test_categories_only_without_pinned_engines() -> None:
    rec = Recorder(httpx.Response(200))
    default = provider(rec).build_params(SearchQuery("q"))
    assert default["categories"] == "general" and "engines" not in default
    news = provider(rec, engines=["brave"]).build_params(SearchQuery("q", topic="news"))  # type: ignore[arg-type]
    assert news["categories"] == "news" and "engines" not in news


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


BLOCKED = {
    "results": [],
    # Shapes seen on a live instance (docs/LIVE_CHECK.md).
    "unresponsive_engines": [
        ["brave", "Suspended: too many requests"],
        ["duckduckgo", "CAPTCHA"],
        ["google", "access denied"],
    ],
}


async def test_all_engines_blocked_is_a_rate_limit() -> None:
    rec = Recorder(httpx.Response(200, json=BLOCKED))
    with pytest.raises(RateLimitError, match=r"brave \(Suspended: too many requests\)") as info:
        await provider(rec).search(SearchQuery("q"))
    assert info.value.retry_after == SEARXNG_SUSPENDED_RETRY_AFTER
    assert info.value.status_code is None


@pytest.mark.parametrize(
    ("unresponsive", "expected"),
    [
        ([["google cse", "Suspended: too many requests"]], RateLimitError),
        # Seen live: a crashed engine next to a suspended one still means "come back later".
        (
            [["duckduckgo", "unexpected crash"], ["google cse", "Suspended: too many requests"]],
            RateLimitError,
        ),
        ([["duckduckgo", "unexpected crash"]], ProviderUnavailableError),
        (
            [["brave", "Suspended: too many requests"], ["bing", "timeout"]],
            ProviderUnavailableError,
        ),
        ([["bing"]], ProviderUnavailableError),
    ],
)
async def test_unresponsive_engines_classification(
    unresponsive: list[list[str]], expected: type[Exception]
) -> None:
    rec = Recorder(httpx.Response(200, json={"results": [], "unresponsive_engines": unresponsive}))
    with pytest.raises(expected):
        await provider(rec).search(SearchQuery("q"))


async def test_blocked_instance_is_skipped_until_suspension_ends(
    clock: FakeClock, sleep: FakeSleep
) -> None:
    rec = Recorder(httpx.Response(200, json=BLOCKED))
    backup = FakeProvider("backup", ["https://example.com/"])
    relay = SeekRelay([provider(rec), backup], cache_ttl=None, clock=clock, sleep=sleep)

    response = await relay.search("q")
    assert response.provider == "backup"
    assert response.attempts[0].error == "RateLimitError"
    assert len(rec.requests) == 1  # not retried
    assert sleep.delays == []

    response = await relay.search("q2")
    assert len(rec.requests) == 1  # skipped while the engines are suspended
    assert response.attempts[0].error == "Cooldown"

    clock.advance(SEARXNG_SUSPENDED_RETRY_AFTER + 1)
    await relay.search("q3")
    assert len(rec.requests) == 2


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
