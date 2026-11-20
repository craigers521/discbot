"""One full check cycle (FR-012, FR-018, data-model.md §6).

Strictly sequential over the watch-list snapshot, in insertion order,
with a fixed pause BETWEEN fetches (not after the last one). The cycle
runs in two passes: every product is fetched first (that is where the
pauses and the sequential ordering live), then every check is applied
through ``StateStore.apply_check`` (the store owns the §6 state machine)
and acted on:

- ``restock_transition`` → POST the webhook; a delivery failure sends
  the exact owner-visible channel notice (status still persisted).
- ``threshold_notice`` → send the exact once-only threshold notice.

Applying after fetching means every check of one cycle is stamped with
the same ``clock.now()`` (the clock only advances on the fetch pauses),
state is saved after each product's applied check, and a product removed
between fetch and apply is simply skipped (``apply_check`` → None).
A fetch failure or an ``unknown`` determination never changes
``last_status`` (guaranteed by the store) and never alerts.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from botalert.fetcher import FetchResult
from botalert.models import CheckResult, Product, StockStatus
from botalert.state import CheckEffect, StateStore
from botalert.stock import determine_status


class Clock(Protocol):
    """Wall-clock seam: ``now()`` stamps results, ``sleep()`` paces fetches."""

    def now(self) -> datetime: ...

    def sleep(self, seconds: float) -> None: ...


#: One page fetch per URL (botalert.fetcher.fetch_page or a test fake).
FetchFn = Callable[[str], FetchResult]
#: Restock alert delivery: (label, url, detected_at) → (ok, error_detail).
RestockFn = Callable[[str, str, datetime], tuple[bool, str | None]]
#: Operational notice to the management channel.
NoticeFn = Callable[[str], None]


def run_cycle(
    snapshot: list[Product],
    fetcher: FetchFn,
    webhook: RestockFn,
    channel: NoticeFn,
    store: StateStore,
    clock: Clock,
    pause_seconds: float,
) -> None:
    """Check every product in ``snapshot`` exactly once, in order.

    ``snapshot`` is the list captured at cycle start (products added or
    removed mid-cycle do not change this cycle's iteration).
    """
    fetched = _fetch_all(snapshot, fetcher, clock, pause_seconds)
    for product, result in fetched:
        check = _to_check_result(product, result, clock)
        effect = store.apply_check(check)
        if effect is None:
            continue  # product removed between fetch and apply
        if effect.restock_transition:
            _alert_restock(effect, webhook, channel, check.checked_at)
        if effect.threshold_notice:
            channel(_threshold_text(effect))


def _fetch_all(
    snapshot: list[Product],
    fetcher: FetchFn,
    clock: Clock,
    pause_seconds: float,
) -> list[tuple[Product, FetchResult]]:
    """Sequential fetch pass: watch-list order, pause between, not after."""
    results: list[tuple[Product, FetchResult]] = []
    for index, product in enumerate(snapshot):
        if index > 0:
            clock.sleep(pause_seconds)
        results.append((product, fetcher(product.url)))
    return results


def _to_check_result(
    product: Product, fetched: FetchResult, clock: Clock
) -> CheckResult:
    """Fold one FetchResult into a CheckResult stamped at apply time."""
    if fetched.error is not None:
        return CheckResult(
            product_id=product.id,
            outcome="error",
            error=fetched.error,
            checked_at=clock.now(),
        )
    status: StockStatus = determine_status(fetched.html or "", product)
    return CheckResult(
        product_id=product.id,
        outcome=status,
        error=None,
        checked_at=clock.now(),
    )


def _alert_restock(
    effect: CheckEffect, webhook: RestockFn, channel: NoticeFn, checked_at: datetime
) -> None:
    """Deliver the restock alert; on failure notify the owner verbatim."""
    ok, detail = webhook(effect.label, effect.url, checked_at)
    if not ok:
        channel(
            "📡 Failed to deliver the restock alert for "
            f"{effect.label} to the webhook: {detail}. "
            "The product status was still updated."
        )


def _threshold_text(effect: CheckEffect) -> str:
    """Exact once-only threshold-notice text (data-model.md §6)."""
    return (
        f"⚠️ {effect.label} ({effect.url}) has failed its last "
        f"{effect.failure_threshold} checks (last error: "
        f"{effect.error_detail or 'unknown'}). It appears unreachable or its "
        "stock indicator no longer matches — check the page or set "
        "per-product indicators. This notice won't repeat until it "
        "recovers or is removed."
    )


__all__ = ["Clock", "FetchFn", "NoticeFn", "RestockFn", "run_cycle"]
