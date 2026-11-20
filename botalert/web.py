"""Flask app factory (tasks T023/T042).

One Flask app serves the management API blueprint (``/api/*``) and the
static dashboard on the single configurable port (env ``PORT``,
default 8080). The built-in Werkzeug server runs with ``threaded=True``
(research.md §5 accepted trade-off: concurrent API requests never block
the scheduler thread, and the scheduler never blocks on the API). The
dashboard shell (``GET /``) and its assets (``/static/*``,
``botalert/static``) are token-free — the shell holds no data and
changes no state; only ``/api/*`` is gated (research.md §8). This
factory returns the app so tests can use a test client.
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask, Response, send_from_directory

from botalert.api import SchedulerWakeup, create_api_blueprint
from botalert.config import Config
from botalert.models import RuntimeFlags
from botalert.state import StateStore

#: ``botalert/static`` — the same directory Flask's default
#: ``static_folder`` points at, named explicitly for the root route.
_STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(
    config: Config,
    state: StateStore,
    scheduler: SchedulerWakeup,
    flags: RuntimeFlags,
) -> Flask:
    """Create the Flask app with the ``/api/*`` blueprint and dashboard
    shell registered."""
    app = Flask(__name__)
    app.register_blueprint(create_api_blueprint(config, state, scheduler, flags))

    @app.get("/")
    def index() -> Response:
        """The static dashboard shell (FR-019, US4); public by design."""
        return send_from_directory(_STATIC_DIR, "index.html")

    return app


def run_app(app: Flask, config: Config) -> None:
    """Serve on ``0.0.0.0:$PORT`` (default 8080) until interrupted.

    ``host="0.0.0.0"`` so a container operator can map ANY host port
    with ``-p <host>:<container>`` (FR-013, US4-3). Blocks the calling
    thread (run on the main thread by the entrypoint).
    """
    app.run(host="0.0.0.0", port=config.port, threaded=True)


__all__ = ["create_app", "run_app"]
