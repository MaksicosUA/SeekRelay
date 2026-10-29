"""Shared fixtures. No test touches the network: HTTP goes through httpx.MockTransport."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

import httpx
import pytest

from seekrelay import SearchProvider, SearchQuery, SearchResponse, SearchResult
from seekrelay.models import ExtractedPage, ExtractResponse

Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    """A MockTransport handler that records requests and replays scripted responses."""

    def __init__(self, *responses: httpx.Response | Exception | Handler) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(item, Exception):
            raise item
        if callable(item) and not isinstance(item, httpx.Response):
            return item(request)
        return item

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def last_json(self) -> Any:
        return json.loads(self.last.content)


@pytest.fixture
def recorder_factory() -> Callable[..., Recorder]:
    return Recorder


class FakeProvider(SearchProvider):
    """Scripted provider for relay tests.

    Each outcome is either an exception to raise or a list of URLs to return.
    The last outcome repeats once the script is exhausted.
    """

    def __init__(
        self,
        name: str,
        *outcomes: Exception | Sequence[str],
        extract: bool = False,
    ) -> None:
        super().__init__(client=httpx.AsyncClient(transport=httpx.MockTransport(_refuse)))
        self.name = name  # type: ignore[misc]
        self.supports_extract = extract  # type: ignore[misc]
        self._outcomes = list(outcomes) or [["https://example.com/"]]
        self.calls = 0
        self.closed = False

    def _next(self) -> Exception | Sequence[str]:
        self.calls += 1
        return self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]

    async def search(self, query: SearchQuery) -> SearchResponse:
        outcome = self._next()
        if isinstance(outcome, Exception):
            raise outcome
        results = [
            SearchResult(title=f"{self.name} {i}", url=url, snippet="snippet", provider=self.name)
            for i, url in enumerate(outcome)
        ]
        return SearchResponse(query=query, provider=self.name, results=results)

    async def extract(self, urls: Sequence[str], *, advanced: bool = False) -> ExtractResponse:
        if not self.supports_extract:
            return await super().extract(urls, advanced=advanced)
        outcome = self._next()
        if isinstance(outcome, Exception):
            raise outcome
        pages = [ExtractedPage(url=u, content=f"content of {u}", provider=self.name) for u in urls]
        return ExtractResponse(provider=self.name, pages=pages)

    async def aclose(self) -> None:
        self.closed = True
        await super().aclose()


def _refuse(request: httpx.Request) -> httpx.Response:  # pragma: no cover - safety net
    raise AssertionError(f"FakeProvider must not do HTTP: {request.url}")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sleep() -> FakeSleep:
    return FakeSleep()
