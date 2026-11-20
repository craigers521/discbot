"""Fetcher backend tests (T052) — fully offline (Constitution III).

Both transports are faked at module-level seams: the urllib path
through ``botalert.fetcher._urlopen``, the curl path through
``botalert.fetcher._new_curl_session`` (a canned-response fake
session). No test touches the network. Error-string shapes (the
threshold-notice contract, data-model.md §6) are asserted verbatim.
"""

from __future__ import annotations

import io
import socket
import urllib.error
from email.message import Message
from typing import Any

import pytest

import botalert.fetcher as fetcher
from botalert.config import ConfigError, load_config
from botalert.fetcher import FetchResult, fetch_page, looks_bot_blocked
from curl_cffi.requests import exceptions as curl_errors

URL = "https://a.example.com/i"
REAL_PAGE = "<h1>Widget</h1><p>In Stock</p>"


# --- fakes ---------------------------------------------------------------


class FakeUrlopenResponse:
    """Stand-in for urllib's urlopen context manager."""

    def __init__(self, body: str, status: int = 200, final_url: str = URL) -> None:
        self._body = body
        self.status = status
        self._final_url = final_url

    def read(self) -> bytes:
        return self._body.encode("utf-8")

    def geturl(self) -> str:
        return self._final_url

    def __enter__(self) -> FakeUrlopenResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class FakeCurlResponse:
    """Stand-in for curl_cffi's Response (the attrs the fetcher reads)."""

    def __init__(
        self, body: str, status: int = 200, url: str = URL
    ) -> None:
        self._body = body
        self.status_code = status
        self.url = url

    @property
    def text(self) -> str:
        return self._body


class FakeCurlSession:
    """Canned responses in call order; records (url, params) of each GET."""

    def __init__(self, responses: list[FakeCurlResponse | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, str] | None]] = []
        self.closed = False

    def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> FakeCurlResponse:
        self.calls.append((url, params))
        if not self._responses:
            raise AssertionError("FakeCurlSession.get called more times than canned")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


def _patch_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    outcome: FakeUrlopenResponse | Exception,
) -> None:
    def fake_urlopen(request: Any, timeout: float) -> Any:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(fetcher, "_urlopen", fake_urlopen)


def _patch_curl_session(
    monkeypatch: pytest.MonkeyPatch, session: FakeCurlSession
) -> None:
    monkeypatch.setattr(fetcher, "_new_curl_session", lambda: session)


def _boom_if_called(*_args: Any, **_kwargs: Any) -> None:
    raise AssertionError("curl session factory must not be called")


def _http_error(code: int, msg: str, body: bytes) -> urllib.error.HTTPError:
    """A urlopen HTTPError with a readable error body (the diagnostic)."""
    return urllib.error.HTTPError(URL, code, msg, Message(), io.BytesIO(body))


# --- looks_bot_blocked ----------------------------------------------------


@pytest.mark.parametrize(
    ("html", "status", "expected"),
    [
        (None, 403, True),        # WAF reject status
        (None, 444, True),        # Walmart's opaque reject
        (None, 404, False),       # genuine "page missing" — never a wall
        ("Robot or human?", 200, True),
        ("<p>just a moment...</p>", 200, True),
        ("<p>validateCaptcha</p>", 200, True),
        ("<p>Request Rejected</p>", 200, True),
        (REAL_PAGE, 200, False),  # normal product page
        (None, None, False),      # connection error, no status
    ],
)
def test_looks_bot_blocked(
    html: str | None, status: int | None, expected: bool
) -> None:
    result = FetchResult(html=html, status=status, error=None if html else "err")
    assert looks_bot_blocked(result) is expected


# --- http backend ---------------------------------------------------------


def test_http_backend_serves_page(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(monkeypatch, FakeUrlopenResponse(REAL_PAGE))
    result = fetch_page(URL, 15.0, "ua", backend="http")
    assert result == FetchResult(html=REAL_PAGE, status=200, error=None)


def test_http_backend_http_error_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(monkeypatch, _http_error(444, "No Response", b"denied"))
    result = fetch_page(URL, 15.0, "ua", backend="http")
    assert result.status == 444
    assert result.error == "HTTP 444"
    assert result.html is None


def test_http_backend_connection_error_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_urlopen(monkeypatch, urllib.error.URLError(socket.gaierror("no host")))
    result = fetch_page(URL, 15.0, "ua", backend="http")
    assert result.status is None
    assert result.error == "connection error: no host"


# --- curl backend ---------------------------------------------------------


def test_curl_backend_serves_page(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeCurlSession([FakeCurlResponse(REAL_PAGE)])
    _patch_curl_session(monkeypatch, session)
    result = fetch_page(URL, 15.0, "ua", backend="curl")
    assert result == FetchResult(html=REAL_PAGE, status=200, error=None)
    assert session.calls == [(URL, None)]
    assert session.closed is True


def test_curl_backend_http_error_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_curl_session(monkeypatch, FakeCurlSession([FakeCurlResponse("", 404)]))
    result = fetch_page(URL, 15.0, "ua", backend="curl")
    assert result.status == 404
    assert result.error == "HTTP 404"


def test_curl_backend_timeout_string_is_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The library message embeds variable ms counts; the error string
    # must not (threshold-notice contract).
    exc = curl_errors.ReadTimeout(
        "Failed to perform, curl: (28) Operation timed out after 15001 ms"
    )
    _patch_curl_session(monkeypatch, FakeCurlSession([exc]))
    result = fetch_page(URL, 15.0, "ua", backend="curl")
    assert result.status is None
    assert result.error == "fetch error: timed out after 15s"


def test_curl_backend_connection_error_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for exc, expected in (
        (curl_errors.DNSError("name resolution failed"), "connection error: DNS lookup failed"),
        (curl_errors.SSLError("certificate verify failed"), "connection error: SSL verification failed"),
        (curl_errors.ConnectionError("connection reset"), "connection error: connection failed"),
    ):
        _patch_curl_session(monkeypatch, FakeCurlSession([exc]))
        result = fetch_page(URL, 15.0, "ua", backend="curl")
        assert result.error == expected, expected


def test_curl_backend_solves_amazon_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = (
        '<html><head><title>Amazon.com</title></head><body>'
        '<form action="/errors_page/validateCaptcha?amzn=abc&amp;amzn-r=xyz">'
        '<input type="hidden" name="amzn" value="abc&quot;def">'
        '<input type="hidden" name="amzn-r" value="xyz">'
        "</form></body></html>"
    )
    session = FakeCurlSession(
        [
            FakeCurlResponse(gate, url="https://www.amazon.com/dp/B0D5CQ9CHQ"),
            FakeCurlResponse("", status=202),
            FakeCurlResponse(REAL_PAGE, url="https://www.amazon.com/dp/B0D5CQ9CHQ"),
        ]
    )
    _patch_curl_session(monkeypatch, session)
    result = fetch_page("https://www.amazon.com/dp/B0D5CQ9CHQ", 15.0, "ua", backend="curl")
    # The refetched real page is what the checker sees.
    assert result == FetchResult(html=REAL_PAGE, status=200, error=None)
    # Fetch → solve (hidden form, unescaped, in the same session) → refetch.
    assert session.calls[0] == ("https://www.amazon.com/dp/B0D5CQ9CHQ", None)
    assert session.calls[1][0].startswith("https://www.amazon.com/errors_page/validateCaptcha")
    assert session.calls[1][1] == {"amzn": 'abc"def', "amzn-r": "xyz"}
    assert session.calls[2] == ("https://www.amazon.com/dp/B0D5CQ9CHQ", None)
    assert len(session.calls) == 3


def test_curl_backend_gate_page_without_form_reported_as_fetched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 200 carrying the marker but no solvable form: report the page as
    # fetched (the stock matcher reads "unknown" from it — never a status).
    gate = "<html><body>validateCaptcha but no form here</body></html>"
    session = FakeCurlSession([FakeCurlResponse(gate)])
    _patch_curl_session(monkeypatch, session)
    result = fetch_page(URL, 15.0, "ua", backend="curl")
    assert result == FetchResult(html=gate, status=200, error=None)
    assert len(session.calls) == 1


def test_curl_backend_gate_solve_failure_reports_gate_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = (
        '<form action="/errors_page/validateCaptcha">'
        '<input type="hidden" name="amzn" value="abc">'
        "</form>"
    )
    session = FakeCurlSession(
        [FakeCurlResponse(gate), curl_errors.ReadTimeout("timed out")]
    )
    _patch_curl_session(monkeypatch, session)
    result = fetch_page(URL, 15.0, "ua", backend="curl")
    assert result == FetchResult(html=gate, status=200, error=None)
    assert len(session.calls) == 2


def test_curl_backend_interrupted_transfer_reports_definitive_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Edge reset mid-body with a definitive 4xx already received: the
    # status is the answer.
    exc = curl_errors.HTTPError("transfer interrupted", 0, FakeCurlResponse("", 404))
    _patch_curl_session(monkeypatch, FakeCurlSession([exc]))
    result = fetch_page(URL, 15.0, "ua", backend="curl")
    assert result.status == 404
    assert result.error == "HTTP 404"


@pytest.mark.parametrize(
    ("status", "error"),
    [
        # A 2xx with an incomplete body is untrustworthy.
        (200, "connection error: response interrupted"),
        # curl's status 0 is "no response at all", not a status to report.
        (0, "connection error: response interrupted"),
    ],
)
def test_curl_backend_interrupted_transfer_is_not_a_success(
    monkeypatch: pytest.MonkeyPatch, status: int, error: str
) -> None:
    # Transport failure with a deterministic string (no curl codes or ms
    # in the notice text), and never a success.
    exc = curl_errors.HTTPError(
        "transfer interrupted", 0, FakeCurlResponse("partial", status)
    )
    _patch_curl_session(monkeypatch, FakeCurlSession([exc]))
    result = fetch_page(URL, 15.0, "ua", backend="curl")
    assert result.status is None
    assert result.error == error
    assert result.html is None


# --- auto backend (fallback behavior) -------------------------------------


def test_auto_retries_curl_after_waf_403(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(monkeypatch, _http_error(403, "Forbidden", b"Just a moment"))
    session = FakeCurlSession([FakeCurlResponse(REAL_PAGE)])
    _patch_curl_session(monkeypatch, session)
    result = fetch_page(URL, 15.0, "ua", backend="auto")
    assert result == FetchResult(html=REAL_PAGE, status=200, error=None)
    assert session.calls == [(URL, None)]


def test_auto_retries_curl_after_interstitial_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_urlopen(monkeypatch, FakeUrlopenResponse("<title>Robot or human?</title>"))
    session = FakeCurlSession([FakeCurlResponse(REAL_PAGE)])
    _patch_curl_session(monkeypatch, session)
    result = fetch_page(URL, 15.0, "ua", backend="auto")
    assert result == FetchResult(html=REAL_PAGE, status=200, error=None)
    assert len(session.calls) == 1


def test_auto_does_not_retry_plain_404(monkeypatch: pytest.MonkeyPatch) -> None:
    # A genuine 404 must stay a failure — no curl retry, no masking.
    _patch_urlopen(monkeypatch, _http_error(404, "Not Found", b"nope"))
    monkeypatch.setattr(fetcher, "_new_curl_session", _boom_if_called)
    result = fetch_page(URL, 15.0, "ua", backend="auto")
    assert result.status == 404
    assert result.error == "HTTP 404"


def test_auto_retries_curl_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # WAF edges that reset plain clients (Best Buy in the live probes)
    # surface as a timeout, not a wall status — one curl retry still.
    _patch_urlopen(
        monkeypatch, urllib.error.URLError(socket.timeout("timed out"))
    )
    session = FakeCurlSession([FakeCurlResponse(REAL_PAGE)])
    _patch_curl_session(monkeypatch, session)
    result = fetch_page(URL, 15.0, "ua", backend="auto")
    assert result == FetchResult(html=REAL_PAGE, status=200, error=None)
    assert session.calls == [(URL, None)]


def test_auto_does_not_retry_plain_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_urlopen(monkeypatch, urllib.error.URLError(OSError("no route to host")))
    monkeypatch.setattr(fetcher, "_new_curl_session", _boom_if_called)
    result = fetch_page(URL, 15.0, "ua", backend="auto")
    assert result.status is None
    assert result.error == "connection error: no route to host"


def test_auto_does_not_retry_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(monkeypatch, FakeUrlopenResponse(REAL_PAGE))
    monkeypatch.setattr(fetcher, "_new_curl_session", _boom_if_called)
    result = fetch_page(URL, 15.0, "ua", backend="auto")
    assert result == FetchResult(html=REAL_PAGE, status=200, error=None)


def test_curl_backend_skips_http_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeCurlSession([FakeCurlResponse(REAL_PAGE)])
    _patch_curl_session(monkeypatch, session)

    def boom_urlopen(request: Any, timeout: float) -> Any:
        raise AssertionError("urlopen must not be called with backend=curl")

    monkeypatch.setattr(fetcher, "_urlopen", boom_urlopen)
    result = fetch_page(URL, 15.0, "ua", backend="curl")
    assert result == FetchResult(html=REAL_PAGE, status=200, error=None)


def test_unknown_backend_rejected() -> None:
    with pytest.raises(ValueError, match="unknown fetch backend"):
        fetch_page(URL, 15.0, "ua", backend="bogus")


# --- FETCH_BACKEND config (data-model.md §5) -------------------------------

_REQUIRED_ENV = {
    "DISCORD_TOKEN": "t",
    "OWNER_DISCORD_ID": "1",
    "DISCORD_MANAGE_CHANNEL_ID": "2",
    "BOTALERT_WEBHOOK_URL": "https://example.com/webhook",
    "BOTALERT_TOKEN": "s",
}


@pytest.mark.parametrize(("value", "expected"), [("curl", "curl"), ("http", "http"), ("auto", "auto")])
def test_fetch_backend_env_accepted(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    for name, raw in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, raw)
    monkeypatch.setenv("FETCH_BACKEND", value)
    assert load_config().fetch_backend == expected


def test_fetch_backend_env_defaults_to_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, raw in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, raw)
    monkeypatch.delenv("FETCH_BACKEND", raising=False)
    assert load_config().fetch_backend == "auto"


def test_fetch_backend_env_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, raw in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, raw)
    monkeypatch.setenv("FETCH_BACKEND", "browser")
    with pytest.raises(ConfigError, match="FETCH_BACKEND must be one of: http|curl|auto"):
        load_config()
