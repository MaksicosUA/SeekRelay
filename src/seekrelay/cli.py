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
import json
import logging
import os
import sys
import textwrap
from collections.abc import Sequence
from typing import Any

from ._version import __version__
from .config import build_relay_from_env, parse_csv, read_env_file
from .errors import ConfigurationError, ProviderError, QueryRejectedError, SeekRelayError
from .models import ExtractResponse, SearchDepth, SearchResponse, TimeRange, Topic
from .relay import SeekRelay

EXIT_OK, EXIT_PROVIDER, EXIT_USAGE, EXIT_REJECTED = 0, 1, 2, 3
DEFAULT_ENV_FILE = ".env"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seekrelay",
        description="Web search for AI agents through Tavily, Brave and SearXNG with fallback.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--env-file",
        help=f"read settings from a .env file (default: ./{DEFAULT_ENV_FILE} if it exists; "
        "env vars win)",
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


def _print_search(response: SearchResponse, fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(response.to_dict(), ensure_ascii=False, indent=2))
        return
    if fmt == "llm":
        print(response.to_llm_context())
        return
    source = "cache" if response.from_cache else f"{response.latency_ms:.0f} ms"
    print(f"[{response.provider}] {len(response.results)} results · {source}")
    for attempt in response.attempts:
        print(f"  fallback: {attempt.provider} -> {attempt.error}: {attempt.message}")
    if response.answer:
        print("\nAnswer: " + textwrap.shorten(response.answer, 500))
    for index, result in enumerate(response.results, start=1):
        print(f"\n{index}. {result.title or '(no title)'}")
        print(f"   {result.url}")
        if result.published_at:
            print(f"   published: {result.published_at}")
        if result.snippet:
            snippet = textwrap.shorten(result.snippet, 300)
            print(textwrap.indent(textwrap.fill(snippet, 96), "   "))


def _print_extract(response: ExtractResponse, fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(response.to_dict(), ensure_ascii=False, indent=2))
        return
    if fmt == "llm":
        print(response.to_llm_context())
        return
    print(f"[{response.provider}] {len(response.pages)} pages · {response.latency_ms:.0f} ms")
    for page in response.pages:
        print(f"\n=== {page.url} ({len(page.content)} chars)\n")
        print(page.content[:2000] + ("\n…" if len(page.content) > 2000 else ""))
    for failure in response.failed:
        print(f"\nFAILED {failure.url}: {failure.error}")


async def _run(args: argparse.Namespace, env: dict[str, str]) -> int:
    order = parse_csv(args.providers) or None
    relay: SeekRelay = build_relay_from_env(env, order=order)
    async with relay:
        if args.command == "providers":
            for index, name in enumerate(relay.provider_names, start=1):
                print(f"{index}. {name}")
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
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        env = dict(os.environ)
        env_file = args.env_file
        if env_file is None and os.path.isfile(DEFAULT_ENV_FILE):
            env_file = DEFAULT_ENV_FILE
        if env_file:
            env = {**read_env_file(env_file), **env}
        return asyncio.run(_run(args, env))
    except QueryRejectedError as exc:
        print(f"rejected: {exc}", file=sys.stderr)
        return EXIT_REJECTED
    except (ConfigurationError, ValueError, TypeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ProviderError as exc:
        print(f"provider error: {exc}", file=sys.stderr)
        return EXIT_PROVIDER
    except SeekRelayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_PROVIDER
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
