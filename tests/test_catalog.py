import json
from pathlib import Path

from concierge.catalog import extract_products, query_to_arguments, search
from concierge.models import Query

_FIXTURE = Path(__file__).parent / "fixtures" / "search_response.json"


class FakeClient:
    """Stands in for McpClient, returning a canned search_catalog result."""

    def __init__(self, result):
        self.result = result
        self.last_arguments = None

    def search_catalog(self, arguments):
        self.last_arguments = arguments
        return self.result


def _fixture_result():
    return json.loads(_FIXTURE.read_text())["result"]


def test_query_to_arguments_maps_text_and_filters():
    query = Query(
        raw_text="waterproof jacket",
        ships_to="CA",
        condition="new",
        categories=["Apparel"],
    )
    catalog = query_to_arguments(query, limit=5)["catalog"]
    assert catalog["query"] == "waterproof jacket"
    assert catalog["limit"] == 5
    assert catalog["ships_to"] == "CA"
    assert catalog["condition"] == "new"
    assert catalog["categories"] == ["Apparel"]


def test_query_to_arguments_omits_empty_filters():
    catalog = query_to_arguments(Query(raw_text="jacket"))["catalog"]
    assert "ships_to" not in catalog
    assert "color" not in catalog


def test_query_to_arguments_forwards_adaptive_attributes():
    query = Query(raw_text="sneakers", color="black", size="10", gender="men")
    catalog = query_to_arguments(query)["catalog"]
    assert catalog["color"] == "black"
    assert catalog["size"] == "10"
    assert catalog["gender"] == "men"


def test_query_to_arguments_appends_keywords_to_text():
    query = Query(raw_text="jacket", keywords=["recycled", "breathable"])
    catalog = query_to_arguments(query)["catalog"]
    # must-have terms sharpen the query rather than replacing it
    assert catalog["query"] == "jacket recycled breathable"


def test_extract_products_reads_structured_content():
    products = extract_products(_fixture_result())
    assert len(products) == 3
    assert products[0]["title"].startswith("Quechua")


def test_search_returns_normalized_products():
    client = FakeClient(_fixture_result())
    products = search(Query(raw_text="waterproof jacket"), client)

    assert len(products) == 3
    assert products[0].price == 149.0  # cents -> dollars
    assert products[1].seller == "Amble Outdoors"
    # the query text was forwarded to the client under the catalog key
    assert client.last_arguments["catalog"]["query"] == "waterproof jacket"
