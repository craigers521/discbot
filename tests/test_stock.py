"""Unit tests for botalert.stock.determine_status (task T012, research.md §6).

Strict precedence: (1) in-stock indicator, (2) out-of-stock indicator,
(3) built-in in phrases, (4) built-in out phrases, (5) unknown — all
case-insensitive substring matches; in-before-out bias on heuristic ties.
"""

from __future__ import annotations

import pytest

from botalert.models import Product, StockStatus
from botalert.stock import (
    IN_STOCK_PHRASES,
    OUT_OF_STOCK_PHRASES,
    determine_status,
)


def product(
    in_stock_indicator: str | None = None,
    out_of_stock_indicator: str | None = None,
) -> Product:
    return Product.create(
        "https://x.example.com/item",
        in_stock_indicator=in_stock_indicator,
        out_of_stock_indicator=out_of_stock_indicator,
    )


class TestPrecedence:
    def test_in_indicator_match_wins(self) -> None:
        p = product(in_stock_indicator="My Special Button")
        assert determine_status("<button>MY SPECIAL BUTTON</button>", p) is StockStatus.IN_STOCK

    def test_out_indicator_match(self) -> None:
        p = product(out_of_stock_indicator="Gone Gone Gone")
        assert determine_status("the item is gone gone gone", p) is StockStatus.OUT_OF_STOCK

    def test_in_indicator_takes_precedence_over_out_indicator(self) -> None:
        p = product(in_stock_indicator="AAA", out_of_stock_indicator="BBB")
        assert determine_status("AAA and BBB", p) is StockStatus.IN_STOCK
        assert determine_status("BBB only", p) is StockStatus.OUT_OF_STOCK

    def test_indicators_do_not_match_fall_through_to_heuristics(self) -> None:
        # Precedence, not exclusivity (research.md §6).
        p = product(in_stock_indicator="NoSuchThing")
        assert determine_status("<button>Add to Cart</button>", p) is StockStatus.IN_STOCK
        p2 = product(in_stock_indicator="NoSuchThing", out_of_stock_indicator="AlsoNo")
        assert determine_status("Sorry, sold out.", p2) is StockStatus.OUT_OF_STOCK
        p3 = product(in_stock_indicator="NoSuchThing", out_of_stock_indicator="AlsoNo")
        assert determine_status("A plain page.", p3) is StockStatus.UNKNOWN


class TestHeuristicPhraseSets:
    @pytest.mark.parametrize("phrase", sorted(IN_STOCK_PHRASES))
    def test_in_phrase_set(self, phrase: str) -> None:
        html = f"<div>we are pleased to say: {phrase.upper()}</div>"
        assert determine_status(html, product()) is StockStatus.IN_STOCK

    @pytest.mark.parametrize("phrase", sorted(OUT_OF_STOCK_PHRASES))
    def test_out_phrase_set(self, phrase: str) -> None:
        html = f"<div>{phrase.title()} — check back later</div>"
        assert determine_status(html, product()) is StockStatus.OUT_OF_STOCK

    def test_no_match_is_unknown(self) -> None:
        assert determine_status("<p>a lovely product page, nothing to see</p>", product()) is StockStatus.UNKNOWN

    def test_matching_is_substring_and_case_insensitive(self) -> None:
        assert determine_status("We are IN STOCK and ready to ship.", product()) is StockStatus.IN_STOCK
        assert determine_status("The item is currently unavailable.", product()) is StockStatus.OUT_OF_STOCK


class TestInBeforeOutBias:
    def test_page_matching_both_heuristic_sets_is_in_stock(self) -> None:
        # A size variant is sold out while the main CTA is present.
        html = (
            "<button>Add to Cart</button>"
            "<p>Some sizes are currently out of stock.</p>"
        )
        assert determine_status(html, product()) is StockStatus.IN_STOCK

    def test_provided_in_indicator_beats_matching_out_phrases(self) -> None:
        p = product(in_stock_indicator="Buy It Now")
        assert determine_status("BUY IT NOW — also sold out elsewhere", p) is StockStatus.IN_STOCK
