"""Typed domain models for botalert (data-model.md §2–§4).

Pure data + field constraints. No I/O, no state-file knowledge: validation
helpers here are the single definition of the constraints quoted in
data-model.md §2 and are shared by the state store and the API.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Final, Literal
from urllib.parse import urlsplit

#: Schema version of the durable state file (fixed in v1).
SCHEMA_VERSION: Final[int] = 1


class ValidationError(Exception):
    """A field value failed its data-model.md §2 constraint.

    ``field`` names the offending field ("url" | "label" | "indicator")
    so the API layer can map it to the contract error code.
    """

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class DuplicateUrlError(Exception):
    """A product with this normalized URL is already monitored."""


class StockStatus(str, Enum):
    """Last determined stock status of a product."""

    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN = "unknown"


def normalize_url(raw: str) -> str:
    """Normalize a product URL per data-model.md §2.

    Strips surrounding whitespace; requires an absolute ``http://`` or
    ``https://`` URL with a non-empty host; lowercases scheme and host
    (the netloc as a whole) and keeps the path (plus any query/fragment)
    verbatim. The result is the product's unique key.

    Raises ValidationError for non-http(s) schemes or hostless URLs.
    """
    url = raw.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValidationError(
            f"url must be an absolute http:// or https:// URL: {raw!r}", field="url"
        )
    if not parts.netloc:
        raise ValidationError(
            f"url must have a non-empty host: {raw!r}", field="url"
        )
    normalized = f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path}"
    if parts.query:
        normalized += f"?{parts.query}"
    if parts.fragment:
        normalized += f"#{parts.fragment}"
    return normalized


def host_of(url: str) -> str:
    """Return the lowercased host of a normalized URL (no port/userinfo)."""
    return (urlsplit(url).hostname or "").lower()


def validate_label(label: str) -> str:
    """Trim a label and enforce 1–100 chars after trim (data-model.md §2)."""
    trimmed = label.strip()
    if not 1 <= len(trimmed) <= 100:
        raise ValidationError(
            "label must be 1–100 characters after trim", field="label"
        )
    return trimmed


def validate_indicator(indicator: str | None) -> str | None:
    """Enforce ≤200 chars on an indicator; None or blank means 'not set'.

    Indicators are trimmed; an empty-string indicator clears it (the PATCH
    contract's "empty-string indicator CLEARS it" rule).
    """
    if indicator is None:
        return None
    trimmed = indicator.strip()
    if len(trimmed) > 200:
        raise ValidationError(
            "indicator must be at most 200 characters", field="indicator"
        )
    return trimmed or None


@dataclass
class Product:
    """A monitored product page (data-model.md §2). One per normalized URL."""

    id: str
    url: str
    label: str
    in_stock_indicator: str | None
    out_of_stock_indicator: str | None
    last_status: StockStatus
    last_checked_at: datetime | None
    consecutive_failures: int
    failure_notified: bool
    created_at: datetime

    @classmethod
    def create(
        cls,
        url: str,
        label: str | None = None,
        in_stock_indicator: str | None = None,
        out_of_stock_indicator: str | None = None,
    ) -> Product:
        """Create a new product (data-model.md §2 'Source' column).

        id: 12-char lowercase hex (uuid4().hex[:12]), immutable.
        url: normalized per normalize_url — the unique key.
        label: trimmed, 1–100 chars; defaults to the URL's host.
        Indicators: ≤200 chars or None.
        last_status: unknown. last_checked_at: None until the first
        completed check. consecutive_failures: 0. failure_notified: False.
        created_at: UTC now.
        """
        normalized = normalize_url(url)
        return cls(
            id=uuid.uuid4().hex[:12],
            url=normalized,
            label=validate_label(label) if label is not None else host_of(normalized),
            in_stock_indicator=validate_indicator(in_stock_indicator),
            out_of_stock_indicator=validate_indicator(out_of_stock_indicator),
            last_status=StockStatus.UNKNOWN,
            last_checked_at=None,
            consecutive_failures=0,
            failure_notified=False,
            created_at=datetime.now(timezone.utc),
        )

    def to_dict(self) -> dict[str, object]:
        """State-file representation (data-model.md §7), full fields."""
        return {
            "id": self.id,
            "url": self.url,
            "label": self.label,
            "in_stock_indicator": self.in_stock_indicator,
            "out_of_stock_indicator": self.out_of_stock_indicator,
            "last_status": self.last_status.value,
            "last_checked_at": (
                self.last_checked_at.isoformat() if self.last_checked_at else None
            ),
            "consecutive_failures": self.consecutive_failures,
            "failure_notified": self.failure_notified,
            "created_at": self.created_at.isoformat(),
        }

    def api_dict(self) -> dict[str, object]:
        """Contract product representation — never exposes failure_notified."""
        data = self.to_dict()
        data.pop("failure_notified")
        return data


#: Check outcome: a determined StockStatus, or the literal "error".
CheckOutcome = StockStatus | Literal["error"]


@dataclass(frozen=True)
class CheckResult:
    """One completed check of one product (data-model.md §4, transient)."""

    product_id: str
    outcome: CheckOutcome
    error: str | None
    checked_at: datetime

    def __post_init__(self) -> None:
        # Invariant: error present iff outcome is "error".
        if (self.error is not None) != (self.outcome == "error"):
            raise ValueError("error must be present iff outcome is 'error'")


@dataclass
class AppState:
    """The persisted watch list + configuration (data-model.md §3)."""

    schema_version: int
    cadence_minutes: float
    control_message_id: str | None
    products: list[Product] = field(default_factory=list)


@dataclass
class RuntimeFlags:
    """In-memory runtime state (data-model.md §1: never persisted).

    Used by GET /api/health; e.g. cycle-in-progress, gateway-connected.
    """

    cycle_in_progress: bool = False
    gateway_connected: bool = False


__all__ = [
    "AppState",
    "CheckOutcome",
    "CheckResult",
    "DuplicateUrlError",
    "Product",
    "RuntimeFlags",
    "SCHEMA_VERSION",
    "StockStatus",
    "ValidationError",
    "host_of",
    "normalize_url",
    "validate_indicator",
    "validate_label",
]
