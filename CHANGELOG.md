# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `SeekRelay(time_budget=...)` / `SEEKRELAY_TIME_BUDGET`: an upper bound for one
  `search` or `extract` call across all providers, retries and back-off. Without it a call
  could take about two minutes with the default settings when every provider is slow.
- `docs/LIVE_CHECK.md`: results of a live run against a self-hosted SearXNG, compared with
  the assumptions in the code.

### Changed
- `docs/DEPLOY_SEARXNG.md` and `.env.example` recommend
  `SEARXNG_ENGINES=brave,bing,duckduckgo,yahoo`, which answered a burst of 12 searches in
  the live check, and explain how to check engines from your own server.
- A `RateLimitError` whose `retry_after` is longer than `RetryPolicy.max_delay` now also
  puts the provider into cooldown for `retry_after` seconds (it already fell back without
  waiting), so later calls do not hit a provider that asked to be left alone.
- Project renamed to SeekRelay: distribution, import package and CLI are now `seekrelay`,
  environment variables use the `SEEKRELAY_` prefix (was `SEARCH_GATEWAY_`), the library
  logger is `seekrelay` and the default User-Agent is `seekrelay/<version>`.
- `SearchGateway` is now `SeekRelay`, `SearchGatewayError` is `SeekRelayError`,
  `build_gateway_from_env` is `build_relay_from_env`, and the `gateway` module is
  `relay`.

### Fixed
- SearXNG: `engines` / `SEARXNG_ENGINES` did not restrict the engines, because the
  provider also sent `categories=general` and SearXNG adds every engine of a requested
  category to an explicit engine list. Web searches with pinned engines now send only
  `engines`; news searches keep `categories=news`.
- SearXNG: when a search returned nothing because the upstream engines were suspended or
  behind a CAPTCHA (SearXNG keeps them out for 3 to 60 minutes), the provider raised a
  retryable `ProviderUnavailableError`. Every search during the suspension lost 1.7-2.9 s
  before falling back (measured live). It now raises `RateLimitError(retry_after=180)`:
  no retry, and the relay skips the instance for 180 s.
- SearXNG: `language` values with a region or script subtag (`en-us`, `pt-br` with a
  country, `zh-hans`) were sent as codes SearXNG rejects with HTTP 400, which stopped the
  search without falling back. The provider now always sends `xx` or `xx-YY`.
- The README setup (`cp .env.example .env`) did not work as written: neither the CLI nor
  `from_env()` read `.env`. The CLI now reads `./.env` when it exists, and
  `SeekRelay.from_env(env_file=".env")` reads a file in code.
- Internationalised domains (`работа.рф`) in `include_domains` / `exclude_domains` raised
  `ValueError`. They are now converted to punycode, and domain filters and URL
  de-duplication treat the Unicode and punycode spellings of a host as the same host.

## [0.1.0] - 2026-10-28

### Added
- `SearchGateway`: one async API over an ordered list of providers with fallback,
  per-provider retries with exponential back-off, `Retry-After` handling, cooldown after
  auth/quota errors, TTL cache, URL de-duplication and a blocking `*_sync` API.
- Providers: `TavilyProvider` (search + extract), `BraveProvider` (web + news search),
  `SearXNGProvider` (self-hosted metasearch).
- Normalised model: `SearchQuery`, `SearchResponse`, `SearchResult`, `ExtractResponse`
  with `to_dict()` and prompt-safe `to_llm_context()`.
- `PIIGuard` that blocks queries and URLs with e-mail addresses, payment card numbers
  (Luhn) and IBANs (mod-97) before they leave the process.
- Exception hierarchy with `retryable` / `fallback` semantics.
- Environment-based configuration and `.env` reader without extra dependencies.
- `search-gateway` CLI (`search`, `extract`, `providers`; text / JSON / LLM output).
- Docker Compose setup for a private SearXNG instance.
- Test suite (no network), CI workflow, technical documentation.
