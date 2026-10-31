"""Command-line interface.

Examples::

    seekrelay search "fastapi background tasks" -n 3
    seekrelay search "python vacancy remote" --topic news --time-range week \\
        --include-domain djinni.co --format json
    seekrelay extract https://docs.tavily.com --format llm
    seekrelay providers

Exit codes: 0 success, 1 provider failure, 2 usage/configuration error,
3 request rejected by the PII guard.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import sys
import textwrap
from collections.abc import Sequence
from typing import Any, TextIO

from ._text import UNSAFE_CHARS_RE, one_line, strip_invisible
from ._version import __version__
from .config import build_relay_from_env, parse_csv
from .errors import (
    ConfigurationError,
    NoProviderConfiguredError,
    ProviderError,
    QueryRejectedError,
    SeekRelayError,
)
from .models import ExtractResponse, SearchDepth, SearchResponse, TimeRange, Topic
from .relay import SeekRelay

EXIT_OK, EXIT_PROVIDER, EXIT_USAGE, EXIT_REJECTED = 0, 1, 2, 3
_LOCAL_ENV_FILE = ".env"
_ENV_FILE_HINT = (
    f"note: ./{_LOCAL_ENV_FILE} is not read automatically; "
    f"if you trust it, put --env-file {_LOCAL_ENV_FILE} before the command"
)
_MAX_LINE = 1000


class _SafeArgumentParser(argparse.ArgumentParser):
    """Clean what argparse prints: its errors echo argv, and argv can hold text from the web."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.color = False  # Python 3.14 colours help with the sequences that the filter removes

    def _print_message(self, message: str | None, file: Any = None) -> None:
        super()._print_message(strip_invisible(message or ""), file)


def _build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="seekrelay",
        description="Web search for AI agents through Tavily, Brave and SearXNG with fallback.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--env-file",
        help="a .env file to read settings from; none is read by default (env vars win)",
    )
    parser.add_argument("--providers", help="comma-separated provider order, e.g. searxng,tavily")
    parser.add_argument("-v", "--verbose", action="store_true", help="log to stderr")
    sub = parser.add_subparsers(dest="command", required=True)

    search = sub.add_parser("search", help="search the web")
    search.add_argument("query", help="query text")
    search.add_argument("-n", "--max-results", type=int, default=5)
    search.add_argument("--depth", choices=[d.value for d in SearchDepth], default="basic")
    search.add_argument("--topic", choices=[t.value for t in Topic], default="general")
    search.add_argument("--time-range", choices=[t.value for t in TimeRange])
    search.add_argument("--include-domain", action="append", default=[], metavar="DOMAIN")
    search.add_argument("--exclude-domain", action="append", default=[], metavar="DOMAIN")
    search.add_argument("--lang", help="ISO 639-1 language, e.g. en, uk, ru")
    search.add_argument("--country", help="ISO 3166-1 alpha-2 country, e.g. ua, kz")
    search.add_argument("--raw", action="store_true", help="include full page text (Tavily)")
    search.add_argument("--format", choices=["text", "json", "llm"], default="text")

    extract = sub.add_parser("extract", help="extract clean content from URLs")
    extract.add_argument("urls", nargs="+")
    extract.add_argument("--advanced", action="store_true")
    extract.add_argument("--format", choices=["text", "json", "llm"], default="text")

    sub.add_parser("providers", help="show configured providers in priority order")
    return parser


def _emit(text: str, *, file: TextIO | None = None) -> None:
    """Print through here only: a terminal obeys control sequences in web and provider text."""
    print(strip_invisible(text), file=file)


def _shorten(text: str, width: int) -> str:
    """Shorten the text that will be shown, so invisible padding cannot use up the width."""
    return textwrap.shorten(strip_invisible(text), width)


def _line(text: str) -> str:
    """One line of bounded length, so that web text cannot pass for a line of our own."""
    line = one_line(text)
    return line if len(line) <= _MAX_LINE else line[: _MAX_LINE - 1] + "\U00002026"


def _dump_json(data: dict[str, Any]) -> str:
    """Readable JSON that stays lossless: unsafe characters become escape sequences."""
    dumped = json.dumps(data, ensure_ascii=False, indent=2)
    dumped = UNSAFE_CHARS_RE.sub(lambda match: json.dumps(match.group())[1:-1], dumped)
    try:
        dumped.encode(getattr(sys.stdout, "encoding", None) or "utf-8")
    except UnicodeEncodeError:
        # backslashreplace would print \xe9 or \U0001f600, which are not JSON escapes.
        return json.dumps(data, indent=2)
    return dumped


class _SafeFormatter(logging.Formatter):
    """Clean the final record: messages and tracebacks can hold a provider's text.

    LF stays, because tracebacks are multi-line.
    """

    def format(self, record: logging.LogRecord) -> str:
        return strip_invisible(super().format(record))


def _print_search(response: SearchResponse, fmt: str) -> None:
    if fmt == "json":
        _emit(_dump_json(response.to_dict()))
        return
    if fmt == "llm":
        _emit(response.to_llm_context())
        return
    source = "cache" if response.from_cache else f"{response.latency_ms:.0f} ms"
    _emit(f"[{response.provider}] {len(response.results)} results · {source}")
    for attempt in response.attempts:
        _emit(f"  fallback: {attempt.provider} -> {attempt.error}: {_line(attempt.message)}")
    if response.answer:
        _emit("\nAnswer: " + _shorten(response.answer, 500))
    for index, result in enumerate(response.results, start=1):
        _emit(f"\n{index}. {_line(result.title) or '(no title)'}")
        _emit(f"   {_line(result.url)}")
        published = _line(result.published_at or "")
        if published:
            _emit(f"   published: {published}")
        if result.snippet:
            snippet = _shorten(result.snippet, 300)
            _emit(textwrap.indent(textwrap.fill(snippet, 96), "   "))


def _print_extract(response: ExtractResponse, fmt: str) -> None:
    if fmt == "json":
        _emit(_dump_json(response.to_dict()))
        return
    if fmt == "llm":
        _emit(response.to_llm_context())
        return
    _emit(f"[{response.provider}] {len(response.pages)} pages · {response.latency_ms:.0f} ms")
    for page in response.pages:
        content = strip_invisible(page.content)  # before the cut, as in _shorten
        _emit(f"\n=== {_line(page.url)} ({len(content)} chars)\n")
        # Indented, so that no line of a page can pass for a header or a FAILED line.
        _emit(textwrap.indent(content[:2000], "   ") + ("\n…" if len(content) > 2000 else ""))
    for failure in response.failed:
        _emit(f"\nFAILED {_line(failure.url)}: {_line(failure.error)}")


async def _run(args: argparse.Namespace, relay: SeekRelay) -> int:
    async with relay:
        if args.command == "providers":
            for index, name in enumerate(relay.provider_names, start=1):
                _emit(f"{index}. {name}")
            return EXIT_OK
        if args.command == "search":
            options: dict[str, Any] = {
                "max_results": args.max_results,
                "depth": args.depth,
                "topic": args.topic,
                "time_range": args.time_range,
                "include_domains": tuple(args.include_domain),
                "exclude_domains": tuple(args.exclude_domain),
                "language": args.lang,
                "country": args.country,
                "include_raw_content": args.raw,
            }
            _print_search(await relay.search(args.query, **options), args.format)
            return EXIT_OK
        _print_extract(await relay.extract(args.urls, advanced=args.advanced), args.format)
        return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    # A title outside the output encoding must not lose the results and report a usage error.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_SafeFormatter("%(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING, handlers=[handler]
    )
    # Their INFO lines hold request URLs: search queries, and a password in a SearXNG URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        # Only a file named on the command line: ./.env may be a stranger's, for example in a
        # cloned repository, and it could redirect queries or switch the PII guard off. Read
        # outside the event loop, so that Ctrl-C works while a pipe has no writer.
        relay = build_relay_from_env(
            os.environ,
            env_file=args.env_file or None,
            order=parse_csv(args.providers) or None,
        )
        return asyncio.run(_run(args, relay))
    except QueryRejectedError as exc:
        _emit(_line(f"rejected: {exc}"), file=sys.stderr)
        return EXIT_REJECTED
    except NoProviderConfiguredError as exc:
        _emit(_line(f"error: {exc}"), file=sys.stderr)
        if args.env_file is None and os.path.isfile(_LOCAL_ENV_FILE):
            _emit(_ENV_FILE_HINT, file=sys.stderr)
        return EXIT_USAGE
    except (ConfigurationError, ValueError, TypeError, OSError) as exc:
        _emit(_line(f"error: {exc}"), file=sys.stderr)
        return EXIT_USAGE
    except ProviderError as exc:
        # With --format llm a model reads this, and a provider's words have no untrusted wrapper.
        detail = type(exc).__name__ if getattr(args, "format", None) == "llm" else exc
        _emit(_line(f"provider error: {detail}"), file=sys.stderr)
        return EXIT_PROVIDER
    except SeekRelayError as exc:
        _emit(_line(f"error: {exc}"), file=sys.stderr)
        return EXIT_PROVIDER
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
