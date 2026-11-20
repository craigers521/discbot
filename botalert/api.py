"""Management API blueprint (contracts/management-api.md, US1 slice).

Serves ``/api/*`` from the shared Flask app:

- ``GET /api/health`` — public liveness probe: no auth, no watch-list
  data, no secrets (operator/container health checks).
- Every other route requires header ``X-Botalert-Token``, compared
  constant-time with ``hmac.compare_digest`` BEFORE any state access
  (SC-008: rejected requests alter nothing); missing/invalid →
  ``401 {"error": {"code": "unauthorized", ...}}``.
- ``GET /api/products`` — insertion-ordered watch list in the contract
  product representation (``failure_notified`` never exposed).
- ``POST /api/products`` — add; ``201`` created product; ``400
  invalid_url`` / ``invalid_label`` / ``invalid_indicator``; ``409
  duplicate_url``; a successful add wakes the scheduler (SC-001).
- ``POST /api/check-now`` — ``202 {"status": "cycle_scheduled"}`` plus
  scheduler wakeup, or ``409 cycle_in_progress`` (FR-018: no second
  cycle is started while one is in flight).

All non-2xx responses use the contract error shape
``{"error": {"code": ..., "message": ...}}``. The scheduler seam only
needs ``wakeup()`` (a protocol, so the blueprint never depends on the
scheduler implementation).
"""

from __future__ import annotations

import hmac
from typing import Protocol

from flask import Blueprint, Response, jsonify, request

from botalert.config import Config
from botalert.models import DuplicateUrlError, Product, RuntimeFlags, ValidationError
from botalert.state import StateStore

#: Exact contract message for a normalized-URL collision (US4-6).
_DUPLICATE_URL_MESSAGE = "A product with this URL is already monitored."

_HEALTH_PATH = "/api/health"
_TOKEN_HEADER = "X-Botalert-Token"


class SchedulerWakeup(Protocol):
    """The scheduler seam the API needs: end the current wait early.

    Satisfied by both ``botalert.scheduler.Scheduler`` and
    ``tests.fakes.FakeScheduler`` (SC-001).
    """

    def wakeup(self) -> None: ...


def _error(code: str, message: str, status: int) -> tuple[Response, int]:
    """Contract error shape for every non-2xx API response."""
    return jsonify({"error": {"code": code, "message": message}}), status


def _validation_code(exc: ValidationError) -> str:
    """Map a data-model.md §2 field failure to the contract error code."""
    if exc.field == "url":
        return "invalid_url"
    if exc.field == "label":
        return "invalid_label"
    return "invalid_indicator"


def create_api_blueprint(
    config: Config,
    state: StateStore,
    scheduler: SchedulerWakeup,
    flags: RuntimeFlags,
) -> Blueprint:
    """Build the ``/api/*`` blueprint over the shared store and flags.

    ``config.token`` (env ``BOTALERT_TOKEN``) is the shared secret; the
    auth check runs in ``before_request`` so it precedes ANY state
    access on every route except the public health probe.
    """
    bp = Blueprint("api", __name__, url_prefix="/api")

    @bp.before_request
    def _auth_guard() -> tuple[Response, int] | None:
        """Constant-time token check before any state access (SC-008)."""
        if request.path == _HEALTH_PATH:
            return None
        provided = request.headers.get(_TOKEN_HEADER)
        if provided is None or not hmac.compare_digest(provided, config.token):
            return _error(
                "unauthorized",
                "missing or invalid X-Botalert-Token header.",
                401,
            )
        return None

    @bp.get("/health")
    def health() -> Response:
        """Public liveness probe: status + runtime flags only, never data."""
        return jsonify(
            {
                "status": "ok",
                "gateway_connected": flags.gateway_connected,
                "cycle_in_progress": flags.cycle_in_progress,
            }
        )

    @bp.get("/products")
    def list_products() -> Response:
        """Insertion-ordered watch list, contract product shape (FR-003)."""
        return jsonify(
            {"products": [product.api_dict() for product in state.get_products()]}
        )

    @bp.post("/products")
    def add_product() -> Response | tuple[Response, int]:
        """Add a product (FR-001, US4-4/5/6); wakes the scheduler on 201.

        Body: ``url`` (required string) plus optional ``label`` /
        ``in_stock_indicator`` / ``out_of_stock_indicator`` strings.
        Non-string values for any of them are rejected with the
        matching contract code before the store is touched.
        """
        payload = request.get_json(silent=True)
        body: dict[str, object] = payload if isinstance(payload, dict) else {}
        url = body.get("url")
        label = body.get("label")
        in_indicator = body.get("in_stock_indicator")
        out_indicator = body.get("out_of_stock_indicator")

        if not isinstance(url, str):
            return _error(
                "invalid_url",
                "url must be a string (absolute http(s) URL).",
                400,
            )
        if label is not None and not isinstance(label, str):
            return _error(
                "invalid_label",
                "label must be a string (1-100 characters).",
                400,
            )
        if in_indicator is not None and not isinstance(in_indicator, str):
            return _error(
                "invalid_indicator",
                "in_stock_indicator must be a string (at most 200 characters).",
                400,
            )
        if out_indicator is not None and not isinstance(out_indicator, str):
            return _error(
                "invalid_indicator",
                "out_of_stock_indicator must be a string (at most 200 characters).",
                400,
            )

        try:
            product: Product = state.add_product(
                url, label, in_indicator, out_indicator
            )
        except ValidationError as exc:
            return _error(_validation_code(exc), str(exc), 400)
        except DuplicateUrlError:
            return _error("duplicate_url", _DUPLICATE_URL_MESSAGE, 409)

        scheduler.wakeup()  # SC-001: a fresh watch list is not left waiting
        return jsonify(product.api_dict()), 201

    @bp.post("/check-now")
    def check_now() -> Response | tuple[Response, int]:
        """Trigger an immediate cycle (SC-001/SC-002 verifiability).

        No body. ``202`` when the scheduler is woken; ``409`` when a
        cycle is already in flight (no second cycle is started, FR-018).
        """
        if flags.cycle_in_progress:
            return _error(
                "cycle_in_progress",
                "a check cycle is already in progress; try again after it finishes.",
                409,
            )
        scheduler.wakeup()
        return jsonify({"status": "cycle_scheduled"}), 202

    return bp


__all__ = ["SchedulerWakeup", "create_api_blueprint"]
