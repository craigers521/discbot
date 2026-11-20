"""Restock alert delivery (FR-013, FR-014, research.md §3/§8).

Outbound notifications are exactly one Discord webhook POST per restock
alert and one management-channel send per owner notice. Delivery
failures are surfaced to the caller as ``(False, detail)`` — never
silently dropped (the checker then notifies the owner, contract
management-api.md §Restock).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime

#: Fixed webhook delivery timeout in seconds (contract: 10 s).
WEBHOOK_TIMEOUT_SECONDS: float = 10.0


def build_restock_content(label: str, url: str, detected_at: datetime) -> str:
    """Build the webhook text for one restock alert.

    Exact shape (management-api.md §Restock):

    .. code-block:: text

        🔔 Restock: {label}
        {url}
        (detected {H:%M:%S} UTC)
    """
    return (
        f"🔔 Restock: {label}\n{url}\n(detected {detected_at:%H:%M:%S} UTC)"
    )


def send_restock(
    webhook_url: str,
    label: str,
    url: str,
    detected_at: datetime,
) -> tuple[bool, str | None]:
    """POST the restock alert to the configured Discord webhook.

    Returns ``(True, None)`` on a 2xx response. Any HTTP error, transport
    error, or non-2xx final status returns ``(False, detail)`` with a
    human-readable detail (status code or transport reason) so the
    caller can notify the owner (FR-014).
    """
    payload = json.dumps(
        {"content": build_restock_content(label, url, detected_at)}
    ).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT_SECONDS) as response:
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        return False, f"webhook HTTP error: {exc.code}"
    except urllib.error.URLError as exc:
        return False, str(exc.reason)
    if 200 <= status < 300:
        return True, None
    return False, f"webhook non-2xx status: {status}"


def notify_owner(sender: Callable[[str], None], text: str) -> None:
    """Send an operational notice verbatim to the management channel."""
    sender(text)


__all__ = [
    "WEBHOOK_TIMEOUT_SECONDS",
    "build_restock_content",
    "notify_owner",
    "send_restock",
]
