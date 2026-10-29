"""Collect fresh remote vacancies from job boards into a JSON file.

A realistic scheduled job: several queries, restricted to job-board domains, last week
only, de-duplicated across queries. Cheap providers first (SearXNG on your own server),
paid ones as fallback:

    SEEKRELAY_PROVIDERS=searxng,brave,tavily python examples/vacancy_search.py

Job boards often render listings with JavaScript or hide them behind a login, so
snippets can be short; open the URLs (or call relay.extract) for full descriptions.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from seekrelay import AllProvidersFailedError, SeekRelay

JOB_BOARDS = ("djinni.co", "jobs.dou.ua", "work.ua", "robota.ua", "hh.kz")
QUERIES = (
    "remote Python developer vacancy",
    "remote AI engineer LLM vacancy",
    "AI automation engineer remote",
)


async def collect(output: Path) -> None:
    found: dict[str, dict[str, str]] = {}
    async with SeekRelay.from_env(fallback_on_empty=True) as relay:
        for text in QUERIES:
            try:
                response = await relay.search(
                    text, max_results=10, time_range="week", include_domains=JOB_BOARDS
                )
            except AllProvidersFailedError as exc:
                print(f"skipped {text!r}: {exc}")
                continue
            for result in response.results:
                found.setdefault(
                    result.url,
                    {"title": result.title, "snippet": result.snippet, "via": response.provider},
                )
            print(f"{text!r}: {len(response.results)} results via {response.provider}")

    payload = {
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "vacancies": [{"url": url, **info} for url, info in found.items()],
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(found)} unique vacancies -> {output}")


if __name__ == "__main__":
    asyncio.run(collect(Path("vacancies.json")))
