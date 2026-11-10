from __future__ import annotations

import pytest

from seekrelay import SeekRelay, TTLCache

from .conftest import FakeClock, FakeProvider, FakeSleep


def test_byte_limit_drops_the_least_recently_used(clock: FakeClock) -> None:
    cache: TTLCache[str] = TTLCache(60, 100, clock=clock, max_bytes=10, sizeof=len)
    cache.set("a", "xxxx")
    cache.set("b", "yyyy")
    assert cache.get("a") == "xxxx"  # now the most recent
    cache.set("c", "zzzz")  # 12 bytes: "b" has to go
    assert cache.get("b") is None
    assert cache.get("a") == "xxxx" and cache.get("c") == "zzzz"
    assert cache.size_bytes == 8

    cache.set("big", "x" * 11)  # larger than the whole limit: not stored
    assert cache.get("big") is None and cache.size_bytes == 8
    cache.set("a", "x")  # a new value replaces the old size
    assert cache.size_bytes == 5
    cache.clear()
    assert cache.size_bytes == 0 and len(cache) == 0


def test_expired_entries_free_their_bytes(clock: FakeClock) -> None:
    cache: TTLCache[str] = TTLCache(60, 100, clock=clock, max_bytes=10, sizeof=len)
    cache.set("a", "xxxx")
    clock.advance(61)
    assert cache.get("a") is None
    assert cache.size_bytes == 0


def test_max_bytes_needs_sizeof() -> None:
    with pytest.raises(ValueError, match="sizeof"):
        TTLCache(60, max_bytes=10)
    assert TTLCache(60).size_bytes == 0  # entries only, as before


async def test_relay_skips_a_response_over_the_byte_limit(
    clock: FakeClock, sleep: FakeSleep
) -> None:
    small = FakeProvider("a")
    relay = SeekRelay([small], cache_max_bytes=100, clock=clock, sleep=sleep)
    await relay.search("q")
    await relay.search("q")
    assert small.calls == 2
    roomy = FakeProvider("b")
    relay = SeekRelay([roomy], clock=clock, sleep=sleep)
    await relay.search("q")
    assert (await relay.search("q")).from_cache
    assert roomy.calls == 1
