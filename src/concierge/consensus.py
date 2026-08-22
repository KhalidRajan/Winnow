"""Deterministic weighted consensus over per-agent scores.

Pure functions: no network, no LLM, no framework.
"""

from __future__ import annotations

from concierge.constants import TRADEOFF_THRESHOLD
from concierge.enums import AgentName
from concierge.models import AgentScore, Product, Recommendation


def _normalize_weights(weights: dict[AgentName, float]) -> dict[AgentName, float]:
    """Normalize positive weights to sum to 1; fall back to equal weights."""
    positive = {k: w for k, w in weights.items() if w > 0}
    total = sum(positive.values())
    if total <= 0:
        n = len(weights) or 1
        return {k: 1.0 / n for k in weights}
    return {k: positive.get(k, 0.0) / total for k in weights}


def _detect_tradeoffs(agent_scores: list[AgentScore]) -> list[str]:
    if len(agent_scores) < 2:
        return []
    high = max(agent_scores, key=lambda s: s.score)
    low = min(agent_scores, key=lambda s: s.score)
    if high.score - low.score >= TRADEOFF_THRESHOLD:
        note = (
            f"{high.agent} rates this {high.score:.2f} "
            f"but {low.agent} only {low.score:.2f}"
        )
        return [note]
    return []


def combine(
    products: list[Product],
    per_agent_scores: list[AgentScore],
    weights: dict[AgentName, float],
    top_n: int = 5,
) -> list[Recommendation]:
    """Combine per-agent scores into a ranked, weighted shortlist."""
    normalized = _normalize_weights(weights)

    scores_by_upid: dict[str, list[AgentScore]] = {}
    for score in per_agent_scores:
        scores_by_upid.setdefault(score.product_upid, []).append(score)

    recommendations: list[Recommendation] = []
    for product in products:
        agent_scores = scores_by_upid.get(product.upid, [])
        final = sum(normalized.get(s.agent, 0.0) * s.score for s in agent_scores)
        recommendations.append(
            Recommendation(
                product=product,
                final_score=final,
                per_agent=agent_scores,
                tradeoffs=_detect_tradeoffs(agent_scores),
            )
        )

    recommendations.sort(key=lambda r: r.final_score, reverse=True)
    return recommendations[:top_n]
