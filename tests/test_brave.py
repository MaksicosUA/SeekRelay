from __future__ import annotations

import httpx
import pytest

from search_gateway import (
    AuthenticationError,
    BraveProvider,
    ConfigurationError,
    InvalidRequestError,
    QuotaExceededError,
    RateLimitError,
    SearchQuery,
    UnsupportedOperationError,
)

from .conftest import Recorder

WEB_OK = {
    "query": {"original": "q"},
    "web": {
        "type": "search",
        "results": [
            {
                "title": "Python <strong>Jobs</strong> &amp; more",
                "url": "https://djinni.co/jobs/1",
                "description": "Remote <strong>Python</strong> role",
                "extra_snippets": ["Salary <strong>3k</strong>", "Kyiv"],
                "page_age": "2026-10-20T10:00:00",
            },
            {"title": "Other", "url": "https://spam.com/x", "description": "spam"},
            {
                "title": "Dou",
                "url": "https://dou.ua/vacancy",
                "description": "dou",
                "age": "2 days ago",
            },
        ],
    },
}


def provider(rec: Recorder, **kwargs: object) -> BraveProvider:
    return BraveProvider("brave-key", client=rec.client(), **kwargs)  # type: ignore[arg-type]


def test_validates_config() -> None:
    with pytest.raises(ConfigurationError):
        BraveProvider("")
    with pytest.raises(ConfigurationError):
        BraveProvider("k", safesearch="maybe")


async def test_web_search_params_and_parsing() -> None:
    rec = Recorder(httpx.Response(200, json=WEB_OK))
    query = SearchQuery(
        "python remote", max_results=3, time_range="week", country="de", language="uk"
    )
    response = await provider(rec).search(query)

    request = rec.last
    assert request.url.path == "/res/v1/web/search"
    assert request.headers["X-Subscription-Token"] == "brave-key"
    params = dict(request.url.params)
    assert params == {
        "q": "python remote",
        "count": "3",
        "safesearch": "moderate",
        "text_decorations": "false",
        "freshness": "pw",
        "country": "DE",
        "search_lang": "uk",
        "extra_snippets": "true",
    }
    first = response.results[0]
    assert first.title == "Python Jobs & more"
    assert first.snippet == "Remote Python role … Salary 3k … Kyiv"
    assert first.published_at == "2026-10-20T10:00:00"
    assert response.results[2].published_at == "2 days ago"


async def test_domain_filters_use_operators_and_client_side_filtering() -> None:
    rec = Recorder(httpx.Response(200, json=WEB_OK))
    query = SearchQuery(
        "python",
        max_results=5,
        include_domains=["djinni.co", "dou.ua"],
        exclude_domains=["spam.com"],
    )
    response = await provider(rec).search(query)
    params = dict(rec.last.url.params)
    assert params["q"] == "python -site:spam.com"  # several includes -> no OR operator
    assert params["count"] == "20"  # over-fetch so enough results survive filtering
    assert [r.url for r in response.results] == [
        "https://djinni.co/jobs/1",
        "https://dou.ua/vacancy",
    ]

    await provider(rec).search(SearchQuery("python", include_domains=["djinni.co"]))
    assert dict(rec.last.url.params)["q"] == "python site:djinni.co"


async def test_max_results_is_respected() -> None:
    rec = Recorder(httpx.Response(200, json=WEB_OK))
    response = await provider(rec).search(SearchQuery("q", max_results=1))
    assert len(response.results) == 1


async def test_news_endpoint() -> None:
    body = {
        "results": [
            {"title": "News", "url": "https://news.example/a", "description": "d", "age": "1h"}
        ]
    }
    rec = Recorder(httpx.Response(200, json=body))
    response = await provider(rec, extra_snippets=False).search(SearchQuery("ai", topic="news"))  # type: ignore[arg-type]
    assert rec.last.url.path == "/res/v1/news/search"
    params = dict(rec.last.url.params)
    assert "text_decorations" not in params
    assert "extra_snippets" not in params
    assert response.results[0].url == "https://news.example/a"


async def test_no_web_section_means_no_results() -> None:
    rec = Recorder(httpx.Response(200, json={"query": {"original": "zzzz"}}))
    response = await provider(rec).search(SearchQuery("zzzz"))
    assert response.results == []


async def test_rate_limit_uses_ratelimit_reset_header() -> None:
    rec = Recorder(httpx.Response(429, headers={"X-RateLimit-Reset": "1, 1419704"}, json={}))
    with pytest.raises(RateLimitError) as info:
        await provider(rec).search(SearchQuery("q"))
    assert info.value.retry_after == 1.0


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, AuthenticationError),
        (403, AuthenticationError),
        (402, QuotaExceededError),
        (422, InvalidRequestError),
    ],
)
async def test_error_mapping(status: int, error: type[Exception]) -> None:
    rec = Recorder(httpx.Response(status, json={"error": {"detail": "nope", "code": "X"}}))
    with pytest.raises(error, match="nope"):
        await provider(rec).search(SearchQuery("q"))


async def test_too_long_query_with_operators_is_unsupported_not_invalid() -> None:
    rec = Recorder(httpx.Response(200, json=WEB_OK))
    query = SearchQuery("word " * 49 + "x", exclude_domains=["a.com", "b.com"])
    with pytest.raises(UnsupportedOperationError) as info:
        await provider(rec).search(query)
    assert info.value.fallback  # another provider can still run it
    assert rec.requests == []


async def test_unsupported_country_and_language_are_skipped() -> None:
    rec = Recorder(httpx.Response(200, json=WEB_OK))
    await provider(rec).search(SearchQuery("q", country="ua", language="kk"))
    params = dict(rec.last.url.params)
    assert "country" not in params
    assert "search_lang" not in params
    await provider(rec).search(SearchQuery("q", country="jp", language="ja"))
    params = dict(rec.last.url.params)
    assert (params["country"], params["search_lang"]) == ("JP", "jp")


@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({}, {"error": {"code": "QUOTA_LIMITED", "detail": "quota exceeded"}}),
        ({"X-RateLimit-Remaining": "1, 0", "X-RateLimit-Reset": "1, 86400"}, {}),
    ],
)
async def test_exhausted_plan_is_quota_not_rate_limit(
    headers: dict[str, str], body: dict[str, object]
) -> None:
    rec = Recorder(httpx.Response(429, headers=headers, json=body))
    with pytest.raises(QuotaExceededError):
        await provider(rec).search(SearchQuery("q"))


async def test_burst_limit_stays_rate_limit() -> None:
    headers = {"X-RateLimit-Remaining": "0, 900", "X-RateLimit-Reset": "1, 86400"}
    rec = Recorder(httpx.Response(429, headers=headers, json={"error": {"code": "RATE_LIMITED"}}))
    with pytest.raises(RateLimitError):
        await provider(rec).search(SearchQuery("q"))


async def test_malformed_items_are_skipped() -> None:
    body = {
        "web": {
            "results": [
                {"title": "bad", "url": "http://[::1"},
                {"title": "bad port", "url": "https://a.com:abc/"},
                {"title": "ftp", "url": "ftp://a.com/"},
                {
                    "title": 123,
                    "url": "https://ok.com/",
                    "page_age": 1700000000,
                    "extra_snippets": "not-a-list",
                },
            ]
        }
    }
    rec = Recorder(httpx.Response(200, json=body))
    response = await provider(rec).search(SearchQuery("q", exclude_domains=["b.com"]))
    assert [r.url for r in response.results] == ["https://ok.com/"]
    assert response.results[0].title == "123"
    assert response.results[0].published_at == "1700000000"
    assert "1700000000" in response.to_llm_context()


async def test_extract_is_unsupported() -> None:
    with pytest.raises(UnsupportedOperationError):
        await provider(Recorder(httpx.Response(200))).extract(["https://a.com"])
