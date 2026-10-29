from __future__ import annotations

from pathlib import Path

import pytest

from seekrelay import (
    BraveProvider,
    ConfigurationError,
    SearchGateway,
    SearXNGProvider,
    TavilyProvider,
)
from seekrelay.config import build_gateway_from_env, build_providers_from_env, read_env_file


async def test_builds_available_providers_in_order() -> None:
    env = {
        "SEEKRELAY_PROVIDERS": "searxng, tavily ,brave",
        "TAVILY_API_KEY": "tvly-x",
        "SEARXNG_BASE_URL": "http://127.0.0.1:8888",
        "SEARXNG_ENGINES": "duckduckgo,brave",
    }
    providers = build_providers_from_env(env)
    assert [type(p) for p in providers] == [SearXNGProvider, TavilyProvider]
    for p in providers:
        await p.aclose()


async def test_default_order_and_brave_alias() -> None:
    env = {"BRAVE_SEARCH_API_KEY": "b", "TAVILY_API_KEY": "t"}
    providers = build_providers_from_env(env)
    assert [type(p) for p in providers] == [TavilyProvider, BraveProvider]
    for p in providers:
        await p.aclose()


def test_errors() -> None:
    with pytest.raises(ConfigurationError, match="No search provider"):
        build_providers_from_env({})
    with pytest.raises(ConfigurationError, match="Unknown provider"):
        build_providers_from_env({"SEEKRELAY_PROVIDERS": "google", "TAVILY_API_KEY": "t"})
    with pytest.raises(ConfigurationError, match="SEEKRELAY_PII_GUARD"):
        build_gateway_from_env({"TAVILY_API_KEY": "t", "SEEKRELAY_PII_GUARD": "maybe"})
    with pytest.raises(ConfigurationError, match="SEEKRELAY_TIMEOUT"):
        build_providers_from_env({"TAVILY_API_KEY": "t", "SEEKRELAY_TIMEOUT": "fast"})
    with pytest.raises(ConfigurationError, match="SearXNG base URL"):
        build_providers_from_env({"SEARXNG_BASE_URL": "127.0.0.1:8888"})


async def test_gateway_settings_from_env() -> None:
    env = {
        "TAVILY_API_KEY": "t",
        "SEEKRELAY_CACHE_TTL": "0",
        "SEEKRELAY_PII_GUARD": "off",
        "SEEKRELAY_MAX_ATTEMPTS": "3",
        "SEEKRELAY_COOLDOWN": "10",
    }
    gw = build_gateway_from_env(env)
    assert isinstance(gw, SearchGateway)
    assert gw._cache is None
    assert gw._pii_guard is None
    assert gw._retry.max_attempts == 3
    assert gw._cooldown_seconds == 10
    await gw.aclose()

    gw = SearchGateway.from_env(
        {"TAVILY_API_KEY": "t", "BRAVE_API_KEY": "b"}, order=["brave"], cache_ttl=5
    )
    assert gw.provider_names == ["brave"]
    assert gw._cache is not None
    await gw.aclose()


async def test_env_file_is_merged_and_environment_wins(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("TAVILY_API_KEY=t\nSEEKRELAY_PROVIDERS=tavily\n", encoding="utf-8")
    env = {"BRAVE_API_KEY": "b", "SEEKRELAY_PROVIDERS": "brave,tavily"}
    gw = SearchGateway.from_env(env, env_file=path)
    assert gw.provider_names == ["brave", "tavily"]
    await gw.aclose()

    with pytest.raises(FileNotFoundError):
        build_gateway_from_env(env, env_file=tmp_path / "missing.env")


def test_read_env_file(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# comment\n"
        "\n"
        "TAVILY_API_KEY=tvly-abc\n"
        "export BRAVE_API_KEY='brave key'\n"
        'SEARXNG_BASE_URL="http://127.0.0.1:8888"\n'
        "SEEKRELAY_CACHE_TTL=60 # one minute\n",
        encoding="utf-8",
    )
    assert read_env_file(path) == {
        "TAVILY_API_KEY": "tvly-abc",
        "BRAVE_API_KEY": "brave key",
        "SEARXNG_BASE_URL": "http://127.0.0.1:8888",
        "SEEKRELAY_CACHE_TTL": "60",
    }
    path.write_text("BROKEN LINE\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match=":1:"):
        read_env_file(path)


async def test_empty_values_keep_safe_defaults() -> None:
    gw = build_gateway_from_env(
        {"TAVILY_API_KEY": "t", "SEEKRELAY_PII_GUARD": " ", "SEEKRELAY_CACHE_TTL": ""}
    )
    assert gw._pii_guard is not None  # empty must not disable the guard
    assert gw._cache is not None
    await gw.aclose()


@pytest.mark.parametrize("value", ["nan", "inf", "-1"])
def test_non_finite_or_negative_numbers_rejected(value: str) -> None:
    with pytest.raises(ConfigurationError):
        build_gateway_from_env({"TAVILY_API_KEY": "t", "SEEKRELAY_COOLDOWN": value})


def test_quoted_value_with_trailing_comment(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "TAVILY_API_KEY=\"tvly-abc\" # prod key\nX='a # not a comment'\n", encoding="utf-8"
    )
    assert read_env_file(path) == {"TAVILY_API_KEY": "tvly-abc", "X": "a # not a comment"}
    path.write_text('BROKEN="no end\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="unterminated"):
        read_env_file(path)


async def test_order_is_case_insensitive() -> None:
    providers = build_providers_from_env({"TAVILY_API_KEY": "t"}, order=[" Tavily "])
    assert [p.name for p in providers] == ["tavily"]
    for p in providers:
        await p.aclose()
