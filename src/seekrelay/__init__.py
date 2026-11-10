"""SeekRelay: one async interface for AI-agent web search.

Quick start::

    from seekrelay import SeekRelay

    async with SeekRelay.from_env() as relay:
        response = await relay.search("fastapi lifespan events", max_results=3)
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
    LimitExceededError,
    NoProviderConfiguredError,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    QueryRejectedError,
    QuotaExceededError,
    RateLimitError,
    ResponseFormatError,
    SeekRelayError,
    TimeBudgetExceededError,
    UnsupportedOperationError,
)
from .events import (
    EventHandler,
    ProviderStats,
    ProviderStatus,
    RelayEvent,
    RelayStats,
)
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
from .relay import RetryPolicy, SeekRelay

# Library convention: emit nothing unless the application configures logging.
logging.getLogger("seekrelay").addHandler(logging.NullHandler())

__all__ = [
    "AllProvidersFailedError",
    "AuthenticationError",
    "BraveProvider",
    "ConfigurationError",
    "EventHandler",
    "ExtractResponse",
    "ExtractedPage",
    "FailedExtraction",
    "InvalidRequestError",
    "LimitExceededError",
    "NoProviderConfiguredError",
    "PIIGuard",
    "ProviderAttempt",
    "ProviderError",
    "ProviderStats",
    "ProviderStatus",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "QueryRejectedError",
    "QuotaExceededError",
    "RateLimitError",
    "RelayEvent",
    "RelayStats",
    "ResponseFormatError",
    "RetryPolicy",
    "SearXNGProvider",
    "SearchDepth",
    "SearchProvider",
    "SearchQuery",
    "SearchResponse",
    "SearchResult",
    "SeekRelay",
    "SeekRelayError",
    "TTLCache",
    "TavilyProvider",
    "TimeBudgetExceededError",
    "TimeRange",
    "Topic",
    "UnsupportedOperationError",
    "__version__",
]
