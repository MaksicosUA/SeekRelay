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
| mojeek, startpage | suspended (too many requests) at the time of the test |

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
