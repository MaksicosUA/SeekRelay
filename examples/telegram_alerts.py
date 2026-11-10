"""Send provider alerts to a Telegram chat. An example to copy, not part of the package.

Only events with severity "alert" go out: a provider went down or came back, so one outage
makes two messages. The handler only puts a line in a bounded queue and returns; a
background task sends it. A slow or unreachable Telegram therefore never slows a search
down, and memory stays bounded however many events arrive.

Run with TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID and a search provider set (see .env.example):

    python examples/telegram_alerts.py
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time

import httpx

from seekrelay import RelayEvent, SeekRelay

QUEUE_SIZE = 100
QUIET_SECONDS = 30 * 60  # at most one message per provider and kind in this time


class TelegramAlerts:
    def __init__(self, token: str, chat_id: str) -> None:
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._last_sent: dict[tuple[str, str], float] = {}  # providers x 2 kinds at most
        self.dropped = 0

    def __call__(self, event: RelayEvent) -> None:
        """The ``on_event`` handler: decides and queues, never waits."""
        if event.severity != "alert":
            return
        key = (event.provider or "?", event.kind)
        now = time.monotonic()
        if now - self._last_sent.get(key, -QUIET_SECONDS) < QUIET_SECONDS:
            return
        self._last_sent[key] = now
        try:
            self._queue.put_nowait(_message(event))
        except asyncio.QueueFull:
            self.dropped += 1

    async def run(self) -> None:
        """Send queued messages until cancelled."""
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                text = await self._queue.get()
                with contextlib.suppress(httpx.HTTPError):  # this alert is lost; searches go on
                    await client.post(self._url, json={"chat_id": self._chat_id, "text": text})


def _message(event: RelayEvent) -> str:
    if event.kind == "provider_down":
        reason = f": {event.error}" if event.error else ""
        return f"\U0001f534 {event.provider} is down{reason}"
    minutes = (event.seconds or 0) / 60
    return f"\U0001f7e2 {event.provider} is back after {minutes:.0f} min"


async def main() -> None:
    # httpx writes request URLs at INFO, and this URL holds the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    alerts = TelegramAlerts(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])
    sender = asyncio.create_task(alerts.run())
    async with SeekRelay.from_env(on_event=alerts) as relay:
        response = await relay.search("python asyncio timeout", max_results=3)
        print(response.provider, len(response.results), "results")
    await asyncio.sleep(1)  # let a queued alert go out
    sender.cancel()


if __name__ == "__main__":
    asyncio.run(main())
