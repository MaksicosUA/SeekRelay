# Configuration

There are two ways to configure the relay: environment variables (via
`SeekRelay.from_env()` or the CLI) and constructor arguments. Constructor arguments
and `from_env(**overrides)` win over the environment.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `SEEKRELAY_PROVIDERS` | `tavily,brave,searxng` | Priority order, comma-separated. Providers without credentials are skipped |
| `TAVILY_API_KEY` | - | Enables Tavily |
| `TAVILY_INCLUDE_ANSWER` | `false` | Ask Tavily for a short generated answer (`response.answer`) |
| `BRAVE_API_KEY` | - | Enables Brave. `BRAVE_SEARCH_API_KEY` is accepted as an alias |
| `SEARXNG_BASE_URL` | - | Enables SearXNG, e.g. `http://127.0.0.1:8888` |
| `SEARXNG_ENGINES` | instance default | Comma-separated engine names for web search; they replace the instance's default engines. News searches use the instance's news engines |
| `SEARXNG_AUTH_HEADER` | - | Full `Authorization` header value for a protected instance (e.g. `Basic dXNlcjpwYXNz`) |
| `SEEKRELAY_TIMEOUT` | `20` | Limit for one request in seconds, from sending it to the last byte of the response. A request that runs out of time is not retried: the relay moves on to the next provider |
| `SEEKRELAY_MAX_ATTEMPTS` | `2` | Attempts per provider for retryable errors (1 = no retries) |
| `SEEKRELAY_CACHE_TTL` | `600` | Seconds to cache identical queries; `0` disables the cache |
| `SEEKRELAY_CACHE_MAX_MB` | `64` | Size limit of the cache in MiB (an estimate of the stored text); `0` limits the number of entries only |
| `SEEKRELAY_COOLDOWN` | `300` | Seconds to skip a provider after an auth or quota error |
| `SEEKRELAY_TIME_BUDGET` | `30` | Max seconds for one `search`/`extract` call across all providers, retries and back-off; `0` means no limit |
| `SEEKRELAY_MAX_CONCURRENCY` | `8` | Requests in flight to each provider; more wait for a free slot. `0` means no cap |
| `SEEKRELAY_MAX_REQUESTS_PER_MINUTE` | no limit | Calls per minute that may reach the providers (cache hits are free); `0` or empty means no limit |
| `SEEKRELAY_PII_GUARD` | `true` | `false` disables the PII guard |

Boolean values accept `true/false`, `1/0`, `yes/no`, `on/off`. An empty value means
"use the default" (so `SEEKRELAY_PII_GUARD=` keeps the guard on). Numbers must be
finite and non-negative. Invalid values raise `ConfigurationError` at start-up rather
than being silently ignored. Provider names are case-insensitive.

### `.env` files

No file is read unless you name it. `./.env` in the current directory is ignored, because
it may belong to a repository you have just cloned, and it could send your queries elsewhere
or switch the PII guard off. If no provider is configured and `./.env` exists, the CLI says
so. Name the file before the command:

```bash
seekrelay --env-file .env search "python asyncio taskgroup"
```

In code, use an absolute path. A relative one resolves against the working directory, and
an agent process often inherits the directory of the repository it works in:

```python
from pathlib import Path

from seekrelay import SeekRelay

relay = SeekRelay.from_env(env_file=Path(__file__).with_name(".env"))  # real environment wins
```

`from_env()` without `env_file` reads only the process environment. In both cases a
variable that is already set in the environment wins over the file, even when it is
empty (`TAVILY_API_KEY= seekrelay search ...` skips Tavily for one call). The two Brave
names count as one setting.

`read_env_file` supports `KEY=VALUE`, `export KEY=VALUE`, single or double quotes
(text after the closing quote is treated as a comment, and `#` inside quotes is kept)
and ` #` comments after unquoted values, so `KEY= # note` is empty. It reads a file the way
the shell does: lines end at LF or CRLF, only spaces and tabs are blanks, and a carriage return
inside a line is an error. A UTF-8 BOM is skipped, and a file over 1 MiB is refused. Pipes work
(`--env-file <(sops -d secrets.env)`). Use python-dotenv if you need variable interpolation.

## Constructor options

### `SeekRelay`

| Argument | Default | Description |
|---|---|---|
| `providers` | required | Providers in priority order; names must be unique |
| `retry` | `RetryPolicy()` | See below |
| `cache_ttl` | `600.0` | `None`/`0` disables caching |
| `cache_size` | `256` | Max cached queries (LRU) |
| `cache_max_bytes` | `64 MiB` | Max estimated size of the cache; least recently used entries go first, and a response larger than the limit is not cached. `None` limits entries only |
| `pii_guard` | `PIIGuard()` | `None` disables |
| `fallback_on_invalid_request` | `False` | Fall back on 400/422 too (see ARCHITECTURE.md for why it is off) |
| `fallback_on_empty` | `False` | Try the next provider when one returns zero results |
| `cooldown_seconds` | `300.0` | Cooldown after auth/quota errors; `0` disables |
| `time_budget` | `30.0` | Max seconds for one call across all providers; when it runs out the running request is cancelled, the rest are skipped and the call raises `AllProvidersFailedError`. `None` means no limit. `search(..., time_budget=5)` and `extract(..., time_budget=5)` set it for one call, `time_budget=None` lifts it for one call |
| `max_requests_per_minute` | `None` | Calls per minute that may reach the providers; over it a call raises `LimitExceededError` at once (with `retry_after`) and sends nothing. Cache hits are free. `None` means no limit |
| `on_event` | `None` | A plain function that receives a `RelayEvent` at each step; see [MONITORING.md](MONITORING.md) |

### `RetryPolicy`

| Field | Default | Description |
|---|---|---|
| `max_attempts` | `2` | Attempts per provider, including the first |
| `base_delay` | `0.5` | First back-off delay; doubles each retry |
| `max_delay` | `8.0` | Cap per delay; longer `Retry-After` values trigger fallback instead of waiting |
| `jitter` | `0.2` | Random +/- fraction applied to computed delays |

### Providers

All providers accept `timeout` (seconds), `client` (an `httpx.AsyncClient` to reuse;
not closed by the provider), `user_agent` and `max_concurrency` (requests in flight to this
provider, default 8, `None` for no cap; a request waiting for a slot is not yet timed).

| Provider | Extra arguments |
|---|---|
| `TavilyProvider(api_key, ...)` | `base_url`, `include_answer` |
| `BraveProvider(api_key, ...)` | `base_url`, `safesearch` (`off`/`moderate`/`strict`), `extra_snippets` |
| `SearXNGProvider(base_url, ...)` | `engines`, `safesearch` (0/1/2), `headers` |

### `PIIGuard`

| Field | Default |
|---|---|
| `detect_emails` | `True` |
| `detect_cards` | `True` |
| `detect_ibans` | `True` |
| `detect_phones` | `False` (noisy) |
| `detect_tax_ids` | `True` |
| `detect_secrets` | `True` |

Rules and known gaps: [SECURITY.md](SECURITY.md#pii-guard-details).

## Logging

The library logs to the `seekrelay` logger and is silent unless the application
configures logging:

```python
import logging

logging.basicConfig(level=logging.INFO)  # fallbacks and retries
logging.getLogger("seekrelay").setLevel(logging.DEBUG)  # cache hits, skipped params
```

Levels: `WARNING` for provider failures (the provider, error class and status) and, once
per relay, for an `on_event` handler that raised or took longer than 10 ms, `INFO` for
retries and served-after-fallback, `DEBUG` for the provider's own error text, tracebacks of
unexpected errors, cache hits and parameters a provider cannot express. The text is cleaned
and kept on one line; the raw message stays in `response.attempts`. Keys and query PII are
never logged. For data your code can act on, use events and counters
([MONITORING.md](MONITORING.md)) rather than parsing log lines.

## Recipes

**Cheapest first for a scheduled job**

```bash
SEEKRELAY_PROVIDERS=searxng,brave,tavily
SEEKRELAY_CACHE_TTL=3600
```

**Interactive agent, fail fast**

```python
SeekRelay.from_env(
    retry=RetryPolicy(max_attempts=1),
    fallback_on_empty=True,
    time_budget=15,  # live runs: enough for one fallback
    max_requests_per_minute=30,  # an agent in a loop cannot burn the credits
)
```

**Tests in your own project**: inject a provider whose client uses
`httpx.MockTransport`, exactly as this project's `tests/` do.
