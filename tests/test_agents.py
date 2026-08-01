from concierge.agents.base import build_prompt, run_evaluator
from concierge.enums import AgentName
from concierge.features import budget_features
from concierge.models import Product, Query, ScoreItem, ScoreList


def _products():
    return [
        Product(upid="a", title="Cheap Jacket", price=50),
        Product(upid="b", title="Pricey Jacket", price=300),
    ]


class FakeOutput:
    def __init__(self, content):
        self.content = content


class FakeAgent:
    """Stands in for an Agno Agent — records the prompt, returns canned scores."""

    def __init__(self, score_list):
        self.score_list = score_list
        self.prompt = None

    def run(self, prompt):
        self.prompt = prompt
        return FakeOutput(self.score_list)


def test_build_prompt_includes_titles_and_features():
    products = _products()
    feats = budget_features(products, Query(raw_text="jacket"))
    prompt = build_prompt(AgentName.BUDGET, products, feats, Query(raw_text="jacket"))
    assert "Cheap Jacket" in prompt
    assert "price_percentile" in prompt
    assert "budget evaluator" in prompt.lower()


def test_run_evaluator_maps_scores_and_attaches_features():
    products = _products()
    feats = budget_features(products, Query(raw_text="jacket"))
    canned = ScoreList(
        scores=[
            ScoreItem(product_upid="a", score=0.9, reasons=["cheapest"]),
            ScoreItem(product_upid="b", score=0.2, reasons=["expensive"]),
        ]
    )
    agent = FakeAgent(canned)

    scores = run_evaluator(
        agent, AgentName.BUDGET, products, feats, Query(raw_text="j")
    )

    assert {s.product_upid: s.score for s in scores} == {"a": 0.9, "b": 0.2}
    assert all(s.agent is AgentName.BUDGET for s in scores)
    # the deterministic feature record is attached back for audit
    by_upid = {s.product_upid: s for s in scores}
    assert by_upid["a"].features is not None
    assert by_upid["a"].features.product_upid == "a"


def test_run_evaluator_ignores_unknown_upid():
    products = _products()
    feats = budget_features(products, Query(raw_text="jacket"))
    canned = ScoreList(
        scores=[
            ScoreItem(product_upid="a", score=0.5),
            ScoreItem(product_upid="ghost", score=1.0),  # hallucinated
        ]
    )
    scores = run_evaluator(
        FakeAgent(canned), AgentName.BUDGET, products, feats, Query(raw_text="j")
    )
    assert {s.product_upid for s in scores} == {"a"}
