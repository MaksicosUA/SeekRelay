"""A summary for a period from two ``stats()`` snapshots. An example to copy.

The relay's counters only grow and keep no history, so a period is the difference between
a later snapshot and an earlier one. ``report_every`` prints one each period; send the text
to a chat or a log instead of printing it.

    python examples/hourly_summary.py
"""

from __future__ import annotations

import asyncio

from seekrelay import ProviderStats, RelayStats, SeekRelay


def summary(before: RelayStats, after: RelayStats) -> str:
    calls = after.searches + after.extracts - before.searches - before.extracts
    lines = [
        f"{calls} calls, {after.cache_hits - before.cache_hits} from the cache, "
        f"{after.all_failed - before.all_failed} without an answer"
    ]
    for name, now in after.providers.items():
        was = before.providers.get(name, ProviderStats())
        ok, failed = now.ok - was.ok, now.failed - was.failed
        backup = now.served_as_backup - was.served_as_backup
        lines.append(f"  {name}: {ok} answered ({backup} as backup), {failed} failed")
    return "\n".join(lines)


async def report_every(relay: SeekRelay, seconds: float) -> None:
    previous = relay.stats()
    while True:
        await asyncio.sleep(seconds)
        current = relay.stats()
        print(summary(previous, current))
        for status in relay.status().values():
            if status.state == "down":
                print(f"  {status.name} is down: {status.last_error}")
        previous = current


async def main() -> None:
    async with SeekRelay.from_env() as relay:
        reporter = asyncio.create_task(report_every(relay, seconds=5))  # 3600 for an hour
        for query in ("python asyncio timeout", "httpx streaming", "python asyncio timeout"):
            await relay.search(query, max_results=3)
        await asyncio.sleep(5.5)
        reporter.cancel()


if __name__ == "__main__":
    asyncio.run(main())
