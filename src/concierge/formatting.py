"""Render a ranked shortlist — for the terminal, or for Telegram."""

from __future__ import annotations

import html

from concierge.enums import AgentName
from concierge.models import Recommendation

# Telegram renders a proportional font, so terminal tricks (block bars, padded
# columns) turn into noise. Use icons and short lines instead.
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
            f"   {_price(rec)}   final score {rec.final_score:.2f}  {_bar(rec.final_score)}"
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
        title = html.escape(product.title)
        heading = (
            f'<a href="{html.escape(product.url, quote=True)}">{title}</a>'
            if product.url
            else title
        )
        lines = [
            f"<b>{rank}. {heading}</b>",
            f"{_price(rec)} · score {rec.final_score:.2f}",
        ]

        scores = " · ".join(
            f"{_AGENT_ICON.get(s.agent, '•')} {s.agent.value} {s.score:.2f}"
            for s in rec.per_agent
        )
        if scores:
            lines.append(scores)
        for agent, reason in _reasons_by_agent(rec):
            lines.append(f"{_AGENT_ICON.get(agent, '•')} {html.escape(reason)}")
        # The consensus/team synthesis — the one line that explains the ranking
        # rather than any single agent's view.
        if rec.reasoning:
            lines.append(f"→ <i>{html.escape(rec.reasoning)}</i>")
        for tradeoff in rec.tradeoffs:
            lines.append(f"⚖️ <i>{html.escape(tradeoff)}</i>")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)
