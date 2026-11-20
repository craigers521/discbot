"""Unit tests for botalert.scheduler (task T015).

Background loop: wait(cadence) → cycle → re-read cadence (SC-004);
wakeup() ends the current wait immediately (SC-001) and is a no-op while
a cycle is in flight (FR-018); no overlapping cycles; clean stop; and the
SC-006 scale case (25 products at a 5-minute cadence fit in the interval).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pytest
from flask import Flask

from botalert.api import create_api_blueprint
from botalert.checker import run_cycle
from botalert.models import Product, RuntimeFlags
from botalert.scheduler import Scheduler
from botalert.state import StateStore
from tests.fakes import FakeChannelSender, FakeClock, FakeFetcher, FakeWebhook, ScriptedWait
from tests.helpers import make_config

IN_HTML = "<button>Add to Cart</button>"


def make_state(tmp_path: Path) -> StateStore:
    return StateStore(str(tmp_path / "state.json"), make_config())


class TestWakeup:
    def test_wakeup_ends_current_wait_immediately(self, tmp_path: Path) -> None:
        store = make_state(tmp_path)  # default cadence 5 min = 300 s wait
        flags = RuntimeFlags()
        cycles: list[int] = []

        def cycle_fn(snapshot: list[Product]) -> None:
            cycles.append(len(snapshot))

        scheduler = Scheduler(store, flags, cycle_fn)
        scheduler.start()
        scheduler.wakeup()
        deadline = time.monotonic() + 5
        while not cycles and time.monotonic() < deadline:
            time.sleep(0.01)
        scheduler.stop()
        # The cycle ran because of the wakeup — not after a 300 s cadence.
        assert cycles == [0]

    def test_wakeup_during_cycle_in_flight_is_noop(self, tmp_path: Path) -> None:
        store = make_state(tmp_path)
        flags = RuntimeFlags()
        wait = ScriptedWait(results=[True])
        started = threading.Event()
        cycle_starts: list[float] = []

        def cycle_fn(snapshot: list[Product]) -> None:
            started.set()
            cycle_starts.append(time.monotonic())
            time.sleep(0.2)

        scheduler = Scheduler(
            store, flags, cycle_fn, wait=wait, signal=wait.wake
        )
        scheduler.start()
        assert started.wait(5.0)
        scheduler.wakeup()  # cycle in flight → must be a no-op
        time.sleep(0.2)  # let the in-flight cycle finish
        wait.wake()
        scheduler.stop()
        # If the wakeup had been honored, a second cycle would have started
        # immediately after the first ended (signaled wait returns at once).
        assert len(cycle_starts) == 1


class TestCadence:
    def test_next_wait_uses_updated_cadence_no_restart(self, tmp_path: Path) -> None:
        store = make_state(tmp_path)
        flags = RuntimeFlags()
        wait = ScriptedWait(results=[True, True, True, True])
        first_cycle = threading.Event()
        release_first = threading.Event()
        count = 0

        def cycle_fn(snapshot: list[Product]) -> None:
            nonlocal count
            count += 1
            if count == 1:
                first_cycle.set()
                # Park cycle 1 so the cross-thread write below is guaranteed
                # to land BEFORE the scheduler's next cadence read — the
                # write happens while cycle 1 is in flight (between cycles),
                # with no restart (SC-004).
                release_first.wait(5.0)

        scheduler = Scheduler(store, flags, cycle_fn, wait=wait, signal=wait.wake)
        scheduler.start()
        assert first_cycle.wait(5.0)
        store.set_cadence(2.0)  # written while cycle 1 is in flight (SC-004)
        release_first.set()
        deadline = time.monotonic() + 5
        while len(wait.timeouts) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        wait.wake()
        scheduler.stop()
        assert wait.timeouts[0] == pytest.approx(300.0)  # initial 5 min
        assert wait.timeouts[1] == pytest.approx(120.0)  # new 2 min cadence
        assert wait.timeouts[2] == pytest.approx(120.0)


class TestCadenceViaApi:
    def test_put_config_next_wait_uses_new_cadence_no_restart(self, tmp_path: Path) -> None:
        """SC-004 through the full API chain (US3 T034): ``PUT /api/config``
        (auth → validation → store update under the lock → scheduler wakeup)
        and the RUNNING scheduler's NEXT wait picks up the new cadence —
        no restart, 100% of the time (fake clock / scripted wait).
        """
        config = make_config()
        store = StateStore(str(tmp_path / "state.json"), config)
        flags = RuntimeFlags()
        wait = ScriptedWait(results=[True, True, True, True])
        first_cycle = threading.Event()
        release_first = threading.Event()
        count = 0

        def cycle_fn(snapshot: list[Product]) -> None:
            nonlocal count
            count += 1
            if count == 1:
                first_cycle.set()
                release_first.wait(5.0)

        scheduler = Scheduler(store, flags, cycle_fn, wait=wait, signal=wait.wake)
        scheduler.start()
        assert first_cycle.wait(5.0)
        # The exact production chain: auth header, JSON body, store, wakeup.
        app = Flask(__name__)
        app.register_blueprint(create_api_blueprint(config, store, scheduler, flags))
        with app.test_client() as client:
            response = client.put(
                "/api/config",
                json={"cadence_minutes": 2.0},
                headers={"X-Botalert-Token": "test-shared-secret"},
            )
        assert response.status_code == 200
        release_first.set()
        deadline = time.monotonic() + 5
        while len(wait.timeouts) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        wait.wake()
        scheduler.stop()
        assert wait.timeouts[0] == pytest.approx(300.0)  # initial 5 min
        assert wait.timeouts[1] == pytest.approx(120.0)  # next wait: new 2 min
        assert wait.timeouts[2] == pytest.approx(120.0)  # ...and after that


class TestNoOverlap:
    def test_cycles_never_run_in_parallel(self, tmp_path: Path) -> None:
        store = make_state(tmp_path)
        flags = RuntimeFlags()
        wait = ScriptedWait(results=[True] * 5)
        lock = threading.Lock()
        active = 0
        max_active = 0
        completed = threading.Event()
        done = 0

        def cycle_fn(snapshot: list[Product]) -> None:
            nonlocal active, max_active, done
            with lock:
                active += 1
                max_active = max(max_active, active)
            assert flags.cycle_in_progress is True
            time.sleep(0.02)
            with lock:
                active -= 1
                done += 1
                if done == 5:
                    completed.set()

        scheduler = Scheduler(store, flags, cycle_fn, wait=wait, signal=wait.wake)
        scheduler.start()
        assert completed.wait(5.0)
        wait.wake()
        scheduler.stop()
        assert max_active == 1

    def test_stop_prevents_further_cycles(self, tmp_path: Path) -> None:
        store = make_state(tmp_path)
        flags = RuntimeFlags()
        wait = ScriptedWait(results=[True] * 10)
        count = 0

        def cycle_fn(snapshot: list[Product]) -> None:
            nonlocal count
            count += 1
            time.sleep(0.01)

        scheduler = Scheduler(store, flags, cycle_fn, wait=wait, signal=wait.wake)
        scheduler.start()
        deadline = time.monotonic() + 5
        while count < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        wait.wake()
        scheduler.stop()
        time.sleep(0.05)  # allow any in-flight cycle to finish
        first = count
        time.sleep(0.2)
        assert count == first


class TestScale:
    def test_25_product_cycle_fits_within_cadence_interval(self, tmp_path: Path) -> None:
        # SC-006: 25 products at 5-minute cadence — one full sequential
        # cycle must complete within the interval; every product checked.
        store = make_state(tmp_path)
        fetcher = FakeFetcher()
        clock = FakeClock()
        urls = [f"https://s{n}.example.com/item" for n in range(25)]
        for url in urls:
            fetcher.set_page(url, IN_HTML)
            store.add_product(url)
        snapshot = store.get_products()
        run_cycle(
            snapshot,
            fetcher,
            FakeWebhook(),
            FakeChannelSender(),
            store,
            clock,
            pause_seconds=2.0,
        )
        assert fetcher.calls == urls  # all 25, in watch-list order
        assert clock.slept == [2.0] * 24  # pause between, not after the last
        assert sum(clock.slept) < store.get_cadence() * 60.0  # 48 s < 300 s
        products = store.get_products()
        assert len(products) == 25
        assert all(p.last_checked_at == clock.now() for p in products)

    def test_scheduler_initial_wait_is_full_cadence(self, tmp_path: Path) -> None:
        store = make_state(tmp_path)  # default cadence 5 min
        flags = RuntimeFlags()
        wait = ScriptedWait(results=[True])
        first_cycle = threading.Event()
        fetcher = FakeFetcher()
        clock = FakeClock()
        url = "https://s0.example.com/item"
        fetcher.set_page(url, IN_HTML)
        store.add_product(url)

        def cycle_fn(snapshot: list[Product]) -> None:
            first_cycle.set()
            run_cycle(
                snapshot,
                fetcher,
                FakeWebhook(),
                FakeChannelSender(),
                store,
                clock,
                pause_seconds=2.0,
            )

        scheduler = Scheduler(store, flags, cycle_fn, wait=wait, signal=wait.wake)
        scheduler.start()
        assert first_cycle.wait(5.0)
        wait.wake()
        scheduler.stop()
        assert wait.timeouts[0] == pytest.approx(300.0)


class TestCrashResilience:
    def test_cycle_crash_is_logged_and_loop_survives(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = make_state(tmp_path)
        flags = RuntimeFlags()
        wait = ScriptedWait(results=[True, True])
        count = 0

        def cycle_fn(snapshot: list[Product]) -> None:
            nonlocal count
            count += 1
            if count == 1:
                raise RuntimeError("simulated cycle crash")

        scheduler = Scheduler(store, flags, cycle_fn, wait=wait, signal=wait.wake)
        with caplog.at_level(logging.ERROR, logger="botalert.scheduler"):
            scheduler.start()
            deadline = time.monotonic() + 5
            while count < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            scheduler.stop()
        # Cycle 1 raised; the loop must have survived and run cycle 2
        # (a dead scheduler thread = checks silently stop, no log at all).
        assert count == 2
        # The crash was logged with its traceback (in the record's
        # exc_info) and the loop explicitly continued.
        crashed = [
            record
            for record in caplog.records
            if record.exc_info is not None
            and "simulated cycle crash" in str(record.exc_info[1])
        ]
        assert crashed
        assert any(
            "continuing with the next cycle" in record.message
            for record in caplog.records
        )
