import pytest

from concierge.consensus import combine
from concierge.enums import AgentName
from concierge.models import AgentScore, Product

BUDGET = AgentName.BUDGET
QUALITY = AgentName.QUALITY


def _product(upid):
    return Product(upid=upid, title=upid, price=100)


def _score(agent, upid, score):
    return AgentScore(agent=agent, product_upid=upid, score=score)


def _scores():
    # Product "a": budget-favored; product "b": quality-favored.
    return [
        _score(BUDGET, "a", 0.9),
        _score(QUALITY, "a", 0.1),
        _score(BUDGET, "b", 0.2),
        _score(QUALITY, "b", 0.8),
    ]


def test_weighting_toward_budget_ranks_budget_pick_first():
    products = [_product("a"), _product("b")]
    recs = combine(products, _scores(), {BUDGET: 0.8, QUALITY: 0.2})
    assert recs[0].product.upid == "a"


def test_weighting_toward_quality_reorders():
    products = [_product("a"), _product("b")]
    recs = combine(products, _scores(), {BUDGET: 0.2, QUALITY: 0.8})
    assert recs[0].product.upid == "b"


def test_tradeoff_note_on_disagreement():
    products = [_product("a")]
    scores = [_score(BUDGET, "a", 0.9), _score(QUALITY, "a", 0.1)]
    recs = combine(products, scores, {BUDGET: 0.5, QUALITY: 0.5})
    assert recs[0].tradeoffs  # non-empty: agents disagree by 0.8


def test_top_n_truncation():
    products = [_product(x) for x in "abcde"]
    scores = [_score(BUDGET, x, i / 10) for i, x in enumerate("abcde")]
    recs = combine(products, scores, {BUDGET: 1.0}, top_n=3)
    assert len(recs) == 3
    assert recs[0].product.upid == "e"  # highest score


def test_final_score_is_normalized_weighted_sum():
    products = [_product("a")]
    scores = [_score(BUDGET, "a", 0.9), _score(QUALITY, "a", 0.1)]
    # weights 3:1 normalize to 0.75/0.25 -> 0.75*0.9 + 0.25*0.1 = 0.7
    recs = combine(products, scores, {BUDGET: 3.0, QUALITY: 1.0})
    assert recs[0].final_score == pytest.approx(0.7)


def test_agent_name_coerced_from_string():
    # Passing a raw string still works — Pydantic coerces it to the enum,
    # and an unknown agent name would fail loud at validation time.
    score = _score("budget", "a", 0.5)
    assert score.agent is AgentName.BUDGET
