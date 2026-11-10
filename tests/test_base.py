from __future__ import annotations

import asyncio
import gzip
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from seekrelay import ProviderTimeoutError, SearchQuery, SearXNGProvider, SeekRelay
from seekrelay.errors import ResponseFormatError
from seekrelay.providers import base

from .conftest import FakeProvider, Recorder

BODY = json.dumps({"results": [{"url": "https://a.example/", "title": "t", "content": "c"}]})
QUERY = SearchQuery("q")


def provider(response: httpx.Response, **kwargs: Any) -> SearXNGProvider:
    return SearXNGProvider("https://s.example", client=Recorder(response).client(), **kwargs)


def gzipped(data: bytes) -> httpx.Response:
    return httpx.Response(
        200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(gzip.compress(data))
    )


class _Drip(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            yield b" "
            await asyncio.sleep(0.001)


async def test_gzip_body_is_unpacked_once() -> None:
    response = await provider(gzipped(BODY.encode())).search(QUERY)
    assert [result.url for result in response.results] == ["https://a.example/"]


async def test_limit_counts_unpacked_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "MAX_RESPONSE_BYTES", 2000)
    assert len(gzip.compress(b" " * 1_000_000)) < 2000
    with pytest.raises(ResponseFormatError, match="larger than 2000 bytes"):
        await provider(gzipped(b" " * 1_000_000)).search(QUERY)


async def test_oversized_body_moves_on_to_the_next_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(base, "MAX_RESPONSE_BYTES", 10)
    oversized = provider(httpx.Response(200, content=BODY))
    relay = SeekRelay([oversized, FakeProvider("backup", ["https://b.example/"])], cache_ttl=None)
    response = await relay.search("q")
    assert response.provider == "backup"
    assert response.attempts[0].error == "ResponseFormatError"


async def test_body_sent_slowly_cannot_outlive_the_timeout() -> None:
    with pytest.raises(ProviderTimeoutError, match=r"timed out after 0\.05s"):
        await provider(httpx.Response(200, stream=_Drip()), timeout=0.05).search(QUERY)


@pytest.mark.parametrize(("cap", "peak"), [(2, 2), (None, 6)])
async def test_requests_over_max_concurrency_wait_for_a_slot(cap: int | None, peak: int) -> None:
    in_flight = highest = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, highest
        in_flight += 1
        highest = max(highest, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return httpx.Response(200, content=BODY)

    searx = SearXNGProvider(
        "https://s.example", client=Recorder(handler).client(), max_concurrency=cap
    )
    await asyncio.gather(*(searx.search(SearchQuery(f"q{i}")) for i in range(6)))
    assert highest == peak


def test_max_concurrency_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_concurrency"):
        SearXNGProvider("https://s.example", max_concurrency=0)
