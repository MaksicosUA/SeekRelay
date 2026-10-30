# Live check

The unit tests run against mocked HTTP responses built from the providers'
documentation. This page records what a run against a real service showed, so the
assumptions in the code can be compared with reality.

## Run of 2026-10-29: self-hosted SearXNG

| | |
|---|---|
| Instance | `deploy/searxng` from this repository, image `searxng/searxng:latest` (2026.10.29-cff884f98), default engines, bound to `127.0.0.1:8888` |
| Client | SeekRelay at commit `e3a681f`, SearXNG as the only provider |
| Machine | One Linux workstation. Engine availability depends on the IP address, so other machines can see different results |
| Volume | About 50 requests over 20 minutes |
| Not covered | Tavily and the Brave Search API (no keys were available) |

### Assumptions compared with real responses

| Assumption in the code | Observed | Result |
|---|---|---|
| Result fields `url`, `title`, `content`, `engine`, `engines` (list), `score` (float), `publishedDate` (ISO string or null) | All present with these types | as assumed |
| `unresponsive_engines` is a list of `[engine, reason]` pairs | `[["duckduckgo", "CAPTCHA"]]`, `[["brave", "Suspended: too many requests"], ...]` | as assumed |
| SearXNG accepts only `xx` / `xx-YY` and answers 400 otherwise | `en-us-US` 400, `en-US` 200, `zh-hans` 400, `zh` 200, `uk-UA` 200 | as assumed; confirms the language fix |
| A format that is not enabled answers 403 | `format=rss` 403 | as assumed |
| `time_range` accepts `day` / `week` / `month` / `year` | all 200; `decade` 400 | as assumed |
| Page 1 holds enough results for `max_results` up to 20 | 23-34 results per page; `max_results=20` returned 20 | as assumed |
| `site:a OR site:b` narrows results to the given domains | 28 of 28 raw results on `djinni.co` / `jobs.dou.ua`; through SeekRelay 10 of 10 | as assumed |
| Internationalised domains work as punycode `site:` operators | `site:xn--d1abbgf6aiiy.xn--p1ai`: 30 of 30 results on the domain; `site:президент.рф`: 2 of 12. Result URLs come back in Unicode and match the punycode filter; through SeekRelay 5 of 5 | as assumed; confirms the punycode choice |
| `time_budget` stops a slow call | budget 0.3 s, call ended after 302 ms with `ProviderUnavailableError` "time budget ran out" | as assumed |
| PII guard blocks before any request | query with an e-mail address: CLI exit code 3, nothing sent | as assumed |
| Identical queries are served from the cache | cache hit in 0.05 ms | as assumed |

### Numbers

| Measurement | Value |
|---|---|
| Successful search through SeekRelay | 0.45-2.3 s, median about 1.0 s |
| Failed search (engines unresponsive, 2 attempts) | 1.7-2.9 s |
| Burst that triggered upstream blocking | about 7 searches in 10 s |
| Pace that kept working | one search every 15-20 s |
| SearXNG suspension after "too many requests" / "access denied" | 180 s (instance default) |
| SearXNG suspension after a CAPTCHA | 3600 s (instance default) |

Compared with the defaults: the 20 s per-request timeout (`SEEKRELAY_TIMEOUT`) is far
above real SearXNG latency, because SearXNG itself gives up on upstream engines after
8 s. For agents, `SEEKRELAY_TIME_BUDGET=10` to `15` leaves room for one fallback.

### Upstream engines

Queried one engine at a time (`engines=<name>`, query "python asyncio taskgroup"):

| Engine | Answer |
|---|---|
| bing | 10 results, 0.25 s |
| duckduckgo | 10 results, 0.66 s (earlier in the run: CAPTCHA) |
| yahoo | 7 results, 1.0 s |
| brave | 25 results, 1.5 s |
| wikipedia | 0 results (no article for this query; the engine answered) |
| google | access denied |
| qwant | access denied |

`mojeek` and `startpage` are not loaded in this image, so they could not be tested.
SearXNG ignores unknown names and uses its default engines instead; check a name with
`/config` first, and look at the engines that really answered.

With the default engine set, almost all results came from the `brave` engine:
`google cse` was suspended for most of the run and `duckduckgo` often asked for a CAPTCHA.

### Findings

- **Retries while upstream engines are suspended.** When every unresponsive engine is
  suspended or behind a CAPTCHA, the instance stays useless for 3 to 60 minutes. SeekRelay
  treated this as a transient `ProviderUnavailableError`: it retried SearXNG and tried it
  again on the next call, so every search in that period lost 1.7-2.9 s before falling
  back. *Fixed after this run:* the provider now raises `RateLimitError` with
  `retry_after=180` in this case, and the relay skips the instance for that long without
  retrying.
- **Request rate.** A private SearXNG suits scheduled jobs and occasional agent lookups,
  not bursts. Keep a paid provider behind it in `SEEKRELAY_PROVIDERS`.
- **Engine choice depends on the IP address.** Check the engines from the machine that
  runs SearXNG before pinning `SEARXNG_ENGINES`.
- **`SEARXNG_ENGINES` did not pin anything.** The provider sent `categories=general`
  together with `engines=...`, and SearXNG adds every engine of a requested category to
  an explicit list: `engines=brave,google cse` alone returned nothing from suspended
  engines, with `categories=general` it returned 10 results from `duckduckgo`. *Fixed
  after this run:* with pinned engines, web searches send only `engines`.

### Re-check after the fixes

Same instance, same day, with the two fixes above:

| Check | Before | After |
|---|---|---|
| `engines=["bing"]` | results from the default engines as well | 10 results, all from `bing` |
| Search while the pinned engine is suspended, backup provider behind SearXNG | 1.7-2.9 s per call (retry, then fallback) | first call 4 ms (one request, no retry), then 0 ms: SearXNG skipped for 180 s |

### Recommended engines (2026-10-30)

Same instance, `SEARXNG_ENGINES=brave,bing,duckduckgo,yahoo`, one search per engine
first, then the set:

| Check | Result |
|---|---|
| Each engine alone, sources verified | brave 10 results, 1.1 s; bing 10, 0.4 s; yahoo 7, 0.9 s; duckduckgo CAPTCHA |
| The set, 15 s between searches | 3 of 3 answered, 0.8-1.1 s, all four engines contributed |
| The set, burst of 12 without a pause | 12 of 12 answered, median 0.8 s, max 0.9 s |
| Engines that answered during the burst | bing 12, yahoo 12, brave 8, duckduckgo 2 |
| `SEARXNG_ENGINES` through the CLI with 2 domains, right after the burst | 5 results, all from `yahoo` and inside the filter |

`brave` and `duckduckgo` still drop out under load, but `bing` and `yahoo` kept the set
answering. For comparison, the default engines behaved differently between runs: one burst
stopped answering after about 7 searches, another kept answering for 15 searches because
`duckduckgo` showed no CAPTCHA that time (`brave` dropped out at search 12). One machine and
few runs: treat the set as a starting point, not a guarantee.

### Reproducing

```bash
cd deploy/searxng
cp .env.example .env && sed -i "s/change-me/$(openssl rand -hex 32)/" .env
docker compose up -d
export SEARXNG_BASE_URL=http://127.0.0.1:8888
seekrelay --providers searxng search "python asyncio taskgroup" -n 5
seekrelay --providers searxng search "python developer remote" \
    --include-domain djinni.co --include-domain jobs.dou.ua -n 10
```

Leave 15-20 s between searches, or the upstream engines start blocking the instance.

## Run of 2026-10-30: Tavily

| | |
|---|---|
| Service | Tavily API, free "Researcher" plan (1,000 credits a month) |
| Client | SeekRelay 0.2.0, the Tavily provider alone unless noted |
| Machine | The same Linux workstation |
| Volume | About 30 requests. Each search response reported `usage.credits: 1`. The `/usage` endpoint still showed 0 after the run, so the total could not be confirmed |
| Not triggered | HTTP 429, 432, 433, 434 |

### Assumptions compared with real responses

| Assumption in the code | Observed | Result |
|---|---|---|
| Response fields `results`, `answer`, `request_id`; results with `url`, `title`, `content`, `score` (float), `raw_content` | All present with these types; extra `follow_up_questions`, `images`, `usage`, `id` are ignored | as assumed |
| `language` is passed through | `uk`, `en-us`, `zh-hans`, `pt-br` all accepted | as assumed; no SearXNG-style 400 |
| `country` uses Tavily's lower-case names | All our names are valid | as assumed, but the table had only 47 of 166 countries. *Fixed after this run:* the table is complete, `ru` and `br` are accepted |
| `country` is sent only with `topic=general` | News search with `country="de"` worked with the parameter skipped | as assumed |
| Domain filters are native | 1 domain: 10 of 10; 2 domains: 10 of 10; `exclude_domains`: nothing leaked | as assumed |
| Internationalised domains work as punycode | Punycode: 5 of 5 on the domain, URLs returned in punycode. Unicode spelling: 0 results | as assumed; confirms the punycode choice |
| `published_date` is available | Only for news: 0 dated results on web search | wrong for web search. *Fixed after this run:* the provider sends `include_published_date`, and dates then appeared on 5 of 7 web results |
| `max_results` results come back | 2-5 for `max_results=5`, 11 for 20, 3 for 10 with an exclusion; the number varied between identical requests | it is an upper bound; documented now |
| `include_answer` | 534 characters, included as untrusted text by `to_llm_context()` | as assumed |
| `include_raw_content` | Filled for 2 of 2 results | as assumed |
| `/extract` merges pages and `failed_results` | 4 URLs: 3 pages (Markdown), 1 unknown host in `failed` | as assumed |
| 401 maps to `AuthenticationError` with cooldown and fallback | Bad key: served by the backup provider, cooldown 300 s | as assumed |
| Fallback in the CLI, `./.env` read automatically | SearXNG down: `ConnectError`, then served by Tavily | as assumed |
| Tavily blocks queries with personal data | A query with a made-up e-mail address was accepted (3 results) | **not confirmed on this plan.** `PIIGuard` is what protects such queries; the docs are corrected |

### Numbers

| Measurement | Value |
|---|---|
| Search through SeekRelay | 1.2-2.9 s, median 1.9 s (12 searches) |
| Search with an internationalised domain filter | 5.0 s (one search) |
| `depth="advanced"` | 3.9 s (one search) |
| Extract 4 URLs | 0.9 s |
| Extract 1 URL with `advanced=True`, already fetched | 0.16 s |

The client-side figures include the network and TLS; Tavily's own `response_time` for one
raw search was 0.99 s. `SEEKRELAY_TIME_BUDGET=10` to `15` also fits Tavily, with room for
one fallback.
