"""Budget evaluator: reasons over price features to score value."""

from __future__ import annotations

from typing import Any

from agno.agent import Agent

from concierge.agents.base import run_evaluator
from concierge.enums import AgentName
from concierge.features import budget_features
from concierge.models import AgentScore, Product, Query, ScoreList

_INSTRUCTIONS = (
    "You are a budget-conscious shopping evaluator. You reward genuine value: "
    "low price relative to the result set, staying within budget, and sensible "
    "price tier. Penalize over-budget items heavily. Score 0.0-1.0."
)


def build_budget_agent(model: Any) -> Agent:
    return Agent(
        model=model,
        output_schema=ScoreList,
        instructions=_INSTRUCTIONS,
        name=AgentName.BUDGET.value,
    )


def score_budget(products: list[Product], query: Query, agent: Any) -> list[AgentScore]:
    features = budget_features(products, query)
    return run_evaluator(agent, AgentName.BUDGET, products, features, query)
