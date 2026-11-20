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
- ``PATCH /api/products?url=<url>`` — modify by URL query param (FR-004):
  any of ``label`` / ``in_stock_indicator`` / ``out_of_stock_indicator``,
  at least one required, empty-string indicator clears it; ``200`` updated
  product; ``400 invalid_label`` / ``invalid_indicator`` / (empty body);
  ``404 not_found``. Every provided value is validated before the store is
  touched, so a rejection applies nothing; never wakes the scheduler.
- ``DELETE /api/products?url=<url>`` — remove (FR-002); ``204`` no body
  (failure counters vanish with the row); ``404 not_found``; never wakes
  the scheduler.
- ``POST /api/check-now`` — ``202 {"status": "cycle_scheduled"}`` plus
  scheduler wakeup, or ``409 cycle_in_progress`` (FR-018: no second
  cycle is started while one is in flight).
- ``GET /api/config`` — current cadence plus the configured floor and
  ceiling (FR-005).
- ``PUT /api/config`` — change the cadence (SC-004): only a JSON number
  (never a bool or a numeric string) within the inclusive ``[min, max]``
  is accepted; success persists, wakes the scheduler (new interval from
  the next cycle) and echoes the config shape; failure returns the fixed
  ``400 invalid_cadence`` and leaves state and the scheduler untouched.

All non-2xx responses use the contract error shape
``{"error": {"code": ..., "message": ...}}``. The scheduler seam only
needs ``wakeup()`` (a protocol, so the blueprint never depends on the
scheduler implementation).
"""

from __future__ import annotations

import hmac
import math
from typing import Protocol

from flask import Blueprint, Response, jsonify, request

from botalert.config import Config
from botalert.models import DuplicateUrlError, Product, RuntimeFlags, ValidationError
from botalert.state import StateStore, UNSET

#: Exact contract message for a normalized-URL collision (US4-6).
_DUPLICATE_URL_MESSAGE = "A product with this URL is already monitored."

#: ``not_found`` message for a URL no product is monitored at (US4).
_NOT_FOUND_MESSAGE = "No product is monitored at this URL."

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

    @bp.patch("/products")
    def modify_product() -> Response | tuple[Response, int]:
        """Modify a product by its URL query param (FR-004, US4).

        Body: any of ``label`` / ``in_stock_indicator`` /
        ``out_of_stock_indicator``; at least one required; an
        empty-string indicator clears it. The URL itself is immutable
        (changing it = remove + add). Every provided value is validated
        before the store is touched, so a 400 applies nothing (one
        critical section, data-model.md §6). Never wakes the scheduler.
        """
        url = request.args.get("url")
        payload = request.get_json(silent=True)
        body: dict[str, object] = payload if isinstance(payload, dict) else {}
        raw_label = body.get("label")
        raw_in = body.get("in_stock_indicator")
        raw_out = body.get("out_of_stock_indicator")
        if (
            raw_label is None
            and raw_in is None
            and raw_out is None
        ):
            return _error(
                "invalid_label",
                "provide at least one field to update: label, "
                "in_stock_indicator or out_of_stock_indicator.",
                400,
            )
        if raw_label is not None and not isinstance(raw_label, str):
            return _error(
                "invalid_label",
                "label must be a string (1-100 characters).",
                400,
            )
        if raw_in is not None and not isinstance(raw_in, str):
            return _error(
                "invalid_indicator",
                "in_stock_indicator must be a string (at most 200 characters).",
                400,
            )
        if raw_out is not None and not isinstance(raw_out, str):
            return _error(
                "invalid_indicator",
                "out_of_stock_indicator must be a string (at most 200 characters).",
                400,
            )
        try:
            product = state.modify_product(
                url or "",
                label=raw_label if raw_label is not None else UNSET,
                in_stock_indicator=raw_in if raw_in is not None else UNSET,
                out_of_stock_indicator=raw_out
                if raw_out is not None
                else UNSET,
            )
        except ValidationError as exc:
            return _error(_validation_code(exc), str(exc), 400)
        if product is None:
            return _error("not_found", _NOT_FOUND_MESSAGE, 404)
        return jsonify(product.api_dict())

    @bp.delete("/products")
    def delete_product() -> Response | tuple[Response, int]:
        """Remove a product by its URL query param (FR-002, US4).

        No body. ``204`` with no body on success — the failure counters
        vanish with the row (data-model.md §6). Never wakes the scheduler.
        """
        url = request.args.get("url")
        removed = state.remove_product(url) if url else None
        if removed is None:
            return _error("not_found", _NOT_FOUND_MESSAGE, 404)
        return Response(status=204)

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

    @bp.get("/config")
    def get_config() -> Response:
        """Current cadence plus the configured floor and ceiling (FR-005)."""
        return jsonify(
            {
                "cadence_minutes": state.get_cadence(),
                "min_cadence_minutes": config.min_cadence_minutes,
                "max_cadence_minutes": config.max_cadence_minutes,
            }
        )

    @bp.put("/config")
    def put_config() -> Response | tuple[Response, int]:
        """Change the cadence (SC-004): validate, persist, wake, echo.

        Body: ``{"cadence_minutes": <number>}``. Only a JSON number
        (int/float, never a bool or a numeric string) within the
        inclusive configured ``[min, max]`` is accepted. On success the
        scheduler is woken so the new interval applies from the next
        cycle; on failure the store is untouched and nothing is woken,
        and the fixed ``invalid_cadence`` message is returned (never
        routed through ``_validation_code``).
        """
        payload = request.get_json(silent=True)
        body: dict[str, object] = payload if isinstance(payload, dict) else {}
        value = body.get("cadence_minutes")
        message = (
            f"Cadence must be at least {float(config.min_cadence_minutes)} "
            f"minutes (minimum) and at most "
            f"{float(config.max_cadence_minutes)} minutes (maximum)."
        )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return _error("invalid_cadence", message, 400)
        if not math.isfinite(value):
            return _error("invalid_cadence", message, 400)
        if (
            value < config.min_cadence_minutes
            or value > config.max_cadence_minutes
        ):
            return _error("invalid_cadence", message, 400)
        minutes = float(value)
        state.set_cadence(minutes)
        scheduler.wakeup()  # SC-004: new interval from the next cycle
        return jsonify(
            {
                "cadence_minutes": state.get_cadence(),
                "min_cadence_minutes": config.min_cadence_minutes,
                "max_cadence_minutes": config.max_cadence_minutes,
            }
        )

    return bp


__all__ = ["SchedulerWakeup", "create_api_blueprint"]
