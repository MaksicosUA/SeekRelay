# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-10-30

### Added
- `SeekRelay(time_budget=...)` / `SEEKRELAY_TIME_BUDGET`: an upper bound for one
  `search` or `extract` call across all providers, retries and back-off. Without it a call
  could take about two minutes with the default settings when every provider is slow.
- `docs/LIVE_CHECK.md`: results of live runs against a self-hosted SearXNG and Tavily,
  compared with the assumptions in the code.
- `PIIGuard` detects Ukrainian tax numbers (`detect_tax_ids`) and secrets (`detect_secrets`).
  Both are on by default, and a guard built to switch off the older detectors keeps them on.
- `NoProviderConfiguredError`, a `ConfigurationError` for "no provider has credentials".

### Changed
- `./.env` is no longer read automatically: name the file with `--env-file .env` or
  `env_file=...`. If you kept queries on a self-hosted SearXNG through `./.env` and export
  another provider's key, queries now go to that provider until you name the file.
- `PIIGuard.scan()` and `check()` undo percent-encoding as `scan_url()` does.
- Library log records at `WARNING` name the provider and the error class. The provider's
  text moved to `DEBUG`; it is still in `attempts`.
- `to_llm_context()`: URLs are percent-encoded with internationalised hosts in punycode,
  attributes are limited to 1000 characters, and the provider answer sits on its own lines.
- `is_http_url()` applies `MAX_URL_LENGTH`, so longer URLs from providers are dropped.
- Tracebacks of unexpected provider errors moved from `ERROR` to `DEBUG`.
- Text format: page content from `extract` is indented by three spaces.
- Tavily: every search now sends `include_published_date`, because Tavily otherwise returns
  dates only for news and `published_at` stayed empty for web results.
- The docs no longer say that Tavily blocks queries with personal data: it accepted a query
  with an e-mail address in the live check.
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
- Tavily: `TAVILY_COUNTRY_NAMES` covered 47 of the 166 countries the API accepts, so
  `country="ru"`, `"br"`, `"cn"` and most others were silently skipped. The table is now
  complete.
- Brave: with several `include_domains` the query carried no `site:` operator, so 20 general
  results were filtered locally and often almost nothing survived. Domains are now sent as
  `site:a OR site:b`. If they exceed Brave's query limits, the operators are left out
  rather than failing the search. Not yet tried on the Search API itself.
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
- The README setup (`cp .env.example .env`) did not work as written: nothing read `.env`.
  `SeekRelay.from_env(env_file=...)` reads a file in code. `--env-file` does the same on the
  command line, and `read_env_file` now also handles a UTF-8 BOM and `KEY= # note`.
- Internationalised domains (`работа.рф`) in `include_domains` / `exclude_domains` raised
  `ValueError`. They are now converted to punycode, and domain filters and URL
  de-duplication treat the Unicode and punycode spellings of a host as the same host.
- `SEEKRELAY_MAX_ATTEMPTS` with hundreds of digits ended in a traceback instead of a
  configuration error.
- A title outside the stdout encoding lost the results and reported a usage error. Unknown
  characters are now escaped, and `--format json` stays valid.
- `--format json` could print `NaN` and `Infinity`, which are not JSON. Scores that are not
  finite are now `null`.
- The note about `./.env` appeared after errors that have nothing to do with the file.
- `PIIGuard` read date ranges, lists of numbers, IMEI numbers and 14-digit timestamps as payment
  cards: a date range `2024-01-01 2024-12-31` was blocked about one time in five. A card now
  needs a card layout and a prefix that fits its length. `logo@2x.png` and `git@github.com:`
  no longer count as addresses.

### Security
- A `.env` in the working directory could redirect queries or switch the PII guard off in a
  repository you had just cloned. See Changed above. In a named file, a line that `grep` and
  the shell see as a comment can no longer carry settings, names with invisible characters
  have no effect, a file over 1 MiB is refused, and a real variable under either Brave name
  beats the file.
- Hostile input could stall the event loop: a long URL in the PII guard (10 s for 80,000
  characters), a long digit run, `<` in Brave text, and combining marks on Python builds
  without the CVE-2026-3276 fix. URLs are limited to 8000 characters, the guard is linear and
  yields between URLs, and rendering no longer normalises text.
- Control characters from web pages and providers reached the terminal, including through
  argparse errors. Everything the CLI prints is filtered, one result can no longer pose as
  another result, a header or a log line, and `--format json` stays lossless through escapes.
- `to_llm_context()` passed invisible text and lookalike brackets to the model: Unicode tags,
  zero-width and bidi controls, fullwidth brackets. They are now removed or neutralised. The
  rendered URL is the one the domain filters checked, and hidden characters no longer get a
  URL past `exclude_domains`.
- Domain filters could be passed with a percent-encoded host, a backslash, a line break or a
  trailing dot, because URL parsers read such hosts differently. A URL with an ambiguous host
  now passes no filter, and Tavily results are filtered like those of the other providers.
- `PIIGuard` gaps closed: Unicode and bracketed e-mail addresses, cards with dots, slashes or
  underscores and the PROSTIR range, dashed and unusually grouped IBANs, base64, up to three
  rounds of percent-encoding, invisible characters, fullwidth digits, and domain filters.

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
