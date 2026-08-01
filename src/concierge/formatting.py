"""Render a ranked shortlist for the terminal."""

from __future__ import annotations

from concierge.models import Recommendation


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
