"""Shared plumbing for the hybrid evaluator agents.

Each agent computes deterministic features (pure Python), hands them to the LLM
as a table, and gets back a score per product. The LLM interprets the facts; it
never recomputes them. Injecting the ``agent`` (anything with ``.run(str)``
returning ``.content: ScoreList``) keeps this testable without a real model.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from concierge.enums import AgentName
from concierge.models import AgentScore, FeatureRecord, Product, Query


def _clamp(score: float) -> float:
    return max(0.0, min(1.0, score))


def build_prompt(
    agent_name: AgentName,
    products: list[Product],
    features: Sequence[FeatureRecord],
    query: Query,
) -> str:
    titles = {p.upid: p.title for p in products}
    rows: list[dict[str, Any]] = []
    for feature in features:
        row = feature.model_dump(mode="json")
        row["title"] = titles.get(row.get("product_upid", ""), "")
        rows.append(row)
    return (
        f"Shopping query: {query.raw_text!r}\n\n"
        f"You are the {agent_name.value} evaluator. For each product below, weigh "
        f"the provided feature record and assign a score from 0.0 (poor) to 1.0 "
        f"(excellent), with a one-line reason citing the specific features. Do not "
        f"recompute any numbers — interpret the ones given.\n\n"
        f"Products:\n{json.dumps(rows, indent=2)}\n\n"
        f"Return exactly one score per product_upid."
    )


def run_evaluator(
    agent: Any,
    agent_name: AgentName,
    products: list[Product],
    features: Sequence[FeatureRecord],
    query: Query,
) -> list[AgentScore]:
    """Run one evaluator and map its scores back onto AgentScore records."""
    prompt = build_prompt(agent_name, products, features, query)
    output = agent.run(prompt)
    score_list = output.content

    features_by_upid = {feature.product_upid: feature for feature in features}
    scores: list[AgentScore] = []
    for item in score_list.scores:
        feature = features_by_upid.get(item.product_upid)
        if feature is None:
            continue  # ignore any hallucinated upid
        scores.append(
            AgentScore(
                agent=agent_name,
                product_upid=item.product_upid,
                score=_clamp(item.score),  # clamp; no schema constraint
                reasons=item.reasons,
                features=feature,
            )
        )
    return scores


def build_revision_prompt(
    agent_name: AgentName,
    products: list[Product],
    features: Sequence[FeatureRecord],
    query: Query,
    own_by_upid: dict[str, AgentScore],
    peers_by_upid: dict[str, list[AgentScore]],
    contested: set[str],
) -> str:
    titles = {p.upid: p.title for p in products}
    features_by_upid = {f.product_upid: f for f in features}
    blocks: list[str] = []
    for upid in contested:
        own = own_by_upid.get(upid)
        if own is None:
            continue
        feature = features_by_upid.get(upid)
        peers = "; ".join(
            f"{p.agent.value} scored {p.score:.2f} ({'; '.join(p.reasons)})"
            for p in peers_by_upid.get(upid, [])
        )
        rendered_features = json.dumps(
            feature.model_dump(mode="json") if feature else {}
        )
        blocks.append(
            f"- {titles.get(upid, upid)}\n"
            f"  features: {rendered_features}\n"
            f"  your current score: {own.score:.2f} ({'; '.join(own.reasons)})\n"
            f"  other evaluators: {peers or 'none'}"
        )
    return (
        f"Shopping query: {query.raw_text!r}\n\n"
        f"You are the {agent_name.value} evaluator. Other evaluators disagree with "
        f"you on the products below. Reconsider each from YOUR perspective, having "
        f"read their reasoning. You may hold your score if you still believe it, or "
        f"adjust it — but stay true to your domain; do not adopt another agent's "
        f"priorities. Return one score (0.0-1.0) and a one-line reason per "
        f"product_upid.\n\n"
        f"Contested products:\n" + "\n".join(blocks)
    )


def run_revision(
    agent: Any,
    agent_name: AgentName,
    products: list[Product],
    features: Sequence[FeatureRecord],
    query: Query,
    own_scores: list[AgentScore],
    all_scores: list[AgentScore],
    contested: set[str],
) -> list[AgentScore]:
    """Ask one agent to reconsider its contested scores; keep the rest unchanged."""
    own_by_upid = {s.product_upid: s for s in own_scores}
    peers_by_upid: dict[str, list[AgentScore]] = {}
    for s in all_scores:
        if s.agent != agent_name:
            peers_by_upid.setdefault(s.product_upid, []).append(s)
    features_by_upid = {f.product_upid: f for f in features}

    prompt = build_revision_prompt(
        agent_name, products, features, query, own_by_upid, peers_by_upid, contested
    )
    output = agent.run(prompt)
    revised = {item.product_upid: item for item in output.content.scores}

    result: list[AgentScore] = []
    for score in own_scores:
        item = revised.get(score.product_upid)
        if score.product_upid in contested and item is not None:
            result.append(
                AgentScore(
                    agent=agent_name,
                    product_upid=score.product_upid,
                    score=_clamp(item.score),
                    reasons=item.reasons,
                    features=features_by_upid.get(score.product_upid),
                )
            )
        else:
            result.append(score)  # unchanged
    return result
