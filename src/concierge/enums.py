"""Closed vocabularies for the domain.

Prefer these over bare string literals: a typo in an enum member is a failure at
import/lookup time, whereas a typo in a string ("budgett") silently becomes a
weight of 0.0 or an unmatched branch. All are ``StrEnum`` so they serialize as
their value and compare and hash equal to that string (Pydantic/JSON-friendly).
"""

from __future__ import annotations

from enum import StrEnum


class AgentName(StrEnum):
    """The specialized evaluator agents. Used as score labels and weight keys."""

    BUDGET = "budget"
    QUALITY = "quality"
    # Full scope:
    LOGISTICS = "logistics"
    SUSTAINABILITY = "sustainability"


class PriceTier(StrEnum):
    """Coarse price positioning of a product within a result set."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


class Availability(StrEnum):
    """Whether a product can currently be purchased.

    Derived by us from the catalog's boolean ``available`` flag, so it's a closed
    vocabulary we own. (``condition`` stays a plain ``str`` — its value set is
    supplied by the external catalog and we don't control it.)
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class LLMProvider(StrEnum):
    """Supported LLM access providers (OpenRouter is the default)."""

    OPENROUTER = "openrouter"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
