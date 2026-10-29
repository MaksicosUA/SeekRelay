# SeekRelay

One async Python interface for web search in AI agents and RAG pipelines, with
**Tavily**, **Brave Search** and a self-hosted **SearXNG** behind it.

```python
from seekrelay import SeekRelay

async with SeekRelay.from_env() as relay:
    response = await relay.search("fastapi lifespan events", max_results=3, time_range="year")
    prompt_block = response.to_llm_context()  # escaped, marked as untrusted web content
```

If Tavily runs out of credits, the same call is served by Brave; if Brave is rate-limited,
by your own SearXNG. Application code never changes.

## Why

Search APIs for agents differ in price, quality, latency and failure modes, and any one
of them can change its pricing, run out of quota or go down. Hard-wiring one SDK into an
agent makes all of that your problem. SeekRelay puts a small, typed layer in
between:

| Problem | What SeekRelay does |
|---|---|
| Vendor lock-in, price changes | Providers are interchangeable behind `SearchProvider`; order is configuration |
| Credits run out mid-month (requests just stop) | `QuotaExceededError` -> provider goes into cooldown -> next provider |
| Timeouts, 5xx, 429 | Retries with exponential back-off, honours short `Retry-After`, falls back on long ones and skips that provider until then |
| An agent stuck waiting while every provider is slow | `time_budget` caps one call across all providers and retries |
| Different response shapes | One normalised `SearchResponse` / `SearchResult` model |
| Prompt injection via web pages | `to_llm_context()` escapes all web text and labels it untrusted |
| Agents leaking personal data into queries | `PIIGuard` blocks e-mails, card numbers and IBANs before any request |
| Paying twice for the same lookup | TTL cache for identical queries |
| Duplicate URLs (`utm_*`, `www.`, trailing `/`) | Canonical-URL de-duplication |

## Providers

| | Tavily | Brave Search | SearXNG |
|---|---|---|---|
| Type | AI-native search API | Independent web index | Self-hosted metasearch |
| Web search | yes | yes | yes |
| News search | yes | yes | yes (`news` category) |
| Page extraction | yes (`extract`) | no | no |
| Domain filter | native | `site:` + client-side | `site:` + client-side |
| Time filter | day/week/month/year | day/week/month/year | day/week/month/year* |
| Cost | credits (free tier) | per request | your server only |

\* depends on the upstream engines. Details, limits and pricing notes:
[docs/PROVIDERS.md](docs/PROVIDERS.md).

## Install

Requires Python 3.10+. The only runtime dependency is `httpx`.

```bash
git clone https://github.com/MaksicosUA/SeekRelay.git && cd SeekRelay
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # add at least one key or a SearXNG URL
```

The `seekrelay` CLI reads `./.env` automatically. In code, `SeekRelay.from_env()`
reads only the process environment; pass `env_file=".env"` to read the file too
(variables already set in the environment win).

## Usage

### Async

```python
import asyncio
from seekrelay import SeekRelay, AllProvidersFailedError, QueryRejectedError


async def main() -> None:
    async with SeekRelay.from_env() as relay:
        try:
            response = await relay.search(
                "remote python developer vacancy",
                max_results=10,
                time_range="week",
                include_domains=["djinni.co", "jobs.dou.ua"],
                language="uk",
                country="ua",
            )
        except QueryRejectedError as exc:  # personal data in the query
            print(exc)
            return
        except AllProvidersFailedError as exc:  # every provider failed / in cooldown
            print(exc.attempts)
            return

        print(response.provider, response.latency_ms, response.attempts)
        for result in response.results:
            print(result.title, result.url, result.snippet)

        pages = await relay.extract([r.url for r in response.results[:3]])
        print(pages.to_llm_context())


asyncio.run(main())
```

### Sync (scripts, notebooks without a running loop)

```python
from seekrelay import SeekRelay

with SeekRelay.from_env() as relay:
    response = relay.search_sync("python 3.13 free-threading", max_results=5)
```

### Explicit providers

```python
from seekrelay import (
    BraveProvider,
    RetryPolicy,
    SeekRelay,
    SearXNGProvider,
    TavilyProvider,
)

relay = SeekRelay(
    [
        SearXNGProvider("http://127.0.0.1:8888"),  # free, first
        BraveProvider(api_key="..."),
        TavilyProvider(api_key="tvly-...", include_answer=True),
    ],
    retry=RetryPolicy(max_attempts=3),
    cache_ttl=900,
    fallback_on_empty=True,
)
```

### As an agent tool

[examples/agent_tool.py](examples/agent_tool.py) contains a framework-neutral tool
definition (JSON schema) and a handler that returns `to_llm_context()` text and turns
errors into short messages the model can act on.

### CLI

```bash
seekrelay providers
seekrelay search "fastapi background tasks" -n 3
seekrelay search "LLM engineer remote" --time-range week \
    --include-domain djinni.co --include-domain jobs.dou.ua --format json
seekrelay --providers searxng,tavily search "rag evaluation" --format llm
seekrelay extract https://docs.searxng.org/dev/search_api.html
```

Exit codes: `0` ok, `1` provider failure, `2` usage/configuration error,
`3` blocked by the PII guard.

## Configuration

Everything can be set via environment variables or a `.env` file; see
[.env.example](.env.example) and [docs/CONFIGURATION.md](docs/CONFIGURATION.md).
Providers without credentials are skipped, so one `.env` works everywhere.

## Documentation

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, request flow, error model, design decisions |
| [docs/PROVIDERS.md](docs/PROVIDERS.md) | Parameter mapping, limits and pricing notes per provider |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Environment variables and constructor options |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model: prompt injection, data leakage, keys, self-hosting |
| [docs/EXTENDING.md](docs/EXTENDING.md) | Adding a provider (Exa, Firecrawl, Jina Reader ...) |
| [docs/DEPLOY_SEARXNG.md](docs/DEPLOY_SEARXNG.md) | Private SearXNG on a VPS with Docker Compose |
| [docs/LIVE_CHECK.md](docs/LIVE_CHECK.md) | Results of runs against real services, compared with the code's assumptions |

## Development

```bash
pytest                  # no network access needed: HTTP is mocked
ruff check . && ruff format --check .
mypy                    # strict
```

Project layout:

```
src/seekrelay/
├── relay.py          # SeekRelay, RetryPolicy, de-duplication
├── models.py         # SearchQuery, SearchResponse, ... + to_llm_context()
├── errors.py         # exception hierarchy (retryable / fallback flags)
├── pii.py            # PIIGuard
├── cache.py          # TTL + LRU cache
├── config.py         # env-based construction, .env reader
├── cli.py            # seekrelay command
└── providers/
    ├── base.py       # SearchProvider ABC, HTTP + error mapping
    ├── tavily.py
    ├── brave.py
    └── searxng.py
```

## Status and limitations

- All providers are covered by tests against mocked HTTP responses built from the
  providers' documentation (October 2026). SearXNG has also been checked live; Tavily and
  the Brave Search API have not yet, so run a smoke test with your keys before relying
  on them.
- Only Tavily implements `extract`. Brave and SearXNG return snippets.
- The cache is per-process and in-memory.
- `PIIGuard` is a seatbelt for obvious identifiers, not a DLP system.

### Live check (SearXNG, 2026-10-29)

About 50 real requests against the private instance from `deploy/searxng`. Every
assumption the SearXNG provider makes held: response fields and types, HTTP 400 for
invalid language codes (which the language fix avoids), `site:a OR site:b` filters
(28 of 28 results on the requested domains) and punycode filters for internationalised
domains (30 of 30). Measured on one machine:

| | |
|---|---|
| Search latency through SeekRelay | 0.45-2.3 s, median about 1.0 s |
| Cache hit | 0.05 ms |
| Upstream engines start blocking the instance | after about 7 searches in 10 s |
| Pace that kept working | one search every 15-20 s |
| Engines that answered | bing, duckduckgo, yahoo, brave (google and qwant: access denied) |

A private SearXNG suits scheduled jobs and occasional lookups, not bursts; keep a paid
provider behind it. Full results and how to reproduce them:
[docs/LIVE_CHECK.md](docs/LIVE_CHECK.md).

## License

[MIT](LICENSE)
