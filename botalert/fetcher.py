"""Page fetching (FR-012, research.md §6; WAF-tolerant backends per T052).

One HTTP GET per product, over one of two backends selected by the
``FETCH_BACKEND`` setting (data-model.md §5):

* ``http`` — stdlib ``urllib.request`` with the configured
  ``User-Agent`` and timeout (the original v1 path).
* ``curl`` — ``curl_cffi`` with ``impersonate="chrome"``: a full
  Chrome TLS/JA3 + HTTP/2 fingerprint. Retailer edges that reject
  non-browser clients (Walmart, Newegg, Amazon, Best Buy — verified
  against live product pages) serve the real page to this identity,
  while headless *browsers* get refused; a curl-level client is both
  lighter and more effective here. This backend also solves Amazon's
  soft cookie gate (the ~4 KB ``validateCaptcha`` page served as a
  200): fetch → submit the hidden form in the same session → refetch.
* ``auto`` (default) — ``http`` first; if the result looks like bot
  protection (a WAF reject status, or a 2xx page carrying a known
  gate marker), or the fetch timed out (some WAF edges drop plain
  clients instead of answering with a wall page), one ``curl`` retry
  follows.

Only the final response (after any redirects) is evaluated. A non-2xx
final status, a timeout, or a connection error yields a ``FetchResult``
carrying an ``error`` — a failure, which is NEVER interpreted as a
stock status (FR-012). Error strings keep their stable shapes
(``HTTP <code>``, ``connection error: <reason>``, ``fetch error:
<reason>``) and carry no variable content (ms counts, curl codes) so
threshold-notice text stays readable across runs. SSL verification
stays on; no JS rendering is ever attempted (the ``curl`` backend
passes these gates without one — see T052 for the evidence).

Every fetch is logged (this module is where "the check failed" stops
being a mystery): successes at INFO with the FINAL url (after redirects),
status, byte count and duration; failures at WARNING with the same plus,
for HTTP errors, a whitespace-collapsed excerpt of the error response
body — that excerpt is what reveals bot-protection interstitials
("Request Rejected", "Just a moment...") behind an opaque 403/444.
"""

from __future__ import annotations

import html as _html
import logging
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Final

from curl_cffi import requests as creq
from curl_cffi.requests import exceptions as _curl_errors

logger = logging.getLogger(__name__)

#: Max length (chars) of an error-response excerpt in the failure log.
_EXCERPT_LIMIT = 200

#: Fetch backends accepted by ``fetch_page`` / ``FETCH_BACKEND``.
BACKENDS: Final[tuple[str, ...]] = ("http", "curl", "auto")

#: Final HTTP statuses that are WAF rejects, not genuine "page missing"
#: answers — worth one browser-impersonated retry under ``auto``.
_BLOCK_STATUSES: Final[frozenset[int]] = frozenset({403, 407, 429, 444, 451, 503})

#: Lowercase body markers of known bot gates that are served with a 2xx
#: status (the wall pretends to be a normal page): Walmart
#: "Robot or human?", Cloudflare "Just a moment...", Amazon's soft
#: ``validateCaptcha`` cookie gate, and Akamai "Request Rejected".
_BLOCK_MARKERS: Final[tuple[str, ...]] = (
    "robot or human",
    "just a moment",
    "validatecaptcha",
    "request rejected",
    "cf-challenge",
)

#: Amazon soft gate: relative form action and its hidden fields.
_AMAZON_GATE_ACTION_RE: Final[re.Pattern[str]] = re.compile(
    r'action="(/errors_page/validateCaptcha[^"]*)"'
)
_AMAZON_GATE_FIELD_RE: Final[re.Pattern[str]] = re.compile(
    r'<input type="hidden" name="([^"]+)" value="([^"]*)"'
)

#: Module-level urlopen seam so tests can substitute the transport
#: without touching the network (Constitution Principle III).
_urlopen = urllib.request.urlopen


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


def looks_bot_blocked(result: FetchResult) -> bool:
    """Whether a fetch result looks like bot protection, not a real answer.

    True for WAF reject statuses (``_BLOCK_STATUSES``) and for 2xx pages
    carrying a known gate marker (``_BLOCK_MARKERS``). A plain 404 and a
    normal product page are never "blocked": the ``auto`` backend must
    not retry those — a real 404 stays a failure, a good page stays a
    success (and the gate body itself is re-examined by the stock
    matcher, where it reads ``unknown``, never a status).
    """
    if result.status in _BLOCK_STATUSES:
        return True
    if result.html is None:
        return False
    lowered = result.html.lower()
    return any(marker in lowered for marker in _BLOCK_MARKERS)


def _looks_like_timeout(result: FetchResult) -> bool:
    """Whether a failure is a timeout — and thus possibly a WAF reset.

    Some edges (Best Buy observed in the live probes) do not answer a
    non-browser client with a wall status at all: they drop or stall
    the connection, which surfaces as a timeout. ``auto`` therefore
    gives timeouts one curl retry too. A timeout with a status set is
    not a failure in the first place, so only status-less results are
    considered.
    """
    if result.status is not None:
        return False
    error = (result.error or "").lower()
    return "timed out" in error or "timeout" in error


def _fetch_http(url: str, timeout: float, user_agent: str) -> FetchResult:
    """The original stdlib-urllib fetch (``http`` backend)."""
    request = urllib.request.Request(
        url, headers={"User-Agent": user_agent}, method="GET"
    )
    started = time.monotonic()
    try:
        with _urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            status = int(response.status)
            final_url = response.geturl()
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
        return FetchResult(html=None, status=None, error=f"connection error: {exc.reason}")
    except (socket.timeout, TimeoutError, OSError) as exc:
        logger.warning(
            "fetch %s: fetch error after %.0f ms: %r",
            url, (time.monotonic() - started) * 1000.0, exc,
        )
        return FetchResult(html=None, status=None, error=f"fetch error: {exc}")


def _new_curl_session() -> creq.Session:
    """Fresh Chrome-impersonating session (one per fetch).

    A fresh session per fetch keeps one product's cookies (e.g. Amazon's
    gate token) from leaking into the next product's fetch — and is
    exactly what the live probes validated. Tests monkeypatch this name.
    """
    return creq.Session(impersonate="chrome")


def _curl_error_string(exc: BaseException, timeout: float) -> str:
    """Deterministic error string for a curl-cffi failure.

    The library's own messages embed variable content (elapsed ms, curl
    error codes); threshold-notice text carries these strings verbatim,
    so failures are mapped by exception TYPE to a stable phrase instead.
    """
    if isinstance(exc, _curl_errors.Timeout):
        return f"fetch error: timed out after {timeout:.0f}s"
    if isinstance(exc, _curl_errors.DNSError):
        return "connection error: DNS lookup failed"
    if isinstance(exc, _curl_errors.SSLError):
        return "connection error: SSL verification failed"
    if isinstance(exc, _curl_errors.ConnectionError):
        return "connection error: connection failed"
    return f"fetch error: {type(exc).__name__}"


def _solve_amazon_gate(
    session: creq.Session,
    url: str,
    timeout: float,
    gate_body: str,
    gate_url: str,
) -> tuple[int, str, str] | None:
    """Solve Amazon's soft cookie gate (the ``validateCaptcha`` 200 page).

    The page is a plain form, not a CAPTCHA: submit its hidden fields
    back (same session, so the returned cookie sticks), then refetch the
    product URL. Returns ``(status, body, final_url)`` of the refetch, or
    ``None`` when the page is not that gate or the solve failed — the
    caller then reports the gate page as fetched, where the stock
    matcher reads ``unknown`` (never a status).
    """
    match = _AMAZON_GATE_ACTION_RE.search(gate_body)
    if match is None:
        return None
    fields = {
        name: _html.unescape(value)
        for name, value in _AMAZON_GATE_FIELD_RE.findall(gate_body)
    }
    try:
        session.get(
            urllib.parse.urljoin(gate_url, match.group(1)),
            params=fields,
            timeout=timeout,
        )
        response = session.get(url, timeout=timeout)
    except _curl_errors.RequestException as exc:
        logger.warning(
            "fetch %s: Amazon gate solve failed (%s); reporting the gate page",
            url, _curl_error_string(exc, timeout),
        )
        return None
    return response.status_code, response.text, str(response.url)


def _fetch_curl(url: str, timeout: float) -> FetchResult:
    """Fetch via curl_cffi with a full Chrome TLS/HTTP/2 identity.

    ``USER_AGENT`` is deliberately NOT applied here: this backend's
    identity is the impersonated Chrome client as a whole (JA3/JA4,
    HTTP/2 settings, headers), and overriding the UA string would break
    that match — the thing the WAF walls check first.
    """
    started = time.monotonic()
    session = _new_curl_session()
    try:
        try:
            response = session.get(url, timeout=timeout)
        except _curl_errors.HTTPError as exc:
            # curl_cffi raises HTTPError when the transfer fails AFTER
            # headers arrived (an edge resetting mid-body — Best Buy does
            # this intermittently). If the received status is a
            # definitive 4xx/5xx, report it as such; otherwise the body
            # is incomplete — a transport failure, never a success.
            code = getattr(getattr(exc, "response", None), "status_code", None)
            # code 0 is curl's "no response at all" — not a status to
            # report, just a transport failure.
            if code is not None and code != 0 and not 200 <= code < 300:
                logger.warning(
                    "fetch %s [curl]: HTTP %d in %.0f ms "
                    "(transfer interrupted after headers)",
                    url, code, (time.monotonic() - started) * 1000.0,
                )
                return FetchResult(html=None, status=code, error=f"HTTP {code}")
            logger.warning(
                "fetch %s [curl]: connection error after %.0f ms: "
                "response interrupted",
                url, (time.monotonic() - started) * 1000.0,
            )
            return FetchResult(
                html=None, status=None, error="connection error: response interrupted"
            )
        except _curl_errors.RequestException as exc:
            logger.warning(
                "fetch %s [curl]: %s after %.0f ms",
                url, _curl_error_string(exc, timeout),
                (time.monotonic() - started) * 1000.0,
            )
            return FetchResult(
                html=None, status=None, error=_curl_error_string(exc, timeout)
            )
        status = response.status_code
        body = response.text
        final_url = str(response.url)
        if 200 <= status < 300 and "validateCaptcha" in body:
            # Amazon serves its soft cookie gate as a 200; solve it in
            # this session and report the refetched page instead.
            solved = _solve_amazon_gate(session, url, timeout, body, final_url)
            if solved is not None:
                status, body, final_url = solved
        elapsed_ms = (time.monotonic() - started) * 1000.0
        if 200 <= status < 300:
            logger.info(
                "fetch %s [curl]: HTTP %d, %d bytes in %.0f ms (final url %s)",
                url, status, len(body), elapsed_ms, final_url,
            )
            return FetchResult(html=body, status=status, error=None)
        logger.warning(
            "fetch %s [curl]: HTTP %d in %.0f ms (final url %s) — not 2xx",
            url, status, elapsed_ms, final_url,
        )
        return FetchResult(html=None, status=status, error=f"HTTP {status}")
    finally:
        session.close()


def fetch_page(
    url: str, timeout: float, user_agent: str, backend: str = "auto"
) -> FetchResult:
    """Fetch a page and return a ``FetchResult`` (network failures never raise).

    ``backend`` selects the transport: ``http`` (stdlib urllib + the
    configured UA), ``curl`` (Chrome-impersonated curl_cffi), or
    ``auto`` — ``http`` first, then one ``curl`` retry when the result
    looks like bot protection (see :func:`looks_bot_blocked`) or the
    fetch timed out (see :func:`_looks_like_timeout`). All
    network and HTTP failures are folded into ``FetchResult.error`` so
    the checker treats them uniformly as a failed check (FR-012).
    """
    if backend not in BACKENDS:
        raise ValueError(f"unknown fetch backend: {backend!r}")
    if backend == "curl":
        return _fetch_curl(url, timeout)
    result = _fetch_http(url, timeout, user_agent)
    if backend == "auto" and looks_bot_blocked(result):
        logger.info(
            "fetch %s: bot protection suspected (status %s); "
            "retrying with the curl (Chrome-impersonated) backend",
            url, result.status,
        )
        return _fetch_curl(url, timeout)
    if backend == "auto" and _looks_like_timeout(result):
        logger.info(
            "fetch %s: timeout (%s); retrying once with the curl "
            "(Chrome-impersonated) backend in case of a WAF reset",
            url, result.error,
        )
        return _fetch_curl(url, timeout)
    return result


__all__ = ["BACKENDS", "FetchResult", "fetch_page", "looks_bot_blocked"]
