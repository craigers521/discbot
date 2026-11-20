"""Background check scheduler (SC-001, SC-004, FR-018).

A single daemon thread loops: check stop → ``wait(cadence)`` → check
stop again → run one cycle. The cadence is re-read from the store on
every iteration (SC-004: ``set_cadence`` between cycles takes effect on
the next wait without a restart). ``wakeup()`` ends the current wait
early (SC-001) but is a NO-OP while a cycle is in flight (FR-018) so a
wakeup can never queue a second, overlapping cycle. Cycles never run in
parallel: the loop itself is single-threaded and the stop flag is
re-checked after every wait so a stop that races a wakeup never starts
another cycle.

The cycle receives the watch-list snapshot captured just before it runs
(products added or removed mid-cycle are picked up next cycle).

``wait``/``signal`` are injectable seams for tests: ``wait(timeout)``
returns ``True`` when woken early, and ``signal()`` is what ``wakeup()``
and ``stop()`` use to end a pending wait. Defaults are a plain
``threading.Event``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from botalert.models import Product, RuntimeFlags
from botalert.state import StateStore


class Scheduler:
    """One background thread that runs check cycles at the store's cadence."""

    def __init__(
        self,
        store: StateStore,
        flags: RuntimeFlags,
        cycle_fn: Callable[[list[Product]], None],
        wait: Callable[[float], bool] | None = None,
        signal: Callable[[], None] | None = None,
    ) -> None:
        self._store = store
        self._flags = flags
        self._cycle_fn = cycle_fn
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if wait is not None and signal is not None:
            self._wait = wait
            self._signal = signal
            self._owned_event: threading.Event | None = None
        else:
            event = threading.Event()
            self._wait = event.wait
            self._signal = event.set
            self._owned_event = event

    def start(self) -> None:
        """Start the background loop (no-op if already started)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="botalert-scheduler", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            # Re-read the cadence on every wait (SC-004).
            self._wait(self._store.get_cadence() * 60.0)
            if self._owned_event is not None:
                self._owned_event.clear()
            if self._stop.is_set():
                break
            self._flags.cycle_in_progress = True
            try:
                self._cycle_fn(self._store.get_products())
            finally:
                self._flags.cycle_in_progress = False

    def wakeup(self) -> None:
        """End the current wait early; no-op while a cycle is in flight."""
        if self._flags.cycle_in_progress:
            return
        self._signal()

    def stop(self) -> None:
        """Stop the loop after any in-flight wait/cycle; joins the thread."""
        self._stop.set()
        self._signal()
        if self._thread is not None:
            self._thread.join()


__all__ = ["Scheduler"]
