"""Environment-backed configuration with fail-loud validation."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from concierge.enums import LLMProvider


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


# env var -> Settings field, for the four required values.
_REQUIRED = {
    "SHOPIFY_CLIENT_ID": "shopify_client_id",
    "SHOPIFY_CLIENT_SECRET": "shopify_client_secret",
    "AGENT_PROFILE_URL": "agent_profile_url",
    "LLM_API_KEY": "llm_api_key",
}


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings loaded from the environment."""

    shopify_client_id: str
    shopify_client_secret: str
    agent_profile_url: str
    llm_api_key: str
    llm_provider: LLMProvider = LLMProvider.OPENROUTER
    llm_model: str = "anthropic/claude-opus-4-8"
    mcp_endpoint: str = "https://catalog.shopify.com/api/ucp/mcp"
    auth_endpoint: str = "https://api.shopify.com/auth/access_token"
    # Telegram is opt-in: only required when running with --telegram, which
    # validates them itself (see telegram.require_telegram_settings).
    telegram_bot_token: str | None = None
    telegram_allowed_chat_ids: tuple[str, ...] = ()

    @classmethod
    def load(cls, env_file: str | None = None) -> Settings:
        """Load settings from the environment (and a ``.env`` file if present).

        Raises ``ConfigError`` naming *every* missing required variable at once,
        rather than failing on the first one.
        """
        # override=False: real environment variables win over .env, which keeps
        # tests that monkeypatch os.environ deterministic.
        load_dotenv(dotenv_path=env_file, override=False)

        present: dict[str, str] = {}
        missing: list[str] = []
        for env in _REQUIRED:
            value = os.environ.get(env)
            if value:
                present[env] = value  # narrowed to str for the type checker
            else:
                missing.append(env)
        if missing:
            raise ConfigError(
                "Missing required environment variables: " + ", ".join(sorted(missing))
            )

        provider_raw = os.environ.get("LLM_PROVIDER", LLMProvider.OPENROUTER.value)
        try:
            llm_provider = LLMProvider(provider_raw)
        except ValueError as exc:
            valid = ", ".join(p.value for p in LLMProvider)
            raise ConfigError(
                f"Invalid LLM_PROVIDER {provider_raw!r}; expected one of: {valid}"
            ) from exc

        return cls(
            shopify_client_id=present["SHOPIFY_CLIENT_ID"],
            shopify_client_secret=present["SHOPIFY_CLIENT_SECRET"],
            agent_profile_url=present["AGENT_PROFILE_URL"],
            llm_api_key=present["LLM_API_KEY"],
            llm_provider=llm_provider,
            llm_model=os.environ.get("LLM_MODEL", "anthropic/claude-opus-4-8"),
            mcp_endpoint=os.environ.get(
                "MCP_ENDPOINT", "https://catalog.shopify.com/api/ucp/mcp"
            ),
            auth_endpoint=os.environ.get(
                "AUTH_ENDPOINT", "https://api.shopify.com/auth/access_token"
            ),
            # Stripped like the chat ids below: a token pasted from BotFather
            # with a trailing newline is truthy, so it survives
            # require_telegram_settings and fails much later as an opaque 404
            # from getUpdates. The trailing `or None` keeps a whitespace-only
            # value failing loud there instead of becoming a one-space token.
            telegram_bot_token=(os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
            or None,
            telegram_allowed_chat_ids=tuple(
                chat_id.strip()
                for chat_id in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(
                    ","
                )
                if chat_id.strip()
            ),
        )
