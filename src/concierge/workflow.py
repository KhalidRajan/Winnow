"""Pipeline orchestration: search -> score (Budget + Logistics) -> consensus.

A straight fan-out/fan-in in plain Python; the Agno pieces are the two evaluator
agents. With ``debate_rounds > 0`` an optional debate loop (``debate.py``) lets
the agents reconsider contested products before the vote. Full scope can lift
this into an Agno Workflow without touching the pure core.
"""

from __future__ import annotations

from typing import Any

from concierge import catalog, consensus, debate, team
from concierge.agents.base import run_revision
from concierge.agents.budget import build_budget_agent, score_budget
from concierge.agents.logistics import build_logistics_agent, score_logistics
from concierge.enums import AgentName
from concierge.features import budget_features, logistics_features
from concierge.mcp_client import McpClient
from concierge.models import AgentScore, Product, Query, Recommendation


def run(
    query: Query,
    client: McpClient,
    *,
    model: Any = None,
    use_llm: bool = True,
    use_team: bool = False,
    weights: dict[AgentName, float] | None = None,
    top_n: int = 5,
    debate_rounds: int = 0,
    budget_agent: Any = None,
    logistics_agent: Any = None,
    team_instance: Any = None,
) -> list[Recommendation]:
    """Search, score with both agents, and return a ranked shortlist.

    Default path: fan out to both agents, optional debate, deterministic
    weighted consensus. With ``use_team`` (LLM only), an Agno ``Team`` coordinates
    the agents and synthesizes the ranking instead — see ``team.py``.
    """
    products = catalog.search(query, client)
    if not products:
        return []

    if use_llm and use_team:
        return team.run_team(
            products, query, team_instance or team.build_team(model), top_n=top_n
        )

    if use_llm:
        budget_agent = budget_agent or build_budget_agent(model)
        logistics_agent = logistics_agent or build_logistics_agent(model)
        scores = score_budget(products, query, budget_agent) + score_logistics(
            products, query, logistics_agent
        )
        if debate_rounds > 0:
            scores = _debate(
                products, query, scores, budget_agent, logistics_agent, debate_rounds
            )
    else:
        scores = deterministic_scores(products, query)

    weights = weights or query.weights
    recommendations = consensus.combine(products, scores, weights, top_n=top_n)
    for rec in recommendations:
        rec.reasoning = _compose_reasoning(rec)
    return recommendations


def _debate(
    products: list[Product],
    query: Query,
    scores: list[AgentScore],
    budget_agent: Any,
    logistics_agent: Any,
    rounds: int,
) -> list[AgentScore]:
    """Build per-agent revisers over their own features and run the debate loop."""
    bfeats = budget_features(products, query)
    lfeats = logistics_features(products, query)

    def budget_reviser(
        own: list[AgentScore], all_scores: list[AgentScore], contested: set[str]
    ) -> list[AgentScore]:
        return run_revision(
            budget_agent,
            AgentName.BUDGET,
            products,
            bfeats,
            query,
            own,
            all_scores,
            contested,
        )

    def logistics_reviser(
        own: list[AgentScore], all_scores: list[AgentScore], contested: set[str]
    ) -> list[AgentScore]:
        return run_revision(
            logistics_agent,
            AgentName.LOGISTICS,
            products,
            lfeats,
            query,
            own,
            all_scores,
            contested,
        )

    return debate.run_debate(
        scores,
        {AgentName.BUDGET: budget_reviser, AgentName.LOGISTICS: logistics_reviser},
        rounds,
    )


def deterministic_scores(products: list[Product], query: Query) -> list[AgentScore]:
    """LLM-free scoring for --no-llm runs and tests. Simple, transparent rules."""
    scores: list[AgentScore] = []

    for feature in budget_features(products, query):
        base = 1.0 - (feature.price_percentile or 0.0)  # cheaper -> higher
        score = base * (0.2 if feature.over_budget else 1.0)
        reason = f"{feature.price_tier.value}-tier" + (
            ", over budget" if feature.over_budget else ""
        )
        scores.append(
            AgentScore(
                agent=AgentName.BUDGET,
                product_upid=feature.product_upid,
                score=round(score, 3),
                reasons=[reason],
                features=feature,
            )
        )

    for lf in logistics_features(products, query):
        assortment = min(1.0, (lf.size_count + lf.color_count) / 12.0)
        score = (0.5 + 0.5 * assortment) if lf.in_stock else 0.1
        reason = (
            f"{'in stock' if lf.in_stock else 'out of stock'}, "
            f"{lf.size_count} sizes / {lf.color_count} colors"
        )
        if lf.requested_size_available is False:
            score = min(score, 0.15)  # their size isn't offered
            reason += ", requested size unavailable"
        elif lf.requested_size_available is True:
            reason += ", requested size available"
        scores.append(
            AgentScore(
                agent=AgentName.LOGISTICS,
                product_upid=lf.product_upid,
                score=round(score, 3),
                reasons=[reason],
                features=lf,
            )
        )

    return scores


def _compose_reasoning(rec: Recommendation) -> str:
    parts = [
        f"[{score.agent.value}] {reason}"
        for score in rec.per_agent
        for reason in score.reasons
    ]
    return "  ".join(parts)
