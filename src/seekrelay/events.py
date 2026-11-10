"""Events for an ``on_event`` callback, counters and provider state.

The relay creates an event only when a callback is set, hands it over and keeps no reference
to it, so memory does not grow with the number of calls. Counters and state have a fixed
size: a few numbers per provider, keyed only by provider names and error classes.
See docs/MONITORING.md.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "DOWN_AFTER_FAILURES",
    "EventHandler",
    "EventKind",
    "Operation",
    "ProviderStats",
    "ProviderStatus",
    "RelayEvent",
    "RelayStats",
    "Severity",
]

EventKind = Literal[
    "served",
    "retry",
    "failure",
    "cooldown",
    "fallback",
    "all_failed",
    "blocked",
    "limit_exceeded",
    "provider_down",
    "provider_up",
]
Severity = Literal["info", "warning", "alert"]
Operation = Literal["search", "extract"]

SEVERITY: dict[EventKind, Severity] = {
    "served": "info",
    "retry": "info",
    "failure": "warning",
    "cooldown": "warning",
    "fallback": "warning",
    "all_failed": "warning",
    "blocked": "warning",
    "limit_exceeded": "warning",
    "provider_down": "alert",
    "provider_up": "alert",
}

DOWN_AFTER_FAILURES = 3
"""Failed calls in a row after which a provider counts as down (a cooldown counts at once)."""

_MAX_ERROR_KINDS = 32  # a guard: error classes are a fixed set, but a custom provider may add


@dataclass(frozen=True, slots=True)
class RelayEvent:
    """One thing that happened during a ``search`` or ``extract`` call.

    An event holds names, numbers and the error class only: never the query, URLs, API keys
    or the provider's own text, so it can go to a monitoring service as it is. Fields that do
    not apply to a kind are ``None``. New kinds may appear in minor versions: skip the ones
    you do not know.
    """

    kind: EventKind
    severity: Severity
    operation: Operation
    provider: str | None = None
    error: str | None = None
    status_code: int | None = None
    attempt: int | None = None
    seconds: float | None = None
    latency_ms: float | None = None
    results: int | None = None
    failed: int | None = None
    from_cache: bool = False
    at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "operation": self.operation,
            "provider": self.provider,
            "error": self.error,
            "status_code": self.status_code,
            "attempt": self.attempt,
            "seconds": self.seconds,
            "latency_ms": self.latency_ms,
            "results": self.results,
            "failed": self.failed,
            "from_cache": self.from_cache,
            "at": self.at,
        }


EventHandler = Callable[[RelayEvent], object]
"""A plain (not async) function. It runs inside the call, so it must return quickly."""


@dataclass(frozen=True, slots=True)
class ProviderStats:
    """Counters of one provider since the relay was created.

    ``calls`` counts calls of the provider (with their retries), ``ok`` and ``failed`` how
    they ended, ``errors`` every error seen by class, including failed batches of an
    ``extract`` that still returned pages. Latency covers successful calls.
    """

    calls: int = 0
    ok: int = 0
    failed: int = 0
    retries: int = 0
    cooldowns: int = 0
    served_as_backup: int = 0
    latency_ms_total: float = 0.0
    latency_ms_max: float = 0.0
    errors: Mapping[str, int] = field(default_factory=dict)

    @property
    def latency_ms_avg(self) -> float:
        return self.latency_ms_total / self.ok if self.ok else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "ok": self.ok,
            "failed": self.failed,
            "retries": self.retries,
            "cooldowns": self.cooldowns,
            "served_as_backup": self.served_as_backup,
            "latency_ms_avg": round(self.latency_ms_avg, 1),
            "latency_ms_max": round(self.latency_ms_max, 1),
            "errors": dict(self.errors),
        }


@dataclass(frozen=True, slots=True)
class RelayStats:
    """A snapshot of the relay's counters since it was created.

    The counters only grow. For a period, subtract an earlier snapshot from a later one.
    """

    started_at: float
    searches: int
    extracts: int
    cache_hits: int
    all_failed: int
    blocked: int
    limit_exceeded: int
    handler_errors: int
    providers: Mapping[str, ProviderStats]

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "searches": self.searches,
            "extracts": self.extracts,
            "cache_hits": self.cache_hits,
            "all_failed": self.all_failed,
            "blocked": self.blocked,
            "limit_exceeded": self.limit_exceeded,
            "handler_errors": self.handler_errors,
            "providers": {name: s.to_dict() for name, s in self.providers.items()},
        }


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """The state of one provider right now.

    ``since`` is the wall-clock time (``time.time()``) of the last change between ``up`` and
    ``down``, or of the relay's creation.
    """

    name: str
    state: Literal["up", "down"]
    since: float
    cooldown_remaining: float
    consecutive_failures: int
    last_error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "since": self.since,
            "cooldown_remaining": round(self.cooldown_remaining, 1),
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
        }


class _ProviderRecord:
    """Mutable counters and state of one provider; a fixed set of slots."""

    __slots__ = (
        "calls",
        "changed_at",
        "consecutive_failures",
        "cooldowns",
        "down_since",
        "errors",
        "failed",
        "last_error",
        "latency_max",
        "latency_total",
        "ok",
        "retries",
        "served_as_backup",
        "up",
    )

    def __init__(self, now: float) -> None:
        self.calls = 0
        self.ok = 0
        self.failed = 0
        self.retries = 0
        self.cooldowns = 0
        self.served_as_backup = 0
        self.latency_total = 0.0
        self.latency_max = 0.0
        self.errors: dict[str, int] = {}
        self.up = True
        self.changed_at = now  # wall clock, for status()
        self.down_since = 0.0  # relay clock, for the length of an outage
        self.consecutive_failures = 0
        self.last_error: str | None = None


class Monitor:
    """Counters and up/down state behind ``SeekRelay.stats()`` and ``status()``."""

    def __init__(self, names: Iterable[str], clock: Callable[[], float]) -> None:
        self._clock = clock
        self.started_at = time.time()
        self._records = {name: _ProviderRecord(self.started_at) for name in names}
        self.searches = 0
        self.extracts = 0
        self.cache_hits = 0
        self.all_failed = 0
        self.blocked = 0
        self.limit_exceeded = 0
        self.handler_errors = 0

    def called(self, name: str) -> None:
        self._records[name].calls += 1

    def retried(self, name: str) -> None:
        self._records[name].retries += 1

    def error_seen(self, name: str, error: str) -> None:
        record = self._records[name]
        record.last_error = error
        if error not in record.errors and len(record.errors) >= _MAX_ERROR_KINDS:
            error = "other"
        record.errors[error] = record.errors.get(error, 0) + 1

    def succeeded(self, name: str, latency_ms: float, *, backup: bool) -> float | None:
        """Count a successful call. Returns the outage length if the provider was down."""
        record = self._records[name]
        record.ok += 1
        record.latency_total += latency_ms
        record.latency_max = max(record.latency_max, latency_ms)
        if backup:
            record.served_as_backup += 1
        record.consecutive_failures = 0
        if record.up:
            return None
        record.up = True
        record.changed_at = time.time()
        return max(self._clock() - record.down_since, 0.0)

    def failed(self, name: str, error: str, *, health: bool) -> bool:
        """Count a failed call. Returns True when the provider has just gone down.

        ``health`` is False for errors that say nothing about the provider (a rejected
        request, a used-up time budget): they are counted but do not take it down.
        """
        record = self._records[name]
        record.failed += 1
        self.error_seen(name, error)
        if not health:
            return False
        record.consecutive_failures += 1
        return record.consecutive_failures >= DOWN_AFTER_FAILURES and self._go_down(record)

    def paused(self, name: str) -> bool:
        """Count a cooldown, which takes the provider down at once. True if it was up."""
        record = self._records[name]
        record.cooldowns += 1
        return self._go_down(record)

    def _go_down(self, record: _ProviderRecord) -> bool:
        if not record.up:
            return False
        record.up = False
        record.changed_at = time.time()
        record.down_since = self._clock()
        return True

    def stats(self) -> RelayStats:
        return RelayStats(
            started_at=self.started_at,
            searches=self.searches,
            extracts=self.extracts,
            cache_hits=self.cache_hits,
            all_failed=self.all_failed,
            blocked=self.blocked,
            limit_exceeded=self.limit_exceeded,
            handler_errors=self.handler_errors,
            providers={
                name: ProviderStats(
                    calls=r.calls,
                    ok=r.ok,
                    failed=r.failed,
                    retries=r.retries,
                    cooldowns=r.cooldowns,
                    served_as_backup=r.served_as_backup,
                    latency_ms_total=r.latency_total,
                    latency_ms_max=r.latency_max,
                    errors=dict(r.errors),
                )
                for name, r in self._records.items()
            },
        )

    def status(self, name: str, cooldown_remaining: float) -> ProviderStatus:
        record = self._records[name]
        return ProviderStatus(
            name=name,
            state="up" if record.up else "down",
            since=record.changed_at,
            cooldown_remaining=cooldown_remaining,
            consecutive_failures=record.consecutive_failures,
            last_error=record.last_error,
        )
