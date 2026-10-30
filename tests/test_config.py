from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

import seekrelay
from seekrelay import (
    BraveProvider,
    ConfigurationError,
    NoProviderConfiguredError,
    SearXNGProvider,
    SeekRelay,
    TavilyProvider,
)
from seekrelay.config import build_providers_from_env, build_relay_from_env, read_env_file


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
        build_relay_from_env({"TAVILY_API_KEY": "t", "SEEKRELAY_PII_GUARD": "maybe"})
    with pytest.raises(ConfigurationError, match="SEEKRELAY_TIMEOUT"):
        build_providers_from_env({"TAVILY_API_KEY": "t", "SEEKRELAY_TIMEOUT": "fast"})
    with pytest.raises(ConfigurationError, match="SearXNG base URL"):
        build_providers_from_env({"SEARXNG_BASE_URL": "127.0.0.1:8888"})


async def test_relay_settings_from_env() -> None:
    env = {
        "TAVILY_API_KEY": "t",
        "SEEKRELAY_CACHE_TTL": "0",
        "SEEKRELAY_PII_GUARD": "off",
        "SEEKRELAY_MAX_ATTEMPTS": "3",
        "SEEKRELAY_COOLDOWN": "10",
    }
    relay = build_relay_from_env(env)
    assert isinstance(relay, SeekRelay)
    assert relay._cache is None
    assert relay._pii_guard is None
    assert relay._retry.max_attempts == 3
    assert relay._cooldown_seconds == 10
    await relay.aclose()

    relay = SeekRelay.from_env(
        {"TAVILY_API_KEY": "t", "BRAVE_API_KEY": "b"}, order=["brave"], cache_ttl=5
    )
    assert relay.provider_names == ["brave"]
    assert relay._cache is not None
    await relay.aclose()


async def test_time_budget_from_env() -> None:
    relay = build_relay_from_env({"TAVILY_API_KEY": "t", "SEEKRELAY_TIME_BUDGET": "15"})
    assert relay._time_budget == 15
    await relay.aclose()
    relay = build_relay_from_env({"TAVILY_API_KEY": "t", "SEEKRELAY_TIME_BUDGET": "0"})
    assert relay._time_budget is None
    await relay.aclose()


async def test_env_file_is_merged_and_environment_wins(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("TAVILY_API_KEY=t\nSEEKRELAY_PROVIDERS=tavily\n", encoding="utf-8")
    env = {"BRAVE_API_KEY": "b", "SEEKRELAY_PROVIDERS": "brave,tavily"}
    relay = SeekRelay.from_env(env, env_file=path)
    assert relay.provider_names == ["brave", "tavily"]
    await relay.aclose()

    with pytest.raises(FileNotFoundError):
        build_relay_from_env(env, env_file=tmp_path / "missing.env")


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
    relay = build_relay_from_env(
        {"TAVILY_API_KEY": "t", "SEEKRELAY_PII_GUARD": " ", "SEEKRELAY_CACHE_TTL": ""}
    )
    assert relay._pii_guard is not None  # empty must not disable the guard
    assert relay._cache is not None
    await relay.aclose()


@pytest.mark.parametrize("value", ["nan", "inf", "-1"])
def test_non_finite_or_negative_numbers_rejected(value: str) -> None:
    with pytest.raises(ConfigurationError):
        build_relay_from_env({"TAVILY_API_KEY": "t", "SEEKRELAY_COOLDOWN": value})


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


def test_no_provider_has_its_own_error() -> None:
    assert issubclass(NoProviderConfiguredError, ConfigurationError)
    assert "NoProviderConfiguredError" in seekrelay.__all__
    assert "NoProviderConfiguredError" in seekrelay.errors.__all__
    with pytest.raises(NoProviderConfiguredError, match="No search provider"):
        build_providers_from_env({})
    with pytest.raises(NoProviderConfiguredError):
        build_providers_from_env({"TAVILY_API_KEY": " "}, order=["tavily"])


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({"SEEKRELAY_PROVIDERS": "gogle", "TAVILY_API_KEY": "t"}, id="unknown-name"),
        pytest.param({"SEEKRELAY_TIMEOUT": "fast", "TAVILY_API_KEY": "t"}, id="bad-number"),
        pytest.param({"SEARXNG_BASE_URL": "127.0.0.1:8888"}, id="bad-url"),
    ],
)
def test_other_configuration_errors_do_not_claim_that_nothing_is_set(env: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError) as info:
        build_providers_from_env(env)
    assert not isinstance(info.value, NoProviderConfiguredError)


@pytest.mark.parametrize(
    "separator",
    [
        pytest.param("\x0b", id="vt"),
        pytest.param("\x0c", id="ff"),
        pytest.param("\x1c", id="fs"),
        pytest.param("\x1d", id="gs"),
        pytest.param("\x1e", id="rs"),
        pytest.param("\x85", id="nel"),
        pytest.param("\U00002028", id="line-separator"),
        pytest.param("\U00002029", id="paragraph-separator"),
        pytest.param("\r", id="lone-cr"),
    ],
)
def test_comment_ends_only_at_lf(separator: str, tmp_path: Path) -> None:
    path = tmp_path / ".env"
    settings = [
        "# rotate keys",
        "SEARXNG_BASE_URL=http://evil.example",
        "SEEKRELAY_PII_GUARD=false",
    ]
    path.write_bytes((separator.join(settings) + "\nTAVILY_API_KEY=t\n").encode("utf-8"))
    assert read_env_file(path) == {"TAVILY_API_KEY": "t"}


def test_crlf_files_still_work(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(
        b"# comment\r\n"
        b"\r\n"
        b"TAVILY_API_KEY=tvly-abc\r\n"
        b"export BRAVE_API_KEY='brave key'\r\n"
        b"SEEKRELAY_CACHE_TTL=60 # one minute\r\n"
        b"SEARXNG_ENGINES=\r\n"
    )
    assert read_env_file(path) == {
        "TAVILY_API_KEY": "tvly-abc",
        "BRAVE_API_KEY": "brave key",
        "SEEKRELAY_CACHE_TTL": "60",
        "SEARXNG_ENGINES": "",
    }


@pytest.mark.parametrize("first_line", [b"SEEKRELAY_PROVIDERS=searxng\n", b"# comment\n"])
def test_bom_does_not_lose_the_first_line(first_line: bytes, tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"\xef\xbb\xbf" + first_line + b"SEARXNG_BASE_URL=http://127.0.0.1:8888\n")
    plain = tmp_path / "plain.env"
    plain.write_bytes(first_line + b"SEARXNG_BASE_URL=http://127.0.0.1:8888\n")
    assert read_env_file(path) == read_env_file(plain)


@pytest.mark.parametrize(
    ("line", "value"),
    [
        pytest.param("KEY= # comment", "", id="comment-after-a-space"),
        pytest.param("KEY=   # comment", "", id="comment-after-spaces"),
        pytest.param("KEY=\t# comment", "", id="comment-after-a-tab"),
        pytest.param("KEY = # comment", "", id="spaces-around-the-sign"),
        pytest.param("export KEY= # comment", "", id="export"),
        pytest.param("KEY=#abc", "#abc", id="hash-without-a-space"),
        pytest.param("KEY=#abc # comment", "#abc", id="hash-value-and-a-comment"),
        pytest.param("KEY=a#b", "a#b", id="hash-inside"),
        pytest.param("KEY=abc # comment", "abc", id="value-and-a-comment"),
        pytest.param("KEY=", "", id="empty"),
        pytest.param("KEY=   ", "", id="blank"),
        pytest.param("KEY=' # quoted'", " # quoted", id="comment-in-quotes"),
        pytest.param('KEY= "# quoted"', "# quoted", id="space-before-the-quotes"),
    ],
)
def test_hash_after_the_equals_sign(line: str, value: str, tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(line + "\nNEXT=1\n", encoding="utf-8")
    assert read_env_file(path) == {"KEY": value, "NEXT": "1"}


async def test_blank_key_with_a_comment_does_not_enable_a_provider(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("TAVILY_API_KEY= # not used here\nBRAVE_API_KEY=b\n", encoding="utf-8")
    relay = build_relay_from_env({}, env_file=path)
    assert relay.provider_names == ["brave"]
    await relay.aclose()


def test_env_file_size_limit(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"#" * 1_048_576)
    assert read_env_file(path) == {}
    path.write_bytes(b"#" * 1_048_577)
    with pytest.raises(ConfigurationError, match=r"\.env: larger than 1 MiB"):
        read_env_file(path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_env_file_may_be_a_pipe(tmp_path: Path) -> None:
    path = tmp_path / "fifo"
    os.mkfifo(path)  # what --env-file <(sops -d file) hands over

    def write() -> None:
        path.write_text("TAVILY_API_KEY=t\n", encoding="utf-8")

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    assert read_env_file(path) == {"TAVILY_API_KEY": "t"}
    writer.join(10)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_long_stream_stops_at_the_size_limit(tmp_path: Path) -> None:
    path = tmp_path / "fifo"
    os.mkfifo(path)
    stopped_early: list[bool] = []

    def write() -> None:
        try:
            path.write_bytes(b"#" * (4 * 1_048_576))
        except BrokenPipeError:
            stopped_early.append(True)

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    with pytest.raises(ConfigurationError, match="larger than 1 MiB"):
        read_env_file(path)
    writer.join(10)
    assert stopped_early == [True]  # the reader left, instead of draining the stream


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        pytest.param("SEEKRELAY_MAX_ATTEMPTS", "9" * 400, "must be a number", id="int-overflow"),
        pytest.param("SEEKRELAY_COOLDOWN", "9" * 400, "must be a finite number", id="float-inf"),
        pytest.param("SEEKRELAY_COOLDOWN", "1e999", "must be a finite number", id="exponent"),
        pytest.param("SEEKRELAY_COOLDOWN", "soon", "must be a number", id="not-a-number"),
    ],
)
def test_bad_numbers(name: str, value: str, message: str) -> None:
    with pytest.raises(ConfigurationError, match=f"{name} {message}"):
        build_relay_from_env({"TAVILY_API_KEY": "t", name: value})


_BRAVE_NAMES = [
    pytest.param("BRAVE_SEARCH_API_KEY", "BRAVE_API_KEY", id="real-alias-file-main-name"),
    pytest.param("BRAVE_API_KEY", "BRAVE_SEARCH_API_KEY", id="real-main-name-file-alias"),
]


@pytest.mark.parametrize(("real", "in_file"), _BRAVE_NAMES)
async def test_real_brave_key_beats_the_file_under_either_name(
    real: str, in_file: str, tmp_path: Path
) -> None:
    path = tmp_path / ".env"
    path.write_text(f"{in_file}=from-file\n", encoding="utf-8")
    relay = build_relay_from_env({real: "real"}, env_file=path)
    assert relay.providers[0]._api_key == "real"
    await relay.aclose()


@pytest.mark.parametrize(("real", "in_file"), _BRAVE_NAMES)
def test_empty_real_brave_key_skips_brave_under_either_name(
    real: str, in_file: str, tmp_path: Path
) -> None:
    path = tmp_path / ".env"
    path.write_text(f"{in_file}=from-file\n", encoding="utf-8")
    with pytest.raises(NoProviderConfiguredError):
        build_relay_from_env({real: ""}, env_file=path)


@pytest.mark.parametrize("name", ["BRAVE_API_KEY", "BRAVE_SEARCH_API_KEY"])
async def test_file_brave_key_counts_without_a_real_one(name: str, tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(f"{name}=from-file\n", encoding="utf-8")
    relay = build_relay_from_env({"TAVILY_API_KEY": "t"}, env_file=path)
    assert relay.providers[1]._api_key == "from-file"
    await relay.aclose()


async def test_from_env_uses_the_same_merge(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("BRAVE_API_KEY=from-file\n", encoding="utf-8")
    relay = SeekRelay.from_env({"BRAVE_SEARCH_API_KEY": "real"}, env_file=path)
    assert relay.providers[0]._api_key == "real"
    await relay.aclose()


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("SEEKRELAY_PII_GUARD\xa0=false", id="no-break-space"),
        pytest.param("SEEKRELAY_PII_GUARD\x1f=false", id="unit-separator"),
        pytest.param("\x1cSEEKRELAY_PII_GUARD=false", id="file-separator"),
        pytest.param("\x85SEEKRELAY_PII_GUARD=false", id="next-line"),
    ],
)
def test_env_file_takes_only_spaces_and_tabs_around_a_name(tmp_path: Path, line: str) -> None:
    path = tmp_path / "settings.env"
    path.write_bytes(f"{line}\n".encode())
    assert "SEEKRELAY_PII_GUARD" not in read_env_file(path)


def test_env_file_refuses_a_carriage_return_inside_a_line(tmp_path: Path) -> None:
    path = tmp_path / "settings.env"
    path.write_bytes(b"OK=1\nSEEKRELAY_PII_GUARD=false\r # the guard stays on\n")
    with pytest.raises(ConfigurationError, match=r":2: carriage return"):
        read_env_file(path)


def test_env_file_keeps_a_carriage_return_inside_a_comment_harmless(tmp_path: Path) -> None:
    path = tmp_path / "settings.env"
    path.write_bytes(b"# note\rSEEKRELAY_PII_GUARD=false\nA=1\r\n")
    assert read_env_file(path) == {"A": "1"}


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param("export\tA=1", {"A": "1"}, id="tab-after-export"),
        pytest.param("export   A=1", {"A": "1"}, id="spaces-after-export"),
        pytest.param("A=1\t# note", {"A": "1"}, id="tab-before-a-comment"),
        pytest.param("A=\t# note", {"A": ""}, id="tab-before-a-comment-on-an-empty-value"),
        pytest.param("exported=1", {"exported": "1"}, id="name-starting-with-export"),
    ],
)
def test_env_file_reads_tabs_as_the_shell_does(
    tmp_path: Path, line: str, expected: dict[str, str]
) -> None:
    path = tmp_path / "settings.env"
    path.write_text(f"{line}\n", encoding="utf-8")
    assert read_env_file(path) == expected
