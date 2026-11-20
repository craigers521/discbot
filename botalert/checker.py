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

Every product's outcome is logged so a failing watch list is
diagnosable from the container log alone: errors at WARNING (with the
fetcher's error detail, which already names the HTTP status), unknown
determinations at WARNING plus a DEBUG one-line page snippet (the
diagnostic for "fetched fine but the wording no longer matches"),
determined statuses at INFO with the previous status.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from botalert.fetcher import FetchResult
from botalert.models import CheckResult, Product, StockStatus
from botalert.state import CheckEffect, StateStore
from botalert.stock import determine_status

logger = logging.getLogger(__name__)

#: Max chars of a fetched page included in an unknown-status debug log.
_SNIPPET_LIMIT = 240


class Clock(Protocol):
    """Wall-clock seam: ``now()`` stamps results, ``sleep()`` paces fetches."""

    def now(self) -> datetime:
        """The shared stamp applied to every check of the cycle."""

    def sleep(self, seconds: float) -> None:
        """The fixed pause BETWEEN page fetches (not after the last)."""


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
    logger.info("check cycle starting: %d product(s)", len(snapshot))
    fetched = _fetch_all(snapshot, fetcher, clock, pause_seconds)
    errors = unknowns = determined = 0
    for product, result in fetched:
        check = _to_check_result(product, result, clock)
        effect = store.apply_check(check)
        if effect is None:
            logger.info(
                "check %s (%s): removed between fetch and apply; skipped",
                product.label, product.url,
            )
            continue  # product removed between fetch and apply
        _log_outcome(product, result, check, effect)
        if check.outcome == "error":
            errors += 1
        elif check.outcome is StockStatus.UNKNOWN:
            unknowns += 1
        else:
            determined += 1
        if effect.restock_transition:
            logger.info(
                "restock transition: %s (%s) out_of_stock → in_stock; "
                "delivering webhook alert",
                effect.label, effect.url,
            )
            _alert_restock(effect, webhook, channel, check.checked_at)
        if effect.threshold_notice:
            logger.warning(
                "failure threshold reached: %s (%s) failed %d consecutive "
                "checks (last error: %s); sending once-only notice",
                effect.label, effect.url, effect.consecutive_failures,
                effect.error_detail or "unknown",
            )
            channel(_threshold_text(effect))
    logger.info(
        "check cycle summary: %d checked — %d determined, %d error(s), "
        "%d unknown",
        len(fetched), determined, errors, unknowns,
    )


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


def _log_outcome(
    product: Product,
    fetched: FetchResult,
    check: CheckResult,
    effect: CheckEffect,
) -> None:
    """One log line per applied check — the container-log diagnostic.

    The snapshot's ``product`` still carries the PRE-check status, so a
    determined result can be logged as ``in_stock (was out_of_stock)``.
    """
    failures = (
        f" (failures {effect.consecutive_failures}/{effect.failure_threshold})"
    )
    if check.outcome == "error":
        logger.warning(
            "check %s (%s): error: %s%s",
            product.label, product.url, check.error, failures,
        )
    elif check.outcome is StockStatus.UNKNOWN:
        logger.warning(
            "check %s (%s): unknown — no indicator or built-in phrase "
            "matched%s",
            product.label, product.url, failures,
        )
        logger.debug(
            "check %s (%s): page snippet: %r",
            product.label, product.url, _snippet(fetched.html),
        )
    else:
        logger.info(
            "check %s (%s): %s (was %s)",
            product.label, product.url, check.outcome.value,
            product.last_status.value,
        )


def _snippet(html: str | None, limit: int = _SNIPPET_LIMIT) -> str:
    """Whitespace-collapsed, length-capped one-line page text for debug."""
    if html is None:
        return "(no page body)"
    return " ".join(html.split())[:limit]


def _alert_restock(
    effect: CheckEffect, webhook: RestockFn, channel: NoticeFn, checked_at: datetime
) -> None:
    """Deliver the restock alert; on failure notify the owner verbatim."""
    ok, detail = webhook(effect.label, effect.url, checked_at)
    if not ok:
        logger.warning(
            "restock webhook delivery failed for %s (%s): %s; "
            "sending channel notice",
            effect.label, effect.url, detail,
        )
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
