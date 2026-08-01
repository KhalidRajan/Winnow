from concierge.enums import PriceTier
from concierge.features import budget_features, quality_features
from concierge.models import Product, Query


def _product(upid, price=None, rating=None, reviews=None):
    return Product(
        upid=upid, title=upid, price=price, rating=rating, review_count=reviews
    )


def _by_upid(features):
    return {f.product_upid: f for f in features}


def test_budget_percentiles_and_set_stats():
    products = [
        _product("a", 145),
        _product("b", 189),
        _product("c", 210),
        _product("d", 340),
    ]
    feats = _by_upid(budget_features(products, Query(raw_text="x", max_price=200)))

    assert feats["a"].price_percentile == 0.0  # cheapest
    assert feats["d"].price_percentile == 1.0  # priciest
    assert feats["a"].set_min_price == 145
    assert feats["d"].set_max_price == 340
    assert feats["c"].set_median_price == (189 + 210) / 2


def test_budget_over_budget_flag():
    products = [_product("a", 145), _product("d", 340)]
    feats = _by_upid(budget_features(products, Query(raw_text="x", max_price=200)))
    assert feats["a"].over_budget is False
    assert feats["d"].over_budget is True


def test_budget_tiers_from_percentile():
    products = [
        _product("a", 145),
        _product("b", 189),
        _product("c", 210),
        _product("d", 340),
    ]
    feats = _by_upid(budget_features(products, Query(raw_text="x")))
    assert feats["a"].price_tier is PriceTier.LOW
    assert feats["b"].price_tier is PriceTier.MEDIUM
    assert feats["d"].price_tier is PriceTier.HIGH


def test_bayesian_rating_shrinks_low_review_products_more():
    products = [
        _product("hi", price=100, rating=5.0, reviews=500),
        _product("lo", price=100, rating=5.0, reviews=1),
        _product("mid", price=100, rating=3.0, reviews=200),
    ]
    feats = _by_upid(quality_features(products, Query(raw_text="x")))
    # Same 5-star rating, but the 1-review product is pulled harder toward the mean.
    assert feats["hi"].bayesian_rating > feats["lo"].bayesian_rating


def test_no_rating_is_flagged():
    products = [_product("rated", rating=4.0, reviews=50), _product("unrated")]
    feats = _by_upid(quality_features(products, Query(raw_text="x")))
    assert feats["rated"].has_rating is True
    assert feats["unrated"].has_rating is False


def test_empty_and_single_product_sets():
    query = Query(raw_text="x")
    assert budget_features([], query) == []

    single = budget_features([_product("a", 100)], query)
    assert single[0].price_percentile == 0.0
    assert single[0].set_median_price == 100


def _product_with_options(upid, availability=None, sizes=None, colors=None):
    from concierge.enums import Availability

    options = {}
    if sizes is not None:
        options["Size"] = sizes
    if colors is not None:
        options["Colour"] = colors  # British spelling, exercises case/variant match
    return Product(
        upid=upid,
        title=upid,
        availability=Availability.AVAILABLE
        if availability
        else Availability.UNAVAILABLE,
        options=options,
    )


def test_logistics_in_stock_and_assortment():
    from concierge.features import logistics_features

    products = [
        _product_with_options(
            "a", availability=True, sizes=["s", "m", "l"], colors=["red"]
        ),
        _product_with_options("b", availability=False, sizes=["m"]),
    ]
    feats = {
        f.product_upid: f for f in logistics_features(products, Query(raw_text="x"))
    }
    assert feats["a"].in_stock is True
    assert feats["a"].size_count == 3
    assert feats["a"].color_count == 1  # matched "Colour"
    assert feats["b"].in_stock is False
    assert feats["b"].color_count == 0


def test_logistics_ships_to_applied_flag():
    from concierge.features import logistics_features

    without = logistics_features(
        [_product_with_options("a", True)], Query(raw_text="x")
    )
    withq = logistics_features(
        [_product_with_options("a", True)], Query(raw_text="x", ships_to="CA")
    )
    assert without[0].ships_to_applied is False
    assert withq[0].ships_to_applied is True


def test_logistics_requested_size_availability():
    from concierge.features import logistics_features

    products = [
        _product_with_options("has", availability=True, sizes=["S", "M", "L"]),
        _product_with_options("lacks", availability=True, sizes=["XS", "S"]),
    ]
    # no size requested -> None
    none_q = logistics_features(products, Query(raw_text="x"))
    assert all(f.requested_size_available is None for f in none_q)

    # size "m" requested (case-insensitive match against "M")
    feats = {
        f.product_upid: f
        for f in logistics_features(products, Query(raw_text="x", size="m"))
    }
    assert feats["has"].requested_size_available is True
    assert feats["lacks"].requested_size_available is False


def test_requested_size_matches_word_and_abbreviation_forms():
    from concierge.features import logistics_features

    # Catalog writes "M"; shopper says "medium" / "men's medium".
    products = [_product_with_options("a", availability=True, sizes=["S", "M", "L"])]
    for requested in ("medium", "M", "men's medium", "Med"):
        feats = logistics_features(products, Query(raw_text="x", size=requested))
        assert feats[0].requested_size_available is True, requested

    # A size genuinely not offered still reads False.
    feats = logistics_features(products, Query(raw_text="x", size="XXL"))
    assert feats[0].requested_size_available is False


def test_requested_size_unknown_when_product_lists_no_sizes():
    from concierge.features import logistics_features

    # No size options at all -> None ("can't tell"), never False.
    products = [_product_with_options("a", availability=True, colors=["red"])]
    feats = logistics_features(products, Query(raw_text="x", size="M"))
    assert feats[0].requested_size_available is None
