"""Build providers and a gateway from environment variables.

Variables (all optional except at least one provider's credentials):

=============================== ===================================================
``SEEKRELAY_PROVIDERS``         Priority order, comma-separated.
                                Default ``tavily,brave,searxng``.
``TAVILY_API_KEY``              Enables Tavily.
``TAVILY_INCLUDE_ANSWER``       ``true`` to request Tavily's short answer.
``BRAVE_API_KEY``               Enables Brave (``BRAVE_SEARCH_API_KEY`` also works).
``SEARXNG_BASE_URL``            Enables SearXNG, e.g. ``http://127.0.0.1:8888``.
``SEARXNG_ENGINES``             Comma-separated engine list (instance default if empty).
``SEARXNG_AUTH_HEADER``         Value for an ``Authorization`` header (reverse proxy).
``SEEKRELAY_TIMEOUT``           Per-request timeout in seconds. Default ``20``.
``SEEKRELAY_MAX_ATTEMPTS``      Attempts per provider. Default ``2``.
``SEEKRELAY_CACHE_TTL``         Cache TTL in seconds, ``0`` disables. Default ``600``.
``SEEKRELAY_COOLDOWN``          Cooldown after auth/quota errors. Default ``300``.
``SEEKRELAY_PII_GUARD``         ``false`` disables the PII guard. Default ``true``.
=============================== ===================================================

Providers listed in ``SEEKRELAY_PROVIDERS`` but missing credentials are skipped
with an INFO log, so one ``.env`` works on machines with different keys.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .gateway import RetryPolicy, SearchGateway
from .pii import PIIGuard
from .providers import BraveProvider, SearchProvider, SearXNGProvider, TavilyProvider

__all__ = [
    "DEFAULT_PROVIDER_ORDER",
    "build_gateway_from_env",
    "build_providers_from_env",
    "parse_csv",
    "read_env_file",
]

logger = logging.getLogger("seekrelay")

DEFAULT_PROVIDER_ORDER = ("tavily", "brave", "searxng")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _get(env: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = env.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = env.get(name)
    if value is None or not value.strip():  # unset or empty: keep the safe default
        return default
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigurationError(f"{name} must be true/false, got {value!r}")


def _number(
    env: Mapping[str, str], name: str, default: float, cast: Callable[[str], Any] = float
) -> Any:
    value = env.get(name)
    if value is None or not value.strip():
        return default
    try:
        number = cast(value.strip())
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number, got {value!r}") from exc
    if not math.isfinite(number):
        raise ConfigurationError(f"{name} must be a finite number, got {value!r}")
    if number < 0:
        raise ConfigurationError(f"{name} must not be negative")
    return number


def parse_csv(value: str | None) -> list[str]:
    """Split a comma-separated setting into lower-case, non-empty items."""
    return [part.strip().lower() for part in (value or "").split(",") if part.strip()]


def read_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """Parse a minimal ``.env`` file: ``KEY=VALUE`` lines, ``#`` comments, optional quotes.

    Kept dependency-free on purpose; use python-dotenv if you need interpolation.
    """
    values: dict[str, str] = {}
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stripped = stripped.removeprefix("export ").strip()
        if "=" not in stripped:
            raise ConfigurationError(f"{path}:{number}: expected KEY=VALUE")
        key, value = stripped.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] in ("'", '"'):
            closing = value.find(value[0], 1)
            if closing == -1:
                raise ConfigurationError(f"{path}:{number}: unterminated quote")
            value = value[1:closing]  # anything after the closing quote is a comment
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def build_providers_from_env(
    env: Mapping[str, str] | None = None,
    *,
    order: list[str] | None = None,
) -> list[SearchProvider]:
    """Create the providers that have credentials, in the configured order.

    Raises:
        ConfigurationError: unknown provider name, or no provider has credentials.
    """
    env = os.environ if env is None else env
    if order:
        names = [name.strip().lower() for name in order if name.strip()]
    else:
        names = parse_csv(env.get("SEEKRELAY_PROVIDERS")) or list(DEFAULT_PROVIDER_ORDER)
    unknown = sorted(set(names) - set(DEFAULT_PROVIDER_ORDER))
    if unknown:
        raise ConfigurationError(
            f"Unknown provider(s): {', '.join(unknown)}. Known: {', '.join(DEFAULT_PROVIDER_ORDER)}"
        )
    timeout = _number(env, "SEEKRELAY_TIMEOUT", 20.0)

    providers: list[SearchProvider] = []
    for name in dict.fromkeys(names):
        if name == "tavily":
            key = _get(env, "TAVILY_API_KEY")
            if key:
                providers.append(
                    TavilyProvider(
                        key,
                        include_answer=_bool(env, "TAVILY_INCLUDE_ANSWER", False),
                        timeout=timeout,
                    )
                )
                continue
        elif name == "brave":
            key = _get(env, "BRAVE_API_KEY", "BRAVE_SEARCH_API_KEY")
            if key:
                providers.append(BraveProvider(key, timeout=timeout))
                continue
        elif name == "searxng":
            base_url = _get(env, "SEARXNG_BASE_URL")
            if base_url:
                auth = _get(env, "SEARXNG_AUTH_HEADER")
                providers.append(
                    SearXNGProvider(
                        base_url,
                        engines=parse_csv(env.get("SEARXNG_ENGINES")),
                        headers={"Authorization": auth} if auth else None,
                        timeout=timeout,
                    )
                )
                continue
        logger.info("provider %s skipped: no credentials configured", name)

    if not providers:
        raise ConfigurationError(
            "No search provider is configured. Set at least one of "
            "TAVILY_API_KEY, BRAVE_API_KEY or SEARXNG_BASE_URL."
        )
    return providers


def build_gateway_from_env(env: Mapping[str, str] | None = None, **overrides: Any) -> SearchGateway:
    """Create a :class:`SearchGateway` from environment variables.

    Keyword ``overrides`` are passed to :class:`SearchGateway` and win over the env.
    ``order=[...]`` overrides the provider order.
    """
    env = os.environ if env is None else env
    order = overrides.pop("order", None)
    providers = build_providers_from_env(env, order=order)
    settings: dict[str, Any] = {
        "retry": RetryPolicy(max_attempts=_number(env, "SEEKRELAY_MAX_ATTEMPTS", 2, int) or 1),
        "cache_ttl": _number(env, "SEEKRELAY_CACHE_TTL", 600.0) or None,
        "cooldown_seconds": _number(env, "SEEKRELAY_COOLDOWN", 300.0),
        "pii_guard": PIIGuard() if _bool(env, "SEEKRELAY_PII_GUARD", True) else None,
    }
    settings.update(overrides)
    return SearchGateway(providers, **settings)
