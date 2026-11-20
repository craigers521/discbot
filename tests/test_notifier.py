"""Unit tests for botalert.notifier (task T013).

Webhook POST payload is exactly the contract shape with a 10 s timeout;
delivery failures (non-2xx or transport error) are surfaced to the
caller, never silently dropped.
"""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timezone
from email.message import Message
from typing import Any, Literal

import pytest

from botalert.notifier import build_restock_content, notify_owner, send_restock


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> Literal[False]:
        return False


def _capture_urlopen(monkeypatch: pytest.MonkeyPatch, status: int = 204) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: object, timeout: float | None = None) -> _FakeResponse:
        captured["request"] = request
        captured["timeout"] = timeout
        return _FakeResponse(status)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return captured


class TestSendRestock:
    def test_payload_shape_method_timeout_and_content(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _capture_urlopen(monkeypatch, status=204)
        detected_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        ok, error = send_restock(
            "https://hook.example/abc", "Sneakers", "https://shop.example.com/i", detected_at
        )
        assert ok is True
        assert error is None
        request = captured["request"]
        assert request.method == "POST"
        assert request.full_url == "https://hook.example/abc"
        assert captured["timeout"] == pytest.approx(10.0)
        body = json.loads(request.data.decode("utf-8"))
        assert body == {
            "content": "🔔 Restock: Sneakers\nhttps://shop.example.com/i\n(detected 12:00:00 UTC)"
        }

    def test_content_builder(self) -> None:
        detected_at = datetime(2026, 1, 1, 0, 5, 9, tzinfo=timezone.utc)
        assert build_restock_content(
            "L", "https://x.example.com/i", detected_at
        ) == "🔔 Restock: L\nhttps://x.example.com/i\n(detected 00:05:09 UTC)"

    def test_non_2xx_http_error_surfaced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raising_urlopen(request: object, timeout: float | None = None) -> _FakeResponse:
            raise urllib.error.HTTPError(
                "https://hook.example/abc", 500, "Internal Server Error", Message(), None
            )

        monkeypatch.setattr("urllib.request.urlopen", raising_urlopen)
        ok, error = send_restock(
            "https://hook.example/abc",
            "Sneakers",
            "https://shop.example.com/i",
            datetime.now(timezone.utc),
        )
        assert ok is False
        assert error is not None and "500" in error

    def test_transport_error_surfaced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raising_urlopen(request: object, timeout: float | None = None) -> _FakeResponse:
            raise urllib.error.URLError("connection refused (fake)")

        monkeypatch.setattr("urllib.request.urlopen", raising_urlopen)
        ok, error = send_restock(
            "https://hook.example/abc",
            "Sneakers",
            "https://shop.example.com/i",
            datetime.now(timezone.utc),
        )
        assert ok is False
        assert error == "connection refused (fake)"

    def test_non_2xx_final_status_without_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # urlopen normally raises on non-2xx; a 304-style response that
        # slips through must still be treated as a delivery failure.
        _capture_urlopen(monkeypatch, status=304)
        ok, error = send_restock(
            "https://hook.example/abc",
            "Sneakers",
            "https://shop.example.com/i",
            datetime.now(timezone.utc),
        )
        assert ok is False
        assert error is not None and "304" in error


class TestNotifyOwner:
    def test_uses_injected_sender(self) -> None:
        from tests.fakes import FakeChannelSender

        sender = FakeChannelSender()
        notify_owner(sender, "hello owner")
        assert sender.sent == ["hello owner"]

    def test_sender_receives_text_verbatim(self) -> None:
        captured: list[str] = []
        notify_owner(captured.append, "⚠️ threshold notice")
        assert captured == ["⚠️ threshold notice"]
