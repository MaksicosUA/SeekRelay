from __future__ import annotations

import asyncio

import pytest

from seekrelay import (
    AllProvidersFailedError,
    AuthenticationError,
    ConfigurationError,
    InvalidRequestError,
    PIIGuard,
    ProviderUnavailableError,
    QueryRejectedError,
    QuotaExceededError,
    RateLimitError,
    RetryPolicy,
    SearchGateway,
    SearchQuery,
)

from .conftest import FakeClock, FakeProvider, FakeSleep


def gateway(
    *providers: FakeProvider, clock: FakeClock, sleep: FakeSleep, **kwargs: object
) -> SearchGateway:
    kwargs.setdefault("retry", RetryPolicy(max_attempts=2, jitter=0))
    return SearchGateway(list(providers), clock=clock, sleep=sleep, **kwargs)  # type: ignore[arg-type]


async def test_first_provider_wins(clock: FakeClock, sleep: FakeSleep) -> None:
    a, b = FakeProvider("a", ["https://a.com/1"]), FakeProvider("b")
    response = await gateway(a, b, clock=clock, sleep=sleep).search("q")
    assert response.provider == "a"
    assert response.attempts == ()
    assert (a.calls, b.calls) == (1, 0)


async def test_quota_falls_back_and_cools_down(clock: FakeClock, sleep: FakeSleep) -> None:
    a = FakeProvider("a", QuotaExceededError("a", "HTTP 432: plan limit", status_code=432))
    b = FakeProvider("b", ["https://b.com/1"])
    gw = gateway(a, b, clock=clock, sleep=sleep, cache_ttl=None, cooldown_seconds=60)

    response = await gw.search("q")
    assert response.provider == "b"
    assert [(x.provider, x.error, x.status_code) for x in response.attempts] == [
        ("a", "QuotaExceededError", 432)
    ]
    assert sleep.delays == []  # quota errors are not retried
    assert gw.cooldown_remaining("a") == 60

    response = await gw.search("q2")
    assert a.calls == 1  # skipped while cooling down
    assert response.attempts[0].error == "Cooldown"

    clock.advance(61)
    await gw.search("q3")
    assert a.calls == 2


async def test_auth_error_also_cools_down(clock: FakeClock, sleep: FakeSleep) -> None:
    a = FakeProvider("a", AuthenticationError("a", "bad key", status_code=401))
    gw = gateway(a, FakeProvider("b"), clock=clock, sleep=sleep)
    await gw.search("q")
    assert gw.cooldown_remaining("a") > 0
    gw.reset_cooldowns()
    assert gw.cooldown_remaining("a") == 0


async def test_retryable_error_is_retried_with_backoff(clock: FakeClock, sleep: FakeSleep) -> None:
    a = FakeProvider("a", ProviderUnavailableError("a", "HTTP 503"), ["https://a.com/1"])
    b = FakeProvider("b")
    response = await gateway(a, b, clock=clock, sleep=sleep).search("q")
    assert response.provider == "a"
    assert a.calls == 2
    assert b.calls == 0
    assert sleep.delays == [0.5]


async def test_exhausted_retries_fall_back(clock: FakeClock, sleep: FakeSleep) -> None:
    a = FakeProvider("a", ProviderUnavailableError("a", "HTTP 503"))
    b = FakeProvider("b")
    retry = RetryPolicy(max_attempts=3, base_delay=1, jitter=0)
    response = await gateway(a, b, clock=clock, sleep=sleep, retry=retry).search("q")
    assert a.calls == 3
    assert sleep.delays == [1, 2]
    assert response.provider == "b"
    assert gateway(a, b, clock=clock, sleep=sleep).cooldown_remaining("a") == 0


async def test_rate_limit_respects_short_retry_after(clock: FakeClock, sleep: FakeSleep) -> None:
    a = FakeProvider("a", RateLimitError("a", "429", retry_after=1.5), ["https://a.com"])
    await gateway(a, clock=clock, sleep=sleep).search("q")
    assert sleep.delays == [1.5]


async def test_long_retry_after_falls_back_without_waiting(
    clock: FakeClock, sleep: FakeSleep
) -> None:
    a = FakeProvider("a", RateLimitError("a", "429", retry_after=120))
    b = FakeProvider("b")
    response = await gateway(a, b, clock=clock, sleep=sleep).search("q")
    assert sleep.delays == []
    assert response.provider == "b"
    assert response.attempts[0].error == "RateLimitError"


async def test_invalid_request_does_not_leak_to_next_provider(
    clock: FakeClock, sleep: FakeSleep
) -> None:
    a = FakeProvider("a", InvalidRequestError("a", "Query blocked, PII detected", status_code=400))
    b = FakeProvider("b")
    with pytest.raises(InvalidRequestError):
        await gateway(a, b, clock=clock, sleep=sleep).search("q")
    assert b.calls == 0


async def test_invalid_request_fallback_can_be_enabled(clock: FakeClock, sleep: FakeSleep) -> None:
    a = FakeProvider("a", InvalidRequestError("a", "bad param", status_code=400))
    b = FakeProvider("b")
    gw = gateway(a, b, clock=clock, sleep=sleep, fallback_on_invalid_request=True)
    assert (await gw.search("q")).provider == "b"


async def test_all_failed_reports_attempts(clock: FakeClock, sleep: FakeSleep) -> None:
    a = FakeProvider("a", QuotaExceededError("a", "limit"))
    b = FakeProvider("b", AuthenticationError("b", "bad key", status_code=401))
    gw = gateway(a, b, clock=clock, sleep=sleep)
    with pytest.raises(AllProvidersFailedError) as info:
        await gw.search("q")
    assert [(x.provider, x.error) for x in info.value.attempts] == [
        ("a", "QuotaExceededError"),
        ("b", "AuthenticationError"),
    ]
    assert "a: QuotaExceededError" in str(info.value)

    with pytest.raises(AllProvidersFailedError) as info:
        await gw.search("q2")
    assert {x.error for x in info.value.attempts} == {"Cooldown"}


async def test_pii_guard_blocks_before_any_provider(clock: FakeClock, sleep: FakeSleep) -> None:
    a = FakeProvider("a")
    gw = gateway(a, clock=clock, sleep=sleep)
    with pytest.raises(QueryRejectedError):
        await gw.search("vacancies for ivan.petrenko@gmail.com")
    assert a.calls == 0
    gw_off = gateway(a, clock=clock, sleep=sleep, pii_guard=None)
    await gw_off.search("vacancies for ivan.petrenko@gmail.com")
    assert a.calls == 1


async def test_custom_pii_guard(clock: FakeClock, sleep: FakeSleep) -> None:
    gw = gateway(
        FakeProvider("a"), clock=clock, sleep=sleep, pii_guard=PIIGuard(detect_phones=True)
    )
    with pytest.raises(QueryRejectedError):
        await gw.search("who owns +380 67 123 45 67")


async def test_cache_hits_and_expires(clock: FakeClock, sleep: FakeSleep) -> None:
    a = FakeProvider("a", ["https://a.com/1"])
    gw = gateway(a, clock=clock, sleep=sleep, cache_ttl=100)

    first = await gw.search("q", max_results=3)
    second = await gw.search(SearchQuery(" q ", max_results=3))
    assert a.calls == 1
    assert second.from_cache and not first.from_cache
    second.results.clear()  # mutating a cached copy must not poison the cache
    assert len((await gw.search("q", max_results=3)).results) == 1

    await gw.search("q", max_results=4)  # different query -> new call
    assert a.calls == 2
    clock.advance(101)
    await gw.search("q", max_results=3)
    assert a.calls == 3

    gw.clear_cache()
    await gw.search("q", max_results=3)
    assert a.calls == 4


async def test_cache_disabled(clock: FakeClock, sleep: FakeSleep) -> None:
    a = FakeProvider("a")
    gw = gateway(a, clock=clock, sleep=sleep, cache_ttl=None)
    await gw.search("q")
    await gw.search("q")
    assert a.calls == 2


async def test_results_are_deduplicated(clock: FakeClock, sleep: FakeSleep) -> None:
    urls = ["https://a.com/x?utm_source=t", "https://www.a.com/x/", "https://a.com/y"]
    response = await gateway(FakeProvider("a", urls), clock=clock, sleep=sleep).search("q")
    assert [r.url for r in response.results] == ["https://a.com/x?utm_source=t", "https://a.com/y"]


async def test_fallback_on_empty(clock: FakeClock, sleep: FakeSleep) -> None:
    a, b = FakeProvider("a", []), FakeProvider("b", ["https://b.com"])
    assert (await gateway(a, b, clock=clock, sleep=sleep).search("q")).provider == "a"
    gw = gateway(a, b, clock=clock, sleep=sleep, fallback_on_empty=True, cache_ttl=None)
    response = await gw.search("q")
    assert response.provider == "b"
    assert response.attempts[0].error == "EmptyResult"

    c = FakeProvider("c", [])
    only_empty = gateway(FakeProvider("a", []), c, clock=clock, sleep=sleep, fallback_on_empty=True)
    response = await only_empty.search("q")
    assert response.results == [] and response.provider == "a"
    assert [x.provider for x in response.attempts] == ["a", "c"]


async def test_query_object_and_kwargs_are_exclusive(clock: FakeClock, sleep: FakeSleep) -> None:
    with pytest.raises(TypeError):
        await gateway(FakeProvider("a"), clock=clock, sleep=sleep).search(
            SearchQuery("q"), max_results=2
        )


def test_constructor_validation() -> None:
    with pytest.raises(ConfigurationError):
        SearchGateway([])
    with pytest.raises(ConfigurationError):
        SearchGateway([FakeProvider("a"), FakeProvider("a")])
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)


def test_retry_policy_delays() -> None:
    policy = RetryPolicy(base_delay=1, max_delay=5, jitter=0)
    assert [policy.delay(n) for n in (1, 2, 3, 4)] == [1, 2, 4, 5]
    assert policy.delay(1, retry_after=10) == 5
    jittery = RetryPolicy(base_delay=1, jitter=0.5)
    assert all(0.5 <= jittery.delay(1) <= 1.5 for _ in range(50))


async def test_extract_uses_capable_provider(clock: FakeClock, sleep: FakeSleep) -> None:
    plain = FakeProvider("brave")
    capable = FakeProvider("tavily", ["unused"], extract=True)
    gw = gateway(plain, capable, clock=clock, sleep=sleep)
    response = await gw.extract(["https://a.com/1", "https://a.com/1", "https://b.com"])
    assert response.provider == "tavily"
    assert [p.url for p in response.pages] == ["https://a.com/1", "https://b.com"]
    assert plain.calls == 0

    single = await gw.extract("https://c.com")
    assert single.pages[0].url == "https://c.com"


async def test_extract_validation(clock: FakeClock, sleep: FakeSleep) -> None:
    gw = gateway(FakeProvider("brave"), clock=clock, sleep=sleep)
    with pytest.raises(ConfigurationError):
        await gw.extract("https://a.com")
    with pytest.raises(ValueError):
        await gw.extract("not-a-url")
    with pytest.raises(ValueError):
        await gw.extract([])
    capable = gateway(FakeProvider("t", extract=True), clock=clock, sleep=sleep)
    with pytest.raises(QueryRejectedError):
        await capable.extract("https://a.com/?email=jane@corp.example")


async def test_extract_falls_back(clock: FakeClock, sleep: FakeSleep) -> None:
    first = FakeProvider("t1", QuotaExceededError("t1", "limit"), extract=True)
    second = FakeProvider("t2", ["ok"], extract=True)
    response = await gateway(first, second, clock=clock, sleep=sleep).extract("https://a.com")
    assert response.provider == "t2"
    assert response.attempts[0].provider == "t1"


async def test_async_context_manager_closes_providers(clock: FakeClock, sleep: FakeSleep) -> None:
    a, b = FakeProvider("a"), FakeProvider("b")
    async with gateway(a, b, clock=clock, sleep=sleep) as gw:
        await gw.search("q")
    assert a.closed and b.closed


def test_sync_api() -> None:
    a = FakeProvider("a", ["https://a.com"])
    with SearchGateway([a]) as gw:
        assert gw.search_sync("q").provider == "a"
        assert gw.search_sync("q").from_cache
    assert a.closed


async def test_sync_api_refuses_inside_event_loop() -> None:
    gw = SearchGateway([FakeProvider("a")])
    with pytest.raises(RuntimeError, match="await"):
        gw.search_sync("q")
    await gw.aclose()


async def test_concurrent_searches(clock: FakeClock, sleep: FakeSleep) -> None:
    a = FakeProvider("a", ["https://a.com"])
    gw = gateway(a, clock=clock, sleep=sleep, cache_ttl=None)
    responses = await asyncio.gather(*(gw.search(f"q{i}") for i in range(10)))
    assert {r.provider for r in responses} == {"a"}
    assert a.calls == 10


async def test_cached_response_is_isolated_from_callers(clock: FakeClock, sleep: FakeSleep) -> None:
    a = FakeProvider("a", QuotaExceededError("a", "limit"))
    b = FakeProvider("b", ["https://b.com/1"])
    gw = gateway(a, b, clock=clock, sleep=sleep)
    first = await gw.search("q")
    assert first.attempts  # served after a fallback
    first.results.clear()  # caller mutates its own response
    hit = await gw.search("q")
    assert hit.from_cache
    assert [r.url for r in hit.results] == ["https://b.com/1"]
    assert hit.attempts == ()  # no stale fallback history on cache hits


async def test_unexpected_provider_exception_falls_back(
    clock: FakeClock, sleep: FakeSleep, caplog: pytest.LogCaptureFixture
) -> None:
    a = FakeProvider("a", KeyError("results"))
    b = FakeProvider("b", ["https://b.com"])
    response = await gateway(a, b, clock=clock, sleep=sleep).search("q")
    assert response.provider == "b"
    assert response.attempts[0].error == "ProviderError"
    assert "unexpected KeyError" in response.attempts[0].message
    assert a.calls == 1  # not retried
    assert any("unexpected error" in r.message for r in caplog.records)


async def test_unsupported_request_falls_back(clock: FakeClock, sleep: FakeSleep) -> None:
    from seekrelay import UnsupportedOperationError

    a = FakeProvider("a", UnsupportedOperationError("a", "query too long for this provider"))
    b = FakeProvider("b", ["https://b.com"])
    assert (await gateway(a, b, clock=clock, sleep=sleep).search("q")).provider == "b"


async def test_closed_gateway_refuses_calls(clock: FakeClock, sleep: FakeSleep) -> None:
    gw = gateway(FakeProvider("a", extract=True), clock=clock, sleep=sleep)
    await gw.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await gw.search("q")
    with pytest.raises(RuntimeError, match="closed"):
        await gw.extract("https://a.com")


async def test_extract_checks_decoded_urls(clock: FakeClock, sleep: FakeSleep) -> None:
    capable = FakeProvider("t", extract=True)
    gw = gateway(capable, clock=clock, sleep=sleep)
    with pytest.raises(QueryRejectedError):
        await gw.extract("https://a.com/?u=jane%40corp.example")
    assert capable.calls == 0


def test_jitter_never_exceeds_max_delay() -> None:
    policy = RetryPolicy(base_delay=8, max_delay=8, jitter=0.5)
    assert all(policy.delay(1) <= 8 for _ in range(100))
