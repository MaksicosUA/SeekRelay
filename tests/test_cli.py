"""CLI tests that never reach the network (config errors, PII rejection, listing)."""

from __future__ import annotations

from pathlib import Path

import pytest

from search_gateway.cli import EXIT_OK, EXIT_REJECTED, EXIT_USAGE, main

_ENV_VARS = (
    "TAVILY_API_KEY",
    "BRAVE_API_KEY",
    "BRAVE_SEARCH_API_KEY",
    "SEARXNG_BASE_URL",
    "SEARCH_GATEWAY_PROVIDERS",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_providers_listing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://127.0.0.1:8888")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    assert main(["providers"]) == EXIT_OK
    assert capsys.readouterr().out.splitlines() == ["1. tavily", "2. searxng"]
    assert main(["--providers", "searxng,tavily", "providers"]) == EXIT_OK
    assert capsys.readouterr().out.splitlines() == ["1. searxng", "2. tavily"]


def test_env_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("BRAVE_API_KEY=b\n", encoding="utf-8")
    assert main(["--env-file", str(env_file), "providers"]) == EXIT_OK
    assert capsys.readouterr().out.strip() == "1. brave"


def test_missing_configuration(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["search", "hello"]) == EXIT_USAGE
    assert "No search provider" in capsys.readouterr().err


def test_pii_rejected_before_network(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    assert main(["search", "jobs for jane@corp.example"]) == EXIT_REJECTED
    err = capsys.readouterr().err
    assert "email" in err and "jane@corp.example" not in err


def test_invalid_query_is_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    assert main(["search", "q", "-n", "50"]) == EXIT_USAGE
    assert "max_results" in capsys.readouterr().err


class _FakeGatewayFactory:
    """Replace the env-based builder with a gateway over scripted providers."""

    def __init__(self) -> None:
        from search_gateway import QuotaExceededError

        from .conftest import FakeProvider

        self.providers = [
            FakeProvider("tavily", QuotaExceededError("tavily", "limit")),
            FakeProvider("brave", ["https://a.com/1", "https://b.com/2"], extract=True),
        ]

    def __call__(self, env: object, **overrides: object) -> object:
        from search_gateway import SearchGateway

        return SearchGateway(self.providers, cache_ttl=None)


@pytest.fixture
def fake_gateway(monkeypatch: pytest.MonkeyPatch) -> _FakeGatewayFactory:
    factory = _FakeGatewayFactory()
    monkeypatch.setattr("search_gateway.cli.build_gateway_from_env", factory)
    return factory


def test_search_text_output(fake_gateway: object, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["search", "python jobs", "-n", "2", "--time-range", "week"]) == EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("[brave] 2 results")
    assert "fallback: tavily -> QuotaExceededError: limit" in out
    assert "1. brave 0" in out and "https://b.com/2" in out


@pytest.mark.parametrize(("fmt", "marker"), [("json", '"provider": "brave"'), ("llm", "<result")])
def test_search_machine_formats(
    fake_gateway: object, capsys: pytest.CaptureFixture[str], fmt: str, marker: str
) -> None:
    assert main(["search", "q", "--format", fmt]) == EXIT_OK
    assert marker in capsys.readouterr().out


@pytest.mark.parametrize("fmt", ["text", "json", "llm"])
def test_extract_formats(
    fake_gateway: object, capsys: pytest.CaptureFixture[str], fmt: str
) -> None:
    assert main(["extract", "https://a.com", "--format", fmt]) == EXIT_OK
    assert "content of https://a.com" in capsys.readouterr().out


def test_all_failed_exit_code(
    fake_gateway: _FakeGatewayFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    from search_gateway import ProviderUnavailableError

    from .conftest import FakeProvider

    fake_gateway.providers[:] = [FakeProvider("x", ProviderUnavailableError("x", "down"))]
    assert main(["search", "q"]) == 1
    assert "All providers failed" in capsys.readouterr().err


def test_provider_names_are_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    assert main(["--providers", "Tavily", "providers"]) == EXIT_OK
    assert capsys.readouterr().out.strip() == "1. tavily"
