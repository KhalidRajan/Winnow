"""Manual smoke test: one real search against the live Global Catalog MCP.

Not part of the test suite — it hits the network and needs live access
(catalog credentials + a hosted AGENT_PROFILE_URL in .env).

    .venv/bin/python scripts/smoke_search.py "waterproof hiking jacket under $200"

Prints the raw search_catalog result and the parsed Products, so we can confirm
the last integration seams (auth body format, tool name, argument keys) against
the real endpoint.
"""

from __future__ import annotations

import json
import sys

from concierge.auth import AuthError, TokenProvider
from concierge.catalog import extract_products, query_to_arguments
from concierge.config import ConfigError, Settings
from concierge.mcp_client import McpClient, McpError
from concierge.models import Product, Query


def main() -> int:
    query_text = (
        sys.argv[1] if len(sys.argv) > 1 else "waterproof hiking jacket under $200"
    )

    try:
        settings = Settings.load()
    except ConfigError as exc:
        print(f"Config error: {exc}")
        return 1

    query = Query(raw_text=query_text)
    arguments = query_to_arguments(query)
    tokens = TokenProvider(settings)
    client = McpClient(settings, tokens)

    print(f"Endpoint:  {settings.mcp_endpoint}")
    print(f"Profile:   {settings.agent_profile_url}")
    print(f"Query:     {query_text!r}")
    print(f"Arguments: {arguments}\n")

    try:
        token = tokens.get_token()
        print(f"Auth OK — token starts {token[:10]}…\n")
    except AuthError as exc:
        print(f"AUTH FAILED: {exc}")
        print(
            "Seam to try: switch auth.py `data=` -> `json=` if the body format is wrong."
        )
        return 1

    try:
        result = client.search_catalog(arguments)
    except McpError as exc:
        print(f"MCP CALL FAILED: {exc}")
        print(
            "Seams to try: tool name ('search_catalog'), meta placement, or arg keys."
        )
        return 1

    raw = json.dumps(result, indent=2)
    dump_path = "live_search_response.json"
    with open(dump_path, "w") as fh:
        fh.write(raw)
    print(f"=== Full raw result written to {dump_path} ===")
    print(raw[:1500])

    products = [Product.from_mcp(p) for p in extract_products(result)]
    print(f"\n=== Parsed {len(products)} product(s) ===")
    for product in products[:10]:
        price = f"${product.price:.2f}" if product.price is not None else "n/a"
        rating = (
            f"{product.rating}* ({product.review_count})"
            if product.rating is not None
            else "no rating"
        )
        print(
            f"- {product.title[:55]:55}  {price:>9}  {rating:>16}  [{product.seller}]"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
