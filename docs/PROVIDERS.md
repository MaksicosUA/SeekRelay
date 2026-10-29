# Providers

How each provider maps the common `SearchQuery`, what it returns and where it falls
short. Pricing notes reflect the providers' public pages in October 2026 and change
often; check the official pages before budgeting.

## Common model

| `SearchQuery` field | Meaning |
|---|---|
| `text` | 1-400 characters, whitespace normalised |
| `max_results` | 1-20 |
| `depth` | `basic` / `advanced` (only Tavily distinguishes them) |
| `topic` | `general` / `news` |
| `time_range` | `day` / `week` / `month` / `year` |
| `include_domains`, `exclude_domains` | Bare domains; subdomains match (`dou.ua` matches `jobs.dou.ua`) |
| `language` | ISO 639-1 (`en`, `uk`, `ru`, ...) |
| `country` | ISO 3166-1 alpha-2 (`ua`, `kz`, `de`, ...) |
| `include_raw_content` | Full page text in results (Tavily only) |

---

## Tavily

* Endpoints: `POST https://api.tavily.com/search`, `POST https://api.tavily.com/extract`
* Auth: `Authorization: Bearer tvly-...`
* Docs: <https://docs.tavily.com/documentation/api-reference>

### Parameter mapping

| SearchQuery | Tavily `/search` body |
|---|---|
| `text` | `query` |
| `depth` | `search_depth` (`basic` = 1 credit, `advanced` = 2 credits) |
| `max_results` | `max_results` |
| `topic` | `topic` (`general` / `news`) |
| `time_range` | `time_range` |
| `include_domains` / `exclude_domains` | same names (native, up to 300 / 150 domains) |
| `language` | `language` |
| `country` | `country` as a lower-case English name via `TAVILY_COUNTRY_NAMES` (`ua` -> `ukraine`); only sent with `topic=general`; unknown codes are skipped |
| `include_raw_content` | `include_raw_content: "markdown"` |
| provider option `include_answer=True` | `include_answer: "basic"` |

`/extract`: URLs are de-duplicated and sent in batches of 20 (the documented maximum),
concurrently; `extract_depth` is `basic` or `advanced` (`advanced=True`), format
`markdown`. Successful pages and `failed_results` are merged. If some batches fail, the
successful ones are kept and the URLs of failed batches are reported in `failed` (so a
retry never pays for the same pages twice); only when every batch fails is the error
raised for the gateway to retry or fall back.

### Status codes

| Code | Meaning | Exception |
|---|---|---|
| 400 | invalid request (also: query blocked because of PII) | `InvalidRequestError` |
| 401 | missing / invalid key | `AuthenticationError` |
| 422 | validation error (`detail` list) | `InvalidRequestError` |
| 429 | rate limit, `Retry-After` header | `RateLimitError` |
| 432 | key or plan limit exceeded | `QuotaExceededError` |
| 433 | pay-as-you-go limit exceeded | `QuotaExceededError` |
| 434 | live-fetch capacity temporarily exceeded | `ProviderUnavailableError` |
| 5xx | server error | `ProviderUnavailableError` |

### Cost notes (October 2026)

* Free tier: 1,000 credits per month, no card; requests stop when credits run out
  (reset on the 1st of the month).
* Pay-as-you-go: $0.008 per credit. Basic search 1 credit, advanced 2; extract
  1 credit per 5 successful URLs (advanced 2); the Research endpoint (not used here)
  costs up to 250 credits per call.
* Owned by Nebius since 2026; the brand and API continue.

### Strengths / limits

+ Search and extraction in one API, LLM-ready snippets, native domain filters.
- Most expensive per search of the three at volume. Extraction can struggle with
  JavaScript-heavy pages; content behind logins is not reachable.

---

## Brave Search

* Endpoints: `GET https://api.search.brave.com/res/v1/web/search`,
  `GET https://api.search.brave.com/res/v1/news/search`
* Auth: `X-Subscription-Token: <key>`
* Docs: <https://api-dashboard.search.brave.com/documentation>

### Parameter mapping

| SearchQuery | Brave query parameter |
|---|---|
| `text` | `q` (max 400 characters / 50 words including operators; longer -> `UnsupportedOperationError`, so the gateway falls back) |
| `max_results` | `count` (20 when a domain filter is active, see below) |
| `topic=news` | `news/search` endpoint instead of `web/search` |
| `time_range` | `freshness`: `pd` / `pw` / `pm` / `py` |
| `country` | `country` (upper-case), only for codes in `BRAVE_COUNTRIES`; others (e.g. `ua`, `kz`) are skipped with a DEBUG log because Brave answers them with 422 |
| `language` | `search_lang`, only for Brave's own codes in `BRAVE_LANGUAGES`; aliases `ja`->`jp`, `zh`->`zh-hans`, `pt`->`pt-pt`, `no`->`nb`; others skipped |
| `include_domains` (exactly one) | `site:domain` appended to `q` |
| `exclude_domains` | `-site:domain` appended to `q` |
| provider option `safesearch` | `safesearch` (`off` / `moderate` / `strict`) |
| provider option `extra_snippets` | `extra_snippets=true`, joined into the snippet with ` … ` |
| (always, web) | `text_decorations=false`; remaining `<strong>` tags and entities are stripped anyway |

**Domain filtering.** Brave has no domain filter parameter. With several include domains
no operator is added; in every case results are filtered strictly on the client side and
the provider requests 20 results so that enough survive. A filtered search can return
fewer than `max_results`.

### Status codes

| Code | Exception |
|---|---|
| 401, 403 | `AuthenticationError` |
| 402 | `QuotaExceededError` |
| 422, 400 | `InvalidRequestError` |
| 429 with `error.code = QUOTA_LIMITED`, or the last window of `X-RateLimit-Remaining` at `0` | `QuotaExceededError` (plan exhausted -> cooldown) |
| other 429 | `RateLimitError` (`retry_after` from `Retry-After`, else the first value of `X-RateLimit-Reset`) |
| 5xx | `ProviderUnavailableError` |

### Cost notes (October 2026)

* $5 per 1,000 requests with $5 of free credit per month (the old perpetual free tier
  was retired in February 2026).
* Independent index (not derived from Google/Bing), SOC 2 Type II.

### Strengths / limits

+ Fast, own index, strong results in independent agentic-search benchmarks.
- No page extraction; snippets only. `extra_snippets` availability depends on plan.

---

## SearXNG (self-hosted)

* Endpoint: `GET {base_url}/search?format=json`
* Auth: none by default; optional custom headers (`SEARXNG_AUTH_HEADER`) for a reverse
  proxy
* Docs: <https://docs.searxng.org/dev/search_api.html>
* Deployment: [DEPLOY_SEARXNG.md](DEPLOY_SEARXNG.md)

### Parameter mapping

| SearchQuery | SearXNG parameter |
|---|---|
| `text` | `q` |
| `include_domains` | `site:a OR site:b` appended to `q` (honoured by most engines) + client-side filter |
| `exclude_domains` | client-side filter only |
| `topic` | `categories=general` / `categories=news` |
| `time_range` | `time_range` (only engines that support it apply it) |
| `language` (+ `country`) | `language=uk` or `language=uk-UA`. SearXNG accepts only `xx` / `xx-YY` (anything else is HTTP 400), so a two-letter region in `language` wins over `country` (`en-gb` -> `en-GB`) and other subtags are dropped (`zh-hans` -> `zh`) |
| `max_results` | client-side slice of page 1 |
| provider option `engines` | `engines=duckduckgo,brave,...` |
| provider option `safesearch` | `safesearch=0/1/2` |

Results keep `source_engines` (which upstream engines returned the URL) and SearXNG's
`score`.

### Behaviour worth knowing

* **JSON must be enabled.** Without `json` in `search.formats` the instance answers
  403; the provider raises `AuthenticationError` with a hint.
* **Upstream blocking.** Google and others rate-limit or CAPTCHA metasearch servers. If
  a search returns nothing *and* `unresponsive_engines` is non-empty, the provider raises
  `ProviderUnavailableError` so the gateway falls back instead of returning a misleading
  empty list. Choosing engines that tolerate automated use (`SEARXNG_ENGINES`) helps.
* **Terms of use.** SearXNG queries third-party engines; heavy automated commercial use
  can conflict with their terms. Keep it for internal tools.

### Cost

Only the server. A small VPS is enough for personal agents and scheduled jobs.

---

## Choosing an order

| Workload | Suggested `SEEKRELAY_PROVIDERS` |
|---|---|
| Interactive agent, quality first | `tavily,brave,searxng` |
| Scheduled jobs, cost first | `searxng,brave,tavily` |
| No paid keys | `searxng` |
| Need page content | Tavily anywhere in the list (`extract` uses only capable providers) |
