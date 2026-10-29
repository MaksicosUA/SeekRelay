"""Expose the gateway as a tool for an LLM agent (framework-neutral).

`TOOL_SPEC` is a JSON-schema tool definition in the shape most tool-calling APIs
accept (name / description / input_schema). `handle_tool_call` turns the model's
arguments into a gateway call and returns a prompt-safe text block.

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
    QueryRejectedError,
    SearchGateway,
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


async def handle_tool_call(gateway: SearchGateway, arguments: dict[str, Any]) -> str:
    query = str(arguments.get("query", ""))
    options = {k: v for k, v in arguments.items() if k in _ALLOWED_ARGS and v is not None}
    if "include_domains" in options:
        options["include_domains"] = tuple(options["include_domains"])
    try:
        response = await gateway.search(query, **options)
    except QueryRejectedError as exc:
        return f"Search not performed: {exc}"
    except (ValueError, TypeError, InvalidRequestError) as exc:
        return f"Invalid search request: {exc}"
    except AllProvidersFailedError:
        return "Web search is currently unavailable. Answer without it and say so."
    return response.to_llm_context(max_chars_per_result=1200)


async def demo() -> None:
    async with SearchGateway.from_env() as gateway:
        print(json.dumps(TOOL_SPEC, indent=2))
        print(await handle_tool_call(gateway, {"query": "httpx 1.0 release", "max_results": 3}))


if __name__ == "__main__":
    asyncio.run(demo())
