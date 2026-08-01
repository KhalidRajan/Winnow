import json
from pathlib import Path

from concierge.enums import Availability
from concierge.models import Product

_FIXTURE = Path(__file__).parent / "fixtures" / "search_response.json"


def _fixture_products():
    data = json.loads(_FIXTURE.read_text())
    return data["result"]["structuredContent"]["products"]


def _by_upid(raw_products):
    products = [Product.from_mcp(p) for p in raw_products]
    return {p.upid: p for p in products}


def test_from_mcp_parses_price_from_cents():
    products = _by_upid(_fixture_products())
    quechua = products["gid://shopify/p/6jRQHV9BCbA6lQhslgl92g"]
    # price_range.min.amount is 14900 cents -> $149.00
    assert quechua.price == 149.0
    assert quechua.currency == "USD"


def test_from_mcp_prefers_product_level_rating():
    products = _by_upid(_fixture_products())
    refuge = products["gid://shopify/p/5nFYvM52R4QOU8uMkR0kmj"]
    # Product-level rating (count 6265) wins over the variant's (count 213).
    assert refuge.rating == 4.9
    assert refuge.review_count == 6265


def test_from_mcp_falls_back_to_variant_rating():
    products = _by_upid(_fixture_products())
    quechua = products["gid://shopify/p/6jRQHV9BCbA6lQhslgl92g"]
    # No product-level rating -> use the variant's (5.0, count 7).
    assert quechua.rating == 5.0
    assert quechua.review_count == 7


def test_from_mcp_handles_missing_rating():
    products = _by_upid(_fixture_products())
    highlander = products["gid://shopify/p/3BxA1jTVYlFtQDToj05VEh"]
    assert highlander.rating is None
    assert highlander.review_count is None


def test_from_mcp_reads_variant_level_fields():
    products = _by_upid(_fixture_products())
    quechua = products["gid://shopify/p/6jRQHV9BCbA6lQhslgl92g"]
    assert quechua.seller == "Decathlon"
    assert quechua.condition == "new"
    assert quechua.availability is Availability.AVAILABLE
    assert quechua.url.startswith("https://www.decathlon.com/")


def test_from_mcp_strips_seller_whitespace():
    products = _by_upid(_fixture_products())
    highlander = products["gid://shopify/p/3BxA1jTVYlFtQDToj05VEh"]
    assert highlander.seller == "Preppers Shop UK"  # leading space trimmed


def test_from_mcp_tolerates_sparse_dict():
    product = Product.from_mcp({"id": "gid://x", "title": "Bare"})
    assert product.upid == "gid://x"
    assert product.price is None
    assert product.rating is None
    assert product.availability is None
    assert product.ships_to == []


def test_from_mcp_lean_live_shape():
    """The real search_catalog omits rating/seller/condition but includes
    options + availability. Parser returns None for the absent fields."""
    lean = {
        "id": "gid://shopify/p/live1",
        "title": "Live Jacket",
        "options": [
            {
                "name": "Size",
                "values": [{"label": "s"}, {"label": "m"}, {"label": "l"}],
            },
            {"name": "Colour", "values": [{"label": "Black"}]},
        ],
        "variants": [
            {
                "id": "gid://shopify/ProductVariant/1",
                "url": "https://shop.example/p/1",
                "price": {"amount": 9900, "currency": "USD"},
                "availability": {"available": True},
            }
        ],
        "price_range": {"min": {"amount": 9900, "currency": "USD"}},
    }
    product = Product.from_mcp(lean)
    assert product.price == 99.0
    assert product.rating is None  # not returned by live search
    assert product.seller is None  # not returned by live search
    assert product.condition is None
    assert product.availability is Availability.AVAILABLE
    assert product.options["Size"] == ["s", "m", "l"]
    assert product.options["Colour"] == ["Black"]
