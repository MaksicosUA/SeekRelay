# Configuration

There are two ways to configure the gateway: environment variables (via
`SearchGateway.from_env()` or the CLI) and constructor arguments. Constructor arguments
and `from_env(**overrides)` win over the environment.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `SEARCH_GATEWAY_PROVIDERS` | `tavily,brave,searxng` | Priority order, comma-separated. Providers without credentials are skipped |
| `TAVILY_API_KEY` | - | Enables Tavily |
| `TAVILY_INCLUDE_ANSWER` | `false` | Ask Tavily for a short generated answer (`response.answer`) |
| `BRAVE_API_KEY` | - | Enables Brave. `BRAVE_SEARCH_API_KEY` is accepted as an alias |
| `SEARXNG_BASE_URL` | - | Enables SearXNG, e.g. `http://127.0.0.1:8888` |
| `SEARXNG_ENGINES` | instance default | Comma-separated engine names |
| `SEARXNG_AUTH_HEADER` | - | Full `Authorization` header value for a protected instance (e.g. `Basic dXNlcjpwYXNz`) |
| `SEARCH_GATEWAY_TIMEOUT` | `20` | Per-request timeout in seconds |
| `SEARCH_GATEWAY_MAX_ATTEMPTS` | `2` | Attempts per provider for retryable errors (1 = no retries) |
| `SEARCH_GATEWAY_CACHE_TTL` | `600` | Seconds to cache identical queries; `0` disables the cache |
| `SEARCH_GATEWAY_COOLDOWN` | `300` | Seconds to skip a provider after an auth or quota error |
| `SEARCH_GATEWAY_PII_GUARD` | `true` | `false` disables the PII guard |

Boolean values accept `true/false`, `1/0`, `yes/no`, `on/off`. An empty value means
"use the default" (so `SEARCH_GATEWAY_PII_GUARD=` keeps the guard on). Numbers must be
finite and non-negative. Invalid values raise `ConfigurationError` at start-up rather
than being silently ignored. Provider names are case-insensitive.

### `.env` files

The CLI accepts `--env-file .env`. In code:

```python
import os
from search_gateway import SearchGateway
from search_gateway.config import read_env_file

env = {**read_env_file(".env"), **os.environ}  # real environment wins
gateway = SearchGateway.from_env(env)
```

`read_env_file` supports `KEY=VALUE`, `export KEY=VALUE`, single or double quotes
(text after the closing quote is treated as a comment, and `#` inside quotes is kept)
and ` #` comments after unquoted values. Use python-dotenv if you need variable
interpolation.

## Constructor options

### `SearchGateway`

| Argument | Default | Description |
|---|---|---|
| `providers` | required | Providers in priority order; names must be unique |
| `retry` | `RetryPolicy()` | See below |
| `cache_ttl` | `600.0` | `None`/`0` disables caching |
| `cache_size` | `256` | Max cached queries (LRU) |
| `pii_guard` | `PIIGuard()` | `None` disables |
| `fallback_on_invalid_request` | `False` | Fall back on 400/422 too (see ARCHITECTURE.md for why it is off) |
| `fallback_on_empty` | `False` | Try the next provider when one returns zero results |
| `cooldown_seconds` | `300.0` | Cooldown after auth/quota errors; `0` disables |

### `RetryPolicy`

| Field | Default | Description |
|---|---|---|
| `max_attempts` | `2` | Attempts per provider, including the first |
| `base_delay` | `0.5` | First back-off delay; doubles each retry |
| `max_delay` | `8.0` | Cap per delay; longer `Retry-After` values trigger fallback instead of waiting |
| `jitter` | `0.2` | Random +/- fraction applied to computed delays |

### Providers

All providers accept `timeout` (seconds), `client` (an `httpx.AsyncClient` to reuse;
not closed by the provider) and `user_agent`.

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

## Logging

The library logs to the `search_gateway` logger and is silent unless the application
configures logging:

```python
import logging

logging.basicConfig(level=logging.INFO)  # fallbacks and retries
logging.getLogger("search_gateway").setLevel(logging.DEBUG)  # cache hits, skipped params
```

Levels: `WARNING` for provider failures, `INFO` for retries and served-after-fallback,
`DEBUG` for cache hits and parameters a provider cannot express. Keys and query PII are
never logged.

## Recipes

**Cheapest first for a scheduled job**

```bash
SEARCH_GATEWAY_PROVIDERS=searxng,brave,tavily
SEARCH_GATEWAY_CACHE_TTL=3600
```

**Interactive agent, fail fast**

```python
SearchGateway.from_env(retry=RetryPolicy(max_attempts=1), fallback_on_empty=True)
```

**Tests in your own project**: inject a provider whose client uses
`httpx.MockTransport`, exactly as this project's `tests/` do.
