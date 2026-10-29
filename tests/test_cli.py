"""CLI tests that never reach the network (config errors, PII rejection, listing)."""

from __future__ import annotations

from pathlib import Path

import pytest

from seekrelay.cli import EXIT_OK, EXIT_REJECTED, EXIT_USAGE, main

_ENV_VARS = (
    "TAVILY_API_KEY",
    "BRAVE_API_KEY",
    "BRAVE_SEARCH_API_KEY",
    "SEARXNG_BASE_URL",
    "SEEKRELAY_PROVIDERS",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # The CLI reads ./.env; keep a developer's real .env (and keys) out of the tests.
    monkeypatch.chdir(tmp_path)


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


def test_dotenv_in_current_directory_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".env").write_text("BRAVE_API_KEY=b\nSEEKRELAY_PROVIDERS=brave\n", encoding="utf-8")
    assert main(["providers"]) == EXIT_OK
    assert capsys.readouterr().out.strip() == "1. brave"

    # Real environment variables win over the file.
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    monkeypatch.setenv("SEEKRELAY_PROVIDERS", "tavily,brave")
    assert main(["providers"]) == EXIT_OK
    assert capsys.readouterr().out.splitlines() == ["1. tavily", "2. brave"]


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


class _FakeRelayFactory:
    """Replace the env-based builder with a relay over scripted providers."""

    def __init__(self) -> None:
        from seekrelay import QuotaExceededError

        from .conftest import FakeProvider

        self.providers = [
            FakeProvider("tavily", QuotaExceededError("tavily", "limit")),
            FakeProvider("brave", ["https://a.com/1", "https://b.com/2"], extract=True),
        ]

    def __call__(self, env: object, **overrides: object) -> object:
        from seekrelay import SeekRelay

        return SeekRelay(self.providers, cache_ttl=None)


@pytest.fixture
def fake_relay(monkeypatch: pytest.MonkeyPatch) -> _FakeRelayFactory:
    factory = _FakeRelayFactory()
    monkeypatch.setattr("seekrelay.cli.build_relay_from_env", factory)
    return factory


def test_search_text_output(fake_relay: object, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["search", "python jobs", "-n", "2", "--time-range", "week"]) == EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("[brave] 2 results")
    assert "fallback: tavily -> QuotaExceededError: limit" in out
    assert "1. brave 0" in out and "https://b.com/2" in out


@pytest.mark.parametrize(("fmt", "marker"), [("json", '"provider": "brave"'), ("llm", "<result")])
def test_search_machine_formats(
    fake_relay: object, capsys: pytest.CaptureFixture[str], fmt: str, marker: str
) -> None:
    assert main(["search", "q", "--format", fmt]) == EXIT_OK
    assert marker in capsys.readouterr().out


@pytest.mark.parametrize("fmt", ["text", "json", "llm"])
def test_extract_formats(fake_relay: object, capsys: pytest.CaptureFixture[str], fmt: str) -> None:
    assert main(["extract", "https://a.com", "--format", fmt]) == EXIT_OK
    assert "content of https://a.com" in capsys.readouterr().out


def test_all_failed_exit_code(
    fake_relay: _FakeRelayFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    from seekrelay import ProviderUnavailableError

    from .conftest import FakeProvider

    fake_relay.providers[:] = [FakeProvider("x", ProviderUnavailableError("x", "down"))]
    assert main(["search", "q"]) == 1
    assert "All providers failed" in capsys.readouterr().err


def test_provider_names_are_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    assert main(["--providers", "Tavily", "providers"]) == EXIT_OK
    assert capsys.readouterr().out.strip() == "1. tavily"
