"""Keep a history of events in a JSON Lines file. An example to copy.

The relay keeps no history of its own. This handler writes one line per event through
``logging``'s RotatingFileHandler, so the files stay within a fixed size on disk too.
Events hold no query text, URLs or keys, so the file holds none either.

    python examples/events_to_jsonl.py && tail seekrelay-events.jsonl
"""

from __future__ import annotations

import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler

from seekrelay import EventHandler, RelayEvent, SeekRelay


def jsonl_history(path: str, *, max_bytes: int = 10_000_000, backups: int = 3) -> EventHandler:
    log = logging.getLogger("seekrelay_events")
    log.propagate = False  # not into the application's own log
    log.setLevel(logging.INFO)
    handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)

    def on_event(event: RelayEvent) -> None:
        log.info(json.dumps(event.to_dict(), ensure_ascii=False))

    return on_event


async def main() -> None:
    async with SeekRelay.from_env(on_event=jsonl_history("seekrelay-events.jsonl")) as relay:
        await relay.search("python asyncio timeout", max_results=3)


if __name__ == "__main__":
    asyncio.run(main())
