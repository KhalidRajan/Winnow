"""Debate loop: agents reconsider contested scores over a few rounds.

Framework-independent. The loop itself is pure orchestration over a set of
per-agent ``reviser`` callables; the LLM-driven revision lives in the agents
layer. A product is *contested* when the agents' scores for it span at least
``spread_threshold``. The loop stops early once nothing is contested or scores
stop moving (convergence), which is the point of debating rather than looping a
fixed number of times.
"""

from __future__ import annotations

from collections.abc import Callable

from concierge.constants import (
    DEBATE_CONVERGENCE_EPSILON,
    DEBATE_SPREAD_THRESHOLD,
)
from concierge.enums import AgentName
from concierge.models import AgentScore

# (own_scores, all_scores, contested_upids) -> the agent's full revised scores.
Reviser = Callable[[list[AgentScore], list[AgentScore], set[str]], list[AgentScore]]


def contested_upids(
    scores: list[AgentScore], threshold: float = DEBATE_SPREAD_THRESHOLD
) -> set[str]:
    """UPIDs where the agents disagree by at least ``threshold``."""
    by_upid: dict[str, list[float]] = {}
    for score in scores:
        by_upid.setdefault(score.product_upid, []).append(score.score)
    return {
        upid
        for upid, values in by_upid.items()
        if len(values) >= 2 and max(values) - min(values) >= threshold
    }


def run_debate(
    scores: list[AgentScore],
    revisers: dict[AgentName, Reviser],
    rounds: int,
    *,
    spread_threshold: float = DEBATE_SPREAD_THRESHOLD,
    epsilon: float = DEBATE_CONVERGENCE_EPSILON,
) -> list[AgentScore]:
    """Run up to ``rounds`` revision rounds, stopping on convergence."""
    for _ in range(rounds):
        contested = contested_upids(scores, spread_threshold)
        if not contested:
            break  # nothing left to argue about

        previous = {(s.agent, s.product_upid): s.score for s in scores}
        revised: list[AgentScore] = []
        max_delta = 0.0
        for agent_name, reviser in revisers.items():
            own = [s for s in scores if s.agent == agent_name]
            new_own = reviser(own, scores, contested)
            revised.extend(new_own)
            for s in new_own:
                before = previous.get((agent_name, s.product_upid), s.score)
                max_delta = max(max_delta, abs(s.score - before))

        scores = revised
        if max_delta < epsilon:
            break  # scores stopped moving

    return scores
