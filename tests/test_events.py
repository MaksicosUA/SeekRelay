"""Events, counters, provider state, the rate limit and the per-call time budget."""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import time

import pytest

from seekrelay import (
    AllProvidersFailedError,
    InvalidRequestError,
    LimitExceededError,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    QueryRejectedError,
    QuotaExceededError,
    RelayEvent,
    RetryPolicy,
    SearchQuery,
    SearchResponse,
    SeekRelay,
    TimeBudgetExceededError,
)
from seekrelay import relay as relay_module
from seekrelay.events import DOWN_AFTER_FAILURES, SEVERITY

from .conftest import FakeClock, FakeProvider, FakeSleep


class Events(list[RelayEvent]):
    """An ``on_event`` handler that keeps what it gets."""

    def __call__(self, event: RelayEvent) -> None:
        self.append(event)

    @property
    def kinds(self) -> list[str]:
        return [e.kind for e in self]

    def take(self) -> list[str]:
        kinds = self.kinds
        self.clear()
        return kinds


def make_relay(
    *providers: FakeProvider, clock: FakeClock, sleep: FakeSleep, **kwargs: object
) -> SeekRelay:
    kwargs.setdefault("retry", RetryPolicy(max_attempts=2, jitter=0))
    kwargs.setdefault("cache_ttl", None)
    return SeekRelay(list(providers), clock=clock, sleep=sleep, **kwargs)  # type: ignore[arg-type]


async def test_served_event(clock: FakeClock, sleep: FakeSleep) -> None:
    events = Events()
    relay = make_relay(
        FakeProvider("a", ["https://a.com/1"]), clock=clock, sleep=sleep, on_event=events
    )
    await relay.search("q")
    (event,) = events
    assert (event.kind, event.severity, event.operation) == ("served", "info", "search")
    assert (event.provider, event.results, event.from_cache) == ("a", 1, False)
    assert event.error is None and event.at > 0


def test_every_kind_has_a_severity() -> None:
    assert set(SEVERITY.values()) == {"info", "warning", "alert"}
    assert {k for k, v in SEVERITY.items() if v == "alert"} == {"provider_down", "provider_up"}


async def test_quota_goes_down_once_and_comes_back(clock: FakeClock, sleep: FakeSleep) -> None:
    events = Events()
    a = FakeProvider("a", QuotaExceededError("a", "limit", status_code=432), ["https://a.com/1"])
    b = FakeProvider("b", ["https://b.com/1"])
    relay = make_relay(a, b, clock=clock, sleep=sleep, cooldown_seconds=60, on_event=events)

    await relay.search("q1")
    assert events.kinds == ["failure", "cooldown", "provider_down", "fallback", "served"]
    failure, cooldown, down = events[:3]
    assert (failure.error, failure.status_code, failure.severity) == (
        "QuotaExceededError",
        432,
        "warning",
    )
    assert cooldown.seconds == 60
    assert (down.severity, down.provider, down.error) == ("alert", "a", "QuotaExceededError")
    assert events[-1].provider == "b"
    events.clear()

    await relay.search("q2")  # a is paused: no second alert
    assert events.take() == ["fallback", "served"]

    clock.advance(61)
    await relay.search("q3")
    assert events.kinds == ["provider_up", "served"]
    assert events[0].seconds == 61
    assert events[1].provider == "a"


async def test_down_after_failures_in_a_row_without_flapping(
    clock: FakeClock, sleep: FakeSleep
) -> None:
    events = Events()
    outcomes: list[Exception | list[str]] = [
        ProviderUnavailableError("a", "HTTP 503") for _ in range(DOWN_AFTER_FAILURES + 1)
    ]
    a = FakeProvider("a", *outcomes, ["https://a.com/1"])
    b = FakeProvider("b", ["https://b.com/1"])
    relay = make_relay(
        a, b, clock=clock, sleep=sleep, retry=RetryPolicy(max_attempts=1), on_event=events
    )
    for i in range(DOWN_AFTER_FAILURES + 2):
        await relay.search(f"q{i}")
    assert events.kinds.count("provider_down") == 1
    assert events.kinds.count("provider_up") == 1
    down_at = events.kinds.index("provider_down")
    assert events.kinds[:down_at].count("failure") == DOWN_AFTER_FAILURES


async def test_retry_event(clock: FakeClock, sleep: FakeSleep) -> None:
    events = Events()
    a = FakeProvider("a", ProviderUnavailableError("a", "HTTP 503", status_code=503), ["https://a"])
    await make_relay(a, clock=clock, sleep=sleep, on_event=events).search("q")
    retry = events[0]
    assert (retry.kind, retry.attempt, retry.seconds, retry.status_code) == ("retry", 2, 0.5, 503)
    assert events.kinds == ["retry", "served"]


async def test_timeouts_are_not_retried(clock: FakeClock, sleep: FakeSleep) -> None:
    a = FakeProvider("a", ProviderTimeoutError("a", "request timed out after 20s"))
    b = FakeProvider("b", ["https://b.com/1"])
    response = await make_relay(a, b, clock=clock, sleep=sleep).search("q")
    assert response.provider == "b"
    assert a.calls == 1
    assert sleep.delays == []
    assert isinstance(ProviderTimeoutError("a", "x"), ProviderUnavailableError)


async def test_request_errors_and_used_up_budgets_do_not_take_a_provider_down(
    clock: FakeClock, sleep: FakeSleep
) -> None:
    events = Events()
    a = FakeProvider("a", InvalidRequestError("a", "bad", status_code=400))
    relay = make_relay(a, clock=clock, sleep=sleep, on_event=events)
    for _ in range(DOWN_AFTER_FAILURES + 1):
        with pytest.raises(InvalidRequestError):
            await relay.search("q")
    assert "provider_down" not in events.kinds
    assert relay.status()["a"].state == "up"
    assert relay.stats().providers["a"].failed == DOWN_AFTER_FAILURES + 1


async def test_all_failed_and_blocked(clock: FakeClock, sleep: FakeSleep) -> None:
    events = Events()
    a = FakeProvider("a", QuotaExceededError("a", "limit"))
    relay = make_relay(a, clock=clock, sleep=sleep, on_event=events)
    with pytest.raises(AllProvidersFailedError):
        await relay.search("q")
    assert events.kinds[-1] == "all_failed"
    assert events[-1].attempt == 1
    events.clear()
    with pytest.raises(QueryRejectedError):
        await relay.search("mail jane@corp.example")
    assert events.kinds == ["blocked"]
    assert events[0].error == "QueryRejectedError"
    stats = relay.stats()
    assert (stats.all_failed, stats.blocked, stats.searches) == (1, 1, 2)


async def test_cache_hit_event(clock: FakeClock, sleep: FakeSleep) -> None:
    events = Events()
    relay = make_relay(FakeProvider("a"), clock=clock, sleep=sleep, cache_ttl=60, on_event=events)
    await relay.search("q")
    await relay.search("q")
    assert [(e.kind, e.from_cache, e.latency_ms) for e in events][-1] == ("served", True, 0.0)
    assert relay.stats().cache_hits == 1


async def test_extract_events(clock: FakeClock, sleep: FakeSleep) -> None:
    events = Events()
    first = FakeProvider("t1", QuotaExceededError("t1", "limit"), extract=True)
    second = FakeProvider("t2", ["ok"], extract=True)
    relay = make_relay(first, second, clock=clock, sleep=sleep, on_event=events)
    await relay.extract(["https://a.com", "https://b.com"])
    assert events.kinds == ["failure", "cooldown", "provider_down", "fallback", "served"]
    assert {e.operation for e in events} == {"extract"}
    assert (events[-1].results, events[-1].failed) == (2, 0)
    assert relay.stats().extracts == 1


async def test_no_events_are_made_without_a_handler(
    clock: FakeClock, sleep: FakeSleep, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("an event was created without a handler")

    monkeypatch.setattr(relay_module, "RelayEvent", refuse)
    a = FakeProvider("a", QuotaExceededError("a", "limit"))
    relay = make_relay(a, FakeProvider("b"), clock=clock, sleep=sleep)
    await relay.search("q")
    assert relay.stats().providers["b"].served_as_backup == 1


async def test_a_failing_handler_never_breaks_the_call(
    clock: FakeClock, sleep: FakeSleep, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="seekrelay")

    def broken(event: RelayEvent) -> None:
        raise RuntimeError("monitoring is down")

    relay = make_relay(FakeProvider("a"), clock=clock, sleep=sleep, on_event=broken)
    for i in range(3):
        assert (await relay.search(f"q{i}")).provider == "a"
    assert relay.stats().handler_errors == 3
    warnings = [r.getMessage() for r in caplog.records]
    assert warnings == ["on_event handler raised RuntimeError; the call goes on (logged once)"]


async def test_a_slow_handler_is_reported_once(
    clock: FakeClock, sleep: FakeSleep, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="seekrelay")

    def slow(event: RelayEvent) -> None:
        time.sleep(0.02)

    relay = make_relay(FakeProvider("a"), clock=clock, sleep=sleep, on_event=slow)
    await relay.search("q1")
    await relay.search("q2")
    assert len(caplog.records) == 1
    assert "hand slow work to a queue" in caplog.records[0].getMessage()


async def test_events_carry_no_query_url_or_provider_text(
    clock: FakeClock, sleep: FakeSleep
) -> None:
    events = Events()
    secret = "zebra-quokka-7731"
    a = FakeProvider("a", ProviderError("a", f"HTTP 500: you asked about {secret}"), extract=True)
    b = FakeProvider("b", ["https://b.com/1"], extract=True)
    relay = make_relay(a, b, clock=clock, sleep=sleep, on_event=events)
    await relay.search(secret, include_domains=[f"{secret}.example"])
    await relay.extract(f"https://{secret}.example/{secret}")
    assert events
    for event in events:
        assert secret not in repr(event)
        assert secret not in json.dumps(event.to_dict())


async def test_stats_and_status(clock: FakeClock, sleep: FakeSleep) -> None:
    a = FakeProvider("a", QuotaExceededError("a", "limit"))
    b = FakeProvider("b", ["https://b.com/1"])
    relay = make_relay(a, b, clock=clock, sleep=sleep, cooldown_seconds=60)
    await relay.search("q1")
    await relay.search("q2")

    stats = relay.stats()
    assert stats.searches == 2
    sa, sb = stats.providers["a"], stats.providers["b"]
    assert (sa.calls, sa.ok, sa.failed, sa.cooldowns) == (1, 0, 1, 1)
    assert dict(sa.errors) == {"QuotaExceededError": 1}
    assert (sb.calls, sb.ok, sb.served_as_backup) == (2, 2, 2)
    json.dumps(stats.to_dict())

    status = relay.status()
    assert list(status) == ["a", "b"]
    assert status["a"].state == "down" and status["a"].cooldown_remaining == 60
    assert status["a"].last_error == "QuotaExceededError"
    assert status["b"].state == "up" and status["b"].cooldown_remaining == 0
    assert status["a"].since >= stats.started_at
    json.dumps(status["a"].to_dict())


async def test_stats_are_a_snapshot(clock: FakeClock, sleep: FakeSleep) -> None:
    relay = make_relay(FakeProvider("a", QuotaExceededError("a", "x")), clock=clock, sleep=sleep)
    with pytest.raises(AllProvidersFailedError):
        await relay.search("q")
    snapshot = relay.stats()
    snapshot.providers["a"].errors["QuotaExceededError"] = 99  # type: ignore[index]
    assert relay.stats().providers["a"].errors["QuotaExceededError"] == 1


async def test_error_counters_have_a_fixed_number_of_keys(
    clock: FakeClock, sleep: FakeSleep
) -> None:
    kinds = [type(f"Custom{i}Error", (ProviderError,), {}) for i in range(40)]
    a = FakeProvider("a", *(kind("a", "x") for kind in kinds))
    relay = make_relay(a, clock=clock, sleep=sleep)
    for i in range(40):
        with pytest.raises(AllProvidersFailedError):
            await relay.search(f"q{i}")
    errors = relay.stats().providers["a"].errors
    assert len(errors) == 33 and errors["other"] == 8


async def test_memory_does_not_grow_with_events(clock: FakeClock, sleep: FakeSleep) -> None:
    seen = 0

    def count(event: RelayEvent) -> None:
        nonlocal seen
        seen += 1

    a = FakeProvider("a", QuotaExceededError("a", "limit"))
    relay = make_relay(a, FakeProvider("b"), clock=clock, sleep=sleep, on_event=count)
    query = SearchQuery("q")
    for _ in range(200):  # warm up allocator caches
        await relay.search(query)
        await asyncio.sleep(0)
    gc.collect()
    before = len(gc.get_objects())
    for _ in range(5000):
        await relay.search(query)
        # A real provider waits for the network, and the loop then drops the cancelled
        # timers of each call's time budget. FakeProvider never yields, so yield here.
        await asyncio.sleep(0)
    gc.collect()
    assert seen > 10_000
    assert len(gc.get_objects()) - before < 200  # not one object per call or event


async def test_rate_limit(clock: FakeClock, sleep: FakeSleep) -> None:
    events = Events()
    a = FakeProvider("a")
    relay = make_relay(
        a,
        clock=clock,
        sleep=sleep,
        cache_ttl=60,
        max_requests_per_minute=2,
        on_event=events,
    )
    await relay.search("q1")
    await relay.search("q2")
    await relay.search("q1")  # a cache hit is free
    with pytest.raises(LimitExceededError) as info:
        await relay.search("q3")
    assert info.value.retry_after == pytest.approx(30)
    assert a.calls == 2
    assert events.kinds[-1] == "limit_exceeded"
    assert events[-1].seconds == pytest.approx(30)
    assert relay.stats().limit_exceeded == 1

    clock.advance(30)
    await relay.search("q3")
    assert a.calls == 3


def test_limits_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_requests_per_minute"):
        SeekRelay([FakeProvider("a")], max_requests_per_minute=0)
    with pytest.raises(ValueError, match="cache_max_bytes"):
        SeekRelay([FakeProvider("a")], cache_max_bytes=0)


class SlowProvider(FakeProvider):
    def __init__(self, name: str, seconds: float) -> None:
        super().__init__(name)
        self.seconds = seconds

    async def search(self, query: SearchQuery) -> SearchResponse:
        self.calls += 1
        await asyncio.sleep(self.seconds)
        return await super().search(query)


def test_default_time_budget() -> None:
    assert relay_module.DEFAULT_TIME_BUDGET == 30
    assert SeekRelay([FakeProvider("a")])._time_budget == 30


async def test_time_budget_per_call(clock: FakeClock, sleep: FakeSleep) -> None:
    events = Events()
    relay = make_relay(SlowProvider("slow", 0.2), clock=clock, sleep=sleep, on_event=events)
    with pytest.raises(AllProvidersFailedError) as info:
        await relay.search("q", time_budget=0.05)
    assert info.value.attempts[0].error == "TimeBudgetExceededError"
    assert relay.status()["slow"].state == "up"  # a used-up budget is not the provider's fault

    strict = make_relay(SlowProvider("slow", 0.1), clock=clock, sleep=sleep, time_budget=0.01)
    assert (await strict.search("q", time_budget=None)).provider == "slow"
    with pytest.raises(ValueError, match="time_budget"):
        await strict.search("q", time_budget=0)


def test_time_budget_in_the_sync_api() -> None:
    with SeekRelay([SlowProvider("slow", 0.2)], cache_ttl=None) as relay:
        with pytest.raises(AllProvidersFailedError):
            relay.search_sync("q", time_budget=0.05)
        assert relay.search_sync("q", time_budget=None).provider == "slow"


def test_time_budget_error_is_unavailable_and_not_retried() -> None:
    error = TimeBudgetExceededError("a", "cancelled")
    assert isinstance(error, ProviderUnavailableError)
    assert not error.retryable


async def test_extract_url_limit(clock: FakeClock, sleep: FakeSleep) -> None:
    capable = FakeProvider("t", extract=True)
    relay = make_relay(capable, clock=clock, sleep=sleep)
    with pytest.raises(ValueError, match="At most 100 URLs"):
        await relay.extract([f"https://a.example/{i}" for i in range(101)])
    await relay.extract([f"https://a.example/{i}" for i in range(100)])
    assert capable.calls == 1


async def test_backup_answers_are_marked_for_the_model(clock: FakeClock, sleep: FakeSleep) -> None:
    a = FakeProvider("a", QuotaExceededError("a", "limit"), extract=True)
    b = FakeProvider("b", ["https://b.com/1"], extract=True)
    relay = make_relay(a, b, clock=clock, sleep=sleep, cache_ttl=60)
    backup = await relay.search("q")
    assert "backup provider" in backup.to_llm_context()
    assert "backup provider" not in (await relay.search("q")).to_llm_context()  # cache hit
    assert "backup provider" in (await relay.extract("https://b.com/1")).to_llm_context()
    direct = make_relay(FakeProvider("c"), clock=clock, sleep=sleep)
    assert "backup provider" not in (await direct.search("q")).to_llm_context()
