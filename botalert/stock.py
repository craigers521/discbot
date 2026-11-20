"""Stock-status determination (research.md §6).

Strict precedence, all case-insensitive substring matches against the
fetched page text: (1) the product's in-stock indicator, (2) the
product's out-of-stock indicator, (3) the built-in in-stock phrase set,
(4) the built-in out-of-stock phrase set, (5) ``UNKNOWN``. Provided
indicators that do not match fall through to the heuristics (precedence,
not exclusivity); a page matching both heuristic sets is in-stock
("in-before-out bias"). No per-retailer plugins, no regex, no HTML parsing.
"""

from __future__ import annotations

import logging
from typing import Final

from botalert.models import Product, StockStatus

logger = logging.getLogger(__name__)

#: Built-in in-stock phrase set (frozen data table, research.md §6).
IN_STOCK_PHRASES: Final[frozenset[str]] = frozenset(
    {
        "add to cart",
        "add to basket",
        "buy now",
        "in stock",
        "available now",
    }
)

#: Built-in out-of-stock phrase set (frozen data table, research.md §6).
OUT_OF_STOCK_PHRASES: Final[frozenset[str]] = frozenset(
    {
        "out of stock",
        "sold out",
        "notify me",
        "currently unavailable",
        "unavailable",
    }
)


def determine_status(html: str, product: Product) -> StockStatus:
    """Determine a product's stock status from fetched page text.

    ``html`` is the raw page text (any markup is irrelevant — matching is
    plain substring). Returns the first status whose indicator or phrase
    matches, per the strict precedence above; ``UNKNOWN`` when nothing
    matches.
    """
    text = html.lower()
    in_indicator = product.in_stock_indicator
    if in_indicator is not None and in_indicator.lower() in text:
        logger.debug(
            "determine_status: matched product in-stock indicator %r",
            in_indicator,
        )
        return StockStatus.IN_STOCK
    out_indicator = product.out_of_stock_indicator
    if out_indicator is not None and out_indicator.lower() in text:
        logger.debug(
            "determine_status: matched product out-of-stock indicator %r",
            out_indicator,
        )
        return StockStatus.OUT_OF_STOCK
    for phrase in IN_STOCK_PHRASES:
        if phrase in text:
            logger.debug("determine_status: matched built-in phrase %r", phrase)
            return StockStatus.IN_STOCK
    for phrase in OUT_OF_STOCK_PHRASES:
        if phrase in text:
            logger.debug("determine_status: matched built-in phrase %r", phrase)
            return StockStatus.OUT_OF_STOCK
    logger.debug(
        "determine_status: no indicator or built-in phrase matched; UNKNOWN"
    )
    return StockStatus.UNKNOWN


__all__ = [
    "IN_STOCK_PHRASES",
    "OUT_OF_STOCK_PHRASES",
    "determine_status",
]
