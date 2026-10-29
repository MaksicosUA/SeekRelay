"""In-memory TTL + LRU cache for search responses.

Agents often repeat near-identical lookups within one task. Caching identical queries
for a few minutes saves credits and latency. The cache is per-process; for a shared
cache across workers put Redis behind the same interface.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Generic, TypeVar

__all__ = ["TTLCache"]

V = TypeVar("V")


class TTLCache(Generic[V]):
    """A small LRU cache whose entries expire ``ttl`` seconds after insertion."""

    def __init__(
        self,
        ttl: float = 600.0,
        max_entries: int = 256,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._ttl = ttl
        self._max_entries = max_entries
        self._clock = clock
        self._data: OrderedDict[Hashable, tuple[float, V]] = OrderedDict()

    def get(self, key: Hashable) -> V | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: Hashable, value: V) -> None:
        self._data[key] = (self._clock() + self._ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self._max_entries:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)
