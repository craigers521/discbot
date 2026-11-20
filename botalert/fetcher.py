"""Page fetching (FR-012, research.md §6).

A single HTTP GET per product via stdlib ``urllib.request`` with a
configurable ``User-Agent`` and timeout. Only the final response (after
any redirects) is evaluated. A non-2xx final status, a timeout, or a
connection error yields a ``FetchResult`` carrying an ``error`` — a
failure, which is NEVER interpreted as a stock status. SSL verification
stays on (stdlib default); no JS rendering (spec Assumptions).
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
from dataclasses import dataclass


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


def fetch_page(url: str, timeout: float, user_agent: str) -> FetchResult:
    """Fetch a page and return a ``FetchResult``.

    ``timeout`` is the per-attempt socket timeout in seconds. Network and
    HTTP failures never raise — they are folded into ``FetchResult.error``
    so the checker can treat them uniformly as a failed check (FR-012).
    """
    request = urllib.request.Request(
        url, headers={"User-Agent": user_agent}, method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        # Non-2xx final status: urlopen raises HTTPError for these.
        return FetchResult(html=None, status=exc.code, error=f"HTTP {exc.code}")
    except urllib.error.URLError as exc:
        return FetchResult(html=None, status=None, error=f"connection error: {exc.reason}")
    except (socket.timeout, TimeoutError, OSError) as exc:
        return FetchResult(html=None, status=None, error=f"fetch error: {exc}")

    if 200 <= status < 300:
        return FetchResult(html=body, status=status, error=None)
    return FetchResult(html=None, status=status, error=f"HTTP {status}")


__all__ = ["FetchResult", "fetch_page"]
