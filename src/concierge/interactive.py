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

_MAX_TURNS = 8

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
) -> Query:
    """Free-form conversational intake: chat until the concierge has enough.

    A fresh agent per turn reasons over the whole transcript and returns an
    ``IntakeState`` (its next `reply` + extracted slots + a `done` flag).
    """
    write(
        "🛍  Shopping Concierge — tell me what you're after. (Ctrl-D or 'done' to search.)\n"
    )

    transcript: list[str] = []
    first_message = ""
    state = IntakeState()
    for _ in range(max_turns):
        try:
            user_message = read("> ").strip()
        except EOFError:
            # Ctrl-D (or piped input running out): search with what we have.
            write("")
            break
        if not first_message:
            first_message = user_message
        transcript.append(f"Shopper: {user_message}")

        output = agent_factory().run(
            "\n".join(transcript) + "\n\nReply to the shopper and extract known fields."
        )
        state = output.content
        write(state.reply)
        transcript.append(f"Concierge: {state.reply}")
        # Guard: the model sometimes flags done while its reply still asks a
        # question. Breaking there would discard the shopper's next answer (and
        # strand their keystrokes in the terminal), so keep listening.
        if state.done and not _asks_a_question(state.reply):
            break

    write("\nSearching…\n")

    return _state_to_query(state, first_message)
