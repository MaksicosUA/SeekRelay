# Monitoring

A fallback keeps answers coming when a provider fails, which also hides the failure: the
backup answers, quality drops, and nobody notices. SeekRelay gives three ways to see it,
without any monitoring service built in:

| What | How you get it | For |
|---|---|---|
| Events | `SeekRelay(on_event=handler)`: a plain function called at each step | metrics, alerts, a history in your own storage |
| Counters | `relay.stats()`: a snapshot since the relay was created | dashboards, a summary per hour |
| State | `relay.status()`: each provider `up` or `down` right now | an admin page, a health check, an agent tool |

Where the data goes (Telegram, Slack, Prometheus, Sentry, a file) is up to your code; the
package contains no client for any of them. Ready examples to copy:
[telegram_alerts.py](../examples/telegram_alerts.py),
[hourly_summary.py](../examples/hourly_summary.py),
[events_to_jsonl.py](../examples/events_to_jsonl.py).

```python
from seekrelay import RelayEvent, SeekRelay


def on_event(event: RelayEvent) -> None:
    if event.severity == "alert":  # a provider went down or came back
        alert_queue.put_nowait(f"{event.provider}: {event.kind} ({event.error})")


relay = SeekRelay.from_env(on_event=on_event)
```

## Events

| `kind` | `severity` | When | Fields set |
|---|---|---|---|
| `served` | info | an answer was returned, also from the cache | `provider`, `results`, `latency_ms`, `from_cache`; `failed` for `extract` |
| `retry` | info | the same provider is asked again | `provider`, `error`, `status_code`, `attempt` (the next one), `seconds` (the wait) |
| `failure` | warning | a provider answered with an error | `provider`, `error`, `status_code` |
| `cooldown` | warning | a provider is skipped for a while | `provider`, `error`, `seconds` |
| `fallback` | warning | the answer comes from a provider after the first one | `provider` (who answered) |
| `all_failed` | warning | no provider answered | `attempt` (number of attempts) |
| `blocked` | warning | the PII guard stopped the call | `error` |
| `limit_exceeded` | warning | `max_requests_per_minute` stopped the call | `error`, `seconds` (until the next call can pass) |
| `provider_down` | alert | a provider stopped working | `provider`, `error`, `status_code` |
| `provider_up` | alert | it works again | `provider`, `seconds` (length of the outage) |

Every event also has `operation` (`search` or `extract`) and `at` (`time.time()`). A call
where the first provider runs out of credits gives
`failure`, `cooldown`, `provider_down`, `fallback`, `served`; while that provider is paused,
each call gives `fallback`, `served`.

**What an event never holds:** the query, URLs, API keys or the provider's own error text.
Only names, numbers and the error class, so an event can go to a third-party service as it
is. `event.to_dict()` gives a JSON-ready dict.

New kinds may appear in minor versions; a handler should skip kinds it does not know.
Renaming a kind or a field is a breaking change.

### Down and up

A provider goes `down` when it is put into cooldown (bad key, quota, a long `Retry-After`)
or after 3 failed calls in a row (`seekrelay.events.DOWN_AFTER_FAILURES`). While it is down,
further failures and cooldowns give no new `provider_down`. The first successful answer
gives `provider_up`. A broken key, paused for 5 minutes again and again, therefore makes one
`provider_down`, and one `provider_up` once the key is fixed, not an alert every 5 minutes.

Errors that say nothing about the provider never take it down: `InvalidRequestError`
(the request was refused), `UnsupportedOperationError` and `TimeBudgetExceededError` (the
call's budget ran out, possibly spent by an earlier provider). They are still counted.

### Rules for the handler

1. A plain function, not `async`. It runs inside the call, so it must return in
   microseconds: put slow work (an HTTP request to a chat) into a queue and send it from a
   background task, as `telegram_alerts.py` does. A handler slower than 10 ms is reported
   once in the `seekrelay` log.
2. An error inside the handler never breaks the call. It is logged once at `WARNING` and
   counted in `stats().handler_errors`.
3. Without a handler no event object is created at all; the counters still work.

## Counters: `stats()`

`relay.stats()` returns a `RelayStats` snapshot (`.to_dict()` for JSON):

| Field | Counts |
|---|---|
| `searches`, `extracts` | calls that passed argument checks |
| `cache_hits` | searches answered from the cache |
| `all_failed` | calls that ended in `AllProvidersFailedError` |
| `blocked` | calls stopped by the PII guard |
| `limit_exceeded` | calls stopped by `max_requests_per_minute` |
| `handler_errors` | errors raised by `on_event` |
| `providers[name]` | a `ProviderStats` per provider, below |

| `ProviderStats` field | Counts |
|---|---|
| `calls`, `ok`, `failed` | calls of this provider (retries included in one call) and how they ended |
| `retries`, `cooldowns` | retries and cooldowns started |
| `served_as_backup` | answers given after an earlier provider failed or was skipped |
| `latency_ms_avg`, `latency_ms_max` | latency of successful calls |
| `errors` | errors by class, including failed batches of an `extract` that still returned pages |

The counters only grow. For a period, subtract an earlier snapshot from a later one, as
`hourly_summary.py` does. They live in the process: a restart resets them, and with several
processes you add them up outside, for example from events.

## State: `status()`

`relay.status()` returns a dict in priority order of `ProviderStatus`: `state` (`up` or
`down`), `since` (wall-clock time of the last change, or of the relay's creation),
`cooldown_remaining` (seconds), `consecutive_failures` and `last_error` (a class name).

## Memory and cost

Events are not stored: the relay creates one, hands it to the handler and keeps no
reference. Counters and state have a fixed size, keyed only by provider names and error
classes (at most 32 classes per provider; more are counted as `other`), never by query text
or URLs. An event holds the error's class name, not the exception, so it cannot keep a
traceback, and with it a response body, alive.

Measured on one machine (Python 3.12): an event and a call of an empty handler cost about
1 microsecond, the counters about 0.14 microseconds per answer, against about 1.7 s for a
search through Tavily. Counters and state for three providers take about 2.5 KiB. 100,000
searches with 200,000 events left the number of live objects unchanged and raised peak
memory by 0.1 MiB.

## Is search degrading?

| Signal | Where |
|---|---|
| A provider is down | `provider_down` events, `status()` |
| Answers come from the backup | `served_as_backup` against `ok` of the first provider, `fallback` events |
| Nothing answers | `all_failed` in `stats()` and events |
| Credits or keys | `errors["QuotaExceededError"]`, `errors["AuthenticationError"]` |
| A looping agent | `limit_exceeded`, a jump in `searches` |
| Your monitoring itself fails | `handler_errors` |

The library also logs to the `seekrelay` logger (see
[CONFIGURATION.md](CONFIGURATION.md#logging)); logs are text for people, events are data
for code.
