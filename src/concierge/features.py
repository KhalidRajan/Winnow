"""Deterministic feature extraction — the reproducible facts the agents reason over.

Pure functions: no network, no LLM, no framework. Fully unit-testable.
"""

from __future__ import annotations

import re
from statistics import fmean, median

from concierge.enums import Availability, PriceTier
from concierge.models import (
    BudgetFeatures,
    LogisticsFeatures,
    Product,
    QualityFeatures,
    Query,
)

# Prior weight for the Bayesian rating shrinkage. Larger -> ratings with few
# reviews are pulled harder toward the set mean.
_BAYES_PRIOR_WEIGHT = 20.0


def _percentile(value: float | None, population: list[float | None]) -> float | None:
    """Fraction of the population strictly below ``value``, in ``[0, 1]``.

    Lowest value -> 0.0, highest -> 1.0. Returns ``None`` when ``value`` is
    missing. With one (or zero) comparable values, returns 0.0.
    """
    if value is None:
        return None
    present = [v for v in population if v is not None]
    if len(present) <= 1:
        return 0.0
    below = sum(1 for v in present if v < value)
    return below / (len(present) - 1)


def _tier(percentile: float | None) -> PriceTier:
    if percentile is None:
        return PriceTier.UNKNOWN
    if percentile < 1 / 3:
        return PriceTier.LOW
    if percentile < 2 / 3:
        return PriceTier.MEDIUM
    return PriceTier.HIGH


def budget_features(products: list[Product], query: Query) -> list[BudgetFeatures]:
    """Compute per-product price facts across the result set."""
    prices = [p.price for p in products]
    present = [p for p in prices if p is not None]
    set_min = min(present) if present else None
    set_max = max(present) if present else None
    set_median = median(present) if present else None

    features: list[BudgetFeatures] = []
    for product in products:
        percentile = _percentile(product.price, prices)
        over_budget = bool(
            query.max_price is not None
            and product.price is not None
            and product.price > query.max_price
        )
        features.append(
            BudgetFeatures(
                product_upid=product.upid,
                price=product.price,
                price_percentile=percentile,
                over_budget=over_budget,
                price_tier=_tier(percentile),
                set_median_price=set_median,
                set_min_price=set_min,
                set_max_price=set_max,
            )
        )
    return features


def quality_features(products: list[Product], query: Query) -> list[QualityFeatures]:
    """Compute per-product rating/review facts, incl. a Bayesian-shrunk rating."""
    ratings = [p.rating for p in products]
    review_counts: list[float | None] = [
        float(p.review_count) if p.review_count is not None else None for p in products
    ]
    rated = [r for r in ratings if r is not None]
    set_mean = fmean(rated) if rated else None

    features: list[QualityFeatures] = []
    for product in products:
        rating = product.rating
        has_rating = rating is not None
        bayesian: float | None
        if rating is not None and set_mean is not None:
            v = float(product.review_count or 0)
            bayesian = (v / (v + _BAYES_PRIOR_WEIGHT)) * rating + (
                _BAYES_PRIOR_WEIGHT / (v + _BAYES_PRIOR_WEIGHT)
            ) * set_mean
        else:
            # No rating -> fall fully back to the prior (set mean), which is
            # below any well-reviewed product and flagged via has_rating.
            bayesian = set_mean

        features.append(
            QualityFeatures(
                product_upid=product.upid,
                rating=product.rating,
                review_count=product.review_count,
                has_rating=has_rating,
                bayesian_rating=bayesian,
                rating_percentile=_percentile(product.rating, ratings),
                review_count_percentile=_percentile(
                    float(product.review_count)
                    if product.review_count is not None
                    else None,
                    review_counts,
                ),
                set_mean_rating=set_mean,
            )
        )
    return features


def _option_values(product: Product, *names: str) -> list[str]:
    """Values for an option, matching the option name case-insensitively."""
    wanted = {n.lower() for n in names}
    for name, values in product.options.items():
        if name.lower() in wanted:
            return values
    return []


# Catalogs write sizes as "M" / "Medium" / "m", shoppers say "medium" — compare
# canonical forms so a real match isn't missed (which would wrongly mark every
# product as "your size unavailable").
_SIZE_ALIASES: dict[str, set[str]] = {
    "xs": {"xs", "xsmall", "extrasmall"},
    "s": {"s", "small"},
    "m": {"m", "med", "medium"},
    "l": {"l", "large"},
    "xl": {"xl", "xlarge", "extralarge"},
    "2xl": {"2xl", "xxl", "xxlarge", "2x"},
    "3xl": {"3xl", "xxxl", "3x"},
}


def _canonical_size(raw: str) -> str:
    key = re.sub(r"[^a-z0-9]", "", raw.lower())
    for canonical, aliases in _SIZE_ALIASES.items():
        if key in aliases:
            return canonical
    return key


def _requested_size_forms(requested: str) -> set[str]:
    """Canonical forms to match on, incl. each token ("men's medium" -> m)."""
    forms = {_canonical_size(requested)}
    forms.update(_canonical_size(tok) for tok in re.split(r"[\s,/]+", requested) if tok)
    return {f for f in forms if f}


def _size_available(product: Product, requested: str | None) -> bool | None:
    """True/False if the shopper's size is offered; None when it can't be judged."""
    if not requested:
        return None
    offered = _option_values(product, "size")
    if not offered:
        return None  # no size data at all — unknown, not "unavailable"
    wanted = _requested_size_forms(requested)
    return any(_canonical_size(value) in wanted for value in offered)


def logistics_features(
    products: list[Product], query: Query
) -> list[LogisticsFeatures]:
    """Compute per-product availability/fulfillment facts.

    In-stock acts as a floor (search results are usually all in stock); the
    size/color assortment is the real differentiator. When the shopper named a
    size, ``requested_size_available`` makes that personal.
    """
    ships_to_applied = query.ships_to is not None
    return [
        LogisticsFeatures(
            product_upid=product.upid,
            in_stock=product.availability == Availability.AVAILABLE,
            size_count=len(_option_values(product, "size")),
            color_count=len(_option_values(product, "color", "colour")),
            ships_to_applied=ships_to_applied,
            requested_size_available=_size_available(product, query.size),
        )
        for product in products
    ]
