"""Opt-in, real-model sanity checks for the hybrid scoring.

These call a live LLM (via the configured provider) over fixture products and
assert *tolerant* properties — never exact scores or ordering, which vary run to
run. Excluded from the default run; enable with:

    .venv/bin/python -m pytest -m eval

Skipped automatically if configuration/credentials are missing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from concierge.agents.budget import build_budget_agent, score_budget
from concierge.agents.logistics import build_logistics_agent, score_logistics
from concierge.config import ConfigError, Settings
from concierge.model_factory import build_model
from concierge.models import Product, Query

pytestmark = pytest.mark.eval

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "search_response.json"


@pytest.fixture(scope="module")
def model():
    try:
        settings = Settings.load()
    except ConfigError as exc:
        pytest.skip(f"eval needs live config: {exc}")
    return build_model(settings)


def _products():
    data = json.loads(_FIXTURE.read_text())
    raw = data["result"]["structuredContent"]["products"]
    return [Product.from_mcp(p) for p in raw]


def test_budget_scores_are_in_range_and_favor_cheaper(model):
    products = _products()
    query = Query(raw_text="waterproof jacket", max_price=200)
    scores = {
        s.product_upid: s.score
        for s in score_budget(products, query, build_budget_agent(model))
    }

    assert scores, "budget agent returned no scores"
    assert all(0.0 <= v <= 1.0 for v in scores.values())

    cheapest = min(products, key=lambda p: p.price or float("inf"))
    priciest = max(products, key=lambda p: p.price or 0.0)
    # tolerant: the cheapest should not score below the most expensive
    assert scores[cheapest.upid] >= scores[priciest.upid]


def test_logistics_scores_are_in_range(model):
    products = _products()
    scores = score_logistics(
        products, Query(raw_text="waterproof jacket"), build_logistics_agent(model)
    )
    assert scores
    assert all(0.0 <= s.score <= 1.0 for s in scores)
    # every product carries the deterministic feature record it was judged on
    assert all(s.features is not None for s in scores)
