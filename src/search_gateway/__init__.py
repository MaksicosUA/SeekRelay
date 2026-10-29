"""search-gateway: one async interface for AI-agent web search.

Quick start::

    from search_gateway import SearchGateway

    async with SearchGateway.from_env() as gateway:
        response = await gateway.search("fastapi lifespan events", max_results=3)
        prompt_block = response.to_llm_context()
"""

import logging

from ._version import __version__
from .cache import TTLCache
from .errors import (
    AllProvidersFailedError,
    AuthenticationError,
    ConfigurationError,
    InvalidRequestError,
    ProviderError,
    ProviderUnavailableError,
    QueryRejectedError,
    QuotaExceededError,
    RateLimitError,
    ResponseFormatError,
    SearchGatewayError,
    UnsupportedOperationError,
)
from .gateway import RetryPolicy, SearchGateway
from .models import (
    ExtractedPage,
    ExtractResponse,
    FailedExtraction,
    ProviderAttempt,
    SearchDepth,
    SearchQuery,
    SearchResponse,
    SearchResult,
    TimeRange,
    Topic,
)
from .pii import PIIGuard
from .providers import BraveProvider, SearchProvider, SearXNGProvider, TavilyProvider

# Library convention: emit nothing unless the application configures logging.
logging.getLogger("search_gateway").addHandler(logging.NullHandler())

__all__ = [
    "AllProvidersFailedError",
    "AuthenticationError",
    "BraveProvider",
    "ConfigurationError",
    "ExtractResponse",
    "ExtractedPage",
    "FailedExtraction",
    "InvalidRequestError",
    "PIIGuard",
    "ProviderAttempt",
    "ProviderError",
    "ProviderUnavailableError",
    "QueryRejectedError",
    "QuotaExceededError",
    "RateLimitError",
    "ResponseFormatError",
    "RetryPolicy",
    "SearXNGProvider",
    "SearchDepth",
    "SearchGateway",
    "SearchGatewayError",
    "SearchProvider",
    "SearchQuery",
    "SearchResponse",
    "SearchResult",
    "TTLCache",
    "TavilyProvider",
    "TimeRange",
    "Topic",
    "UnsupportedOperationError",
    "__version__",
]
