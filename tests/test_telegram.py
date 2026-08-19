import pytest

from concierge.config import ConfigError, Settings
from concierge.telegram import (
    ChatSession,
    TelegramError,
    _Inbox,
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
    inbox = _Inbox()
    allowed = frozenset({ALLOWED})

    assert inbox.next_message(client, allowed) == (ALLOWED, "hi")  # stranger dropped
    assert inbox.next_message(client, allowed) is None  # nothing else queued
    assert client.sent == []  # and never replied to


def test_offset_advances_past_every_update():
    client = FakeClient([[_update(7, STRANGER, "x"), _update(8, ALLOWED, "y")]])
    inbox = _Inbox()
    inbox.next_message(client, frozenset({ALLOWED}))
    # advances past the stranger too, so it isn't re-fetched forever
    assert inbox.offset == 9


def test_non_text_updates_are_skipped():
    client = FakeClient([[{"update_id": 1, "message": {"chat": {"id": ALLOWED}}}]])
    assert _Inbox().next_message(client, frozenset({ALLOWED})) is None


def test_inbox_keeps_messages_it_was_not_asked_for():
    """A poll for one chat must not destroy another chat's messages."""
    client = FakeClient([[_update(1, STRANGER, "mine"), _update(2, ALLOWED, "yours")]])
    inbox = _Inbox()
    allowed = frozenset({ALLOWED, STRANGER})

    # ask for ALLOWED first — STRANGER's message arrived in the same batch
    assert inbox.next_message(client, allowed, chat_id=ALLOWED) == (ALLOWED, "yours")
    # ...and is still there afterwards, without another poll
    assert inbox.next_message(client, allowed, chat_id=STRANGER) == (STRANGER, "mine")


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
    session = ChatSession(client, ALLOWED, "first", _Inbox(), frozenset({ALLOWED}))

    assert session.read() == "first"  # the message that opened the chat
    assert session.read() == "second"  # then long-polls


def test_session_write_sends_and_skips_blanks():
    client = FakeClient()
    session = ChatSession(client, ALLOWED, "hi", _Inbox(), frozenset({ALLOWED}))

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
        _Inbox(),
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


def test_a_burst_of_messages_reaches_one_conversation():
    """ "hi" then "waterproof jacket" in one batch must not become two chats."""
    client = FakeClient(
        [[_update(1, ALLOWED, "hi"), _update(2, ALLOWED, "waterproof jacket")]]
    )
    seen = []

    def handle(session):
        seen.append(session.read())
        seen.append(session.read())  # the rest of the burst, not a fresh poll

    run_bot(_settings(), handle, client=client, drop_pending=False, max_conversations=1)

    assert seen == ["hi", "waterproof jacket"]


def test_a_read_leaves_another_chats_message_queued():
    """A poll inside one conversation used to silently discard other chats."""
    allowed = frozenset({ALLOWED, STRANGER})
    inbox = _Inbox()
    client = FakeClient([[_update(2, STRANGER, "me too")]])
    ticks = iter([0.0, 0.0, 999.0])
    session = ChatSession(
        client,
        ALLOWED,
        "hi",
        inbox,
        allowed,
        timeout=10.0,
        clock=lambda: next(ticks),
    )

    assert session.read() == "hi"
    with pytest.raises(EOFError):
        session.read()  # polls, finds only STRANGER's message, gives up waiting

    # ...and STRANGER's message survived that poll, ready for the next conversation
    assert inbox.next_message(client, allowed) == (STRANGER, "me too")


def test_a_transient_poll_failure_is_retried_not_fatal():
    class FlakyClient(FakeClient):
        def __init__(self):
            super().__init__([[_update(1, ALLOWED, "hi")]])
            self.attempts = 0

        def get_updates(self, offset):
            self.attempts += 1
            if self.attempts == 1:
                raise TelegramError("connection reset")
            return super().get_updates(offset)

    client = FlakyClient()
    seen = []
    run_bot(
        _settings(),
        lambda s: seen.append(s.read()),
        client=client,
        drop_pending=False,
        max_conversations=1,
        sleep=lambda _seconds: None,
    )

    assert seen == ["hi"]  # rode out the blip


def test_a_dead_transport_eventually_gives_up():
    class DeadClient(FakeClient):
        def get_updates(self, offset):
            raise TelegramError("network unreachable")

    with pytest.raises(TelegramError, match="consecutive polling failures"):
        run_bot(
            _settings(),
            lambda s: None,
            client=DeadClient(),
            drop_pending=False,
            sleep=lambda _seconds: None,
        )


def test_a_failing_apology_does_not_kill_the_bot():
    """If the error reply itself fails to send, keep serving."""

    class UnsendableClient(FakeClient):
        def send_message(self, chat_id, text, parse_mode=None):
            raise TelegramError("sendMessage returned 403")

    client = UnsendableClient([[_update(1, ALLOWED, "boom")]])

    def handle(session):
        raise ValueError("scoring blew up")

    # must return normally rather than propagating the TelegramError
    run_bot(_settings(), handle, client=client, drop_pending=False, max_conversations=1)


def test_skip_token_reads_as_the_blank_line_intake_expects():
    client = FakeClient([[_update(2, ALLOWED, "SKIP ")]])
    session = ChatSession(client, ALLOWED, "hi", _Inbox(), frozenset({ALLOWED}))

    assert session.read() == "hi"
    assert session.read() == ""  # intake treats blank as "skip this question"


# --- telegram rendering ---


def _recommendation(title, url, price, tradeoffs=(), reasoning="", currency="USD"):
    from concierge.enums import AgentName
    from concierge.models import AgentScore, Product, Recommendation

    return Recommendation(
        product=Product(upid="a", title=title, price=price, currency=currency, url=url),
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
        reasoning=reasoning,
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


def test_telegram_render_includes_the_consensus_reasoning():
    """The synthesis line is the most valuable one — the terminal prints it too."""
    from concierge.formatting import render_telegram

    out = render_telegram(
        [_recommendation("X", "https://x", 10.0, reasoning="Best value & in stock")]
    )
    assert "Best value &amp; in stock" in out


def test_write_html_uses_html_parse_mode():
    client = FakeClient()
    session = ChatSession(client, ALLOWED, "hi", _Inbox(), frozenset({ALLOWED}))

    session.write_html("<b>hi</b>")
    session.write("plain")

    assert client.parse_modes == ["HTML", None]


# --- regressions found in review ---


def test_apology_never_repeats_the_exception_text():
    """Pipeline errors quote upstream response bodies; a chat message is forever."""
    client = FakeClient([[_update(1, ALLOWED, "hi")]])

    def handle(session):
        raise RuntimeError("Token response missing 'access_token': {'secret': 'oops'}")

    run_bot(_settings(), handle, client=client, drop_pending=False, max_conversations=1)

    apology = client.sent[-1][1]
    assert "access_token" not in apology and "oops" not in apology
    assert "went wrong" in apology


def test_session_read_rides_out_a_transient_poll_failure():
    """A blip mid-intake must not discard a conversation already under way."""

    class FlakyClient(FakeClient):
        def get_updates(self, offset):
            if not self.offsets:  # fail the first poll only
                self.offsets.append(offset)
                raise TelegramError("getUpdates failed: connection reset")
            return super().get_updates(offset)

    client = FlakyClient([[_update(2, ALLOWED, "a waterproof one")]])
    slept = []
    session = ChatSession(
        client,
        ALLOWED,
        "hi",
        _Inbox(),
        frozenset({ALLOWED}),
        sleep=slept.append,
    )

    assert session.read() == "hi"
    assert session.read() == "a waterproof one"
    assert slept  # backed off rather than aborting the conversation


def test_session_read_gives_up_after_persistent_poll_failures():
    class DeadClient(FakeClient):
        def get_updates(self, offset):
            raise TelegramError("getUpdates failed: connection reset")

    session = ChatSession(
        DeadClient(),
        ALLOWED,
        "hi",
        _Inbox(),
        frozenset({ALLOWED}),
        sleep=lambda _: None,
    )
    session.read()
    with pytest.raises(TelegramError):
        session.read()


def test_telegram_render_escapes_the_catalog_supplied_currency():
    from concierge.formatting import render_telegram

    out = render_telegram([_recommendation("X", "https://x", 10.0, currency="<b>USD")])
    assert "&lt;b&gt;USD" in out and "<b>USD" not in out


def test_telegram_render_keeps_every_line_chunkable():
    """No rendered line may exceed the chunk limit, or a split lands mid-tag."""
    from concierge.formatting import render_telegram
    from concierge.telegram import _MAX_MESSAGE, chunk

    out = render_telegram(
        [
            _recommendation(
                "T" * 5000,
                "https://x/" + "u" * 5000,
                10.0,
                tradeoffs=["W" * 5000],
                reasoning="R" * 5000,
            )
        ]
    )
    assert all(len(line) <= _MAX_MESSAGE for line in out.splitlines())
    # every chunk boundary therefore falls between complete elements
    assert all(piece.count("<b>") == piece.count("</b>") for piece in chunk(out))
