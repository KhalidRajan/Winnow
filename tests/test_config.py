import pytest

from concierge.config import ConfigError, Settings

_REQUIRED = {
    "SHOPIFY_CLIENT_ID": "id",
    "SHOPIFY_CLIENT_SECRET": "secret",
    "AGENT_PROFILE_URL": "https://example.com/profile.json",
    "LLM_API_KEY": "key",
}
_ALL_KEYS = list(_REQUIRED) + [
    "LLM_PROVIDER",
    "LLM_MODEL",
    "MCP_ENDPOINT",
    "AUTH_ENDPOINT",
]
# Point load() at a path that doesn't exist so a real .env on disk can't leak
# into these tests — env comes solely from monkeypatch.
_NO_ENV = "/nonexistent/.env"


def _set_env(monkeypatch, **overrides):
    for key in _ALL_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in {**_REQUIRED, **overrides}.items():
        monkeypatch.setenv(key, value)


def test_load_populates_settings(monkeypatch):
    _set_env(monkeypatch)
    settings = Settings.load(env_file=_NO_ENV)
    assert settings.shopify_client_id == "id"
    assert settings.llm_provider == "openrouter"  # default
    assert settings.mcp_endpoint.endswith("/ucp/mcp")


def test_load_respects_optional_overrides(monkeypatch):
    _set_env(monkeypatch, LLM_PROVIDER="anthropic", LLM_MODEL="claude-opus-4-8")
    settings = Settings.load(env_file=_NO_ENV)
    assert settings.llm_provider == "anthropic"
    assert settings.llm_model == "claude-opus-4-8"


def test_invalid_provider_raises(monkeypatch):
    _set_env(monkeypatch, LLM_PROVIDER="not-a-provider")
    with pytest.raises(ConfigError) as exc:
        Settings.load(env_file=_NO_ENV)
    assert "LLM_PROVIDER" in str(exc.value)


def test_missing_vars_are_all_reported(monkeypatch):
    for key in _ALL_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SHOPIFY_CLIENT_ID", "id")  # only one present

    with pytest.raises(ConfigError) as exc:
        Settings.load(env_file=_NO_ENV)

    message = str(exc.value)
    assert "SHOPIFY_CLIENT_SECRET" in message
    assert "AGENT_PROFILE_URL" in message
    assert "LLM_API_KEY" in message
    # The one that *is* set should not be listed as missing.
    assert "SHOPIFY_CLIENT_ID" not in message
