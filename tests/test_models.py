"""Unit tests for botalert.models (task T009).

Covers Product construction, URL normalization, label/indicator
constraints, StockStatus serialization, and the CheckResult invariant.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from botalert.models import (
    CheckResult,
    Product,
    StockStatus,
    ValidationError,
    host_of,
    normalize_url,
    validate_indicator,
    validate_label,
)

_HEX12 = re.compile(r"^[0-9a-f]{12}$")


class TestProductCreate:
    def test_id_is_12_char_lowercase_hex(self) -> None:
        product = Product.create("https://shop.example.com/item")
        assert _HEX12.fullmatch(product.id)

    def test_new_product_initial_state(self) -> None:
        product = Product.create("https://shop.example.com/item")
        assert product.last_status is StockStatus.UNKNOWN
        assert product.last_checked_at is None
        assert product.consecutive_failures == 0
        assert product.failure_notified is False
        assert product.created_at.tzinfo is not None

    def test_ids_are_unique(self) -> None:
        a = Product.create("https://shop.example.com/item")
        b = Product.create("https://shop.example.com/other")
        assert a.id != b.id


class TestUrlNormalization:
    def test_strips_surrounding_whitespace(self) -> None:
        assert normalize_url("  https://shop.example.com/a  ") == (
            "https://shop.example.com/a"
        )

    def test_lowercases_scheme_and_host_keeps_path_verbatim(self) -> None:
        assert normalize_url("HTTPS://SHOP.Example.COM/Item/42") == (
            "https://shop.example.com/Item/42"
        )

    def test_query_and_fragment_kept(self) -> None:
        assert normalize_url("http://Shop.example.com/p?x=Y#top") == (
            "http://shop.example.com/p?x=Y#top"
        )

    @pytest.mark.parametrize(
        "raw",
        ["ftp://shop.example.com/item", "example.com/item", "shop.example.com", ""],
    )
    def test_rejects_non_https_or_schemeless(self, raw: str) -> None:
        with pytest.raises(ValidationError):
            normalize_url(raw)

    @pytest.mark.parametrize(
        "raw", ["http:///path", "https://", "http://?q=1", "   "]
    )
    def test_rejects_hostless_urls(self, raw: str) -> None:
        with pytest.raises(ValidationError):
            normalize_url(raw)


class TestLabel:
    def test_defaults_to_host(self) -> None:
        product = Product.create("https://shop.example.com/item")
        assert product.label == "shop.example.com"
        assert host_of("https://shop.example.com:8443/x") == "shop.example.com"

    def test_label_is_trimmed(self) -> None:
        assert validate_label("  padded  ") == "padded"
        product = Product.create("https://x.example.com/i", label="  Shoes  ")
        assert product.label == "Shoes"

    def test_enforces_1_to_100_after_trim(self) -> None:
        assert len(validate_label("x" * 100)) == 100
        with pytest.raises(ValidationError):
            validate_label("")
        with pytest.raises(ValidationError):
            validate_label("   ")
        with pytest.raises(ValidationError):
            validate_label("x" * 101)


class TestIndicators:
    def test_200_chars_ok_201_rejected(self) -> None:
        assert validate_indicator("a" * 200) == "a" * 200
        with pytest.raises(ValidationError):
            validate_indicator("a" * 201)

    def test_none_and_blank_become_none(self) -> None:
        assert validate_indicator(None) is None
        assert validate_indicator("") is None
        assert validate_indicator("   ") is None

    def test_product_keeps_valid_indicator(self) -> None:
        product = Product.create(
            "https://x.example.com/i", in_stock_indicator="Add to Cart"
        )
        assert product.in_stock_indicator == "Add to Cart"
        assert product.out_of_stock_indicator is None


class TestStockStatus:
    @pytest.mark.parametrize(
        "member,value",
        [
            (StockStatus.IN_STOCK, "in_stock"),
            (StockStatus.OUT_OF_STOCK, "out_of_stock"),
            (StockStatus.UNKNOWN, "unknown"),
        ],
    )
    def test_serialization_round_trip(self, member: StockStatus, value: str) -> None:
        assert member.value == value
        assert StockStatus(value) is member

    def test_unknown_value_rejected(self) -> None:
        with pytest.raises(ValueError):
            StockStatus("maybe")


class TestCheckResult:
    def test_error_present_with_error_outcome(self) -> None:
        result = CheckResult(
            product_id="abc",
            outcome="error",
            error="HTTP 500",
            checked_at=datetime.now(timezone.utc),
        )
        assert result.error == "HTTP 500"

    def test_error_absent_with_determined_outcome(self) -> None:
        result = CheckResult(
            product_id="abc",
            outcome=StockStatus.IN_STOCK,
            error=None,
            checked_at=datetime.now(timezone.utc),
        )
        assert result.error is None

    @pytest.mark.parametrize(
        "outcome,error",
        [
            (StockStatus.IN_STOCK, "should not have an error"),
            ("error", None),
        ],
    )
    def test_invariant_violations_raise(
        self, outcome: object, error: str | None
    ) -> None:
        with pytest.raises(ValueError):
            CheckResult(
                product_id="abc",
                outcome=outcome,  # type: ignore[arg-type]
                error=error,
                checked_at=datetime.now(timezone.utc),
            )


class TestProductDicts:
    def test_to_dict_has_state_shape(self) -> None:
        product = Product.create("https://shop.example.com/item", label="Sneakers")
        data = product.to_dict()
        assert set(data) == {
            "id",
            "url",
            "label",
            "in_stock_indicator",
            "out_of_stock_indicator",
            "last_status",
            "last_checked_at",
            "consecutive_failures",
            "failure_notified",
            "created_at",
        }
        assert data["last_status"] == "unknown"
        assert data["last_checked_at"] is None

    def test_api_dict_never_exposes_failure_notified(self) -> None:
        data = Product.create("https://shop.example.com/item").api_dict()
        assert "failure_notified" not in data
        assert set(data) == {
            "id",
            "url",
            "label",
            "in_stock_indicator",
            "out_of_stock_indicator",
            "last_status",
            "last_checked_at",
            "consecutive_failures",
            "created_at",
        }
