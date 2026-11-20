"""Entrypoint: ``python -m botalert`` (task T024).

Wires the US1 pieces together:

1. ``load_config()`` — fail fast with a named-variable error when a
   required variable is missing/empty (data-model.md §5).
2. ``StateStore`` on ``config.state_file`` (durable, atomic).
3. Cycle dependencies: ``fetch_page`` bound to the configured timeout
   and user agent; ``send_restock`` bound to the configured webhook;
   a stdout-logging channel-sender fallback (the real Discord gateway
   sender is wired in US2, task T031); a wall clock.
4. ``Scheduler`` background thread (cycle = ``run_cycle``).
5. Flask app (``web.create_app``) served on the main thread.

``SIGINT``/``SIGTERM`` → stop the scheduler (join; no graceful drain
beyond the in-flight fetch's ``CHECK_TIMEOUT_SECONDS``, research.md §5)
→ exit 0.
"""

from __future__ import annotations

import functools
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from types import FrameType

from botalert.checker import FetchFn, RestockFn, run_cycle
from botalert.config import ConfigError, load_config
from botalert.fetcher import fetch_page
from botalert.models import Product, RuntimeFlags
from botalert.notifier import send_restock
from botalert.scheduler import Scheduler
from botalert.state import StateStore
from botalert.web import create_app, run_app

logger = logging.getLogger("botalert")


class WallClock:
    """Production clock for the checker: real UTC now, real sleep."""

    def now(self) -> datetime:
        """UTC now (state timestamps are UTC ISO-8601)."""
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        """Block for the inter-fetch pause (``CHECK_PAUSE_SECONDS``)."""
        time.sleep(seconds)


def _stdout_channel_sender(text: str) -> None:
    """US1 management-channel fallback: print the notice to stdout.

    US2 (task T031) replaces this with the Discord gateway channel
    sender; until then owner notices (📡 / ⚠️) are still delivered —
    to the operator's console.
    """
    print(f"[botalert channel notice] {text}", flush=True)


def main() -> int:
    """Run botalert: scheduler thread + Flask on the main thread."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"botalert: configuration error: {exc}", file=sys.stderr)
        return 1

    state = StateStore(config.state_file, config)
    flags = RuntimeFlags()

    fetcher: FetchFn = functools.partial(
        fetch_page,
        timeout=config.check_timeout_seconds,
        user_agent=config.user_agent,
    )
    # webhook_url is send_restock's FIRST positional parameter and the
    # checker calls it positionally (label, url, detected_at) — bind it
    # positionally or the keyword collides (got multiple values).
    webhook: RestockFn = functools.partial(send_restock, config.webhook_url)
    clock = WallClock()

    def cycle(snapshot: list[Product]) -> None:
        """One full check cycle over the scheduler's watch-list snapshot."""
        run_cycle(
            snapshot,
            fetcher,
            webhook,
            _stdout_channel_sender,
            state,
            clock,
            pause_seconds=config.check_pause_seconds,
        )

    scheduler = Scheduler(state, flags, cycle)
    scheduler.start()
    logger.info("scheduler armed; Flask starting on port %d", config.port)

    app = create_app(config, state, scheduler, flags)

    def _shutdown(signum: int, _frame: FrameType | None) -> None:
        """SIGINT/SIGTERM: stop the scheduler, then exit 0."""
        logger.info("signal %d received; stopping scheduler", signum)
        scheduler.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        run_app(app, config)
    finally:
        scheduler.stop()  # idempotent (the signal path already stopped it)
    return 0


if __name__ == "__main__":
    sys.exit(main())
