"""Build the Agno model from settings. The only place providers are selected."""

from __future__ import annotations

from agno.models.base import Model

from concierge.config import Settings
from concierge.enums import LLMProvider


def build_model(settings: Settings) -> Model:
    """Instantiate an Agno model for the configured provider.

    OpenRouter is the default; switching provider or model is env-only
    (``LLM_PROVIDER`` / ``LLM_MODEL``) with no code change.
    """
    if settings.llm_provider == LLMProvider.OPENROUTER:
        from agno.models.openrouter import OpenRouter

        return OpenRouter(id=settings.llm_model, api_key=settings.llm_api_key)
    if settings.llm_provider == LLMProvider.ANTHROPIC:
        from agno.models.anthropic import Claude

        return Claude(id=settings.llm_model, api_key=settings.llm_api_key)
    if settings.llm_provider == LLMProvider.OPENAI:
        from agno.models.openai import OpenAIChat

        return OpenAIChat(id=settings.llm_model, api_key=settings.llm_api_key)
    raise ValueError(f"Unsupported LLM provider: {settings.llm_provider}")
