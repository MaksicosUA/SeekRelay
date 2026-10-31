from __future__ import annotations

import json

import httpx
import pytest

from seekrelay import (
    AuthenticationError,
    ConfigurationError,
    InvalidRequestError,
    ProviderError,
    ProviderUnavailableError,
    QuotaExceededError,
    RateLimitError,
    ResponseFormatError,
    SearchQuery,
    TavilyProvider,
)
from seekrelay.providers.tavily import TAVILY_COUNTRY_NAMES

from .conftest import Recorder

SEARCH_OK = {
    "query": "q",
    "answer": "Short answer",
    "results": [
        {
            "title": " FastAPI ",
            "url": "https://fastapi.tiangolo.com/",
            "content": "FastAPI framework",
            "score": "0.91",
            "raw_content": "# FastAPI",
            "published_date": "2026-10-01",
        },
        {"title": "no url"},
        {"title": "Second", "url": "https://example.com/2", "content": "two", "score": None},
        {"title": "Third", "url": "https://example.com/3", "content": "three", "score": 0.1},
    ],
    "response_time": 0.42,
    "request_id": "req-123",
}


def provider(recorder: Recorder, **kwargs: object) -> TavilyProvider:
    return TavilyProvider("tvly-test", client=recorder.client(), **kwargs)  # type: ignore[arg-type]


def test_requires_key() -> None:
    with pytest.raises(ConfigurationError):
        TavilyProvider("  ")


async def test_search_builds_payload_and_maps_results() -> None:
    rec = Recorder(httpx.Response(200, json=SEARCH_OK))
    query = SearchQuery(
        "fastapi lifespan",
        max_results=2,
        depth="advanced",  # type: ignore[arg-type]
        time_range="month",  # type: ignore[arg-type]
        include_domains=["fastapi.tiangolo.com", "example.com"],
        exclude_domains=["spam.com"],
        language="en",
        country="ua",
        include_raw_content=True,
    )
    response = await provider(rec, include_answer=True).search(query)

    request = rec.last
    assert request.method == "POST"
    assert str(request.url) == "https://api.tavily.com/search"
    assert request.headers["Authorization"] == "Bearer tvly-test"
    assert rec.last_json() == {
        "query": "fastapi lifespan",
        "search_depth": "advanced",
        "max_results": 2,
        "topic": "general",
        "include_answer": "basic",
        "include_raw_content": "markdown",
        "include_published_date": True,
        "time_range": "month",
        "include_domains": ["fastapi.tiangolo.com", "example.com"],
        "exclude_domains": ["spam.com"],
        "language": "en",
        "country": "ukraine",
    }

    assert response.provider == "tavily"
    assert response.answer == "Short answer"
    assert response.request_id == "req-123"
    assert [r.url for r in response.results] == [
        "https://fastapi.tiangolo.com/",
        "https://example.com/2",
    ]
    first = response.results[0]
    assert first.title == "FastAPI"
    assert first.score == pytest.approx(0.91)
    assert first.raw_content == "# FastAPI"
    assert first.published_at == "2026-10-01"
    assert response.results[1].score is None


def test_country_table_covers_the_api_list() -> None:
    assert len(set(TAVILY_COUNTRY_NAMES.values())) == 166  # the `country` enum, October 2026
    assert TAVILY_COUNTRY_NAMES["ru"] == "russia"
    assert TAVILY_COUNTRY_NAMES["br"] == "brazil"
    assert TAVILY_COUNTRY_NAMES["gb"] == TAVILY_COUNTRY_NAMES["uk"] == "united kingdom"


async def test_minimal_payload_and_country_rules() -> None:
    rec = Recorder(httpx.Response(200, json={"results": []}))
    tavily = provider(rec)
    await tavily.search(SearchQuery("q", country="zz"))
    body = rec.last_json()
    assert body["include_answer"] is False
    assert body["include_raw_content"] is False
    assert "country" not in body  # unknown code is skipped, not sent
    await tavily.search(SearchQuery("q", country="ua", topic="news"))  # type: ignore[arg-type]
    assert "country" not in rec.last_json()  # country only works with topic=general


async def test_raw_content_not_returned_unless_requested() -> None:
    rec = Recorder(httpx.Response(200, json=SEARCH_OK))
    response = await provider(rec).search(SearchQuery("q"))
    assert response.results[0].raw_content is None


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, InvalidRequestError),
        (401, AuthenticationError),
        (422, InvalidRequestError),
        (432, QuotaExceededError),
        (433, QuotaExceededError),
        (434, ProviderUnavailableError),
        (500, ProviderUnavailableError),
        (503, ProviderUnavailableError),
    ],
)
async def test_maps_http_errors(status: int, error: type[Exception]) -> None:
    rec = Recorder(httpx.Response(status, json={"detail": {"error": "boom happened"}}))
    with pytest.raises(error) as info:
        await provider(rec).search(SearchQuery("q"))
    assert "boom happened" in str(info.value)
    assert info.value.status_code == status  # type: ignore[attr-defined]


async def test_validation_error_detail_list() -> None:
    body = {"detail": [{"loc": ["body", "query"], "msg": "Input should be a valid string"}]}
    rec = Recorder(httpx.Response(422, json=body))
    with pytest.raises(InvalidRequestError, match="valid string"):
        await provider(rec).search(SearchQuery("q"))


async def test_rate_limit_reads_retry_after() -> None:
    rec = Recorder(
        httpx.Response(429, headers={"Retry-After": "3"}, json={"detail": {"error": "slow down"}})
    )
    with pytest.raises(RateLimitError) as info:
        await provider(rec).search(SearchQuery("q"))
    assert info.value.retry_after == 3.0
    assert info.value.retryable


async def test_timeout_and_network_errors_become_unavailable() -> None:
    rec = Recorder(httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderUnavailableError, match="timed out"):
        await provider(rec, timeout=1.5).search(SearchQuery("q"))
    rec = Recorder(httpx.ConnectError("refused"))
    with pytest.raises(ProviderUnavailableError, match="ConnectError"):
        await provider(rec).search(SearchQuery("q"))


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"no": "results"}),
        httpx.Response(200, json=[1, 2, 3]),
    ],
)
async def test_bad_payloads(response: httpx.Response) -> None:
    with pytest.raises(ResponseFormatError):
        await provider(Recorder(response)).search(SearchQuery("q"))


async def test_extract_batches_and_merges() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        urls = json.loads(request.content)["urls"]
        return httpx.Response(
            200,
            json={
                "results": [{"url": u, "raw_content": f"md {u}"} for u in urls[:-1]],
                "failed_results": [{"url": urls[-1], "error": "blocked"}],
            },
        )

    rec = Recorder(handler)
    urls = [f"https://example.com/{i}" for i in range(45)] + ["https://example.com/0"]
    response = await provider(rec).extract(urls, advanced=True)

    sizes = [len(json.loads(r.content)["urls"]) for r in rec.requests]
    assert sorted(sizes) == [5, 20, 20]  # 45 unique URLs, max 20 per call
    assert all(json.loads(r.content)["extract_depth"] == "advanced" for r in rec.requests)
    assert all(str(r.url) == "https://api.tavily.com/extract" for r in rec.requests)
    assert len(response.pages) == 42
    assert len(response.failed) == 3
    assert response.failed[0].error == "blocked"
    assert response.pages[0].content.startswith("md https://example.com/")


async def test_extract_rejects_bad_urls() -> None:
    rec = Recorder(httpx.Response(200, json={}))
    with pytest.raises(InvalidRequestError):
        await provider(rec).extract(["file:///etc/passwd"])
    with pytest.raises(InvalidRequestError):
        await provider(rec).extract([])
    with pytest.raises(InvalidRequestError):
        await provider(rec).extract(["http://localhost:8080/admin"])
    assert rec.requests == []


def test_repr_hides_key() -> None:
    assert "tvly" not in repr(TavilyProvider("tvly-secret"))


async def test_extract_keeps_successful_batches_when_one_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        urls = json.loads(request.content)["urls"]
        if urls[0].endswith("/20"):  # the second batch fails
            return httpx.Response(500, json={"detail": {"error": "boom"}})
        return httpx.Response(200, json={"results": [{"url": u, "raw_content": "x"} for u in urls]})

    rec = Recorder(handler)
    urls = [f"https://example.com/{i}" for i in range(25)]
    response = await provider(rec).extract(urls)
    assert len(rec.requests) == 2  # no re-sending of the successful batch
    assert len(response.pages) == 20
    assert {f.url for f in response.failed} == {f"https://example.com/{i}" for i in range(20, 25)}
    assert "boom" in response.failed[0].error


async def test_extract_raises_when_every_batch_fails() -> None:
    rec = Recorder(httpx.Response(432, json={"detail": {"error": "limit"}}))
    with pytest.raises(QuotaExceededError):
        await provider(rec).extract([f"https://example.com/{i}" for i in range(25)])


async def test_bad_items_are_skipped_and_coerced() -> None:
    body = {
        "results": [
            {"title": "x", "url": "http://[::1"},
            {"title": None, "url": "https://ok.com", "content": 42, "published_date": 2026},
        ],
        "answer": 7,
    }
    response = await provider(Recorder(httpx.Response(200, json=body))).search(SearchQuery("q"))
    assert [r.url for r in response.results] == ["https://ok.com"]
    result = response.results[0]
    assert (result.title, result.snippet, result.published_at) == ("", "42", "2026")
    assert response.answer == "7"


@pytest.mark.parametrize("value", ["nan", "inf", "soon"])
async def test_unusable_retry_after_is_ignored(value: str) -> None:
    rec = Recorder(httpx.Response(429, headers={"Retry-After": value}, json={}))
    with pytest.raises(RateLimitError) as info:
        await provider(rec).search(SearchQuery("q"))
    assert info.value.retry_after is None


async def test_injected_client_never_follows_redirects() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "api.tavily.com":
            return httpx.Response(307, headers={"Location": "https://evil.example/collect"})
        return httpx.Response(200, json={"results": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    with pytest.raises(ProviderError, match="redirect"):
        await TavilyProvider("tvly-secret", client=client).search(SearchQuery("q"))
    assert seen == ["https://api.tavily.com/search"]


async def test_decoding_error_is_response_format_error() -> None:
    rec = Recorder(httpx.DecodingError("bad gzip"))
    with pytest.raises(ResponseFormatError):
        await provider(rec).search(SearchQuery("q"))


@pytest.mark.parametrize(
    "score",
    [
        pytest.param(b'"NaN"', id="nan-string"),
        pytest.param(b"NaN", id="nan-literal"),
        pytest.param(b'"inf"', id="inf-string"),
        pytest.param(b"Infinity", id="infinity-literal"),
        pytest.param(b"-Infinity", id="negative-infinity-literal"),
        pytest.param(b"1e999", id="float-overflow"),
        pytest.param(b"1" + b"0" * 400, id="huge-integer"),
    ],
)
async def test_score_must_be_a_finite_number(score: bytes) -> None:
    body = b'{"results":[{"title":"t","url":"https://a.example/","score":' + score + b"}]}"
    rec = Recorder(httpx.Response(200, content=body))
    response = await provider(rec).search(SearchQuery("q"))
    assert [r.url for r in response.results] == ["https://a.example/"]
    assert response.results[0].score is None
    json.dumps(response.to_dict(), allow_nan=False)  # raises on NaN and Infinity


async def test_search_applies_the_filters_to_what_tavily_returns() -> None:
    rec = Recorder(httpx.Response(200, json=SEARCH_OK))
    query = SearchQuery("q", include_domains=["fastapi.tiangolo.com"])
    response = await provider(rec).search(query)
    assert [r.url for r in response.results] == ["https://fastapi.tiangolo.com/"]

    rec = Recorder(httpx.Response(200, json=SEARCH_OK))
    response = await provider(rec).search(SearchQuery("q", exclude_domains=["example.com"]))
    assert [r.url for r in response.results] == ["https://fastapi.tiangolo.com/"]


async def test_extract_drops_pages_that_are_not_http_urls() -> None:
    results = [
        {"url": "https://a.example/", "raw_content": "kept"},
        {"url": "javascript:alert(1)", "raw_content": "dropped"},
        {"url": "file:///etc/passwd", "raw_content": "dropped"},
    ]
    rec = Recorder(httpx.Response(200, json={"results": results}))
    response = await provider(rec).extract(["https://a.example/"])
    assert [page.url for page in response.pages] == ["https://a.example/"]
