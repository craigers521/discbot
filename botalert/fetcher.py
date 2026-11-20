"""Page fetching (FR-012, research.md §6).

A single HTTP GET per product via stdlib ``urllib.request`` with a
configurable ``User-Agent`` and timeout. Only the final response (after
any redirects) is evaluated. A non-2xx final status, a timeout, or a
connection error yields a ``FetchResult`` carrying an ``error`` — a
failure, which is NEVER interpreted as a stock status. SSL verification
stays on (stdlib default); no JS rendering (spec Assumptions).

Every fetch is logged (this module is where "the check failed" stops
being a mystery): successes at INFO with the FINAL url (after redirects),
status, byte count and duration; failures at WARNING with the same plus,
for HTTP errors, a whitespace-collapsed excerpt of the error response
body — that excerpt is what reveals bot-protection interstitials
("Request Rejected", "Just a moment...") behind an opaque 403/444.
"""

from __future__ import annotations

import logging
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Max length (chars) of an error-response excerpt in the failure log.
_EXCERPT_LIMIT = 200


@dataclass(frozen=True)
class FetchResult:
    """The outcome of one page fetch.

    Exactly one of ``html`` (success) or ``error`` (failure) is set. On
    success ``status`` is the final response status; on failure it is the
    HTTP status if one was received, else ``None``.
    """

    html: str | None
    status: int | None
    error: str | None


def _excerpt(text: str, limit: int = _EXCERPT_LIMIT) -> str:
    """Whitespace-collapsed, length-capped one-line text for logging."""
    return " ".join(text.split())[:limit]


def fetch_page(url: str, timeout: float, user_agent: str) -> FetchResult:
    """Fetch a page and return a ``FetchResult``.

    ``timeout`` is the per-attempt socket timeout in seconds. Network and
    HTTP failures never raise — they are folded into ``FetchResult.error``
    so the checker can treat them uniformly as a failed check (FR-012).
    """
    request = urllib.request.Request(
        url, headers={"User-Agent": user_agent}, method="GET"
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            status = int(response.status)
            final_url = response.geturl()
    except urllib.error.HTTPError as exc:
        # Non-2xx final status: urlopen raises HTTPError for these.
        # The excerpt of the error body is the diagnostic: bot walls
        # (Cloudflare "Just a moment...", WAF "Request Rejected") show up
        # here, while bare 404s stay short and obvious.
        try:
            excerpt = _excerpt(exc.read().decode("utf-8", "replace"))
        except Exception:
            excerpt = "(error body unreadable)"
        logger.warning(
            "fetch %s: HTTP %d in %.0f ms (final url %s, body excerpt: %r)",
            url,
            exc.code,
            (time.monotonic() - started) * 1000.0,
            getattr(exc, "url", url),
            excerpt,
        )
        return FetchResult(html=None, status=exc.code, error=f"HTTP {exc.code}")
    except urllib.error.URLError as exc:
        logger.warning(
            "fetch %s: connection error after %.0f ms: %s",
            url, (time.monotonic() - started) * 1000.0, exc.reason,
        )
        return FetchResult(
            html=None, status=None, error=f"connection error: {exc.reason}"
        )
    except (socket.timeout, TimeoutError, OSError) as exc:
        logger.warning(
            "fetch %s: fetch error after %.0f ms: %r",
            url, (time.monotonic() - started) * 1000.0, exc,
        )
        return FetchResult(html=None, status=None, error=f"fetch error: {exc}")

    elapsed_ms = (time.monotonic() - started) * 1000.0
    if 200 <= status < 300:
        logger.info(
            "fetch %s: HTTP %d, %d bytes in %.0f ms (final url %s)",
            url, status, len(body), elapsed_ms, final_url,
        )
        return FetchResult(html=body, status=status, error=None)
    logger.warning(
        "fetch %s: HTTP %d in %.0f ms (final url %s) — not 2xx",
        url, status, elapsed_ms, final_url,
    )
    return FetchResult(html=None, status=status, error=f"HTTP {status}")


__all__ = ["FetchResult", "fetch_page"]
