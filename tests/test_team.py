from concierge.models import Product, Query, ScoreItem, ScoreList
from concierge.team import run_team


class FakeOutput:
    def __init__(self, content):
        self.content = content


class FakeTeam:
    """Stands in for an Agno Team — records the prompt, returns canned scores."""

    def __init__(self, score_list):
        self.score_list = score_list
        self.prompt = None

    def run(self, prompt):
        self.prompt = prompt
        return FakeOutput(self.score_list)


def _products():
    return [
        Product(upid="a", title="Cheap", price=50),
        Product(upid="b", title="Pricey", price=300),
    ]


def test_run_team_ranks_by_synthesized_score():
    canned = ScoreList(
        scores=[
            ScoreItem(product_upid="b", score=0.3, reasons=["expensive but stocked"]),
            ScoreItem(product_upid="a", score=0.8, reasons=["great value"]),
        ]
    )
    team = FakeTeam(canned)

    recs = run_team(_products(), Query(raw_text="jacket"), team, top_n=5)

    assert [r.product.upid for r in recs] == ["a", "b"]  # sorted by score desc
    assert recs[0].final_score == 0.8
    assert recs[0].reasoning == "great value"
    assert recs[0].per_agent == []  # Team synthesizes; no per-agent breakdown
    # both domains' feature records were handed to the team
    assert '"budget"' in team.prompt and '"logistics"' in team.prompt


def test_run_team_truncates_and_ignores_unknown_upid():
    canned = ScoreList(
        scores=[
            ScoreItem(product_upid="a", score=0.5),
            ScoreItem(product_upid="ghost", score=0.99),  # not a real product
        ]
    )
    recs = run_team(_products(), Query(raw_text="x"), FakeTeam(canned), top_n=1)
    assert [r.product.upid for r in recs] == ["a"]
