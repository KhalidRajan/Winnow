"""Domain models shared across the pipeline (framework-independent Pydantic)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from concierge.enums import AgentName, Availability, PriceTier


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _maybe_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _minor_to_major(amount: Any) -> float | None:
    """Convert an integer minor-unit amount (cents) to major units (dollars)."""
    value = _to_float(amount)
    return None if value is None else value / 100.0


def _availability(obj: Any) -> Availability | None:
    """Map a variant ``availability`` object to our enum."""
    if not isinstance(obj, dict):
        return None
    available = obj.get("available")
    if available is True:
        return Availability.AVAILABLE
    if available is False:
        return Availability.UNAVAILABLE
    return None


class Query(BaseModel):
    """A parsed shopping request."""

    raw_text: str
    max_price: float | None = None
    ships_to: str | None = None
    ships_from: str | None = None
    condition: str | None = None
    min_rating: float | None = None
    categories: list[str] = Field(default_factory=list)
    price_tier: str | None = None
    color: str | None = None
    gender: str | None = None
    size: str | None = None
    keywords: list[str] = Field(default_factory=list)
    weights: dict[AgentName, float] = Field(
        default_factory=lambda: {AgentName.BUDGET: 0.5, AgentName.LOGISTICS: 0.5}
    )


class Product(BaseModel):
    """A catalog product, normalized from an MCP search result."""

    upid: str
    product_id: str | None = None
    title: str
    price: float | None = None
    currency: str | None = None
    rating: float | None = None
    review_count: int | None = None
    condition: str | None = None
    ships_to: list[str] = Field(default_factory=list)
    ships_from: str | None = None
    availability: Availability | None = None
    seller: str | None = None
    url: str | None = None
    image_url: str | None = None
    # Option name -> available values, e.g. {"Size": ["s", "m", "l"]}. Used by
    # the Logistics agent as an assortment/availability signal.
    options: dict[str, list[str]] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_mcp(cls, data: dict[str, Any]) -> Product:
        """Parse a Global Catalog ``search_catalog`` product entry.

        Products are UPID-clustered with a ``variants`` list; price/seller/url/
        condition/availability live on the first variant, while rating may be at
        product *or* variant level (or absent). Prices are integer minor units
        (cents). Fields are read tolerantly and the original dict is preserved in
        ``raw``. Confirmed against ``tests/fixtures/search_response.json``.
        """
        variants = data.get("variants") or []
        variant: dict[str, Any] = variants[0] if variants else {}

        # Price + currency: prefer the product's "from" price, else the variant's.
        price_min = (data.get("price_range") or {}).get("min") or {}
        variant_price = variant.get("price") or {}
        amount = price_min.get("amount")
        if amount is None:
            amount = variant_price.get("amount")
        currency = price_min.get("currency") or variant_price.get("currency")

        # Rating: product-level aggregate preferred, else the variant's.
        rating_obj = data.get("rating") or variant.get("rating") or {}

        seller = (variant.get("seller") or {}).get("name")
        if isinstance(seller, str):
            seller = seller.strip() or None

        conditions = variant.get("condition") or []
        condition = str(conditions[0]) if conditions else None

        media = data.get("media") or variant.get("media") or []
        image_url = media[0].get("url") if media else None

        options: dict[str, list[str]] = {}
        for opt in data.get("options") or []:
            name = opt.get("name")
            values = [
                str(v["label"])
                for v in (opt.get("values") or [])
                if isinstance(v, dict) and v.get("label") is not None
            ]
            if name:
                options[str(name)] = values

        return cls(
            upid=str(data.get("id", "")),
            product_id=_maybe_str(variant.get("id")),
            title=str(data.get("title", "")),
            price=_minor_to_major(amount),
            currency=_maybe_str(currency),
            rating=_to_float(rating_obj.get("value")),
            review_count=_to_int(rating_obj.get("count")),
            condition=condition,
            # Region is a search input, not returned per-product by search_catalog.
            ships_to=[],
            ships_from=None,
            availability=_availability(variant.get("availability")),
            seller=seller,
            url=_maybe_str(variant.get("url")),
            image_url=_maybe_str(image_url),
            options=options,
            raw=data,
        )


class FeatureRecord(BaseModel):
    """Base for per-domain deterministic feature records (all carry a upid)."""

    product_upid: str


class BudgetFeatures(FeatureRecord):
    """Deterministic price facts the Budget agent reasons over."""

    price: float | None = None
    price_percentile: float | None = None
    over_budget: bool = False
    price_tier: PriceTier = PriceTier.UNKNOWN
    set_median_price: float | None = None
    set_min_price: float | None = None
    set_max_price: float | None = None


class QualityFeatures(FeatureRecord):
    """Deterministic rating/review facts the Quality agent reasons over."""

    rating: float | None = None
    review_count: int | None = None
    has_rating: bool = False
    bayesian_rating: float | None = None
    rating_percentile: float | None = None
    review_count_percentile: float | None = None
    set_mean_rating: float | None = None


class LogisticsFeatures(FeatureRecord):
    """Deterministic availability/fulfillment facts the Logistics agent weighs.

    ``search_catalog`` reliably returns stock status and the option assortment;
    it does not return per-product shipping, so ``ships_to_applied`` records
    whether the buyer's region was enforced as a search filter.
    """

    in_stock: bool = False
    size_count: int = 0
    color_count: int = 0
    ships_to_applied: bool = False
    # None when the shopper didn't specify a size; else whether that size is in
    # this product's option list — makes assortment *personal*.
    requested_size_available: bool | None = None


class ScoreItem(BaseModel):
    """The lightweight schema the LLM fills — score only, no features echoed back.

    ``score`` carries no ge/le constraint on purpose: some providers reject
    JSON-schema ``minimum``/``maximum`` on structured-output number fields, so we
    clamp to [0, 1] in code (see ``agents.base.run_evaluator``) instead.
    """

    product_upid: str
    score: float
    reasons: list[str] = Field(default_factory=list)


class ScoreList(BaseModel):
    """Wrapper for the LLM's structured output (a bare list can't be a schema)."""

    scores: list[ScoreItem] = Field(default_factory=list)


class IntakeState(BaseModel):
    """The concierge's structured read of the intake conversation so far.

    Doubles as the turn's control signal: ``reply`` is what to say next, ``done``
    flips true once enough is known to search, and the rest are extracted slots.
    """

    reply: str = ""
    done: bool = False
    product: str | None = None
    max_price: float | None = None
    ships_to: str | None = None
    priority: str | None = None  # "budget" | "logistics" | "both"
    # Attributes only relevant to some products (apparel/footwear); the agent
    # populates them only when it actually asked. Left None otherwise.
    color: str | None = None
    size: str | None = None
    gender: str | None = None
    # Free-form product-specific requirements (e.g. "recycled", "USB-C").
    must_haves: list[str] = Field(default_factory=list)


class AgentScore(BaseModel):
    """A single agent's verdict on a product, with the facts it was based on."""

    agent: AgentName
    product_upid: str
    score: float
    reasons: list[str] = Field(default_factory=list)
    features: FeatureRecord | None = None


class Recommendation(BaseModel):
    """A ranked product with its per-agent scores and combined reasoning."""

    product: Product
    final_score: float
    per_agent: list[AgentScore] = Field(default_factory=list)
    reasoning: str = ""
    tradeoffs: list[str] = Field(default_factory=list)
