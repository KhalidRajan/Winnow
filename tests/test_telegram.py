import pytest

from concierge.config import ConfigError, Settings
from concierge.telegram import (
    ChatSession,
    _Offset,
    chunk,
    require_telegram_settings,
    run_bot,
)

ALLOWED = "111"
STRANGER = "999"


def _settings(token="tok", chat_ids=(ALLOWED,)):
    return Settings(
        shopify_client_id="cid",
        shopify_client_secret="secret",
        agent_profile_url="https://example.com/profile.json",
        llm_api_key="key",
        telegram_bot_token=token,
        telegram_allowed_chat_ids=chat_ids,
    )


def _update(update_id, chat_id, text):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


class FakeClient:
    """Stands in for TelegramClient: scripted update batches, captured sends."""

    def __init__(self, batches=()):
        self.batches = list(batches)
        self.sent = []
        self.offsets = []
        self.parse_modes = []

    def get_updates(self, offset):
        self.offsets.append(offset)
        return self.batches.pop(0) if self.batches else []

    def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))
        self.parse_modes.append(parse_mode)

    def drop_pending(self):
        return None


# --- config ---


def test_require_settings_reports_every_missing_var():
    with pytest.raises(ConfigError) as exc:
        require_telegram_settings(_settings(token=None, chat_ids=()))
    message = str(exc.value)
    assert "TELEGRAM_BOT_TOKEN" in message
    assert "TELEGRAM_ALLOWED_CHAT_IDS" in message


def test_require_settings_rejects_empty_allowlist():
    """An unconfigured allowlist must refuse to start, not serve everyone."""
    with pytest.raises(ConfigError):
        require_telegram_settings(_settings(chat_ids=()))


def test_require_settings_returns_token_and_allowlist():
    token, allowed = require_telegram_settings(_settings())
    assert token == "tok"
    assert allowed == frozenset({ALLOWED})


# --- allowlist ---


def test_stranger_messages_are_ignored_silently():
    client = FakeClient([[_update(1, STRANGER, "hello?"), _update(2, ALLOWED, "hi")]])
    messages = _Offset().poll(client, frozenset({ALLOWED}))

    assert messages == [(ALLOWED, "hi")]  # stranger dropped
    assert client.sent == []  # and never replied to


def test_offset_advances_past_every_update():
    client = FakeClient([[_update(7, STRANGER, "x"), _update(8, ALLOWED, "y")]])
    offset = _Offset()
    offset.poll(client, frozenset({ALLOWED}))
    # advances past the stranger too, so it isn't re-fetched forever
    assert offset.value == 9


def test_non_text_updates_are_skipped():
    client = FakeClient([[{"update_id": 1, "message": {"chat": {"id": ALLOWED}}}]])
    assert _Offset().poll(client, frozenset({ALLOWED})) == []


# --- chunking ---


def test_short_text_is_one_chunk():
    assert chunk("hello", limit=100) == ["hello"]


def test_long_text_splits_on_line_boundaries():
    text = "\n".join(f"line {i}" for i in range(100))
    pieces = chunk(text, limit=50)
    assert len(pieces) > 1
    assert all(len(p) <= 50 for p in pieces)
    assert "".join(pieces) == text  # nothing lost


def test_single_overlong_line_is_hard_split():
    pieces = chunk("x" * 250, limit=100)
    assert [len(p) for p in pieces] == [100, 100, 50]


# --- session ---


def test_session_replays_first_message_then_polls():
    client = FakeClient([[_update(2, ALLOWED, "second")]])
    session = ChatSession(client, ALLOWED, "first", _Offset(), frozenset({ALLOWED}))

    assert session.read() == "first"  # the message that opened the chat
    assert session.read() == "second"  # then long-polls


def test_session_write_sends_and_skips_blanks():
    client = FakeClient()
    session = ChatSession(client, ALLOWED, "hi", _Offset(), frozenset({ALLOWED}))

    session.write("hello")
    session.write("   ")  # the intake loop writes blanks on EOF
    assert client.sent == [(ALLOWED, "hello")]


def test_session_read_times_out_as_eof():
    """A shopper who goes quiet ends intake via the existing EOF path."""
    ticks = iter([0.0, 0.0, 999.0])
    session = ChatSession(
        FakeClient(),
        ALLOWED,
        "hi",
        _Offset(),
        frozenset({ALLOWED}),
        timeout=10.0,
        clock=lambda: next(ticks),
    )
    session.read()  # consumes the seeded first message
    with pytest.raises(EOFError):
        session.read()


# --- bot loop ---


def test_run_bot_hands_each_conversation_to_the_handler():
    client = FakeClient([[_update(1, ALLOWED, "I need a jacket")]])
    seen = []

    def handle(session):
        seen.append(session.read())
        session.write("on it")

    run_bot(_settings(), handle, client=client, drop_pending=False, max_conversations=1)

    assert seen == ["I need a jacket"]
    assert client.sent == [(ALLOWED, "on it")]


def test_run_bot_survives_a_failing_conversation():
    client = FakeClient([[_update(1, ALLOWED, "boom")]])

    def handle(session):
        raise ValueError("scoring blew up")

    run_bot(_settings(), handle, client=client, drop_pending=False, max_conversations=1)

    # the shopper is told, and the bot stays alive
    assert client.sent and "went wrong" in client.sent[0][1]


# --- telegram rendering ---


def _recommendation(title, url, price, tradeoffs=()):
    from concierge.enums import AgentName
    from concierge.models import AgentScore, Product, Recommendation

    return Recommendation(
        product=Product(upid="a", title=title, price=price, currency="USD", url=url),
        final_score=0.82,
        per_agent=[
            AgentScore(
                agent=AgentName.BUDGET,
                product_upid="a",
                score=1.0,
                reasons=["cheapest"],
            ),
            AgentScore(
                agent=AgentName.LOGISTICS,
                product_upid="a",
                score=0.65,
                reasons=["in stock"],
            ),
        ],
        tradeoffs=list(tradeoffs),
    )


def test_telegram_render_links_the_title_instead_of_dumping_the_url():
    from concierge.formatting import render_telegram

    out = render_telegram(
        [_recommendation("Oud Candle", "https://shop/p?a=1&b=2", 25.5)]
    )
    assert '<a href="https://shop/p?a=1&amp;b=2">Oud Candle</a>' in out
    assert "\nhttps://shop" not in out  # no bare URL on its own line
    assert "█" not in out  # no terminal bars


def test_telegram_render_escapes_html_in_titles_and_reasons():
    from concierge.formatting import render_telegram

    out = render_telegram([_recommendation("Tom & Jerry <b>", "https://x", 10.0)])
    assert "Tom &amp; Jerry &lt;b&gt;" in out


def test_telegram_render_includes_scores_and_tradeoffs():
    from concierge.formatting import render_telegram

    out = render_telegram(
        [_recommendation("X", "https://x", 10.0, ["budget vs logistics"])]
    )
    assert "budget 1.00" in out and "logistics 0.65" in out
    assert "⚖️" in out and "budget vs logistics" in out


def test_write_html_uses_html_parse_mode():
    client = FakeClient()
    session = ChatSession(client, ALLOWED, "hi", _Offset(), frozenset({ALLOWED}))

    session.write_html("<b>hi</b>")
    session.write("plain")

    assert client.parse_modes == ["HTML", None]
