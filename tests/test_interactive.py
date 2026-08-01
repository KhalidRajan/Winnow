from concierge.enums import AgentName
from concierge.interactive import collect_query_llm
from concierge.models import IntakeState


class ScriptedIO:
    """Feeds canned answers to each prompt and captures what's written."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.written = []

    def read(self, _prompt):
        return self.answers.pop(0)

    def write(self, text):
        self.written.append(text)


class _Output:
    def __init__(self, content):
        self.content = content


class FakeIntakeAgent:
    def __init__(self, state):
        self.state = state
        self.last_prompt = None

    def run(self, prompt):
        self.last_prompt = prompt
        return _Output(self.state)


def test_collect_query_llm_loops_until_done():
    # Turn 1: agent needs more info; Turn 2: it's satisfied.
    states = iter(
        [
            IntakeState(reply="What's your budget?", done=False, product="sneakers"),
            IntakeState(
                reply="Great — searching now!",
                done=True,
                product="black leather sneakers",
                max_price=150,
                ships_to="Canada",
                priority="budget",
                color="black",
                size="10",
                gender="men",
                must_haves=["leather"],
            ),
        ]
    )
    io = ScriptedIO(
        ["I want black sneakers", "under 150, ship to Canada, budget matters"]
    )

    query = collect_query_llm(
        lambda: FakeIntakeAgent(next(states)), read=io.read, write=io.write
    )

    assert query.raw_text == "black leather sneakers"
    assert query.max_price == 150
    assert query.ships_to == "Canada"
    assert query.weights[AgentName.BUDGET] > query.weights[AgentName.LOGISTICS]
    # adaptive attributes flow into the query (and on to the search filters)
    assert query.color == "black"
    assert query.size == "10"
    assert query.gender == "men"
    assert query.keywords == ["leather"]
    # both concierge replies were shown to the shopper
    assert any("budget" in w.lower() for w in io.written)


def test_done_is_ignored_while_the_reply_still_asks_a_question():
    """Regression: the agent flagged done while still asking, so the shopper's
    final answer (their priority) was dropped and the loop searched too early."""
    states = iter(
        [
            # done=true, but the reply is still a question -> must keep listening
            IntakeState(
                reply="Does budget matter more, or availability?",
                done=True,
                product="smart glasses",
                max_price=750,
                ships_to="Canada",
            ),
            IntakeState(
                reply="Got it — searching now.",
                done=True,
                product="smart glasses",
                max_price=750,
                ships_to="Canada",
                priority="logistics",
            ),
        ]
    )
    io = ScriptedIO(["smart glasses, $750, Canada", "availability"])

    query = collect_query_llm(
        lambda: FakeIntakeAgent(next(states)), read=io.read, write=io.write
    )

    # the second answer was read, so the stated priority actually took effect
    assert query.weights[AgentName.LOGISTICS] > query.weights[AgentName.BUDGET]
    assert not io.answers  # both scripted inputs were consumed


def test_collect_query_llm_stops_at_max_turns():
    never_done = IntakeState(reply="Tell me more?", done=False, product="hat")
    io = ScriptedIO(["a hat", "blue", "warm", "wool", "cheap"])

    query = collect_query_llm(
        lambda: FakeIntakeAgent(never_done),
        read=io.read,
        write=io.write,
        max_turns=3,
    )
    # falls back to extracted product after the cap; balanced default weights
    assert query.raw_text == "hat"
    assert query.weights[AgentName.BUDGET] == query.weights[AgentName.LOGISTICS]


def test_eof_ends_the_conversation_gracefully():
    """Ctrl-D (or piped input running out) should search, not raise EOFError."""

    class EofIO(ScriptedIO):
        def read(self, _prompt):
            if not self.answers:
                raise EOFError
            return self.answers.pop(0)

    io = EofIO(["a rain jacket"])
    state = IntakeState(reply="What's your budget?", done=False, product="rain jacket")

    query = collect_query_llm(
        lambda: FakeIntakeAgent(state), read=io.read, write=io.write
    )
    assert query.raw_text == "rain jacket"


class RecordingAgent:
    """Always asks another question, and records the prompts it received."""

    def __init__(self, sink):
        self.sink = sink

    def run(self, prompt):
        self.sink.append(prompt)
        return _Output(
            IntakeState(reply="Anything else?", done=False, product="rain jacket")
        )


def test_conversation_is_capped_and_never_strands_the_shopper():
    """The agent can't keep asking forever: the last turn forces a wrap-up."""
    prompts = []
    io = ScriptedIO(["rain jacket", "$150", "Canada", "blue", "extra", "more"])

    query = collect_query_llm(
        lambda: RecordingAgent(prompts), read=io.read, write=io.write, max_turns=4
    )

    assert len(prompts) == 4  # capped, despite the agent always asking again
    # the shopper is never left answering into a closed loop
    assert len(io.answers) == 2  # only 4 of the 6 scripted lines consumed
    assert query.raw_text == "rain jacket"
    # the final turn instructs the agent to finish rather than ask again
    assert "FINAL turn" in prompts[-1]
    assert "FINAL turn" not in prompts[0]


def test_blank_input_is_recorded_as_a_skip():
    """Enter on a question means 'no preference', not an empty answer."""
    prompts = []
    io = ScriptedIO(["rain jacket", "", ""])

    collect_query_llm(
        lambda: RecordingAgent(prompts), read=io.read, write=io.write, max_turns=3
    )

    assert "skipped" in prompts[1]  # the blank turn reached the agent as a skip
