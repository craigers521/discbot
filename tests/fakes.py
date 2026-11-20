"""Deterministic offline seams (Constitution Principle III).

Every network I/O the product performs — page fetch, webhook POST,
management-channel send, wall-clock — sits behind one of these fakes so
the full suite is deterministic and offline. The Discord gateway harness
(US2) is added here when botalert/discord_bot.py lands.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from botalert.fetcher import FetchResult


class FakeFetcher:
    """In-memory fetcher: canned html/error per URL, records call order.

    Calls are counted and recorded in order so tests can assert the
    checker fetches strictly sequentially in watch-list order (FR-018).
    Unknown URLs fail with a fake connection error.
    """

    def __init__(self) -> None:
        self.pages: dict[str, str] = {}
        self.errors: dict[str, str] = {}
        self.calls: list[str] = []

    def set_page(self, url: str, html: str) -> None:
        # Last-set state wins: a new page clears a prior error for this URL.
        self.pages[url] = html
        self.errors.pop(url, None)

    def set_error(self, url: str, error: str) -> None:
        # Last-set state wins: a new error clears a prior page for this URL.
        self.errors[url] = error
        self.pages.pop(url, None)

    def __call__(self, url: str) -> FetchResult:
        self.calls.append(url)
        if url in self.errors:
            return FetchResult(html=None, status=None, error=self.errors[url])
        if url in self.pages:
            return FetchResult(html=self.pages[url], status=200, error=None)
        return FetchResult(html=None, status=None, error=f"fake connection error for {url}")


@dataclass
class FakeWebhook:
    """Records every restock webhook body; can simulate delivery failure.

    Implements the checker's RestockFn signature (label, url, detected_at).
    """

    fail: bool = False
    error_detail: str = "webhook transport error"
    messages: list[str] = field(default_factory=list)

    def __call__(self, label: str, url: str, detected_at: datetime) -> tuple[bool, str | None]:
        if self.fail:
            return False, self.error_detail
        self.messages.append(
            f"🔔 Restock: {label}\n{url}\n(detected {detected_at:%H:%M:%S} UTC)"
        )
        return True, None


@dataclass
class FakeChannelSender:
    """Records operational notices destined for the management channel."""

    sent: list[str] = field(default_factory=list)

    def __call__(self, text: str) -> None:
        self.sent.append(text)


class FakeClock:
    """Manual-advance clock for scheduler/checker determinism.

    ``sleep`` advances the fake now() instead of blocking, so a full
    25-product cycle with 2 s pauses completes instantly in tests.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self._now += timedelta(seconds=seconds)


@dataclass
class ScriptedWait:
    """Fake threading.Event.wait for scheduler tests.

    Records every timeout it is asked to wait; returns the scripted
    results in order, then blocks on ``wake()`` (mirrors a real
    Event.wait that is woken early).
    """

    results: list[bool] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)
    _wake: threading.Event = field(default_factory=threading.Event, repr=False)

    def __call__(self, timeout: float) -> bool:
        self.timeouts.append(timeout)
        if self.results:
            return self.results.pop(0)
        return self._wake.wait(timeout=5.0)

    def wake(self) -> None:
        self._wake.set()


@dataclass
class FakeScheduler:
    """Records wakeup()/stop() calls; satisfies the API's scheduler seam."""

    wakeups: list[bool] = field(default_factory=lambda: [False])

    def wakeup(self) -> None:
        self.wakeups.append(True)

    def stop(self) -> None:
        self.wakeups.append(False)
