"""Agno Team variant — an alternative orchestration to the default pipeline.

Instead of our plain-Python fan-out + deterministic ``consensus.combine``, this
hands coordination to an Agno ``Team`` (``coordinate`` mode): a team-leader model
delegates to the Budget and Logistics member agents and *synthesizes* one blended
score per product. Selected with ``--team``.

Trade-off (the reason it's a variant, not the default): the final ranking is an
LLM synthesis, so it is not the deterministic, weight-configurable vote the
default path produces, and there is no separate per-agent score breakdown — the
members' assessments survive only inside the blended reasons. It exists to
demonstrate the ``Team`` primitive side by side with the auditable pipeline.
"""

from __future__ import annotations

import json
from typing import Any

from agno.team import Team, TeamMode

from concierge.agents.budget import build_budget_agent
from concierge.agents.logistics import build_logistics_agent
from concierge.features import budget_features, logistics_features
from concierge.models import Product, Query, Recommendation, ScoreList

_INSTRUCTIONS = (
    "You lead a shopping evaluation team with a Budget member and a Logistics "
    "member. Delegate each product to both members, then return ONE blended "
    "score (0.0-1.0) per product_upid. Keep reasons terse: a single short phrase "
    "(<= 15 words), one entry in the reasons list. Do not recompute the provided "
    "numbers or write long explanations."
)


class TeamError(Exception):
    """Raised when the Team does not return a parseable structured result."""


def build_team(model: Any) -> Team:
    return Team(
        members=[build_budget_agent(model), build_logistics_agent(model)],
        model=model,
        mode=TeamMode.coordinate,
        output_schema=ScoreList,
        instructions=_INSTRUCTIONS,
        name="concierge-team",
    )


def _build_prompt(products: list[Product], query: Query) -> str:
    budget = {f.product_upid: f for f in budget_features(products, query)}
    logistics = {f.product_upid: f for f in logistics_features(products, query)}
    rows = [
        {
            "product_upid": p.upid,
            "title": p.title,
            "budget": budget[p.upid].model_dump(mode="json"),
            "logistics": logistics[p.upid].model_dump(mode="json"),
        }
        for p in products
    ]
    return (
        f"Shopping query: {query.raw_text!r}\n\n"
        f"Products with per-domain feature records:\n{json.dumps(rows, indent=2)}\n\n"
        f"Return one blended score per product_upid."
    )


def run_team(
    products: list[Product], query: Query, team: Any, top_n: int = 5
) -> list[Recommendation]:
    """Run the Team and map its synthesized scores to a ranked shortlist."""
    try:
        output = team.run(_build_prompt(products, query))
    except Exception as exc:  # Agno coordinate-mode internals can fail on bad tool JSON
        raise TeamError(
            "Agno Team coordination failed while delegating/synthesizing "
            f"({type(exc).__name__}: {exc}). This is a known fragility of LLM-driven "
            "team coordination — retry, or use the default (deterministic) path."
        ) from exc

    content = output.content
    if not isinstance(content, ScoreList):
        raise TeamError(
            "Team did not return structured scores "
            f"(got {type(content).__name__}); the leader synthesis may have been "
            f"truncated. Retry, or use the default (deterministic) path."
        )

    by_upid = {p.upid: p for p in products}
    recommendations: list[Recommendation] = []
    for item in content.scores:
        product = by_upid.get(item.product_upid)
        if product is None:
            continue
        recommendations.append(
            Recommendation(
                product=product,
                final_score=max(0.0, min(1.0, item.score)),
                per_agent=[],  # Team synthesizes; no separate per-agent breakdown
                reasoning="  ".join(item.reasons),
            )
        )
    recommendations.sort(key=lambda r: r.final_score, reverse=True)
    return recommendations[:top_n]
