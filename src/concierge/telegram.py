"""Telegram front-end for the concierge, via Bot API long-polling.

Long-polling (not webhooks) means the process only makes *outbound* HTTPS calls,
so this runs on a laptop behind NAT with no deployment, tunnel, or open port.

Access is allowlist-only: updates from any chat outside
``TELEGRAM_ALLOWED_CHAT_IDS`` are ignored silently (a reply would confirm the bot
is live to whoever is probing). The allowlist is required — an unconfigured bot
refuses to start rather than serving, and paying for, the whole internet.

The intake loop already takes injected ``read``/``write`` callables, so a chat
session just supplies Telegram-flavoured versions; nothing else changes.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

import httpx

from concierge.config import ConfigError, Settings
from concierge.constants import CONVERSATION_TIMEOUT

_log = logging.getLogger(__name__)

_API = "https://api.telegram.org"
# Telegram hard-caps a message at 4096 characters; leave room for the chunk
# suffix and any markdown we might add later.
_MAX_MESSAGE = 3900
# Seconds Telegram holds a long-poll open before returning empty.
_POLL_TIMEOUT = 50
# You can't send an empty Telegram message, so the blank line that means "skip
# this question" in the terminal needs a word instead.
SKIP_TOKEN = "skip"
# Telegram asks us to back off via `parameters.retry_after`; obey it a few times
# before treating the throttling as fatal.
_MAX_RATE_LIMIT_RETRIES = 3
_DEFAULT_RETRY_AFTER = 1.0
# A bot that lives for days must ride out transient network failures instead of
# exiting on the first one — but still give up if the transport is truly gone.
_MAX_CONSECUTIVE_POLL_FAILURES = 5
_BACKOFF_BASE = 2.0
_BACKOFF_CAP = 60.0
# A poll that returns nothing *for us* must not re-poll at network speed. Updates
# from non-allowlisted chats come back immediately and are dropped, so without
# this pause anyone who can message the bot can spin the loop into Telegram's
# rate limiter and out through the consecutive-failure budget.
_EMPTY_POLL_PAUSE = 1.0


class TelegramError(Exception):
    """Raised when the Bot API cannot be reached or returns an error."""


def require_telegram_settings(settings: Settings) -> tuple[str, frozenset[str]]:
    """Validate Telegram config, failing loud with every missing var at once."""
    missing = []
    if not settings.telegram_bot_token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not settings.telegram_allowed_chat_ids:
        missing.append("TELEGRAM_ALLOWED_CHAT_IDS")
    if missing:
        raise ConfigError(
            "Missing Telegram configuration: "
            + ", ".join(missing)
            + ". TELEGRAM_ALLOWED_CHAT_IDS is required so the bot only answers "
            "you — message the bot, then read message.chat.id from "
            "https://api.telegram.org/bot<TOKEN>/getUpdates"
        )
    assert settings.telegram_bot_token is not None
    return settings.telegram_bot_token, frozenset(settings.telegram_allowed_chat_ids)


def chunk(text: str, limit: int = _MAX_MESSAGE) -> list[str]:
    """Split text into Telegram-sized pieces, preferring line boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # a single very long line
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()]


def _retry_after(response: httpx.Response) -> float:
    """Seconds Telegram wants us to wait, from a 429 body's ``retry_after``."""
    try:
        body = response.json()
    except ValueError:
        return _DEFAULT_RETRY_AFTER
    if not isinstance(body, dict):
        return _DEFAULT_RETRY_AFTER
    value: Any = (body.get("parameters") or {}).get("retry_after")
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return _DEFAULT_RETRY_AFTER


def _backoff_delay(failures: int) -> float:
    """Exponential backoff for consecutive poll failures, capped."""
    return min(_BACKOFF_BASE**failures, _BACKOFF_CAP)


def _update_id(update: dict[str, Any]) -> int | None:
    """``update_id`` when it is the integer Telegram promises, else ``None``.

    Same reasoning as ``_message_of`` one screen down: the offset arithmetic is
    the one place a missing or oddly-typed key would raise past both poll loops
    instead of counting as a poll failure.
    """
    update_id = update.get("update_id")
    return update_id if isinstance(update_id, int) else None


class TelegramClient:
    """Minimal Bot API client — just the two calls we need."""

    def __init__(
        self,
        token: str,
        http_client: httpx.Client | None = None,
        poll_timeout: int = _POLL_TIMEOUT,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base = f"{_API}/bot{token}"
        self._poll_timeout = poll_timeout
        self._client = http_client or httpx.Client(timeout=poll_timeout + 15)
        self._sleep = sleep

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        # Rate limiting is routine (Telegram throttles bursts of sendMessage
        # chunks), so honour retry_after rather than surfacing it as an error.
        for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
            try:
                response = self._client.post(f"{self._base}/{method}", json=payload)
            except httpx.HTTPError as exc:
                raise TelegramError(f"{method} failed: {exc}") from exc
            if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
                # Exhaustion has to leave by the loop's own exit, or the final
                # 429 falls through to the generic status check below and the
                # operator is told "returned 429" instead of that we gave up
                # retrying.
                if attempt == _MAX_RATE_LIMIT_RETRIES:
                    break
                self._sleep(_retry_after(response))
                continue
            if response.status_code != HTTPStatus.OK:
                raise TelegramError(
                    f"{method} returned {response.status_code}: {response.text}"
                )
            try:
                body = response.json()
            except ValueError as exc:
                # A 200 that isn't JSON is a captive portal or an intercepting
                # proxy — exactly what a laptop bot meets. It must arrive as a
                # TelegramError: the poll loops retry on that, whereas the bare
                # ValueError this raises escapes both their except tuples and
                # kills a process meant to run for days. Deliberately not
                # quoting the body, which is typically a whole HTML page.
                raise TelegramError(f"{method} returned a non-JSON body") from exc
            if not isinstance(body, dict):
                raise TelegramError(
                    f"{method} returned {type(body).__name__}, not a JSON object"
                )
            if not body.get("ok"):
                raise TelegramError(f"{method} error: {body}")
            return body.get("result")
        raise TelegramError(
            f"{method} still rate-limited after {_MAX_RATE_LIMIT_RETRIES} retries"
        )

    def get_updates(
        self, offset: int | None, timeout: int | None = None
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": self._poll_timeout if timeout is None else timeout
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload)
        if result is None:
            return []
        # The declared return type is a claim about someone else's payload, so
        # check it here rather than letting a surprise shape reach _Inbox as an
        # AttributeError/TypeError that no poll loop catches.
        if not isinstance(result, list):
            raise TelegramError(
                f"getUpdates returned {type(result).__name__}, not a list"
            )
        return [update for update in result if isinstance(update, dict)]

    def send_message(
        self, chat_id: str, text: str, parse_mode: str | None = None
    ) -> None:
        for piece in chunk(text):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": piece}
            if parse_mode:
                payload["parse_mode"] = parse_mode
                # Otherwise Telegram appends a big preview card for the first
                # link, which crowds out the rest of the shortlist.
                payload["link_preview_options"] = {"is_disabled": True}
            self._call("sendMessage", payload)

    def drop_pending(self) -> int | None:
        """Skip messages queued while the bot was offline; return the next offset.

        ``timeout=0`` makes this a non-blocking drain. A long-poll here would
        hold startup open for ``_POLL_TIMEOUT`` seconds *after* the caller has
        announced the bot is running, and would consume — then discard — the
        first message the shopper sends in that window.
        """
        updates = self.get_updates(offset=-1, timeout=0)
        for update in reversed(updates):
            update_id = _update_id(update)
            if update_id is not None:
                return update_id + 1
        return None

    def close(self) -> None:
        self._client.close()


def _unskip(text: str) -> str:
    """Map the "skip" keyword onto the blank line the intake loop treats as a skip."""
    return "" if text.strip().lower() == SKIP_TOKEN else text


def _message_of(update: dict[str, Any]) -> tuple[str, str] | None:
    """Extract (chat_id, text) from an update, or None if it isn't a text message."""
    message = update.get("message") or update.get("edited_message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    text = message.get("text")
    if chat_id is None or not text:
        return None
    return str(chat_id), str(text)


class ChatSession:
    """Binds one chat to the ``read``/``write`` callables the intake loop wants."""

    def __init__(
        self,
        client: TelegramClient,
        chat_id: str,
        first_message: str,
        inbox: _Inbox,
        allowed: frozenset[str],
        timeout: float = CONVERSATION_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._chat_id = chat_id
        self._pending = [first_message]
        self._inbox = inbox
        self._allowed = allowed
        self._timeout = timeout
        self._clock = clock
        self._sleep = sleep

    def read(self, _prompt: str = "") -> str:
        """Return the shopper's next message (the CLI's "> " prompt is dropped)."""
        if self._pending:
            return _unskip(self._pending.pop(0))

        deadline = self._clock() + self._timeout
        failures = 0
        while self._clock() < deadline:
            try:
                queued = self._inbox.next_message(
                    self._client, self._allowed, chat_id=self._chat_id
                )
            except (TelegramError, httpx.HTTPError):
                # Same reasoning as run_bot's outer loop, and it matters more
                # here: dropping this poll discards a conversation already part
                # way through intake, not just an idle wait.
                failures += 1
                if failures >= _MAX_CONSECUTIVE_POLL_FAILURES:
                    raise
                self._sleep(_backoff_delay(failures))
                continue
            failures = 0
            if queued is not None:
                return _unskip(queued[1])
            # Nothing for *this* chat. The same pause run_bot takes, and for the
            # same reason: a non-allowlisted chat's updates return immediately
            # and are dropped, so without this the loop re-polls at network
            # speed for as long as a stranger keeps sending. See
            # _EMPTY_POLL_PAUSE.
            self._sleep(_EMPTY_POLL_PAUSE)
        # Treated by the intake loop as "search with what we have".
        raise EOFError("no reply from the shopper")

    def _send(self, text: str, parse_mode: str | None = None) -> None:
        if text and text.strip():
            self._client.send_message(self._chat_id, text, parse_mode=parse_mode)

    def write(self, text: str) -> None:
        """Send plain text. Signature matches ``interactive.Writer`` exactly."""
        self._send(text)

    def write_html(self, text: str) -> None:
        """Send HTML-formatted output (the rendered shortlist)."""
        self._send(text, parse_mode="HTML")


class _Inbox:
    """Tracks the getUpdates offset and queues what the caller didn't ask for.

    ``getUpdates`` drains messages for *every* allowed chat at once and advances
    the offset irreversibly, so a poller that keeps only the messages it wanted
    silently destroys the rest. Both the outer loop and :meth:`ChatSession.read`
    therefore draw from this one queue: a batch of "hi" + "waterproof jacket"
    reaches the same conversation in order, and a second allowed chat's messages
    wait their turn instead of vanishing.
    """

    def __init__(self, offset: int | None = None) -> None:
        self.offset = offset
        self._queue: deque[tuple[str, str]] = deque()

    def _fill(self, client: TelegramClient, allowed: frozenset[str]) -> None:
        for update in client.get_updates(self.offset):
            update_id = _update_id(update)
            if update_id is None:
                continue
            self.offset = update_id + 1
            parsed = _message_of(update)
            if parsed is None:
                continue
            chat_id, text = parsed
            if chat_id not in allowed:
                continue  # silently ignore strangers — never confirm we're here
            self._queue.append((chat_id, text))

    def _take(self, chat_id: str | None) -> tuple[str, str] | None:
        if chat_id is None:
            return self._queue.popleft() if self._queue else None
        for index, entry in enumerate(self._queue):
            if entry[0] == chat_id:
                del self._queue[index]
                return entry
        return None

    def next_message(
        self,
        client: TelegramClient,
        allowed: frozenset[str],
        chat_id: str | None = None,
    ) -> tuple[str, str] | None:
        """Next queued message — for ``chat_id``, or any chat — polling if empty."""
        queued = self._take(chat_id)
        if queued is not None:
            return queued
        self._fill(client, allowed)
        return self._take(chat_id)


def _apologise(session: ChatSession, exc: Exception) -> None:
    """Tell the shopper we failed — but never let *that* failure kill the bot.

    The detail stays in the local log. Anything raised by the pipeline can end
    up here, and some of it carries the upstream response body — an AuthError
    quotes the token endpoint verbatim — which a chat message would copy into
    Telegram's history for good.
    """
    _log.exception("conversation failed", exc_info=exc)
    try:
        session.write("Sorry — something went wrong. Please try again.")
    except (TelegramError, httpx.HTTPError):
        pass  # can't even apologise; the next poll decides whether we're done


def run_bot(
    settings: Settings,
    handle: Callable[[ChatSession], None],
    client: TelegramClient | None = None,
    drop_pending: bool = True,
    max_conversations: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Long-poll for messages and run ``handle(session)`` per conversation.

    ``handle`` receives a :class:`ChatSession` and drives the normal pipeline
    with its ``read``/``write``. ``max_conversations`` bounds the loop (tests).

    A transient network blip must not end a bot that's meant to run for days, so
    polling failures are retried with exponential backoff and only become fatal
    after ``_MAX_CONSECUTIVE_POLL_FAILURES`` in a row.
    """
    token, allowed = require_telegram_settings(settings)
    # Only a client we created is ours to close; an injected one is the caller's.
    owned_client = client is None
    client = client or TelegramClient(token)
    try:
        inbox = _Inbox(client.drop_pending() if drop_pending else None)

        handled = 0
        failures = 0
        while max_conversations is None or handled < max_conversations:
            try:
                queued = inbox.next_message(client, allowed)
            except (TelegramError, httpx.HTTPError) as exc:
                failures += 1
                if failures >= _MAX_CONSECUTIVE_POLL_FAILURES:
                    raise TelegramError(
                        f"giving up after {failures} consecutive polling "
                        f"failures: {exc}"
                    ) from exc
                sleep(_backoff_delay(failures))
                continue
            failures = 0
            if queued is None:
                # Nothing for an allowlisted chat. Pause before polling again;
                # see _EMPTY_POLL_PAUSE for why this must not be a bare continue.
                sleep(_EMPTY_POLL_PAUSE)
                continue

            chat_id, text = queued
            session = ChatSession(client, chat_id, text, inbox, allowed, sleep=sleep)
            try:
                handle(session)
            except EOFError:
                # Defensive only: collect_query_llm catches the read deadline's
                # EOFError itself and searches with the slots it has, so this
                # does not fire for the current handler.
                pass
            except Exception as exc:  # noqa: BLE001 - one bad chat can't kill the bot
                _apologise(session, exc)
            handled += 1
    finally:
        if owned_client:
            client.close()
