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

from typing import Any

import httpx

from concierge.config import ConfigError, Settings

_API = "https://api.telegram.org"
# Telegram hard-caps a message at 4096 characters; leave room for the chunk
# suffix and any markdown we might add later.
_MAX_MESSAGE = 3900
# Seconds Telegram holds a long-poll open before returning empty.
_POLL_TIMEOUT = 50
# Give up on a silent conversation after this long (raises EOFError, which the
# intake loop already treats as "search with what we have").
_CONVERSATION_TIMEOUT = 600.0


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


class TelegramClient:
    """Minimal Bot API client — just the two calls we need."""

    def __init__(
        self,
        token: str,
        http_client: httpx.Client | None = None,
        poll_timeout: int = _POLL_TIMEOUT,
    ) -> None:
        self._base = f"{_API}/bot{token}"
        self._poll_timeout = poll_timeout
        self._client = http_client or httpx.Client(timeout=poll_timeout + 15)

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        try:
            response = self._client.post(f"{self._base}/{method}", json=payload)
        except httpx.HTTPError as exc:
            raise TelegramError(f"{method} failed: {exc}") from exc
        if response.status_code != 200:
            raise TelegramError(
                f"{method} returned {response.status_code}: {response.text}"
            )
        body = response.json()
        if not body.get("ok"):
            raise TelegramError(f"{method} error: {body}")
        return body.get("result")

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
        offset_holder: _Offset,
        allowed: frozenset[str],
        timeout: float = _CONVERSATION_TIMEOUT,
        clock: Any = None,
    ) -> None:
        self._client = client
        self._chat_id = chat_id
        self._pending = [first_message]
        self._offset = offset_holder
        self._allowed = allowed
        self._timeout = timeout
        import time

        self._clock = clock or time.monotonic

    def read(self, _prompt: str = "") -> str:
        """Return the shopper's next message (the CLI's "> " prompt is dropped)."""
        if self._pending:
            return self._pending.pop(0)

        deadline = self._clock() + self._timeout
        while self._clock() < deadline:
            for chat_id, text in self._offset.poll(self._client, self._allowed):
                if chat_id == self._chat_id:
                    return text
        # Treated by the intake loop as "search with what we have".
        raise EOFError("no reply from the shopper")

    def write(self, text: str, parse_mode: str | None = None) -> None:
        if text and text.strip():
            self._client.send_message(self._chat_id, text, parse_mode=parse_mode)

    def write_html(self, text: str) -> None:
        """Send HTML-formatted output (the rendered shortlist)."""
        self.write(text, parse_mode="HTML")


class _Offset:
    """Tracks the getUpdates offset and filters updates through the allowlist."""

    def __init__(self, value: int | None = None) -> None:
        self.value = value

    def poll(
        self, client: TelegramClient, allowed: frozenset[str]
    ) -> list[tuple[str, str]]:
        messages: list[tuple[str, str]] = []
        for update in client.get_updates(self.value):
            self.value = update["update_id"] + 1
            parsed = _message_of(update)
            if parsed is None:
                continue
            chat_id, text = parsed
            if chat_id not in allowed:
                continue  # silently ignore strangers — never confirm we're here
            messages.append((chat_id, text))
        return messages


def run_bot(
    settings: Settings,
    handle: Any,
    client: TelegramClient | None = None,
    drop_pending: bool = True,
    max_conversations: int | None = None,
) -> None:
    """Long-poll for messages and run ``handle(session)`` per conversation.

    ``handle`` receives a :class:`ChatSession` and drives the normal pipeline
    with its ``read``/``write``. ``max_conversations`` bounds the loop (tests).
    """
    token, allowed = require_telegram_settings(settings)
    client = client or TelegramClient(token)
    offset = _Offset(client.drop_pending() if drop_pending else None)

    handled = 0
    while max_conversations is None or handled < max_conversations:
        for chat_id, text in offset.poll(client, allowed):
            session = ChatSession(client, chat_id, text, offset, allowed)
            try:
                handle(session)
            except EOFError:
                pass  # shopper went quiet; wait for the next conversation
            except (TelegramError, httpx.HTTPError):
                raise  # transport is broken — don't spin
            except Exception as exc:  # noqa: BLE001 - one bad chat must not kill the bot
                session.write(f"Sorry — something went wrong: {exc}")
            handled += 1
            if max_conversations is not None and handled >= max_conversations:
                break
