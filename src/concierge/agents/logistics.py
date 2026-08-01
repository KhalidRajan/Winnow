"""Logistics evaluator: reasons over availability/fulfillment features."""

from __future__ import annotations

from typing import Any

from agno.agent import Agent

from concierge.agents.base import run_evaluator
from concierge.enums import AgentName
from concierge.features import logistics_features
from concierge.models import AgentScore, Product, Query, ScoreList

_INSTRUCTIONS = (
    "You are an availability and fulfillment evaluator. In-stock is a floor: "
    "out-of-stock items score low. If requested_size_available is false, the "
    "shopper's size is not offered — score very low regardless of other signals; "
    "if true, reward it. Otherwise reward broader size and color assortment "
    "(better odds the shopper's variant is available) and confirmed shipping to "
    "the requested region. Score 0.0-1.0."
)


def build_logistics_agent(model: Any) -> Agent:
    return Agent(
        model=model,
        output_schema=ScoreList,
        instructions=_INSTRUCTIONS,
        name=AgentName.LOGISTICS.value,
    )


def score_logistics(
    products: list[Product], query: Query, agent: Any
) -> list[AgentScore]:
    features = logistics_features(products, query)
    return run_evaluator(agent, AgentName.LOGISTICS, products, features, query)
