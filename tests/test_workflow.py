import json
from pathlib import Path

from concierge import workflow
from concierge.enums import AgentName
from concierge.models import Query, ScoreItem, ScoreList

_FIXTURE = Path(__file__).parent / "fixtures" / "search_response.json"


class FakeClient:
    def search_catalog(self, arguments):
        return json.loads(_FIXTURE.read_text())["result"]


class FakeOutput:
    def __init__(self, content):
        self.content = content


class FakeAgent:
    """Scores every product 0.5 so we can test wiring without a model."""

    def run(self, prompt):
        upids = [
            line.split('"')[3]
            for line in prompt.splitlines()
            if '"product_upid"' in line
        ]
        return FakeOutput(
            ScoreList(scores=[ScoreItem(product_upid=u, score=0.5) for u in upids])
        )


def test_deterministic_run_produces_ranked_recommendations():
    recs = workflow.run(Query(raw_text="jacket"), FakeClient(), use_llm=False, top_n=3)

    assert len(recs) == 3
    # cheapest product should rank at or near the top under equal weights
    assert recs[0].product.price == min(r.product.price for r in recs)
    # each rec carries both agents' scores and composed reasoning
    agents = {s.agent for s in recs[0].per_agent}
    assert agents == {AgentName.BUDGET, AgentName.LOGISTICS}
    assert recs[0].reasoning


def test_llm_run_with_injected_fake_agents():
    recs = workflow.run(
        Query(raw_text="jacket"),
        FakeClient(),
        use_llm=True,
        budget_agent=FakeAgent(),
        logistics_agent=FakeAgent(),
        top_n=5,
    )
    assert len(recs) == 3  # fixture has 3 products
    assert all(len(r.per_agent) == 2 for r in recs)


def _upids_from_prompt(prompt):
    import re

    return re.findall(r"gid://shopify/p/\w+", prompt)


class RevisingAgent:
    """Scores every product `initial`; on a revision prompt, scores `revised`."""

    def __init__(self, initial, revised):
        self.initial = initial
        self.revised = revised

    def run(self, prompt):
        score = self.revised if "Contested products" in prompt else self.initial
        upids = _upids_from_prompt(prompt)
        return FakeOutput(
            ScoreList(scores=[ScoreItem(product_upid=u, score=score) for u in upids])
        )


def test_debate_revises_contested_scores_and_narrows_spread():
    # Budget starts high, Logistics low -> every product contested; revision
    # moves them together (0.6 vs 0.5) so the debate converges.
    recs = workflow.run(
        Query(raw_text="jacket"),
        FakeClient(),
        use_llm=True,
        budget_agent=RevisingAgent(initial=0.9, revised=0.6),
        logistics_agent=RevisingAgent(initial=0.2, revised=0.5),
        debate_rounds=2,
        top_n=5,
    )
    assert len(recs) == 3
    for rec in recs:
        by_agent = {s.agent: s.score for s in rec.per_agent}
        assert by_agent[AgentName.BUDGET] == 0.6  # revised down
        assert by_agent[AgentName.LOGISTICS] == 0.5  # revised up


def test_empty_results_return_empty():
    class EmptyClient:
        def search_catalog(self, arguments):
            return {"structuredContent": {"products": []}}

    assert workflow.run(Query(raw_text="x"), EmptyClient(), use_llm=False) == []
