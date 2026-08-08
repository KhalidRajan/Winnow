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

import time
from collections import deque
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

import httpx

from concierge.config import ConfigError, Settings
from concierge.constants import CONVERSATION_TIMEOUT

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
    value: Any = (body.get("parameters") or {}).get("retry_after")
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return _DEFAULT_RETRY_AFTER


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
            if (
                response.status_code == HTTPStatus.TOO_MANY_REQUESTS
                and attempt < _MAX_RATE_LIMIT_RETRIES
            ):
                self._sleep(_retry_after(response))
                continue
            if response.status_code != HTTPStatus.OK:
                raise TelegramError(
                    f"{method} returned {response.status_code}: {response.text}"
                )
            body = response.json()
            if not body.get("ok"):
                raise TelegramError(f"{method} error: {body}")
            return body.get("result")
        raise TelegramError(
            f"{method} still rate-limited after {_MAX_RATE_LIMIT_RETRIES} retries"
        )

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": self._poll_timeout}
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload)
        return list(result or [])

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
        """Skip messages queued while the bot was offline; return the next offset."""
        updates = self.get_updates(offset=-1)
        return updates[-1]["update_id"] + 1 if updates else None

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
    ) -> None:
        self._client = client
        self._chat_id = chat_id
        self._pending = [first_message]
        self._inbox = inbox
        self._allowed = allowed
        self._timeout = timeout
        self._clock = clock

    def read(self, _prompt: str = "") -> str:
        """Return the shopper's next message (the CLI's "> " prompt is dropped)."""
        if self._pending:
            return _unskip(self._pending.pop(0))

        deadline = self._clock() + self._timeout
        while self._clock() < deadline:
            queued = self._inbox.next_message(
                self._client, self._allowed, chat_id=self._chat_id
            )
            if queued is not None:
                return _unskip(queued[1])
        # Treated by the intake loop as "search with what we have".
        raise EOFError("no reply from the shopper")

    def write(self, text: str, parse_mode: str | None = None) -> None:
        if text and text.strip():
            self._client.send_message(self._chat_id, text, parse_mode=parse_mode)

    def write_html(self, text: str) -> None:
        """Send HTML-formatted output (the rendered shortlist)."""
        self.write(text, parse_mode="HTML")


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
            self.offset = update["update_id"] + 1
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
    """Tell the shopper we failed — but never let *that* failure kill the bot."""
    try:
        session.write(f"Sorry — something went wrong: {exc}")
    except (TelegramError, httpx.HTTPError):
        pass  # can't even apologise; the next poll decides whether we're done


def run_bot(
    settings: Settings,
    handle: Any,
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
    client = client or TelegramClient(token)
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
                    f"giving up after {failures} consecutive polling failures: {exc}"
                ) from exc
            sleep(min(_BACKOFF_BASE**failures, _BACKOFF_CAP))
            continue
        failures = 0
        if queued is None:
            continue  # long-poll came back empty; go round again

        chat_id, text = queued
        session = ChatSession(client, chat_id, text, inbox, allowed)
        try:
            handle(session)
        except EOFError:
            pass  # shopper went quiet; wait for the next conversation
        except Exception as exc:  # noqa: BLE001 - one bad chat must not kill the bot
            _apologise(session, exc)
        handled += 1
