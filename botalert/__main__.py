"""Entrypoint: ``python -m botalert`` (tasks T024, T031).

Wires the US1 + US2 pieces together:

1. ``load_config()`` — fail fast with a named-variable error when a
   required variable is missing/empty (data-model.md §5).
2. ``StateStore`` on ``config.state_file`` (durable, atomic).
3. Cycle dependencies: ``fetch_page`` bound to the configured timeout
   and user agent; ``send_restock`` bound to the configured webhook; a
   wall clock. Channel notices go through
   ``BotalertClient.enqueue_notice`` (gateway post-ready; stdout mirror
   plus a buffer flushed exactly once on ready pre-ready).
4. ``BotalertClient`` Discord gateway on a daemon thread — a bad token
   degrades to gateway-less operation, not a crash (the webhook, API
   and scheduler keep working).
5. ``Scheduler`` background thread (cycle = ``run_cycle``).
6. Flask app (``web.create_app``) served on the main thread.

``LOG_LEVEL`` (optional env, default ``INFO``) sets the process-wide
logging level — ``DEBUG`` additionally logs the exact phrase that
matched a stock determination and one-line page snippets for
``unknown`` results (the "fetched fine but nothing matched" case).

``SIGINT``/``SIGTERM`` → stop the scheduler and the bot (join; no
graceful drain beyond the in-flight fetch's ``CHECK_TIMEOUT_SECONDS``,
research.md §5) → exit 0.
"""

from __future__ import annotations

import functools
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from types import FrameType

import discord

from botalert.checker import FetchFn, RestockFn, run_cycle
from botalert.config import ConfigError, load_config
from botalert.discord_bot import BotalertClient
from botalert.fetcher import fetch_page
from botalert.models import Product, RuntimeFlags
from botalert.notifier import send_restock
from botalert.scheduler import Scheduler
from botalert.state import StateStore
from botalert.web import create_app, run_app

logger = logging.getLogger("botalert")

#: LOG_LEVEL env values accepted verbatim (stdlib level names, upper).
_VALID_LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")


def _resolve_log_level() -> str:
    """LOG_LEVEL env override (default INFO); invalid names → INFO."""
    raw = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    if raw in _VALID_LOG_LEVELS:
        return raw
    print(
        f"botalert: invalid LOG_LEVEL {raw!r}; falling back to INFO",
        file=sys.stderr,
    )
    return "INFO"


class WallClock:
    """Production clock for the checker: real UTC now, real sleep."""

    def now(self) -> datetime:
        """UTC now (state timestamps are UTC ISO-8601)."""
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        """Block for the inter-fetch pause (``CHECK_PAUSE_SECONDS``)."""
        time.sleep(seconds)


def main() -> int:
    """Run botalert: scheduler thread + Flask on the main thread."""
    logging.basicConfig(
        level=_resolve_log_level(),
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
    # Effective check configuration (no secrets — T046): this line is the
    # first thing to compare against expectations after a deploy.
    logger.info(
        "botalert starting: %d watched product(s), cadence %g min, "
        "fetch timeout %ds, inter-fetch pause %ds, failure threshold %d, "
        "user agent %r, state file %s",
        len(state.get_products()),
        state.get_cadence(),
        config.check_timeout_seconds,
        config.check_pause_seconds,
        config.failure_threshold,
        config.user_agent,
        config.state_file,
    )

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
        """One full check cycle over the scheduler's watch-list snapshot.

        Resolves ``bot`` at call time: the scheduler is constructed below
        before the client (to hand its ``wakeup`` to the router), but the
        client is always bound before ``scheduler.start()`` and a cycle
        can run.
        """
        run_cycle(
            snapshot,
            fetcher,
            webhook,
            bot.enqueue_notice,
            state,
            clock,
            pause_seconds=config.check_pause_seconds,
        )

    def _bot_thread() -> None:
        """Run the Discord gateway; a bad token must not kill the daemon."""
        try:
            bot.run(config.discord_token)
        except discord.LoginFailure:
            logger.warning(
                "Discord login failed (invalid DISCORD_TOKEN); "
                "continuing without the gateway"
            )
        except Exception:
            logger.warning(
                "Discord gateway error; continuing without the gateway",
                exc_info=True,
            )

    # Constructed before the client so its ``wakeup`` can be wired into
    # the router (SC-004: a cadence change ends the current wait).
    scheduler = Scheduler(state, flags, cycle)
    bot = BotalertClient(config, state, flags, wakeup=scheduler.wakeup)
    scheduler.start()
    bot_thread = threading.Thread(
        target=_bot_thread, name="botalert-bot", daemon=True
    )
    bot_thread.start()
    logger.info("scheduler armed; bot + Flask starting on port %d", config.port)

    app = create_app(config, state, scheduler, flags)

    def _shutdown(signum: int, _frame: FrameType | None) -> None:
        """SIGINT/SIGTERM: stop the scheduler and the bot, then exit 0."""
        logger.info("signal %d received; stopping scheduler and bot", signum)
        scheduler.stop()
        bot.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        run_app(app, config)
    finally:
        scheduler.stop()  # idempotent (the signal path already stopped it)
        bot.stop()  # idempotent (schedules Client.close on the bot loop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
