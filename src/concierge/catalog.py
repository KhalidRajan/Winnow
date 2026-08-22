"""High-level catalog search: Query -> search_catalog -> list[Product].

Maps our ``Query`` onto ``search_catalog`` arguments and unwraps the
``structuredContent.products`` payload into ``Product`` objects.
"""

from __future__ import annotations

from typing import Any

from concierge.constants import DEFAULT_SEARCH_LIMIT
from concierge.mcp_client import McpClient
from concierge.models import Product, Query


def query_to_arguments(
    query: Query, limit: int = DEFAULT_SEARCH_LIMIT
) -> dict[str, Any]:
    """Build ``search_catalog`` arguments from a Query.

    The search parameters nest under a ``catalog`` key (per the live request
    shape); the client adds the ``meta`` sibling. Price is intentionally *not*
    sent as a filter — the catalog exposes region/attribute filters but no price
    filter, so budget is applied downstream by the Budget agent. The individual
    filter key names remain a seam to confirm against the UCP schema.
    """
    text = query.raw_text
    if query.keywords:  # extra must-have terms sharpen, not replace, the query
        text = f"{text} {' '.join(query.keywords)}".strip()
    catalog: dict[str, Any] = {"query": text, "limit": limit}

    optional = {
        "ships_to": query.ships_to,
        "ships_from": query.ships_from,
        "condition": query.condition,
        "color": query.color,
        "gender": query.gender,
        "size": query.size,
    }
    catalog.update({key: value for key, value in optional.items() if value})
    if query.categories:
        catalog["categories"] = query.categories
    return {"catalog": catalog}


def extract_products(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the raw product dicts out of a search_catalog result envelope."""
    structured = result.get("structuredContent") or {}
    products = structured.get("products") or []
    return [p for p in products if isinstance(p, dict)]


def search(
    query: Query, client: McpClient, limit: int = DEFAULT_SEARCH_LIMIT
) -> list[Product]:
    """Run a catalog search and return normalized products."""
    result = client.search_catalog(query_to_arguments(query, limit=limit))
    return [Product.from_mcp(p) for p in extract_products(result)]
