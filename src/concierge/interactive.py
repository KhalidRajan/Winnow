"""Conversational intake: a free-form chat that builds a Query from the shopper.

An intake agent asks follow-ups and extracts the slots (product, budget,
destination, priority, plus product-appropriate attributes) as the conversation
goes. The agent is injected via ``agent_factory`` and IO via ``read``/``write``,
so this module stays framework-independent and the whole chat is testable
without a model or real stdin.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from concierge.enums import AgentName
from concierge.models import IntakeState, Query

Reader = Callable[[str], str]
Writer = Callable[[str], None]
# Returns a fresh agent (with .run(str).content -> IntakeState) each call, so the
# conversation state is driven entirely by the transcript we pass, not hidden
# per-agent memory. Injectable so tests can drive the chat without a real model.
AgentFactory = Callable[[], Any]

# One opening message plus at most three follow-ups. Intake should feel brief;
# the agent is told to spend its questions on budget/destination first.
_MAX_TURNS = 4
_SKIPPED = "(skipped — no preference)"

_PRIORITY_WEIGHTS: dict[str, dict[AgentName, float]] = {
    "budget": {AgentName.BUDGET: 0.8, AgentName.LOGISTICS: 0.2},
    "logistics": {AgentName.BUDGET: 0.2, AgentName.LOGISTICS: 0.8},
    "both": {AgentName.BUDGET: 0.5, AgentName.LOGISTICS: 0.5},
}


def _priority_weights(raw: str) -> dict[AgentName, float]:
    """Map the shopper's stated priority onto agent weights."""
    key = raw.strip().lower()
    if key in ("b", "budget"):
        return dict(_PRIORITY_WEIGHTS["budget"])
    if key in ("l", "logistics"):
        return dict(_PRIORITY_WEIGHTS["logistics"])
    return dict(_PRIORITY_WEIGHTS["both"])


def _asks_a_question(reply: str) -> bool:
    """True if the concierge's reply is still soliciting an answer."""
    return reply.rstrip().endswith("?")


def _state_to_query(state: IntakeState, fallback_text: str) -> Query:
    return Query(
        raw_text=(state.product or fallback_text or "").strip(),
        max_price=state.max_price,
        ships_to=state.ships_to,
        color=state.color,
        size=state.size,
        gender=state.gender,
        keywords=state.must_haves,  # extra search terms (material, features, …)
        weights=_priority_weights(state.priority or "both"),
    )


def collect_query_llm(
    agent_factory: AgentFactory,
    read: Reader = input,
    write: Writer = print,
    max_turns: int = _MAX_TURNS,
    skip_hint: str = "(press Enter to skip any question)",
) -> Query:
    """Free-form conversational intake: chat until the concierge has enough.

    A fresh agent per turn reasons over the whole transcript and returns an
    ``IntakeState`` (its next `reply` + extracted slots + a `done` flag).

    ``skip_hint`` is caller-supplied because how you skip a question depends on
    the front-end: a terminal takes a blank line, Telegram can't send one.
    """
    write(
        "🛍  Shopping Concierge — tell me what you're after.\n"
        + (f"   {skip_hint}\n" if skip_hint else "")
    )

    transcript: list[str] = []
    first_message = ""
    state = IntakeState()
    for turn in range(max_turns):
        try:
            user_message = read("> ").strip()
        except EOFError:
            write("")  # safety net: piped input ran out / Ctrl-D
            break
        if not first_message:
            first_message = user_message
        # A blank line means "skip this one" — recorded explicitly so the agent
        # moves on instead of re-asking.
        transcript.append(f"Shopper: {user_message or _SKIPPED}")

        # On the final turn the agent must wrap up rather than ask again,
        # otherwise the shopper would answer into a closed loop.
        last_turn = turn == max_turns - 1
        instruction = (
            "This is the FINAL turn. Set done=true and reply with a short "
            "confirmation of what you'll search for. Do not ask anything."
            if last_turn
            else "Reply to the shopper and extract known fields."
        )
        output = agent_factory().run("\n".join(transcript) + "\n\n" + instruction)
        state = output.content
        write(state.reply)
        transcript.append(f"Concierge: {state.reply}")

        # Don't stop while the reply is still a question — the shopper's answer
        # would be discarded. On the last turn we stop regardless.
        if last_turn or (state.done and not _asks_a_question(state.reply)):
            break

    write("\nSearching…\n")

    return _state_to_query(state, first_message)
