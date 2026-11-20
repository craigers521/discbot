"""Unit tests for botalert.checker.run_cycle (task T014).

The data-model.md §6 state machine, end to end against a real StateStore
with faked fetch/webhook/channel/clock: no alert on add, exactly one
webhook per out→in transition, failures never change status, once-only
threshold notice, strictly sequential fetches with the inter-fetch pause,
state saved after each product, webhook-failure channel notice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from botalert.checker import run_cycle
from botalert.models import Product, StockStatus
from botalert.state import StateStore
from tests.fakes import FakeChannelSender, FakeClock, FakeFetcher, FakeWebhook
from tests.helpers import make_config

OUT_HTML = "<p>currently out of stock. Notify me.</p>"
IN_HTML = "<button>Add to Cart</button>"


@dataclass
class Harness:
    """Wires one run_cycle invocation with fresh fakes + a tmp state store."""

    path: Path
    config: object = field(default=None)
    fetcher: FakeFetcher = field(default_factory=FakeFetcher)
    webhook: FakeWebhook = field(default_factory=FakeWebhook)
    channel: FakeChannelSender = field(default_factory=FakeChannelSender)
    clock: FakeClock = field(default_factory=FakeClock)
    store: StateStore | None = None
    pause: float = 1.5

    def build(self) -> StateStore:
        self.store = StateStore(str(self.path), make_config())
        return self.store

    def run(self, snapshot: list[Product]) -> None:
        assert self.store is not None
        run_cycle(
            snapshot,
            self.fetcher,
            self.webhook,
            self.channel,
            self.store,
            self.clock,
            pause_seconds=self.pause,
        )


@pytest.fixture()
def harness(tmp_path: Path) -> Harness:
    return Harness(path=tmp_path / "state.json")


class TestTransitions:
    def test_first_determined_check_no_alert(self, harness: Harness) -> None:
        store = harness.build()
        product = store.add_product("https://a.example.com/i", label="A")
        harness.fetcher.set_page("https://a.example.com/i", IN_HTML)
        harness.run([product])
        assert harness.webhook.messages == []  # no alert on add (US1-4)
        assert store.get_products()[0].last_status is StockStatus.IN_STOCK

    def test_out_to_in_sends_exactly_one_webhook_and_resets_counters(
        self, harness: Harness
    ) -> None:
        store = harness.build()
        product = store.add_product("https://a.example.com/i", label="A")
        url = "https://a.example.com/i"
        harness.fetcher.set_page(url, OUT_HTML)
        harness.run([product])
        assert store.get_products()[0].last_status is StockStatus.OUT_OF_STOCK

        harness.fetcher.set_page(url, IN_HTML)
        harness.run([product])
        assert len(harness.webhook.messages) == 1
        message = harness.webhook.messages[0]
        assert message.startswith("🔔 Restock: A\n")
        assert "https://a.example.com/i" in message
        assert "(detected " in message and message.endswith(" UTC)")
        product_after = store.get_products()[0]
        assert product_after.last_status is StockStatus.IN_STOCK
        assert product_after.consecutive_failures == 0
        assert product_after.failure_notified is False

    def test_stable_status_no_alert_counters_reset(self, harness: Harness) -> None:
        store = harness.build()
        product = store.add_product("https://a.example.com/i", label="A")
        url = "https://a.example.com/i"
        harness.fetcher.set_page(url, IN_HTML)
        harness.run([product])  # unknown → in_stock, no alert
        # Simulate a failure (counters climb), then a determined in_stock.
        harness.fetcher.set_error(url, "boom")
        harness.run([product])
        assert store.get_products()[0].consecutive_failures == 1
        harness.fetcher.set_page(url, IN_HTML)
        harness.run([product])
        assert harness.webhook.messages == []
        assert store.get_products()[0].consecutive_failures == 0
        assert store.get_products()[0].failure_notified is False


class TestFailures:
    def test_fetch_failure_never_changes_status(self, harness: Harness) -> None:
        store = harness.build()
        product = store.add_product("https://a.example.com/i", label="A")
        url = "https://a.example.com/i"
        harness.fetcher.set_page(url, OUT_HTML)
        harness.run([product])
        before_checked_at = store.get_products()[0].last_checked_at
        harness.clock.sleep(1.0)  # fake clock only advances on explicit sleep
        harness.fetcher.set_error(url, "connection refused")
        harness.run([product])
        after = store.get_products()[0]
        assert after.last_status is StockStatus.OUT_OF_STOCK  # unchanged
        assert after.consecutive_failures == 1
        assert after.last_checked_at is not None and after.last_checked_at != before_checked_at
        assert harness.webhook.messages == []  # never alerts (FR-012)

    def test_unknown_determination_counts_as_failure(self, harness: Harness) -> None:
        store = harness.build()
        product = store.add_product("https://a.example.com/i", label="A")
        url = "https://a.example.com/i"
        harness.fetcher.set_page(url, OUT_HTML)
        harness.run([product])
        harness.fetcher.set_page(url, "<p>mystery page</p>")  # no phrase match
        harness.run([product])
        after = store.get_products()[0]
        assert after.last_status is StockStatus.OUT_OF_STOCK  # unchanged
        assert after.consecutive_failures == 1

    def test_threshold_notice_once_only_at_ten(self, harness: Harness) -> None:
        store = harness.build()
        product = store.add_product("https://a.example.com/i", label="A")
        url = "https://a.example.com/i"
        harness.fetcher.set_error(url, "connection refused")
        for _ in range(12):
            harness.run([product])
        notices = [m for m in harness.channel.sent if m.startswith("⚠️")]
        assert len(notices) == 1  # fired at the 10th, not repeated
        assert notices[0] == (
            "⚠️ A (https://a.example.com/i) has failed its last 10 checks "
            "(last error: connection refused). It appears unreachable or its "
            "stock indicator no longer matches — check the page or set "
            "per-product indicators. This notice won't repeat until it "
            "recovers or is removed."
        )
        assert store.get_products()[0].failure_notified is True

    def test_threshold_notice_can_refire_after_recovery(self, harness: Harness) -> None:
        store = harness.build()
        product = store.add_product("https://a.example.com/i", label="A")
        url = "https://a.example.com/i"
        harness.fetcher.set_error(url, "refused")
        for _ in range(10):
            harness.run([product])
        assert len(harness.channel.sent) == 1
        # Recovery resets the once-only gate.
        harness.fetcher.set_page(url, IN_HTML)
        harness.run([product])
        harness.fetcher.set_error(url, "refused again")
        for _ in range(10):
            harness.run([product])
        assert len(harness.channel.sent) == 2


class TestSequentialExecution:
    def test_fetches_run_in_watch_list_order_with_pause_between(self, harness: Harness) -> None:
        store = harness.build()
        urls = [f"https://p{n}.example.com/i" for n in range(4)]
        products = [store.add_product(u, label=f"P{n}") for n, u in enumerate(urls)]
        for url in urls:
            harness.fetcher.set_page(url, IN_HTML)
        harness.pause = 2.0
        harness.run(products)
        assert harness.fetcher.calls == urls  # strictly sequential, in order
        assert harness.clock.slept == [2.0, 2.0, 2.0]  # pause between, not after last

    def test_state_saved_after_each_product(self, harness: Harness) -> None:
        store = harness.build()
        urls = [f"https://p{n}.example.com/i" for n in range(3)]
        products = [store.add_product(u) for u in urls]
        for url in urls:
            harness.fetcher.set_page(url, IN_HTML)

        saves: list[bool] = []
        original_save = store._save

        def counting_save() -> None:
            original_save()
            saves.append(True)

        store._save = counting_save  # type: ignore[method-assign]
        harness.run(products)
        assert len(saves) >= 3  # one save per product's applied check
        # Persisted: reload sees every product checked.
        reloaded = StateStore(str(harness.path), make_config())
        assert all(p.last_checked_at is not None for p in reloaded.get_products())


class TestWebhookDeliveryFailure:
    def test_failed_webhook_still_records_status_and_sends_channel_notice(
        self, harness: Harness
    ) -> None:
        store = harness.build()
        product = store.add_product("https://a.example.com/i", label="A")
        url = "https://a.example.com/i"
        harness.fetcher.set_page(url, OUT_HTML)
        harness.run([product])
        harness.fetcher.set_page(url, IN_HTML)
        harness.webhook = FakeWebhook(fail=True, error_detail="webhook HTTP 500")
        harness.run([product])
        # Status recorded: the transition is persisted despite the failure.
        assert store.get_products()[0].last_status is StockStatus.IN_STOCK
        # Owner-visible channel notice, exact contract text, never dropped.
        assert harness.channel.sent == [
            "📡 Failed to deliver the restock alert for A to the webhook: "
            "webhook HTTP 500. The product status was still updated."
        ]
        # No silent re-alert later: status is now in_stock → in_stock.
        harness.webhook = FakeWebhook()
        harness.run([product])
        assert harness.webhook.messages == []


class TestLogging:
    """Observability contract: a failing check must be visible in the
    container log (caplog) — this is the fix for "no log output".

    Asserts the log LINES only; state behavior is covered above and must
    be unaffected by logging.
    """

    def test_failed_check_logs_warning_with_url_error_and_summary(
        self, harness: Harness, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = harness.build()
        product = store.add_product("https://a.example.com/i", label="A")
        harness.fetcher.set_error("https://a.example.com/i", "HTTP 444")
        with caplog.at_level(logging.INFO, logger="botalert.checker"):
            harness.run([product])
        messages = [record.message for record in caplog.records]
        # The per-product line names the product, its URL and the error.
        assert any(
            "A" in m and "https://a.example.com/i" in m and "HTTP 444" in m
            for m in messages
        )
        # The cycle summary counts the error so the shape of a bad cycle
        # (all-blocked vs. one-off) is visible at a glance.
        assert any(
            "check cycle summary" in m and "1 error(s)" in m for m in messages
        )

    def test_unknown_check_logs_warning_and_debug_page_snippet(
        self, harness: Harness, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = harness.build()
        product = store.add_product("https://a.example.com/i", label="A")
        # Fetched fine (200) but nothing matches — the silent failure mode.
        harness.fetcher.set_page(
            "https://a.example.com/i", "<p>challenge interstitial body</p>"
        )
        with caplog.at_level(logging.DEBUG, logger="botalert.checker"):
            harness.run([product])
        messages = [record.message for record in caplog.records]
        assert any(
            "unknown" in m and "no indicator" in m and "A" in m for m in messages
        )
        # The DEBUG snippet carries the page text that did not match.
        assert any("challenge interstitial body" in m for m in messages)

    def test_determined_check_logs_status_with_previous_status(
        self, harness: Harness, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = harness.build()
        product = store.add_product("https://a.example.com/i", label="A")
        url = "https://a.example.com/i"
        harness.fetcher.set_page(url, OUT_HTML)
        harness.run([product])  # unknown → out_of_stock (no logging here)
        harness.fetcher.set_page(url, IN_HTML)
        with caplog.at_level(logging.INFO, logger="botalert.checker"):
            # Fresh snapshot like the scheduler takes each cycle: its
            # last_status is the PRE-check status the log's "was" quotes.
            harness.run([store.get_products()[0]])  # out_of_stock → in_stock
        messages = [record.message for record in caplog.records]
        assert any("in_stock (was out_of_stock)" in m for m in messages)
        assert any("restock transition" in m for m in messages)
