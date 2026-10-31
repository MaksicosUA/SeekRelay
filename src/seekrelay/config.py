"""Build providers and a relay from environment variables.

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
``SEEKRELAY_TIMEOUT``           Limit for one request, to the last byte, in seconds. Default ``20``.
``SEEKRELAY_MAX_ATTEMPTS``      Attempts per provider. Default ``2``.
``SEEKRELAY_CACHE_TTL``         Cache TTL in seconds, ``0`` disables. Default ``600``.
``SEEKRELAY_COOLDOWN``          Cooldown after auth/quota errors. Default ``300``.
``SEEKRELAY_TIME_BUDGET``       Max seconds per call across all providers; ``0`` or
                                empty means no limit. Default: no limit.
``SEEKRELAY_PII_GUARD``       ``false`` disables the PII guard. Default ``true``.
=============================== ===================================================

Providers listed in ``SEEKRELAY_PROVIDERS`` but missing credentials are skipped
with an INFO log, so one ``.env`` works on machines with different keys.
"""

from __future__ import annotations

import logging
import math
import os
import re
from collections.abc import Callable, Mapping
from typing import Any

from .errors import ConfigurationError, NoProviderConfiguredError
from .pii import PIIGuard
from .providers import BraveProvider, SearchProvider, SearXNGProvider, TavilyProvider
from .relay import RetryPolicy, SeekRelay

__all__ = [
    "DEFAULT_PROVIDER_ORDER",
    "build_providers_from_env",
    "build_relay_from_env",
    "parse_csv",
    "read_env_file",
]

logger = logging.getLogger("seekrelay")

DEFAULT_PROVIDER_ORDER = ("tavily", "brave", "searxng")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_BRAVE_KEY_NAMES = ("BRAVE_API_KEY", "BRAVE_SEARCH_API_KEY")
_MAX_ENV_FILE_BYTES = 1_048_576
_ENV_BLANKS = " \t"
_ENV_EXPORT_RE = re.compile(r"^export[ \t]+")
_ENV_COMMENT_RE = re.compile(r"[ \t]#")


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
        finite = math.isfinite(number)  # overflows on an int with hundreds of digits
    except (ValueError, OverflowError) as exc:
        raise ConfigurationError(f"{name} must be a number, got {value!r}") from exc
    if not finite:
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
    # A size cap, not a check for regular files: a symlink to /dev/zero has to stop here, but
    # --env-file <(sops -d file) is a pipe and a legitimate use.
    with open(path, "rb") as file:
        data = file.read(_MAX_ENV_FILE_BYTES + 1)
    if len(data) > _MAX_ENV_FILE_BYTES:
        raise ConfigurationError(f"{path}: larger than 1 MiB")

    values: dict[str, str] = {}
    # LF only: str.splitlines() also breaks on VT, FF, FS, GS, RS, NEL, LS, PS and a lone CR, so
    # a line that grep and the shell see as one comment could carry live settings. Spaces and
    # tabs only, for the same reason: str.strip() also takes NBSP and those separators, and a
    # name that grep cannot find would still take effect. A CR inside a line can hide the rest
    # of it on a terminal. utf-8-sig: a BOM must not silently lose the first setting.
    for number, line in enumerate(data.decode("utf-8-sig").split("\n"), start=1):
        stripped = line.removesuffix("\r").strip(_ENV_BLANKS)
        if not stripped or stripped.startswith("#"):
            continue
        if "\r" in stripped:
            raise ConfigurationError(f"{path}:{number}: carriage return inside a line")
        stripped = _ENV_EXPORT_RE.sub("", stripped)
        if "=" not in stripped:
            raise ConfigurationError(f"{path}:{number}: expected KEY=VALUE")
        key, raw_value = stripped.split("=", 1)
        key, value = key.strip(_ENV_BLANKS), raw_value.strip(_ENV_BLANKS)
        if value[:1] in ("'", '"'):
            closing = value.find(value[0], 1)
            if closing == -1:
                raise ConfigurationError(f"{path}:{number}: unterminated quote")
            value = value[1:closing]  # anything after the closing quote is a comment
        elif value.startswith("#") and raw_value[:1] in (" ", "\t"):
            value = ""  # as in the shell, "KEY= # note" is empty and "KEY=#abc" is a value
        else:
            value = _ENV_COMMENT_RE.split(value, maxsplit=1)[0].rstrip(_ENV_BLANKS)
        values[key] = value
    return values


def build_providers_from_env(
    env: Mapping[str, str] | None = None,
    *,
    order: list[str] | None = None,
) -> list[SearchProvider]:
    """Create the providers that have credentials, in the configured order.

    Raises:
        NoProviderConfiguredError: no provider has credentials.
        ConfigurationError: unknown provider name or an invalid setting.
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
            key = _get(env, *_BRAVE_KEY_NAMES)
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
        raise NoProviderConfiguredError(
            "No search provider is configured. Set at least one of "
            "TAVILY_API_KEY, BRAVE_API_KEY or SEARXNG_BASE_URL."
        )
    return providers


def _merge_env_file(env: Mapping[str, str], path: str | os.PathLike[str]) -> dict[str, str]:
    """Put a file's values under ``env``. The Brave key has two names, so either one hides both."""
    values = read_env_file(path)
    if any(name in env for name in _BRAVE_KEY_NAMES):
        for name in _BRAVE_KEY_NAMES:
            values.pop(name, None)
    return {**values, **env}


def build_relay_from_env(
    env: Mapping[str, str] | None = None,
    *,
    env_file: str | os.PathLike[str] | None = None,
    **overrides: Any,
) -> SeekRelay:
    """Create a :class:`SeekRelay` from environment variables.

    ``env_file`` is a ``.env`` file read in addition to ``env`` (see
    :func:`read_env_file`); variables already present in ``env`` win. Keyword
    ``overrides`` are passed to :class:`SeekRelay` and win over the env.
    ``order=[...]`` overrides the provider order.
    """
    env = os.environ if env is None else env
    if env_file is not None:
        env = _merge_env_file(env, env_file)
    order = overrides.pop("order", None)
    providers = build_providers_from_env(env, order=order)
    settings: dict[str, Any] = {
        "retry": RetryPolicy(max_attempts=_number(env, "SEEKRELAY_MAX_ATTEMPTS", 2, int) or 1),
        "cache_ttl": _number(env, "SEEKRELAY_CACHE_TTL", 600.0) or None,
        "cooldown_seconds": _number(env, "SEEKRELAY_COOLDOWN", 300.0),
        "time_budget": _number(env, "SEEKRELAY_TIME_BUDGET", 0.0) or None,
        "pii_guard": PIIGuard() if _bool(env, "SEEKRELAY_PII_GUARD", True) else None,
    }
    settings.update(overrides)
    return SeekRelay(providers, **settings)
