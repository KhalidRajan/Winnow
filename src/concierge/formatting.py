"""Render a ranked shortlist — for the terminal, or for Telegram."""

from __future__ import annotations

import html

from concierge.enums import AgentName
from concierge.models import Recommendation

# Telegram renders a proportional font, so terminal tricks (block bars, padded
# columns) turn into noise. Use icons and short lines instead.
# Telegram caps a message at 4096 characters, and `chunk()` only guarantees a
# safe split *between* lines — each rendered line is a self-contained element,
# but a single line longer than the cap gets cut at a raw offset, which can land
# inside a tag or an entity and get the whole message rejected. So every
# free-text field is bounded *after* escaping: `html.escape` expands by up to 6x
# (`'` -> `&#x27;`), so clipping the raw string leaves the rendered line
# unbounded, which is how a 700-char field became a 4210-char line.
_MAX_FIELD = 700
# An HTML character reference runs from "&" to ";". Truncating between the two
# leaves a fragment Telegram rejects for the whole message, so the clip backs off
# past an unterminated one.
_ENTITY_START = "&"
_ENTITY_END = ";"


def _clip_escaped(text: str, limit: int = _MAX_FIELD) -> str:
    """HTML-escape ``text``, then bound the *escaped* length.

    Truncating escaped text is what makes this fiddly: cutting at a raw offset
    can leave a half-written entity (`&#x2`), which Telegram rejects for the
    whole message. So back off to before the trailing unterminated `&`.
    """
    escaped = html.escape(text)
    if len(escaped) <= limit:
        return escaped
    cut = escaped[: limit - 1]
    entity = cut.rfind(_ENTITY_START)
    if entity != -1 and _ENTITY_END not in cut[entity:]:
        cut = cut[:entity]
    return cut.rstrip() + "…"


def _is_web_url(url: str | None) -> bool:
    """True for the only two schemes Telegram will render as a product link."""
    return url is not None and url.lower().startswith(("http://", "https://"))


_AGENT_ICON = {
    AgentName.BUDGET: "💰",
    AgentName.LOGISTICS: "📦",
    AgentName.QUALITY: "⭐",
    AgentName.SUSTAINABILITY: "🌱",
}


def _price(rec: Recommendation) -> str:
    product = rec.product
    if product.price is None:
        return "price n/a"
    currency = product.currency or "USD"
    return f"{product.price:,.2f} {currency}"


def _bar(score: float, width: int = 10) -> str:
    filled = round(score * width)
    return "█" * filled + "·" * (width - filled)


def render(recommendations: list[Recommendation]) -> str:
    if not recommendations:
        return "No products matched your search."

    lines: list[str] = []
    for rank, rec in enumerate(recommendations, start=1):
        product = rec.product
        lines.append(f"{rank}. {product.title}")
        lines.append(
            f"   {_price(rec)}   final score {rec.final_score:.2f}  "
            f"{_bar(rec.final_score)}"
        )
        for score in rec.per_agent:
            lines.append(
                f"     {score.agent.value:<9} {score.score:.2f} {_bar(score.score, 8)}"
            )
        if rec.reasoning:
            lines.append(f"   → {rec.reasoning}")
        for tradeoff in rec.tradeoffs:
            lines.append(f"   ⚖ {tradeoff}")
        if product.url:
            lines.append(f"   {product.url}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _reasons_by_agent(rec: Recommendation) -> list[tuple[AgentName, str]]:
    return [
        (score.agent, "; ".join(score.reasons))
        for score in rec.per_agent
        if score.reasons
    ]


def render_telegram(recommendations: list[Recommendation]) -> str:
    """Render for Telegram (``parse_mode=HTML``).

    The title becomes a tappable link instead of a wrapped raw URL, scores go on
    one compact line, and each agent's reasoning gets an icon — all of which read
    far better in a chat bubble than the terminal's aligned columns.
    """
    if not recommendations:
        return "No products matched your search."

    blocks: list[str] = []
    for rank, rec in enumerate(recommendations, start=1):
        product = rec.product
        title = _clip_escaped(product.title)
        # A clipped URL would be a broken link, so an over-long one drops the
        # wrapper entirely rather than being truncated. Measured after escaping,
        # for the same reason the fields are.
        #
        # The scheme is checked first because escaping only stops attribute
        # breakout: it does not stop the catalog handing us a scheme Telegram
        # refuses, which 400s the *whole* message and costs the entire
        # shortlist, nor one it renders as a link to somewhere the title does
        # not describe. Same degradation as the length guard — fall back to
        # plain text.
        usable_url = product.url if _is_web_url(product.url) else ""
        escaped_url = html.escape(usable_url, quote=True) if usable_url else ""
        heading = (
            f'<a href="{escaped_url}">{title}</a>'
            if escaped_url and len(escaped_url) <= _MAX_FIELD
            else title
        )
        lines = [
            f"<b>{rank}. {heading}</b>",
            f"{_clip_escaped(_price(rec))} · score {rec.final_score:.2f}",
        ]

        scores = " · ".join(
            f"{_AGENT_ICON.get(s.agent, '•')} {s.agent.value} {s.score:.2f}"
            for s in rec.per_agent
        )
        if scores:
            lines.append(scores)
        for agent, reason in _reasons_by_agent(rec):
            lines.append(f"{_AGENT_ICON.get(agent, '•')} {_clip_escaped(reason)}")
        # The consensus/team synthesis — the one line that explains the ranking
        # rather than any single agent's view.
        if rec.reasoning:
            lines.append(f"→ <i>{_clip_escaped(rec.reasoning)}</i>")
        for tradeoff in rec.tradeoffs:
            lines.append(f"⚖️ <i>{_clip_escaped(tradeoff)}</i>")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)
