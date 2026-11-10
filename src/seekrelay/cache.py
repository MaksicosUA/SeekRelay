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
    """A small LRU cache whose entries expire ``ttl`` seconds after insertion.

    With ``max_bytes`` and ``sizeof`` (an estimate of one value's size), the least recently
    used entries also go once the total passes ``max_bytes``; a value larger than that on its
    own is not stored.
    """

    def __init__(
        self,
        ttl: float = 600.0,
        max_entries: int = 256,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_bytes: int | None = None,
        sizeof: Callable[[V], int] | None = None,
    ) -> None:
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        if max_bytes is not None and (max_bytes < 1 or sizeof is None):
            raise ValueError("max_bytes must be positive and needs sizeof")
        self._ttl = ttl
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._sizeof = sizeof
        self._clock = clock
        self._data: OrderedDict[Hashable, tuple[float, V, int]] = OrderedDict()
        self._bytes = 0

    def get(self, key: Hashable) -> V | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, value, _ = entry
        if self._clock() >= expires_at:
            self._drop(key)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: Hashable, value: V) -> None:
        size = self._sizeof(value) if self._max_bytes is not None and self._sizeof else 0
        self._drop(key)
        if self._max_bytes is not None and size > self._max_bytes:
            return
        self._data[key] = (self._clock() + self._ttl, value, size)
        self._bytes += size
        while len(self._data) > self._max_entries or (
            self._max_bytes is not None and self._bytes > self._max_bytes
        ):
            _, (_, _, dropped) = self._data.popitem(last=False)
            self._bytes -= dropped

    def clear(self) -> None:
        self._data.clear()
        self._bytes = 0

    @property
    def size_bytes(self) -> int:
        """Estimated size of the stored values (0 without ``max_bytes``)."""
        return self._bytes

    def _drop(self, key: Hashable) -> None:
        entry = self._data.pop(key, None)
        if entry is not None:
            self._bytes -= entry[2]

    def __len__(self) -> int:
        return len(self._data)
