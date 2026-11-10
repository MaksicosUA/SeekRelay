"""Expose the relay as a tool for an LLM agent (framework-neutral).

`TOOL_SPEC` is a JSON-schema tool definition in the shape most tool-calling APIs
accept (name / description / input_schema). `handle_tool_call` turns the model's
arguments into a relay call and returns a prompt-safe text block.

Errors are returned as short text instead of raised, so the agent can react
("search unavailable, answer from memory and say so") instead of crashing.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from seekrelay import (
    AllProvidersFailedError,
    InvalidRequestError,
    LimitExceededError,
    QueryRejectedError,
    SeekRelay,
)

TOOL_SPEC: dict[str, Any] = {
    "name": "web_search",
    "description": (
        "Search the public web for current information. Returns titles, URLs and "
        "snippets. Use for facts that may have changed recently. Never put personal "
        "data (emails, card numbers) into the query."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query, 1-400 characters"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            "topic": {"type": "string", "enum": ["general", "news"], "default": "general"},
            "time_range": {"type": "string", "enum": ["day", "week", "month", "year"]},
            "include_domains": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["query"],
    },
}

_ALLOWED_ARGS = {"max_results", "topic", "time_range", "include_domains"}


async def handle_tool_call(relay: SeekRelay, arguments: dict[str, Any]) -> str:
    query = str(arguments.get("query", ""))
    options = {k: v for k, v in arguments.items() if k in _ALLOWED_ARGS and v is not None}
    if "include_domains" in options:
        options["include_domains"] = tuple(options["include_domains"])
    try:
        response = await relay.search(query, **options)
    except QueryRejectedError as exc:
        kinds = ", ".join(exc.findings) or "personal data"  # kinds only, never the values
        return f"Search not performed: the query contains {kinds}. Remove it and try again."
    except LimitExceededError as exc:
        return f"Too many searches. Wait {exc.retry_after:.0f} s or answer without searching."
    except (ValueError, TypeError) as exc:
        return f"Invalid search request: {exc}"  # SeekRelay's own check of the arguments
    except InvalidRequestError:
        # The provider's own words would reach the model outside the untrusted block.
        return "The search provider rejected this request. Rephrase the query."
    except AllProvidersFailedError:
        return "Web search is currently unavailable. Answer without it and say so."
    return response.to_llm_context(max_chars_per_result=1200)


async def demo() -> None:
    # An agent should not wait minutes for a tool: cap each search at 20 seconds.
    async with SeekRelay.from_env(time_budget=20) as relay:
        print(json.dumps(TOOL_SPEC, indent=2))
        print(await handle_tool_call(relay, {"query": "httpx 1.0 release", "max_results": 3}))


if __name__ == "__main__":
    asyncio.run(demo())
