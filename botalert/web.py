"""Flask app factory (task T023).

One Flask app serves the management API blueprint (``/api/*``) on the
single configurable port (env ``PORT``, default 8080). The built-in
Werkzeug server runs with ``threaded=True`` (research.md §5 accepted
trade-off: concurrent API requests never block the scheduler thread,
and the scheduler never blocks on the API). The static dashboard is
served by the same app and lands in US4 (task T042); this factory
returns the app so tests can use a test client.
"""

from __future__ import annotations

from flask import Flask

from botalert.api import SchedulerWakeup, create_api_blueprint
from botalert.config import Config
from botalert.models import RuntimeFlags
from botalert.state import StateStore


def create_app(
    config: Config,
    state: StateStore,
    scheduler: SchedulerWakeup,
    flags: RuntimeFlags,
) -> Flask:
    """Create the Flask app with the ``/api/*`` blueprint registered."""
    app = Flask(__name__)
    app.register_blueprint(create_api_blueprint(config, state, scheduler, flags))
    return app


def run_app(app: Flask, config: Config) -> None:
    """Serve on ``0.0.0.0:$PORT`` (default 8080) until interrupted.

    ``host="0.0.0.0"`` so a container operator can map ANY host port
    with ``-p <host>:<container>`` (FR-013, US4-3). Blocks the calling
    thread (run on the main thread by the entrypoint).
    """
    app.run(host="0.0.0.0", port=config.port, threaded=True)


__all__ = ["create_app", "run_app"]
