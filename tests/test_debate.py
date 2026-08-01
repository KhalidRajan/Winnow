from concierge.debate import contested_upids, run_debate
from concierge.enums import AgentName
from concierge.models import AgentScore

BUDGET = AgentName.BUDGET
LOGISTICS = AgentName.LOGISTICS


def _s(agent, upid, score):
    return AgentScore(agent=agent, product_upid=upid, score=score)


def test_contested_detects_wide_spread():
    scores = [
        _s(BUDGET, "a", 0.9),
        _s(LOGISTICS, "a", 0.1),  # spread 0.8 -> contested
        _s(BUDGET, "b", 0.6),
        _s(LOGISTICS, "b", 0.55),  # spread 0.05 -> not contested
    ]
    assert contested_upids(scores) == {"a"}


def _converging_reviser(step):
    """Each round nudges the agent's contested scores toward 0.5 by `step`."""

    def reviser(own, all_scores, contested):
        out = []
        for s in own:
            if s.product_upid in contested:
                delta = step if s.score < 0.5 else -step
                out.append(_s(s.agent, s.product_upid, s.score + delta))
            else:
                out.append(s)
        return out

    return reviser


def test_debate_converges_and_narrows_spread():
    scores = [_s(BUDGET, "a", 0.9), _s(LOGISTICS, "a", 0.1)]
    revisers = {BUDGET: _converging_reviser(0.2), LOGISTICS: _converging_reviser(0.2)}

    result = run_debate(scores, revisers, rounds=5)

    spread = max(s.score for s in result) - min(s.score for s in result)
    assert spread < 0.8  # agents moved closer
    assert contested_upids(result) == set()  # settled below threshold


def test_debate_stops_when_nothing_contested():
    scores = [_s(BUDGET, "a", 0.6), _s(LOGISTICS, "a", 0.55)]
    calls = {"n": 0}

    def counting_reviser(own, all_scores, contested):
        calls["n"] += 1
        return own

    run_debate(scores, {BUDGET: counting_reviser}, rounds=3)
    assert calls["n"] == 0  # never entered a round; already converged


def test_no_movement_converges_immediately():
    scores = [_s(BUDGET, "a", 1.0), _s(LOGISTICS, "a", 0.0)]
    calls = {"n": 0}

    def noop(own, all_scores, contested):
        calls["n"] += 1
        return own

    run_debate(scores, {BUDGET: noop, LOGISTICS: noop}, rounds=5)
    # one round runs, but zero movement -> convergence break (no endless looping)
    assert calls["n"] == 2


def test_debate_respects_round_cap_when_agents_keep_moving():
    scores = [_s(BUDGET, "a", 1.0), _s(LOGISTICS, "a", 0.0)]
    calls = {"n": 0}

    def oscillate(own, all_scores, contested):
        calls["n"] += 1
        return [
            _s(s.agent, s.product_upid, 1.0 - s.score)
            if s.product_upid in contested
            else s
            for s in own
        ]

    run_debate(scores, {BUDGET: oscillate, LOGISTICS: oscillate}, rounds=2)
    assert calls["n"] == 4  # 2 agents x 2 rounds, never converges
