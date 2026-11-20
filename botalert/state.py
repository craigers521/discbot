"""Durable state store (data-model.md §3/§6/§7, research.md §7).

Single process-wide ``threading.Lock``; every read-modify-write is ONE
critical section followed by an atomic save (serialize → ``<file>.tmp``
in the same directory → ``os.replace``). Load rules:

- missing file → fresh state (default cadence, empty watch list);
- corrupt/unparseable file or unknown ``schema_version`` → log an error,
  rename the offender to ``<file>.corrupt-<utcstamp>`` (never delete
  owner data) and start fresh — never guess.

Documented ``Any`` boundary (Constitution I justification, plan.md):
parsed JSON from stdlib ``json.loads`` is received as ``Any`` in this
module for one scope, then immediately validated and narrowed into
``AppState``/``Product`` before anything leaves the module. No other
module sees ``Any``.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

from botalert.config import Config
from botalert.models import (
    SCHEMA_VERSION,
    AppState,
    CheckResult,
    DuplicateUrlError,
    Product,
    StockStatus,
    ValidationError,
    host_of,
    normalize_url,
    validate_indicator,
    validate_label,
)

logger = logging.getLogger(__name__)


class UnsetType:
    """Sentinel: a modify field was not provided (distinct from '' = clear)."""

    def __repr__(self) -> str:
        return "UNSET"


UNSET: Final[UnsetType] = UnsetType()


@dataclass(frozen=True)
class CheckEffect:
    """Side effects of applying one check result (data-model.md §6).

    The store decides; the checker acts (webhook / channel notices).
    """

    restock_transition: bool
    threshold_notice: bool
    label: str
    url: str
    error_detail: str | None
    consecutive_failures: int
    failure_threshold: int


_HEX: Final[frozenset[str]] = frozenset("0123456789abcdef")


def _utc_now() -> datetime:
    """UTC now, timezone-aware (timestamps are UTC ISO-8601 in the file)."""
    return datetime.now(timezone.utc)


def _parse_utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValidationError(f"{field_name} must be a UTC (offset-bearing) timestamp")
    return parsed


class StateStore:
    """Locked, atomic, schema-validated store for a single AppState.

    Contract: every public mutator is one critical section + one atomic
    save; readers return copies, never internal objects.
    """

    def __init__(self, path: str, config: Config) -> None:
        self._path = path
        self._config = config
        self._lock = threading.Lock()
        self._state = self._load()

    # ------------------------------------------------------------------ load

    def _fresh_state(self) -> AppState:
        return AppState(
            schema_version=SCHEMA_VERSION,
            cadence_minutes=self._config.default_cadence_minutes,
            control_message_id=None,
            products=[],
        )

    def _load(self) -> AppState:
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except FileNotFoundError:
            logger.info("No state file at %s; starting fresh", self._path)
            return self._fresh_state()
        try:
            # Documented Any boundary (see module docstring): the JSON API is
            # not generic, so parsed JSON is received as Any here for one
            # scope and immediately validated/narrowed in _parse_state.
            data: Any = json.loads(text)
            return self._parse_state(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            self._quarantine(f"corrupt state file: {exc}")
            return self._fresh_state()

    def _quarantine(self, reason: str) -> None:
        """Rename the offender to <file>.corrupt-<utcstamp> — never delete."""
        stamp = _utc_now().strftime("%Y%m%dT%H%M%S.%fZ")
        offender = f"{self._path}.corrupt-{stamp}"
        logger.error("Quarantining %s: %s", self._path, reason)
        os.replace(self._path, offender)

    def _parse_state(self, data: Any) -> AppState:
        if not isinstance(data, dict):
            raise ValidationError("state file must be a JSON object")
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValidationError(
                f"unknown schema_version {version!r} (expected {SCHEMA_VERSION})"
            )
        cadence = data.get("cadence_minutes")
        if isinstance(cadence, bool) or not isinstance(cadence, (int, float)):
            raise ValidationError("cadence_minutes must be a number")
        cadence = float(cadence)
        if not self._config.min_cadence_minutes <= cadence <= self._config.max_cadence_minutes:
            raise ValidationError(
                f"cadence_minutes {cadence} outside "
                f"[{self._config.min_cadence_minutes}, {self._config.max_cadence_minutes}]"
            )
        control_id = data.get("control_message_id")
        if control_id is not None and not isinstance(control_id, str):
            raise ValidationError("control_message_id must be a string or null")
        products_raw = data.get("products")
        if not isinstance(products_raw, list):
            raise ValidationError("products must be a list")
        products: list[Product] = []
        seen: set[str] = set()
        for item in products_raw:
            product = self._parse_product(item)
            if product.url in seen:
                raise ValidationError(f"duplicate URL in state file: {product.url}")
            seen.add(product.url)
            products.append(product)
        return AppState(
            schema_version=SCHEMA_VERSION,
            cadence_minutes=cadence,
            control_message_id=control_id,
            products=products,
        )

    def _parse_product(self, item: Any) -> Product:
        if not isinstance(item, dict):
            raise ValidationError("each product must be a JSON object")
        pid = item.get("id")
        if not (isinstance(pid, str) and len(pid) == 12 and set(pid) <= _HEX):
            raise ValidationError(f"product id must be 12-char lowercase hex: {pid!r}")
        url = item.get("url")
        if not isinstance(url, str):
            raise ValidationError("product url must be a string")
        normalized = normalize_url(url)
        if normalized != url:
            raise ValidationError(f"product url not normalized: {url!r}")
        label = item.get("label")
        if not isinstance(label, str):
            raise ValidationError("product label must be a string")
        validate_label(label)
        for key in ("in_stock_indicator", "out_of_stock_indicator"):
            indicator = item.get(key)
            if indicator is not None:
                if not isinstance(indicator, str):
                    raise ValidationError(f"{key} must be a string or null")
                validate_indicator(indicator)
        status_raw = item.get("last_status")
        if not isinstance(status_raw, str):
            raise ValidationError("last_status must be a string")
        try:
            last_status = StockStatus(status_raw)
        except ValueError:
            raise ValidationError(f"unknown last_status: {status_raw!r}") from None
        last_checked_raw = item.get("last_checked_at")
        if last_checked_raw is None:
            last_checked = None
        else:
            last_checked = _parse_utc(last_checked_raw, "last_checked_at")
        failures = item.get("consecutive_failures")
        if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
            raise ValidationError("consecutive_failures must be an int >= 0")
        notified = item.get("failure_notified")
        if not isinstance(notified, bool):
            raise ValidationError("failure_notified must be a boolean")
        created_at = _parse_utc(item.get("created_at"), "created_at")
        return Product(
            id=pid,
            url=normalized,
            label=label,
            in_stock_indicator=item.get("in_stock_indicator"),
            out_of_stock_indicator=item.get("out_of_stock_indicator"),
            last_status=last_status,
            last_checked_at=last_checked,
            consecutive_failures=failures,
            failure_notified=notified,
            created_at=created_at,
        )

    # ------------------------------------------------------------------ save

    def _save(self) -> None:
        """Atomic save: serialize → temp file in same dir → os.replace.

        Caller must hold the lock.
        """
        payload = {
            "schema_version": SCHEMA_VERSION,
            "cadence_minutes": self._state.cadence_minutes,
            "control_message_id": self._state.control_message_id,
            "products": [product.to_dict() for product in self._state.products],
        }
        tmp_path = f"{self._path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(tmp_path, self._path)

    # ---------------------------------------------------------------- readers

    def get_products(self) -> list[Product]:
        """Insertion-ordered snapshot (copies)."""
        with self._lock:
            return [copy.copy(product) for product in self._state.products]

    def get_product(self, url: str) -> Product | None:
        """Look up by (normalized) URL; unparseable URLs simply miss."""
        try:
            normalized = normalize_url(url)
        except ValidationError:
            return None
        with self._lock:
            for product in self._state.products:
                if product.url == normalized:
                    return copy.copy(product)
            return None

    def get_cadence(self) -> float:
        """The current cadence in minutes (re-read every scheduler wait)."""
        with self._lock:
            return self._state.cadence_minutes

    def get_control_message_id(self) -> str | None:
        """The stored control message id, or None when not posted yet."""
        with self._lock:
            return self._state.control_message_id

    # ---------------------------------------------------------------- mutators

    def add_product(
        self,
        url: str,
        label: str | None = None,
        in_stock_indicator: str | None = None,
        out_of_stock_indicator: str | None = None,
    ) -> Product:
        """Add a product (validates, then one critical section + save).

        Raises ValidationError on bad fields; DuplicateUrlError when the
        normalized URL is already monitored (any casing — no silent merge).
        """
        product = Product.create(
            url, label, in_stock_indicator, out_of_stock_indicator
        )
        with self._lock:
            if any(existing.url == product.url for existing in self._state.products):
                raise DuplicateUrlError(
                    f"a product with this URL is already monitored: {product.url}"
                )
            self._state.products.append(product)
            self._save()
        return copy.copy(product)

    def remove_product(self, url: str) -> Product | None:
        """Remove by (normalized) URL; returns the removed product or None."""
        try:
            normalized = normalize_url(url)
        except ValidationError:
            return None
        with self._lock:
            for index, product in enumerate(self._state.products):
                if product.url == normalized:
                    removed = self._state.products.pop(index)
                    self._save()
                    return copy.copy(removed)
            return None

    def modify_product(
        self,
        url: str,
        label: str | UnsetType = UNSET,
        in_stock_indicator: str | UnsetType = UNSET,
        out_of_stock_indicator: str | UnsetType = UNSET,
    ) -> Product | None:
        """PATCH semantics: only provided fields change; '' clears an
        indicator (empty-string = clear); unknown URL → None.

        Raises ValidationError on bad values. Every provided value is
        validated BEFORE the product is touched, so a rejection applies
        nothing — the read-modify-write stays one critical section with
        no partial mutation (data-model.md §6).
        """
        try:
            normalized = normalize_url(url)
        except ValidationError:
            return None
        new_label: str | None = None
        new_in: str | None = None
        new_out: str | None = None
        if not isinstance(label, UnsetType):
            new_label = validate_label(label)
        if not isinstance(in_stock_indicator, UnsetType):
            new_in = validate_indicator(in_stock_indicator)
        if not isinstance(out_of_stock_indicator, UnsetType):
            new_out = validate_indicator(out_of_stock_indicator)
        with self._lock:
            product = next(
                (p for p in self._state.products if p.url == normalized), None
            )
            if product is None:
                return None
            if new_label is not None:
                product.label = new_label
            if not isinstance(in_stock_indicator, UnsetType):
                product.in_stock_indicator = new_in
            if not isinstance(out_of_stock_indicator, UnsetType):
                product.out_of_stock_indicator = new_out
            self._save()
            return copy.copy(product)

    def set_cadence(self, minutes: float) -> None:
        """Set the global cadence; raises ValidationError outside the
        configured [min, max] floor/ceiling."""
        if not self._config.min_cadence_minutes <= minutes <= self._config.max_cadence_minutes:
            raise ValidationError(
                f"cadence {minutes} outside "
                f"[{self._config.min_cadence_minutes}, {self._config.max_cadence_minutes}]"
            )
        with self._lock:
            self._state.cadence_minutes = float(minutes)
            self._save()

    def set_control_message_id(self, message_id: str | None) -> None:
        """Store the control message id (None clears it); persisted."""
        with self._lock:
            self._state.control_message_id = message_id
            self._save()

    def apply_check(self, result: CheckResult) -> CheckEffect | None:
        """Apply one check per the data-model.md §6 state machine.

        - fetch failure or ``unknown``: last_status unchanged;
          consecutive_failures += 1; last_checked_at updated; at
          consecutive_failures == failure_threshold with
          failure_notified == False → threshold_notice and
          failure_notified = True (once-only until recovery/removal).
        - determined status: last_status updated; both failure counters
          reset to 0/False; restock_transition iff out_of_stock → in_stock.

        Returns the effect for the caller to act on (webhook / channel
        notices), or None if the product was removed in the meantime.
        The state is saved inside the same critical section.
        """
        with self._lock:
            product = next(
                (p for p in self._state.products if p.id == result.product_id), None
            )
            if product is None:
                return None
            if result.outcome == "error" or result.outcome is StockStatus.UNKNOWN:
                product.consecutive_failures += 1
                product.last_checked_at = result.checked_at
                notice = (
                    product.consecutive_failures == self._config.failure_threshold
                    and not product.failure_notified
                )
                if notice:
                    product.failure_notified = True
                self._save()
                return CheckEffect(
                    restock_transition=False,
                    threshold_notice=notice,
                    label=product.label,
                    url=product.url,
                    error_detail=result.error,
                    consecutive_failures=product.consecutive_failures,
                    failure_threshold=self._config.failure_threshold,
                )
            determined = result.outcome
            restock = (
                product.last_status is StockStatus.OUT_OF_STOCK
                and determined is StockStatus.IN_STOCK
            )
            product.last_status = determined
            product.last_checked_at = result.checked_at
            product.consecutive_failures = 0
            product.failure_notified = False
            self._save()
            return CheckEffect(
                restock_transition=restock,
                threshold_notice=False,
                label=product.label,
                url=product.url,
                error_detail=None,
                consecutive_failures=0,
                failure_threshold=self._config.failure_threshold,
            )
