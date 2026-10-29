# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- Project renamed to SeekRelay: distribution, import package and CLI are now `seekrelay`,
  environment variables use the `SEEKRELAY_` prefix (was `SEARCH_GATEWAY_`), the library
  logger is `seekrelay` and the default User-Agent is `seekrelay/<version>`.

### Fixed
- SearXNG: `language` values with a region or script subtag (`en-us`, `pt-br` with a
  country, `zh-hans`) were sent as codes SearXNG rejects with HTTP 400, which stopped the
  search without falling back. The provider now always sends `xx` or `xx-YY`.
- The README setup (`cp .env.example .env`) did not work as written: neither the CLI nor
  `from_env()` read `.env`. The CLI now reads `./.env` when it exists, and
  `SearchGateway.from_env(env_file=".env")` reads a file in code.

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
