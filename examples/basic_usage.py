"""Minimal async usage.

Run:
    export TAVILY_API_KEY=tvly-...        # and/or BRAVE_API_KEY, SEARXNG_BASE_URL
    python examples/basic_usage.py "fastapi lifespan events"
"""

from __future__ import annotations

import asyncio
import sys

from search_gateway import AllProvidersFailedError, QueryRejectedError, SearchGateway


async def main(query: str) -> None:
    async with SearchGateway.from_env() as gateway:
        print("Providers:", " -> ".join(gateway.provider_names))
        try:
            response = await gateway.search(query, max_results=5, time_range="year")
        except QueryRejectedError as exc:
            print("Blocked locally:", exc)
            return
        except AllProvidersFailedError as exc:
            for attempt in exc.attempts:
                print(f"  {attempt.provider}: {attempt.error} - {attempt.message}")
            raise

        for attempt in response.attempts:
            print(f"fallback: {attempt.provider} failed with {attempt.error}")
        print(f"Served by {response.provider} in {response.latency_ms:.0f} ms\n")
        for result in response.results:
            print(f"- {result.title}\n  {result.url}")


if __name__ == "__main__":
    asyncio.run(main(" ".join(sys.argv[1:]) or "python asyncio best practices"))
